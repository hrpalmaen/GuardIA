from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ParamValidationError

from guardia_local.bus import LocalEventBus, matches
from guardia_local.clock import LocalScheduler, VirtualClock
from guardia_local.stepfunctions import ReplayStepFunctionsClient

T0 = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("pattern,event,expected", [
    ({"source": ["a"]}, {"source": "a"}, True),
    ({"source": ["a"]}, {"source": "b"}, False),
    ({"detail": {"status": ["FAILED", "ABORTED"]}}, {"detail": {"status": "ABORTED"}}, True),
    ({"detail": {"status": ["FAILED"]}}, {"detail": {"status": "SUCCEEDED"}}, False),
    ({"detail": {"error": [{"exists": False}]}}, {"detail": {}}, True),
    ({"detail": {"error": [{"exists": True}]}}, {"detail": {}}, False),
])  # fmt: skip
def test_event_patterns(pattern, event, expected):
    assert matches(pattern, event) is expected


def test_put_events_builds_the_envelope_and_delivers_to_matching_rules():
    bus, received = LocalEventBus(clock=lambda: T0), []
    bus.put_rule("r", {"source": ["guardia.watchdog"]}, received.append)
    bus.put_rule("other", {"source": ["aws.states"]}, lambda e: pytest.fail("wrong rule"))
    out = bus.put_events(Entries=[{"Source": "guardia.watchdog", "DetailType": "D",
                                   "Detail": json.dumps({"kind": "MISSED_RUN"})}])  # fmt: skip

    assert out["FailedEntryCount"] == 0
    [event] = received
    assert event["detail"] == {"kind": "MISSED_RUN"} and event["time"] == "2026-11-16T05:00:00Z"


def test_put_events_validates_like_the_real_client():
    with pytest.raises(ParamValidationError):
        LocalEventBus().put_events(Entries=[{"Source": "x", "Detail": {"not": "a string"}}])


def test_failing_target_is_recorded_like_a_dlq():
    bus = LocalEventBus(clock=lambda: T0)
    bus.put_rule("r", {"source": ["s"]}, lambda e: 1 / 0)
    bus.put_events(Entries=[{"Source": "s", "DetailType": "D", "Detail": "{}"}])
    assert bus.deliveries[0].error.startswith("ZeroDivisionError")


def test_scheduler_runs_jobs_in_time_order_on_the_virtual_clock():
    clock = VirtualClock(T0)
    scheduler, seen = LocalScheduler(clock), []
    scheduler.every(timedelta(minutes=15), "discover", lambda t: seen.append(("d", t, clock())))
    scheduler.every(timedelta(minutes=5), "watchdog", lambda t: seen.append(("w", t, clock())),
                    first=T0 + timedelta(minutes=5))  # fmt: skip
    scheduler.at(T0 + timedelta(minutes=7), "once", lambda t: seen.append(("o", t, clock())))
    scheduler.run_until(T0 + timedelta(minutes=15))

    assert [(k, int((t - T0).total_seconds() // 60)) for k, t, _ in seen] == [
        ("d", 0), ("w", 5), ("o", 7), ("w", 10), ("d", 15), ("w", 15),
    ]  # fmt: skip
    assert all(t == now for _, t, now in seen)  # handlers see the job time
    assert clock() == T0 + timedelta(minutes=15)


def test_clock_cannot_go_back():
    with pytest.raises(ValueError):
        VirtualClock(T0).set(T0 - timedelta(seconds=1))


# --------------------------------------------------------------------------- replay over time
@pytest.fixture
def emt12_run(real_executions):
    success = real_executions.by_id("emt12-20261113-1630-succeeded")
    run = success.replayed_as("night-0830", T0, duration=timedelta(minutes=10))
    return real_executions.only([run]), run


def test_replayed_run_keeps_its_events_in_order_and_takes_the_new_duration(emt12_run):
    _, run = emt12_run
    stamps = [e["timestamp"] for e in run.history_events()]
    assert stamps == sorted(stamps)
    assert run.stop_date - run.start_date == timedelta(minutes=10)
    assert run.execution_arn.endswith(":night-0830") and run.describe["name"] == "night-0830"


def test_execution_is_invisible_before_it_starts_and_running_until_it_stops(emt12_run):
    fixtures, run = emt12_run
    clock = VirtualClock(T0 - timedelta(minutes=1))
    client = ReplayStepFunctionsClient(fixtures, clock=clock)
    arn = run.state_machine_arn

    assert client.list_executions(stateMachineArn=arn)["executions"] == []

    clock.set(T0 + timedelta(minutes=5))
    described = client.describe_execution(executionArn=run.execution_arn)
    assert described["status"] == "RUNNING" and "stopDate" not in described
    history = client.get_execution_history(executionArn=run.execution_arn)["events"]
    assert 0 < len(history) < len(run.events)
    assert all(e["timestamp"] <= clock() for e in history)
    running = client.list_executions(stateMachineArn=arn, statusFilter="RUNNING")["executions"]
    assert [e["name"] for e in running] == ["night-0830"]

    clock.set(T0 + timedelta(minutes=11))
    assert client.describe_execution(executionArn=run.execution_arn)["status"] == "SUCCEEDED"
    assert client.list_executions(stateMachineArn=arn, statusFilter="RUNNING")["executions"] == []


def test_list_executions_is_newest_first_and_paginated(real_executions):
    success = real_executions.by_id("emt12-20261113-1630-succeeded")
    runs = [success.replayed_as(f"r{i}", T0 + timedelta(hours=i)) for i in range(5)]
    client = ReplayStepFunctionsClient(real_executions.only(runs))

    first = client.list_executions(stateMachineArn=success.state_machine_arn, maxResults=3)
    rest = client.list_executions(stateMachineArn=success.state_machine_arn, maxResults=3,
                                  nextToken=first["nextToken"])  # fmt: skip
    names = [e["name"] for e in first["executions"] + rest["executions"]]
    assert names == ["r4", "r3", "r2", "r1", "r0"] and "nextToken" not in rest
