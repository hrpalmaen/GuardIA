"""ClassifyIncident (task 4.1 · requirements 4.1, 4.2, 4.3, 4.4).

First state of GuardIA-Incident-Flow. Classifies the incident with the
deterministic ErrorCatalog only; it never decides or executes an action.

Output `classification`:
    auto_recoverable | requires_escalation  -> RemediationPolicy
    unknown                                 -> InvokeAgent (Bedrock, 4.5)

Environment:
    INCIDENTS_TABLE, ERROR_CATALOG_TABLE, INCIDENT_EVENTS_TABLE
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3
from pydantic import ValidationError

from guardia_common.audit import AuditRepository
from guardia_common.classification import classify
from guardia_common.logger import get_logger, log
from guardia_common.models import CatalogEntry, Incident, from_dynamo

LOGGER = get_logger("guardia.classify_incident")
ACTOR = "ClassifyIncident"
_CATALOG_CACHE_SECONDS = 300
_catalog_cache: dict[str, Any] = {"entries": None, "loaded_at": 0.0, "table": None}


class IncidentNotFoundError(LookupError):
    """The flow received an incident_id that is not in the Incidents table."""


@dataclass
class Deps:
    incidents: Any
    catalog: Any
    events: Any


def load_catalog(table: Any, *, force: bool = False) -> list[CatalogEntry]:
    """Scans ErrorCatalog. Invalid rows are skipped and logged: a broken row
    must not stop classification (the incident then falls to `unknown`)."""
    fresh = time.monotonic() - _catalog_cache["loaded_at"] < _CATALOG_CACHE_SECONDS
    if not force and _catalog_cache["table"] is table and fresh:
        return _catalog_cache["entries"]

    rows, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    entries = []
    for row in rows:
        try:
            entries.append(CatalogEntry.model_validate(from_dynamo(row)))
        except ValidationError as err:
            log(
                LOGGER,
                logging.WARNING,
                "Invalid ErrorCatalog row skipped",
                process_type=row.get("process_type"),
                error_pattern=row.get("error_pattern"),
                errors=err.error_count(),
            )

    _catalog_cache.update(entries=entries, loaded_at=time.monotonic(), table=table)
    return entries


def clear_catalog_cache() -> None:
    _catalog_cache.update(entries=None, loaded_at=0.0, table=None)


def classify_incident(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("ClassifyIncident requires incident_id")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)
    incident = Incident.from_item(item)

    result = classify(incident, load_catalog(deps.catalog))

    deps.incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET classification = :c, catalog_action = :a, catalog_max_wait_s = :w",
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={
            ":c": str(result.classification),
            ":a": str(result.action) if result.action else None,
            ":w": result.max_wait_s,
        },
    )
    AuditRepository(deps.events, actor=ACTOR).append(
        incident_id, "INCIDENT_CLASSIFIED", result.as_dict(), now=now
    )
    log(
        LOGGER,
        logging.INFO,
        "Incident classified",
        incident_id=incident_id,
        classification=str(result.classification),
        source=result.source,
        matched_pattern=result.matched_pattern,
    )

    return {
        "incident_id": incident_id,
        "incident": {
            k: v
            for k, v in incident.model_dump(mode="json").items()
            if k
            in (
                "incident_id",
                "execution_arn",
                "organization",
                "process_type",
                "kind",
                "error",
                "cause",
                "deadline_iso",
                "remediation",
                "attempts",
            )  # fmt: skip
        },
        **result.as_dict(),
    }


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        catalog=dynamodb.Table(os.environ["ERROR_CATALOG_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
    )


def handler(event: dict, context: Any) -> dict:
    return classify_incident(event, _aws_deps())
