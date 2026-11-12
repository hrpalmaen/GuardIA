"""OpenIncident (task 3.1 · requirements 2.2, 2.3, 2.4).

Opens exactly one incident per execution, enriches it with DescribeExecution,
records it in IncidentEvents, and starts GuardIA-Incident-Flow.

Environment:
    INCIDENTS_TABLE, PROCESS_REGISTRY_TABLE, INCIDENT_EVENTS_TABLE, INCIDENT_FLOW_ARN
    GUARDIA_SSM_PREFIX (optional, default /guardia/config)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from guardia_common.audit import AuditRepository
from guardia_common.config import get_threshold, load_thresholds
from guardia_common.incidents import Detection, build_incident, parse_detection
from guardia_common.logger import get_logger, log
from guardia_common.models import Incident, ProcessRecord
from guardia_common.schedule import minutes_until, parse_timestamp

LOGGER = get_logger("guardia.open_incident")
ACTOR = "OpenIncident"

# Fields of the incident event contract (design.md §3) sent to the flow.
CONTRACT_FIELDS = (
    "incident_id", "execution_arn", "organization", "process_type", "kind",
    "error", "cause", "deadline_iso", "remediation",
)  # fmt: skip


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table
    registry: Any
    events: Any
    sfn: Any  # boto3 Step Functions client
    ssm: Any  # boto3 SSM client
    flow_arn: str


def _enrich(detection: Detection, sfn: Any) -> tuple[Detection, bool]:
    """Adds error, cause, and input from DescribeExecution. Detection must not be
    lost if the call fails, so on error it keeps the data from the event."""
    if detection.source != "eventbridge" or not detection.execution_arn:
        return detection, True
    try:
        info = sfn.describe_execution(executionArn=detection.execution_arn)
    except ClientError as err:
        log(
            LOGGER,
            logging.WARNING,
            "DescribeExecution failed; using event data",
            execution_arn=detection.execution_arn,
            error_code=err.response["Error"]["Code"],
        )
        return detection, False
    return replace(
        detection,
        error=info.get("error") or detection.error,
        cause=info.get("cause") or detection.cause,
        execution_input=info.get("input") or detection.execution_input,
        started_at=parse_timestamp(info.get("startDate")) or detection.started_at,
        stopped_at=parse_timestamp(info.get("stopDate")) or detection.stopped_at,
    ), True


def _flow_input(incident: Incident) -> dict:
    data = incident.model_dump(mode="json")
    return {field: data.get(field) for field in CONTRACT_FIELDS}


def _start_flow(incident: Incident, deps: Deps) -> str:
    """Starts the flow named after the incident: Step Functions itself rejects a
    second execution with the same name, so a retried event cannot start two flows."""
    try:
        resp = deps.sfn.start_execution(
            stateMachineArn=deps.flow_arn,
            name=incident.incident_id,
            input=json.dumps(_flow_input(incident)),
        )
        flow_execution_arn = resp["executionArn"]
    except deps.sfn.exceptions.ExecutionAlreadyExists:
        flow_execution_arn = (
            deps.flow_arn.replace(":stateMachine:", ":execution:", 1) + f":{incident.incident_id}"
        )
    deps.incidents.update_item(
        Key={"incident_id": incident.incident_id},
        UpdateExpression="SET flow_execution_arn = :arn",
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={":arn": flow_execution_arn},
    )
    return flow_execution_arn


def open_incident(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)

    detection = parse_detection(event)
    if detection is None:
        return {"opened": False, "reason": "ignored_event"}

    row = deps.registry.get_item(Key={"resource_arn": detection.resource_arn}).get("Item")
    if not row:
        log(
            LOGGER,
            logging.WARNING,
            "Process not in ProcessRegistry; ignored",
            resource_arn=detection.resource_arn,
            kind=str(detection.kind),
        )
        return {"opened": False, "reason": "not_monitored"}
    process = ProcessRecord.from_item(row)

    detection, enriched = _enrich(detection, deps.sfn)
    thresholds = load_thresholds(deps.ssm)
    incident = build_incident(
        detection,
        process,
        default_deadline_local=get_threshold(thresholds, "deadline-local"),
        default_timezone=get_threshold(thresholds, "timezone"),
        now=now,
    )
    tz_name = process.timezone or get_threshold(thresholds, "timezone")
    audit = AuditRepository(deps.events, actor=ACTOR, tz_name=tz_name)

    try:
        deps.incidents.put_item(
            Item=incident.to_item(), ConditionExpression="attribute_not_exists(incident_id)"
        )
        created = True
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        created = False
        incident = Incident.from_item(
            deps.incidents.get_item(Key={"incident_id": incident.incident_id}, ConsistentRead=True)[
                "Item"
            ]
        )

    minutes_left = minutes_until(datetime.fromisoformat(incident.deadline_iso), now)
    if created:
        audit.append(
            incident.incident_id,
            "INCIDENT_OPENED",
            {
                "kind": str(incident.kind),
                "organization": incident.organization,
                "process_type": incident.process_type,
                "execution_arn": incident.execution_arn,
                "deadline_iso": incident.deadline_iso,
                "minutes_left": minutes_left,
                "enriched_with_describe_execution": enriched,
            },
            now=now,
        )
        log(
            LOGGER,
            logging.INFO,
            "Incident opened",
            incident_id=incident.incident_id,
            kind=str(incident.kind),
            organization=incident.organization,
            minutes_left=minutes_left,
        )
    else:
        log(
            LOGGER,
            logging.INFO,
            "Duplicate detection; incident already exists",
            incident_id=incident.incident_id,
        )

    # Also covers a retry after the incident was written but the flow failed to start.
    if not incident.flow_execution_arn:
        incident.flow_execution_arn = _start_flow(incident, deps)

    return {
        "opened": True,
        "created": created,
        "incident_id": incident.incident_id,
        "flow_execution_arn": incident.flow_execution_arn,
        "minutes_left": minutes_left,
        "incident": _flow_input(incident),
    }


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        registry=dynamodb.Table(os.environ["PROCESS_REGISTRY_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        sfn=boto3.client("stepfunctions"),
        ssm=boto3.client("ssm"),
        flow_arn=os.environ["INCIDENT_FLOW_ARN"],
    )


def handler(event: dict, context: Any) -> dict:
    return open_incident(event, _aws_deps())
