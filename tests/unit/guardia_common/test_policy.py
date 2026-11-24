"""RemediationPolicy (task 5.1, 5.4): one case per rule and the exact edges."""

from __future__ import annotations

from datetime import timedelta

import pytest

from config import DEFAULT_THRESHOLDS
from guardia_common.models import Incident
from guardia_common.policy import (
    DEFAULT_RETRY_AFTER_S,
    Decision,
    decide,
    retry_after_seconds,
)
from tests.unit.conftest import bogota

NOW = bogota(4, 0)  # 120 minutes before the 06:00 deadline
THRESHOLDS = {**DEFAULT_THRESHOLDS, "remediation-enabled": "true"}


def incident(**overrides) -> Incident:
    base = {
        "incident_id": "i-1",
        "organization": "EMTSAS",
        "process_type": "conversations",
        "kind": "FAILED",
        "deadline_iso": bogota(6, 0).isoformat(),
        "remediation": "auto",
        "resource_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:p",
        "opened_at": NOW.isoformat(),
        "classification": "auto_recoverable",
        "catalog_action": "redrive",
        "catalog_max_wait_s": 0,
        "attempts": 0,
    }
    return Incident.model_validate({**base, **overrides})


def run(inc: Incident, now=NOW, thresholds=THRESHOLDS, **kwargs):
    return decide(inc, thresholds, now, **kwargs)


RULES = [
    pytest.param({"kind": "MISSED_RUN"}, Decision.ESCALATE, "watchdog-detection",
                 marks=pytest.mark.req("4.4"), id="1-missed-run"),
    pytest.param({"kind": "STUCK_RUN"}, Decision.ESCALATE, "watchdog-detection",
                 marks=pytest.mark.req("4.4"), id="1-stuck-run"),
    pytest.param({"classification": "unknown"}, Decision.ESCALATE, "unknown-no-agent",
                 id="2-unknown"),
    pytest.param({"classification": None}, Decision.ESCALATE, "unknown-no-agent",
                 id="2-not-classified"),
    pytest.param({"classification": "requires_escalation", "catalog_action": "escalate"},
                 Decision.ESCALATE, "catalog-requires-escalation", marks=pytest.mark.req("4.3"),
                 id="3-requires-escalation"),
    pytest.param({"remediation": "escalate-only"}, Decision.ESCALATE, "process-escalate-only",
                 marks=pytest.mark.req("5.6"), id="4-escalate-only"),
    pytest.param({"attempts": 2}, Decision.ESCALATE, "attempts-exhausted",
                 marks=pytest.mark.req("5.5"), id="6-two-attempts"),
    pytest.param({"opened_at": (NOW - timedelta(minutes=20)).isoformat()}, Decision.ESCALATE,
                 "remediation-window-exceeded", marks=pytest.mark.req("5.5"), id="7-20-minutes"),
    pytest.param({"catalog_action": "wait_and_redrive", "cause": "429 Retry-After: 601"},
                 Decision.ESCALATE, "wait-too-long", marks=pytest.mark.req("5.3"),
                 id="8-wait-601"),
    pytest.param({"deadline_iso": (NOW + timedelta(minutes=30)).isoformat()},
                 Decision.REMEDIATE_AND_ESCALATE, "low-time", marks=pytest.mark.req("6.1"),
                 id="9-30-minutes-left"),
    pytest.param({}, Decision.REMEDIATE, "recoverable", marks=pytest.mark.req("5.1"),
                 id="10-recoverable"),
]  # fmt: skip


@pytest.mark.parametrize(("overrides", "decision", "reason"), RULES)
def test_each_rule(overrides, decision, reason) -> None:
    result = run(incident(**overrides))
    assert (result.decision, result.reason) == (decision, reason)


def test_observe_only_escalates_even_a_recoverable_incident() -> None:
    result = run(incident(), thresholds={**THRESHOLDS, "remediation-enabled": "false"})
    assert (result.decision, result.reason) == (Decision.ESCALATE, "observe-only")


def test_production_default_is_observe_only() -> None:
    assert run(incident(), thresholds=DEFAULT_THRESHOLDS).reason == "observe-only"


# ---------------------------------------------------------------- the exact edges
@pytest.mark.req("5.5")
def test_one_attempt_left_still_remediates() -> None:
    assert run(incident(attempts=1)).decision == Decision.REMEDIATE


@pytest.mark.req("5.5")
def test_just_under_20_minutes_still_remediates() -> None:
    opened = NOW - timedelta(minutes=19, seconds=59)
    assert run(incident(opened_at=opened.isoformat())).decision == Decision.REMEDIATE


@pytest.mark.req("5.2")
def test_wait_of_exactly_600_seconds_is_allowed() -> None:
    result = run(incident(catalog_action="wait_and_redrive", cause="Retry-After: 600"))
    assert (result.decision, result.wait_seconds) == (Decision.REMEDIATE, 600)


@pytest.mark.req("6.1")
def test_31_minutes_left_is_not_parallel() -> None:
    deadline = NOW + timedelta(minutes=31)
    assert run(incident(deadline_iso=deadline.isoformat())).decision == Decision.REMEDIATE


def test_rules_apply_in_order() -> None:
    # escalate-only and out of attempts: rule 4 is reported, not rule 6
    assert run(incident(remediation="escalate-only", attempts=2)).reason == "process-escalate-only"
    # low time never overrides an escalation rule
    late = incident(classification="requires_escalation", deadline_iso=NOW.isoformat())
    assert run(late).decision == Decision.ESCALATE


def test_minutes_left_is_reported_and_can_be_negative() -> None:
    assert run(incident()).minutes_left == 120
    past = incident(deadline_iso=(NOW - timedelta(minutes=5)).isoformat())
    assert run(past).minutes_left == -5


# ------------------------------------------------------------- inside the parallel
def test_parallel_branch_retries_without_starting_another_escalation() -> None:
    low = incident(deadline_iso=(NOW + timedelta(minutes=10)).isoformat(), attempts=1)
    assert run(low).decision == Decision.REMEDIATE_AND_ESCALATE
    assert run(low, escalation_running=True).decision == Decision.REMEDIATE


def test_parallel_branch_stops_trying_when_the_policy_says_escalate() -> None:
    spent = incident(attempts=2)
    assert run(spent, escalation_running=True).decision == Decision.ESCALATE


# ------------------------------------------------------------------ Retry-After (5.4)
@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("HTTP 429 Too Many Requests. Retry-After: 120", 120),
        ('{"status": 429, "retryAfter": 45}', 45),
        ('{"retry_after": "30"}', 30),
        ("rate limited, retry after 90 seconds", 90),
        ("retry-after=1500 ms", 2),
        ("429 Too Many Requests", None),
        (None, None),
    ],
)
def test_retry_after_is_read_from_the_cause(text, seconds) -> None:
    assert retry_after_seconds(text) == seconds


@pytest.mark.req("5.2")
def test_root_cause_is_read_before_the_final_cause() -> None:
    inc = incident(
        catalog_action="wait_and_redrive",
        root_cause="Genesys 429, Retry-After: 75",
        cause="IngestionController failed",
    )
    assert run(inc).wait_seconds == 75


@pytest.mark.req("5.2")
def test_429_without_retry_after_uses_the_default_wait() -> None:
    inc = incident(catalog_action="wait_and_redrive", cause="429 Too Many Requests")
    assert run(inc).wait_seconds == DEFAULT_RETRY_AFTER_S


def test_plain_redrive_does_not_wait() -> None:
    assert run(incident(cause="Retry-After: 300")).wait_seconds == 0


def test_decision_serializes_for_the_flow() -> None:
    assert run(incident()).as_dict() == {
        "decision": "REMEDIATE", "reason": "recoverable", "wait_seconds": 0, "minutes_left": 120,
    }  # fmt: skip


# ------------------------------------------------------------------ T8 · rule 2 with the agent
def agent_incident(**overrides) -> Incident:
    """An incident the catalog did not know and the agent recommended for."""
    base = {
        "classification": "unknown",
        "catalog_action": None,
        "agent_classification": "auto_recoverable",
        "agent_action": "redrive",
        "agent_confidence": 0.85,
        "agent_wait_seconds": 0,
        "agent_summary": "Un 500 transitorio.",
    }
    return incident(**{**base, **overrides})


@pytest.mark.req("4.5")
def test_an_unknown_incident_uses_the_agents_recommendation_as_if_it_were_the_catalogs() -> None:
    result = run(agent_incident())

    assert (result.decision, result.reason, result.source) == (
        Decision.REMEDIATE,
        "recoverable",
        "agent",
    )
    assert result.as_dict()["source"] == "agent"


def test_the_catalog_decisions_do_not_record_a_source() -> None:
    assert "source" not in run(incident()).as_dict()
    assert "source" not in run(incident(classification="unknown")).as_dict()


@pytest.mark.req("4.5")
def test_the_minimum_confidence_comes_from_the_threshold() -> None:
    low = agent_incident(agent_confidence=0.69)

    assert run(low).reason == "agent-low_confidence"
    assert run(agent_incident(agent_confidence=0.7)).reason == "recoverable"  # exactly the minimum
    strict = {**THRESHOLDS, "agent-min-confidence": "0.9"}
    assert run(agent_incident(agent_confidence=0.85), thresholds=strict).reason == (
        "agent-low_confidence"
    )


@pytest.mark.req("4.6")
@pytest.mark.parametrize(
    "failure", ["timeout", "invalid_output", "provider_error", "low_confidence"]
)
def test_a_failed_agent_escalates_and_the_reason_says_why(failure) -> None:
    failed = incident(classification="unknown", agent_failure=failure)

    result = run(failed)

    assert (result.decision, result.reason) == (Decision.ESCALATE, f"agent-{failure}")


@pytest.mark.req("4.5")
def test_an_agent_that_says_escalate_escalates() -> None:
    by_class = agent_incident(agent_classification="requires_escalation", agent_action="escalate")
    by_action = agent_incident(agent_action="escalate")

    assert run(by_class).reason == "agent-requires-escalation"
    assert run(by_action).reason == "agent-suggests-escalation"


@pytest.mark.req("12.2")
@pytest.mark.parametrize(
    ("overrides", "thresholds", "reason"),
    [
        ({"remediation": "escalate-only"}, THRESHOLDS, "process-escalate-only"),
        ({}, {**THRESHOLDS, "remediation-enabled": "false"}, "observe-only"),
        ({"attempts": 2}, THRESHOLDS, "attempts-exhausted"),
        (
            {"opened_at": (NOW - timedelta(minutes=20)).isoformat()},
            THRESHOLDS,
            "remediation-window-exceeded",
        ),
        (
            {"agent_action": "wait_and_redrive", "agent_wait_seconds": 120},
            {**THRESHOLDS, "max-wait-seconds": "100"},
            "wait-too-long",
        ),
    ],
)
def test_the_agent_cannot_bypass_the_fixed_rules(overrides, thresholds, reason) -> None:
    result = run(agent_incident(**overrides), thresholds=thresholds)

    assert result.decision == Decision.ESCALATE and result.reason == reason


def test_little_time_left_still_recovers_and_escalates_in_parallel_with_the_agent() -> None:
    soon = agent_incident(deadline_iso=(NOW + timedelta(minutes=30)).isoformat())

    result = run(soon)

    assert (result.decision, result.reason) == (Decision.REMEDIATE_AND_ESCALATE, "low-time")


@pytest.mark.req("5.2")
def test_the_agents_wait_is_used_when_the_cause_does_not_say_how_long() -> None:
    waits = agent_incident(agent_action="wait_and_redrive", agent_wait_seconds=120)

    assert run(waits).wait_seconds == 120


def test_a_retry_after_in_the_cause_wins_over_the_agents_guess() -> None:
    told = agent_incident(
        agent_action="wait_and_redrive", agent_wait_seconds=120, cause="429 Retry-After: 45"
    )

    assert run(told).wait_seconds == 45


def test_an_agent_wait_of_zero_falls_back_to_the_default_wait() -> None:
    waits = agent_incident(agent_action="wait_and_redrive", agent_wait_seconds=0)

    assert run(waits).wait_seconds == DEFAULT_RETRY_AFTER_S


def test_the_agent_never_overrides_a_catalog_classification() -> None:
    known = incident(
        classification="requires_escalation",
        catalog_action="escalate",
        agent_classification="auto_recoverable",
        agent_action="redrive",
        agent_confidence=0.99,
    )

    result = run(known)

    assert result.reason == "catalog-requires-escalation" and result.source == "catalog"


def test_the_watchdog_incidents_ignore_the_agent() -> None:
    result = run(agent_incident(kind="MISSED_RUN"))

    assert result.reason == "watchdog-detection"
