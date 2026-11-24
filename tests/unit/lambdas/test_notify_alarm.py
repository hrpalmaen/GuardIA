"""NotifyAlarm (8.2 · 13.2): GuardIA's own alarms posted to Teams."""

from __future__ import annotations

import json

import pytest

from config import TEAMS_ALERTS_SECRET_NAME
from guardia_common.teams_client import TeamsPostError, build_alarm_card
from lambdas.notify_alarm.handler import AlarmNotificationError, Deps, notify_alarm
from tests.unit.fakes import WEBHOOK, FakeSecrets, card_text


def alarm(state: str = "ALARM", name: str = "GuardIA-Heartbeat", **extra) -> dict:
    return {
        "AlarmName": name,
        "AlarmDescription": "MissedRunWatchdog has not finished a cycle in 10 minutes.",
        "NewStateValue": state,
        "NewStateReason": "Threshold Crossed: no datapoints were received for 2 periods.",
        "StateChangeTime": "2026-11-24T14:15:00.000+0000",
        **extra,
    }


def sns_event(*messages) -> dict:
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {"Message": m if isinstance(m, str) else json.dumps(m)},
            }
            for m in messages
        ]
    }


class Poster:
    def __init__(self, error: Exception | None = None):
        self.sent: list[tuple[str, dict]] = []
        self.error = error

    def __call__(self, url: str, card: dict):
        self.sent.append((url, card))
        if self.error:
            raise self.error
        return None


def deps(poster: Poster) -> Deps:
    return Deps(secrets=FakeSecrets(), secret_id=TEAMS_ALERTS_SECRET_NAME, poster=poster)


@pytest.mark.req("13.2")
def test_an_alarm_is_posted_to_teams():
    poster = Poster()
    out = notify_alarm(sns_event(alarm()), deps(poster))

    assert out == {"posted": 1, "skipped": 0}
    url, card = poster.sent[0]
    assert url == WEBHOOK
    text = card_text(card)
    assert "GuardIA-Heartbeat" in text and "En alarma" in text
    assert "2026-11-24 09:15 (hora Colombia)" in text


@pytest.mark.req("13.2")
def test_ok_states_and_non_alarm_messages_are_skipped():
    poster = Poster()
    out = notify_alarm(sns_event(alarm(state="OK"), "not json", {"foo": 1}), deps(poster))
    assert out == {"posted": 0, "skipped": 3}
    assert poster.sent == []


@pytest.mark.req("13.2")
def test_teams_failure_raises_without_the_webhook_address():
    poster = Poster(error=TeamsPostError("Teams webhook answered HTTP 502", status=502))
    with pytest.raises(AlarmNotificationError) as info:
        notify_alarm(sns_event(alarm()), deps(poster))
    assert WEBHOOK not in str(info.value) and "sig=" not in str(info.value)
    assert info.value.__cause__ is None


def test_the_alarm_card_mentions_nobody_and_truncates_the_reason():
    card = build_alarm_card(alarm(NewStateReason="x" * 2000))
    content = card["attachments"][0]["content"]
    assert "msteams" not in content
    reason = next(f["value"] for f in content["body"][2]["facts"] if f["title"] == "Motivo")
    assert len(reason) == 500


def test_unparseable_state_change_time_is_left_out():
    poster = Poster()
    notify_alarm(sns_event(alarm(StateChangeTime="yesterday")), deps(poster))
    assert "Hora" not in card_text(poster.sent[0][1])
