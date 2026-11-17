"""The scripted night of `scripts/demo_incident_flow.py`: the eight Lambdas chained on the
virtual clock, with the stand-in for the incident flow (tasks 5.1, 5.4, 5.5 and 6.4)."""

from __future__ import annotations

import json

import pytest
from demo_incident_flow import decide, run_night


@pytest.fixture(scope="module")
def night():
    return run_night(say=lambda line: None)


def incident(night, organization):
    return next(row for row in night["incidents"] if row["organization"] == organization)


@pytest.mark.req("5.1")
@pytest.mark.parametrize(
    ("classification", "remediation", "attempts", "expected"),
    [
        ("auto_recoverable", "auto", 0, "recover"),
        ("auto_recoverable", "escalate-only", 0, "escalate"),
        ("auto_recoverable", "auto", 2, "escalate"),
        ("requires_escalation", "auto", 0, "escalate"),
        ("unknown", "auto", 0, "escalate"),
    ],
)
def test_the_stand_in_policy_only_recovers_what_is_allowed(
    classification, remediation, attempts, expected
):
    assert decide(classification, remediation, attempts, 2)[0] == expected


@pytest.mark.req("5.1")
@pytest.mark.req("5.4")
def test_a_recoverable_failure_is_redriven_polled_and_resolved_without_escalating(night):
    row = incident(night, "EMTSAS")

    assert row["kind"] == "FAILED" and row["classification"] == "auto_recoverable"
    assert row["status"] == "RESOLVED"
    assert "RECOVERY_STARTED" in row["events"] and "RECOVERY_FINISHED" in row["events"]
    assert "ESCALATION_SENT" not in row["events"]


@pytest.mark.req("7.2")
def test_a_failure_that_needs_a_human_posts_the_card_and_mentions_the_on_call(night):
    row = incident(night, "EMT12")

    assert row["classification"] == "requires_escalation" and row["status"] == "ESCALATED"
    assert row["events"].index("ONCALL_RESOLVED") < row["events"].index("ESCALATION_SENT")
    assert any("<at>Ana Guardia</at>" in json.dumps(card) for card in night["cards"])


@pytest.mark.req("7.3")
def test_teams_down_records_the_failure_after_three_attempts_and_sends_no_email(night):
    row = incident(night, "EMT22")

    assert row["kind"] == "MISSED_RUN"
    assert "NOTIFICATION_FAILED" in row["events"] and "ESCALATION_SENT" not in row["events"]
    assert row["status"] != "ESCALATED"
    assert night["teams_rejected"] == 3
