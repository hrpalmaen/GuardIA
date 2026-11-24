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
    GUARDIA_ONCALL_EMAIL (D19, optional) on-call engineer's email, which is the Teams
        mention id (UPN). When set, the table is not read.
    GUARDIA_ONCALL_NAME (optional; defaults to the part of the email before "@")
    GUARDIA_ONCALL_BACKUP_EMAIL (optional) the backup, same rules; without it the
        answer has no backup and carries the NO_BACKUP_ONCALL warning (7.4)
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
from guardia_common.oncall import WARNING_NO_BACKUP, OnCallPerson, OnCallResolution, week_start
from guardia_common.oncall import resolve_on_call as lookup_on_call

LOGGER = get_logger("guardia.resolve_on_call")
ACTOR = "ResolveOnCall"


@dataclass
class Deps:
    schedule: Any  # boto3 DynamoDB Table: OnCallSchedule
    events: Any  # boto3 DynamoDB Table: IncidentEvents
    ssm: Any  # boto3 SSM client
    primary_email: str | None = None  # D19: when set, replaces the OnCallSchedule lookup
    primary_name: str | None = None
    backup_email: str | None = None


def _person(email: str, name: str | None = None) -> OnCallPerson:
    return OnCallPerson(display_name=name or email.split("@")[0], teams_user_id=email)


def _from_environment(deps: Deps, now: datetime, tz_name: str) -> OnCallResolution:
    """D19: one on-call person configured at deploy time, rotated by redeploying."""
    backup = _person(deps.backup_email) if deps.backup_email else None
    return OnCallResolution(
        week_start=week_start(now, tz_name),
        primary=_person(deps.primary_email, deps.primary_name),
        backup=backup,
        warnings=[] if backup else [WARNING_NO_BACKUP],
    )


def resolve_oncall(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    tz_name = get_threshold(load_thresholds(deps.ssm), "timezone")
    if deps.primary_email:
        resolution = _from_environment(deps, now, tz_name)
    else:
        resolution = lookup_on_call(deps.schedule, now, tz_name)

    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    flags = {
        "week_start": resolution.week_start,
        "has_primary": resolution.primary is not None,
        "has_backup": resolution.backup is not None,
        "warnings": resolution.warnings,
    }
    if deps.primary_email:
        flags["source"] = "environment"  # the email itself is never written to the audit
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
        primary_email=os.environ.get("GUARDIA_ONCALL_EMAIL") or None,
        primary_name=os.environ.get("GUARDIA_ONCALL_NAME") or None,
        backup_email=os.environ.get("GUARDIA_ONCALL_BACKUP_EMAIL") or None,
    )


def handler(event: dict, context: Any) -> dict:
    return resolve_oncall(event, _aws_deps())
