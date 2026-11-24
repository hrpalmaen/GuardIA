"""InvokeAgent (T8, tasks 4.2 and 4.3 · requirements 4.5, 4.6, 12.2).

Runs the AI agent on one incident and records what it recommended. The agent
only recommends: it never executes anything, and RemediationPolicy decides
what GuardIA does with the recommendation.

Event:  {"incident_id": "...", "mode": "classify" | "diagnose"}
Output: {"ok": bool, "mode", "decision": {...} | null, "reason": str | null}

* `classify`: for an incident the catalog does not know. Stores
  `agent_classification`, `agent_action`, `agent_confidence`,
  `agent_wait_seconds`, and `agent_summary` in the incident.
* `diagnose`: for an escalation. Stores only `agent_summary`, which
  SendEscalation shows in the Teams card. The classification is not changed.

It never raises because the model failed (timeout, invalid answer, provider
down, low confidence): the output says `ok=false`, the audit has
`AGENT_FAILED`, and the flow escalates without the AI diagnosis (4.6).
The audit keeps the validated decision and the usage, never the prompt or the
model's full answer.

Environment:
    INCIDENTS_TABLE, INCIDENT_EVENTS_TABLE, ERROR_CATALOG_TABLE
    AGENT_PROVIDER   azure_openai | bedrock
    AGENT_SECRET_ID  (azure_openai) name of the secret that holds the settings
    AGENT_MODEL      (bedrock) model id
    GUARDIA_SSM_PREFIX (optional, default /guardia/config)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from typing import Any

import boto3
from botocore.config import Config

from guardia_common.agent.adapters import AZURE_OPENAI, BEDROCK, build_client
from guardia_common.agent.models import (
    MODES,
    NOT_CONFIGURED,
    PROVIDER_ERROR,
    TIMEOUT,
    AgentConfigError,
    AgentDecision,
    AgentLimits,
    AgentResult,
    ModelClient,
)
from guardia_common.agent.port import run_agent
from guardia_common.agent.tools import build_toolbox
from guardia_common.audit import AuditRepository
from guardia_common.config import load_thresholds
from guardia_common.logger import get_logger, log

LOGGER = get_logger("guardia.invoke_agent")
ACTOR = "InvokeAgent"
# After one of these, the provider is not worth calling again for the same incident:
# the escalation must not wait another full timeout for a diagnosis (13.1).
PROVIDER_DOWN = {TIMEOUT, PROVIDER_ERROR, NOT_CONFIGURED}


class IncidentNotFoundError(LookupError):
    """The flow sent an incident_id that is not in the Incidents table."""


@dataclass
class Deps:
    incidents: Any
    events: Any
    catalog: Any
    ssm: Any
    sfn: Any
    client_factory: Callable[[], ModelClient]
    provider: str | None = None
    model: str | None = None  # recorded in the audit; empty for Azure (the deployment is secret)


def _output(result: AgentResult, **extra: Any) -> dict:
    return {
        "ok": result.ok,
        "mode": result.mode,
        "decision": result.decision_dict(),
        "reason": result.reason,
        **extra,
    }


def _skipped(mode: str, ok: bool, decision: dict | None, reason: str | None, why: str) -> dict:
    return {"ok": ok, "mode": mode, "decision": decision, "reason": reason, "skipped": why}


def invoke_agent(event: dict, deps: Deps, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    incident_id = event.get("incident_id") or (event.get("incident") or {}).get("incident_id")
    if not incident_id:
        raise ValueError("InvokeAgent requires incident_id")
    mode = event.get("mode")
    if mode not in MODES:
        raise ValueError(f"InvokeAgent mode must be one of {list(MODES)}, not {mode!r}")

    item = deps.incidents.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        raise IncidentNotFoundError(incident_id)

    skipped = _already_done(item, mode)
    if skipped:
        log(
            LOGGER,
            logging.INFO,
            "Agent not run again",
            incident_id=incident_id,
            why=skipped["skipped"],
        )
        return skipped

    limits = AgentLimits.from_thresholds(load_thresholds(deps.ssm))
    try:
        client = deps.client_factory()
    except AgentConfigError:
        result = AgentResult(ok=False, mode=mode, reason=NOT_CONFIGURED)
    else:
        tools = build_toolbox(
            item,
            sfn=deps.sfn,
            incidents=deps.incidents,
            catalog=deps.catalog,
            max_output_chars=limits.max_tool_output_chars,
        )
        result = run_agent(item, mode, client, tools, limits)

    _audit(deps, incident_id, result, now)
    _store(deps, incident_id, result)
    log(
        LOGGER,
        logging.INFO,
        "Agent finished",
        incident_id=incident_id,
        mode=mode,
        ok=result.ok,
        reason=result.reason,
        turns=result.turns,
        tools=result.tools_used,
        latency_ms=result.latency_ms,
    )
    return _output(result)


def _already_done(item: dict, mode: str) -> dict | None:
    """Step Functions can retry a state, and the escalation asks for a diagnosis even
    when the recovery flow already got one: do not pay for the same answer twice."""
    if mode == "classify" and item.get("agent_classification"):
        return _skipped(mode, True, None, None, "already_classified")
    if mode == "diagnose" and item.get("agent_summary"):
        summary = {"summary_es": item["agent_summary"], "evidence": []}
        return _skipped(mode, True, summary, None, "already_diagnosed")
    if mode == "diagnose" and item.get("agent_failure") in PROVIDER_DOWN:
        return _skipped(mode, False, None, item["agent_failure"], "provider_down")
    return None


def _audit(deps: Deps, incident_id: str, result: AgentResult, now: datetime) -> None:
    detail = {
        "mode": result.mode,
        "provider": deps.provider,
        "model": deps.model,
        "turns": result.turns,
        "tools_used": result.tools_used,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "decision": result.decision_dict(),
    }
    if result.ok:
        event_type = "AGENT_DECIDED"
    else:
        event_type = "AGENT_FAILED"
        detail["reason"] = result.reason
    AuditRepository(deps.events, actor=ACTOR).append(incident_id, event_type, detail, now=now)


def _store(deps: Deps, incident_id: str, result: AgentResult) -> None:
    values: dict[str, Any] = {}
    decision = result.decision
    if result.ok and isinstance(decision, AgentDecision):
        values = {
            "agent_classification": decision.classification,
            "agent_action": decision.suggested_action,
            "agent_confidence": Decimal(str(decision.confidence)),
            "agent_wait_seconds": decision.wait_seconds,
            "agent_summary": decision.summary_es,
        }
    elif result.ok and decision is not None:
        values = {"agent_summary": decision.summary_es}
    elif result.mode == "classify":
        values = {"agent_failure": result.reason}
    if not values:
        return
    names = list(values)
    deps.incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET " + ", ".join(f"{name} = :{name}" for name in names),
        ConditionExpression="attribute_exists(incident_id)",
        ExpressionAttributeValues={f":{name}": values[name] for name in names},
    )


@cache
def _aws_deps() -> Deps:
    provider = os.environ.get("AGENT_PROVIDER") or None
    model = os.environ.get("AGENT_MODEL") or None
    dynamodb = boto3.resource("dynamodb")

    def client_factory() -> ModelClient:
        if provider == AZURE_OPENAI:
            return build_client(
                provider,
                secrets=boto3.client("secretsmanager"),
                secret_id=os.environ.get("AGENT_SECRET_ID"),
            )
        if provider == BEDROCK:
            runtime = boto3.client(
                "bedrock-runtime",
                config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 1}),
            )
            return build_client(provider, bedrock_runtime=runtime, model_id=model)
        raise AgentConfigError("no agent provider is configured")

    return Deps(
        incidents=dynamodb.Table(os.environ["INCIDENTS_TABLE"]),
        events=dynamodb.Table(os.environ["INCIDENT_EVENTS_TABLE"]),
        catalog=dynamodb.Table(os.environ["ERROR_CATALOG_TABLE"]),
        ssm=boto3.client("ssm"),
        sfn=boto3.client("stepfunctions"),
        client_factory=client_factory,
        provider=provider,
        model=model if provider == BEDROCK else None,
    )


def handler(event: dict, context: Any) -> dict:
    return invoke_agent(event, _aws_deps())
