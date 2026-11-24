"""RemediationPolicy Lambda (task 5.1): reads everything from the table, audits the decision."""

from __future__ import annotations

import pytest
from boto3.dynamodb.conditions import Key

from lambdas.classify_incident.handler import Deps as ClassifyDeps
from lambdas.classify_incident.handler import classify_incident
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident
from lambdas.remediation_policy.handler import (
    Deps,
    IncidentNotFoundError,
    remediation_policy,
)
from tests.unit.conftest import SSM_PREFIX, bogota, status_event


@pytest.fixture
def classified(catalog_loaded):
    aws = catalog_loaded
    t = aws["tables"]

    def _open(cause: str, now=None) -> str:
        now = now or bogota(4, 0)
        opened = open_incident(
            status_event(cause=cause),
            OpenDeps(t["Incidents"], t["ProcessRegistry"], t["IncidentEvents"],
                     aws["sfn"], aws["ssm"], aws["flow_arn"]),
            now=now,
        )  # fmt: skip
        classify_incident(
            {"incident_id": opened["incident_id"]},
            ClassifyDeps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"]),
            now=now,
        )
        return opened["incident_id"]

    return aws, _open


def _deps(aws) -> Deps:
    return Deps(aws["tables"]["Incidents"], aws["tables"]["IncidentEvents"], aws["ssm"])


def _enable_remediation(aws) -> None:
    aws["ssm"].put_parameter(
        Name=f"{SSM_PREFIX}/remediation-enabled", Value="true", Type="String", Overwrite=True
    )


@pytest.mark.req("5.2")
def test_429_with_retry_after_waits_then_remediates(classified) -> None:
    aws, open_ = classified
    _enable_remediation(aws)
    incident_id = open_('{"status": 429, "message": "Too Many Requests", "retryAfter": 90}')

    out = remediation_policy({"incident_id": incident_id}, _deps(aws), now=bogota(4, 1))

    assert out == {
        "incident_id": incident_id, "decision": "REMEDIATE", "reason": "recoverable",
        "wait_seconds": 90, "minutes_left": 119,
    }  # fmt: skip


@pytest.mark.req("5.6")
def test_default_thresholds_are_observe_only(classified) -> None:
    aws, open_ = classified
    incident_id = open_('{"status": 429, "message": "Too Many Requests"}')

    out = remediation_policy({"incident_id": incident_id}, _deps(aws), now=bogota(4, 1))

    assert (out["decision"], out["reason"]) == ("ESCALATE", "observe-only")


def test_the_event_cannot_steer_the_decision(classified) -> None:
    aws, open_ = classified
    _enable_remediation(aws)
    incident_id = open_("403 Forbidden")  # requires_escalation in the catalog

    out = remediation_policy(
        {"incident_id": incident_id, "classification": {"classification": "auto_recoverable"},
         "remediation": "auto"},
        _deps(aws),
        now=bogota(4, 1),
    )  # fmt: skip

    assert (out["decision"], out["reason"]) == ("ESCALATE", "catalog-requires-escalation")


def test_decision_is_audited(classified) -> None:
    aws, open_ = classified
    _enable_remediation(aws)
    incident_id = open_("500 Server Error")

    remediation_policy({"incident_id": incident_id}, _deps(aws), now=bogota(4, 1))

    events = aws["tables"]["IncidentEvents"].query(
        KeyConditionExpression=Key("incident_id").eq(incident_id)
    )["Items"]
    (decided,) = [e for e in events if e["event_type"] == "POLICY_DECIDED"]
    assert decided["actor"] == "RemediationPolicy"
    assert decided["detail"]["decision"] == "REMEDIATE"
    assert decided["detail"]["attempts"] == 0


def test_escalation_running_flag_reaches_the_policy(classified) -> None:
    aws, open_ = classified
    _enable_remediation(aws)
    incident_id = open_("500 Server Error", now=bogota(5, 45))  # 15 minutes left

    first = remediation_policy({"incident_id": incident_id}, _deps(aws), now=bogota(5, 45))
    again = remediation_policy(
        {"incident_id": incident_id, "escalation_running": True}, _deps(aws), now=bogota(5, 46)
    )

    assert first["decision"] == "REMEDIATE_AND_ESCALATE"
    assert again["decision"] == "REMEDIATE"


def test_unknown_or_missing_incident(catalog_loaded) -> None:
    deps = _deps(catalog_loaded)
    with pytest.raises(IncidentNotFoundError):
        remediation_policy({"incident_id": "nope"}, deps)
    with pytest.raises(ValueError, match="incident_id"):
        remediation_policy({}, deps)
