"""Local demo of ResolveOnCall (6.1) and SendEscalation (6.3).

Runs in memory, with no AWS account and no real Teams. The card is posted with
the real `post_card` to a tiny HTTP server on this machine that plays the
Teams Workflows webhook, so the retries and the failure path are the real ones.

    python scripts/demo_escalation.py

Scenarios (a night at 04:00 Bogota time, deadline 06:00):
    1. Normal week: the on-call engineer and the backup are mentioned.
    2. Nobody on call: the backup is mentioned and a configuration warning is recorded.
    3. Teams down: 3 attempts, then NOTIFICATION_FAILED is recorded (no email) and it raises.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from datetime import datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src", "infra", "scripts", "."):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

os.environ.setdefault("LOG_LEVEL", "ERROR")  # keep the output readable

from tests.unit.fakes import (  # noqa: E402
    BACKUP,
    PRIMARY,
    FakeSecrets,
    FakeTable,
    event_rows,
    fresh_ssm,
    new_incident_item,
)

from guardia_common.teams_client import post_card  # noqa: E402
from lambdas.resolve_on_call import handler as resolve_module  # noqa: E402
from lambdas.send_escalation import handler as send_module  # noqa: E402

NOW = datetime(2026, 11, 12, 4, 0, tzinfo=ZoneInfo("America/Bogota"))
WEEK = "2026-11-09"
FAKE_WEBHOOK = "https://demo.invalid/teams-webhook?sig=demo"  # never contacted: see _local_opener


class FakeTeams(BaseHTTPRequestHandler):
    """Stands in for the Workflows webhook: 202 normally, 500 when `fail` is set."""

    fail = False
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTeams.received.append(body)
        self.send_response(500 if FakeTeams.fail else 202)
        self.end_headers()

    def log_message(self, *args):  # silence the access log
        pass


def _local_opener(port: int):
    """Sends the request to the local server instead of the (fake) https address."""

    def opener(request, timeout):
        local = urllib.request.Request(
            f"http://127.0.0.1:{port}/", data=request.data, headers=dict(request.header_items())
        )
        return urllib.request.urlopen(local, timeout=timeout)  # noqa: S310 - localhost only

    return opener


def build(port: int, schedule_rows: list[dict]):
    schedule = FakeTable("week_start")
    for row in schedule_rows:
        schedule.put_row(row)
    events = FakeTable("incident_id", "sk")
    incidents = FakeTable("incident_id")
    incidents.put_row(new_incident_item("inc-demo"))
    ssm = fresh_ssm()
    resolve = resolve_module.Deps(schedule=schedule, events=events, ssm=ssm)
    send = send_module.Deps(
        incidents=incidents,
        events=events,
        secrets=FakeSecrets(FAKE_WEBHOOK),
        ssm=ssm,
        secret_id="demo-secret",  # noqa: S106 - a name, not a value
        poster=partial(post_card, opener=_local_opener(port)),
        backoff_s=0.2,
    )
    return resolve, send, events


def show_card(card: dict) -> None:
    content = card["attachments"][0]["content"]
    for block in content["body"]:
        if block["type"] == "FactSet":
            for fact in block["facts"]:
                print(f"      {fact['title']}: {fact['value']}")
        else:
            print(f"      {block['text']}")


def show_events(events: FakeTable) -> None:
    for row in event_rows(events, "inc-demo"):
        print(f"   evento {row['event_type']:<20} {json.dumps(row['detail'], ensure_ascii=False)}")


def scenario(title: str, port: int, rows: list[dict], *, fail: bool = False) -> None:
    print(f"\n=== {title}")
    FakeTeams.fail = fail
    FakeTeams.received = []
    resolve, send, events = build(port, rows)

    oncall = resolve_module.resolve_oncall({"incident_id": "inc-demo"}, resolve, now=NOW)
    print(f"   ResolveOnCall -> {json.dumps(oncall, ensure_ascii=False)}")

    event = {
        "incident_id": "inc-demo",
        "recipients": oncall,
        "diagnosis": {
            "summary": "Credenciales de Genesys vencidas; requiere criterio humano.",
            "actions": [{"action": "reintento de ejecución", "result": "falló igual"}],
        },
    }
    try:
        result = send_module.send_escalation(event, send, now=NOW)
        print(f"   SendEscalation -> {json.dumps(result, ensure_ascii=False)}")
        print("   Tarjeta enviada a Teams:")
        show_card(FakeTeams.received[-1])
    except send_module.EscalationDeliveryError as err:
        print(f"   SendEscalation lanzó: {err}")
        print(f"   Intentos que recibió Teams: {len(FakeTeams.received)}")
    show_events(events)


def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), FakeTeams)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        full = {"week_start": WEEK, "primary": PRIMARY, "backup": BACKUP}
        scenario("1. Semana normal: se menciona a la persona de turno y al respaldo", port, [full])
        scenario(
            "2. Sin persona de turno: se menciona al respaldo y se registra el aviso",
            port,
            [{"week_start": WEEK, "backup": BACKUP}],
        )
        scenario("3. Teams caído: 3 intentos y se registra el fallo", port, [full], fail=True)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
