"""Synth tests for GuardIA-Sim (T3)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from config import TEAMS_ALERTS_SECRET_NAME, environment
from sim.processes import NO_AUTO_ORG, sim_processes
from stacks.data_stack import DataStack
from stacks.sim_stack import STUB_MEMORY_MB, SimNotAllowedError, SimStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")


def _sim(env_name: str) -> SimStack:
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV)
    return SimStack(
        app, "S", env=ENV, config=environment(env_name),
        teams_alerts_secret=data.secrets[TEAMS_ALERTS_SECRET_NAME],
    )  # fmt: skip


@pytest.fixture(scope="module")
def template() -> Template:
    return Template.from_stack(_sim("dev"))


def test_simulator_refuses_production() -> None:
    with pytest.raises(SimNotAllowedError, match="env=prod"):
        _sim("prod")


def _tags(resource: dict) -> dict:
    return {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}


@pytest.mark.req("1.3")
def test_each_process_is_a_state_machine_with_its_real_name_and_guardia_tags(template) -> None:
    machines = {
        m["Properties"]["StateMachineName"]: m
        for m in template.find_resources("AWS::StepFunctions::StateMachine").values()
    }
    for process in sim_processes():
        tags = _tags(machines[process.name])
        for key, value in process.tags.items():
            assert tags[key] == value, (process.org, key)
    no_auto = next(p for p in sim_processes() if p.org == NO_AUTO_ORG)
    assert _tags(machines[no_auto.name])["guardia:remediation"] == "escalate-only"


def test_each_cron_rule_has_its_own_role_and_the_scheduled_input(template) -> None:
    rules = [
        r for r in template.find_resources("AWS::Events::Rule").values()
        if "launches" in r["Properties"].get("Description", "")
    ]  # fmt: skip
    expressions = sorted(r["Properties"]["ScheduleExpression"] for r in rules)
    assert expressions == sorted(p.schedule for p in sim_processes())
    roles = {json.dumps(r["Properties"]["Targets"][0]["RoleArn"]) for r in rules}
    assert len(roles) == len(rules)
    for rule in rules:
        target_input = json.loads(rule["Properties"]["Targets"][0]["Input"])
        assert target_input["source"] == "scheduled"


def test_stub_is_small_enough_to_run_out_of_memory(template) -> None:
    template.has_resource_properties("AWS::Lambda::Function", {
        "MemorySize": STUB_MEMORY_MB,
        "Description": Match.string_like_regexp("stand-in for every step"),
    })  # fmt: skip


def test_janitor_can_only_restore_rule_roles_and_the_teams_secret(template) -> None:
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            if "iam:DeleteRolePolicy" in actions:
                assert actions == ["iam:DeleteRolePolicy"]
                assert "*" not in json.dumps(statement["Resource"])
            if "secretsmanager:UpdateSecretVersionStage" in actions:
                assert "secretsmanager:GetSecretValue" not in actions  # never reads the value


def test_outputs_let_sim_py_find_everything(template) -> None:
    outputs = template.to_json()["Outputs"]
    assert "ScenariosTableName" in outputs
    for process in sim_processes():
        assert f"{process.org}StateMachineArn" in outputs
        assert f"{process.org}RuleRoleName" in outputs
