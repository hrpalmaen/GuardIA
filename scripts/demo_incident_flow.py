"""Local demo of the whole incident flow: detect, classify, recover or escalate.

It chains the eight Lambdas on the virtual clock of `demo_discovery_and_watchdog.py`
(moto, no AWS account, no real Teams):

    DiscoverProcesses -> MissedRunWatchdog -> OpenIncident -> ClassifyIncident
        auto_recoverable    -> RedriveOrRestart -> PollExecution -> resolved
        requires_escalation -> ResolveOnCall -> SendEscalation -> Teams card

`GuardIA-Incident-Flow` (tasks 5.1, 5.4, 5.5) and `ResolveOnSuccess` (6.4) do not exist
yet, so `IncidentFlow` below stands in for them. It decides with a few fixed rules, waits
on the virtual clock, and sets the `RESOLVED` / `ESCALATED` status itself. When the real
state machine arrives, this file is the list of things it must do.

Scenarios (Bogota time, the real failures of the Level 0 demo):
    00:00  EMTSAS fails (Genesys 500)   auto_recoverable -> redrive -> succeeds -> RESOLVED
    02:30  EMT12 fails (out of memory)  requires_escalation -> Teams card -> ESCALATED
    03:10  EMT22 never started          MISSED_RUN, but Teams is down: 3 attempts, then
                                        NOTIFICATION_FAILED is recorded (no email) and it raises

    pip install -e ".[dev]"
    python scripts/demo_incident_flow.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

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

import boto3  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402
from demo_discovery_and_watchdog import (  # noqa: E402
    EMT12,
    EMT22,
    EMTSAS,
    SOURCES,
    bogota,
    hhmm,
    report,
    short,
)
from load_error_catalog import load_catalog, read_catalog  # noqa: E402
from moto import mock_aws  # noqa: E402

from config import DEFAULT_THRESHOLDS, SSM_PREFIX  # noqa: E402
from guardia_common import config as guardia_config  # noqa: E402
from guardia_common.audit import AuditRepository  # noqa: E402
from guardia_common.config import get_threshold, load_thresholds  # noqa: E402
from guardia_common.models import Incident  # noqa: E402
from guardia_common.teams_client import post_card  # noqa: E402
from guardia_local.clock import LocalScheduler  # noqa: E402
from guardia_local.environment import REGION, LocalEnvironment  # noqa: E402
from guardia_local.fixtures import ExecutionFixtures  # noqa: E402
from guardia_local.flow import DetectionPipeline  # noqa: E402
from guardia_local.timeline import scheduled_replays, status_changes  # noqa: E402
from lambdas.classify_incident.handler import Deps as ClassifyDeps  # noqa: E402
from lambdas.classify_incident.handler import clear_catalog_cache  # noqa: E402
from lambdas.discover_processes.handler import Deps as DiscoverDeps  # noqa: E402
from lambdas.discover_processes.handler import discover_processes  # noqa: E402
from lambdas.missed_run_watchdog.handler import Deps as WatchdogDeps  # noqa: E402
from lambdas.missed_run_watchdog.handler import run_watchdog  # noqa: E402
from lambdas.open_incident.handler import Deps as OpenDeps  # noqa: E402
from lambdas.poll_execution.handler import Deps as PollDeps  # noqa: E402
from lambdas.poll_execution.handler import poll_execution  # noqa: E402
from lambdas.redrive_or_restart.handler import (  # noqa: E402
    AttemptsExhaustedError,
    RemediationNotAllowedError,
    redrive_or_restart,
)
from lambdas.redrive_or_restart.handler import Deps as RedriveDeps  # noqa: E402
from lambdas.resolve_on_call.handler import Deps as ResolveDeps  # noqa: E402
from lambdas.resolve_on_call.handler import resolve_oncall  # noqa: E402
from lambdas.send_escalation.handler import Deps as SendDeps  # noqa: E402
from lambdas.send_escalation.handler import EscalationDeliveryError, send_escalation  # noqa: E402

START, END = bogota(23, 0, day=15), bogota(3, 45)
RECOVERY_TIME = timedelta(minutes=9)  # how long the redriven execution takes to finish
POLL_EVERY = timedelta(minutes=3)  # the flow's Wait -> PollExecution loop
TEAMS_DOWN = (bogota(3, 0), bogota(3, 40))  # the webhook answers 500 in this window

WEBHOOK = "https://demo.invalid/teams-webhook?sig=demo-not-a-real-secret"
SECRET_ID = "guardia/teams-alerts-demo"  # noqa: S105 - a name, not a value
ON_CALL_WEEK = {  # Monday of the demo night, as OnCallSchedule stores it
    "week_start": "2026-11-16",
    "primary": {"display_name": "Ana Guardia", "teams_user_id": "ana-demo-id",
                "email": "ana@demo.invalid"},
    "backup": {"display_name": "Beto Respaldo", "teams_user_id": "beto-demo-id",
               "email": "beto@demo.invalid"},
}  # fmt: skip


# --------------------------------------------------------------------------- policy
def decide(
    classification: str, remediation: str, attempts: int, max_attempts: int
) -> tuple[str, str]:
    """Stand-in for RemediationPolicy (5.1): fixed rules, never the AI agent.

    The catalog classification and the process tag decide; nothing else does.
    """
    if classification == "unknown":
        return "escalate", "sin regla en el catálogo (aquí se invocaría al agente, 4.3)"
    if classification == "requires_escalation":
        return "escalate", "el catálogo exige criterio humano"
    if remediation != "auto":
        return "escalate", "el proceso es escalate-only"
    if attempts >= max_attempts:
        return "escalate", "se agotaron los intentos de recuperación"
    return "recover", "recuperable y el proceso permite recuperación automática"


# ----------------------------------------------------------------- local stand-ins
class DemoStepFunctions:
    """Step Functions as the recovery Lambdas see it.

    moto does not implement RedriveExecution, so a recovery is simulated here:
    the redriven execution, or the relaunched one when a redrive would only
    repeat the final state (EMTSAS ends in a Fail state), is RUNNING for
    `RECOVERY_TIME` and then SUCCEEDED. Everything else goes to the replay client.
    """

    def __init__(self, replay: Any, clock: Callable[[], datetime]):
        self._replay = replay
        self._clock = clock
        self._recovering: dict[str, tuple[datetime, datetime]] = {}

    def redrive_execution(self, **params: Any) -> dict:
        now = self._clock()
        self._recovering[params["executionArn"]] = (now, now + RECOVERY_TIME)
        return {"redriveDate": now}

    def start_execution(self, **params: Any) -> dict:
        now = self._clock()
        arn = params["stateMachineArn"].replace(":stateMachine:", ":execution:", 1)
        arn = f"{arn}:{params['name']}"
        self._recovering[arn] = (now, now + RECOVERY_TIME)
        return {"executionArn": arn, "startDate": now}

    def describe_execution(self, **params: Any) -> dict:
        window = self._recovering.get(params["executionArn"])
        if window is None:
            return self._replay.describe_execution(**params)
        since, ends = window
        info = {"executionArn": params["executionArn"], "startDate": since}
        if self._clock() < ends:
            info["status"] = "RUNNING"
        else:
            info.update(status="SUCCEEDED", stopDate=ends)
        return info

    def __getattr__(self, name: str) -> Any:
        return getattr(self._replay, name)


class _Accepted:
    """What a Workflows webhook answers: 202 and no body."""

    status = 202

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> _Accepted:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class TeamsStandIn:
    """The Teams webhook: prints the card it receives, or answers 500 while it is down."""

    def __init__(self, clock: Callable[[], datetime], say: Callable[[str], None]):
        self._clock = clock
        self._say = say
        self.cards: list[dict] = []
        self.rejected = 0

    def __call__(self, request: Any, timeout: float | None = None) -> _Accepted:
        start, end = TEAMS_DOWN
        if start <= self._clock() < end:
            self.rejected += 1
            self._say("       Teams respondió HTTP 500")
            raise urllib.error.HTTPError(
                request.full_url, 500, "Internal Server Error", {}, io.BytesIO(b"")
            )
        card = json.loads(request.data)
        self.cards.append(card)
        self._say("       Teams recibe la tarjeta:")
        for block in card["attachments"][0]["content"]["body"]:
            if block["type"] == "FactSet":
                for fact in block["facts"]:
                    self._say(f"         {fact['title']}: {fact['value']}")
            else:
                self._say(f"         {block['text']}")
        return _Accepted()


# ----------------------------------------------------------------------------- flow
@dataclass
class IncidentFlow:
    """Stand-in for GuardIA-Incident-Flow (5.5), ResolveOnSuccess (6.4) and the Wait state."""

    incidents: Any
    events: Any
    scheduler: LocalScheduler
    redrive_deps: RedriveDeps
    poll_deps: PollDeps
    resolve_deps: ResolveDeps
    send_deps: SendDeps
    max_attempts: int
    parallel_minutes: int
    say: Callable[[str], None] = print
    outcomes: dict[str, str] = field(default_factory=dict)

    def _incident(self, incident_id: str) -> Incident:
        item = self.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True)
        return Incident.from_item(item["Item"])

    def _set_status(self, incident_id: str, status: str, event_type: str, now: datetime) -> None:
        self.incidents.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
        AuditRepository(self.events, actor="LocalFlow").append(
            incident_id, event_type, {"status": status}, now=now
        )

    # -- entry point: DetectionPipeline calls this after ClassifyIncident
    def start(self, result: dict, now: datetime) -> None:
        incident_id = result["incident_id"]
        incident = self._incident(incident_id)
        classification = result["classification"]["classification"]
        decision, why = decide(
            classification, str(incident.remediation), incident.attempts, self.max_attempts
        )
        self.say(f"       → RemediationPolicy (stand-in): {decision} — {why}")
        if decision == "recover":
            self._recover(incident_id, result["minutes_left"], now)
        else:
            self._escalate(incident_id, result["minutes_left"], now, diagnosis=None)

    def _recover(self, incident_id: str, minutes_left: int, now: datetime) -> None:
        try:
            out = redrive_or_restart({"incident_id": incident_id}, self.redrive_deps, now=now)
        except (RemediationNotAllowedError, AttemptsExhaustedError) as err:
            self.say(f"       → RedriveOrRestart rechazó la recuperación ({type(err).__name__})")
            return self._escalate(incident_id, minutes_left, now, diagnosis=None)
        if not out["recovered"]:
            self.say(f"       → RedriveOrRestart: {out['reason']}")
            return self._escalate(incident_id, minutes_left, now, diagnosis=None)
        self.say(
            f"       → RedriveOrRestart {out['strategy']}, intento {out['attempt']} de "
            f"{self.max_attempts}; el flujo espera {int(POLL_EVERY.total_seconds() // 60)} min"
        )
        self.scheduler.at(
            now + POLL_EVERY, "PollExecution", lambda due: self._poll(out, minutes_left, due)
        )

    def _poll(self, recovery: dict, minutes_left: int, now: datetime) -> None:
        incident_id = recovery["incident_id"]
        polled = poll_execution(
            {
                "incident_id": incident_id,
                "recovery_execution_arn": recovery["recovery_execution_arn"],
            },
            self.poll_deps,
            now=now,
        )
        self.say(f"{hhmm(now)}  PollExecution     {incident_id[:8]}… {polled['status']}")
        if not polled["done"]:
            self.scheduler.at(
                now + POLL_EVERY,
                "PollExecution",
                lambda due: self._poll(recovery, minutes_left, due),
            )
        elif polled["succeeded"]:
            self._set_status(incident_id, "RESOLVED", "INCIDENT_RESOLVED", now)
            self.outcomes[incident_id] = "RESOLVED"
            self.say(f"       → incidente {incident_id[:8]}… RESUELTO sin molestar a nadie")
        else:
            action = f"{recovery['strategy']} (intento {recovery['attempt']})"
            diagnosis = {"actions": [{"action": action, "result": polled["status"]}]}
            self._escalate(incident_id, minutes_left, now, diagnosis=diagnosis)

    def _escalate(
        self, incident_id: str, minutes_left: int, now: datetime, diagnosis: dict | None
    ) -> None:
        incident = self._incident(incident_id)
        if incident.root_error and diagnosis is None:
            summary = f"Causa raíz {incident.root_error} en {incident.failed_state}"
            diagnosis = {"summary": summary}
        on_call = resolve_oncall({"incident_id": incident_id}, self.resolve_deps, now=now)
        primary, backup = on_call["primary"], on_call["backup"]
        self.say(
            f"{hhmm(now)}  ResolveOnCall     turno: {primary['display_name'] if primary else '—'}"
            f" · respaldo: {backup['display_name'] if backup else '—'}"
            + (f" · avisos: {on_call['warnings']}" if on_call["warnings"] else "")
        )
        parallel = minutes_left <= self.parallel_minutes
        try:
            sent = send_escalation(
                {
                    "incident_id": incident_id,
                    "recipients": on_call,
                    "diagnosis": diagnosis,
                    "parallel": parallel,
                },
                self.send_deps,
                now=now,
            )
        except EscalationDeliveryError:
            self.outcomes[incident_id] = "NOTIFICATION_FAILED"
            self.say(
                f"{hhmm(now)}  SendEscalation    {incident_id[:8]}… Teams falló 3 veces: se "
                "registra NOTIFICATION_FAILED (sin correo) y la Lambda lanza error"
            )
            return
        self._set_status(incident_id, "ESCALATED", "INCIDENT_ESCALATED", now)
        self.outcomes[incident_id] = "ESCALATED"
        self.say(
            f"{hhmm(now)}  SendEscalation    {incident_id[:8]}… publicado, menciona a "
            f"{' y '.join(sent['mentioned'])}"
        )


# ------------------------------------------------------------------------------ night
def run_night(say: Callable[[str], None] = print) -> dict:
    """Runs the scripted night and returns a summary (also used by the smoke test)."""
    clear_catalog_cache()
    guardia_config.clear_cache()
    with mock_aws():
        env = LocalEnvironment.create(
            ExecutionFixtures.load(),
            start=START,
            thresholds=DEFAULT_THRESHOLDS,
            ssm_prefix=SSM_PREFIX,
        )
        runs = scheduled_replays(
            env.fixtures,
            SOURCES,
            START,
            END,
            skip={(EMT22, bogota(3, 0))},
            replace={
                (EMTSAS, bogota(0, 0)): "emtsas-20260918-0500-failed",
                (EMT12, bogota(2, 30)): "emt12-20260923-2230-failed",
            },
        )
        env.replace_fixtures(env.fixtures.only(runs))

        tables = env.tables
        registry, incidents, events = (
            tables["ProcessRegistry"],
            tables["Incidents"],
            tables["IncidentEvents"],
        )
        tables["OnCallSchedule"].put_item(Item=ON_CALL_WEEK)
        secrets = boto3.client("secretsmanager", region_name=REGION)
        secrets.create_secret(Name=SECRET_ID, SecretString=WEBHOOK)
        load_catalog(tables["ErrorCatalog"], read_catalog())

        thresholds = load_thresholds(env.ssm)
        demo_sfn = DemoStepFunctions(env.sfn, env.clock)
        teams = TeamsStandIn(env.clock, say)
        scheduler = LocalScheduler(env.clock)
        flow = IncidentFlow(
            incidents=incidents,
            events=events,
            scheduler=scheduler,
            redrive_deps=RedriveDeps(incidents, events, demo_sfn),
            poll_deps=PollDeps(events, demo_sfn),
            resolve_deps=ResolveDeps(tables["OnCallSchedule"], events, env.ssm),
            send_deps=SendDeps(
                incidents=incidents,
                events=events,
                secrets=secrets,
                ssm=env.ssm,
                secret_id=SECRET_ID,
                poster=partial(post_card, opener=teams),
                sleep=lambda seconds: None,  # the backoff is not worth real seconds here
            ),
            max_attempts=int(get_threshold(thresholds, "remediation-max-attempts")),
            parallel_minutes=int(get_threshold(thresholds, "parallel-escalation-minutes-left")),
            say=say,
        )

        def after_classification(event: dict, result: dict, now: datetime) -> None:
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                report(event, result, now)  # Level 0 lines, as in the other demo
            for line in captured.getvalue().splitlines():
                say(line)
            if result.get("created") and "classification" in result:
                flow.start(result, now)

        open_deps = OpenDeps(incidents, registry, events, env.sfn, env.ssm, env.flow_arn)
        classify_deps = ClassifyDeps(incidents, tables["ErrorCatalog"], events)
        DetectionPipeline(
            open_deps, env.clock, classify_deps, on_result=after_classification
        ).attach(env.bus)
        env.bus.raise_errors = True

        discover_deps = DiscoverDeps(
            registry, env.tagging, env.events, env.scheduler, env.sfn, env.ssm
        )
        watchdog_deps = WatchdogDeps(registry, incidents, env.sfn, env.bus, env.ssm)
        monitored: set[str] = set()

        def discovery(now: datetime) -> None:
            out = discover_processes(discover_deps, now=now)
            arns = {p["resource_arn"] for p in out["monitored"]}
            if arns != monitored:
                names = ", ".join(short(arn) for arn in sorted(arns - monitored))
                say(f"{hhmm(now)}  DiscoverProcesses  monitorea {len(arns - monitored)}: {names}")
                monitored.update(arns)

        say(f"Noche simulada {hhmm(START)} → {hhmm(END)} (hora Bogotá), reloj virtual")
        say(f"Teams caído entre las {hhmm(TEAMS_DOWN[0])} y las {hhmm(TEAMS_DOWN[1])}\n")
        scheduler.every(timedelta(minutes=15), "DiscoverProcesses", discovery)
        scheduler.every(
            timedelta(minutes=5),
            "MissedRunWatchdog",
            lambda now: run_watchdog(watchdog_deps, now=now),
            first=START + timedelta(minutes=5),
        )
        for stop, event in status_changes(runs):
            if event["detail"]["status"] != "SUCCEEDED":  # the rule would drop them anyway
                scheduler.at(stop, "status-change", lambda now, event=event: env.bus.emit(event))
        scheduler.run_until(END)

        return _summary(incidents, events, flow, teams, say)


def _summary(incidents: Any, events: Any, flow: IncidentFlow, teams: TeamsStandIn,
             say: Callable[[str], None]) -> dict:  # fmt: skip
    rows = sorted(incidents.scan()["Items"], key=lambda row: row["opened_at"])
    result = []
    say(f"\n{len(rows)} incidentes · {len(teams.cards)} tarjetas en Teams · "
        f"{teams.rejected} intentos rechazados por Teams caído")  # fmt: skip
    for row in rows:
        trail = events.query(KeyConditionExpression=Key("incident_id").eq(row["incident_id"]))
        types = [item["event_type"] for item in trail["Items"]]
        result.append({
            "organization": row["organization"],
            "kind": row["kind"],
            "classification": row.get("classification"),
            "status": row["status"],
            "outcome": flow.outcomes.get(row["incident_id"]),
            "events": types,
        })  # fmt: skip
        say(f"   {row['opened_at'][11:16]}  {row['kind']:<10} {row['organization']:<7} "
            f"{row.get('classification', '-'):<20} estado {row['status']}")  # fmt: skip
        say(f"          historial: {' → '.join(types)}")
    return {"incidents": result, "cards": teams.cards, "teams_rejected": teams.rejected}


def main() -> None:
    run_night()


if __name__ == "__main__":
    main()
