"""CloudWatch metrics in Embedded Metric Format (EMF), task 8.1 and 8.2.

A Lambda writes one JSON line to stdout and CloudWatch Logs turns it into a
metric. No `cloudwatch:PutMetricData` permission and no extra latency.

The line is written straight to stdout, not through `logging`: the Lambda
JSON log format wraps records of the root logger, and a wrapped line is no
longer EMF. GuardIA's own logger (`guardia_common.logger`) also writes to
stdout directly.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TextIO

NAMESPACE = "GuardIA"
HEARTBEAT_METRIC = "Heartbeat"
# The heartbeat comes from MissedRunWatchdog (decision D28): a finished cycle
# proves its schedule, the Lambda, the tables, and the permissions work.
HEARTBEAT_DIMENSIONS = {"Component": "MissedRunWatchdog"}


def emf_record(
    metric: str,
    value: float,
    *,
    dimensions: Mapping[str, str],
    unit: str = "Count",
    now: datetime | None = None,
    properties: Mapping[str, object] | None = None,
) -> dict:
    """One EMF document. `properties` are searchable in Logs but are not metrics."""
    now = now or datetime.now(UTC)
    return {
        "_aws": {
            "Timestamp": int(now.timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": metric, "Unit": unit}],
                }
            ],
        },
        **dimensions,
        **(properties or {}),
        metric: value,
    }


def emit(record: dict, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write(json.dumps(record, default=str) + "\n")
    out.flush()


def emit_heartbeat(
    now: datetime | None = None,
    *,
    properties: Mapping[str, object] | None = None,
    writer: Callable[[dict], None] = emit,
) -> dict:
    """`GuardIA/Heartbeat = 1` with `Component=MissedRunWatchdog`."""
    record = emf_record(
        HEARTBEAT_METRIC, 1, dimensions=HEARTBEAT_DIMENSIONS, now=now, properties=properties
    )
    writer(record)
    return record
