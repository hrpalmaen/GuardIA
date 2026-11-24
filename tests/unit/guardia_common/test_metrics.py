"""EMF records for the GuardIA heartbeat (tasks 8.1 and 8.2)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from guardia_common.metrics import (
    HEARTBEAT_DIMENSIONS,
    HEARTBEAT_METRIC,
    NAMESPACE,
    emf_record,
    emit,
    emit_heartbeat,
)

NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


@pytest.mark.req("13.2")
def test_heartbeat_is_a_valid_emf_document():
    record = emit_heartbeat(NOW, writer=lambda _record: None)
    directive = record["_aws"]["CloudWatchMetrics"][0]

    assert record["_aws"]["Timestamp"] == int(NOW.timestamp() * 1000)
    assert directive["Namespace"] == NAMESPACE == "GuardIA"
    assert directive["Metrics"] == [{"Name": HEARTBEAT_METRIC, "Unit": "Count"}]
    # Every dimension in the directive must be a top-level member of the document.
    assert directive["Dimensions"] == [list(HEARTBEAT_DIMENSIONS)]
    for name, value in HEARTBEAT_DIMENSIONS.items():
        assert record[name] == value
    assert record[HEARTBEAT_METRIC] == 1


def test_properties_are_kept_but_are_not_metrics():
    record = emf_record("X", 2, dimensions={"Component": "C"}, now=NOW, properties={"n": 3})
    assert record["n"] == 3
    assert [m["Name"] for m in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]] == ["X"]


def test_emit_writes_one_json_line():
    stream = io.StringIO()
    emit(emf_record("X", 1, dimensions={"Component": "C"}, now=NOW), stream)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["X"] == 1
