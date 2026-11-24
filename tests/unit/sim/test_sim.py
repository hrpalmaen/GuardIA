"""GuardIA-Sim (T3): rebuilt definitions, the stub step, the janitor, and sim.py."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from sim.definition import all_states, build_definition, check_against_history
from sim.processes import NO_AUTO_ORG, sim_processes
from sim_lambdas.janitor.handler import MISSED_POLICY_NAME, TEAMS_DOWN_KEY, janitor, missed_key
from sim_lambdas.stub_task import handler as stub
from tests.unit.conftest import EMT12_OOM, EMTSAS_500, REAL_SUCCEEDED, REGION

SECRET = "guardia/teams-alerts-webhook"  # noqa: S105 - a secret name


def current_version(secrets, secret_id: str = SECRET) -> str:
    """moto moves the AWSCURRENT label but keeps answering the old value in
    GetSecretValue, so the tests check the label, which is what a restore moves."""
    stages = secrets.describe_secret(SecretId=secret_id)["VersionIdsToStages"]
    return next(version for version, labels in stages.items() if "AWSCURRENT" in labels)


ALL_FIXTURES = (EMTSAS_500, EMT12_OOM, *REAL_SUCCEEDED)


# ---------------------------------------------------------------- definitions
@pytest.mark.parametrize("fixture_id", ALL_FIXTURES)
def test_every_state_of_the_real_histories_exists_with_its_type(real_executions, fixture_id):
    execution = real_executions.by_id(fixture_id)
    org = execution.state_machine_arn.rsplit(":", 1)[-1].split("-")[1]
    assert check_against_history(build_definition(org), execution.history_events()) == []


def test_emtsas_ends_like_the_real_failure():
    fail = all_states(build_definition("EMTSAS"))["BI-EMTSAS-GCRAW-FailIngestionController"]
    assert (fail["Error"], fail["Cause"]) == (
        "IngestionControllerError",
        "IngestionController failed",
    )


def test_emt12_keeps_the_jsonpath_defect_of_the_error_path():
    states = all_states(build_definition("EMT12"))
    checkpoint = states["BI-EMT12-GCRAW-UpdateCheckpointTask"]["Parameters"]["Payload"]
    assert checkpoint["messagesSent.$"] == "$.mapResults[0].messagesSent"
    map_state = states["BI-EMT12-GCRAW-FetchRawDataMap"]
    assert map_state["ResultPath"] == "$.mapResults"
    assert map_state["Catch"][0]["ResultPath"] == "$.mapError"


def test_generated_files_are_up_to_date():
    from build_sim_definitions import main

    assert main(["--check"]) == 0


def test_processes_keep_the_real_tags_and_add_one_without_auto_remediation():
    processes = {p.org: p for p in sim_processes()}
    assert set(processes) == {"EMT12", "EMT22", "EMTSAS", NO_AUTO_ORG}
    assert processes["EMT12"].tags["guardia:remediation"] == "auto"
    assert processes["EMT12"].schedule == "cron(30 * * * ? *)"
    assert processes[NO_AUTO_ORG].tags["guardia:remediation"] == "escalate-only"
    assert all(p.tags["guardia:simulated"] == "true" for p in processes.values())
    assert all(p.tags["guardia:monitor"] == "true" for p in processes.values())


# ------------------------------------------------------------------ stub step
@pytest.fixture
def scenarios():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="SimScenarios",
            KeySchema=[{"AttributeName": "org", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "org", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def step(org="EMTSAS", name="IngestionController", execution="run-a", redrive=0, **extra):
    return {"org": org, "step": name, "execution_id": execution, "redrive_count": redrive, **extra}


def test_without_scenario_every_step_succeeds_and_feeds_the_choices(scenarios):
    ingestion = stub.stub_task(step(), scenarios)
    assert ingestion["totalTasks"] == len(ingestion["tasks"]) == 3
    fetched = stub.stub_task(
        step(name="FetchRawConversations", task=ingestion["tasks"][0]), scenarios
    )
    assert fetched == {"recordsProcessed": 10}
    split = stub.stub_task(step(name="ProcessConversationSplitter", records=10), scenarios)
    assert split == {"messagesSent": 10}


def test_http500_fails_one_run_with_the_real_message_and_lets_the_redrive_succeed(scenarios):
    scenarios.put_item(Item={"org": "EMTSAS", "scenario": "http500", "times_left": 1})

    with pytest.raises(Exception, match="500 Server Error") as raised:
        stub.stub_task(step(), scenarios)
    assert type(raised.value) is Exception  # errorType "Exception", like the real step

    assert stub.stub_task(step(redrive=1), scenarios)["totalTasks"] == 3  # the redrive
    assert stub.stub_task(step(execution="run-b"), scenarios)["totalTasks"] == 3


def test_a_failing_run_fails_in_every_step_of_that_run(scenarios):
    scenarios.put_item(Item={"org": "EMT12", "scenario": "oom", "times_left": 1})

    def splitter(execution="run-a"):
        return step("EMT12", "ProcessConversationSplitter", execution, records=10)

    calls = []
    original = stub._fill_memory
    stub._fill_memory = lambda: (_ for _ in ()).throw(MemoryError("killed"))
    try:
        for _ in range(3):  # three Map iterations of the same run
            with pytest.raises(MemoryError):
                stub.stub_task(splitter(), scenarios)
            calls.append(1)
        assert stub.stub_task(splitter("run-b"), scenarios) == {"messagesSent": 10}
    finally:
        stub._fill_memory = original
    item = scenarios.get_item(Key={"org": "EMT12"})["Item"]
    assert item["times_left"] == 0 and item["failed_runs"] == {"run-a#0"}


def test_scenario_only_hits_its_own_step(scenarios):
    scenarios.put_item(Item={"org": "EMT12", "scenario": "oom", "times_left": 1})
    assert stub.stub_task(step("EMT12"), scenarios)["totalTasks"] == 3
    assert scenarios.get_item(Key={"org": "EMT12"})["Item"]["times_left"] == 1


def test_unknown_raises_an_error_outside_the_catalog(scenarios):
    scenarios.put_item(Item={"org": "EMT22", "scenario": "unknown", "times_left": 1})
    with pytest.raises(stub.SchemaValidationError, match="wrapUpCodeV2"):
        stub.stub_task(step("EMT22"), scenarios)


def test_stuck_asks_the_run_to_wait(scenarios):
    scenarios.put_item(
        Item={"org": "EMT12", "scenario": "stuck", "times_left": 1, "wait_seconds": 2400}
    )
    assert stub.stub_task(step("EMT12"), scenarios)["simWaitSeconds"] == 2400
    assert "simWaitSeconds" not in stub.stub_task(step("EMT12", execution="run-b"), scenarios)


# -------------------------------------------------------------------- janitor
@pytest.fixture
def timed(scenarios):
    iam = boto3.client("iam", region_name=REGION)
    iam.create_role(RoleName="emt22-rule", AssumeRolePolicyDocument="{}")
    deny = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Deny", "Action": "states:StartExecution", "Resource": "*"}],
    }
    iam.put_role_policy(
        RoleName="emt22-rule", PolicyName=MISSED_POLICY_NAME, PolicyDocument=json.dumps(deny)
    )  # fmt: skip
    secrets = boto3.client("secretsmanager", region_name=REGION)
    secrets.create_secret(Name="guardia/teams-alerts-webhook", SecretString="https://good")
    stages = secrets.describe_secret(SecretId="guardia/teams-alerts-webhook")["VersionIdsToStages"]
    (good,) = stages
    secrets.put_secret_value(SecretId="guardia/teams-alerts-webhook", SecretString="https://bad")
    return scenarios, iam, secrets, good


def test_janitor_restores_only_expired_scenarios(timed):
    scenarios, iam, secrets, good = timed
    scenarios.put_item(
        Item={"org": missed_key("EMT22"), "role_name": "emt22-rule", "restore_at": 100}
    )
    scenarios.put_item(Item={"org": TEAMS_DOWN_KEY, "secret_id": "guardia/teams-alerts-webhook",
                             "previous_version": good, "restore_at": 500})  # fmt: skip

    assert janitor(scenarios, iam, secrets, now=200) == [
        "rule role emt22-rule can start executions again"
    ]
    assert iam.list_role_policies(RoleName="emt22-rule")["PolicyNames"] == []
    assert len(scenarios.scan()["Items"]) == 1

    janitor(scenarios, iam, secrets, now=600)
    assert current_version(secrets) == good
    assert scenarios.scan()["Items"] == []


def test_restoring_twice_is_harmless(timed):
    scenarios, iam, secrets, good = timed
    item = {"org": missed_key("EMT22"), "role_name": "emt22-rule", "restore_at": 1}
    scenarios.put_item(Item=item)
    janitor(scenarios, iam, secrets, now=2)
    from sim_lambdas.janitor.handler import restore

    assert "can start executions again" in restore(item, iam, secrets)


# --------------------------------------------------------------------- sim.py
@pytest.fixture
def cli(timed):
    from tests.unit.sim.sim_cli import load_cli

    scenarios, iam, secrets, good = timed
    registry = boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName="ProcessRegistry",
        KeySchema=[{"AttributeName": "resource_arn", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "resource_arn", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    arn = "arn:aws:states:us-east-1:123456789012:stateMachine:BI-EMT12-GCRAW-Orchestrator"
    registry.put_item(Item={"resource_arn": arn, "organization": "EMT12"})
    module = load_cli()
    # A fresh secret: moto gets confused once stages were moved by hand.
    secrets.create_secret(Name="guardia/cli-webhook", SecretString="https://good")
    good = current_version(secrets, "guardia/cli-webhook")
    sim = module.Sim(
        scenarios=scenarios, registry=registry, sfn=None, iam=iam, secrets=secrets,
        state_machines={"EMT12": arn}, rule_roles={"EMT22": "emt22-rule"},
        secret_id="guardia/cli-webhook",  # noqa: S106 - a secret name
    )  # fmt: skip
    iam.delete_role_policy(RoleName="emt22-rule", PolicyName=MISSED_POLICY_NAME)
    return module, sim, good


def test_fail_writes_a_scenario_that_expires(cli):
    module, sim, _ = cli
    out = module.fail(sim, "EMT12", "oom", 2, 40, now=1000)
    item = sim.scenarios.get_item(Key={"org": "EMT12"})["Item"]
    assert item["scenario"] == "oom" and item["times_left"] == 2
    assert item["expires_at"] == 1000 + module.SCENARIO_HOURS * 3600
    assert "ProcessConversationSplitter" in out


def test_missed_denies_the_rule_role_and_clear_restores_it(cli):
    module, sim, _ = cli
    module.missed(sim, "EMT22", 70, now=1000)
    assert sim.iam.list_role_policies(RoleName="emt22-rule")["PolicyNames"] == [MISSED_POLICY_NAME]

    module.clear(sim, "EMT22")
    assert sim.iam.list_role_policies(RoleName="emt22-rule")["PolicyNames"] == []


def test_teams_down_never_reads_the_value_and_restores_the_saved_version(cli):
    module, sim, good = cli
    module.teams_down(sim, 15, now=1000)
    module.teams_down(sim, 30, now=1100)  # extending keeps the original version
    assert current_version(sim.secrets, sim.secret_id) != good
    saved = sim.scenarios.get_item(Key={"org": TEAMS_DOWN_KEY})["Item"]
    assert saved["previous_version"] == good and saved["restore_at"] == 1100 + 30 * 60

    module.clear(sim, None)
    assert current_version(sim.secrets, sim.secret_id) == good


def test_deadline_sets_and_clears_the_operator_field(cli):
    module, sim, _ = cli
    module.deadline(sim, "EMT12", 40, now=1_763_996_400)  # 2025-11-24 10:00 Bogota
    item = sim.registry.get_item(Key={"resource_arn": sim.state_machines["EMT12"]})["Item"]
    assert item["deadline_local"] == "10:40"
    module.deadline(sim, "EMT12", None, now=0)
    item = sim.registry.get_item(Key={"resource_arn": sim.state_machines["EMT12"]})["Item"]
    assert "deadline_local" not in item
