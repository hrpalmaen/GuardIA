"""In-memory EventBridge bus: drop-in for `boto3.client("events").put_events`.

Rules are registered with an EventBridge event pattern and a target callable;
`put_events` builds the event envelope AWS delivers and calls every matching
target synchronously. Requests are validated against the botocore model.

Only the pattern features GuardIA uses are supported: exact values in lists,
nested objects, and `{"exists": bool}`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import botocore.session
from botocore.exceptions import ParamValidationError
from botocore.validate import ParamValidator

_MODEL = botocore.session.get_session().get_service_model("events")
_VALIDATOR = ParamValidator()


def matches(pattern: dict, event: dict) -> bool:
    for key, expected in pattern.items():
        present = key in event
        value = event.get(key)
        if isinstance(expected, dict):
            if not isinstance(value, dict) or not matches(expected, value):
                return False
            continue
        for option in expected:
            if isinstance(option, dict) and "exists" in option:
                if option["exists"] == present:
                    break
            elif present and value == option:
                break
        else:
            return False
    return True


@dataclass
class Rule:
    name: str
    pattern: dict
    target: Callable[[dict], Any]


@dataclass
class Delivery:
    rule: str
    event: dict
    result: Any = None
    error: str | None = None


@dataclass
class LocalEventBus:
    account: str = "123456789012"
    region: str = "us-east-1"
    clock: Callable[[], datetime] | None = None
    rules: list[Rule] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    # True in tests: a failing target raises instead of being recorded.
    raise_errors: bool = False

    def put_rule(self, name: str, pattern: dict, target: Callable[[dict], Any]) -> None:
        self.rules.append(Rule(name, pattern, target))

    def _envelope(self, entry: dict) -> dict:
        moment = entry.get("Time") or (self.clock() if self.clock else datetime.now(UTC))
        return {
            "version": "0",
            "id": str(uuid.uuid4()),
            "detail-type": entry["DetailType"],
            "source": entry["Source"],
            "account": self.account,
            "time": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": self.region,
            "resources": entry.get("Resources", []),
            "detail": json.loads(entry["Detail"]),
        }

    def put_events(self, **params: Any) -> dict:
        report = _VALIDATOR.validate(params, _MODEL.operation_model("PutEvents").input_shape)
        if report.has_errors():
            raise ParamValidationError(report=report.generate_report())
        results = []
        for entry in params["Entries"]:
            event = self._envelope(entry)
            self.emit(event)
            results.append({"EventId": event["id"]})
        return {"FailedEntryCount": 0, "Entries": results}

    def emit(self, event: dict) -> None:
        """Delivers an already built event (e.g. a Step Functions status change)."""
        for rule in self.rules:
            if not matches(rule.pattern, event):
                continue
            delivery = Delivery(rule.name, event)
            try:
                delivery.result = rule.target(event)
            except Exception as exc:  # EventBridge would retry and then use the DLQ
                if self.raise_errors:
                    raise
                delivery.error = f"{type(exc).__name__}: {exc}"
            self.deliveries.append(delivery)
