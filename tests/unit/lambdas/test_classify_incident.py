from __future__ import annotations

import pytest

from lambdas.classify_incident.handler import Deps, IncidentNotFoundError, classify_incident
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident
from tests.unit.conftest import SM_PREFIX, bogota, status_event


@pytest.fixture
def run(catalog_loaded):
    aws = catalog_loaded
    t = aws["tables"]

    def _run(event: dict) -> dict:
        opened = open_incident(
            event,
            OpenDeps(
                t["Incidents"],
                t["ProcessRegistry"],
                t["IncidentEvents"],
                aws["sfn"],
                aws["ssm"],
                aws["flow_arn"],
            ),
            now=bogota(4, 0),
        )
        return classify_incident(
            {"incident_id": opened["incident_id"]},
            Deps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"]),
            now=bogota(4, 0),
        )

    return _run


def failed(error, cause, status="FAILED", machine="genesys-master-orgA"):
    return status_event(status=status, machine=machine, error=error, cause=cause)


# Table-driven cases from task 4.1: 429, 403, failed 404, TIMED_OUT, MISSED_RUN, unknown.
CASES = [
    pytest.param(
        failed("Genesys.ApiError", '{"status": 429, "message": "Too Many Requests"}'),
        "auto_recoverable",
        "wait_and_redrive",
        600,
        marks=pytest.mark.req("4.2"),
        id="429",
    ),
    pytest.param(
        failed("Genesys.ApiError", "rate limit exceeded for org"),
        "auto_recoverable",
        "wait_and_redrive",
        600,
        marks=pytest.mark.req("4.2"),
        id="rate-limit-text",
    ),
    pytest.param(
        failed("Genesys.ApiError", "HTTP 403 Forbidden"),
        "requires_escalation",
        "escalate",
        0,
        marks=pytest.mark.req("4.3"),
        id="403",
    ),
    pytest.param(
        failed("Genesys.Auth", '{"error": "invalid_client"}'),
        "requires_escalation",
        "escalate",
        0,
        marks=pytest.mark.req("4.3"),
        id="authentication",
    ),
    pytest.param(
        failed("Genesys.ApiError", "HTTP 404 Not Found: /api/v2/analytics"),
        "requires_escalation",
        "escalate",
        0,
        marks=pytest.mark.req("4.4"),
        id="failed-404",
    ),
    pytest.param(
        failed("States.Timeout", None, status="TIMED_OUT"),
        "auto_recoverable",
        "redrive",
        0,
        marks=pytest.mark.req("4.1"),
        id="timed-out",
    ),
    pytest.param(
        {
            "kind": "MISSED_RUN",
            "resource_arn": f"{SM_PREFIX}:genesys-master-orgA",
            "expected_at": "2026-11-12T03:30:00-05:00",
        },
        "requires_escalation",
        "escalate",
        0,
        marks=pytest.mark.req("4.4"),
        id="missed-run",
    ),
    pytest.param(
        failed("KeyError", "KeyError: 'participants'"),
        "unknown",
        None,
        None,
        marks=pytest.mark.req("4.5"),
        id="unknown",
    ),
]


@pytest.mark.req("4.1")
@pytest.mark.parametrize("event,classification,action,max_wait", CASES)
def test_catalog_classification(run, event, classification, action, max_wait):
    out = run(event)
    assert out["classification"] == classification
    assert out["action"] == action
    assert out["max_wait_s"] == max_wait


@pytest.mark.req("4.3")
def test_ambiguous_cause_prefers_escalation(run):
    out = run(failed("Genesys.ApiError", "429 Too Many Requests after 403 Forbidden"))
    assert out["classification"] == "requires_escalation"


@pytest.mark.req("4.1")
def test_classification_is_stored_on_the_incident(run, catalog_loaded):
    out = run(failed("Genesys.ApiError", "HTTP 429"))
    item = catalog_loaded["tables"]["Incidents"].get_item(Key={"incident_id": out["incident_id"]})
    assert item["Item"]["classification"] == "auto_recoverable"
    assert item["Item"]["catalog_action"] == "wait_and_redrive"
    assert item["Item"]["status"] == "OPEN"  # ClassifyIncident never changes the lifecycle


@pytest.mark.req("4.1")
def test_process_type_rows_take_precedence_over_wildcard(run, catalog_loaded):
    catalog_loaded["tables"]["ErrorCatalog"].put_item(
        Item={
            "process_type": "conversations",
            "error_pattern": r"\b429\b",
            "classification": "requires_escalation",
            "action": "escalate",
            "max_wait_s": 0,
        }
    )
    master = run(failed("Genesys.ApiError", "HTTP 429"))
    conversations = run(
        status_event(
            machine="genesys-conversations-orgB",
            run="r2",
            error="Genesys.ApiError",
            cause="HTTP 429",
        )
    )
    assert master["classification"] == "auto_recoverable"
    assert conversations["classification"] == "requires_escalation"
    assert conversations["matched_process_type"] == "conversations"


def test_invalid_catalog_row_is_skipped(run, catalog_loaded):
    catalog_loaded["tables"]["ErrorCatalog"].put_item(
        Item={
            "process_type": "*",
            "error_pattern": "KeyError",
            "classification": "fix_it_yourself",
            "action": "delete_everything",
        }
    )
    out = run(failed("KeyError", "KeyError: 'participants'"))
    assert out["classification"] == "unknown"


@pytest.mark.req("10.1")
def test_classification_is_recorded_in_incident_events(run, catalog_loaded):
    out = run(failed("Genesys.ApiError", "HTTP 429"))
    items = catalog_loaded["tables"]["IncidentEvents"].query(
        KeyConditionExpression="incident_id = :i",
        ExpressionAttributeValues={":i": out["incident_id"]},
    )["Items"]
    assert [i["event_type"] for i in items] == ["INCIDENT_OPENED", "INCIDENT_CLASSIFIED"]
    assert items[1]["detail"]["classification"] == "auto_recoverable"


def test_unknown_incident_id_raises(catalog_loaded):
    t = catalog_loaded["tables"]
    with pytest.raises(IncidentNotFoundError):
        classify_incident(
            {"incident_id": "nope"}, Deps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"])
        )


def test_missing_incident_id_raises(catalog_loaded):
    t = catalog_loaded["tables"]
    with pytest.raises(ValueError):
        classify_incident({}, Deps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"]))
