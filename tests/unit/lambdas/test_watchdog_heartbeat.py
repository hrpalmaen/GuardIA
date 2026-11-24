"""MissedRunWatchdog emits the heartbeat only after a cycle that worked (8.2, D34)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lambdas.missed_run_watchdog.handler import Deps, run_watchdog
from tests.unit.fakes import fresh_ssm

NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


class EmptyRegistry:
    def scan(self, **kwargs):
        return {"Items": []}


class BrokenRegistry:
    def scan(self, **kwargs):
        raise RuntimeError("AccessDeniedException")


class Unused:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected call: {name}")


class Bus:
    def put_events(self, Entries):  # noqa: N803 (boto3 signature)
        return {"FailedEntryCount": 0, "Entries": [{} for _ in Entries]}


def _deps(registry, beats: list) -> Deps:
    return Deps(
        registry=registry, incidents=Unused(), sfn=Unused(), bus=Bus(), ssm=fresh_ssm(),
        heartbeat=lambda now, **kwargs: beats.append((now, kwargs)),
    )  # fmt: skip


@pytest.mark.req("13.2")
def test_a_finished_cycle_emits_one_heartbeat():
    beats: list = []
    run_watchdog(_deps(EmptyRegistry(), beats), now=NOW)
    assert beats == [(NOW, {"properties": {"processes": 0, "published": 0}})]


@pytest.mark.req("13.2")
def test_a_failed_cycle_emits_no_heartbeat():
    beats: list = []
    with pytest.raises(RuntimeError):
        run_watchdog(_deps(BrokenRegistry(), beats), now=NOW)
    assert beats == []


def test_the_default_heartbeat_writes_emf_to_stdout(capsys):
    deps = Deps(registry=EmptyRegistry(), incidents=Unused(), sfn=Unused(), bus=Bus(),
                ssm=fresh_ssm())  # fmt: skip
    run_watchdog(deps, now=NOW)
    out = capsys.readouterr().out
    assert '"Heartbeat": 1' in out and '"Namespace": "GuardIA"' in out
