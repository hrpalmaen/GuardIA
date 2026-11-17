"""Local demo of Level 0 (detect) plus classification: DiscoverProcesses (2.1),
MissedRunWatchdog (3.4), OpenIncident (3.1) and ClassifyIncident (4.1) over one
night from 23:00 to 06:00 Bogota time, in a few seconds.

Everything runs in memory with moto, on a virtual clock (no AWS account):

* The 3 real extraction state machines (tests/fixtures/executions) are created
  with their `guardia:*` tags and the hourly EventBridge rule that launches them.
* Their real successful executions are replayed at every scheduled time, except
  for the scenarios below; the real failures are replayed at night times.
* DiscoverProcesses runs every 15 minutes and MissedRunWatchdog every 5, as the
  EventBridge schedules will.
* Every run emits its `Step Functions Execution Status Change` event when it
  ends. Failures and watchdog detections reach OpenIncident through the local
  bus with the production rule patterns; ClassifyIncident (the first state of
  GuardIA-Incident-Flow) classifies each new incident with data/error_catalog.json.

Scenarios (Bogota time):
    23:00  a resource with an incomplete tag (excluded) and one with an invalid
           remediation (monitored as escalate-only; daily at 05:00, it never runs)
    00:00  EMTSAS fails (real run of 18-sep: Genesys 500)  → FAILED, auto_recoverable
    00:01  EventBridge delivers the same event again       → no duplicate incident
    01:00  a new process is tagged: EMT99 master, daily at 04:00 (it never runs)
    02:30  EMT12 fails (real run of 23-sep: hidden OOM)    → FAILED, requires_escalation
    03:00  EMT22 does not start                         → MISSED_RUN at 03:10
    03:30  EMT12 runs 45 min instead of 2 (no history)  → STUCK_RUN at 04:05
    04:00  EMTSAS starts 12 min late                    → MISSED_RUN at 04:10
    04:00  EMT99 master does not start                  → MISSED_RUN at 04:10
    05:00  EMT44 does not start                         → MISSED_RUN at 05:10

    pip install -e ".[dev]"
    python scripts/demo_discovery_and_watchdog.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src", "infra", "scripts"):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

os.environ.setdefault("LOG_LEVEL", "ERROR")
for var, value in {
    "AWS_ACCESS_KEY_ID": "demo",
    "AWS_SECRET_ACCESS_KEY": "demo",
    "AWS_DEFAULT_REGION": "us-east-1",
}.items():
    os.environ[var] = value  # never touch a real account from the demo

from load_error_catalog import load_catalog, read_catalog  # noqa: E402
from moto import mock_aws  # noqa: E402

from config import DEFAULT_THRESHOLDS, SSM_PREFIX  # noqa: E402
from guardia_local.clock import LocalScheduler  # noqa: E402
from guardia_local.environment import LocalEnvironment, create_state_machine  # noqa: E402
from guardia_local.fixtures import ExecutionFixtures  # noqa: E402
from guardia_local.flow import DetectionPipeline  # noqa: E402
from guardia_local.timeline import scheduled_replays, status_changes  # noqa: E402
from lambdas.classify_incident.handler import Deps as ClassifyDeps  # noqa: E402
from lambdas.discover_processes.handler import Deps as DiscoverDeps  # noqa: E402
from lambdas.discover_processes.handler import discover_processes  # noqa: E402
from lambdas.missed_run_watchdog.handler import Deps as WatchdogDeps  # noqa: E402
from lambdas.missed_run_watchdog.handler import run_watchdog  # noqa: E402
from lambdas.open_incident.handler import Deps as OpenDeps  # noqa: E402

BOGOTA = ZoneInfo("America/Bogota")
SM = (
    "arn:aws:states:us-east-1:123456789012:stateMachine:"
    "BI-{}-GCRAW-Orchestrator-ConversationsDetail"
)
EMT12, EMT22, EMTSAS = (SM.format(org) for org in ("EMT12", "EMT22", "EMTSAS"))
SOURCES = {
    EMT12: "emt12-20261113-1630-succeeded",
    EMT22: "emt22-20261113-1600-succeeded",
    EMTSAS: "emtsas-20261113-1600-succeeded",
}
TAGS = {"guardia:monitor": "true", "guardia:process-type": "master",
        "guardia:organization": "EMT99", "guardia:remediation": "auto"}  # fmt: skip


def bogota(hour: int, minute: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=BOGOTA)


START, END = bogota(23, 0, day=15), bogota(6, 0)


def hhmm(moment: datetime | str) -> str:
    moment = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    return moment.astimezone(BOGOTA).strftime("%H:%M")


def short(arn: str) -> str:
    return arn.rsplit(":", 1)[-1].replace("-GCRAW-Orchestrator-ConversationsDetail", "")


NEXT_STEP = {
    "auto_recoverable": "RemediationPolicy",
    "requires_escalation": "RemediationPolicy → escalar",
    "unknown": "InvokeAgent (Bedrock)",
}


def report(event: dict, result: dict, moment: datetime) -> None:
    """Prints what OpenIncident and ClassifyIncident did with one delivered event."""
    detail = event["detail"]
    now = hhmm(moment)
    if event["source"] == "aws.states":
        started = datetime.fromtimestamp(detail["startDate"] / 1000, UTC)
        print(f"{now}  Step Functions     {detail['status']:<10} {short(detail['stateMachineArn'])}"
              f" (iniciada {hhmm(started)})")  # fmt: skip
    else:
        when = detail.get("expected_at") or detail.get("started_at")
        label = "programada" if detail["kind"] == "MISSED_RUN" else "iniciada"
        print(f"{now}  MissedRunWatchdog  {detail['kind']:<10} {short(detail['resource_arn'])}"
              f" ({label} {hhmm(when)})")  # fmt: skip
    if not result.get("opened"):
        print(f"       → OpenIncident ignorado ({result.get('reason')})")
        return
    incident_id = result["incident_id"][:8]
    if not result["created"]:
        print(
            f"       → OpenIncident {incident_id}… ya existe: EventBridge reintentó, "
            "no se abre otro incidente ni otro flujo"
        )
        return
    incident = result["incident"]
    print(f"       → OpenIncident {incident_id}…  quedan {result['minutes_left']} min "
          "para las 06:00")  # fmt: skip
    if incident.get("root_error"):
        print(f"         causa raíz {incident['root_error']} en {incident['failed_state']}"
              f" (error final: {incident['error']})")  # fmt: skip
    out = result["classification"]
    print(f"       → ClassifyIncident {out['classification']} (acción {out['action']}, "
          f"{out['source']}) → siguiente: {NEXT_STEP[out['classification']]}")  # fmt: skip


def main() -> None:
    with mock_aws():
        env = LocalEnvironment.create(
            ExecutionFixtures.load(), start=START, thresholds=DEFAULT_THRESHOLDS,
            ssm_prefix=SSM_PREFIX,
        )  # fmt: skip
        runs = scheduled_replays(
            env.fixtures, SOURCES, START, END,
            skip={(EMT22, bogota(3, 0))},
            delay={(EMTSAS, bogota(4, 0)): timedelta(minutes=12)},
            durations={(EMT12, bogota(3, 30)): timedelta(minutes=45)},
            replace={(EMTSAS, bogota(0, 0)): "emtsas-20260918-0500-failed",
                     (EMT12, bogota(2, 30)): "emt12-20260923-2230-failed"},
        )  # fmt: skip
        env.replace_fixtures(env.fixtures.only(runs))

        # Misconfigured resources the owners tagged by mistake.
        create_state_machine(
            env.moto_sfn,
            env.events,
            "BI-EMT33-Incomplete",
            {k: v for k, v in TAGS.items() if k != "guardia:organization"},
        )
        typo = {**TAGS, "guardia:organization": "EMT44", "guardia:remediation": "automatic"}
        create_state_machine(env.moto_sfn, env.events, "BI-EMT44-Typo", typo, "cron(0 10 * * ? *)")

        registry, incidents = env.tables["ProcessRegistry"], env.tables["Incidents"]
        discover_deps = DiscoverDeps(registry, env.tagging, env.events, env.scheduler,
                                     env.sfn, env.ssm)  # fmt: skip
        watchdog_deps = WatchdogDeps(registry, incidents, env.sfn, env.bus, env.ssm)
        open_deps = OpenDeps(incidents, registry, env.tables["IncidentEvents"], env.sfn,
                             env.ssm, env.flow_arn)  # fmt: skip
        load_catalog(env.tables["ErrorCatalog"], read_catalog())
        classify_deps = ClassifyDeps(incidents, env.tables["ErrorCatalog"],
                                     env.tables["IncidentEvents"])  # fmt: skip
        DetectionPipeline(open_deps, env.clock, classify_deps, on_result=report).attach(env.bus)
        env.bus.raise_errors = True

        print(f"Noche simulada {hhmm(START)} → {hhmm(END)} (hora Bogotá), reloj virtual\n")
        seen_warnings: set[tuple[str, str]] = set()
        monitored: set[str] = set()

        def discovery(now: datetime) -> None:
            out = discover_processes(discover_deps, now=now)
            new = [
                w for w in out["warnings"] if (w["resource_arn"], w["code"]) not in seen_warnings
            ]
            arns = {p["resource_arn"] for p in out["monitored"]}
            if arns != monitored:
                added = [p for p in out["monitored"] if p["resource_arn"] not in monitored]
                names = ", ".join(f"{short(p['resource_arn'])} {p['schedules'][0]} "
                                  f"({p['remediation']})" for p in added)  # fmt: skip
                verb = "monitorea" if not monitored else "agrega"
                print(f"{hhmm(now)}  DiscoverProcesses  {verb} {len(added)}: {names}")
                monitored.clear()
                monitored.update(arns)
            for warning in new:
                seen_warnings.add((warning["resource_arn"], warning["code"]))
                print(f"       ⚠ {short(warning['resource_arn'])}: {warning['code']} "
                      f"({warning['detail']})")  # fmt: skip

        def watchdog(now: datetime) -> None:
            out = run_watchdog(watchdog_deps, now=now)
            return out  # each detection is printed by report() when it is delivered

        def ended(event: dict) -> None:
            env.bus.emit(event)

        def tag_new_process(now: datetime) -> None:
            create_state_machine(env.moto_sfn, env.events, "BI-EMT99-GCRAW-Orchestrator-Master",
                                 TAGS, "cron(0 9 * * ? *)")  # fmt: skip
            print(f"{hhmm(now)}  (los dueños etiquetan BI-EMT99 master, diario 04:00 Bogotá)")

        scheduler = LocalScheduler(env.clock)
        scheduler.every(timedelta(minutes=15), "DiscoverProcesses", discovery)
        scheduler.every(timedelta(minutes=5), "MissedRunWatchdog", watchdog,
                        first=START + timedelta(minutes=5))  # fmt: skip
        scheduler.at(bogota(1, 0) - timedelta(seconds=1), "tag", tag_new_process)
        changes = status_changes(runs)
        for stop, event in changes:
            scheduler.at(stop, "status-change", lambda now, event=event: ended(event))
        first_failure = next(e for _, e in changes if e["detail"]["status"] == "FAILED")
        scheduler.at(bogota(0, 1), "redelivery", lambda now: ended(first_failure))
        scheduler.run_until(END)

        rows = incidents.scan()["Items"]
        cycles = sum(1 for _, name, _ in scheduler.log if name == "MissedRunWatchdog")
        ignored = sum(1 for _, e in changes if e["detail"]["status"] == "SUCCEEDED")
        print(f"\n{cycles} ciclos del watchdog · {len(runs)} ejecuciones reproducidas "
              f"({ignored} SUCCEEDED: la regla no las envía a OpenIncident) · "
              f"{len(rows)} incidentes")  # fmt: skip
        for row in sorted(rows, key=lambda r: r["opened_at"]):
            print(f"   {row['opened_at'][11:16]}  {row['kind']:<10} {row['organization']:<7} "
                  f"{row.get('classification', '-'):<20} estado {row['status']}")  # fmt: skip


if __name__ == "__main__":
    main()
