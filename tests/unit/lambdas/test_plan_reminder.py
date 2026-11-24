"""PlanReminder (T5 · task 6.5 · requirements 6.2, 8.1)."""

from __future__ import annotations

import pytest

from lambdas.plan_reminder.handler import Deps, IncidentNotFoundError, plan_reminder
from tests.unit.fakes import FakeTable, bogota, fresh_ssm, new_incident_item


def make_deps(**ssm_overrides) -> Deps:
    incidents = FakeTable("incident_id")
    incidents.put_row(new_incident_item("inc-1"))  # deadline 06:00 Colombia
    return Deps(incidents=incidents, ssm=fresh_ssm(ssm_overrides or None))


@pytest.mark.req("8.1")
def test_waits_the_reminder_interval_when_there_is_time():
    result = plan_reminder({"incident_id": "inc-1"}, make_deps(), now=bogota(4, 0))

    assert result == {
        "already_resolved": False,
        "deadline_passed": False,
        "wait_seconds": 600,  # reminder-interval-minutes=10
        "minutes_left": 120,
        "post_deadline_seconds": 6 * 3600,
    }


@pytest.mark.req("8.1")
def test_the_interval_comes_from_the_threshold():
    result = plan_reminder(
        {"incident_id": "inc-1"}, make_deps(**{"reminder-interval-minutes": "2"}), now=bogota(4, 0)
    )

    assert result["wait_seconds"] == 120


@pytest.mark.req("6.2")
def test_five_minutes_before_the_deadline_it_waits_only_until_the_deadline():
    result = plan_reminder({"incident_id": "inc-1"}, make_deps(), now=bogota(5, 55))

    assert result["deadline_passed"] is False
    assert result["wait_seconds"] == 300 and result["minutes_left"] == 5


@pytest.mark.req("6.2")
def test_after_the_deadline_it_says_so_and_keeps_waiting_hours_from_the_threshold():
    result = plan_reminder(
        {"incident_id": "inc-1"},
        make_deps(**{"post-deadline-wait-hours": "3"}),
        now=bogota(6, 20),
    )

    assert result["deadline_passed"] is True
    assert result["minutes_left"] == -20
    assert result["post_deadline_seconds"] == 3 * 3600
    assert result["wait_seconds"] >= 1  # Step Functions needs at least 1 second


def test_exactly_at_the_deadline_it_has_passed():
    assert plan_reminder({"incident_id": "inc-1"}, make_deps(), now=bogota(6, 0))["deadline_passed"]


def test_a_missing_incident_is_an_error_not_a_guess():
    with pytest.raises(IncidentNotFoundError):
        plan_reminder({"incident_id": "nope"}, make_deps(), now=bogota(4, 0))
    with pytest.raises(ValueError, match="incident_id"):
        plan_reminder({}, make_deps(), now=bogota(4, 0))


def test_it_writes_nothing():
    deps = make_deps()
    before = dict(deps.incidents.rows)

    plan_reminder({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    assert deps.incidents.rows == before


@pytest.mark.req("6.1")
def test_it_says_when_the_recovery_already_resolved_the_incident():
    deps = make_deps()
    deps.incidents.only()["status"] = "RESOLVED"

    result = plan_reminder({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    assert result["already_resolved"] is True
