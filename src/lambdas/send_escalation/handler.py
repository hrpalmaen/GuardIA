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
    mode           initial (default) | reminder | sla_breached | resolved
    recipients     ResolveOnCall output (not used by `resolved`)
    diagnosis      optional {"summary": str, "actions": [{"action", "result"}]};
                   without it, `initial` builds it from the incident's audit
                   events (RECOVERY_STARTED, RECOVERY_FINISHED, POLICY_DECIDED)
    parallel       optional bool; true for REMEDIATE_AND_ESCALATE (5.5, 6.1)

Output: {"incident_id", "teams_thread_id", "posted", "mentioned", "warnings"}.

Modes (T5):
    initial       full diagnosis; mentions the on-call engineer. Posting twice
                  is avoided: if `teams_thread_id` is already stored, the
                  Lambda answers with it and posts nothing.
    reminder      "still unresolved", minutes left and the reminder number;
                  mentions the on-call engineer and the backup (8.1).
    sla_breached  "the deadline passed"; mentions both (6.2).
    resolved      how it was resolved and how long it took; mentions nobody.

The Incidents status is not changed here: the state machine owns transitions.
The webhook posts a new message each time (it cannot reply in a thread), so
`teams_thread_id` is GuardIA's own identifier and every card shows the id.

Only `initial` fails the execution when Teams rejects 3 attempts; the flow
tolerates a failed reminder, SLA notice, or resolution message.

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

from guardia_common.audit import AuditRepository, read_events
from guardia_common.config import get_threshold, load_thresholds
from guardia_common.logger import get_logger, log
from guardia_common.models import Incident
from guardia_common.oncall import WARNING_NO_PRIMARY
from guardia_common.schedule import get_tz, minutes_until, parse_timestamp
from guardia_common.teams_client import (
    STATUS_MODES,
    TeamsPostError,
    build_escalation_card,
    build_status_card,
    get_webhook_url,
    post_card,
)

LOGGER = get_logger("guardia.send_escalation")
ACTOR = "SendEscalation"
MAX_POST_ATTEMPTS = 3  # requirement 7.3
MODES = ("initial", *STATUS_MODES)
# Audit event written once Teams accepts each kind of message.
SENT_EVENT = {
    "initial": "ESCALATION_SENT",
    "reminder": "REMINDER_SENT",
    "sla_breached": "SLA_BREACH_NOTIFIED",
    "resolved": "RESOLUTION_NOTIFIED",
}


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


def diagnosis_from_events(events: list[dict], summary: str | None = None) -> dict:
    """The diagnosis of the card (7.2) from what GuardIA already recorded: the
    recovery attempts and their outcome, and the policy decision. Only event
    names and short fields are used; error causes never come from here."""
    actions: list[dict] = []
    for event in events:
        detail = event.get("detail") or {}
        kind = event.get("event_type")
        if kind == "RECOVERY_STARTED":
            actions.append(
                {
                    "action": f"{detail.get('strategy', 'recuperación')} "
                    f"(intento {detail.get('attempt', '?')})",
                    "result": "iniciado",
                }
            )
        elif kind == "RECOVERY_FINISHED":
            actions.append({"action": "ejecución recuperada", "result": detail.get("status", "?")})
        elif kind == "POLICY_DECIDED" and not summary:
            summary = f"Decisión de la política: {detail.get('decision')} ({detail.get('reason')})"
    return {"summary": summary, "actions": actions}


def _post_with_retries(
    deps: Deps, webhook_url: str, card: dict, audit: AuditRepository, incident_id: str,
    kind: str, now: datetime,
) -> tuple[int, str | None]:  # fmt: skip
    """Requirement 7.3: up to 3 attempts. After the third, `NOTIFICATION_FAILED`
    and an exception."""
    last_status: int | None = None
    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        try:
            return attempt, deps.poster(webhook_url, card)
        except TeamsPostError as err:
            last_status = err.status
            log(
                LOGGER,
                logging.WARNING,
                "Teams post failed",
                incident_id=incident_id,
                kind=kind,
                attempt=attempt,
                status=err.status,
            )
            if attempt < MAX_POST_ATTEMPTS:
                deps.sleep(deps.backoff_s * 2 ** (attempt - 1))
    audit.append(
        incident_id,
        "NOTIFICATION_FAILED",
        {"kind": kind, "attempts": MAX_POST_ATTEMPTS, "last_status": last_status},
        now,
    )
    raise EscalationDeliveryError(
        f"Teams {kind} failed after {MAX_POST_ATTEMPTS} attempts for {incident_id}"
    )


def send_escalation(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("SendEscalation requires incident_id")
    mode = event.get("mode") or "initial"
    if mode not in MODES:
        raise ValueError(f"SendEscalation mode must be one of {list(MODES)}, not {mode!r}")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)

    existing = item.get("teams_thread_id")
    recipients, mentioned, warnings = _recipients(event)
    if mode == "resolved":
        mentioned = []  # nobody to alert: the incident is over
    if existing and mode == "initial":
        log(LOGGER, logging.INFO, "Escalation already posted", incident_id=incident_id)
        return {
            "incident_id": incident_id,
            "teams_thread_id": existing,
            "posted": False,
            "mentioned": mentioned,
            "warnings": warnings,
        }

    if mode in ("reminder", "sla_breached") and item.get("status") == "RESOLVED":
        # The recovery that ran next to the escalation (D25) closed it: nobody
        # should be paged about an incident that is over.
        log(LOGGER, logging.INFO, "Incident already resolved", incident_id=incident_id, mode=mode)
        return {
            "incident_id": incident_id,
            "teams_thread_id": existing,
            "posted": False,
            "skipped": "already_resolved",
            "mentioned": [],
            "warnings": warnings,
        }

    incident = Incident.from_item(item)
    tz_name = get_threshold(load_thresholds(deps.ssm), "timezone")
    deadline = parse_timestamp(incident.deadline_iso)
    minutes_remaining = minutes_until(deadline, now)
    deadline_label = f"{deadline.astimezone(get_tz(tz_name)):%H:%M}"
    audit = AuditRepository(deps.events, actor=ACTOR, tz_name=tz_name)
    detail: dict = {"mentioned": mentioned, "warnings": warnings}

    if mode == "initial":
        diagnosis = event.get("diagnosis")
        if not diagnosis:
            diagnosis = diagnosis_from_events(
                read_events(deps.events, incident_id), summary=item.get("agent_summary")
            )
        card = build_escalation_card(
            incident=incident.model_dump(mode="json"),
            recipients=recipients,
            diagnosis=diagnosis,
            minutes_remaining=minutes_remaining,
            deadline_label=deadline_label,
            parallel=bool(event.get("parallel")),
            warnings=warnings,
        )
        detail.update(parallel=bool(event.get("parallel")), minutes_remaining=minutes_remaining)
    else:
        events = read_events(deps.events, incident_id)
        reminder_number = 1 + sum(
            1 for e in events if e.get("event_type") == SENT_EVENT["reminder"]
        )
        opened = parse_timestamp(incident.opened_at)
        resolved_by = (event.get("resolution") or {}).get("resolved_by") or incident.resolved_by
        duration = minutes_until(now, opened) if opened else None
        card = build_status_card(
            mode=mode,
            incident=incident.model_dump(mode="json"),
            recipients=recipients,
            minutes_remaining=minutes_remaining,
            deadline_label=deadline_label,
            reminder_number=reminder_number if mode == "reminder" else None,
            resolved_by=resolved_by,
            duration_minutes=duration,
            warnings=warnings,
        )
        if mode == "reminder":
            detail["reminder_number"] = reminder_number
        detail["minutes_remaining"] = minutes_remaining

    webhook_url = get_webhook_url(deps.secrets, deps.secret_id)
    kind = "escalation" if mode == "initial" else mode
    attempt, message_id = _post_with_retries(deps, webhook_url, card, audit, incident_id, kind, now)

    thread_id = existing
    if mode == "initial":
        # Workflows webhooks usually return no id; the incident id then keys the thread.
        thread_id = message_id or f"webhook-{incident_id}"
        deps.incidents.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET teams_thread_id = :t",
            ConditionExpression="attribute_exists(incident_id)",
            ExpressionAttributeValues={":t": thread_id},
        )
        detail["thread_id_from_teams"] = message_id is not None
    audit.append(incident_id, SENT_EVENT[mode], {"attempts_used": attempt, **detail}, now)
    log(
        LOGGER,
        logging.INFO,
        "Teams message posted",
        incident_id=incident_id,
        mode=mode,
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
