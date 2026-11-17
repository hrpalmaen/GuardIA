"""Process discovery rules (requirement 1): validate the `guardia:*` tags.

Pure functions; `DiscoverProcesses` makes the AWS calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import normalize_remediation

MONITOR_TAG = "guardia:monitor"
PROCESS_TYPE_TAG = "guardia:process-type"
ORGANIZATION_TAG = "guardia:organization"
REMEDIATION_TAG = "guardia:remediation"
REQUIRED_TAGS = (PROCESS_TYPE_TAG, ORGANIZATION_TAG, REMEDIATION_TAG)
STATE_MACHINE_RESOURCE_TYPE = "states:stateMachine"


@dataclass(frozen=True)
class TaggedProcess:
    resource_arn: str
    process_type: str
    organization: str
    remediation: str
    remediation_was_invalid: bool


@dataclass(frozen=True)
class ConfigWarning:
    resource_arn: str
    code: str  # missing_tags, invalid_remediation, no_schedule, unsupported_schedule
    detail: str

    def as_dict(self) -> dict:
        return {"resource_arn": self.resource_arn, "code": self.code, "detail": self.detail}


def tags_to_dict(tags: list[dict]) -> dict[str, str]:
    """Tagging API format `[{"Key", "Value"}]` → dict."""
    return {tag["Key"]: tag.get("Value", "") for tag in tags}


def validate_tags(
    resource_arn: str, tags: dict[str, str]
) -> tuple[TaggedProcess | None, ConfigWarning | None]:
    """1.2: a resource missing any of the three mandatory tags is excluded with a
    warning. 1.4: an invalid `guardia:remediation` is kept as `escalate-only`,
    also with a warning so the owner can fix it."""
    missing = [name for name in REQUIRED_TAGS if not tags.get(name, "").strip()]
    if missing:
        return None, ConfigWarning(resource_arn, "missing_tags", ", ".join(missing))

    raw = tags[REMEDIATION_TAG].strip()
    remediation = normalize_remediation(raw)
    process = TaggedProcess(
        resource_arn=resource_arn,
        process_type=tags[PROCESS_TYPE_TAG].strip(),
        organization=tags[ORGANIZATION_TAG].strip(),
        remediation=remediation,
        remediation_was_invalid=remediation != raw,
    )
    warning = (
        ConfigWarning(resource_arn, "invalid_remediation", f"{raw!r} treated as escalate-only")
        if process.remediation_was_invalid
        else None
    )
    return process, warning


def duration_seconds(execution: dict) -> float | None:
    """Duration of a finished execution from a ListExecutions item."""
    start, stop = execution.get("startDate"), execution.get("stopDate")
    if not isinstance(start, datetime) or not isinstance(stop, datetime):
        return None
    return (stop - start).total_seconds()
