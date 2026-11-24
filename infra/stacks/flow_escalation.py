"""GuardIA-Escalation-Flow (Level 2, D25) · T5, tasks 6.1-6.5.

    SetEscalated ─► ResolveOnCall ($.oncall) ─► [InvokeAgentDiagnose, T8] ─► SendEscalation initial
                                                  └ Teams fails 3 times ─► NotificationFailed (Fail)
    ─► PlanReminder ($.timing) ─► DeadlinePassed?
         resolved by the recovery itself ─► SendResolved
         no  ─► WaitForResolution (token, wait_seconds) ── success ─► MarkEscalationResolved
                  └ timeout ─► SendReminder ─► PlanReminder (loop)
         yes ─► SetSlaBreached ─► SendSlaBreached ─► WaitAfterDeadline (token)
                  ├ success ─► MarkEscalationResolved
                  └ timeout ─► ClosedUnresolved (stays SLA_BREACHED; no more reminders, 6.2)
    MarkEscalationResolved ─► SendResolved ─► EscalationResolved

The waiting states store a task token in the incident (`StoreTaskToken`).
`ResolveOnSuccess` answers it when a later execution of the process succeeds.
Only `initial` fails the execution if Teams is down; a reminder, SLA notice, or
resolution message that fails is recorded (`NOTIFICATION_FAILED`) and skipped.

The Lambdas live here, with their permissions, and are registered in
`ctx.functions` for the alarms (T6). `build_escalation` returns the chain that
starts at SetEscalated; FlowStack makes it the state machine that the incident
flow runs with `escalate(...)` (T4).
"""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from components.function import GuardiaFunction
from config import TEAMS_ALERTS_SECRET_NAME
from guardia_common.incidents import SUCCEEDED_PATTERN

from .detection_stack import event_pattern
from .flow_agent import invoke_agent_state
from .flow_context import FlowContext

ESCALATED_CHAIN_START = "SetEscalated"


def _functions(ctx: FlowContext) -> dict[str, GuardiaFunction]:
    """The branch's Lambdas, each with only the permissions it uses."""
    if ctx.oncall is None:
        raise ValueError("the escalation branch needs ctx.oncall (D19: GUARDIA_ONCALL_EMAIL)")
    scope, data, layer = ctx.scope, ctx.data, ctx.layer

    def make(key: str, name: str, env: dict[str, str], description: str, **kwargs):
        function = GuardiaFunction(
            scope, key, name=name, layer=layer, environment=env, description=description, **kwargs
        )
        return ctx.add_function(key, function)

    resolve = make(
        "ResolveOnCall",
        "resolve_on_call",
        {**data.table_env("OnCallSchedule", "IncidentEvents"), **ctx.oncall.lambda_environment()},
        "GuardIA ResolveOnCall (6.1): who is on call (D19)",
    )
    data.grant_items(resolve.function, "OnCallSchedule", "dynamodb:GetItem")
    data.grant_append_events(resolve.function)
    data.grant_read_thresholds(resolve.function)

    send = make(
        "SendEscalation",
        "send_escalation",
        {
            **data.table_env("Incidents", "IncidentEvents"),
            "TEAMS_ALERTS_SECRET_ID": TEAMS_ALERTS_SECRET_NAME,
        },
        "GuardIA SendEscalation (6.3): Teams messages, 3 attempts",
        timeout_seconds=60,  # 3 posts with 1 s and 2 s of backoff
    )
    data.grant_items(send.function, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem")
    data.grant_read_events(send.function)
    data.grant_append_events(send.function)
    data.grant_read_thresholds(send.function)
    data.grant_read_secret(send.function, TEAMS_ALERTS_SECRET_NAME)

    plan = make(
        "PlanReminder",
        "plan_reminder",
        data.table_env("Incidents"),
        "GuardIA PlanReminder (6.5): next wait and deadline check",
    )
    data.grant_items(plan.function, "Incidents", "dynamodb:GetItem")
    data.grant_read_thresholds(plan.function)

    store = make(
        "StoreTaskToken",
        "store_task_token",
        data.table_env("Incidents"),
        "GuardIA StoreTaskToken (6.4): saves the wait token in the incident",
    )
    data.grant_items(store.function, "Incidents", "dynamodb:UpdateItem")

    resolve_on_success = make(
        "ResolveOnSuccess",
        "resolve_on_success",
        data.table_env("Incidents", "ProcessRegistry", "IncidentEvents"),
        "GuardIA ResolveOnSuccess (6.4): a later success closes the escalation",
    )
    data.grant_items(resolve_on_success.function, "Incidents", "dynamodb:Query")
    data.grant_items(resolve_on_success.function, "ProcessRegistry", "dynamodb:GetItem")
    data.grant_append_events(resolve_on_success.function)
    ctx.grant_task_response(resolve_on_success.function)

    events.Rule(
        scope,
        "SucceededToResolveOnSuccess",
        description="GuardIA: successful executions close escalated incidents",
        event_pattern=event_pattern(SUCCEEDED_PATTERN),
        targets=[
            targets.LambdaFunction(
                resolve_on_success.function,
                dead_letter_queue=resolve_on_success.dlq,
                retry_attempts=3,
                max_event_age=Duration.hours(2),
            )
        ],
    )
    return {
        "ResolveOnCall": resolve,
        "SendEscalation": send,
        "PlanReminder": plan,
        "StoreTaskToken": store,
        "ResolveOnSuccess": resolve_on_success,
    }


def build_escalation(ctx: FlowContext) -> sfn.IChainable:
    """The definition of GuardIA-Escalation-Flow (D25). Input: `incident_id`,
    `classification`, and `policy` (`policy.decision` is REMEDIATE_AND_ESCALATE
    when the recovery runs next to it)."""
    fn = _functions(ctx)
    scope = ctx.scope
    incident_id = sfn.JsonPath.string_at("$.incident_id")

    def send(state_name: str, mode: str, *, parallel: bool = False) -> tasks.LambdaInvoke:
        return ctx.invoke(
            state_name,
            fn["SendEscalation"],
            {
                "incident_id": incident_id,
                "mode": mode,
                "recipients": sfn.JsonPath.object_at("$.oncall"),
                "parallel": parallel,
            },
            "$.notification",
        )

    def wait_for_token(state_name: str, timeout_path: str) -> tasks.LambdaInvoke:
        """Stores a fresh task token and waits for ResolveOnSuccess or the timeout."""
        return tasks.LambdaInvoke(
            scope,
            f"{state_name}-Task",
            state_name=state_name,
            lambda_function=fn["StoreTaskToken"].function,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {"incident_id": incident_id, "task_token": sfn.JsonPath.task_token}
            ),
            task_timeout=sfn.Timeout.at(timeout_path),
            result_path="$.resolution",
            retry_on_service_exceptions=True,
        )

    set_escalated = ctx.update_status(
        ESCALATED_CHAIN_START, "ESCALATED", details={"reason": "escalation_started"}
    )
    resolve_on_call = ctx.invoke(
        "ResolveOnCall", fn["ResolveOnCall"], {"incident_id": incident_id}, "$.oncall"
    )
    plan = ctx.invoke("PlanReminder", fn["PlanReminder"], {"incident_id": incident_id}, "$.timing")

    # First message: Teams down after 3 attempts ends the execution, which raises
    # the alarm (D21). Few minutes left: the card says the recovery runs in parallel.
    notification_failed = sfn.Fail(
        scope,
        "NotificationFailed",
        error="NotificationFailed",
        cause="Teams did not accept the escalation after 3 attempts",
    )
    initial = send("SendEscalation", "initial")
    initial_parallel = send("SendEscalationParallel", "initial", parallel=True)
    for state in (initial, initial_parallel):
        state.add_catch(notification_failed, errors=["States.ALL"], result_path="$.error")
        state.next(plan)
    first_message = (
        sfn.Choice(scope, "RecoveryInParallel")
        .when(
            sfn.Condition.and_(
                sfn.Condition.is_present("$.policy.decision"),
                sfn.Condition.string_equals("$.policy.decision", "REMEDIATE_AND_ESCALATE"),
            ),
            initial_parallel,
        )
        .otherwise(initial)
    )

    # ------------------------------------------------------------- resolved
    done = sfn.Succeed(scope, "EscalationResolved")
    send_resolved = send("SendResolved", "resolved")
    send_resolved.add_catch(done, errors=["States.ALL"], result_path="$.error")
    send_resolved.next(done)
    mark_resolved = ctx.update_status(
        "MarkEscalationResolved",
        "RESOLVED",
        details={"resolved_by": sfn.JsonPath.string_at("$.resolution.resolved_by")},
    ).next(send_resolved)

    # ----------------------------------------------------- deadline passed
    closed_unresolved = sfn.Succeed(
        scope, "ClosedUnresolved", comment="Stays SLA_BREACHED; no more reminders (6.2)"
    )
    wait_after_deadline = wait_for_token("WaitAfterDeadline", "$.timing.post_deadline_seconds")
    wait_after_deadline.add_catch(
        closed_unresolved, errors=["States.Timeout"], result_path=sfn.JsonPath.DISCARD
    )
    wait_after_deadline.next(mark_resolved)
    send_sla = send("SendSlaBreached", "sla_breached")
    send_sla.add_catch(wait_after_deadline, errors=["States.ALL"], result_path="$.error")
    send_sla.next(wait_after_deadline)
    breached = ctx.update_status("SetSlaBreached", "SLA_BREACHED").next(send_sla)

    # ------------------------------------------------------ before deadline
    send_reminder = send("SendReminder", "reminder")
    send_reminder.add_catch(plan, errors=["States.ALL"], result_path="$.error")
    send_reminder.next(plan)
    wait_for_resolution = wait_for_token("WaitForResolution", "$.timing.wait_seconds")
    wait_for_resolution.add_catch(
        send_reminder, errors=["States.Timeout"], result_path=sfn.JsonPath.DISCARD
    )
    wait_for_resolution.next(mark_resolved)

    # With the recovery in parallel, the incident can be resolved by the recovery
    # itself: nobody answers the token, so the plan notices it (no more reminders).
    plan.next(
        sfn.Choice(scope, "DeadlinePassed")
        .when(sfn.Condition.boolean_equals("$.timing.already_resolved", True), send_resolved)
        .when(sfn.Condition.boolean_equals("$.timing.deadline_passed", True), breached)
        .otherwise(wait_for_resolution)
    )
    # T8: the agent writes the diagnosis of the card. If it fails, the message goes
    # out without it; it never delays the escalation beyond its own time limit (13.1).
    diagnose = invoke_agent_state(ctx, "InvokeAgentDiagnose", "diagnose", first_message)
    return sfn.Chain.start(set_escalated).next(resolve_on_call).next(diagnose or first_message)
