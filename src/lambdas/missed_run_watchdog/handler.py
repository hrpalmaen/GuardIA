"""MissedRunWatchdog (task 3.4 · requirements 3.1–3.3).

Every 5 minutes, for each process in `ProcessRegistry`:

* `MISSED_RUN`: a scheduled time (from the schedules DiscoverProcesses read)
  with no execution started within the 10-minute tolerance.
* `STUCK_RUN`: an execution in RUNNING for longer than twice the median of the
  last successful executions, or 50% of the periodicity without enough history.

Each new detection is published to EventBridge (`source = guardia.watchdog`);
the rule delivers it to OpenIncident, which opens the incident idempotently.
Detections whose incident already exists are not published again, so a stuck
execution produces one incident, not one per cycle.

After a cycle finishes without errors it emits the `GuardIA/Heartbeat` metric
(EMF, task 8.2 and decision D34). The heartbeat alarm in GuardIA-Observability
fires when the metric is missing for 10 minutes, so a stopped schedule, a
failing Lambda, or a lost permission is noticed even with no incidents.

`Deps.sfn` can be the real client or `guardia_local.stepfunctions.ReplayStepFunctionsClient`;
`Deps.bus` can be the real EventBridge client or `guardia_local.bus.LocalEventBus`.

Environment:
    PROCESS_REGISTRY_TABLE, INCIDENTS_TABLE
    WATCHDOG_EVENT_BUS (optional, default `default`), GUARDIA_SSM_PREFIX (optional)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from guardia_common.config import load_thresholds
from guardia_common.incidents import WATCHDOG_SOURCE, incident_id_for, parse_detection
from guardia_common.logger import get_logger, log
from guardia_common.metrics import emit_heartbeat
from guardia_common.models import ProcessRecord
from guardia_common.watchdog import (
    WatchdogSettings,
    check_window,
    is_stuck,
    missed_run_detection,
    missed_runs,
    stuck_run_detection,
)

LOGGER = get_logger("guardia.missed_run_watchdog")
DETAIL_TYPE = "GuardIA Watchdog Detection"
PAGE_SIZE = 100


class PublishError(RuntimeError):
    """EventBridge rejected a detection; the Lambda fails so it is retried."""


@dataclass
class Deps:
    registry: Any  # boto3 DynamoDB Table
    incidents: Any
    sfn: Any  # Step Functions client
    bus: Any  # EventBridge client (put_events)
    ssm: Any
    event_bus_name: str = "default"
    heartbeat: Callable[..., Any] = emit_heartbeat


def _registry(registry: Any) -> list[ProcessRecord]:
    rows, kwargs = [], {}
    while True:
        page = registry.scan(**kwargs)
        rows += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return [ProcessRecord.from_item(row) for row in rows]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _start_dates_since(sfn: Any, arn: str, since: datetime) -> list[datetime]:
    """Start dates of every execution started after `since` (newest first, paginated)."""
    dates: list[datetime] = []
    kwargs: dict[str, Any] = {"stateMachineArn": arn, "maxResults": PAGE_SIZE}
    while True:
        page = sfn.list_executions(**kwargs)
        for item in page.get("executions", []):
            if item["startDate"] < since:
                return dates
            dates.append(item["startDate"])
        if not page.get("nextToken"):
            return dates
        kwargs["nextToken"] = page["nextToken"]


def _running(sfn: Any, arn: str) -> list[dict]:
    items: list[dict] = []
    kwargs: dict[str, Any] = {"stateMachineArn": arn, "statusFilter": "RUNNING",
                              "maxResults": PAGE_SIZE}  # fmt: skip
    while True:
        page = sfn.list_executions(**kwargs)
        items += page.get("executions", [])
        if not page.get("nextToken"):
            return items
        kwargs["nextToken"] = page["nextToken"]


def _detections(process: ProcessRecord, deps: Deps, now: datetime, settings) -> list[dict]:
    found = []
    if process.schedules:
        start, _ = check_window(process, now, settings)
        starts = _start_dates_since(deps.sfn, process.resource_arn, start - settings.early_slack)
        found += [missed_run_detection(process, t) for t in missed_runs(process, starts, now,
                                                                        settings)]  # fmt: skip
    for execution in _running(deps.sfn, process.resource_arn):
        if is_stuck(process, execution["startDate"], now, settings):
            found.append(
                stuck_run_detection(process, execution["executionArn"], execution["startDate"])
            )
    return found


def _already_open(incidents: Any, detection: dict) -> bool:
    incident_id = incident_id_for(parse_detection(detection))
    return "Item" in incidents.get_item(
        Key={"incident_id": incident_id}, ProjectionExpression="incident_id"
    )


def _publish(deps: Deps, detections: list[dict], now: datetime) -> None:
    for start in range(0, len(detections), 10):  # PutEvents accepts up to 10 entries
        batch = detections[start : start + 10]
        response = deps.bus.put_events(
            Entries=[
                {
                    "Source": WATCHDOG_SOURCE,
                    "DetailType": DETAIL_TYPE,
                    "Detail": json.dumps(detection),
                    "Resources": [detection["resource_arn"]],
                    "EventBusName": deps.event_bus_name,
                    "Time": now,
                }
                for detection in batch
            ]
        )
        if response.get("FailedEntryCount"):
            raise PublishError(f"{response['FailedEntryCount']} detections were rejected")


def run_watchdog(deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    settings = WatchdogSettings.from_thresholds(load_thresholds(deps.ssm))

    new, already_open = [], 0
    processes = _registry(deps.registry)
    for process in processes:
        for detection in _detections(process, deps, now, settings):
            if _already_open(deps.incidents, detection):
                already_open += 1
            else:
                new.append(detection)

    _publish(deps, new, now)
    for detection in new:
        log(
            LOGGER,
            logging.WARNING,
            "Detection published",
            kind=detection["kind"],
            resource_arn=detection["resource_arn"],
            expected_at=detection.get("expected_at"),
            execution_arn=detection.get("execution_arn"),
        )
    log(LOGGER, logging.INFO, "Watchdog cycle finished", processes=len(processes),
        published=len(new), already_open=already_open)  # fmt: skip
    # Only reached when the whole cycle worked: an error above means no heartbeat.
    deps.heartbeat(now, properties={"processes": len(processes), "published": len(new)})
    return {"processes": len(processes), "published": new, "already_open": already_open}


@cache
def _aws_deps() -> Deps:
    dynamodb = boto3.resource("dynamodb")
    return Deps(
        registry=dynamodb.Table(os.environ["PROCESS_REGISTRY_TABLE"]),
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        sfn=boto3.client("stepfunctions"),
        bus=boto3.client("events"),
        ssm=boto3.client("ssm"),
        event_bus_name=os.environ.get("WATCHDOG_EVENT_BUS", "default"),
    )


def handler(event: dict, context: Any) -> dict:
    return run_watchdog(_aws_deps())
