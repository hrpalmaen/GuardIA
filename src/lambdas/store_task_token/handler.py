"""StoreTaskToken (task 6.4/6.5 · requirement 8.3).

Invoked by the state machine with `.waitForTaskToken`. It saves the token in
the incident (the table is encrypted with the data key) and returns at once;
the state then waits until `ResolveOnSuccess` calls `SendTaskSuccess` with the
token, or until the state's timeout.

Each loop of the escalation stores a new token that replaces the previous one,
so a token from an earlier wait is never used again: Step Functions rejects it
and `ResolveOnSuccess` ignores that.

Event:  {"incident_id": "...", "task_token": "..."}
Output: {"incident_id": "...", "stored": true}

The token is a credential for the execution: it is never logged or audited.

Environment:
    INCIDENTS_TABLE
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3

from guardia_common.logger import get_logger, log

LOGGER = get_logger("guardia.store_task_token")


@dataclass
class Deps:
    incidents: Any  # boto3 DynamoDB Table


def store_task_token(event: dict, deps: Deps) -> dict:
    incident_id = event.get("incident_id")
    token = event.get("task_token")
    if not incident_id or not token:
        raise ValueError("StoreTaskToken requires incident_id and task_token")
    deps.incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET task_token = :t",
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={":t": token},
    )
    log(LOGGER, logging.INFO, "Task token stored", incident_id=incident_id)
    return {"incident_id": incident_id, "stored": True}


@cache
def _aws_deps() -> Deps:
    return Deps(incidents=boto3.resource("dynamodb").Table(os.environ["INCIDENTS_TABLE"]))


def handler(event: dict, context: Any) -> dict:
    return store_task_token(event, _aws_deps())
