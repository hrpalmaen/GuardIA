"""RedriveOrRestart (task 5.2 · requirements 5.1, 12.1, 12.4).

Recovers the failed execution of an incident:
  * Standard execution eligible for redrive -> `RedriveExecution` (same execution ARN).
  * Otherwise (Express, not redrivable, expired) -> new execution with the
    original input.

It only acts when the process carries `guardia:remediation=auto`. The check is
repeated here on the state machine tag (defense in depth) in addition to the IAM
condition on the same tag. It never decides *whether* to recover: that is the
RemediationPolicy's job; this Lambda only enforces the safety limits.

Environment:
    INCIDENTS_TABLE, INCIDENT_EVENTS_TABLE
"""

from __future__ import annotations

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
from guardia_common.models import Incident

LOGGER = get_logger("guardia.redrive_or_restart")
ACTOR = "RedriveOrRestart"
REMEDIATION_TAG = "guardia:remediation"
MAX_ATTEMPTS = 2  # requirement 5.5
REDRIVABLE_STATUSES = {"FAILED", "TIMED_OUT", "ABORTED"}


class IncidentNotFoundError(LookupError):
    """The flow received an incident_id that is not in the Incidents table."""


class RemediationNotAllowedError(PermissionError):
    """The process does not have `guardia:remediation=auto` (requirements 12.1, 12.4)."""


class AttemptsExhaustedError(RuntimeError):
    """The incident already used its 2 recovery attempts (requirement 5.5)."""


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table
    events: Any
    sfn: Any  # boto3 Step Functions client


def _remediation_is_auto(sfn: Any, resource_arn: str) -> bool:
    """Reads the tag from the state machine itself; the stored copy is not trusted alone.
    If the tags cannot be read the answer is no: failing closed."""
    try:
        tags = sfn.list_tags_for_resource(resourceArn=resource_arn).get("tags", [])
    except ClientError as err:
        log(
            LOGGER,
            logging.WARNING,
            "Could not read remediation tag; treated as not allowed",
            resource_arn=resource_arn,
            error_code=err.response["Error"]["Code"],
        )
        return False
    return any(t["key"] == REMEDIATION_TAG and t["value"] == "auto" for t in tags)


def _claim_attempt(incidents: Any, incident: Incident) -> int:
    """Counts the attempt before acting. The condition on the current value makes a
    retried Lambda invocation fail instead of consuming a second attempt silently."""
    attempt = incident.attempts + 1
    try:
        incidents.update_item(
            Key={"incident_id": incident.incident_id},
            UpdateExpression="SET attempts = :new, #st = :rem",
            ConditionExpression="attempts = :old AND attempts < :max",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":new": attempt,
                ":old": incident.attempts,
                ":max": MAX_ATTEMPTS,
                ":rem": "REMEDIATING",
            },
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AttemptsExhaustedError(incident.incident_id) from err
        raise
    return attempt


def _is_standard(sfn: Any, state_machine_arn: str) -> bool:
    return sfn.describe_state_machine(stateMachineArn=state_machine_arn).get("type") == "STANDARD"


def _try_redrive(sfn: Any, incident: Incident) -> str | None:
    """Returns the execution ARN if the redrive was accepted, None if it does not apply."""
    if not incident.execution_arn:
        return None
    try:
        if not _is_standard(sfn, incident.resource_arn):
            return None
        info = sfn.describe_execution(executionArn=incident.execution_arn)
        if info.get("status") not in REDRIVABLE_STATUSES:
            return None
        sfn.redrive_execution(executionArn=incident.execution_arn)
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in ("ExecutionNotRedrivable", "ExecutionDoesNotExist"):
            log(
                LOGGER,
                logging.INFO,
                "Redrive does not apply; relaunching",
                incident_id=incident.incident_id,
                reason=code,
            )
            return None
        raise
    return incident.execution_arn


def _restart(sfn: Any, incident: Incident, attempt: int) -> str:
    """New execution with the original input. The name carries the attempt so a retried
    Lambda invocation cannot start the same attempt twice (Step Functions rejects a
    repeated name for Standard executions)."""
    name = f"{incident.incident_id}-r{attempt}"
    try:
        resp = sfn.start_execution(
            stateMachineArn=incident.resource_arn,
            name=name,
            input=incident.execution_input or "{}",
        )
        return resp["executionArn"]
    except sfn.exceptions.ExecutionAlreadyExists:
        return incident.resource_arn.replace(":stateMachine:", ":execution:", 1) + f":{name}"


def redrive_or_restart(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("RedriveOrRestart requires incident_id")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)
    incident = Incident.from_item(item)
    audit = AuditRepository(deps.events, actor=ACTOR)

    if incident.remediation != "auto" or not _remediation_is_auto(deps.sfn, incident.resource_arn):
        audit.append(
            incident_id,
            "REMEDIATION_REJECTED",
            {"reason": "remediation_not_auto", "resource_arn": incident.resource_arn},
            now=now,
        )
        log(
            LOGGER,
            logging.WARNING,
            "Recovery rejected: process is not remediation=auto",
            incident_id=incident_id,
        )
        raise RemediationNotAllowedError(incident.resource_arn)

    if not incident.execution_arn and not incident.execution_input:
        # MISSED_RUN: nothing ran, so there is no original input to relaunch with.
        return {"incident_id": incident_id, "recovered": False, "reason": "nothing_to_recover"}

    attempt = _claim_attempt(deps.incidents, incident)

    recovery_arn = _try_redrive(deps.sfn, incident)
    strategy = "redrive"
    if recovery_arn is None:
        strategy = "restart"
        recovery_arn = _restart(deps.sfn, incident, attempt)

    deps.incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET recovery_execution_arn = :arn",
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={":arn": recovery_arn},
    )
    audit.append(
        incident_id,
        "RECOVERY_STARTED",
        {"strategy": strategy, "attempt": attempt, "recovery_execution_arn": recovery_arn},
        now=now,
    )
    log(
        LOGGER,
        logging.INFO,
        "Recovery started",
        incident_id=incident_id,
        strategy=strategy,
        attempt=attempt,
    )
    return {
        "incident_id": incident_id,
        "recovered": True,
        "strategy": strategy,
        "attempt": attempt,
        "recovery_execution_arn": recovery_arn,
    }


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        sfn=boto3.client("stepfunctions"),
    )


def handler(event: dict, context: Any) -> dict:
    return redrive_or_restart(event, _aws_deps())
