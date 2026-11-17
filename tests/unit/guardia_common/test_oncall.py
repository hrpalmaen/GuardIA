"""OnCallSchedule lookup (task 6.1 · requirements 7.1, 7.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guardia_common.oncall import (
    WARNING_NO_BACKUP,
    WARNING_NO_PRIMARY,
    WARNING_NO_SCHEDULE,
    resolve_on_call,
    week_start,
)
from tests.unit.fakes import BACKUP, PRIMARY, FakeTable, bogota

WEEK_A = "2026-11-09"  # Monday
WEEK_B = "2026-11-16"  # next Monday


def schedule(**rows) -> FakeTable:
    table = FakeTable("week_start")
    for week, row in rows.items():
        table.put_row({"week_start": week.removeprefix("w_").replace("_", "-"), **row})
    return table


@pytest.mark.req("7.1")
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (bogota(0, 0, day=9), WEEK_A),  # Monday 00:00 starts the week
        (bogota(12, 0, day=12), WEEK_A),  # Thursday
        (bogota(23, 59, day=15), WEEK_A),  # Sunday night is still week A
        (bogota(0, 1, day=16), WEEK_B),  # Monday 00:01 is week B
    ],
)
def test_week_starts_on_monday_in_colombia_time(moment, expected):
    assert week_start(moment) == expected


@pytest.mark.req("7.1")
def test_week_uses_colombia_time_not_utc():
    # Monday 04:30 UTC is still Sunday 23:30 in Bogota -> previous week.
    assert week_start(datetime(2026, 11, 16, 4, 30, tzinfo=UTC)) == WEEK_A
    assert week_start(datetime(2026, 11, 16, 5, 30, tzinfo=UTC)) == WEEK_B


@pytest.mark.req("7.1")
def test_normal_week_returns_primary_and_backup():
    table = schedule(w_2026_11_09={"primary": PRIMARY, "backup": BACKUP})

    result = resolve_on_call(table, bogota(4, 0, day=12))

    assert result.week_start == WEEK_A
    assert result.primary.display_name == "Ana Guardia"
    assert result.backup.teams_user_id == "beto-aad-id"
    assert result.warnings == []


@pytest.mark.req("7.1")
def test_rotation_changes_on_monday():
    other = {"display_name": "Carla Nueva", "teams_user_id": "carla-aad-id"}
    table = schedule(
        w_2026_11_09={"primary": PRIMARY, "backup": BACKUP},
        w_2026_11_16={"primary": other, "backup": PRIMARY},
    )

    sunday = resolve_on_call(table, bogota(23, 30, day=15))
    monday = resolve_on_call(table, bogota(0, 30, day=16))

    assert sunday.primary.display_name == "Ana Guardia"
    assert monday.primary.display_name == "Carla Nueva"
    assert monday.backup.display_name == "Ana Guardia"


@pytest.mark.req("7.4")
def test_missing_primary_returns_only_the_backup_with_a_warning():
    table = schedule(w_2026_11_09={"backup": BACKUP})

    result = resolve_on_call(table, bogota(4, 0, day=12))

    assert result.primary is None
    assert result.backup.display_name == "Beto Respaldo"
    assert result.warnings == [WARNING_NO_PRIMARY]


@pytest.mark.req("7.4")
def test_week_without_a_row_returns_nobody_with_a_warning():
    result = resolve_on_call(schedule(), bogota(4, 0, day=12))

    assert result.primary is None and result.backup is None
    assert result.warnings == [WARNING_NO_SCHEDULE]


@pytest.mark.req("7.4")
def test_person_without_a_teams_identifier_cannot_be_mentioned():
    table = schedule(
        w_2026_11_09={
            "primary": {"display_name": "Sin Teams", "email": "x@example.com"},
            "backup": BACKUP,
        }
    )

    result = resolve_on_call(table, bogota(4, 0, day=12))

    assert result.primary is None
    assert result.warnings == [WARNING_NO_PRIMARY]


def test_missing_backup_is_reported():
    table = schedule(w_2026_11_09={"primary": PRIMARY})

    result = resolve_on_call(table, bogota(4, 0, day=12))

    assert result.primary is not None and result.backup is None
    assert result.warnings == [WARNING_NO_BACKUP]


def test_output_never_carries_emails():
    table = schedule(w_2026_11_09={"primary": PRIMARY, "backup": BACKUP})

    output = resolve_on_call(table, bogota(4, 0, day=12)).as_dict()

    assert "email" not in str(output)
    assert output["primary"] == {"display_name": "Ana Guardia", "teams_user_id": "ana-aad-id"}
