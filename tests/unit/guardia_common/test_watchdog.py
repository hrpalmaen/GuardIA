from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from guardia_common.models import ProcessRecord, ScheduleRef
from guardia_common.watchdog import (
    WatchdogSettings,
    is_stuck,
    missed_runs,
    stuck_limit,
)

SETTINGS = WatchdogSettings(
    tolerance=timedelta(minutes=10),
    lookback=timedelta(minutes=60),
    median_multiplier=2,
    period_fraction=0.5,
)


def utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, second, tzinfo=UTC)


def process(expression="cron(30 * * * ? *)", **fields) -> ProcessRecord:
    return ProcessRecord(
        resource_arn="arn:aws:states:us-east-1:123456789012:stateMachine:p",
        process_type="conversations",
        organization="EMT12",
        remediation="auto",
        schedules=[ScheduleRef(expression=expression, source="events-rule", name="r")],
        **fields,
    )


@pytest.mark.req("3.1")
def test_on_time_run_is_not_missed():
    assert missed_runs(process(), [utc(5, 30, 4)], utc(5, 45), SETTINGS) == []


@pytest.mark.req("3.1")
def test_run_inside_the_tolerance_is_not_missed():
    assert missed_runs(process(), [utc(5, 39)], utc(5, 45), SETTINGS) == []


@pytest.mark.req("3.1")
def test_run_that_never_started_is_missed_once_the_tolerance_expires():
    previous_ran = [utc(4, 30)]
    assert missed_runs(process(), previous_ran, utc(5, 39), SETTINGS) == []  # not expired yet
    assert missed_runs(process(), previous_ran, utc(5, 40), SETTINGS) == [utc(5, 30)]


@pytest.mark.req("3.1")
def test_run_delayed_beyond_the_tolerance_is_missed():
    assert missed_runs(process(), [utc(5, 42)], utc(5, 45), SETTINGS) == [utc(5, 30)]


def test_a_run_only_covers_its_own_scheduled_time():
    # The 04:30 run does not cover 05:30.
    assert missed_runs(process(), [utc(4, 30)], utc(5, 45), SETTINGS) == [utc(5, 30)]


def test_only_runs_inside_the_lookback_are_checked():
    # At 07:45 the window is (06:35, 07:35]: only 07:30 is checked.
    assert missed_runs(process(), [], utc(7, 45), SETTINGS) == [utc(7, 30)]


def test_runs_before_the_process_was_discovered_are_ignored():
    new = process(first_seen_iso=utc(5, 35).isoformat())
    assert missed_runs(new, [], utc(5, 45), SETTINGS) == []
    assert missed_runs(new, [], utc(6, 45), SETTINGS) == [utc(6, 30)]


def test_rate_schedule_produces_no_missed_runs():
    assert missed_runs(process("rate(1 hour)"), [], utc(5, 45), SETTINGS) == []


@pytest.mark.req("3.2")
def test_stuck_with_history_uses_twice_the_median():
    with_history = process(median_duration_s=150, duration_samples=14)
    assert stuck_limit(with_history, utc(5, 30), SETTINGS) == timedelta(minutes=5)
    assert not is_stuck(with_history, utc(5, 30), utc(5, 35), SETTINGS)
    assert is_stuck(with_history, utc(5, 30), utc(5, 35, 1), SETTINGS)


@pytest.mark.req("3.3")
def test_stuck_without_history_uses_half_the_period():
    hourly = process(duration_samples=1)
    assert stuck_limit(hourly, utc(5, 30), SETTINGS) == timedelta(minutes=30)
    assert not is_stuck(hourly, utc(5, 30), utc(6, 0), SETTINGS)
    assert is_stuck(hourly, utc(5, 30), utc(6, 0, 1), SETTINGS)


def test_without_schedule_or_history_nothing_is_stuck():
    unknown = ProcessRecord(resource_arn="arn", process_type="x", organization="y")
    assert not is_stuck(unknown, utc(0), utc(12), SETTINGS)
