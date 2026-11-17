"""ResolveOnCall and SendEscalation entry points against moto (tasks 6.1, 6.3)."""

from __future__ import annotations

import boto3
import pytest

from lambdas.resolve_on_call import handler as resolve_module
from lambdas.send_escalation import handler as send_module
from tests.unit.conftest import REGION, bogota
from tests.unit.fakes import BACKUP, PRIMARY, WEBHOOK, new_incident_item


@pytest.fixture
def lambda_env(aws, monkeypatch):
    # The shared `aws` fixture already creates OnCallSchedule (and every other table).
    schedule = aws["tables"]["OnCallSchedule"]
    schedule.put_item(Item={"week_start": "2026-11-09", "primary": PRIMARY, "backup": BACKUP})
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="guardia/teams-webhook", SecretString=WEBHOOK
    )
    aws["tables"]["Incidents"].put_item(Item=new_incident_item("inc-1"))

    monkeypatch.setenv("ONCALL_SCHEDULE_TABLE", "OnCallSchedule")
    monkeypatch.setenv("INCIDENTS_TABLE", "Incidents")
    monkeypatch.setenv("INCIDENT_EVENTS_TABLE", "IncidentEvents")
    monkeypatch.setenv("TEAMS_ALERTS_SECRET_ID", "guardia/teams-webhook")
    monkeypatch.setenv("TEAMS_RETRY_BACKOFF_SECONDS", "0")
    resolve_module._aws_deps.cache_clear()
    send_module._aws_deps.cache_clear()
    yield aws
    resolve_module._aws_deps.cache_clear()
    send_module._aws_deps.cache_clear()


@pytest.mark.req("7.1")
@pytest.mark.req("7.2")
def test_resolve_then_send_through_lambda_handlers(lambda_env, monkeypatch):
    posted = []
    deps = send_module._aws_deps()
    deps.poster = lambda url, card: posted.append((url, card))  # Teams stand-in

    # ResolveOnCall reads the real clock; the week row exists only for 2026-11-09,
    # so call the core function with a fixed time and keep the handler for wiring.
    on_call = resolve_module.resolve_oncall(
        {"incident_id": "inc-1"}, resolve_module._aws_deps(), now=bogota(4, 0)
    )
    result = send_module.handler({"incident_id": "inc-1", "recipients": on_call}, None)

    assert on_call["primary"]["teams_user_id"] == "ana-aad-id"
    assert result["posted"] is True and result["mentioned"] == ["primary", "backup"]
    assert posted[0][0] == WEBHOOK
    incident = lambda_env["tables"]["Incidents"].get_item(Key={"incident_id": "inc-1"})["Item"]
    assert incident["teams_thread_id"] == "webhook-inc-1"
    events = lambda_env["tables"]["IncidentEvents"].scan()["Items"]
    assert sorted(e["event_type"] for e in events) == ["ESCALATION_SENT", "ONCALL_RESOLVED"]
