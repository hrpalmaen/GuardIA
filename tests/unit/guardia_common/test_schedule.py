from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guardia_common.schedule import minutes_until, parse_timestamp, sla_deadline
from tests.unit.conftest import bogota


@pytest.mark.req("2.3")
@pytest.mark.parametrize("reference,expected", [
    (bogota(3, 30), bogota(6, 0)),
    (bogota(5, 59), bogota(6, 0)),
    (bogota(6, 0), bogota(6, 0, day=13)),
    (bogota(22, 0), bogota(6, 0, day=13)),
    (bogota(23, 30, day=31, month=12).replace(year=2026), datetime(2027, 1, 1, 6, 0,
                                                                   tzinfo=bogota(0).tzinfo)),
])  # fmt: skip
def test_sla_deadline_is_next_deadline_in_bogota(reference, expected):
    assert sla_deadline(reference, "06:00", "America/Bogota") == expected


@pytest.mark.req("2.3")
def test_reference_in_utc_is_converted_to_bogota():
    # 08:30 UTC = 03:30 Bogota
    reference = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    assert sla_deadline(reference, "06:00", "America/Bogota").isoformat() == (
        "2026-11-12T06:00:00-05:00"
    )


def test_minutes_until():
    assert minutes_until(bogota(6, 0), bogota(4, 0)) == 120
    assert minutes_until(bogota(6, 0), bogota(5, 30)) == 30
    assert minutes_until(bogota(6, 0), bogota(6, 10)) == -10


def test_parse_timestamp_formats():
    assert parse_timestamp(None) is None
    assert parse_timestamp(1762936200000) == datetime(2025, 11, 12, 8, 30, tzinfo=UTC)
    assert parse_timestamp("2026-11-12T04:00:00-05:00") == bogota(4, 0)
    assert parse_timestamp("2026-11-12T09:00:00Z") == bogota(4, 0)


# --------------------------------------------------------------------------- 3.3
from datetime import timedelta  # noqa: E402

from guardia_common.schedule import (  # noqa: E402
    Schedule,
    UnsupportedScheduleError,
    aws_cron_to_croniter,
    max_duration,
    median_duration_s,
)


def utc(hour: int, minute: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@pytest.mark.req("3.1")
@pytest.mark.parametrize("aws,expected", [
    ("30 * * * ? *", "30 * * * *"),
    ("0 5 ? * MON-FRI *", "0 5 * * MON-FRI"),
    ("0 5 ? * 2-6 *", "0 5 * * 1-5"),        # AWS counts Sunday as 1
    ("0 5 ? * 6L *", "0 5 * * L5"),          # last Friday
    ("0 5 ? * 2#1 *", "0 5 * * 1#1"),        # first Monday
    ("0 0/2 * * ? *", "0 0/2 * * *"),
])  # fmt: skip
def test_aws_cron_is_translated_to_croniter(aws, expected):
    assert aws_cron_to_croniter(aws) == expected


@pytest.mark.parametrize("expression", [
    "cron(0 5 * * ? 2027)", "cron(0 5 * *)", "cron(99 5 * * ? *)", "at(2026-11-16T05:00:00)",
])  # fmt: skip
def test_unsupported_schedules_are_rejected(expression):
    with pytest.raises(UnsupportedScheduleError):
        Schedule(expression).expected_runs(utc(0), utc(6))


@pytest.mark.req("3.1")
def test_expected_runs_of_an_hourly_rule_in_utc():
    runs = Schedule("cron(30 * * * ? *)").expected_runs(utc(5, 0), utc(8, 30))
    assert runs == [utc(5, 30), utc(6, 30), utc(7, 30), utc(8, 30)]  # window is (start, end]


@pytest.mark.req("3.1")
def test_expected_runs_cross_midnight_in_bogota():
    # 22:00 to 02:00 Bogota (03:00 to 07:00 UTC) of a nightly 23:00 and 01:00 schedule.
    schedule = Schedule("cron(0 23,1 * * ? *)", "America/Bogota")
    runs = schedule.expected_runs(bogota(22, 0, day=15), bogota(2, 0, day=16))
    assert runs == [bogota(23, 0, day=15), bogota(1, 0, day=16)]
    assert all(run.tzinfo == UTC for run in runs)


def test_rate_schedules_have_no_expected_times_but_have_a_period():
    schedule = Schedule("rate(2 hours)")
    assert schedule.expected_runs(utc(0), utc(6)) == []
    assert schedule.period_at(utc(3)) == timedelta(hours=2)


@pytest.mark.req("3.3")
@pytest.mark.parametrize("expression,moment,period", [
    ("cron(0 * * * ? *)", utc(5, 40), timedelta(hours=1)),
    ("cron(0 * * * ? *)", utc(5, 0), timedelta(hours=1)),       # exactly at a run
    ("cron(0 0/2 * * ? *)", utc(5, 40), timedelta(hours=2)),
    ("cron(0 8 ? * MON-FRI *)", utc(9, 0, day=20), timedelta(days=3)),  # Friday → Monday
])  # fmt: skip
def test_period_is_the_gap_that_contains_the_moment(expression, moment, period):
    assert Schedule(expression).period_at(moment) == period


@pytest.mark.req("3.2")
def test_median_needs_the_minimum_number_of_samples():
    assert median_duration_s([60, 70, 80, 90], min_samples=5) is None
    assert median_duration_s([60, 70, 80, 90, 300], min_samples=5) == 80


@pytest.mark.req("3.2")
def test_max_duration_is_twice_the_median():
    assert max_duration(median_s=80, multiplier=2, period=timedelta(hours=1)) == timedelta(
        seconds=160
    )


@pytest.mark.req("3.3")
def test_max_duration_without_history_is_half_the_period():
    assert max_duration(median_s=None, multiplier=2, period=timedelta(hours=1)) == timedelta(
        minutes=30
    )
    assert max_duration(median_s=None, multiplier=2, period=None) is None
