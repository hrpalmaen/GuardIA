"""ResolveOnSuccess (task 6.4 · requirements 8.3, 2.2).

An escalated incident closes by itself when the process ends well. EventBridge
sends every `SUCCEEDED` Step Functions event (`SUCCEEDED_PATTERN`); this Lambda
keeps only the ones that matter:

1. the state machine is in `ProcessRegistry` (otherwise: ignored);
2. the same machine has an incident `ESCALATED` or `SLA_BREACHED` (the
   `by_resource_arn` index, newest first) that was opened before the execution
   stopped, in the same organization, with a stored task token.

For each one it calls `SendTaskSuccess` with that token and
`{"resolved_by": "later_execution", "execution_arn": ...}`; the flow continues
at its waiting state, marks the incident `RESOLVED`, and posts the resolution.
This Lambda changes no status itself.

Ignored without error: no incident, other organization, no token yet, a token
that Step Functions no longer accepts (an earlier loop, a duplicate event, a
timed-out wait). Each ignored incident is recorded as `RESOLVE_IGNORED`.

Output: {"signalled": [incident_id], "ignored": [{"incident_id" | null, "reason"}]}

Environment:
    INCIDENTS_TABLE, PROCESS_REGISTRY_TABLE, INCIDENT_EVENTS_TABLE
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from guardia_common.audit import AuditRepository
from guardia_common.logger import get_logger, log
from guardia_common.models import IncidentStatus
from guardia_common.schedule import parse_timestamp
from guardia_common.tables import RESOURCE_ARN_INDEX

LOGGER = get_logger("guardia.resolve_on_success")
ACTOR = "ResolveOnSuccess"
RESOLVED_BY = "later_execution"
OPEN_STATUSES = (IncidentStatus.ESCALATED.value, IncidentStatus.SLA_BREACHED.value)
STALE_TOKEN_ERRORS = {"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"}
MAX_INCIDENTS = 25  # the newest incidents of one process are enough


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table (index by_resource_arn)
    registry: Any  # boto3 DynamoDB Table: ProcessRegistry
    events: Any  # boto3 DynamoDB Table: IncidentEvents
    sfn: Any  # boto3 Step Functions client


def _recent_incidents(table: Any, resource_arn: str) -> list[dict]:
    response = table.query(
        IndexName=RESOURCE_ARN_INDEX,
        KeyConditionExpression="resource_arn = :r",
        ExpressionAttributeValues={":r": resource_arn},
        ScanIndexForward=False,
        Limit=MAX_INCIDENTS,
    )
    return response.get("Items", [])


def resolve_on_success(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    detail = event.get("detail") or {}
    machine_arn = detail.get("stateMachineArn")
    execution_arn = detail.get("executionArn")
    result: dict = {"signalled": [], "ignored": []}
    if detail.get("status") != "SUCCEEDED" or not machine_arn or not execution_arn:
        result["ignored"].append({"incident_id": None, "reason": "not_a_success_event"})
        return result

    process = deps.registry.get_item(Key={"resource_arn": machine_arn}).get("Item")
    if not process:
        result["ignored"].append({"incident_id": None, "reason": "process_not_monitored"})
        return result

    stopped = parse_timestamp(detail.get("stopDate")) or now
    audit = AuditRepository(deps.events, actor=ACTOR)
    open_items = _recent_incidents(deps.incidents, machine_arn)
    candidates = [i for i in open_items if i.get("status") in OPEN_STATUSES]
    if not candidates:
        result["ignored"].append({"incident_id": None, "reason": "no_open_incident"})
        return result

    for item in candidates:
        incident_id = item["incident_id"]
        reason = None
        if item.get("organization") != process.get("organization"):
            reason = "other_organization"
        elif (opened := parse_timestamp(item.get("opened_at"))) and stopped < opened:
            reason = "succeeded_before_the_incident"
        elif not item.get("task_token"):
            reason = "no_task_token_yet"
        if reason is None:
            try:
                deps.sfn.send_task_success(
                    taskToken=item["task_token"],
                    output=json.dumps({"resolved_by": RESOLVED_BY, "execution_arn": execution_arn}),
                )
            except ClientError as err:
                if err.response["Error"]["Code"] not in STALE_TOKEN_ERRORS:
                    raise
                reason = "stale_task_token"
        if reason:
            result["ignored"].append({"incident_id": incident_id, "reason": reason})
            audit.append(
                incident_id,
                "RESOLVE_IGNORED",
                {"reason": reason, "execution_arn": execution_arn},
                now=now,
            )
            continue
        result["signalled"].append(incident_id)
        audit.append(
            incident_id,
            "RESOLVE_SIGNALLED",
            {"resolved_by": RESOLVED_BY, "execution_arn": execution_arn},
            now=now,
        )
        log(LOGGER, logging.INFO, "Resolution signalled", incident_id=incident_id)
    return result


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        registry=dynamodb.Table(os.environ["PROCESS_REGISTRY_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        sfn=boto3.client("stepfunctions"),
    )


def handler(event: dict, context: Any) -> dict:
    return resolve_on_success(event, _aws_deps())
