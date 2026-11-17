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
