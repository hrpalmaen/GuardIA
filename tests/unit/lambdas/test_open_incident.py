from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from guardia_common.incidents import InvalidDetectionError
from lambdas.open_incident.handler import Deps, open_incident
from tests.unit.conftest import EXEC_PREFIX, SM_PREFIX, bogota, status_event


class SfnStub:
    """Real moto client, with DescribeExecution answered by the test."""

    def __init__(self, client, describe=None, fail_code: str | None = None):
        self._client = client
        self._describe = describe or {}
        self._fail_code = fail_code
        self.describe_calls = 0

    def describe_execution(self, executionArn):  # noqa: N803 (boto3 signature)
        self.describe_calls += 1
        if self._fail_code:
            raise ClientError({"Error": {"Code": self._fail_code, "Message": "x"}}, "Describe")
        return {"executionArn": executionArn, **self._describe}

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def deps(aws):
    def make(describe=None, fail_code=None) -> Deps:
        t = aws["tables"]
        return Deps(
            incidents=t["Incidents"],
            registry=t["ProcessRegistry"],
            events=t["IncidentEvents"],
            sfn=SfnStub(aws["sfn"], describe, fail_code),
            ssm=aws["ssm"],
            flow_arn=aws["flow_arn"],
        )

    return make


def _events(aws, incident_id):
    resp = aws["tables"]["IncidentEvents"].query(
        KeyConditionExpression="incident_id = :i", ExpressionAttributeValues={":i": incident_id}
    )
    return [e["event_type"] for e in resp["Items"]]


def _flow_executions(aws):
    return aws["sfn"].list_executions(stateMachineArn=aws["flow_arn"])["executions"]


# --------------------------------------------------------------------------- 2.3
@pytest.mark.req("2.3")
def test_records_organization_process_execution_cause_and_deadline(aws, deps):
    out = open_incident(status_event(), deps(), now=bogota(4, 0))
    item = aws["tables"]["Incidents"].get_item(Key={"incident_id": out["incident_id"]})["Item"]

    assert out["opened"] and out["created"]
    assert item["organization"] == "OrgA"
    assert item["process_type"] == "master"
    assert item["execution_arn"] == f"{EXEC_PREFIX}:genesys-master-orgA:run-1"
    assert "429" in item["cause"]
    assert item["deadline_iso"] == "2026-11-12T06:00:00-05:00"
    assert item["kind"] == "FAILED" and item["status"] == "OPEN" and item["attempts"] == 0
    assert item["remediation"] == "auto"
    assert out["minutes_left"] == 120


@pytest.mark.req("2.3")
def test_enriches_error_cause_and_input_with_describe_execution(aws, deps):
    d = deps(
        describe={
            "error": "States.TaskFailed",
            "cause": "HTTP 403 Forbidden",
            "input": '{"org": "OrgA"}',
        }
    )
    out = open_incident(status_event(error=None, cause=None), d, now=bogota(4, 0))
    item = aws["tables"]["Incidents"].get_item(Key={"incident_id": out["incident_id"]})["Item"]

    assert d.sfn.describe_calls == 1
    assert item["error"] == "States.TaskFailed"
    assert item["cause"] == "HTTP 403 Forbidden"
    assert item["execution_input"] == '{"org": "OrgA"}'


@pytest.mark.req("2.3")
def test_describe_execution_failure_does_not_lose_the_detection(aws, deps):
    out = open_incident(status_event(), deps(fail_code="ThrottlingException"), now=bogota(4, 0))
    item = aws["tables"]["Incidents"].get_item(Key={"incident_id": out["incident_id"]})["Item"]
    assert out["created"] and "429" in item["cause"]


@pytest.mark.req("2.3")
def test_deadline_is_per_process_when_registry_overrides_it(deps):
    out = open_incident(status_event(machine="genesys-master-orgC"), deps(), now=bogota(4, 0))
    assert out["incident"]["deadline_iso"] == "2026-11-12T07:30:00-05:00"


@pytest.mark.req("2.3")
def test_run_started_after_deadline_is_due_next_day(deps):
    event = status_event(start=bogota(22, 0), stop=bogota(22, 30))
    out = open_incident(event, deps(), now=bogota(22, 30))
    assert out["incident"]["deadline_iso"] == "2026-11-13T06:00:00-05:00"
    assert out["minutes_left"] == 450


# --------------------------------------------------------------------------- 2.4
@pytest.mark.req("2.4")
def test_duplicate_event_does_not_create_a_second_incident_or_flow(aws, deps):
    first = open_incident(status_event(), deps(), now=bogota(4, 0))
    second = open_incident(status_event(), deps(), now=bogota(4, 1))

    assert first["incident_id"] == second["incident_id"]
    assert first["created"] and not second["created"]
    assert aws["tables"]["Incidents"].scan()["Count"] == 1
    assert len(_flow_executions(aws)) == 1
    assert _events(aws, first["incident_id"]) == ["INCIDENT_OPENED"]


@pytest.mark.req("2.4")
def test_different_executions_open_different_incidents(deps):
    a = open_incident(status_event(run="run-1"), deps(), now=bogota(4, 0))
    b = open_incident(status_event(run="run-2"), deps(), now=bogota(4, 0))
    assert a["incident_id"] != b["incident_id"]


@pytest.mark.req("2.4")
def test_retry_after_flow_start_failure_starts_the_flow_once(aws, deps):
    d = deps()

    class BrokenStart(SfnStub):
        def start_execution(self, **kwargs):
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"}}, "Start")

    d.sfn = BrokenStart(aws["sfn"])
    with pytest.raises(ClientError):
        open_incident(status_event(), d, now=bogota(4, 0))
    assert len(_flow_executions(aws)) == 0

    retry = open_incident(status_event(), deps(), now=bogota(4, 1))
    assert not retry["created"]
    assert retry["flow_execution_arn"].endswith(retry["incident_id"])
    assert len(_flow_executions(aws)) == 1


@pytest.mark.req("2.4")
def test_flow_is_started_with_the_incident_contract(aws, deps):
    out = open_incident(status_event(), deps(), now=bogota(4, 0))
    execution = _flow_executions(aws)[0]
    payload = json.loads(
        aws["sfn"].describe_execution(executionArn=execution["executionArn"])["input"]
    )

    assert execution["name"] == out["incident_id"]
    assert set(payload) == {
        "incident_id", "execution_arn", "organization", "process_type", "kind",
        "error", "cause", "deadline_iso", "remediation",
    }  # fmt: skip


# --------------------------------------------------------------------------- 2.2
@pytest.mark.req("2.2")
@pytest.mark.parametrize("status", ["SUCCEEDED", "RUNNING", "PENDING_REDRIVE"])
def test_non_failure_statuses_open_nothing(aws, deps, status):
    out = open_incident(status_event(status=status), deps(), now=bogota(4, 0))
    assert out == {"opened": False, "reason": "ignored_event"}
    assert aws["tables"]["Incidents"].scan()["Count"] == 0
    assert _flow_executions(aws) == []


@pytest.mark.req("2.1")
@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_failure_statuses_open_an_incident(deps, status):
    out = open_incident(status_event(status=status), deps(), now=bogota(4, 0))
    assert out["created"] and out["incident"]["kind"] == status


def test_process_not_in_registry_is_ignored(aws, deps):
    out = open_incident(status_event(machine="untagged-process"), deps(), now=bogota(4, 0))
    assert out == {"opened": False, "reason": "not_monitored"}
    assert aws["tables"]["Incidents"].scan()["Count"] == 0


# --------------------------------------------------------------------------- watchdog
@pytest.mark.req("3.1")
def test_missed_run_from_watchdog_is_idempotent_per_scheduled_time(aws, deps):
    detection = {
        "kind": "MISSED_RUN",
        "resource_arn": f"{SM_PREFIX}:genesys-master-orgA",
        "expected_at": "2026-11-12T04:30:00-05:00",
    }
    a = open_incident(detection, deps(), now=bogota(4, 45))
    b = open_incident(
        {"source": "guardia.watchdog", "detail": detection}, deps(), now=bogota(4, 50)
    )

    assert a["created"] and not b["created"]
    assert a["incident"]["kind"] == "MISSED_RUN" and a["incident"]["execution_arn"] is None
    assert a["minutes_left"] == 75
    assert aws["tables"]["Incidents"].scan()["Count"] == 1


@pytest.mark.req("3.2")
def test_stuck_run_uses_the_execution_as_key(deps):
    arn = f"{EXEC_PREFIX}:genesys-master-orgA:run-9"
    detection = {
        "kind": "STUCK_RUN",
        "resource_arn": f"{SM_PREFIX}:genesys-master-orgA",
        "execution_arn": arn,
        "started_at": "2026-11-12T02:00:00-05:00",
    }
    stuck = open_incident(detection, deps(), now=bogota(4, 0))
    later_failed = open_incident(status_event(run="run-9"), deps(), now=bogota(4, 30))
    assert stuck["created"] and not later_failed["created"]
    assert stuck["incident_id"] == later_failed["incident_id"]


@pytest.mark.parametrize(
    "detection",
    [
        {"kind": "MISSED_RUN", "resource_arn": f"{SM_PREFIX}:genesys-master-orgA"},
        {"kind": "STUCK_RUN", "resource_arn": f"{SM_PREFIX}:genesys-master-orgA"},
        {"kind": "MISSED_RUN", "expected_at": "2026-11-12T04:30:00-05:00"},
    ],
)
def test_incomplete_watchdog_detection_is_rejected(deps, detection):
    with pytest.raises(InvalidDetectionError):
        open_incident(detection, deps(), now=bogota(4, 45))


# --------------------------------------------------------------------------- 10.1
@pytest.mark.req("10.1")
def test_opening_is_recorded_in_incident_events_with_timestamp(aws, deps):
    out = open_incident(status_event(), deps(), now=bogota(4, 0))
    items = aws["tables"]["IncidentEvents"].query(
        KeyConditionExpression="incident_id = :i",
        ExpressionAttributeValues={":i": out["incident_id"]},
    )["Items"]
    assert len(items) == 1
    assert items[0]["event_type"] == "INCIDENT_OPENED"
    assert items[0]["timestamp"] == "2026-11-12T04:00:00.000-05:00"
    assert items[0]["sk"].startswith("2026-11-12T04:00:00.000-05:00#")
    assert "cause" not in items[0]["detail"]
