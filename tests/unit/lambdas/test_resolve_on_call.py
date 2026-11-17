"""ResolveOnCall Lambda (task 6.1 · requirements 7.1, 7.4)."""

from __future__ import annotations

import pytest

from lambdas.resolve_on_call.handler import Deps, resolve_oncall
from tests.unit.fakes import BACKUP, PRIMARY, FakeTable, bogota, event_rows, fresh_ssm


def make_deps(*rows: dict) -> Deps:
    schedule = FakeTable("week_start")
    for row in rows:
        schedule.put_row(row)
    return Deps(schedule=schedule, events=FakeTable("incident_id", "sk"), ssm=fresh_ssm())


WEEK = {"week_start": "2026-11-09", "primary": PRIMARY, "backup": BACKUP}


@pytest.mark.req("7.1")
def test_returns_primary_and_backup_for_the_current_colombia_week():
    deps = make_deps(WEEK)

    result = resolve_oncall({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    assert result == {
        "week_start": "2026-11-09",
        "primary": {"display_name": "Ana Guardia", "teams_user_id": "ana-aad-id"},
        "backup": {"display_name": "Beto Respaldo", "teams_user_id": "beto-aad-id"},
        "warnings": [],
    }


@pytest.mark.req("7.1")
def test_audit_records_the_resolution_without_names_or_emails():
    deps = make_deps(WEEK)

    resolve_oncall({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    (row,) = event_rows(deps.events, "inc-1")
    assert row["event_type"] == "ONCALL_RESOLVED"
    assert row["actor"] == "ResolveOnCall"
    assert row["detail"] == {
        "week_start": "2026-11-09",
        "has_primary": True,
        "has_backup": True,
        "warnings": [],
    }
    assert "Ana" not in str(row) and "example.com" not in str(row)


@pytest.mark.req("7.4")
def test_missing_primary_answers_with_the_backup_and_records_a_configuration_warning():
    deps = make_deps({"week_start": "2026-11-09", "backup": BACKUP})

    result = resolve_oncall({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    assert result["primary"] is None
    assert result["backup"]["teams_user_id"] == "beto-aad-id"
    assert result["warnings"] == ["NO_PRIMARY_ONCALL"]
    (row,) = event_rows(deps.events, "inc-1")
    assert row["event_type"] == "CONFIG_WARNING"
    assert row["detail"]["warnings"] == ["NO_PRIMARY_ONCALL"]


@pytest.mark.req("7.4")
def test_week_without_schedule_still_answers_and_warns():
    deps = make_deps()

    result = resolve_oncall({"incident_id": "inc-1"}, deps, now=bogota(4, 0))

    assert result["primary"] is None and result["backup"] is None
    assert result["warnings"] == ["NO_ONCALL_SCHEDULE"]
    assert event_rows(deps.events, "inc-1")[0]["event_type"] == "CONFIG_WARNING"


@pytest.mark.req("7.1")
def test_sunday_night_in_colombia_still_belongs_to_the_previous_week():
    next_week = {"week_start": "2026-11-16", "primary": BACKUP, "backup": PRIMARY}
    deps = make_deps(WEEK, next_week)

    # 04:30 UTC Monday == 23:30 Sunday in Bogota
    from datetime import UTC, datetime

    result = resolve_oncall({}, deps, now=datetime(2026, 11, 16, 4, 30, tzinfo=UTC))

    assert result["week_start"] == "2026-11-09"
    assert result["primary"]["display_name"] == "Ana Guardia"


def test_without_incident_id_nothing_is_audited():
    deps = make_deps(WEEK)

    resolve_oncall({}, deps, now=bogota(4, 0))

    assert deps.events.rows == {}


def test_incident_id_can_come_nested_as_incident():
    deps = make_deps(WEEK)

    resolve_oncall({"incident": {"incident_id": "inc-7"}}, deps, now=bogota(4, 0))

    assert [r["incident_id"] for r in deps.events.rows.values()] == ["inc-7"]
