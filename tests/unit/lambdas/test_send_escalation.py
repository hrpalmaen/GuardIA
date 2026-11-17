"""SendEscalation Lambda (task 6.3 · requirements 7.2, 7.3, 7.4)."""

from __future__ import annotations

import pytest

from guardia_common.teams_client import TeamsPostError
from lambdas.send_escalation.handler import (
    Deps,
    EscalationDeliveryError,
    IncidentNotFoundError,
    send_escalation,
)
from tests.unit.fakes import (
    WEBHOOK,
    FakeSecrets,
    FakeTable,
    bogota,
    event_rows,
    event_types,
    fresh_ssm,
    new_incident_item,
)

NOW = bogota(4, 0)  # deadline 06:00 -> 120 minutes left
PERSON = {"display_name": "Ana Guardia", "teams_user_id": "ana-aad-id"}
OTHER = {"display_name": "Beto Respaldo", "teams_user_id": "beto-aad-id"}
RECIPIENTS = {"primary": PERSON, "backup": OTHER, "warnings": []}


class Poster:
    """Scripted Teams: each call pops the next outcome (an id/None or an exception)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, card):
        self.calls.append((url, card))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(poster, **incident_extra):
    incidents = FakeTable("incident_id")
    incidents.put_row(new_incident_item(cause="password=hunter2", **incident_extra))
    sleeps: list[float] = []
    deps = Deps(
        incidents=incidents,
        events=FakeTable("incident_id", "sk"),
        secrets=FakeSecrets(),
        ssm=fresh_ssm(),
        secret_id="guardia/teams",  # noqa: S106 - a secret name, not a value
        poster=poster,
        sleep=sleeps.append,
    )
    return deps, sleeps


def event(**overrides):
    return {"incident_id": "inc-1", "recipients": RECIPIENTS, **overrides}


def fail(status=500):
    return TeamsPostError("failed", status=status)


@pytest.mark.req("7.2")
def test_posts_one_card_mentioning_the_on_call_with_incident_data_and_minutes_left():
    poster = Poster(None)
    deps, sleeps = make(poster)

    result = send_escalation(
        event(
            diagnosis={
                "summary": "Credenciales vencidas",
                "actions": [{"action": "redrive", "result": "falló"}],
            }
        ),
        deps,
        now=NOW,
    )

    ((url, card),) = poster.calls
    assert url == WEBHOOK
    text = str(card)
    assert "ana-aad-id" in text and "beto-aad-id" in text
    for expected in (
        "OrgA",
        "master",
        "Genesys.AuthError",
        "redrive: falló",
        "120 min para las 06:00",
    ):
        assert expected in text
    assert result["posted"] is True and result["mentioned"] == ["primary", "backup"]
    assert sleeps == []


@pytest.mark.req("7.2")
def test_the_thread_id_is_stored_and_the_status_is_left_to_the_state_machine():
    deps, _ = make(Poster("msg-42"))

    result = send_escalation(event(), deps, now=NOW)

    row = deps.incidents.only()
    assert result["teams_thread_id"] == "msg-42" == row["teams_thread_id"]
    assert row["status"] == "OPEN"
    assert event_types(deps.events, "inc-1") == ["ESCALATION_SENT"]
    assert event_rows(deps.events, "inc-1")[0]["detail"]["thread_id_from_teams"] is True


@pytest.mark.req("7.2")
def test_without_a_message_id_the_thread_is_keyed_by_the_incident():
    deps, _ = make(Poster(None))

    result = send_escalation(event(), deps, now=NOW)

    assert result["teams_thread_id"] == "webhook-inc-1"
    assert deps.incidents.only()["teams_thread_id"] == "webhook-inc-1"
    assert event_rows(deps.events, "inc-1")[0]["detail"]["thread_id_from_teams"] is False


def test_the_raw_cause_never_reaches_teams_or_the_audit_trail():
    poster = Poster(None)
    deps, _ = make(poster)

    send_escalation(event(), deps, now=NOW)

    assert "hunter2" not in str(poster.calls)
    assert "hunter2" not in str(deps.events.rows)


@pytest.mark.req("7.3")
def test_retries_and_succeeds_on_the_third_attempt_with_backoff():
    poster = Poster(fail(503), fail(429), "msg-1")
    deps, sleeps = make(poster)

    result = send_escalation(event(), deps, now=NOW)

    assert len(poster.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert result["posted"] is True
    detail = event_rows(deps.events, "inc-1")[0]["detail"]
    assert detail["attempts_used"] == 3
    assert event_types(deps.events, "inc-1") == ["ESCALATION_SENT"]


@pytest.mark.req("7.3")
def test_three_failures_record_notification_failed_and_raise_without_any_email():
    poster = Poster(fail(500), fail(500), fail(502))
    deps, sleeps = make(poster)

    with pytest.raises(EscalationDeliveryError):
        send_escalation(event(), deps, now=NOW)

    assert len(poster.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert event_types(deps.events, "inc-1") == ["NOTIFICATION_FAILED"]
    row = event_rows(deps.events, "inc-1")[0]
    assert row["detail"] == {"kind": "escalation", "attempts": 3, "last_status": 502}
    assert "teams_thread_id" not in deps.incidents.only()


@pytest.mark.req("7.4")
def test_without_primary_the_backup_is_mentioned_and_a_warning_is_recorded():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation(
        event(recipients={"primary": None, "backup": OTHER, "warnings": []}), deps, now=NOW
    )

    text = str(poster.calls[0][1])
    assert "beto-aad-id" in text and "ana-aad-id" not in text
    assert "NO_PRIMARY_ONCALL" in text
    assert result["mentioned"] == ["backup"]
    assert result["warnings"] == ["NO_PRIMARY_ONCALL"]
    assert event_rows(deps.events, "inc-1")[0]["detail"]["warnings"] == ["NO_PRIMARY_ONCALL"]


@pytest.mark.req("7.4")
def test_warning_from_resolve_on_call_is_not_duplicated():
    deps, _ = make(Poster(None))

    result = send_escalation(
        event(recipients={"backup": OTHER, "warnings": ["NO_PRIMARY_ONCALL"]}), deps, now=NOW
    )

    assert result["warnings"] == ["NO_PRIMARY_ONCALL"]


@pytest.mark.req("7.4")
def test_nobody_on_call_still_posts_the_card_with_the_schedule_warning():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation(
        event(recipients={"primary": None, "backup": None, "warnings": ["NO_ONCALL_SCHEDULE"]}),
        deps,
        now=NOW,
    )

    assert result["mentioned"] == []
    assert "NO_ONCALL_SCHEDULE" in str(poster.calls[0][1])


def test_missing_recipients_still_posts():
    poster = Poster(None)
    deps, _ = make(poster)

    result = send_escalation({"incident_id": "inc-1"}, deps, now=NOW)

    assert result["mentioned"] == [] and len(poster.calls) == 1


def test_parallel_remediation_changes_the_message():
    poster = Poster(None)
    deps, _ = make(poster)

    send_escalation(event(parallel=True), deps, now=bogota(5, 50))

    text = str(poster.calls[0][1])
    assert "10 min para las 06:00" in text
    assert "recupera en paralelo" in text


def test_a_second_invocation_does_not_post_again():
    poster = Poster("msg-1", "msg-2")
    deps, _ = make(poster)

    first = send_escalation(event(), deps, now=NOW)
    second = send_escalation(event(), deps, now=NOW)

    assert len(poster.calls) == 1
    assert second["posted"] is False
    assert second["teams_thread_id"] == first["teams_thread_id"] == "msg-1"
    assert event_types(deps.events, "inc-1") == ["ESCALATION_SENT"]


def test_data_comes_from_the_incident_row_not_from_the_event():
    poster = Poster(None)
    deps, _ = make(poster)

    send_escalation(
        event(organization="Spoofed", incident={"organization": "Spoofed"}), deps, now=NOW
    )

    assert "Spoofed" not in str(poster.calls[0][1])


def test_unknown_incident_is_an_error_and_nothing_is_posted():
    poster = Poster(None)
    deps, _ = make(poster)

    with pytest.raises(IncidentNotFoundError):
        send_escalation({"incident_id": "nope"}, deps, now=NOW)

    assert poster.calls == []


def test_incident_id_is_required():
    deps, _ = make(Poster(None))

    with pytest.raises(ValueError, match="incident_id"):
        send_escalation({"recipients": RECIPIENTS}, deps, now=NOW)


def test_the_webhook_address_is_not_written_to_the_audit_trail_or_the_result():
    deps, _ = make(Poster(fail(500), fail(500), fail(500)))

    with pytest.raises(EscalationDeliveryError) as err:
        send_escalation(event(), deps, now=NOW)

    assert "SUPER-SECRET" not in str(err.value)
    assert "SUPER-SECRET" not in str(deps.events.rows)
