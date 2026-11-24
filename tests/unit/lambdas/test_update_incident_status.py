from __future__ import annotations

import pytest
from boto3.dynamodb.conditions import Key

from guardia_common.models import Incident
from guardia_common.redaction import REDACTED
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident
from lambdas.update_incident_status.handler import (
    Deps,
    IncidentNotFoundError,
    InvalidStatusUpdateError,
    update_incident_status,
)
from tests.unit.conftest import bogota, status_event


@pytest.fixture
def opened(aws) -> tuple[dict, str]:
    t = aws["tables"]
    result = open_incident(
        status_event(),
        OpenDeps(
            t["Incidents"], t["ProcessRegistry"], t["IncidentEvents"],
            aws["sfn"], aws["ssm"], aws["flow_arn"],
        ),
        now=bogota(4, 0),
    )  # fmt: skip
    return aws, result["incident_id"]


def _deps(aws) -> Deps:
    return Deps(aws["tables"]["Incidents"], aws["tables"]["IncidentEvents"])


def _incident(aws, incident_id: str) -> Incident:
    item = aws["tables"]["Incidents"].get_item(Key={"incident_id": incident_id})["Item"]
    return Incident.from_item(item)


def _events(aws, incident_id: str) -> list[dict]:
    return aws["tables"]["IncidentEvents"].query(
        KeyConditionExpression=Key("incident_id").eq(incident_id)
    )["Items"]


@pytest.mark.req("10.1")
def test_escalation_changes_status_and_is_audited(opened) -> None:
    aws, incident_id = opened
    out = update_incident_status(
        {"incident_id": incident_id, "status": "ESCALATED", "details": {"reason": "catalog"}},
        _deps(aws),
        now=bogota(4, 5),
    )

    assert out == {
        "incident_id": incident_id, "status": "ESCALATED", "previous_status": "OPEN",
        "changed": True, "reason": None,
    }  # fmt: skip
    incident = _incident(aws, incident_id)
    assert incident.status == "ESCALATED"
    assert incident.updated_at == bogota(4, 5).isoformat()
    audit = [e for e in _events(aws, incident_id) if e["event_type"] == "INCIDENT_ESCALATED"]
    assert len(audit) == 1
    assert audit[0]["detail"] == {
        "status": "ESCALATED", "previous_status": "OPEN", "reason": "catalog",
    }  # fmt: skip
    assert audit[0]["actor"] == "GuardIA-Incident-Flow"


@pytest.mark.req("8.3")
def test_resolution_records_when_and_by_whom(opened) -> None:
    aws, incident_id = opened
    update_incident_status(
        {"incident_id": incident_id, "status": "RESOLVED",
         "details": {"resolved_by": "later_execution"}},
        _deps(aws),
        now=bogota(5, 10),
    )  # fmt: skip

    incident = _incident(aws, incident_id)
    assert incident.status == "RESOLVED"
    assert incident.resolved_at == bogota(5, 10).isoformat()
    assert incident.resolved_by == "later_execution"
    assert any(e["event_type"] == "INCIDENT_RESOLVED" for e in _events(aws, incident_id))


def test_same_status_twice_changes_and_audits_nothing(opened) -> None:
    aws, incident_id = opened
    event = {"incident_id": incident_id, "status": "ESCALATED"}
    update_incident_status(event, _deps(aws), now=bogota(4, 5))
    before = len(_events(aws, incident_id))

    out = update_incident_status(event, _deps(aws), now=bogota(4, 6))

    assert out["changed"] is False and out["reason"] == "same_status"
    assert len(_events(aws, incident_id)) == before


def test_resolved_is_final(opened) -> None:
    aws, incident_id = opened
    update_incident_status({"incident_id": incident_id, "status": "RESOLVED"}, _deps(aws))

    out = update_incident_status({"incident_id": incident_id, "status": "SLA_BREACHED"}, _deps(aws))

    assert out == {
        "incident_id": incident_id, "status": "RESOLVED", "previous_status": "RESOLVED",
        "changed": False, "reason": "already_final",
    }  # fmt: skip
    assert _incident(aws, incident_id).status == "RESOLVED"


@pytest.mark.parametrize(
    "event",
    [
        {"status": "ESCALATED"},
        {"incident_id": "x", "status": "DONE"},
        {"incident_id": "x", "status": "OPEN"},  # only OpenIncident opens incidents
        {"incident_id": "x", "status": "ESCALATED", "event_type": "drop table"},
        {"incident_id": "x", "status": "ESCALATED", "details": "text"},
    ],
    ids=["no-id", "unknown-status", "open", "bad-event-type", "details-not-object"],
)
def test_invalid_requests_are_rejected(aws, event) -> None:
    with pytest.raises(InvalidStatusUpdateError):
        update_incident_status(event, _deps(aws))


def test_unknown_incident_is_an_error(aws) -> None:
    with pytest.raises(IncidentNotFoundError):
        update_incident_status({"incident_id": "nope", "status": "ESCALATED"}, _deps(aws))


@pytest.mark.req("10.1")
def test_details_are_redacted_before_auditing(opened) -> None:
    aws, incident_id = opened
    update_incident_status(
        {"incident_id": incident_id, "status": "ESCALATED",
         "details": {"access_token": "abc123", "note": "ok"}},
        _deps(aws),
    )  # fmt: skip

    (audit,) = [e for e in _events(aws, incident_id) if e["event_type"] == "INCIDENT_ESCALATED"]
    assert audit["detail"]["access_token"] == REDACTED
    assert audit["detail"]["note"] == "ok"


def test_new_incident_fields_survive_a_read() -> None:
    item = {
        "incident_id": "i", "organization": "o", "process_type": "p", "kind": "FAILED",
        "deadline_iso": "2026-11-12T06:00:00-05:00", "remediation": "auto",
        "resource_arn": "arn", "opened_at": "t", "teams_thread_id": "th",
        "task_token": "tok", "resolved_by": "later_execution",
    }  # fmt: skip
    incident = Incident.from_item(item)
    assert (incident.teams_thread_id, incident.task_token, incident.resolved_by) == (
        "th", "tok", "later_execution",
    )  # fmt: skip
