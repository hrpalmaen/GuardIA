"""UpdateIncidentStatus (task 5.5, T1 · requirements 10.1, 10.2).

The only way the incident status changes after OpenIncident: GuardIA-Incident-Flow
calls this Lambda at each transition (REMEDIATING, ESCALATED, SLA_BREACHED,
RESOLVED), so every change is also an audit event.

Event:
    incident_id   required
    status        required; REMEDIATING | ESCALATED | SLA_BREACHED | RESOLVED
    event_type    optional; defaults per status (INCIDENT_ESCALATED, ...)
    details       optional dict, redacted before it is audited; for RESOLVED,
                  `resolved_by` is also stored on the incident

Output: {"incident_id", "status", "previous_status", "changed", "reason"}

Idempotent: Step Functions may retry a task, so setting the status the
incident already has changes nothing and audits nothing. RESOLVED is final: a
late reminder or SLA transition after the resolution is ignored, not applied.

Environment:
    INCIDENTS_TABLE, INCIDENT_EVENTS_TABLE
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from guardia_common.audit import AuditRepository
from guardia_common.logger import get_logger, log
from guardia_common.models import IncidentStatus
from guardia_common.redaction import redact_value

LOGGER = get_logger("guardia.update_incident_status")
ACTOR = "GuardIA-Incident-Flow"

DEFAULT_EVENT_TYPES = {
    IncidentStatus.REMEDIATING: "INCIDENT_REMEDIATING",
    IncidentStatus.ESCALATED: "INCIDENT_ESCALATED",
    IncidentStatus.SLA_BREACHED: "SLA_BREACHED",
    IncidentStatus.RESOLVED: "INCIDENT_RESOLVED",
}
FINAL_STATUS = IncidentStatus.RESOLVED
_EVENT_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_MAX_RESOLVED_BY = 64


class InvalidStatusUpdateError(ValueError):
    """The flow asked for a status or event type GuardIA does not allow."""


class IncidentNotFoundError(LookupError):
    """The flow received an incident_id that is not in the Incidents table."""


@dataclass
class Deps:
    incidents: Any
    events: Any


def _parse(event: dict) -> tuple[str, IncidentStatus, str, dict]:
    incident_id = event.get("incident_id")
    if not incident_id:
        raise InvalidStatusUpdateError("UpdateIncidentStatus requires incident_id")
    try:
        status = IncidentStatus(event.get("status"))
    except ValueError as exc:
        raise InvalidStatusUpdateError(f"invalid status {event.get('status')!r}") from exc
    if status not in DEFAULT_EVENT_TYPES:
        raise InvalidStatusUpdateError(f"the flow cannot set status {status.value}")
    event_type = event.get("event_type") or DEFAULT_EVENT_TYPES[status]
    if not _EVENT_TYPE.match(event_type):
        raise InvalidStatusUpdateError(f"invalid event_type {event_type!r}")
    details = event.get("details") or {}
    if not isinstance(details, dict):
        raise InvalidStatusUpdateError("details must be an object")
    return incident_id, status, event_type, redact_value(details)


def update_incident_status(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id, status, event_type, details = _parse(event)
    stamp = now.isoformat()

    names = {"#s": "status"}
    values: dict[str, Any] = {":s": status.value, ":u": stamp, ":final": FINAL_STATUS.value}
    expression = "SET #s = :s, updated_at = :u"
    if status == IncidentStatus.RESOLVED:
        expression += ", resolved_at = :u"
        resolved_by = details.get("resolved_by")
        if resolved_by:
            expression += ", resolved_by = :rb"
            values[":rb"] = str(resolved_by)[:_MAX_RESOLVED_BY]

    try:
        response = deps.incidents.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(incident_id) AND #s <> :s AND #s <> :final",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_OLD",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return _unchanged(incident_id, status, deps)

    previous = (response.get("Attributes") or {}).get("status")
    AuditRepository(deps.events, actor=ACTOR).append(
        incident_id,
        event_type,
        {"status": status.value, "previous_status": previous, **details},
        now=now,
    )
    log(
        LOGGER,
        logging.INFO,
        "Incident status changed",
        incident_id=incident_id,
        status=status.value,
        previous_status=previous,
    )
    return {
        "incident_id": incident_id,
        "status": status.value,
        "previous_status": previous,
        "changed": True,
        "reason": None,
    }


def _unchanged(incident_id: str, requested: IncidentStatus, deps: Deps) -> dict:
    item = deps.incidents.get_item(
        Key={"incident_id": incident_id}, ConsistentRead=True, ProjectionExpression="#s",
        ExpressionAttributeNames={"#s": "status"},
    ).get("Item")  # fmt: skip
    if not item:
        raise IncidentNotFoundError(incident_id)
    current = item.get("status")
    reason = "already_final" if current == FINAL_STATUS.value else "same_status"
    log(
        LOGGER,
        logging.INFO,
        "Incident status unchanged",
        incident_id=incident_id,
        requested=requested.value,
        current=current,
        reason=reason,
    )
    return {
        "incident_id": incident_id,
        "status": current,
        "previous_status": current,
        "changed": False,
        "reason": reason,
    }


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
    )


def handler(event: dict, context: Any) -> dict:
    return update_incident_status(event, _aws_deps())
