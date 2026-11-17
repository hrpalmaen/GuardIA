"""SendEscalation (task 6.3 · requirements 7.2, 7.3, 7.4).

Posts the escalation card to Teams, mentioning the on-call engineer (or the
backup if there is none), with the diagnosis and the minutes left until the
deadline. Retries the post up to 3 times with backoff. Teams is the only
escalation channel, so if all three fail the failure is recorded as
`NOTIFICATION_FAILED` and the Lambda raises, which sends the invocation to the
retry/DLQ path and the CloudWatch alarm.

Event:
    incident_id    required; organization, process, error and deadline are read
                   from the Incidents row, not trusted from the event
    recipients     ResolveOnCall output
    diagnosis      optional {"summary": str, "actions": [{"action", "result"}]}
    parallel       optional bool; true for REMEDIATE_AND_ESCALATE (5.5, 6.1)

Output: {"incident_id", "teams_thread_id", "posted", "mentioned", "warnings"}.

The Incidents status is not changed here: the state machine owns transitions.
Posting twice for the same incident is avoided: if `teams_thread_id` is
already stored, the Lambda answers with it and posts nothing.

Environment:
    INCIDENTS_TABLE, INCIDENT_EVENTS_TABLE, TEAMS_ALERTS_SECRET_ID
    GUARDIA_SSM_PREFIX (optional, default /guardia/config)
    TEAMS_RETRY_BACKOFF_SECONDS (optional, default 1.0; waits 1x, 2x between tries)
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from guardia_common.audit import AuditRepository
from guardia_common.config import get_threshold, load_thresholds
from guardia_common.logger import get_logger, log
from guardia_common.models import Incident
from guardia_common.oncall import WARNING_NO_PRIMARY
from guardia_common.schedule import get_tz, minutes_until, parse_timestamp
from guardia_common.teams_client import (
    TeamsPostError,
    build_escalation_card,
    get_webhook_url,
    post_card,
)

LOGGER = get_logger("guardia.send_escalation")
ACTOR = "SendEscalation"
MAX_POST_ATTEMPTS = 3  # requirement 7.3


class IncidentNotFoundError(LookupError):
    """The flow sent an incident_id that is not in the Incidents table."""


class EscalationDeliveryError(RuntimeError):
    """Teams rejected every attempt. Raised so the invocation is retried/dead-lettered."""


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table
    events: Any  # boto3 DynamoDB Table
    secrets: Any  # boto3 Secrets Manager client
    ssm: Any  # boto3 SSM client
    secret_id: str
    poster: Callable[[str, dict], str | None] = post_card
    sleep: Callable[[float], None] = time.sleep
    backoff_s: float = field(default=1.0)


def _recipients(event: dict) -> tuple[dict, list[str], list[str]]:
    recipients = event.get("recipients") or {}
    warnings = list(recipients.get("warnings") or [])
    mentioned = [role for role in ("primary", "backup") if recipients.get(role)]
    # Requirement 7.4: nobody on call -> the backup is mentioned and a warning is recorded.
    if "primary" not in mentioned and WARNING_NO_PRIMARY not in warnings and "backup" in mentioned:
        warnings.append(WARNING_NO_PRIMARY)
    return recipients, mentioned, warnings


def send_escalation(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("SendEscalation requires incident_id")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)

    existing = item.get("teams_thread_id")
    recipients, mentioned, warnings = _recipients(event)
    if existing:
        log(LOGGER, logging.INFO, "Escalation already posted", incident_id=incident_id)
        return {
            "incident_id": incident_id,
            "teams_thread_id": existing,
            "posted": False,
            "mentioned": mentioned,
            "warnings": warnings,
        }

    incident = Incident.from_item(item)
    tz_name = get_threshold(load_thresholds(deps.ssm), "timezone")
    deadline = parse_timestamp(incident.deadline_iso)
    card = build_escalation_card(
        incident=incident.model_dump(mode="json"),
        recipients=recipients,
        diagnosis=event.get("diagnosis"),
        minutes_remaining=minutes_until(deadline, now),
        deadline_label=f"{deadline.astimezone(get_tz(tz_name)):%H:%M}",
        parallel=bool(event.get("parallel")),
        warnings=warnings,
    )

    audit = AuditRepository(deps.events, actor=ACTOR, tz_name=tz_name)
    webhook_url = get_webhook_url(deps.secrets, deps.secret_id)

    last_status: int | None = None
    message_id: str | None = None
    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        try:
            message_id = deps.poster(webhook_url, card)
            break
        except TeamsPostError as err:
            last_status = err.status
            log(
                LOGGER,
                logging.WARNING,
                "Teams post failed",
                incident_id=incident_id,
                attempt=attempt,
                status=err.status,
            )
            if attempt < MAX_POST_ATTEMPTS:
                deps.sleep(deps.backoff_s * 2 ** (attempt - 1))
    else:
        audit.append(
            incident_id,
            "NOTIFICATION_FAILED",
            {"kind": "escalation", "attempts": MAX_POST_ATTEMPTS, "last_status": last_status},
            now,
        )
        raise EscalationDeliveryError(
            f"Teams escalation failed after {MAX_POST_ATTEMPTS} attempts for {incident_id}"
        )

    # Workflows webhooks usually return no id; the incident id then keys the thread.
    thread_id = message_id or f"webhook-{incident_id}"
    deps.incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET teams_thread_id = :t",
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={":t": thread_id},
    )
    audit.append(
        incident_id,
        "ESCALATION_SENT",
        {
            "attempts_used": attempt,
            "mentioned": mentioned,
            "warnings": warnings,
            "parallel": bool(event.get("parallel")),
            "minutes_remaining": minutes_until(deadline, now),
            "thread_id_from_teams": message_id is not None,
        },
        now,
    )
    log(
        LOGGER,
        logging.INFO,
        "Escalation posted",
        incident_id=incident_id,
        mentioned=mentioned,
        warnings=warnings,
        attempts_used=attempt,
    )
    return {
        "incident_id": incident_id,
        "teams_thread_id": thread_id,
        "posted": True,
        "mentioned": mentioned,
        "warnings": warnings,
    }


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        secrets=boto3.client("secretsmanager"),
        ssm=boto3.client("ssm"),
        secret_id=os.environ["TEAMS_ALERTS_SECRET_ID"],
        backoff_s=float(os.environ.get("TEAMS_RETRY_BACKOFF_SECONDS", "1.0")),
    )


def handler(event: dict, context: Any) -> dict:
    return send_escalation(event, _aws_deps())
