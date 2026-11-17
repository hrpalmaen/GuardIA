"""PollExecution (task 5.3 · requirement 5.4).

Queries the status of the recovered execution so the flow can loop
(Wait -> PollExecution -> Choice on `done`). It only reads: it does not mark the
incident resolved; the flow does that when `succeeded` is true.

Input:  {"incident_id": "...", "recovery_execution_arn": "..."}
Output: {"status", "done", "succeeded", ...}

Environment:
    INCIDENT_EVENTS_TABLE
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from guardia_common.audit import AuditRepository
from guardia_common.logger import get_logger, log
from guardia_common.schedule import parse_timestamp

LOGGER = get_logger("guardia.poll_execution")
ACTOR = "PollExecution"
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}


@dataclass
class Deps:
    events: Any
    sfn: Any


def poll_execution(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    arn = event.get("recovery_execution_arn")
    incident_id = event.get("incident_id")
    if not arn or not incident_id:
        raise ValueError("PollExecution requires incident_id and recovery_execution_arn")

    info = deps.sfn.describe_execution(executionArn=arn)
    status = info["status"]
    done = status in FINAL_STATUSES
    stopped = parse_timestamp(info.get("stopDate"))

    result = {
        "incident_id": incident_id,
        "recovery_execution_arn": arn,
        "status": status,
        "done": done,
        "succeeded": status == "SUCCEEDED",
        "stopped_at": stopped.isoformat() if stopped else None,
    }
    if done and status != "SUCCEEDED":
        # Error name only: the cause can carry data from the monitored process.
        result["error"] = info.get("error")

    if done:
        AuditRepository(deps.events, actor=ACTOR).append(
            incident_id,
            "RECOVERY_FINISHED",
            {k: result[k] for k in ("recovery_execution_arn", "status", "stopped_at")},
            now=now,
        )
    log(
        LOGGER,
        logging.INFO,
        "Recovery execution polled",
        incident_id=incident_id,
        status=status,
        done=done,
    )
    return result


@cache
def _aws_deps() -> Deps:
    return Deps(
        events=boto3.resource("dynamodb").Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        sfn=boto3.client("stepfunctions"),
    )


def handler(event: dict, context: Any) -> dict:
    return poll_execution(event, _aws_deps())
