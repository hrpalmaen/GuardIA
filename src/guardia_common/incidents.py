"""Incident opening rules (requirement 2 and the incident event contract).

Pure functions: parse a detection, derive its idempotent identifier, and
build the `Incident` with its SLA deadline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from .models import Incident, IncidentKind, IncidentStatus, ProcessRecord
from .schedule import parse_timestamp, sla_deadline, to_local_iso

STATUS_CHANGE_DETAIL_TYPE = "Step Functions Execution Status Change"
WATCHDOG_SOURCE = "guardia.watchdog"
FAILURE_STATUSES = {IncidentKind.FAILED, IncidentKind.TIMED_OUT, IncidentKind.ABORTED}
WATCHDOG_KINDS = {IncidentKind.MISSED_RUN, IncidentKind.STUCK_RUN}

# Fixed namespace: the same execution always maps to the same incident_id.
_INCIDENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:guardia:incident")


class InvalidDetectionError(ValueError):
    """The event looks like a detection but is missing required fields."""


@dataclass(frozen=True)
class Detection:
    kind: IncidentKind
    resource_arn: str
    execution_arn: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    expected_at: datetime | None = None
    error: str | None = None
    cause: str | None = None
    execution_input: str | None = None
    source: str = "eventbridge"


def parse_detection(event: dict) -> Detection | None:
    """Returns a Detection, or None for events GuardIA must ignore (e.g. SUCCEEDED, 2.2).

    Accepted inputs:
    * EventBridge `Step Functions Execution Status Change` with FAILED, TIMED_OUT, ABORTED.
    * MissedRunWatchdog detections, invoked directly or through EventBridge
      (`source = "guardia.watchdog"`):
      `{"kind": "MISSED_RUN", "resource_arn": ..., "expected_at": ISO}`
      `{"kind": "STUCK_RUN", "resource_arn": ..., "execution_arn": ..., "started_at": ISO}`
    """
    if event.get("detail-type") == STATUS_CHANGE_DETAIL_TYPE:
        detail = event.get("detail") or {}
        status = detail.get("status")
        if status not in {k.value for k in FAILURE_STATUSES}:
            return None
        try:
            return Detection(
                kind=IncidentKind(status),
                resource_arn=detail["stateMachineArn"],
                execution_arn=detail["executionArn"],
                started_at=parse_timestamp(detail.get("startDate")),
                stopped_at=parse_timestamp(detail.get("stopDate")),
                error=detail.get("error"),
                cause=detail.get("cause"),
                execution_input=detail.get("input"),
            )
        except KeyError as exc:
            raise InvalidDetectionError(f"status event without {exc}") from exc

    body = event.get("detail", {}) if event.get("source") == WATCHDOG_SOURCE else event
    kind = body.get("kind")
    if kind not in {k.value for k in WATCHDOG_KINDS}:
        return None
    detection = Detection(
        kind=IncidentKind(kind),
        resource_arn=body.get("resource_arn") or "",
        execution_arn=body.get("execution_arn"),
        started_at=parse_timestamp(body.get("started_at")),
        expected_at=parse_timestamp(body.get("expected_at")),
        source="watchdog",
    )
    if not detection.resource_arn:
        raise InvalidDetectionError(f"{kind} detection without resource_arn")
    if detection.kind == IncidentKind.MISSED_RUN and detection.expected_at is None:
        raise InvalidDetectionError("MISSED_RUN detection without expected_at")
    if detection.kind == IncidentKind.STUCK_RUN and not detection.execution_arn:
        raise InvalidDetectionError("STUCK_RUN detection without execution_arn")
    return detection


def incident_id_for(detection: Detection) -> str:
    """One incident per execution (2.4). A missed run has no execution, so its
    key is the process plus the scheduled time it missed."""
    if detection.execution_arn:
        key = detection.execution_arn
    else:
        key = f"MISSED_RUN|{detection.resource_arn}|{detection.expected_at.isoformat()}"
    return str(uuid.uuid5(_INCIDENT_NAMESPACE, key))


def build_incident(
    detection: Detection,
    process: ProcessRecord,
    *,
    default_deadline_local: str,
    default_timezone: str,
    now: datetime,
) -> Incident:
    """Requirement 2.3: organization, process type, execution, cause, and SLA deadline."""
    tz_name = process.timezone or default_timezone
    reference = detection.expected_at or detection.started_at or now
    deadline = sla_deadline(reference, process.deadline_local or default_deadline_local, tz_name)

    def iso(moment: datetime | None) -> str | None:
        return to_local_iso(moment, tz_name) if moment else None

    return Incident(
        incident_id=incident_id_for(detection),
        execution_arn=detection.execution_arn,
        organization=process.organization,
        process_type=process.process_type,
        kind=detection.kind,
        error=detection.error,
        cause=detection.cause,
        deadline_iso=deadline.isoformat(timespec="seconds"),
        remediation=process.remediation,
        resource_arn=detection.resource_arn,
        status=IncidentStatus.OPEN,
        attempts=0,
        opened_at=iso(now),
        started_at=iso(detection.started_at),
        stopped_at=iso(detection.stopped_at),
        expected_at=iso(detection.expected_at),
        execution_input=detection.execution_input,
    )
