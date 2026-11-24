"""PlanReminder (task 6.5 · requirements 6.2, 8.1, 8.2).

Pure planning step of the escalation loop: it decides how long the flow waits
for the process to recover before the next reminder, and whether the deadline
has already passed. It writes nothing.

Event:  {"incident_id": "..."}
Output: {"already_resolved": bool,    # the recovery resolved it while we waited (D25): stop
         "deadline_passed": bool,
         "wait_seconds": int,           # min(reminder interval, time to the deadline), at least 1
         "minutes_left": int,           # negative once the deadline has passed
         "post_deadline_seconds": int}  # how long to keep waiting for a resolution after it (6.5)

Thresholds (SSM): reminder-interval-minutes, post-deadline-wait-hours.

Environment:
    INCIDENTS_TABLE
    GUARDIA_SSM_PREFIX (optional, default /guardia/config)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from guardia_common.config import get_threshold, load_thresholds
from guardia_common.logger import get_logger, log
from guardia_common.schedule import minutes_until, parse_timestamp

LOGGER = get_logger("guardia.plan_reminder")


class IncidentNotFoundError(LookupError):
    """The flow sent an incident_id that is not in the Incidents table."""


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table
    ssm: Any  # boto3 SSM client


def plan_reminder(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("PlanReminder requires incident_id")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)

    thresholds = load_thresholds(deps.ssm)
    interval_s = int(float(get_threshold(thresholds, "reminder-interval-minutes")) * 60)
    post_deadline_s = int(float(get_threshold(thresholds, "post-deadline-wait-hours")) * 3600)

    deadline = parse_timestamp(item["deadline_iso"])
    seconds_left = int((deadline - now).total_seconds())
    passed = seconds_left <= 0
    # Step Functions needs a timeout of at least 1 second.
    wait_seconds = max(1, min(interval_s, seconds_left)) if not passed else 1
    result = {
        "already_resolved": item.get("status") == "RESOLVED",
        "deadline_passed": passed,
        "wait_seconds": wait_seconds,
        "minutes_left": minutes_until(deadline, now),
        "post_deadline_seconds": post_deadline_s,
    }
    log(LOGGER, logging.INFO, "Reminder planned", incident_id=incident_id, **result)
    return result


@cache
def _aws_deps() -> Deps:
    return Deps(
        incidents=boto3.resource("dynamodb").Table(os.environ["INCIDENTS_TABLE"]),
        ssm=boto3.client("ssm"),
    )


def handler(event: dict, context: Any) -> dict:
    return plan_reminder(event, _aws_deps())
