"""Synth tests for GuardIA-Flow and GuardIA-Detection (Fase B · PR 2)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from config import environment
from guardia_common.incidents import STATUS_CHANGE_PATTERN, WATCHDOG_PATTERN
from stacks.data_stack import AUDIT_DENIED_ACTIONS, DataStack
from stacks.detection_stack import DetectionStack, event_pattern
from stacks.flow_stack import FLOW_NAME, FlowStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")


@pytest.fixture(scope="module")
def stacks() -> dict[str, Template]:
    config = environment("dev")
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV, thresholds=config.thresholds, retain_data=False)
    flow = FlowStack(app, "F", env=ENV, data=data, retain_data=False)
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


def _function_env(template: Template, description_prefix: str) -> dict:
    for resource in template.find_resources("AWS::Lambda::Function").values():
        props = resource["Properties"]
        if props.get("Description", "").startswith(description_prefix):
            return props["Environment"]["Variables"]
    raise AssertionError(f"no function described as {description_prefix!r}")


# --------------------------------------------------------------------------- flow
def _definition(template: Template) -> dict:
    (machine,) = template.find_resources("AWS::StepFunctions::StateMachine").values()
    parts = machine["Properties"]["DefinitionString"]["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "ARN" for p in parts))


def test_flow_state_machine_starts_with_classify_incident(stacks) -> None:
    machines = stacks["flow"].find_resources("AWS::StepFunctions::StateMachine")
    (machine,) = machines.values()
    assert machine["Properties"]["StateMachineName"] == FLOW_NAME
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
def test_flow_routes_recoverable_incidents_to_recovery_and_the_rest_to_escalation(stacks) -> None:
    states = _definition(stacks["flow"])["States"]
    assert states["ClassifyIncident"]["Next"] == "RouteByClassification"
    route = states["RouteByClassification"]
    (choice,) = route["Choices"]
    assert choice == {
        "Variable": "$.classification.classification",
        "StringEquals": "auto_recoverable",
        "Next": "RecoveryPlaceholder",
    }
    assert route["Default"] == "EscalationPlaceholder"
    assert states["RecoveryPlaceholder"]["Next"] == "RecoveryPending"
    assert states["EscalationPlaceholder"]["Next"] == "EscalationPending"


def test_stacks_expose_their_functions_for_observability(stacks) -> None:
    assert set(stacks["flow_stack"].functions) == {"ClassifyIncident", "UpdateIncidentStatus"}
    assert set(stacks["detection_stack"].functions) == {
        "DiscoverProcesses", "MissedRunWatchdog", "OpenIncident",
    }  # fmt: skip


@pytest.mark.req("10.1")
def test_update_incident_status_can_only_get_and_update_incidents(stacks) -> None:
    env = _function_env(stacks["flow"], "GuardIA UpdateIncidentStatus")
    assert set(env) >= {"INCIDENTS_TABLE", "INCIDENT_EVENTS_TABLE"}
    on_incidents = {
        action
        for statement in _statements(stacks["flow"])
        if "Incidents169" in json.dumps(statement.get("Resource"))
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
