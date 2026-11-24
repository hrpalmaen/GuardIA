"""Synth tests for the AI agent in the flows (T8 · requirements 4.5, 4.6, 12.2, 12.3)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from config import AGENT_AZURE_SECRET_NAME, AgentConfig, OnCallConfig, environment
from stacks.data_stack import DataStack
from stacks.flow_agent import bedrock_model_arns
from stacks.flow_stack import ESCALATION_FLOW_NAME, FLOW_NAME, FlowStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")
ONCALL = OnCallConfig(email="oncall@example.com", name="Ana Guardia", backup_email=None)
BEDROCK_MODEL = "amazon.nova-pro-v1:0"


def _build(provider: str | None, model: str | None = None) -> tuple[Template, Template]:
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV, retain_data=False, agent_provider=provider)
    agent = AgentConfig(provider=provider, model=model) if provider else None
    flow = FlowStack(app, "F", env=ENV, data=data, oncall=ONCALL, agent=agent, retain_data=False)
    return Template.from_stack(data), Template.from_stack(flow)


@pytest.fixture(scope="module")
def azure() -> tuple[Template, Template]:
    return _build("azure_openai")


@pytest.fixture(scope="module")
def bedrock() -> tuple[Template, Template]:
    return _build("bedrock", BEDROCK_MODEL)


@pytest.fixture(scope="module")
def without_agent() -> tuple[Template, Template]:
    return _build(None)


def _definition(template: Template, name: str) -> dict:
    for machine in template.find_resources("AWS::StepFunctions::StateMachine").values():
        if machine["Properties"].get("StateMachineName") != name:
            continue
        body = machine["Properties"]["DefinitionString"]
        if isinstance(body, str):
            return json.loads(body)
        return json.loads("".join(p if isinstance(p, str) else "ARN" for p in body["Fn::Join"][1]))
    raise AssertionError(f"no state machine named {name}")


def _agent_function(template: Template) -> tuple[str, dict]:
    for key, resource in template.find_resources("AWS::Lambda::Function").items():
        if resource["Properties"].get("Description", "").startswith("GuardIA InvokeAgent"):
            return key, resource["Properties"]
    raise AssertionError("no InvokeAgent function")


def _agent_statements(template: Template) -> list[dict]:
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if "InvokeAgent" in json.dumps(policy["Properties"]["Roles"]):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def _on(statements: list[dict], table: str) -> set[str]:
    """Actions granted on one table (and not on IncidentEvents, whose name contains 'Incident')."""
    found: set[str] = set()
    for statement in statements:
        resource = json.dumps(statement.get("Resource"))
        if table in resource and not (table == "Incidents" and "IncidentEvents" in resource):
            found |= _actions(statement)
    return found


# ------------------------------------------------------------------------ the Lambda
@pytest.mark.req("4.5")
def test_invoke_agent_has_a_timeout_above_the_agents_own_limit(azure) -> None:
    _, properties = _agent_function(azure[1])

    assert properties["Timeout"] == 45
    assert properties["Environment"]["Variables"]["AGENT_PROVIDER"] == "azure_openai"
    assert properties["Environment"]["Variables"]["AGENT_SECRET_ID"] == AGENT_AZURE_SECRET_NAME


@pytest.mark.req("12.2")
def test_invoke_agent_reads_and_writes_only_what_it_needs(azure) -> None:
    statements = _agent_statements(azure[1])

    assert _on(statements, "Incidents") <= {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
    }
    assert _on(statements, "ErrorCatalog") == {"dynamodb:Scan"}
    forbidden = {"dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:*"}
    assert not _on(statements, "IncidentEvents") & forbidden


@pytest.mark.req("12.2")
def test_invoke_agent_can_only_read_step_functions(azure) -> None:
    actions = {
        a for s in _agent_statements(azure[1]) for a in _actions(s) if a.startswith("states:")
    }

    assert actions == {
        "states:GetExecutionHistory",
        "states:DescribeExecution",
        "states:ListExecutions",
    }


@pytest.mark.req("12.2")
def test_invoke_agent_cannot_change_anything_outside_the_incident(azure) -> None:
    actions = {a for s in _agent_statements(azure[1]) for a in _actions(s)}

    assert not actions & {
        "states:StartExecution",
        "states:RedriveExecution",
        "states:StopExecution",
        "states:SendTaskSuccess",
        "lambda:InvokeFunction",
        "events:PutEvents",
        "ssm:PutParameter",
        "secretsmanager:PutSecretValue",
        "bedrock:*",
    }


# ------------------------------------------------------------------------ providers
@pytest.mark.req("12.3")
def test_azure_reads_its_secret_and_never_calls_bedrock(azure) -> None:
    data, flow = azure
    actions = {a for s in _agent_statements(flow) for a in _actions(s)}

    assert "secretsmanager:GetSecretValue" in actions
    assert not any(a.startswith("bedrock:") for a in actions)
    names = {
        r["Properties"].get("Name")
        for r in data.find_resources("AWS::SecretsManager::Secret").values()
    }
    assert AGENT_AZURE_SECRET_NAME in names


@pytest.mark.req("12.3")
def test_bedrock_invokes_only_the_configured_model_and_has_no_secret(bedrock) -> None:
    data, flow = bedrock
    statements = _agent_statements(flow)
    invoke = [s for s in statements if "bedrock:InvokeModel" in _actions(s)]

    assert len(invoke) == 1 and _actions(invoke[0]) == {"bedrock:InvokeModel"}
    resource = json.dumps(invoke[0]["Resource"])
    assert BEDROCK_MODEL in resource and "foundation-model" in resource
    assert resource != '"*"' and "bedrock:*" not in resource
    assert "secretsmanager:GetSecretValue" not in {a for s in statements for a in _actions(s)}
    names = {
        r["Properties"].get("Name")
        for r in data.find_resources("AWS::SecretsManager::Secret").values()
    }
    assert AGENT_AZURE_SECRET_NAME not in names


def test_bedrock_arns_for_a_foundation_model_and_for_an_inference_profile() -> None:
    assert bedrock_model_arns("aws", "us-east-1", "111122223333", "amazon.nova-pro-v1:0") == [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
    ]
    assert bedrock_model_arns("aws", "us-east-1", "111122223333", "us.amazon.nova-pro-v1:0") == [
        "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.amazon.nova-pro-v1:0",
        "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0",
    ]


# ------------------------------------------------------------------------ the flows
@pytest.mark.req("4.5")
def test_unknown_incidents_go_through_the_agent_before_the_policy(azure) -> None:
    states = _definition(azure[1], FLOW_NAME)["States"]

    assert states["ClassifyIncident"]["Next"] == "NeedsAgent?"
    (rule,) = states["NeedsAgent?"]["Choices"]
    assert rule["StringEquals"] == "unknown" and rule["Next"] == "InvokeAgentClassify"
    assert states["NeedsAgent?"]["Default"] == "RemediationPolicy"
    assert states["InvokeAgentClassify"]["Next"] == "RemediationPolicy"
    assert states["InvokeAgentClassify"]["ResultPath"] == "$.agent"


@pytest.mark.req("4.6")
@pytest.mark.parametrize(
    "flow, state",
    [(FLOW_NAME, "InvokeAgentClassify"), (ESCALATION_FLOW_NAME, "InvokeAgentDiagnose")],
)
def test_a_failing_agent_never_stops_the_flow(azure, flow, state) -> None:
    task = _definition(azure[1], flow)["States"][state]

    (catch,) = task["Catch"]
    assert catch["ErrorEquals"] == ["States.ALL"] and catch["Next"] == task["Next"]


@pytest.mark.req("4.3")
def test_the_escalation_asks_for_a_diagnosis_before_the_first_message(azure) -> None:
    states = _definition(azure[1], ESCALATION_FLOW_NAME)["States"]

    assert states["ResolveOnCall"]["Next"] == "InvokeAgentDiagnose"
    assert states["InvokeAgentDiagnose"]["Next"] == "RecoveryInParallel"


@pytest.mark.req("4.6")
def test_without_an_agent_the_flows_are_the_same_as_before(without_agent) -> None:
    data, flow = without_agent
    incident = _definition(flow, FLOW_NAME)["States"]
    escalation = _definition(flow, ESCALATION_FLOW_NAME)["States"]

    assert incident["ClassifyIncident"]["Next"] == "RemediationPolicy"
    assert "NeedsAgent?" not in incident and "InvokeAgentClassify" not in incident
    assert "InvokeAgentDiagnose" not in escalation
    assert escalation["ResolveOnCall"]["Next"] == "RecoveryInParallel"
    with pytest.raises(AssertionError):
        _agent_function(flow)
    secrets = data.find_resources("AWS::SecretsManager::Secret")
    assert all(r["Properties"].get("Name") != AGENT_AZURE_SECRET_NAME for r in secrets.values())


def test_every_environment_with_an_agent_synthesizes_its_flows() -> None:
    for name in ("dev", "workshop"):
        config = environment(name)
        assert config.agent is not None
        data, flow = _build(config.agent.provider, config.agent.model)
        assert _agent_function(flow)
