"""The replay client must be indistinguishable from boto3 for the code that uses it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import ParamValidator

from guardia_local.events import status_change_event
from guardia_local.fixtures import DEFAULT_DIR
from guardia_local.stepfunctions import ReplayStepFunctionsClient
from tests.unit.conftest import EMT12_OOM, EMTSAS_500

MODEL = botocore.session.get_session().get_service_model("stepfunctions")


def _assert_matches_api(operation: str, response: dict) -> None:
    shape = MODEL.operation_model(operation).output_shape
    report = ParamValidator().validate(response, shape)
    assert not report.has_errors(), report.generate_report()


@pytest.fixture
def client(real_executions):
    return ReplayStepFunctionsClient(real_executions)


# ------------------------------------------------------------------ API shape
def test_every_fixture_matches_the_real_api_output_shape(client, real_executions):
    for execution in real_executions.executions.values():
        arn = execution.execution_arn
        _assert_matches_api("DescribeExecution", client.describe_execution(executionArn=arn))
        _assert_matches_api(
            "GetExecutionHistory", client.get_execution_history(executionArn=arn, maxResults=1000)
        )


def test_timestamps_are_datetimes_like_boto3(client, real_executions):
    arn = real_executions.by_id(EMTSAS_500).execution_arn
    describe = client.describe_execution(executionArn=arn)
    event = client.get_execution_history(executionArn=arn)["events"][0]
    assert describe["startDate"].tzinfo is not None
    assert event["timestamp"].tzinfo is not None


def test_wrong_parameter_fails_like_the_real_client(client, real_executions):
    arn = real_executions.by_id(EMTSAS_500).execution_arn
    with pytest.raises(ParamValidationError):
        client.get_execution_history(executionArn=arn, pageSize=10)


def test_unknown_execution_raises_execution_does_not_exist(client):
    with pytest.raises(ClientError) as err:
        client.describe_execution(
            executionArn="arn:aws:states:us-east-1:123456789012:execution:x:y"
        )
    assert err.value.response["Error"]["Code"] == "ExecutionDoesNotExist"


# ----------------------------------------------------------------- pagination
def test_pagination_and_reverse_order(client, real_executions):
    arn = real_executions.by_id(EMT12_OOM).execution_arn
    first = client.get_execution_history(executionArn=arn, maxResults=600)
    second = client.get_execution_history(
        executionArn=arn, maxResults=600, nextToken=first["nextToken"]
    )
    newest = client.get_execution_history(executionArn=arn, maxResults=1, reverseOrder=True)

    assert len(first["events"]) == 600 and "nextToken" not in second
    assert len(first["events"]) + len(second["events"]) == 990
    assert newest["events"][0]["type"] == "ExecutionFailed"


def test_metadata_only_omits_input(client, real_executions):
    arn = real_executions.by_id(EMTSAS_500).execution_arn
    assert "input" not in client.describe_execution(executionArn=arn, includedData="METADATA_ONLY")


# ------------------------------------------------------------------- fallback
def test_other_calls_go_to_the_fallback_client(real_executions):
    class Fallback:
        exceptions = object()

        def describe_execution(self, **params):
            return {"from": "fallback", **params}

        def start_execution(self, **params):
            return {"executionArn": "started"}

    client = ReplayStepFunctionsClient(real_executions, fallback=Fallback())
    assert client.start_execution(stateMachineArn="x")["executionArn"] == "started"
    unknown = "arn:aws:states:us-east-1:123456789012:execution:x:y"
    assert client.describe_execution(executionArn=unknown)["from"] == "fallback"
    assert client.exceptions is Fallback.exceptions


# --------------------------------------------------------------- event shape
def test_status_change_event_has_the_eventbridge_shape(real_executions):
    describe = real_executions.by_id(EMTSAS_500).describe_response()
    event = status_change_event(describe)

    assert event["source"] == "aws.states"
    assert event["detail-type"] == "Step Functions Execution Status Change"
    assert event["resources"] == [describe["executionArn"]]
    detail = event["detail"]
    assert detail["status"] == "FAILED" and detail["error"] == "IngestionControllerError"
    assert isinstance(detail["startDate"], int) and isinstance(detail["stopDate"], int)


# -------------------------------------------------------------- data hygiene
def test_fixtures_contain_no_credentials_or_real_account():
    token_like = re.compile(r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])[A-Za-z0-9_-]{60,}")
    for path in Path(DEFAULT_DIR).rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not token_like.search(text), path
        accounts = set(re.findall(r"arn:aws:[a-z-]+:[a-z0-9-]*:(\d{12}):", text))
        assert accounts <= {"123456789012"}, path
        for match in re.finditer(r'accessTokens?\\*"\s*:\s*\\*"?([^",\\\]]*)', text):
            assert match.group(1) in ("REDACTED", ""), path


def test_manifest_tags_are_the_ones_guardia_expects():
    manifest = json.loads((Path(DEFAULT_DIR) / "manifest.json").read_text(encoding="utf-8"))
    for machine in manifest["state_machines"]:
        assert set(machine["tags"]) == {
            "guardia:monitor",
            "guardia:process-type",
            "guardia:organization",
            "guardia:remediation",
        }


# ---------------------------------------------------------------- time shift
def test_with_start_moves_every_timestamp_and_keeps_the_duration(real_executions):
    from datetime import datetime

    from tests.unit.conftest import BOGOTA

    start = datetime(2026, 11, 12, 0, 0, tzinfo=BOGOTA)
    shifted = real_executions.with_start(EMTSAS_500, start)
    original = real_executions.by_id(EMTSAS_500)
    moved = shifted.by_id(EMTSAS_500)

    before = original.describe_response()
    after = moved.describe_response()
    assert after["startDate"] == start
    assert after["stopDate"] - after["startDate"] == before["stopDate"] - before["startDate"]
    assert moved.history_events()[0]["timestamp"] == start + (
        original.history_events()[0]["timestamp"] - before["startDate"]
    )
    assert real_executions.by_id(EMTSAS_500).describe == original.describe  # original untouched
