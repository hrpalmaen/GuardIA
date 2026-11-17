"""Missed and stuck execution rules (requirement 3). Pure functions.

`MissedRunWatchdog` lists the executions and publishes the detections; this
module decides which scheduled runs were missed and which executions are stuck.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import IncidentKind, ProcessRecord
from .schedule import Schedule, UnsupportedScheduleError, max_duration


@dataclass(frozen=True)
class WatchdogSettings:
    tolerance: timedelta  # 3.1: 10 minutes after the scheduled time
    lookback: timedelta  # how far back scheduled runs are checked (covers skipped cycles)
    median_multiplier: float  # 3.2: twice the median
    period_fraction: float  # 3.3: 50% of the periodicity
    # EventBridge can start a run a few seconds early; a run started up to this
    # long before its scheduled time still counts for it.
    early_slack: timedelta = timedelta(minutes=1)

    @classmethod
    def from_thresholds(cls, thresholds: dict) -> WatchdogSettings:
        return cls(
            tolerance=timedelta(minutes=int(thresholds["missed-run-tolerance-minutes"])),
            lookback=timedelta(minutes=int(thresholds["watchdog-lookback-minutes"])),
            median_multiplier=float(thresholds["stuck-median-multiplier"]),
            period_fraction=float(thresholds["stuck-period-fraction"]),
        )


def _schedules(process: ProcessRecord) -> list[Schedule]:
    return [Schedule(ref.expression, ref.timezone) for ref in process.schedules]


def check_window(process: ProcessRecord, now: datetime, settings: WatchdogSettings):
    """Scheduled times whose tolerance has expired and that are still in the
    lookback: (now - tolerance - lookback, now - tolerance]. Runs scheduled
    before the process was first discovered are not GuardIA's to report."""
    end = now - settings.tolerance
    start = end - settings.lookback
    if process.first_seen_iso:
        start = max(start, datetime.fromisoformat(process.first_seen_iso))
    return start, end


def expected_runs(process: ProcessRecord, now: datetime, settings: WatchdogSettings):
    start, end = check_window(process, now, settings)
    runs: set[datetime] = set()
    for schedule in _schedules(process):
        try:
            runs.update(schedule.expected_runs(start, end))
        except UnsupportedScheduleError:
            continue  # reported by DiscoverProcesses as a configuration warning
    return sorted(runs)


def missed_runs(
    process: ProcessRecord,
    start_dates: list[datetime],
    now: datetime,
    settings: WatchdogSettings,
) -> list[datetime]:
    """3.1: scheduled times with no execution started within the tolerance.

    A run that starts after the tolerance (delayed) does not cover its scheduled
    time: the incident is opened, and the later success resolves it (8.x)."""
    missed = []
    for expected in expected_runs(process, now, settings):
        earliest = expected - settings.early_slack
        latest = expected + settings.tolerance
        if not any(earliest <= started <= latest for started in start_dates):
            missed.append(expected)
    return missed


def period_for(process: ProcessRecord, moment: datetime) -> timedelta | None:
    """Shortest periodicity among the process schedules at `moment`."""
    periods = []
    for schedule in _schedules(process):
        try:
            periods.append(schedule.period_at(moment))
        except UnsupportedScheduleError:
            continue
    return min(periods) if periods else None


def stuck_limit(
    process: ProcessRecord, started: datetime, settings: WatchdogSettings
) -> timedelta | None:
    """3.2 / 3.3: maximum time in RUNNING for an execution started at `started`."""
    return max_duration(
        median_s=process.median_duration_s,
        multiplier=settings.median_multiplier,
        period=period_for(process, started),
        period_fraction=settings.period_fraction,
    )


def is_stuck(
    process: ProcessRecord, started: datetime, now: datetime, settings: WatchdogSettings
) -> bool:
    limit = stuck_limit(process, started, settings)
    return limit is not None and now - started > limit


def missed_run_detection(process: ProcessRecord, expected: datetime) -> dict:
    """Detection in the shape OpenIncident accepts (guardia_common.incidents)."""
    return {
        "kind": IncidentKind.MISSED_RUN.value,
        "resource_arn": process.resource_arn,
        "expected_at": expected.isoformat(),
    }


def stuck_run_detection(process: ProcessRecord, execution_arn: str, started: datetime) -> dict:
    return {
        "kind": IncidentKind.STUCK_RUN.value,
        "resource_arn": process.resource_arn,
        "execution_arn": execution_arn,
        "started_at": started.isoformat(),
    }
