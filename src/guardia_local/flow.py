"""Local stand-in for the detection rules and the first state of GuardIA-Incident-Flow.

In AWS, two EventBridge rules invoke OpenIncident (failed executions and
watchdog detections), and OpenIncident starts GuardIA-Incident-Flow, whose
first state is ClassifyIncident. Locally the flow is still a placeholder, so
`DetectionPipeline` runs ClassifyIncident right after a new incident opens.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from guardia_common.incidents import STATUS_CHANGE_PATTERN, WATCHDOG_PATTERN
from lambdas.classify_incident.handler import Deps as ClassifyDeps
from lambdas.classify_incident.handler import classify_incident
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident

from .bus import LocalEventBus

RULES = {
    "status-change-to-open-incident": STATUS_CHANGE_PATTERN,
    "watchdog-to-open-incident": WATCHDOG_PATTERN,
}


@dataclass
class DetectionPipeline:
    open_deps: OpenDeps
    clock: Callable[[], datetime]
    classify_deps: ClassifyDeps | None = None
    on_result: Callable[[dict, dict, datetime], Any] | None = None

    def __call__(self, event: dict) -> dict:
        now = self.clock()
        result = open_incident(event, self.open_deps, now=now)
        if result.get("created") and self.classify_deps is not None:
            result["classification"] = classify_incident(
                {"incident_id": result["incident_id"]}, self.classify_deps, now=now
            )
        if self.on_result is not None:
            self.on_result(event, result, now)
        return result

    def attach(self, bus: LocalEventBus) -> None:
        for name, pattern in RULES.items():
            bus.put_rule(name, pattern, self)
