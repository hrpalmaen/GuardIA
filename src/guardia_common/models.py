"""Pydantic models shared across Lambdas (task 1.3).

Included here: the models needed by OpenIncident and ClassifyIncident.
The agent's own contracts (`AgentDecision`) live in `guardia_common.agent.models`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IncidentKind(StrEnum):
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ABORTED = "ABORTED"
    MISSED_RUN = "MISSED_RUN"
    STUCK_RUN = "STUCK_RUN"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    REMEDIATING = "REMEDIATING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    SLA_BREACHED = "SLA_BREACHED"


class Classification(StrEnum):
    AUTO_RECOVERABLE = "auto_recoverable"
    REQUIRES_ESCALATION = "requires_escalation"
    UNKNOWN = "unknown"


class CatalogAction(StrEnum):
    WAIT_AND_REDRIVE = "wait_and_redrive"
    REDRIVE = "redrive"
    ESCALATE = "escalate"


Remediation = Literal["auto", "escalate-only"]


def normalize_remediation(value: Any) -> str:
    """Requirement 1.4: anything other than `auto` or `escalate-only` is `escalate-only`."""
    return value if value in ("auto", "escalate-only") else "escalate-only"


# --------------------------------------------------------------------------- #
# DynamoDB helpers: DynamoDB rejects float and returns Decimal.
# --------------------------------------------------------------------------- #
def to_dynamo(data: dict) -> dict:
    clean = {k: v for k, v in data.items() if v is not None}
    return json.loads(json.dumps(clean, default=str), parse_float=Decimal)


def from_dynamo(value: Any) -> Any:
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class _Item(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    def to_item(self) -> dict:
        return to_dynamo(self.model_dump(mode="json"))

    @classmethod
    def from_item(cls, item: dict):
        return cls.model_validate(from_dynamo(item))


class ScheduleRef(BaseModel):
    """A trigger that launches the process, read from AWS by DiscoverProcesses.

    `source` is `events-rule` (EventBridge rule, always UTC) or `scheduler`
    (EventBridge Scheduler, with its own time zone)."""

    model_config = ConfigDict(extra="ignore")

    expression: str
    timezone: str = "UTC"
    source: Literal["events-rule", "scheduler"]
    name: str


class ProcessRecord(_Item):
    """`ProcessRegistry` row (PK resource_arn), written by DiscoverProcesses.

    `timezone` and `deadline_local` define the SLA deadline (business time);
    each schedule carries the zone its expression is evaluated in.
    """

    resource_arn: str
    process_type: str
    organization: str
    remediation: Remediation = "escalate-only"
    schedules: list[ScheduleRef] = Field(default_factory=list)
    timezone: str | None = None
    deadline_local: str | None = None
    median_duration_s: float | None = None
    duration_samples: int = 0
    first_seen_iso: str | None = None
    last_seen_iso: str | None = None

    @field_validator("remediation", mode="before")
    @classmethod
    def _normalize_remediation(cls, value: Any) -> str:
        return normalize_remediation(value)


class Incident(_Item):
    """`Incidents` row. The first block is the incident event contract (design §3)."""

    incident_id: str
    execution_arn: str | None = None
    organization: str
    process_type: str
    kind: IncidentKind
    error: str | None = None
    cause: str | None = None
    deadline_iso: str
    remediation: Remediation
    # Root cause from the execution history (guardia_common.root_cause); the
    # final error often hides it. All causes are stored sanitized.
    root_error: str | None = None
    root_cause: str | None = None
    failed_state: str | None = None

    resource_arn: str
    status: IncidentStatus = IncidentStatus.OPEN
    attempts: int = Field(default=0, ge=0, le=2)
    opened_at: str
    started_at: str | None = None
    stopped_at: str | None = None
    expected_at: str | None = None
    execution_input: str | None = None
    classification: Classification | None = None
    catalog_action: CatalogAction | None = None
    catalog_max_wait_s: int | None = None
    flow_execution_arn: str | None = None
    recovery_execution_arn: str | None = None  # RedriveOrRestart, last attempt
    # Written by the flow (UpdateIncidentStatus, escalation, ResolveOnSuccess).
    # Declared here because extra fields are dropped on read (extra="ignore").
    updated_at: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    teams_thread_id: str | None = None
    task_token: str | None = None
    # Written by InvokeAgent (T8). The agent only recommends: RemediationPolicy
    # decides whether to use `agent_*` as if it were the catalog (rule 2).
    agent_classification: Literal["auto_recoverable", "requires_escalation"] | None = None
    agent_action: CatalogAction | None = None
    agent_confidence: float | None = Field(default=None, ge=0, le=1)
    agent_wait_seconds: int | None = Field(default=None, ge=0)
    agent_summary: str | None = None  # diagnosis in Spanish for the Teams card
    agent_failure: str | None = None  # why the agent gave no usable answer

    @field_validator("remediation", mode="before")
    @classmethod
    def _normalize_remediation(cls, value: Any) -> str:
        return normalize_remediation(value)


class IncidentEvent(_Item):
    """`IncidentEvents` row (PK incident_id, SK timestamp#seq). Append-only."""

    incident_id: str
    sk: str
    event_type: str
    actor: str
    timestamp: str
    detail: dict = Field(default_factory=dict)
    expires_at: int


class CatalogEntry(_Item):
    """`ErrorCatalog` row (PK process_type, SK error_pattern).

    `error_pattern` is a case-insensitive regular expression searched in the
    incident's kind, error, and cause. `process_type = "*"` applies to every
    process type; a row for the specific process type takes precedence.
    """

    process_type: str
    error_pattern: str
    classification: Literal["auto_recoverable", "requires_escalation"]
    action: CatalogAction
    max_wait_s: int = Field(default=0, ge=0)
    description: str | None = None

    @field_validator("error_pattern")
    @classmethod
    def _valid_regex(cls, value: str) -> str:
        import re

        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value
