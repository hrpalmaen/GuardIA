"""RemediationPolicy (task 5.1, 5.4 · requirements 5.1–5.6, 6.1).

A pure function with fixed rules: given the incident, the SSM thresholds, and
the current time, it decides whether GuardIA recovers the execution on its
own, escalates to a person, or does both at once. It is the only place that
decides whether GuardIA acts on a process; the AI agent (T8) can only feed it
a suggestion, never bypass it.

Rules, in order; the first one that applies wins:

 1. `MISSED_RUN` or `STUCK_RUN`                     -> ESCALATE  (4.4, D22)
 2. classification `unknown` (no agent yet, D16)    -> ESCALATE
 3. classification `requires_escalation`            -> ESCALATE
 4. the process is not `remediation=auto`           -> ESCALATE  (5.6)
 5. threshold `remediation-enabled` is not "true"   -> ESCALATE  (observe-only)
 6. attempts >= `remediation-max-attempts` (2)      -> ESCALATE  (5.5)
 7. `remediation-window-minutes` (20) since opening -> ESCALATE  (5.5)
 8. the wait needed > `max-wait-seconds` (600)      -> ESCALATE  (5.3)
 9. minutes left <= `parallel-escalation-minutes-left` (30)
                                                    -> REMEDIATE_AND_ESCALATE (6.1)
10. otherwise                                       -> REMEDIATE (5.1)

The wait (5.2) is the `Retry-After` found in the cause for `wait_and_redrive`
errors (429), or `DEFAULT_RETRY_AFTER_S` when the cause does not say (D26);
plain `redrive` errors do not wait.

`escalation_running` is set when the policy is asked again inside the parallel
branch, after a failed attempt: escalation is already under way, so
REMEDIATE_AND_ESCALATE becomes REMEDIATE and ESCALATE means "stop trying".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from .models import CatalogAction, Classification, Incident, IncidentKind
from .schedule import minutes_until, parse_timestamp

DEFAULT_RETRY_AFTER_S = 60  # D26: a 429 that does not say how long to wait

_RETRY_AFTER = re.compile(
    r"retry[-_ ]?after[\"']?\s*[:=]?\s*[\"']?\s*(\d{1,6})(?:\s*(ms|milliseconds?|s|sec|seconds?))?",
    re.IGNORECASE,
)


class Decision(StrEnum):
    REMEDIATE = "REMEDIATE"
    REMEDIATE_AND_ESCALATE = "REMEDIATE_AND_ESCALATE"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    wait_seconds: int
    minutes_left: int

    def as_dict(self) -> dict:
        data = asdict(self)
        data["decision"] = self.decision.value
        return data


@dataclass(frozen=True)
class PolicyLimits:
    remediation_enabled: bool
    max_attempts: int
    window_minutes: int
    max_wait_seconds: int
    parallel_minutes_left: int

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, str]) -> PolicyLimits:
        return cls(
            remediation_enabled=str(thresholds["remediation-enabled"]).strip().lower() == "true",
            max_attempts=int(thresholds["remediation-max-attempts"]),
            window_minutes=int(thresholds["remediation-window-minutes"]),
            max_wait_seconds=int(thresholds["max-wait-seconds"]),
            parallel_minutes_left=int(thresholds["parallel-escalation-minutes-left"]),
        )


def retry_after_seconds(*texts: str | None) -> int | None:
    """The wait indicated in an error cause (`Retry-After: 120`, `"retryAfter": 120`,
    `retry after 1500 ms`), in whole seconds rounded up; None if no text says it."""
    for text in texts:
        if not text:
            continue
        match = _RETRY_AFTER.search(text)
        if match:
            value = int(match.group(1))
            unit = (match.group(2) or "s").lower()
            return -(-value // 1000) if unit.startswith("m") else value
    return None


def wait_needed(incident: Incident) -> int:
    if incident.catalog_action != CatalogAction.WAIT_AND_REDRIVE:
        return 0
    indicated = retry_after_seconds(incident.root_cause, incident.cause)
    return DEFAULT_RETRY_AFTER_S if indicated is None else indicated


def decide(
    incident: Incident,
    thresholds: Mapping[str, str],
    now: datetime,
    *,
    escalation_running: bool = False,
) -> PolicyDecision:
    limits = PolicyLimits.from_thresholds(thresholds)
    minutes_left = minutes_until(parse_timestamp(incident.deadline_iso), now)
    decision, reason, wait = _rules(incident, limits, now, minutes_left)

    if escalation_running and decision == Decision.REMEDIATE_AND_ESCALATE:
        decision = Decision.REMEDIATE
    return PolicyDecision(decision, reason, wait, minutes_left)


def _rules(
    incident: Incident, limits: PolicyLimits, now: datetime, minutes_left: int
) -> tuple[Decision, str, int]:
    escalate = Decision.ESCALATE
    if incident.kind in (IncidentKind.MISSED_RUN, IncidentKind.STUCK_RUN):
        return escalate, "watchdog-detection", 0
    if incident.classification in (None, Classification.UNKNOWN):
        return escalate, "unknown-no-agent", 0
    if incident.classification == Classification.REQUIRES_ESCALATION:
        return escalate, "catalog-requires-escalation", 0
    if incident.remediation != "auto":
        return escalate, "process-escalate-only", 0
    if not limits.remediation_enabled:
        return escalate, "observe-only", 0
    if incident.attempts >= limits.max_attempts:
        return escalate, "attempts-exhausted", 0
    opened = parse_timestamp(incident.opened_at)
    if opened and (now - opened).total_seconds() >= limits.window_minutes * 60:
        return escalate, "remediation-window-exceeded", 0
    wait = wait_needed(incident)
    if wait > limits.max_wait_seconds:
        return escalate, "wait-too-long", 0
    if minutes_left <= limits.parallel_minutes_left:
        return Decision.REMEDIATE_AND_ESCALATE, "low-time", wait
    return Decision.REMEDIATE, "recoverable", wait
