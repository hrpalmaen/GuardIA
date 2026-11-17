"""Local demo of OpenIncident (3.1) and ClassifyIncident (4.1).

Runs entirely in memory with moto (a dev dependency): no AWS account and no
credentials. It creates the tables, publishes the SSM thresholds from
`infra/config.py`, loads `data/error_catalog.json`, and runs the scenarios
of a night at 04:00 Bogota time.

The first scenarios replay real executions of the Genesys conversations process
(tests/fixtures/executions): the same OpenIncident and ClassifyIncident code
reads them through `ReplayStepFunctionsClient`, which has the boto3 interface.

    pip install -e ".[dev]"
    python scripts/demo_open_and_classify.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src", "infra", "scripts"):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

# The demo executions do not exist in moto, so DescribeExecution falls back to
# the event data with a WARNING log; hide it to keep the output readable.
os.environ.setdefault("LOG_LEVEL", "ERROR")
for var, value in {
    "AWS_ACCESS_KEY_ID": "demo",
    "AWS_SECRET_ACCESS_KEY": "demo",
    "AWS_DEFAULT_REGION": "us-east-1",
}.items():
    os.environ[var] = value  # never touch a real account from the demo

import boto3  # noqa: E402
from load_error_catalog import load_catalog, read_catalog  # noqa: E402
from moto import mock_aws  # noqa: E402

from config import DEFAULT_THRESHOLDS, SSM_PREFIX  # noqa: E402
from guardia_common.tables import create_local_tables  # noqa: E402
from guardia_local.events import status_change_event  # noqa: E402
from guardia_local.fixtures import ExecutionFixtures  # noqa: E402
from guardia_local.stepfunctions import ReplayStepFunctionsClient  # noqa: E402
from lambdas.classify_incident.handler import Deps as ClassifyDeps  # noqa: E402
from lambdas.classify_incident.handler import classify_incident  # noqa: E402
from lambdas.open_incident.handler import Deps as OpenDeps  # noqa: E402
from lambdas.open_incident.handler import open_incident  # noqa: E402

BOGOTA = ZoneInfo("America/Bogota")
ACCOUNT = "123456789012"
SM = f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine"
EX = f"arn:aws:states:us-east-1:{ACCOUNT}:execution"

PROCESSES = [
    ("genesys-master-orgA", "master", "OrgA", "auto"),
    ("genesys-conversations-orgB", "conversations", "OrgB", "auto"),
    ("genesys-conversations-orgC", "conversations", "OrgC", "escalate-only"),
]


def at(hh_mm: str) -> datetime:
    hour, minute = map(int, hh_mm.split(":"))
    return datetime(2026, 11, 12, hour, minute, tzinfo=BOGOTA)


def failed(machine, run, start, stop, error, cause, status="FAILED"):
    return {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "executionArn": f"{EX}:{machine}:{run}",
            "stateMachineArn": f"{SM}:{machine}",
            "name": run,
            "status": status,
            "startDate": int(at(start).timestamp() * 1000),
            "stopDate": int(at(stop).timestamp() * 1000),
            "input": json.dumps({"organization": machine.rsplit("-", 1)[-1]}),
            "error": error,
            "cause": cause,
        },
    }


# Real executions moved to the demo night, keeping their real durations.
REAL = ExecutionFixtures.load()
for fixture_id, start in (
    ("emtsas-20260918-0500-failed", "00:00"),
    ("emt12-20260923-2230-failed", "02:23"),
    ("emt22-20261113-1600-succeeded", "03:00"),
):
    REAL = REAL.with_start(fixture_id, at(start))


def real(fixture_id: str) -> dict:
    return status_change_event(REAL.by_id(fixture_id).describe_response())


SCENARIOS = [
    ("REAL · EMTSAS 18-sep: Genesys 500 tras 3 reintentos",
     real("emtsas-20260918-0500-failed"), "00:01"),
    ("REAL · EMT12 23-sep: OOM oculto tras States.Runtime",
     real("emt12-20260923-2230-failed"), "02:30"),
    ("REAL · EMT22 13-nov: ejecución exitosa",
     real("emt22-20261113-1600-succeeded"), "03:00"),
    ("Genesys 429 en master OrgA",
     failed("genesys-master-orgA", "r-0330", "03:30", "04:00", "Genesys.ApiError",
            '{"status": 429, "message": "Too Many Requests"}'), "04:00"),
    ("El mismo evento llega otra vez (EventBridge reintenta)",
     failed("genesys-master-orgA", "r-0330", "03:30", "04:00", "Genesys.ApiError",
            '{"status": 429, "message": "Too Many Requests"}'), "04:01"),
    ("403 Forbidden en conversations OrgB",
     failed("genesys-conversations-orgB", "r-0345", "03:45", "04:10", "Genesys.ApiError",
            "HTTP 403 Forbidden"), "04:10"),
    ("TIMED_OUT en conversations OrgC (escalate-only)",
     failed("genesys-conversations-orgC", "r-0300", "03:00", "04:15", "States.Timeout", None,
            status="TIMED_OUT"), "04:15"),
    ("Error desconocido en master OrgA",
     failed("genesys-master-orgA", "r-0420", "04:20", "04:40", "KeyError",
            "KeyError: 'participants'"), "04:40"),
    ("MissedRunWatchdog: master OrgA no arrancó a las 04:30",
     {"kind": "MISSED_RUN", "resource_arn": f"{SM}:genesys-master-orgA",
      "expected_at": "2026-11-12T04:30:00-05:00"}, "04:45"),
    ("Ejecución exitosa (SUCCEEDED)",
     failed("genesys-master-orgA", "r-0500", "05:00", "05:20", None, None,
            status="SUCCEEDED"), "05:20"),
    ("Proceso sin etiquetas guardia:*",
     failed("otro-proceso", "r-1", "04:00", "04:05", "Error", "boom"), "04:05"),
]  # fmt: skip

NEXT_STEP = {
    "auto_recoverable": "RemediationPolicy",
    "requires_escalation": "RemediationPolicy -> escalate",
    "unknown": "InvokeAgent (Bedrock)",
}


def main() -> None:
    with mock_aws():
        tables = create_local_tables(boto3.resource("dynamodb"))
        for machine, ptype, org, remediation in PROCESSES:
            tables["ProcessRegistry"].put_item(Item={
                "resource_arn": f"{SM}:{machine}", "process_type": ptype,
                "organization": org, "remediation": remediation,
            })  # fmt: skip
        ssm = boto3.client("ssm")
        for name, value in DEFAULT_THRESHOLDS.items():
            ssm.put_parameter(Name=f"{SSM_PREFIX}/{name}", Value=value, Type="String")
        for sm in REAL.state_machines.values():
            tables["ProcessRegistry"].put_item(Item={
                "resource_arn": sm.arn,
                "process_type": sm.tags["guardia:process-type"],
                "organization": sm.tags["guardia:organization"],
                "remediation": sm.tags["guardia:remediation"],
            })  # fmt: skip
        load_catalog(tables["ErrorCatalog"], read_catalog())
        moto_sfn = boto3.client("stepfunctions")
        flow_arn = moto_sfn.create_state_machine(
            name="GuardIA-Incident-Flow",
            definition=json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}),
            roleArn=f"arn:aws:iam::{ACCOUNT}:role/guardia-flow",
        )["stateMachineArn"]
        # Real executions come from the fixtures; GuardIA-Incident-Flow lives in moto.
        sfn = ReplayStepFunctionsClient(REAL, fallback=moto_sfn)

        open_deps = OpenDeps(tables["Incidents"], tables["ProcessRegistry"],
                             tables["IncidentEvents"], sfn, ssm, flow_arn)  # fmt: skip
        classify_deps = ClassifyDeps(
            tables["Incidents"], tables["ErrorCatalog"], tables["IncidentEvents"]
        )

        for i, (title, event, clock) in enumerate(SCENARIOS, 1):
            print(f"\n{i}. {title}  [{clock} Bogotá]")
            opened = open_incident(event, open_deps, now=at(clock))
            if not opened["opened"]:
                print(f"   OpenIncident     -> ignorado ({opened['reason']})")
                continue
            inc = opened["incident"]
            short_id = opened["incident_id"][:8]
            print(f"   OpenIncident     -> {short_id}…  created={opened['created']}"
                  f"  {inc['kind']}  {inc['organization']}/{inc['process_type']}"
                  f"  remediation={inc['remediation']}")  # fmt: skip
            print(f"                       deadline {inc['deadline_iso'][11:16]}"
                  f" · quedan {opened['minutes_left']} min")  # fmt: skip
            if not opened["created"]:
                print("                       duplicado: no se crea otro incidente ni otro flujo")
                continue
            if inc.get("root_error"):
                print(f"   Causa raíz       -> {inc['root_error']} en {inc['failed_state']}"
                      f"  (error final: {inc['error']})")  # fmt: skip
            out = classify_incident({"incident_id": opened["incident_id"]}, classify_deps,
                                    now=at(clock))  # fmt: skip
            print(f"   ClassifyIncident -> {out['classification']}"
                  f"  action={out['action']}  max_wait_s={out['max_wait_s']}"
                  f"  ({out['source']})")  # fmt: skip
            step = NEXT_STEP[out["classification"]]
            if inc["remediation"] == "escalate-only" and out["classification"] != "unknown":
                step = "RemediationPolicy -> escalate (escalate-only)"
            print(f"                       siguiente: {step}")

        flows = sfn.list_executions(stateMachineArn=flow_arn)["executions"]
        events = tables["IncidentEvents"].scan()["Items"]
        print(f"\nIncidentes: {tables['Incidents'].scan()['Count']} · "
              f"flujos GuardIA-Incident-Flow iniciados: {len(flows)} · "
              f"eventos de auditoría: {len(events)}")  # fmt: skip
        for e in sorted(events, key=lambda e: e["sk"]):
            print(
                f"   {e['timestamp']}  {e['incident_id'][:8]}…  {e['event_type']:<20} {e['actor']}"
            )


if __name__ == "__main__":
    main()
