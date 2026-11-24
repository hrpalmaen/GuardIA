"""RemediationPolicy (task 5.1, 5.4 · requirements 5.1–5.6, 6.1).

A pure function with fixed rules: given the incident, the SSM thresholds, and
the current time, it decides whether GuardIA recovers the execution on its
own, escalates to a person, or does both at once. It is the only place that
decides whether GuardIA acts on a process; the AI agent (T8) can only feed it
a suggestion for an incident the catalog does not know, and rules 4 to 10 still
apply to that suggestion (tag, attempts, window, wait, observe-only, low time).

Rules, in order; the first one that applies wins:

 1. `MISSED_RUN` or `STUCK_RUN`                     -> ESCALATE  (4.4, D22)
 2. classification `unknown`                          -> ESCALATE  (`unknown-no-agent`,
    unless the agent (T8, D24) answered with `agent-min-confidence` or more: then
    its classification and action are used as if the catalog had said them;
    a failed agent run escalates with the reason `agent-<reason>`)
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
DEFAULT_AGENT_MIN_CONFIDENCE = 0.7
SOURCE_CATALOG = "catalog"
SOURCE_AGENT = "agent"

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
    source: str = SOURCE_CATALOG  # "agent" when rule 2 used the agent's recommendation

    def as_dict(self) -> dict:
        data = asdict(self)
        data["decision"] = self.decision.value
        if self.source == SOURCE_CATALOG:
            del data["source"]  # only worth recording when the agent was used
        return data


@dataclass(frozen=True)
class PolicyLimits:
    remediation_enabled: bool
    max_attempts: int
    window_minutes: int
    max_wait_seconds: int
    parallel_minutes_left: int
    agent_min_confidence: float = DEFAULT_AGENT_MIN_CONFIDENCE
    default_retry_after_seconds: int = DEFAULT_RETRY_AFTER_S

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, str]) -> PolicyLimits:
        return cls(
            remediation_enabled=str(thresholds["remediation-enabled"]).strip().lower() == "true",
            max_attempts=int(thresholds["remediation-max-attempts"]),
            window_minutes=int(thresholds["remediation-window-minutes"]),
            max_wait_seconds=int(thresholds["max-wait-seconds"]),
            parallel_minutes_left=int(thresholds["parallel-escalation-minutes-left"]),
            agent_min_confidence=float(
                thresholds.get("agent-min-confidence", DEFAULT_AGENT_MIN_CONFIDENCE)
            ),
            default_retry_after_seconds=int(
                thresholds.get("default-retry-after-seconds", DEFAULT_RETRY_AFTER_S)
            ),
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


def wait_needed(
    incident: Incident,
    action: str | None = None,
    agent_wait: int = 0,
    default_wait: int = DEFAULT_RETRY_AFTER_S,
) -> int:
    """Seconds to wait before the redrive. `action` is the catalog action unless the
    agent's recommendation is being used (then `agent_wait` is its own suggestion)."""
    action = action or incident.catalog_action
    if action != CatalogAction.WAIT_AND_REDRIVE:
        return 0
    indicated = retry_after_seconds(incident.root_cause, incident.cause)
    if indicated is not None:
        return indicated
    return agent_wait or default_wait


def decide(
    incident: Incident,
    thresholds: Mapping[str, str],
    now: datetime,
    *,
    escalation_running: bool = False,
) -> PolicyDecision:
    limits = PolicyLimits.from_thresholds(thresholds)
    minutes_left = minutes_until(parse_timestamp(incident.deadline_iso), now)
    decision, reason, wait, source = _rules(incident, limits, now, minutes_left)

    if escalation_running and decision == Decision.REMEDIATE_AND_ESCALATE:
        decision = Decision.REMEDIATE
    return PolicyDecision(decision, reason, wait, minutes_left, source)


def _known(
    incident: Incident, limits: PolicyLimits
) -> tuple[Classification | None, str | None, str, str]:
    """(classification, action, source, reason). The classification is None when
    GuardIA does not know what to do: then `reason` says why (rule 2)."""
    if incident.classification not in (None, Classification.UNKNOWN):
        return Classification(incident.classification), incident.catalog_action, SOURCE_CATALOG, ""
    if incident.agent_classification and incident.agent_action:
        confidence = incident.agent_confidence or 0.0
        if confidence >= limits.agent_min_confidence:
            return (
                Classification(incident.agent_classification),
                incident.agent_action,
                SOURCE_AGENT,
                "",
            )
        return None, None, SOURCE_CATALOG, "agent-low_confidence"
    if incident.agent_failure:
        return None, None, SOURCE_CATALOG, f"agent-{incident.agent_failure}"
    return None, None, SOURCE_CATALOG, "unknown-no-agent"


def _rules(
    incident: Incident, limits: PolicyLimits, now: datetime, minutes_left: int
) -> tuple[Decision, str, int, str]:
    escalate = Decision.ESCALATE
    if incident.kind in (IncidentKind.MISSED_RUN, IncidentKind.STUCK_RUN):
        return escalate, "watchdog-detection", 0, SOURCE_CATALOG
    classification, action, source, unknown_reason = _known(incident, limits)
    if classification is None:
        return escalate, unknown_reason, 0, SOURCE_CATALOG
    if classification == Classification.REQUIRES_ESCALATION:
        reason = (
            "catalog-requires-escalation"
            if source == SOURCE_CATALOG
            else "agent-requires-escalation"
        )
        return escalate, reason, 0, source
    if source == SOURCE_AGENT and action == CatalogAction.ESCALATE:
        return escalate, "agent-suggests-escalation", 0, source
    if incident.remediation != "auto":
        return escalate, "process-escalate-only", 0, source
    if not limits.remediation_enabled:
        return escalate, "observe-only", 0, source
    if incident.attempts >= limits.max_attempts:
        return escalate, "attempts-exhausted", 0, source
    opened = parse_timestamp(incident.opened_at)
    if opened and (now - opened).total_seconds() >= limits.window_minutes * 60:
        return escalate, "remediation-window-exceeded", 0, source
    agent_wait = (incident.agent_wait_seconds or 0) if source == SOURCE_AGENT else 0
    wait = wait_needed(incident, action, agent_wait, limits.default_retry_after_seconds)
    if wait > limits.max_wait_seconds:
        return escalate, "wait-too-long", 0, source
    if minutes_left <= limits.parallel_minutes_left:
        return Decision.REMEDIATE_AND_ESCALATE, "low-time", wait, source
    return Decision.REMEDIATE, "recoverable", wait, source
