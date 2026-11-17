"""Deterministic incident classification against the ErrorCatalog (requirement 4).

Pure functions: no AWS calls, so they are fully table-testable.

Rules, in order:
1. `MISSED_RUN` is always `requires_escalation` (4.4), before looking at the catalog.
2. Catalog rows for the incident's process type, then rows for `"*"`.
   Within each group, `requires_escalation` rows are evaluated before
   `auto_recoverable` rows, so an ambiguous cause (for example one that
   mentions both 403 and 429) never becomes an automatic action.
   Ties are broken by `error_pattern` to keep the result deterministic.
3. No match → `unknown`; the flow then consults the Bedrock agent (4.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import CatalogAction, CatalogEntry, Classification, Incident, IncidentKind

ANY_PROCESS_TYPE = "*"


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    source: str  # "rule", "catalog", or "none"
    action: CatalogAction | None = None
    max_wait_s: int | None = None
    matched_pattern: str | None = None
    matched_process_type: str | None = None

    def as_dict(self) -> dict:
        return {
            "classification": str(self.classification),
            "source": self.source,
            "action": str(self.action) if self.action else None,
            "max_wait_s": self.max_wait_s,
            "matched_pattern": self.matched_pattern,
            "matched_process_type": self.matched_process_type,
        }


def _subject(incident: Incident) -> str:
    return "\n".join(part for part in (incident.kind, incident.error, incident.cause) if part)


def _ordered(entries: list[CatalogEntry], process_type: str) -> list[CatalogEntry]:
    def key(entry: CatalogEntry) -> tuple:
        group = 0 if entry.process_type == process_type else 1
        severity = 0 if entry.classification == Classification.REQUIRES_ESCALATION else 1
        return (group, severity, entry.error_pattern)

    relevant = [e for e in entries if e.process_type in (process_type, ANY_PROCESS_TYPE)]
    return sorted(relevant, key=key)


def classify(incident: Incident, catalog: list[CatalogEntry]) -> ClassificationResult:
    if incident.kind == IncidentKind.MISSED_RUN:
        return ClassificationResult(
            classification=Classification.REQUIRES_ESCALATION,
            source="rule",
            action=CatalogAction.ESCALATE,
            max_wait_s=0,
        )

    subject = _subject(incident)
    for entry in _ordered(catalog, incident.process_type):
        if re.search(entry.error_pattern, subject, flags=re.IGNORECASE):
            return ClassificationResult(
                classification=Classification(entry.classification),
                source="catalog",
                action=CatalogAction(entry.action),
                max_wait_s=entry.max_wait_s,
                matched_pattern=entry.error_pattern,
                matched_process_type=entry.process_type,
            )

    return ClassificationResult(classification=Classification.UNKNOWN, source="none")
