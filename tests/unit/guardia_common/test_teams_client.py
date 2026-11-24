"""Teams webhook client (task 6.2 · requirement 7.2)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from guardia_common.teams_client import (
    MAX_ACTIONS,
    MAX_ERROR,
    TeamsPostError,
    WebhookConfigError,
    build_escalation_card,
    get_webhook_url,
    post_card,
    truncate,
)
from tests.unit.fakes import BACKUP, PRIMARY, WEBHOOK, FakeSecrets, card_text

INCIDENT = {
    "incident_id": "inc-1",
    "organization": "OrgA",
    "process_type": "master",
    "kind": "FAILED",
    "error": "Genesys.AuthError",
    "cause": "token=abc123-DO-NOT-LEAK",
}
PERSON = {"display_name": "Ana Guardia", "teams_user_id": "ana-aad-id"}
OTHER = {"display_name": "Beto Respaldo", "teams_user_id": "beto-aad-id"}


def card(**overrides):
    args = {
        "incident": INCIDENT,
        "recipients": {"primary": PERSON, "backup": OTHER},
        "diagnosis": {
            "summary": "Credenciales vencidas",
            "actions": [{"action": "redrive", "result": "falló"}],
        },
        "minutes_remaining": 42,
        "deadline_label": "06:00",
    }
    return build_escalation_card(**{**args, **overrides})


def facts(payload) -> dict:
    content = payload["attachments"][0]["content"]
    fact_set = next(block for block in content["body"] if block["type"] == "FactSet")
    return {fact["title"]: fact["value"] for fact in fact_set["facts"]}


@pytest.mark.req("7.2")
def test_card_mentions_on_call_and_lists_incident_data():
    payload = card()
    content = payload["attachments"][0]["content"]

    assert payload["type"] == "message"
    assert payload["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    entities = content["msteams"]["entities"]
    assert [e["mentioned"]["id"] for e in entities] == ["ana-aad-id", "beto-aad-id"]
    assert entities[0]["text"] == "<at>Ana Guardia</at>"
    assert "<at>Ana Guardia</at>" in card_text(payload)
    assert facts(payload) == {
        "Organización": "OrgA",
        "Proceso": "master",
        "Error": "Genesys.AuthError",
        "Diagnóstico": "Credenciales vencidas",
        "Acciones": "redrive: falló",
        "Tiempo restante": "42 min para las 06:00",
    }


@pytest.mark.req("7.4")
def test_card_with_only_the_backup_mentions_the_backup_and_shows_the_warning():
    payload = card(recipients={"backup": OTHER}, warnings=["NO_PRIMARY_ONCALL"])

    entities = payload["attachments"][0]["content"]["msteams"]["entities"]
    assert [e["mentioned"]["id"] for e in entities] == ["beto-aad-id"]
    assert "Aviso de configuración: NO_PRIMARY_ONCALL" in card_text(payload)


def test_card_never_includes_the_raw_cause():
    assert "DO-NOT-LEAK" not in card_text(card())


@pytest.mark.req("7.2")
def test_card_without_actions_says_so_and_parallel_says_recovery_is_running():
    assert facts(card(diagnosis=None))["Acciones"] == "Ninguna (requiere criterio humano)"

    parallel = card(diagnosis=None, parallel=True)
    assert facts(parallel)["Acciones"] == "GuardIA intenta recuperarlo ahora mismo"
    assert "recupera en paralelo" in card_text(parallel)


@pytest.mark.req("7.2")
def test_card_reports_an_overdue_deadline():
    assert facts(card(minutes_remaining=-7))["Tiempo restante"] == (
        "plazo vencido hace 7 min (las 06:00)"
    )


def test_card_limits_long_values_and_many_actions():
    long_error = "E" * 1000
    actions = [{"action": f"a{i}", "result": "ok"} for i in range(MAX_ACTIONS + 3)]

    payload = card(incident={**INCIDENT, "error": long_error}, diagnosis={"actions": actions})

    assert len(facts(payload)["Error"]) == MAX_ERROR
    assert facts(payload)["Error"].endswith("…")
    assert "… y 3 más" in facts(payload)["Acciones"]


def test_mention_markup_cannot_be_broken_by_the_display_name():
    payload = card(recipients={"primary": {**PERSON, "display_name": "Ana </at><b>"}})

    entity = payload["attachments"][0]["content"]["msteams"]["entities"][0]
    assert entity["text"] == "<at>Ana /atb</at>"
    assert entity["mentioned"]["name"] == "Ana /atb"


def test_truncate_collapses_whitespace_and_handles_none():
    assert truncate(None, 10) == ""
    assert truncate("a \n  b", 10) == "a b"
    assert truncate("abcdefghij", 5) == "abcd…"


# --- webhook secret -------------------------------------------------------


def test_webhook_secret_as_plain_address():
    assert get_webhook_url(FakeSecrets(f"  {WEBHOOK}\n"), "teams") == WEBHOOK


def test_webhook_secret_as_json():
    secret = FakeSecrets(json.dumps({"webhook_url": WEBHOOK}))

    assert get_webhook_url(secret, "teams") == WEBHOOK


@pytest.mark.parametrize(
    "bad",
    ["", "not a url", "http://insecure.example.com/x?sig=SECRET", '{"other": "x"}', "{broken"],
)
def test_invalid_webhook_secret_is_rejected_without_echoing_it(bad):
    with pytest.raises(WebhookConfigError) as err:
        get_webhook_url(FakeSecrets(bad), "teams")

    assert "SECRET" not in str(err.value)


def test_unreadable_secret_is_reported_without_boto_details():
    class Broken:
        def get_secret_value(self, SecretId):  # noqa: N803
            raise RuntimeError(f"AccessDenied for {WEBHOOK}")

    with pytest.raises(WebhookConfigError) as err:
        get_webhook_url(Broken(), "teams")

    assert "SUPER-SECRET" not in str(err.value)
    assert "teams" in str(err.value)


# --- posting ----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=202, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(response):
    sent = {}

    def opener(request, timeout):
        sent["request"] = request
        sent["timeout"] = timeout
        return response

    opener.sent = sent
    return opener


def opener_raising(exc):
    def opener(request, timeout):
        raise exc

    return opener


@pytest.mark.req("7.2")
def test_post_sends_json_and_returns_none_when_flow_gives_no_id():
    opener = opener_returning(FakeResponse(202, b""))

    assert post_card(WEBHOOK, {"type": "message"}, opener=opener) is None

    request = opener.sent["request"]
    assert request.full_url == WEBHOOK
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"type": "message"}
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.parametrize("key", ["id", "messageId", "threadId"])
def test_post_returns_the_message_id_when_the_flow_replies_with_one(key):
    opener = opener_returning(FakeResponse(200, json.dumps({key: "msg-9"}).encode()))

    assert post_card(WEBHOOK, {}, opener=opener) == "msg-9"


def test_post_ignores_a_body_that_is_not_json():
    assert post_card(WEBHOOK, {}, opener=opener_returning(FakeResponse(200, b"ok"))) is None


@pytest.mark.req("7.3")
def test_http_error_becomes_teams_post_error_without_the_address():
    error = urllib.error.HTTPError(WEBHOOK, 429, "Too Many", {}, io.BytesIO(b""))

    with pytest.raises(TeamsPostError) as raised:
        post_card(WEBHOOK, {}, opener=opener_raising(error))

    assert raised.value.status == 429
    assert "SUPER-SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.req("7.3")
def test_network_error_becomes_teams_post_error_without_the_address():
    error = urllib.error.URLError(f"cannot reach {WEBHOOK}")

    with pytest.raises(TeamsPostError) as raised:
        post_card(WEBHOOK, {}, opener=opener_raising(error))

    assert raised.value.status is None
    assert "SUPER-SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_unexpected_status_is_an_error():
    with pytest.raises(TeamsPostError) as raised:
        post_card(WEBHOOK, {}, opener=opener_returning(FakeResponse(500, b"")))

    assert raised.value.status == 500


def test_fixture_people_carry_emails_that_the_card_must_not_expose():
    # PRIMARY/BACKUP mimic raw OnCallSchedule rows; the card only needs name and id.
    payload = card(recipients={"primary": PRIMARY, "backup": BACKUP})

    assert "@example.com" not in card_text(payload)


def test_post_refuses_a_non_https_address_without_sending():
    opener = opener_returning(FakeResponse(202, b""))

    with pytest.raises(TeamsPostError):
        post_card("http://insecure.example.com/x", {}, opener=opener)

    assert "request" not in opener.sent


# --------------------------------------------------------------------------- T5
from guardia_common.teams_client import build_status_card  # noqa: E402


def status_card(mode, **overrides):
    args = {
        "mode": mode,
        "incident": INCIDENT,
        "recipients": {"primary": PERSON, "backup": OTHER},
        "minutes_remaining": 42,
        "deadline_label": "06:00",
    }
    return build_status_card(**{**args, **overrides})


def mentioned_ids(payload) -> list[str]:
    content = payload["attachments"][0]["content"]
    return [e["mentioned"]["id"] for e in content.get("msteams", {}).get("entities", [])]


@pytest.mark.req("8.1")
def test_reminder_card_mentions_both_and_shows_the_number_and_minutes_left():
    payload = status_card("reminder", reminder_number=3)

    assert mentioned_ids(payload) == ["ana-aad-id", "beto-aad-id"]
    assert "recordatorio 3" in card_text(payload)
    assert facts(payload)["Tiempo restante"] == "42 min para las 06:00"


@pytest.mark.req("6.2")
def test_sla_breached_card_mentions_both_and_shows_the_delay():
    payload = status_card("sla_breached", minutes_remaining=-17)

    assert mentioned_ids(payload) == ["ana-aad-id", "beto-aad-id"]
    assert "Se venció la hora límite" in card_text(payload)
    assert facts(payload)["Retraso"] == "17 min"


def test_resolved_card_mentions_nobody_and_shows_how_and_how_long():
    payload = status_card("resolved", resolved_by="later_execution", duration_minutes=95)

    assert mentioned_ids(payload) == []
    assert "msteams" not in payload["attachments"][0]["content"]
    assert facts(payload)["Duración del incidente"] == "95 min"
    assert "ejecución posterior" in facts(payload)["Cómo se resolvió"]


@pytest.mark.req("12.3")
@pytest.mark.parametrize("mode", ["reminder", "sla_breached", "resolved"])
def test_status_cards_never_carry_the_raw_cause_and_show_the_incident_id(mode):
    text = card_text(status_card(mode))

    assert "abc123-DO-NOT-LEAK" not in text
    assert "inc-1" in text  # the webhook cannot thread messages: the id finds them


def test_status_card_rejects_unknown_modes():
    with pytest.raises(ValueError, match="unknown status card mode"):
        status_card("shout")
