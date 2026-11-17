"""Loader for real Step Functions executions saved as fixtures.

Layout (written by `scripts/sanitize_executions.py`):

    tests/fixtures/executions/
        manifest.json               state machines (guardia:* tags, schedule) and executions
        <execution-id>/describe.json    DescribeExecution response (without ResponseMetadata)
        <execution-id>/history.json     GetExecutionHistory response: {"events": [...]}

Timestamps are stored as ISO 8601 strings and returned as timezone-aware
datetimes, which is what boto3 returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "executions"
_DESCRIBE_TIMESTAMPS = ("startDate", "stopDate", "redriveDate")


def _to_datetime(value: Any) -> Any:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class StateMachineFixture:
    arn: str
    name: str
    tags: dict[str, str]
    schedule: str | None = None  # EventBridge rule expression that launches it


@dataclass(frozen=True)
class ExecutionFixture:
    fixture_id: str
    execution_arn: str
    state_machine_arn: str
    status: str
    describe: dict = field(repr=False)
    events: list[dict] = field(repr=False)

    def describe_response(self) -> dict:
        data = json.loads(json.dumps(self.describe))
        for key in _DESCRIBE_TIMESTAMPS:
            if key in data:
                data[key] = _to_datetime(data[key])
        return data

    def history_events(self) -> list[dict]:
        events = json.loads(json.dumps(self.events))
        for event in events:
            event["timestamp"] = _to_datetime(event["timestamp"])
        return events

    @property
    def start_date(self) -> datetime:
        return datetime.fromisoformat(self.describe["startDate"])

    @property
    def stop_date(self) -> datetime | None:
        stop = self.describe.get("stopDate")
        return datetime.fromisoformat(stop) if stop else None

    def replayed_as(
        self, name: str, start: datetime, duration: timedelta | None = None
    ) -> ExecutionFixture:
        """Another run of the same process: new name and ARN, starting at
        `start`. With `duration`, every event is stretched or compressed
        proportionally (e.g. to make a run stuck); the event order is kept."""
        origin = self.start_date
        span = (self.stop_date or origin) - origin
        factor = duration / span if duration is not None and span else 1.0

        def move(value: str) -> str:
            return (start + (datetime.fromisoformat(value) - origin) * factor).isoformat()

        arn = self.execution_arn.rsplit(":", 1)[0] + f":{name}"
        describe = {**self.describe, "executionArn": arn, "name": name}
        for key in _DESCRIBE_TIMESTAMPS:
            if describe.get(key):
                describe[key] = move(describe[key])
        events = [{**e, "timestamp": move(e["timestamp"])} for e in self.events]
        return replace(self, fixture_id=name, execution_arn=arn, describe=describe, events=events)

    def shifted(self, delta: timedelta) -> ExecutionFixture:
        """Same execution moved in time: every timestamp moves by `delta`, so
        durations and event order are preserved."""

        def move(value: str) -> str:
            return (datetime.fromisoformat(value) + delta).isoformat()

        describe = dict(self.describe)
        for key in _DESCRIBE_TIMESTAMPS:
            if describe.get(key):
                describe[key] = move(describe[key])
        events = [{**e, "timestamp": move(e["timestamp"])} for e in self.events]
        return replace(self, describe=describe, events=events)


class ExecutionFixtures:
    def __init__(
        self, state_machines: list[StateMachineFixture], executions: list[ExecutionFixture]
    ):
        self.state_machines = {sm.arn: sm for sm in state_machines}
        self.executions = {ex.execution_arn: ex for ex in executions}

    @classmethod
    def load(cls, directory: Path = DEFAULT_DIR) -> ExecutionFixtures:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        machines = [
            StateMachineFixture(
                arn=m["arn"], name=m["name"], tags=m["tags"], schedule=m.get("schedule")
            )
            for m in manifest["state_machines"]
        ]
        executions = []
        for entry in manifest["executions"]:
            folder = directory / entry["id"]
            executions.append(
                ExecutionFixture(
                    fixture_id=entry["id"],
                    execution_arn=entry["execution_arn"],
                    state_machine_arn=entry["state_machine_arn"],
                    status=entry["status"],
                    describe=json.loads((folder / "describe.json").read_text(encoding="utf-8")),
                    events=json.loads((folder / "history.json").read_text(encoding="utf-8"))[
                        "events"
                    ],
                )
            )
        return cls(machines, executions)

    def with_start(self, fixture_id: str, start: datetime) -> ExecutionFixtures:
        """Copy where one execution starts at `start` (for a demo night or a test clock)."""
        execution = self.by_id(fixture_id)
        delta = start - datetime.fromisoformat(execution.describe["startDate"])
        executions = {**self.executions, execution.execution_arn: execution.shifted(delta)}
        return ExecutionFixtures(list(self.state_machines.values()), list(executions.values()))

    def with_executions(self, executions: list[ExecutionFixture]) -> ExecutionFixtures:
        """Copy with these executions added (or replaced, by ARN)."""
        merged = {**self.executions, **{ex.execution_arn: ex for ex in executions}}
        return ExecutionFixtures(list(self.state_machines.values()), list(merged.values()))

    def only(self, executions: list[ExecutionFixture]) -> ExecutionFixtures:
        """Same state machines, with exactly these executions (for a scripted timeline)."""
        return ExecutionFixtures(list(self.state_machines.values()), executions)

    def by_id(self, fixture_id: str) -> ExecutionFixture:
        for execution in self.executions.values():
            if execution.fixture_id == fixture_id:
                return execution
        raise KeyError(fixture_id)
