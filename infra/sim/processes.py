"""The processes GuardIA-Sim deploys, taken from tests/fixtures/executions/manifest.json.

The three real organizations keep their names, tags, and assumed crons (H8).
A fourth, `SIMNOAUTO`, is the same orchestrator tagged `escalate-only`: it is
there to prove that GuardIA never redrives a process without
`guardia:remediation=auto` (requirements 12.1, 12.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .definition import state_machine_name

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "fixtures" / "executions" / "manifest.json"
DEFINITIONS = Path(__file__).resolve().parent / "definitions"
NO_AUTO_ORG = "SIMNOAUTO"
# The orchestrator's input as the fixtures show it (source=scheduled).
SCHEDULED_INPUT = {
    "source": "scheduled",
    "intervalStart": "",
    "intervalEnd": "",
    "attributesOneByOne": False,
    "bucketName": "",
    "s3Prefix": "",
    "prefix": "",
}


@dataclass(frozen=True)
class SimProcess:
    org: str
    name: str
    schedule: str
    tags: dict[str, str]

    @property
    def definition_file(self) -> Path:
        return DEFINITIONS / f"{self.org.lower()}.asl.json"


def sim_processes() -> list[SimProcess]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    processes = [
        SimProcess(
            org=sm["tags"]["guardia:organization"],
            name=sm["name"],
            schedule=sm["schedule"],
            tags={**sm["tags"], "guardia:simulated": "true"},
        )
        for sm in manifest["state_machines"]
    ]
    processes.append(
        SimProcess(
            org=NO_AUTO_ORG,
            name=state_machine_name(NO_AUTO_ORG),
            schedule="cron(15 * * * ? *)",
            tags={
                "guardia:monitor": "true",
                "guardia:process-type": "conversations",
                "guardia:organization": NO_AUTO_ORG,
                "guardia:remediation": "escalate-only",
                "guardia:simulated": "true",
            },
        )
    )
    return sorted(processes, key=lambda p: p.org)
