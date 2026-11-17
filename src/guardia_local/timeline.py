"""Scripted nights: real executions replayed at every scheduled time.

There is one real successful execution per organization; a demo night needs
one per scheduled run. `scheduled_replays` clones each source execution at
every time its schedule fires, with the exceptions that make a scenario: a
run that never starts, starts late, takes too long, or replays another real
execution (e.g. a real failure) instead of the success.

`status_changes` gives the EventBridge events those runs emit when they end.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timedelta

from guardia_common.schedule import Schedule

from .events import status_change_event
from .fixtures import ExecutionFixture, ExecutionFixtures

Run = tuple[str, datetime]  # (state machine ARN, scheduled time in UTC)
DEFAULT_START_DELAY = timedelta(seconds=4)  # what the real runs show


def scheduled_replays(
    fixtures: ExecutionFixtures,
    sources: Mapping[str, str],
    start: datetime,
    end: datetime,
    *,
    skip: Collection[Run] = (),
    delay: Mapping[Run, timedelta] | None = None,
    durations: Mapping[Run, timedelta] | None = None,
    replace: Mapping[Run, str] | None = None,
) -> list[ExecutionFixture]:
    """`sources` maps each state machine ARN to the fixture id it replays;
    `replace` maps a run to another fixture id of the same state machine."""
    delay, durations, replace = delay or {}, durations or {}, replace or {}
    runs = []
    for arn, fixture_id in sources.items():
        source = fixtures.by_id(fixture_id)
        schedule = Schedule(fixtures.state_machines[arn].schedule)
        for scheduled in schedule.expected_runs(start, end):
            key = (arn, scheduled)
            if key in skip:
                continue
            name = f"{arn.rsplit(':', 1)[-1]}-{scheduled:%Y%m%dT%H%M}"
            begin = scheduled + delay.get(key, DEFAULT_START_DELAY)
            replayed = fixtures.by_id(replace[key]) if key in replace else source
            if replayed.state_machine_arn != arn:
                raise ValueError(f"{replace[key]} is not an execution of {arn}")
            runs.append(replayed.replayed_as(name, begin, durations.get(key)))
    return runs


def status_changes(runs: list[ExecutionFixture]) -> list[tuple[datetime, dict]]:
    """(stop time, `Step Functions Execution Status Change` event) of every run
    that ends, in order. Runs still RUNNING at the end of the fixture emit nothing."""
    ended = [run for run in runs if run.stop_date is not None]
    return [
        (run.stop_date, status_change_event(run.describe_response()))
        for run in sorted(ended, key=lambda run: run.stop_date)
    ]
