"""Recovery branch of GuardIA-Incident-Flow (Level 1) · T4 (tasks 5.1–5.5).

    RemediationPolicy ─► PolicyDecision?
       ESCALATE ───────────────► EscalateByPolicy ──────────────────────────► IncidentHandled
       REMEDIATE ──────────────► recovery loop
       REMEDIATE_AND_ESCALATE ─► Parallel [ recovery loop (inside the branch) , EscalateInParallel ]

    recovery loop:
       WaitRetryAfter ($.policy.wait_seconds, 5.2)
       ─► RedriveOrRestart ($.recovery)      rejected / nothing to recover ─► escalate (or stop)
       ─► StartPolling ─► WaitBeforePoll (3 min) ─► PollExecution ($.poll)
            done and SUCCEEDED ─► MarkResolved (RESOLVED, resolved_by=guardia_recovery)
            done and failed    ─► ask the policy again (another attempt, or escalate)
            still running      ─► next poll, up to MAX_POLLS, then treated as failed

The loop is built twice: once in the main flow, once inside the Parallel
branch (Step Functions does not share states between a Parallel branch and the
rest of the graph). Inside the branch the policy is asked with
`escalation_running=true`, and anything other than REMEDIATE ends the branch,
because the escalation is already under way next to it.

Escalation itself is the GuardIA-Escalation-Flow state machine (T5, D25):
`escalate(state_name, next_state)` returns a state that runs it and waits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aws_cdk import Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from components.function import GuardiaFunction

from .flow_context import FlowContext

POLL_INTERVAL_SECONDS = 180  # `redrive-poll-interval-seconds`; the demo environments shorten it
MAX_POLLS = 10  # 30 minutes, longer than the 20-minute remediation window
POLL_BUDGET_SECONDS = MAX_POLLS * POLL_INTERVAL_SECONDS
REMEDIATION_TAG_CONDITION = {
    "StringEquals": {"aws:ResourceTag/guardia:remediation": "auto"}
}  # 12.1, 12.4; executions inherit the state machine tags
RECOVERY_REJECTED = ["RemediationNotAllowedError", "AttemptsExhaustedError"]

Escalate = Callable[[str, sfn.IChainable], sfn.IChainable]


def poll_settings(ctx: FlowContext) -> tuple[int, int]:
    """(seconds between polls, maximum polls). A shorter interval keeps the same time
    budget (30 minutes), so a fast demo never gives up on a slower redrive."""
    interval = int(ctx.thresholds.get("redrive-poll-interval-seconds", POLL_INTERVAL_SECONDS))
    if interval < 1:
        raise ValueError("redrive-poll-interval-seconds must be at least 1")
    return interval, max(MAX_POLLS, -(-POLL_BUDGET_SECONDS // interval))


@dataclass
class _Functions:
    policy: lambda_.IFunction
    redrive: lambda_.IFunction
    poll: lambda_.IFunction


def build_recovery(ctx: FlowContext, escalate: Escalate) -> sfn.IChainable:
    fns = _functions(ctx)
    handled = sfn.Succeed(ctx.scope, "IncidentHandled")

    policy = _policy(ctx, fns, "RemediationPolicy", escalation_running=False)
    main_loop = _recovery_loop(
        ctx, fns, prefix="",
        after_failure=policy,
        give_up=escalate("EscalateAfterRecoveryRejected", handled),
        done=handled,
    )  # fmt: skip

    parallel = sfn.Parallel(
        ctx.scope,
        "RemediateAndEscalate",
        comment="6.1: little time left, recover and escalate at the same time",
        result_path=sfn.JsonPath.DISCARD,
    )
    parallel.branch(_parallel_recovery_branch(ctx, fns))
    parallel.branch(_parallel_escalation_branch(ctx, escalate))
    parallel.next(handled)

    return policy.next(
        sfn.Choice(ctx.scope, "PolicyDecision")
        .when(_decision_is("REMEDIATE"), main_loop)
        .when(_decision_is("REMEDIATE_AND_ESCALATE"), parallel)
        .otherwise(escalate("EscalateByPolicy", handled))
    )


# --------------------------------------------------------------------------- states
def _decision_is(decision: str) -> sfn.Condition:
    return sfn.Condition.string_equals("$.policy.decision", decision)


def _policy(
    ctx: FlowContext, fns: _Functions, state_name: str, *, escalation_running: bool
) -> tasks.LambdaInvoke:
    return tasks.LambdaInvoke(
        ctx.scope,
        f"{state_name}-Task",
        state_name=state_name,
        lambda_function=fns.policy,
        payload=sfn.TaskInput.from_object(
            {
                "incident_id": sfn.JsonPath.string_at("$.incident_id"),
                "escalation_running": escalation_running,
            }
        ),
        payload_response_only=True,
        result_path="$.policy",
        retry_on_service_exceptions=True,
    )


def _recovery_loop(
    ctx: FlowContext,
    fns: _Functions,
    *,
    prefix: str,
    after_failure: sfn.IChainable,
    give_up: sfn.IChainable,
    done: sfn.IChainable,
) -> sfn.IChainable:
    scope = ctx.scope
    redrive = tasks.LambdaInvoke(
        scope,
        f"{prefix}RedriveOrRestart-Task",
        state_name=f"{prefix}RedriveOrRestart",
        lambda_function=fns.redrive,
        payload=sfn.TaskInput.from_object({"incident_id": sfn.JsonPath.string_at("$.incident_id")}),
        payload_response_only=True,
        result_path="$.recovery",
        retry_on_service_exceptions=True,
    )
    redrive.add_catch(give_up, errors=RECOVERY_REJECTED, result_path="$.recovery_error")

    poll = tasks.LambdaInvoke(
        scope,
        f"{prefix}PollExecution-Task",
        state_name=f"{prefix}PollExecution",
        lambda_function=fns.poll,
        payload=sfn.TaskInput.from_object(
            {
                "incident_id": sfn.JsonPath.string_at("$.incident_id"),
                "recovery_execution_arn": sfn.JsonPath.string_at(
                    "$.recovery.recovery_execution_arn"
                ),
            }
        ),
        payload_response_only=True,
        result_path="$.poll",
        retry_on_service_exceptions=True,
    )
    poll_seconds, max_polls = poll_settings(ctx)
    wait_before_poll = sfn.Wait(
        scope,
        f"{prefix}WaitBeforePoll",
        time=sfn.WaitTime.duration(Duration.seconds(poll_seconds)),
    )
    count_poll = sfn.Pass(
        scope,
        f"{prefix}CountPoll",
        parameters={"count": sfn.JsonPath.math_add(sfn.JsonPath.number_at("$.polls.count"), 1)},
        result_path="$.polls",
    )
    resolved = ctx.update_status(
        f"{prefix}MarkResolved", "RESOLVED", details={"resolved_by": "guardia_recovery"}
    ).next(done)

    after_poll = (
        sfn.Choice(scope, f"{prefix}RecoveryFinished?")
        .when(
            sfn.Condition.and_(
                sfn.Condition.boolean_equals("$.poll.done", True),
                sfn.Condition.boolean_equals("$.poll.succeeded", True),
            ),
            resolved,
        )
        .when(sfn.Condition.boolean_equals("$.poll.done", True), after_failure)
        .when(
            sfn.Condition.number_less_than("$.polls.count", max_polls),
            count_poll.next(wait_before_poll),
        )
        .otherwise(after_failure)
    )
    start_polling = sfn.Pass(
        scope, f"{prefix}StartPolling", result=sfn.Result.from_object({"count": 1}),
        result_path="$.polls",
    )  # fmt: skip
    wait_before_poll.next(poll).next(after_poll)

    recovered = (
        sfn.Choice(scope, f"{prefix}RecoveryStarted?")
        .when(
            sfn.Condition.boolean_equals("$.recovery.recovered", True),
            start_polling.next(wait_before_poll),
        )
        .otherwise(give_up)  # MISSED_RUN-like incidents with nothing to relaunch
    )
    return (
        sfn.Wait(
            scope,
            f"{prefix}WaitRetryAfter",
            time=sfn.WaitTime.seconds_path("$.policy.wait_seconds"),
        )
        .next(redrive)
        .next(recovered)
    )


def _parallel_recovery_branch(ctx: FlowContext, fns: _Functions) -> sfn.IChainable:
    stopped = sfn.Succeed(ctx.scope, "ParallelRecoveryStopped")
    policy_again = _policy(ctx, fns, "ParallelRemediationPolicy", escalation_running=True)
    loop = _recovery_loop(
        ctx, fns, prefix="Parallel",
        after_failure=policy_again, give_up=stopped,
        done=sfn.Succeed(ctx.scope, "ParallelRecoveryResolved"),
    )  # fmt: skip
    policy_again.next(
        sfn.Choice(ctx.scope, "ParallelPolicyDecision")
        .when(_decision_is("REMEDIATE"), loop)
        .otherwise(stopped)
    )
    return loop


def _parallel_escalation_branch(ctx: FlowContext, escalate: Escalate) -> sfn.IChainable:
    # A failed escalation (for example Teams down, D21) must not cancel the
    # recovery next to it; the escalation flow's own failure raises the alarm.
    done = sfn.Succeed(ctx.scope, "ParallelEscalationDone")
    task = escalate("EscalateInParallel", done)
    if isinstance(task, sfn.TaskStateBase):
        task.add_catch(
            sfn.Pass(ctx.scope, "ParallelEscalationFailed").next(done),
            errors=[sfn.Errors.ALL],
            result_path="$.escalation_error",
        )
    return task


# ------------------------------------------------------------------------ Lambdas
def _functions(ctx: FlowContext) -> _Functions:
    data, scope = ctx.data, ctx.scope
    arn = f"arn:{scope.partition}:states:{scope.region}:{scope.account}"
    state_machines, executions = f"{arn}:stateMachine:*", f"{arn}:execution:*"

    policy = ctx.add_function(
        "RemediationPolicy",
        GuardiaFunction(
            scope, "RemediationPolicy", name="remediation_policy", layer=ctx.layer,
            environment=data.table_env("Incidents", "IncidentEvents"),
            description="GuardIA RemediationPolicy (5.1): fixed rules decide",
        ),
    ).function  # fmt: skip
    data.grant_items(policy, "Incidents", "dynamodb:GetItem")
    data.grant_append_events(policy)
    data.grant_read_thresholds(policy)

    redrive = ctx.add_function(
        "RedriveOrRestart",
        GuardiaFunction(
            scope, "RedriveOrRestart", name="redrive_or_restart", layer=ctx.layer,
            environment=data.table_env("Incidents", "IncidentEvents"),
            description="GuardIA RedriveOrRestart (5.2): only remediation=auto",
        ),
    ).function  # fmt: skip
    data.grant_items(redrive, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem")
    data.grant_append_events(redrive)
    redrive.add_to_role_policy(
        iam.PolicyStatement(
            sid="ReadProcess",
            actions=["states:ListTagsForResource", "states:DescribeStateMachine"],
            resources=[state_machines],
        )
    )
    redrive.add_to_role_policy(
        iam.PolicyStatement(
            sid="ReadExecution",
            # the history tells whether a redrive would rerun the root cause
            actions=["states:DescribeExecution", "states:GetExecutionHistory"],
            resources=[executions],
        )
    )
    redrive.add_to_role_policy(
        iam.PolicyStatement(
            sid="RedriveOnlyRemediationAuto",
            actions=["states:RedriveExecution"],
            resources=[executions],
            conditions=REMEDIATION_TAG_CONDITION,
        )
    )
    redrive.add_to_role_policy(
        iam.PolicyStatement(
            sid="RestartOnlyRemediationAuto",
            actions=["states:StartExecution"],
            resources=[state_machines],
            conditions=REMEDIATION_TAG_CONDITION,
        )
    )

    poll = ctx.add_function(
        "PollExecution",
        GuardiaFunction(
            scope, "PollExecution", name="poll_execution", layer=ctx.layer,
            environment=data.table_env("IncidentEvents"),
            description="GuardIA PollExecution (5.3): follows the recovered run",
        ),
    ).function  # fmt: skip
    data.grant_append_events(poll)
    poll.add_to_role_policy(
        iam.PolicyStatement(
            sid="ReadExecution", actions=["states:DescribeExecution"], resources=[executions]
        )
    )
    return _Functions(policy=policy, redrive=redrive, poll=poll)
