"""Scripted nights: replacing a scheduled run with a real failure, and the
status-change events the runs emit when they end."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guardia_common.incidents import STATUS_CHANGE_PATTERN
from guardia_local.bus import matches
from guardia_local.timeline import scheduled_replays, status_changes

SM = "arn:aws:states:us-east-1:123456789012:stateMachine:BI-{}-GCRAW-Orchestrator-ConversationsDetail"  # noqa: E501
EMT12, EMTSAS = SM.format("EMT12"), SM.format("EMTSAS")
SOURCES = {EMT12: "emt12-20261113-1630-succeeded", EMTSAS: "emtsas-20261113-1600-succeeded"}
START, END = datetime(2026, 11, 16, 4, 0, tzinfo=UTC), datetime(2026, 11, 16, 7, 0, tzinfo=UTC)


def test_a_scheduled_run_can_replay_a_real_failure(real_executions):
    runs = scheduled_replays(real_executions, SOURCES, START, END,
                             replace={(EMTSAS, datetime(2026, 11, 16, 5, 0, tzinfo=UTC)):
                                      "emtsas-20260918-0500-failed"})  # fmt: skip
    by_name = {run.fixture_id: run for run in runs}
    failed = by_name["BI-EMTSAS-GCRAW-Orchestrator-ConversationsDetail-20261116T0500"]

    assert failed.describe["status"] == "FAILED"
    assert failed.start_date == datetime(2026, 11, 16, 5, 0, 4, tzinfo=UTC)
    assert failed.events[-1]["type"] == "ExecutionFailed"
    assert sum(run.describe["status"] == "FAILED" for run in runs) == 1


def test_replacing_with_another_process_execution_is_rejected(real_executions):
    with pytest.raises(ValueError, match="not an execution of"):
        scheduled_replays(real_executions, SOURCES, START, END,
                          replace={(EMT12, datetime(2026, 11, 16, 4, 30, tzinfo=UTC)):
                                   "emtsas-20260918-0500-failed"})  # fmt: skip


def test_status_changes_are_emitted_at_each_stop_and_only_failures_match_the_rule(
    real_executions,
):
    runs = scheduled_replays(real_executions, SOURCES, START, END,
                             replace={(EMTSAS, datetime(2026, 11, 16, 5, 0, tzinfo=UTC)):
                                      "emtsas-20260918-0500-failed"})  # fmt: skip
    changes = status_changes(runs)

    assert len(changes) == len(runs)
    assert [stop for stop, _ in changes] == sorted(stop for stop, _ in changes)
    for stop, event in changes:
        assert event["detail"]["stopDate"] == int(stop.timestamp() * 1000)
    matching = [e for _, e in changes if matches(STATUS_CHANGE_PATTERN, e)]
    assert [e["detail"]["status"] for e in matching] == ["FAILED"]
