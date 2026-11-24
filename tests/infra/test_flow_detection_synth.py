"""Synth tests for GuardIA-Flow and GuardIA-Detection (Fase B · PR 2)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from config import MissingOnCallError, OnCallConfig, environment, oncall_from_environment
from guardia_common.incidents import (
    STATUS_CHANGE_PATTERN,
    SUCCEEDED_PATTERN,
    WATCHDOG_PATTERN,
)
from stacks.data_stack import AUDIT_DENIED_ACTIONS, DataStack
from stacks.detection_stack import DetectionStack, event_pattern
from stacks.flow_recovery import MAX_POLLS, REMEDIATION_TAG_CONDITION
from stacks.flow_stack import ESCALATION_FLOW_NAME, FLOW_NAME, FlowStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")
ONCALL = OnCallConfig(
    email="oncall@example.com", name="Ana Guardia", backup_email="backup@example.com"
)


@pytest.fixture(scope="module")
def stacks() -> dict[str, Template]:
    config = environment("dev")
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV, thresholds=config.thresholds, retain_data=False)
    flow = FlowStack(app, "F", env=ENV, data=data, oncall=ONCALL, retain_data=False)
    detection = DetectionStack(
        app, "X", env=ENV, data=data, flow=flow.state_machine,
        thresholds=config.thresholds, retain_data=False,
    )  # fmt: skip
    return {
        "flow": Template.from_stack(flow),
        "detection": Template.from_stack(detection),
        "flow_stack": flow,
        "detection_stack": detection,
    }


def _statements(template: Template) -> list[dict]:
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _actions(statement: dict) -> list[str]:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else actions


def _function_statements(template: Template, function_key: str) -> list[dict]:
    """The IAM statements of one Lambda's role (found by its construct name)."""
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if function_key in json.dumps(policy["Properties"]["Roles"]):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _function_env(template: Template, description_prefix: str) -> dict:
    for resource in template.find_resources("AWS::Lambda::Function").values():
        props = resource["Properties"]
        if props.get("Description", "").startswith(description_prefix):
            return props["Environment"]["Variables"]
    raise AssertionError(f"no function described as {description_prefix!r}")


# --------------------------------------------------------------------------- flow
def _definition(template: Template, name: str = FLOW_NAME) -> dict:
    """The parsed definition of one state machine; ARNs are replaced by "ARN"."""
    machines = template.find_resources("AWS::StepFunctions::StateMachine").values()
    for machine in machines:
        if machine["Properties"].get("StateMachineName", name) != name:
            continue
        body = machine["Properties"]["DefinitionString"]
        if isinstance(body, str):
            return json.loads(body)
        return json.loads("".join(p if isinstance(p, str) else "ARN" for p in body["Fn::Join"][1]))
    raise AssertionError(f"no state machine named {name}")


def test_flow_state_machine_starts_with_classify_incident(stacks) -> None:
    machines = stacks["flow"].find_resources("AWS::StepFunctions::StateMachine").values()
    names = {m["Properties"]["StateMachineName"] for m in machines}
    assert names == {FLOW_NAME, ESCALATION_FLOW_NAME}
    definition = _definition(stacks["flow"])
    assert definition["StartAt"] == "ClassifyIncident"
    assert definition["States"]["ClassifyIncident"]["ResultPath"] == "$.classification"


def test_flow_logs_errors_without_execution_data(stacks) -> None:
    logging = Match.object_like({"Level": "ERROR", "IncludeExecutionData": False})
    stacks["flow"].has_resource_properties(
        "AWS::StepFunctions::StateMachine", {"LoggingConfiguration": logging}
    )


@pytest.mark.req("4.1")
def test_classify_incident_reads_the_catalog_and_cannot_write_it(stacks) -> None:
    env = _function_env(stacks["flow"], "GuardIA ClassifyIncident")
    assert set(env) >= {"INCIDENTS_TABLE", "ERROR_CATALOG_TABLE", "INCIDENT_EVENTS_TABLE"}
    on_catalog = {
        action
        for statement in _statements(stacks["flow"])
        if "ErrorCatalog" in json.dumps(statement.get("Resource"))
        for action in _actions(statement)
    }
    assert on_catalog == {"dynamodb:Scan"}


# ---------------------------------------------------------------------- detection
@pytest.mark.req("1.1")
def test_discovery_and_watchdog_run_on_their_ssm_intervals(stacks) -> None:
    schedules = stacks["detection"].find_resources("AWS::Scheduler::Schedule")
    expressions = sorted(s["Properties"]["ScheduleExpression"] for s in schedules.values())
    assert expressions == ["rate(15 minutes)", "rate(5 minutes)"]


@pytest.mark.req("2.1")
def test_rules_use_the_same_patterns_as_the_local_bus(stacks) -> None:
    rules = stacks["detection"].find_resources("AWS::Events::Rule")
    patterns = [r["Properties"]["EventPattern"] for r in rules.values()]
    assert STATUS_CHANGE_PATTERN in patterns
    assert WATCHDOG_PATTERN in patterns
    for rule in rules.values():
        (target,) = rule["Properties"]["Targets"]
        assert target["RetryPolicy"]["MaximumRetryAttempts"] == 3
        assert "DeadLetterConfig" in target


def test_event_pattern_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="account"):
        event_pattern({"source": ["x"], "account": ["1"]})


def test_open_incident_starts_only_the_guardia_flow(stacks) -> None:
    env = _function_env(stacks["detection"], "GuardIA OpenIncident")
    assert "INCIDENT_FLOW_ARN" in env
    starts = [s for s in _statements(stacks["detection"]) if "states:StartExecution" in _actions(s)]
    assert len(starts) == 1
    assert "Fn::ImportValue" in json.dumps(starts[0]["Resource"])  # the flow ARN, not a wildcard


@pytest.mark.req("12.1")
def test_detection_cannot_redrive_or_change_processes(stacks) -> None:
    actions = {a for s in _statements(stacks["detection"]) for a in _actions(s)}
    forbidden = {
        "states:RedriveExecution", "states:StopExecution", "states:*",
        "states:UpdateStateMachine", "states:DeleteStateMachine",
        "events:PutRule", "events:DeleteRule", "scheduler:UpdateSchedule",
    }  # fmt: skip
    assert not actions & forbidden


@pytest.mark.req("10.3")
@pytest.mark.parametrize("name", ["flow", "detection"])
def test_no_lambda_may_update_or_delete_audit_events(stacks, name) -> None:
    for statement in _statements(stacks[name]):
        resources = json.dumps(statement.get("Resource"))
        if "IncidentEvents" in resources:
            assert not set(_actions(statement)) & {*AUDIT_DENIED_ACTIONS, "dynamodb:*"}


# ------------------------------------------------------------------------ T1
def test_escalation_flow_starts_with_the_t5_chain(stacks) -> None:
    escalation = _definition(stacks["flow"], ESCALATION_FLOW_NAME)
    assert escalation["StartAt"] == "SetEscalated"
    assert "EscalationPlaceholder" not in escalation["States"]


def test_stacks_expose_their_functions_for_observability(stacks) -> None:
    assert set(stacks["flow_stack"].functions) == {
        "ClassifyIncident", "UpdateIncidentStatus",
        "RemediationPolicy", "RedriveOrRestart", "PollExecution",
        "ResolveOnCall", "SendEscalation", "PlanReminder", "StoreTaskToken", "ResolveOnSuccess",
    }  # fmt: skip
    assert set(stacks["detection_stack"].functions) == {
        "DiscoverProcesses", "MissedRunWatchdog", "OpenIncident",
    }  # fmt: skip


@pytest.mark.req("10.1")
def test_update_incident_status_can_only_get_and_update_incidents(stacks) -> None:
    env = _function_env(stacks["flow"], "GuardIA UpdateIncidentStatus")
    assert set(env) >= {"INCIDENTS_TABLE", "INCIDENT_EVENTS_TABLE"}
    on_incidents = {
        action
        for statement in _function_statements(stacks["flow"], "UpdateIncidentStatus")
        if "Incidents" in json.dumps(statement.get("Resource"))
        and "IncidentEvents" not in json.dumps(statement.get("Resource"))
        for action in _actions(statement)
    }
    assert on_incidents <= {"dynamodb:GetItem", "dynamodb:UpdateItem"}


def test_flow_context_rejects_duplicate_functions_and_builds_status_states() -> None:
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_stepfunctions as sfn

    from components.function import GuardiaFunction, shared_layer
    from stacks.flow_context import FlowContext

    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D2", env=ENV)
    stack = cdk.Stack(app, "Ctx", env=ENV)
    ctx = FlowContext(scope=stack, data=data, layer=shared_layer(stack), thresholds={})
    fn = GuardiaFunction(stack, "U", name="update_incident_status", layer=ctx.layer)
    ctx.add_function("UpdateIncidentStatus", fn)
    with pytest.raises(ValueError, match="already registered"):
        ctx.add_function("UpdateIncidentStatus", fn)

    state = ctx.update_status("SetEscalated", "ESCALATED", details={"reason": "x"})
    sfn.StateMachine(stack, "Probe", definition_body=sfn.DefinitionBody.from_chainable(state))
    rendered = _definition(Template.from_stack(stack))["States"]["SetEscalated"]
    assert rendered["ResultPath"] is None  # the flow state is not overwritten
    assert rendered["Parameters"]["Payload"] == {
        "incident_id.$": "$.incident_id", "status": "ESCALATED", "details": {"reason": "x"},
    }  # fmt: skip
    assert isinstance(fn.function, lambda_.Function)


# ------------------------------------------------------------------------ T4
@pytest.mark.req("5.1")
def test_policy_decides_after_classification(stacks) -> None:
    states = _definition(stacks["flow"])["States"]
    assert states["ClassifyIncident"]["Next"] == "RemediationPolicy"
    assert states["RemediationPolicy"]["ResultPath"] == "$.policy"
    decision = states["PolicyDecision"]
    targets = {c["StringEquals"]: c["Next"] for c in decision["Choices"]}
    assert targets == {
        "REMEDIATE": "WaitRetryAfter",
        "REMEDIATE_AND_ESCALATE": "RemediateAndEscalate",
    }
    assert decision["Default"] == "EscalateByPolicy"


@pytest.mark.req("5.2")
def test_recovery_waits_the_policy_seconds_then_redrives(stacks) -> None:
    states = _definition(stacks["flow"])["States"]
    assert states["WaitRetryAfter"]["SecondsPath"] == "$.policy.wait_seconds"
    assert states["WaitRetryAfter"]["Next"] == "RedriveOrRestart"
    (catch,) = states["RedriveOrRestart"]["Catch"]
    assert set(catch["ErrorEquals"]) == {"RemediationNotAllowedError", "AttemptsExhaustedError"}
    assert catch["Next"] == "EscalateAfterRecoveryRejected"


@pytest.mark.req("5.4")
def test_poll_loop_resolves_only_on_success_and_is_bounded(stacks) -> None:
    states = _definition(stacks["flow"])["States"]
    assert states["WaitBeforePoll"]["Seconds"] == 180
    finished = states["RecoveryFinished?"]
    nexts = [c["Next"] for c in finished["Choices"]]
    assert nexts == ["MarkResolved", "RemediationPolicy", "CountPoll"]
    bound = finished["Choices"][2]
    assert bound == {"Variable": "$.polls.count", "NumericLessThan": MAX_POLLS, "Next": "CountPoll"}
    assert finished["Default"] == "RemediationPolicy"  # too long: the policy decides again
    payload = states["MarkResolved"]["Parameters"]["Payload"]
    assert payload["status"] == "RESOLVED"
    assert payload["details"] == {"resolved_by": "guardia_recovery"}


@pytest.mark.req("6.1")
def test_little_time_left_runs_recovery_and_escalation_in_parallel(stacks) -> None:
    parallel = _definition(stacks["flow"])["States"]["RemediateAndEscalate"]
    recovery, escalation = parallel["Branches"]
    assert recovery["StartAt"] == "ParallelWaitRetryAfter"
    again = recovery["States"]["ParallelRemediationPolicy"]["Parameters"]
    assert again["escalation_running"] is True
    assert escalation["StartAt"] == "EscalateInParallel"
    catches = escalation["States"]["EscalateInParallel"]["Catch"]
    assert catches[-1]["ErrorEquals"] == ["States.ALL"]  # Teams down does not stop the recovery


def test_every_escalation_runs_the_escalation_flow_once_per_incident(stacks) -> None:
    states = _definition(stacks["flow"])["States"]
    for name in ("EscalateByPolicy", "EscalateAfterRecoveryRejected"):
        state = states[name]
        assert state["Resource"].endswith("states:startExecution.sync:2")
        assert state["Parameters"]["Name.$"] == "$.incident_id"
        assert state["Catch"][0]["ErrorEquals"] == ["StepFunctions.ExecutionAlreadyExistsException"]


@pytest.mark.req("12.1")
def test_redrive_and_restart_require_the_remediation_auto_tag(stacks) -> None:
    acting = {"states:RedriveExecution", "states:StartExecution"}
    for statement in _statements(stacks["flow"]):
        actions = set(_actions(statement)) & acting
        if not actions:
            continue
        if statement.get("Sid") in ("RedriveOnlyRemediationAuto", "RestartOnlyRemediationAuto"):
            assert statement["Condition"] == REMEDIATION_TAG_CONDITION
        else:
            # only the incident flow may start the escalation flow, and only that one
            assert actions == {"states:StartExecution"}
            assert "EscalationFlow" in json.dumps(statement["Resource"])


@pytest.mark.req("12.4")
def test_policy_and_poll_cannot_act_on_processes(stacks) -> None:
    template = stacks["flow"]
    for resource in template.find_resources("AWS::IAM::Policy").values():
        name = json.dumps(resource["Properties"]["Roles"])
        if "RemediationPolicy" in name or "PollExecution" in name:
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
                assert not {"states:RedriveExecution", "states:StartExecution"} & set(
                    _actions(statement)
                )


# ------------------------------------------------------------------------ T5
def _escalation(stacks) -> dict:
    return _definition(stacks["flow"], ESCALATION_FLOW_NAME)["States"]


def _on(template: Template, function_key: str, table: str) -> set[str]:
    return {
        action
        for statement in _function_statements(template, function_key)
        if table in json.dumps(statement.get("Resource"))
        for action in _actions(statement)
    }


@pytest.mark.req("6.1")
def test_escalation_chain_has_the_states_of_the_ficha(stacks) -> None:
    states = _escalation(stacks)
    assert states["SetEscalated"]["Next"] == "ResolveOnCall"
    assert states["ResolveOnCall"]["ResultPath"] == "$.oncall"
    assert states["ResolveOnCall"]["Next"] == "RecoveryInParallel"
    assert states["SendEscalation"]["ResultPath"] == "$.notification"
    assert states["SendEscalation"]["Next"] == "PlanReminder"
    assert states["PlanReminder"]["ResultPath"] == "$.timing"
    assert states["PlanReminder"]["Next"] == "DeadlinePassed"
    resolved, passed = states["DeadlinePassed"]["Choices"]
    assert (
        resolved["Variable"] == "$.timing.already_resolved" and resolved["Next"] == "SendResolved"
    )
    assert passed["Variable"] == "$.timing.deadline_passed" and passed["Next"] == "SetSlaBreached"
    assert states["DeadlinePassed"]["Default"] == "WaitForResolution"
    assert states["SetSlaBreached"]["Next"] == "SendSlaBreached"
    assert states["SendSlaBreached"]["Next"] == "WaitAfterDeadline"
    assert states["SendReminder"]["Next"] == "PlanReminder"  # the reminder loop
    assert states["WaitForResolution"]["Next"] == "MarkEscalationResolved"
    assert states["WaitAfterDeadline"]["Next"] == "MarkEscalationResolved"
    assert states["MarkEscalationResolved"]["Next"] == "SendResolved"
    assert states["SendResolved"]["Next"] == "EscalationResolved"


@pytest.mark.req("6.1")
def test_the_first_message_says_when_the_recovery_runs_in_parallel(stacks) -> None:
    states = _escalation(stacks)
    (choice,) = states["RecoveryInParallel"]["Choices"]
    assert choice["Next"] == "SendEscalationParallel"
    assert "REMEDIATE_AND_ESCALATE" in json.dumps(choice)
    assert states["RecoveryInParallel"]["Default"] == "SendEscalation"
    assert states["SendEscalationParallel"]["Parameters"]["parallel"] is True
    assert states["SendEscalation"]["Parameters"]["parallel"] is False
    assert states["SendEscalationParallel"]["Next"] == "PlanReminder"


@pytest.mark.req("6.3")
def test_a_failed_first_escalation_fails_the_execution_and_is_not_retried(stacks) -> None:
    states = _escalation(stacks)
    for name in ("SendEscalation", "SendEscalationParallel"):
        (catch,) = states[name]["Catch"]
        assert catch["Next"] == "NotificationFailed"
        for retry in states[name]["Retry"]:  # Teams is already tried 3 times inside
            assert all(error.startswith("Lambda.") for error in retry["ErrorEquals"])
    assert states["NotificationFailed"]["Type"] == "Fail"


def test_follow_up_messages_that_fail_do_not_stop_the_escalation(stacks) -> None:
    states = _escalation(stacks)
    assert states["SendReminder"]["Catch"][0]["Next"] == "PlanReminder"
    assert states["SendSlaBreached"]["Catch"][0]["Next"] == "WaitAfterDeadline"
    assert states["SendResolved"]["Catch"][0]["Next"] == "EscalationResolved"


@pytest.mark.req("8.3")
def test_waits_use_a_task_token_with_the_planned_timeout(stacks) -> None:
    states = _escalation(stacks)
    for name, timeout in (
        ("WaitForResolution", "$.timing.wait_seconds"),
        ("WaitAfterDeadline", "$.timing.post_deadline_seconds"),
    ):
        state = states[name]
        assert state["Resource"].endswith(".waitForTaskToken")
        assert state["TimeoutSecondsPath"] == timeout
        assert state["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token"
        assert state["ResultPath"] == "$.resolution"
    assert states["WaitForResolution"]["Catch"][0]["Next"] == "SendReminder"
    assert states["WaitForResolution"]["Catch"][0]["ErrorEquals"] == ["States.Timeout"]
    assert states["WaitAfterDeadline"]["Catch"][0]["Next"] == "ClosedUnresolved"
    assert states["ClosedUnresolved"]["Type"] == "Succeed"


@pytest.mark.req("6.2")
def test_status_changes_go_through_update_incident_status(stacks) -> None:
    states = _escalation(stacks)
    for name, status in (
        ("SetEscalated", "ESCALATED"),
        ("SetSlaBreached", "SLA_BREACHED"),
        ("MarkEscalationResolved", "RESOLVED"),
    ):
        assert states[name]["Parameters"]["Payload"]["status"] == status
    payload = states["MarkEscalationResolved"]["Parameters"]["Payload"]
    assert payload["details"] == {"resolved_by.$": "$.resolution.resolved_by"}


def test_each_send_escalation_state_sets_its_mode(stacks) -> None:
    states = _escalation(stacks)
    modes = {
        "SendEscalation": "initial",
        "SendEscalationParallel": "initial",
        "SendReminder": "reminder",
        "SendSlaBreached": "sla_breached",
        "SendResolved": "resolved",
    }
    for name, mode in modes.items():
        assert states[name]["Parameters"]["mode"] == mode


def test_the_escalation_states_do_not_reuse_the_incident_flow_names(stacks) -> None:
    incident = set(_definition(stacks["flow"])["States"])
    assert not incident & set(_escalation(stacks))  # one stack, one construct-id space


@pytest.mark.req("7.1")
def test_resolve_on_call_gets_the_on_call_person_from_the_deploy(stacks) -> None:
    env = _function_env(stacks["flow"], "GuardIA ResolveOnCall")
    assert env["GUARDIA_ONCALL_EMAIL"] == "oncall@example.com"
    assert env["GUARDIA_ONCALL_NAME"] == "Ana Guardia"
    assert env["GUARDIA_ONCALL_BACKUP_EMAIL"] == "backup@example.com"


def test_synth_fails_without_the_on_call_email() -> None:
    with pytest.raises(MissingOnCallError, match="GUARDIA_ONCALL_EMAIL"):
        oncall_from_environment({})
    with pytest.raises(MissingOnCallError):
        oncall_from_environment({"GUARDIA_ONCALL_EMAIL": "not-an-email"})
    config = oncall_from_environment({"GUARDIA_ONCALL_EMAIL": " a@b.co "})
    assert config == OnCallConfig(email="a@b.co")
    assert config.lambda_environment() == {"GUARDIA_ONCALL_EMAIL": "a@b.co"}


@pytest.mark.req("7.2")
def test_only_send_escalation_reads_the_alerts_webhook(stacks) -> None:
    env = _function_env(stacks["flow"], "GuardIA SendEscalation")
    assert env["TEAMS_ALERTS_SECRET_ID"] == "guardia/teams-alerts-webhook"  # noqa: S105
    template = stacks["flow"]
    readers = [
        key
        for key in ("ResolveOnCall", "SendEscalation", "PlanReminder", "StoreTaskToken",
                    "ResolveOnSuccess", "ClassifyIncident", "UpdateIncidentStatus")
        if any("secretsmanager:GetSecretValue" in _actions(s)
               for s in _function_statements(template, key))
    ]  # fmt: skip
    assert readers == ["SendEscalation"]
    reads = [
        s for s in _function_statements(template, "SendEscalation")
        if "secretsmanager:GetSecretValue" in _actions(s)
    ]  # fmt: skip
    assert "TeamsAlertsWebhook" in json.dumps(reads) and "TeamsSummary" not in json.dumps(reads)


def test_send_escalation_reads_audit_events_but_cannot_change_them(stacks) -> None:
    on_events = _on(stacks["flow"], "SendEscalation", "IncidentEvents")
    assert "dynamodb:Query" in on_events and "dynamodb:PutItem" in on_events
    assert not on_events & {*AUDIT_DENIED_ACTIONS, "dynamodb:*"}


@pytest.mark.req("8.3")
def test_succeeded_rule_invokes_resolve_on_success(stacks) -> None:
    rules = stacks["flow"].find_resources("AWS::Events::Rule")
    (rule,) = [r for r in rules.values() if r["Properties"]["EventPattern"] == SUCCEEDED_PATTERN]
    (target,) = rule["Properties"]["Targets"]
    assert target["RetryPolicy"]["MaximumRetryAttempts"] == 3 and "DeadLetterConfig" in target
    assert "ResolveOnSuccess" in json.dumps(target["Arn"])


@pytest.mark.req("8.3")
def test_resolve_on_success_can_only_query_and_answer_tokens(stacks) -> None:
    template = stacks["flow"]
    assert _on(template, "ResolveOnSuccess", "Incidents") >= {"dynamodb:Query"}
    assert not _on(template, "ResolveOnSuccess", "Incidents") & {
        "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan",
    }  # fmt: skip
    assert _on(template, "ResolveOnSuccess", "ProcessRegistry") == {"dynamodb:GetItem"}
    answers = [
        s for s in _function_statements(template, "ResolveOnSuccess")
        if "states:SendTaskSuccess" in _actions(s)
    ]  # fmt: skip
    assert len(answers) == 1
    assert set(_actions(answers[0])) == {"states:SendTaskSuccess", "states:SendTaskFailure"}


def test_task_token_is_only_written_by_store_task_token(stacks) -> None:
    template = stacks["flow"]
    assert _on(template, "StoreTaskToken", "Incidents") == {"dynamodb:UpdateItem"}
    assert _on(template, "PlanReminder", "Incidents") == {"dynamodb:GetItem"}
