import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from stacks.data_stack import DataStack


@pytest.fixture
def template() -> Template:
    app = cdk.App()
    return Template.from_stack(DataStack(app, "GuardIA-Data-Test"))


@pytest.mark.req("13.3")
def test_data_stack_synthesizes(template: Template) -> None:
    assert template.to_json()["Resources"]


@pytest.mark.req("13.3")
def test_every_threshold_is_an_ssm_parameter(template: Template) -> None:
    template.resource_count_is("AWS::SSM::Parameter", len(DEFAULT_THRESHOLDS))
    for name, value in DEFAULT_THRESHOLDS.items():
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {"Name": f"{SSM_PREFIX}/{name}", "Type": "String", "Value": value},
        )


def test_default_deadline_matches_design() -> None:
    assert DEFAULT_THRESHOLDS["deadline-local"] == "06:00"
    assert DEFAULT_THRESHOLDS["timezone"] == "America/Bogota"


# --------------------------------------------------------------------------- 1.2
from guardia_common.tables import EXECUTION_ARN_INDEX, TABLES  # noqa: E402


def _tables(template: Template) -> dict:
    return template.find_resources("AWS::DynamoDB::Table")


@pytest.mark.req("10.2")
def test_every_shared_table_is_created_with_its_keys(template: Template) -> None:
    resources = list(_tables(template).values())
    assert len(resources) == len(TABLES)
    for spec in TABLES.values():
        keys = [{"AttributeName": spec.partition_key, "KeyType": "HASH"}]
        if spec.sort_key:
            keys.append({"AttributeName": spec.sort_key, "KeyType": "RANGE"})
        assert any(r["Properties"]["KeySchema"] == keys for r in resources), spec.name


@pytest.mark.req("10.2")
def test_tables_are_encrypted_with_the_customer_key(template: Template) -> None:
    for resource in _tables(template).values():
        sse = resource["Properties"]["SSESpecification"]
        assert sse["SSEEnabled"] is True and sse["SSEType"] == "KMS"
        assert "KMSMasterKeyId" in sse


def test_incidents_has_the_execution_arn_index(template: Template) -> None:
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "incident_id", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [{
            "IndexName": EXECUTION_ARN_INDEX,
            "KeySchema": [{"AttributeName": "execution_arn", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    })  # fmt: skip


@pytest.mark.req("10.3")
def test_incident_events_has_ttl_and_point_in_time_recovery(template: Template) -> None:
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "incident_id", "KeyType": "HASH"},
                      {"AttributeName": "sk", "KeyType": "RANGE"}],
        "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
    })  # fmt: skip


# --------------------------------------------------------------------------- Fase B · PR 1
from aws_cdk import aws_iam as iam  # noqa: E402

from config import TEAMS_ALERTS_SECRET_NAME, TEAMS_SUMMARY_SECRET_NAME, environment  # noqa: E402
from stacks.data_stack import AUDIT_DENIED_ACTIONS  # noqa: E402


@pytest.mark.req("12.3")
def test_webhook_secrets_are_encrypted_with_the_data_key(template: Template) -> None:
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    names = {r["Properties"]["Name"]: r for r in secrets.values()}
    assert set(names) == {TEAMS_ALERTS_SECRET_NAME, TEAMS_SUMMARY_SECRET_NAME}
    for resource in names.values():
        assert "KmsKeyId" in resource["Properties"]


@pytest.mark.req("10.3")
def test_incident_events_denies_updates_and_deletes_to_everyone(template: Template) -> None:
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "incident_id", "KeyType": "HASH"},
                      {"AttributeName": "sk", "KeyType": "RANGE"}],
        "ResourcePolicy": {"PolicyDocument": {"Statement": [{
            "Effect": "Deny", "Principal": {"AWS": "*"},
            "Action": AUDIT_DENIED_ACTIONS, "Resource": "*",
        }]}},
    })  # fmt: skip


@pytest.mark.req("10.3")
def test_grant_append_events_allows_put_but_never_update_or_delete() -> None:
    app = cdk.App()
    stack = DataStack(app, "GuardIA-Data-Grant")
    role = iam.Role(stack, "Writer", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    stack.grant_append_events(role)
    actions: set[str] = set()
    for policy in Template.from_stack(stack).find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            listed = statement["Action"]
            actions.update([listed] if isinstance(listed, str) else listed)
    assert "dynamodb:PutItem" in actions
    assert not actions & {*AUDIT_DENIED_ACTIONS, "dynamodb:*"}


@pytest.mark.req("13.3")
def test_dev_thresholds_are_published_for_the_test_account() -> None:
    dev = environment("dev")
    stack = DataStack(cdk.App(), "GuardIA-Data-Dev", thresholds=dev.thresholds)
    template = Template.from_stack(stack)
    template.has_resource_properties("AWS::SSM::Parameter", {
        "Name": f"{SSM_PREFIX}/reminder-interval-minutes", "Value": "2",
    })  # fmt: skip
    template.has_resource_properties("AWS::SSM::Parameter", {
        "Name": f"{SSM_PREFIX}/remediation-enabled", "Value": "true",
    })  # fmt: skip


@pytest.mark.parametrize(("env_name", "policy", "pending_days"), [
    ("dev", "Delete", 7),
    ("prod", "Retain", 30),
])  # fmt: skip
def test_removal_policy_follows_the_environment(env_name, policy, pending_days) -> None:
    config = environment(env_name)
    stack = DataStack(cdk.App(), f"GuardIA-Data-{env_name}", retain_data=config.retain_data)
    template = Template.from_stack(stack)
    for kind in ("AWS::DynamoDB::Table", "AWS::SecretsManager::Secret", "AWS::KMS::Key"):
        for resource in template.find_resources(kind).values():
            assert resource["DeletionPolicy"] == policy, kind
    template.has_resource_properties("AWS::KMS::Key", {"PendingWindowInDays": pending_days})
