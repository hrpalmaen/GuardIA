"""RemediationPolicy (task 5.1 · requirements 5.1–5.6, 6.1).

Runs `guardia_common.policy.decide` for the incident and records the decision.
Everything it decides on is read from the Incidents table (classification,
catalog action, attempts, opening time, remediation tag copy, deadline), not
from the event, so the flow state cannot steer it.

Event:  {"incident_id": "...", "escalation_running": false}
Output: {"incident_id", "decision", "reason", "wait_seconds", "minutes_left"}
        decision: REMEDIATE | REMEDIATE_AND_ESCALATE | ESCALATE

Environment:
    INCIDENTS_TABLE, INCIDENT_EVENTS_TABLE
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
from guardia_common.config import load_thresholds
from guardia_common.logger import get_logger, log
from guardia_common.models import Incident
from guardia_common.policy import decide

LOGGER = get_logger("guardia.remediation_policy")
ACTOR = "RemediationPolicy"


class IncidentNotFoundError(LookupError):
    """The flow received an incident_id that is not in the Incidents table."""


@dataclass
class Deps:
    incidents: Any
    events: Any
    ssm: Any


def remediation_policy(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id")
    if not incident_id:
        raise ValueError("RemediationPolicy requires incident_id")
    escalation_running = bool(event.get("escalation_running", False))

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)
    incident = Incident.from_item(item)

    result = decide(
        incident, load_thresholds(deps.ssm), now, escalation_running=escalation_running
    ).as_dict()
    AuditRepository(deps.events, actor=ACTOR).append(
        incident_id,
        "POLICY_DECIDED",
        {**result, "attempts": incident.attempts, "escalation_running": escalation_running},
        now=now,
    )
    log(
        LOGGER,
        logging.INFO,
        "Remediation policy decided",
        incident_id=incident_id,
        decision=result["decision"],
        reason=result["reason"],
        attempts=incident.attempts,
    )
    return {"incident_id": incident_id, **result}


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        ssm=boto3.client("ssm"),
    )


def handler(event: dict, context: Any) -> dict:
    return remediation_policy(event, _aws_deps())
