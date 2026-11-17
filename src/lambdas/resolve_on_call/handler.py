"""ResolveOnCall (task 6.1 · requirements 7.1, 7.4).

Answers "who is on call today?" for the escalation branch of
GuardIA-Incident-Flow. Reads `OnCallSchedule` for the current week in
Colombia time. If the on-call engineer is missing it still answers, with only
the backup, and records a configuration warning (7.4).

Event:  {"incident_id": "..."}  (optional; without it nothing is audited)
Output: {"week_start", "primary", "backup", "warnings"}; each person is
        {"display_name", "teams_user_id"} or null. Emails never leave the table.

Environment:
    ONCALL_SCHEDULE_TABLE, INCIDENT_EVENTS_TABLE
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

from guardia_common.audit import AuditRepository
from guardia_common.config import get_threshold, load_thresholds
from guardia_common.logger import get_logger, log
from guardia_common.oncall import resolve_on_call as lookup_on_call

LOGGER = get_logger("guardia.resolve_on_call")
ACTOR = "ResolveOnCall"


@dataclass
class Deps:
    schedule: Any  # boto3 DynamoDB Table: OnCallSchedule
    events: Any  # boto3 DynamoDB Table: IncidentEvents
    ssm: Any  # boto3 SSM client


def resolve_oncall(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    tz_name = get_threshold(load_thresholds(deps.ssm), "timezone")
    resolution = lookup_on_call(deps.schedule, now, tz_name)

    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    flags = {
        "week_start": resolution.week_start,
        "has_primary": resolution.primary is not None,
        "has_backup": resolution.backup is not None,
        "warnings": resolution.warnings,
    }
    if incident_id:
        audit = AuditRepository(deps.events, actor=ACTOR, tz_name=tz_name)
        audit.append(
            incident_id, "CONFIG_WARNING" if resolution.warnings else "ONCALL_RESOLVED", flags, now
        )

    log(
        LOGGER,
        logging.WARNING if resolution.warnings else logging.INFO,
        "On-call resolved",
        incident_id=incident_id,
        **flags,
    )
    return resolution.as_dict()


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        schedule=dynamodb.Table(os.environ["ONCALL_SCHEDULE_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        ssm=boto3.client("ssm"),
    )


def handler(event: dict, context: Any) -> dict:
    return resolve_oncall(event, _aws_deps())
