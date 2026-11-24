"""What the model is told about the incident: a short, sanitized, allow-listed view.

Never included: the execution input (it can hold customer data), ARNs (they
carry the account number), the Teams thread, or the task token.
"""

from __future__ import annotations

from typing import Any

from guardia_common.models import from_dynamo
from guardia_common.redaction import redact_value, sanitize_cause, truncate

MAX_FIELD_CHARS = 1500
_PLAIN_FIELDS = (
    "organization",
    "process_type",
    "kind",
    "error",
    "root_error",
    "failed_state",
    "remediation",
    "attempts",
    "classification",
    "catalog_action",
    "status",
    "opened_at",
    "deadline_iso",
)


def process_name(resource_arn: str | None) -> str | None:
    """`...:stateMachine:genesys-master-orgA` -> `genesys-master-orgA`."""
    return resource_arn.rsplit(":", 1)[-1] if resource_arn else None


def incident_context(item: dict) -> dict[str, Any]:
    item = from_dynamo(item)
    context: dict[str, Any] = {key: item.get(key) for key in _PLAIN_FIELDS if item.get(key)}
    context["process_name"] = process_name(item.get("resource_arn"))
    for key in ("cause", "root_cause"):
        text = sanitize_cause(item.get(key), MAX_FIELD_CHARS)
        if text:
            context[key] = text
    redacted = redact_value(context)
    return {
        key: truncate(value, MAX_FIELD_CHARS) if isinstance(value, str) else value
        for key, value in redacted.items()
        if value is not None
    }
