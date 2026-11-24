"""SendEscalation modes (T5 · tasks 6.3 and 6.5 · requirements 6.2, 7.2, 8.1)."""

from __future__ import annotations

import pytest

from lambdas.send_escalation.handler import diagnosis_from_events, send_escalation
from tests.unit.fakes import bogota, card_text, event_rows, event_types
from tests.unit.lambdas.test_send_escalation import OTHER, PERSON, RECIPIENTS, Poster, make


def event(**overrides):
    return {"incident_id": "inc-1", "recipients": RECIPIENTS, **overrides}


def add_event(deps, event_type: str, seq: int, detail: dict | None = None) -> None:
    deps.events.put_row(
        {
            "incident_id": "inc-1",
            "sk": f"2026-11-12T04:0{seq}:00.000-05:00#0000",
            "event_type": event_type,
            "actor": "test",
            "detail": detail or {},
        }
    )


@pytest.mark.req("8.1")
def test_reminder_mentions_both_people_and_counts_the_reminders():
    poster = Poster(None, None)
    deps, _ = make(poster)

    first = send_escalation(event(mode="reminder"), deps, now=bogota(4, 10))
    second = send_escalation(event(mode="reminder"), deps, now=bogota(4, 20))

    assert first["mentioned"] == ["primary", "backup"] and first["posted"] is True
    assert second["posted"] is True
    text = [card_text(card) for _, card in poster.calls]
    assert "recordatorio 1" in text[0] and "recordatorio 2" in text[1]
    assert "ana-aad-id" in text[0] and "beto-aad-id" in text[0]
    assert "110 min para las 06:00" in text[0]
    assert event_types(deps.events, "inc-1").count("REMINDER_SENT") == 2
    assert [r["detail"]["reminder_number"] for r in event_rows(deps.events, "inc-1")] == [1, 2]


@pytest.mark.req("8.1")
def test_reminder_posts_even_if_the_initial_message_was_already_sent():
    poster = Poster(None)
    deps, _ = make(poster, teams_thread_id="webhook-inc-1")

    result = send_escalation(event(mode="reminder"), deps, now=bogota(4, 10))

    assert result["posted"] is True and result["teams_thread_id"] == "webhook-inc-1"
    assert len(poster.calls) == 1
    assert deps.incidents.only()["teams_thread_id"] == "webhook-inc-1"


@pytest.mark.req("6.2")
def test_sla_breached_mentions_both_and_says_the_deadline_passed():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation(event(mode="sla_breached"), deps, now=bogota(6, 25))

    assert result["mentioned"] == ["primary", "backup"]
    ((_, card),) = poster.calls
    text = card_text(card)
    assert "Se venció la hora límite" in text and "25 min" in text
    assert "ana-aad-id" in text and "beto-aad-id" in text
    assert event_types(deps.events, "inc-1") == ["SLA_BREACH_NOTIFIED"]


def test_resolved_mentions_nobody_and_shows_how_and_how_long():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation(
        event(mode="resolved", resolution={"resolved_by": "later_execution"}),
        deps,
        now=bogota(5, 30),
    )

    assert result["mentioned"] == []
    ((_, card),) = poster.calls
    text = card_text(card)
    assert "Incidente resuelto" in text
    assert "una ejecución posterior terminó bien" in text
    assert "90 min" in text  # opened at 04:00
    assert "ana-aad-id" not in text and "beto-aad-id" not in text
    assert event_types(deps.events, "inc-1") == ["RESOLUTION_NOTIFIED"]


def test_initial_is_still_the_default_and_does_not_post_twice():
    poster = Poster("msg-1", "msg-2")
    deps, _ = make(poster)

    first = send_escalation(event(), deps, now=bogota(4, 0))
    again = send_escalation(event(mode="initial"), deps, now=bogota(4, 1))

    assert first["posted"] is True and first["teams_thread_id"] == "msg-1"
    assert again["posted"] is False and again["teams_thread_id"] == "msg-1"
    assert len(poster.calls) == 1


def test_unknown_mode_is_rejected():
    deps, _ = make(Poster())
    with pytest.raises(ValueError, match="mode must be one of"):
        send_escalation(event(mode="shout"), deps, now=bogota(4, 0))


@pytest.mark.req("7.3")
def test_a_failed_reminder_records_the_failure_with_its_kind_and_raises():
    from guardia_common.teams_client import TeamsPostError
    from lambdas.send_escalation.handler import EscalationDeliveryError

    deps, _ = make(Poster(*[TeamsPostError("x", status=500)] * 3))

    with pytest.raises(EscalationDeliveryError):
        send_escalation(event(mode="reminder"), deps, now=bogota(4, 10))

    (row,) = event_rows(deps.events, "inc-1")
    assert row["event_type"] == "NOTIFICATION_FAILED" and row["detail"]["kind"] == "reminder"


@pytest.mark.req("7.2")
def test_initial_builds_the_diagnosis_from_the_audit_events():
    poster = Poster(None)
    deps, _ = make(poster)
    add_event(deps, "RECOVERY_STARTED", 1, {"strategy": "redrive", "attempt": 1})
    add_event(deps, "RECOVERY_FINISHED", 2, {"status": "FAILED"})
    add_event(deps, "POLICY_DECIDED", 3, {"decision": "ESCALATE", "reason": "attempts-exhausted"})

    send_escalation(event(), deps, now=bogota(4, 5))

    text = card_text(poster.calls[0][1])
    assert "redrive (intento 1): iniciado" in text
    assert "ejecución recuperada: FAILED" in text
    assert "ESCALATE (attempts-exhausted)" in text


def test_a_diagnosis_in_the_event_wins_over_the_audit_events():
    poster = Poster(None)
    deps, _ = make(poster)
    add_event(deps, "POLICY_DECIDED", 1, {"decision": "ESCALATE", "reason": "observe-only"})

    send_escalation(
        event(diagnosis={"summary": "Resumen del agente", "actions": []}), deps, now=bogota(4, 5)
    )

    text = card_text(poster.calls[0][1])
    assert "Resumen del agente" in text and "observe-only" not in text


def test_the_agent_summary_stored_in_the_incident_is_the_diagnosis_when_there_is_nothing_else():
    poster = Poster(None)
    deps, _ = make(poster, agent_summary="El agente vio un límite de tasa")

    send_escalation(event(), deps, now=bogota(4, 5))

    assert "El agente vio un límite de tasa" in card_text(poster.calls[0][1])


def test_diagnosis_from_events_ignores_other_events_and_the_error_cause():
    events = [
        {"event_type": "INCIDENT_OPENED", "detail": {"cause": "token=abc"}},
        {"event_type": "RECOVERY_STARTED", "detail": {"strategy": "restart", "attempt": 2}},
    ]
    assert diagnosis_from_events(events) == {
        "summary": None,
        "actions": [{"action": "restart (intento 2)", "result": "iniciado"}],
    }


def test_there_is_no_one_to_mention_in_a_resolved_message_even_without_recipients():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation({"incident_id": "inc-1", "mode": "resolved"}, deps, now=bogota(5, 0))

    assert result["mentioned"] == [] and OTHER["teams_user_id"] not in card_text(poster.calls[0][1])
    assert PERSON["teams_user_id"] not in card_text(poster.calls[0][1])


@pytest.mark.req("6.1")
@pytest.mark.parametrize("mode", ["reminder", "sla_breached"])
def test_nobody_is_paged_about_an_incident_the_recovery_already_resolved(mode):
    poster = Poster(None)
    deps, _ = make(poster, status="RESOLVED")

    result = send_escalation(event(mode=mode), deps, now=bogota(4, 10))

    assert result["posted"] is False and result["skipped"] == "already_resolved"
    assert poster.calls == [] and result["mentioned"] == []
    assert event_types(deps.events, "inc-1") == []


def test_the_resolved_message_still_goes_out_for_a_resolved_incident():
    poster = Poster(None)
    deps, _ = make(poster, status="RESOLVED", resolved_by="guardia_recovery")

    result = send_escalation(event(mode="resolved"), deps, now=bogota(4, 30))

    assert result["posted"] is True
    assert "guardia_recovery" in card_text(poster.calls[0][1])
