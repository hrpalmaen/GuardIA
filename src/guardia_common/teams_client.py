"""Teams webhook client (task 6.2 · requirement 7.2).

Posts an adaptive card with mentions to the Workflows (Power Automate) flow
webhook. The webhook address comes from Secrets Manager and is never written
to logs, exceptions, or the card.

The raw error `cause` is deliberately not part of the card: it can contain
credentials or personal data. The card carries the short `error`, the
diagnosis summary produced for the incident, and the actions attempted.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# Limits keep the card inside Teams' payload size and readable on a phone.
MAX_ERROR = 300
MAX_SUMMARY = 800
MAX_ACTION = 200
MAX_ACTIONS = 8
ELLIPSIS = "…"

CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


class TeamsPostError(RuntimeError):
    """Teams did not accept the post. The message never contains the webhook address."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class WebhookConfigError(RuntimeError):
    """The webhook secret is missing or is not a valid https address."""


def truncate(text: Any, limit: int) -> str:
    value = "" if text is None else " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + ELLIPSIS


def _clean_name(name: str) -> str:
    # `<` and `>` would break the <at>…</at> mention markup.
    return re.sub(r"[<>]", "", name).strip()


def _mentions(recipients: dict) -> list[dict]:
    people = [recipients.get("primary"), recipients.get("backup")]
    return [p for p in people if p]


def build_escalation_card(
    *,
    incident: dict,
    recipients: dict,
    diagnosis: dict | None,
    minutes_remaining: int,
    deadline_label: str,
    parallel: bool = False,
    warnings: list[str] | None = None,
) -> dict:
    """Workflows webhook payload: an adaptive card with the diagnosis and mentions.

    `recipients` is the ResolveOnCall output. The on-call engineer is mentioned
    first; when there is none, the backup is mentioned instead (7.4).
    """
    diagnosis = diagnosis or {}
    people = _mentions(recipients)
    entities = []
    for person in people:
        name = _clean_name(person["display_name"])
        entities.append(
            {
                "type": "mention",
                "text": f"<at>{name}</at>",
                "mentioned": {"id": person["teams_user_id"], "name": name},
            }
        )

    if minutes_remaining >= 0:
        remaining = f"{minutes_remaining} min para las {deadline_label}"
    else:
        remaining = f"plazo vencido hace {-minutes_remaining} min (las {deadline_label})"

    actions = diagnosis.get("actions") or []
    if actions:
        shown = [
            truncate(f"{a.get('action', '?')}: {a.get('result', '?')}", MAX_ACTION)
            for a in actions[:MAX_ACTIONS]
        ]
        if len(actions) > MAX_ACTIONS:
            shown.append(f"… y {len(actions) - MAX_ACTIONS} más")
        actions_text = "; ".join(shown)
    elif parallel:
        actions_text = "GuardIA intenta recuperarlo ahora mismo"
    else:
        actions_text = "Ninguna (requiere criterio humano)"

    facts = [
        ("Organización", truncate(incident.get("organization"), MAX_ACTION)),
        ("Proceso", truncate(incident.get("process_type"), MAX_ACTION)),
        ("Error", truncate(incident.get("error") or incident.get("kind"), MAX_ERROR)),
        ("Acciones", actions_text),
        ("Tiempo restante", remaining),
    ]
    summary = truncate(diagnosis.get("summary"), MAX_SUMMARY)
    if summary:
        label = "Diagnóstico de GuardIA (IA)" if diagnosis.get("ai") else "Diagnóstico"
        facts.insert(3, (label, summary))

    mention_line = " ".join(e["text"] for e in entities)
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"GuardIA - Incidente {incident.get('incident_id', '')}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        }
    ]
    if mention_line:
        body.append({"type": "TextBlock", "text": mention_line, "wrap": True})
    for warning in warnings or []:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Aviso de configuración: {warning}",
                "color": "Warning",
                "wrap": True,
            }
        )
    body.append(
        {
            "type": "FactSet",
            "facts": [{"title": title, "value": value} for title, value in facts],
        }
    )
    body.append(
        {
            "type": "TextBlock",
            "text": (
                "Quedan pocos minutos: se avisa ahora y GuardIA recupera en paralelo."
                if parallel
                else "Se necesita criterio humano."
            ),
            "wrap": True,
        }
    )

    content: dict = {
        "$schema": CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if entities:
        content["msteams"] = {"entities": entities}
    return {
        "type": "message",
        "attachments": [{"contentType": CARD_CONTENT_TYPE, "content": content}],
    }


STATUS_MODES = ("reminder", "sla_breached", "resolved")
_STATUS_TITLES = {
    "reminder": "Recordatorio: el incidente sigue sin resolver",
    "sla_breached": "Se venció la hora límite sin resolver el incidente",
    "resolved": "Incidente resuelto",
}


def _mention_entities(people: list[dict]) -> list[dict]:
    entities = []
    for person in people:
        name = _clean_name(person["display_name"])
        entities.append(
            {
                "type": "mention",
                "text": f"<at>{name}</at>",
                "mentioned": {"id": person["teams_user_id"], "name": name},
            }
        )
    return entities


def build_status_card(
    *,
    mode: str,
    incident: dict,
    recipients: dict,
    minutes_remaining: int,
    deadline_label: str,
    reminder_number: int | None = None,
    resolved_by: str | None = None,
    duration_minutes: int | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Follow-up cards (6.5): reminder, SLA breached, and resolved.

    `reminder` and `sla_breached` mention the on-call engineer and the backup
    (8.1, 6.2). `resolved` mentions nobody. The webhook posts a new message
    each time, so every card repeats the incident id to find it in the channel.
    """
    if mode not in STATUS_MODES:
        raise ValueError(f"unknown status card mode '{mode}'")
    entities = [] if mode == "resolved" else _mention_entities(_mentions(recipients))

    title = _STATUS_TITLES[mode]
    if mode == "reminder" and reminder_number:
        title = f"{title} (recordatorio {reminder_number})"
    facts = [
        ("Organización", truncate(incident.get("organization"), MAX_ACTION)),
        ("Proceso", truncate(incident.get("process_type"), MAX_ACTION)),
        ("Error", truncate(incident.get("error") or incident.get("kind"), MAX_ERROR)),
    ]
    if mode == "reminder":
        facts.append(("Tiempo restante", f"{minutes_remaining} min para las {deadline_label}"))
    elif mode == "sla_breached":
        facts.append(("Hora límite", f"las {deadline_label}"))
        facts.append(("Retraso", f"{max(-minutes_remaining, 0)} min"))
    else:
        how = "una ejecución posterior terminó bien"
        if resolved_by and resolved_by != "later_execution":
            how = truncate(resolved_by, MAX_ACTION)
        facts.append(("Cómo se resolvió", how))
        if duration_minutes is not None:
            facts.append(("Duración del incidente", f"{max(duration_minutes, 0)} min"))

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"GuardIA - Incidente {incident.get('incident_id', '')}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "TextBlock", "text": title, "wrap": True},
    ]
    if entities:
        body.append(
            {"type": "TextBlock", "text": " ".join(e["text"] for e in entities), "wrap": True}
        )
    for warning in warnings or []:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Aviso de configuración: {warning}",
                "color": "Warning",
                "wrap": True,
            }
        )
    body.append({"type": "FactSet", "facts": [{"title": t, "value": v} for t, v in facts]})
    if mode == "sla_breached":
        body.append(
            {
                "type": "TextBlock",
                "text": "No habrá más recordatorios; GuardIA registra la resolución cuando ocurra.",
                "wrap": True,
            }
        )

    content: dict = {"$schema": CARD_SCHEMA, "type": "AdaptiveCard", "version": "1.4", "body": body}
    if entities:
        content["msteams"] = {"entities": entities}
    return {
        "type": "message",
        "attachments": [{"contentType": CARD_CONTENT_TYPE, "content": content}],
    }


def get_webhook_url(secrets_client: Any, secret_id: str) -> str:
    """Reads the webhook address. The secret is either the address itself or
    JSON with a `webhook_url` key. Errors never include the value."""
    try:
        raw = secrets_client.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    except Exception as exc:  # noqa: BLE001 - boto errors can echo request details
        raise WebhookConfigError(f"cannot read secret '{secret_id}'") from exc
    if raw.startswith("{"):
        try:
            raw = str(json.loads(raw).get("webhook_url", "")).strip()
        except ValueError as exc:
            raise WebhookConfigError(f"secret '{secret_id}' is not valid JSON") from exc
    if not raw.startswith("https://"):
        raise WebhookConfigError(f"secret '{secret_id}' does not hold an https webhook address")
    return raw


def post_card(
    webhook_url: str,
    payload: dict,
    *,
    timeout: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str | None:
    """POSTs the card. Returns the message id if the flow answers with one.

    Workflows webhooks answer 202 with no body unless the flow is built to
    reply, so the id is optional; callers must have a fallback.
    """
    if not webhook_url.startswith("https://"):
        raise TeamsPostError("Teams webhook address must be https")
    request = urllib.request.Request(  # noqa: S310 - https enforced above
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read() if hasattr(response, "read") else b""
    except urllib.error.HTTPError as exc:
        raise TeamsPostError(f"Teams webhook answered HTTP {exc.code}", status=exc.code) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        # Deliberately no `from exc`: the original message can include the address.
        raise TeamsPostError("Teams webhook unreachable") from None
    if not 200 <= int(status) < 300:
        raise TeamsPostError(f"Teams webhook answered HTTP {status}", status=int(status))
    return _message_id(body)


def _message_id(body: bytes) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        for key in ("id", "messageId", "message_id", "threadId", "thread_id"):
            if data.get(key):
                return str(data[key])
    return None


# --------------------------------------------------------------------------- alarms
MAX_ALARM_REASON = 500
_ALARM_STATE_LABELS = {
    "ALARM": "En alarma",
    "OK": "Normal",
    "INSUFFICIENT_DATA": "Sin datos suficientes",
}


def build_alarm_card(alarm: dict, *, time_label: str | None = None) -> dict:
    """GuardIA watching itself (requirement 13.2): a CloudWatch alarm as a card.

    `alarm` is the JSON message CloudWatch publishes to SNS. Nobody is
    mentioned: alarms are for the team that runs GuardIA, not the on-call
    engineer of the processes. The alarm reason only carries metric values.
    """
    state = str(alarm.get("NewStateValue", ""))
    facts = [
        ("Alarma", truncate(alarm.get("AlarmName"), MAX_ACTION)),
        ("Estado", _ALARM_STATE_LABELS.get(state, truncate(state, MAX_ACTION))),
        ("Qué vigila", truncate(alarm.get("AlarmDescription") or "—", MAX_SUMMARY)),
        ("Motivo", truncate(alarm.get("NewStateReason"), MAX_ALARM_REASON)),
    ]
    if time_label:
        facts.append(("Hora", time_label))

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "GuardIA - Alarma de GuardIA",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Attention",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "GuardIA detectó un problema en sí misma. Revisa CloudWatch.",
            "wrap": True,
        },
        {"type": "FactSet", "facts": [{"title": t, "value": v} for t, v in facts]},
    ]
    content: dict = {"$schema": CARD_SCHEMA, "type": "AdaptiveCard", "version": "1.4", "body": body}
    return {
        "type": "message",
        "attachments": [{"contentType": CARD_CONTENT_TYPE, "content": content}],
    }
