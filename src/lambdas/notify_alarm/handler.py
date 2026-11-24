"""NotifyAlarm (task 8.2 · requirement 13.2).

Subscribed to the `guardia-alarms` SNS topic next to the team's email
subscription. When a GuardIA alarm goes into ALARM it posts a card to the
Teams alerts channel. This path does not go through GuardIA-Incident-Flow, so
it still works when the flow is the thing that failed; and when Teams is the
thing that failed, the SNS email still arrives.

Each invocation posts once. If Teams rejects the post the Lambda raises, and
the asynchronous invocation is retried twice by Lambda (3 attempts in total,
as in 7.3) before the event goes to the DLQ. It has no `Errors` alarm of its
own on purpose: that alarm would notify this same function in a loop while
Teams is down (decision D34).

Input: the SNS event. Each record's `Message` is the CloudWatch alarm JSON.
Only `ALARM` states are posted; `OK` and other messages are ignored.
Output: {"posted": int, "skipped": int}.

Environment:
    TEAMS_ALERTS_SECRET_ID
    GUARDIA_TIMEZONE (optional, default America/Bogota; only for the card time)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3

from guardia_common.logger import get_logger, log
from guardia_common.schedule import get_tz, parse_timestamp
from guardia_common.teams_client import (
    TeamsPostError,
    build_alarm_card,
    get_webhook_url,
    post_card,
)

LOGGER = get_logger("guardia.notify_alarm")
DEFAULT_TIMEZONE = "America/Bogota"


class AlarmNotificationError(RuntimeError):
    """Teams did not accept the alarm card. Never contains the webhook address."""


@dataclass
class Deps:
    secrets: Any  # boto3 Secrets Manager client
    secret_id: str
    poster: Callable[[str, dict], Any] = post_card
    timezone: str = DEFAULT_TIMEZONE


def _alarms(event: dict) -> list[dict]:
    """The CloudWatch alarm messages in an SNS event; other messages are dropped."""
    found = []
    for record in event.get("Records", []):
        message = (record.get("Sns") or {}).get("Message", "")
        try:
            alarm = json.loads(message)
        except (TypeError, ValueError):
            log(LOGGER, logging.WARNING, "SNS message is not an alarm; ignored")
            continue
        if isinstance(alarm, dict) and alarm.get("AlarmName"):
            found.append(alarm)
        else:
            log(LOGGER, logging.WARNING, "SNS message is not an alarm; ignored")
    return found


def _time_label(alarm: dict, tz_name: str) -> str | None:
    raw = alarm.get("StateChangeTime")
    if not raw:
        return None
    try:
        # CloudWatch sends "2026-11-24T10:15:00.000+0000"; fromisoformat wants "+00:00".
        text = str(raw)
        if len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit():
            text = f"{text[:-2]}:{text[-2:]}"
        moment = parse_timestamp(text)
    except ValueError:
        return None
    return moment.astimezone(get_tz(tz_name)).strftime("%Y-%m-%d %H:%M (hora Colombia)")


def notify_alarm(event: dict, deps: Deps) -> dict:
    alarms = _alarms(event)
    to_post = [a for a in alarms if a.get("NewStateValue") == "ALARM"]
    skipped = len(event.get("Records", [])) - len(to_post)
    if not to_post:
        return {"posted": 0, "skipped": skipped}

    webhook_url = get_webhook_url(deps.secrets, deps.secret_id)
    for alarm in to_post:
        card = build_alarm_card(alarm, time_label=_time_label(alarm, deps.timezone))
        try:
            deps.poster(webhook_url, card)
        except TeamsPostError as err:
            log(LOGGER, logging.ERROR, "Teams rejected the alarm card",
                alarm=alarm.get("AlarmName"), status=err.status)  # fmt: skip
            raise AlarmNotificationError(
                f"Teams did not accept the card for alarm {alarm.get('AlarmName')}"
            ) from None
        log(LOGGER, logging.INFO, "Alarm posted to Teams", alarm=alarm.get("AlarmName"))
    return {"posted": len(to_post), "skipped": skipped}


@cache
def _aws_deps() -> Deps:
    return Deps(
        secrets=boto3.client("secretsmanager"),
        secret_id=os.environ["TEAMS_ALERTS_SECRET_ID"],
        timezone=os.environ.get("GUARDIA_TIMEZONE", DEFAULT_TIMEZONE),
    )


def handler(event: dict, context: Any) -> dict:
    return notify_alarm(event, _aws_deps())
