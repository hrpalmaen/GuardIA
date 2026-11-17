"""DiscoverProcesses (task 2.1 · requirements 1.1–1.4, and the inputs of 3.1–3.3).

Every 15 minutes:

1. Lists the state machines tagged `guardia:monitor=true` (Resource Groups
   Tagging API) and validates the other three `guardia:*` tags.
2. Reads the schedules that launch each one: EventBridge rules that target it
   (cron in UTC) and EventBridge Scheduler schedules (cron in their own zone).
   The process owners add nothing for this; the schedule is whatever really
   launches the process.
3. Computes the median duration of the last successful executions.
4. Updates `ProcessRegistry` and removes the rows of processes that are no
   longer tagged or no longer valid.

Fields an operator sets by hand (`timezone`, `deadline_local`) are kept.

Environment:
    PROCESS_REGISTRY_TABLE, GUARDIA_SSM_PREFIX (optional)
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

from guardia_common.config import get_threshold, load_thresholds
from guardia_common.discovery import (
    MONITOR_TAG,
    STATE_MACHINE_RESOURCE_TYPE,
    ConfigWarning,
    TaggedProcess,
    duration_seconds,
    tags_to_dict,
    validate_tags,
)
from guardia_common.logger import get_logger, log
from guardia_common.models import ScheduleRef, to_dynamo
from guardia_common.schedule import (
    Schedule,
    UnsupportedScheduleError,
    median_duration_s,
    to_local_iso,
)

LOGGER = get_logger("guardia.discover_processes")


@dataclass
class Deps:
    registry: Any  # boto3 DynamoDB Table
    tagging: Any  # resourcegroupstaggingapi client
    events: Any  # EventBridge client (rules)
    scheduler: Any  # EventBridge Scheduler client
    sfn: Any  # Step Functions client
    ssm: Any


# --------------------------------------------------------------------------- AWS reads
def _tagged_resources(tagging: Any) -> list[tuple[str, dict[str, str]]]:
    paginator = tagging.get_paginator("get_resources")
    pages = paginator.paginate(
        TagFilters=[{"Key": MONITOR_TAG, "Values": ["true"]}],
        ResourceTypeFilters=[STATE_MACHINE_RESOURCE_TYPE],
    )
    return [
        (item["ResourceARN"], tags_to_dict(item.get("Tags", [])))
        for page in pages
        for item in page["ResourceTagMappingList"]
    ]


def _rule_schedules(events: Any, target_arn: str) -> list[ScheduleRef]:
    names: list[str] = []
    kwargs: dict[str, Any] = {"TargetArn": target_arn}
    while True:
        page = events.list_rule_names_by_target(**kwargs)
        names += page.get("RuleNames", [])
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]

    schedules = []
    for name in names:
        rule = events.describe_rule(Name=name)
        if rule.get("State") == "ENABLED" and rule.get("ScheduleExpression"):
            schedules.append(
                ScheduleRef(
                    expression=rule["ScheduleExpression"],
                    timezone="UTC",
                    source="events-rule",
                    name=name,
                )
            )
    return schedules


def _scheduler_schedules(scheduler: Any, targets: set[str]) -> dict[str, list[ScheduleRef]]:
    """Scheduler cannot filter by target, so one listing serves every process."""
    found: dict[str, list[ScheduleRef]] = {}
    for page in scheduler.get_paginator("list_schedules").paginate(State="ENABLED"):
        for summary in page.get("Schedules", []):
            target = (summary.get("Target") or {}).get("Arn")
            if target not in targets:
                continue
            detail = scheduler.get_schedule(
                Name=summary["Name"], GroupName=summary.get("GroupName", "default")
            )
            found.setdefault(target, []).append(
                ScheduleRef(
                    expression=detail["ScheduleExpression"],
                    timezone=detail.get("ScheduleExpressionTimezone") or "UTC",
                    source="scheduler",
                    name=f"{summary.get('GroupName', 'default')}/{summary['Name']}",
                )
            )
    return found


def _recent_durations(sfn: Any, state_machine_arn: str, window: int) -> list[float]:
    page = sfn.list_executions(
        stateMachineArn=state_machine_arn, statusFilter="SUCCEEDED", maxResults=window
    )
    durations = (duration_seconds(item) for item in page.get("executions", []))
    return [d for d in durations if d is not None][:window]


# --------------------------------------------------------------------------- registry
def _write(
    registry: Any,
    process: TaggedProcess,
    schedules: list[ScheduleRef],
    median_s: float | None,
    samples: int,
    seen_iso: str,
) -> None:
    values = {
        ":process_type": process.process_type,
        ":organization": process.organization,
        ":remediation": process.remediation,
        ":schedules": [s.model_dump() for s in schedules],
        ":samples": samples,
        ":seen": seen_iso,
    }
    sets = [
        "process_type = :process_type",
        "organization = :organization",
        "remediation = :remediation",
        "schedules = :schedules",
        "duration_samples = :samples",
        "last_seen_iso = :seen",
        "first_seen_iso = if_not_exists(first_seen_iso, :seen)",
    ]
    if median_s is not None:
        sets.append("median_duration_s = :median")
        values[":median"] = median_s
    expression = "SET " + ", ".join(sets)
    if median_s is None:
        expression += " REMOVE median_duration_s"
    registry.update_item(
        Key={"resource_arn": process.resource_arn},
        UpdateExpression=expression,
        ExpressionAttributeValues=to_dynamo(values),
    )


def _remove_stale(registry: Any, keep: set[str]) -> list[str]:
    removed = []
    kwargs: dict[str, Any] = {"ProjectionExpression": "resource_arn"}
    while True:
        page = registry.scan(**kwargs)
        for item in page.get("Items", []):
            if item["resource_arn"] not in keep:
                registry.delete_item(Key={"resource_arn": item["resource_arn"]})
                removed.append(item["resource_arn"])
        if "LastEvaluatedKey" not in page:
            return removed
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _check_schedules(arn: str, schedules: list[ScheduleRef], now: datetime) -> list[ConfigWarning]:
    if not schedules:
        return [
            ConfigWarning(arn, "no_schedule", "no enabled rule or schedule; MISSED_RUN disabled")
        ]
    warnings = []
    for ref in schedules:
        try:
            schedule = Schedule(ref.expression, ref.timezone)
            if not schedule.is_cron:
                warnings.append(
                    ConfigWarning(arn, "rate_schedule", f"{ref.name}: rate() has no fixed times")
                )
            schedule.period_at(now)
        except UnsupportedScheduleError as exc:
            warnings.append(ConfigWarning(arn, "unsupported_schedule", f"{ref.name}: {exc}"))
    return warnings


# --------------------------------------------------------------------------- entry point
def discover_processes(deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    thresholds = load_thresholds(deps.ssm)
    window = int(get_threshold(thresholds, "median-window-executions"))
    min_samples = int(get_threshold(thresholds, "median-min-executions"))
    seen_iso = to_local_iso(now, get_threshold(thresholds, "timezone"))

    valid: list[TaggedProcess] = []
    warnings: list[ConfigWarning] = []
    for arn, tags in _tagged_resources(deps.tagging):
        process, warning = validate_tags(arn, tags)
        if warning:
            warnings.append(warning)
        if process:
            valid.append(process)

    try:
        from_scheduler = _scheduler_schedules(deps.scheduler, {p.resource_arn for p in valid})
    except ClientError as err:  # Scheduler not used or not allowed: rules still work
        log(LOGGER, logging.WARNING, "Scheduler listing failed",
            error_code=err.response["Error"]["Code"])  # fmt: skip
        from_scheduler = {}

    monitored = []
    for process in valid:
        arn = process.resource_arn
        schedules = _rule_schedules(deps.events, arn) + from_scheduler.get(arn, [])
        warnings += _check_schedules(arn, schedules, now)
        durations = _recent_durations(deps.sfn, arn, window)
        median_s = median_duration_s(durations, min_samples=min_samples)
        _write(deps.registry, process, schedules, median_s, len(durations), seen_iso)
        monitored.append(
            {
                "resource_arn": arn,
                "organization": process.organization,
                "process_type": process.process_type,
                "remediation": process.remediation,
                "schedules": [s.expression for s in schedules],
                "median_duration_s": median_s,
                "duration_samples": len(durations),
            }
        )

    removed = _remove_stale(deps.registry, {p.resource_arn for p in valid})

    for warning in warnings:
        log(LOGGER, logging.WARNING, "Process configuration warning", **warning.as_dict())
    log(
        LOGGER,
        logging.INFO,
        "Discovery finished",
        monitored=len(monitored),
        warnings=len(warnings),
        removed=len(removed),
    )
    return {
        "monitored": monitored,
        "warnings": [w.as_dict() for w in warnings],
        "removed": removed,
    }


@cache
def _aws_deps() -> Deps:
    return Deps(
        registry=boto3.resource("dynamodb").Table(os.environ["PROCESS_REGISTRY_TABLE"]),
        tagging=boto3.client("resourcegroupstaggingapi"),
        events=boto3.client("events"),
        scheduler=boto3.client("scheduler"),
        sfn=boto3.client("stepfunctions"),
        ssm=boto3.client("ssm"),
    )


def handler(event: dict, context: Any) -> dict:
    return discover_processes(_aws_deps())
