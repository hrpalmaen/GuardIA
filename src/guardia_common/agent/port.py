"""The agent loop (`AgentPort`), independent of the model provider.

1. The system instructions and the sanitized incident go to the model.
2. If the model asks for tools, they run here (in the Lambda), their output is
   sanitized and cut, and it goes back to the model.
3. The loop ends when the model calls `submit_decision`, whose arguments are
   validated against the closed contract (`AgentDecision` or `AgentDiagnosis`).

Nothing here raises for a model failure: the result says `ok=False` and why
(timeout, invalid_output, provider_error, low_confidence), and the flow
escalates (4.6). The model never executes an action (12.2).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from pydantic import ValidationError

from guardia_common.logger import get_logger, log
from guardia_common.redaction import redact_text

from .context import incident_context
from .models import (
    INVALID_OUTPUT,
    LOW_CONFIDENCE,
    PROVIDER_ERROR,
    SUBMIT_TOOL,
    TIMEOUT,
    AgentDecision,
    AgentDiagnosis,
    AgentLimits,
    AgentProviderError,
    AgentResult,
    AgentTimeoutError,
    Mode,
    ModelClient,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from .prompts import NUDGE, SUBMIT_DESCRIPTION, SUBMIT_SCHEMAS, system_prompt, user_prompt
from .tools import ToolBox

LOGGER = get_logger("guardia.agent")
MAX_CALLS_PER_TURN = 6


def submit_spec(mode: Mode) -> ToolSpec:
    return ToolSpec(SUBMIT_TOOL, SUBMIT_DESCRIPTION, SUBMIT_SCHEMAS[mode])


def _clean(model: AgentDecision | AgentDiagnosis) -> AgentDecision | AgentDiagnosis:
    """The model's own words go through the redaction too, before they are stored or posted."""
    return model.model_copy(
        update={
            "summary_es": redact_text(model.summary_es),
            "evidence": [redact_text(item) for item in model.evidence],
        }
    )


def run_agent(
    incident: dict,
    mode: Mode,
    client: ModelClient,
    tools: ToolBox,
    limits: AgentLimits,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """`incident` is the stored Incidents row. Returns the validated recommendation."""
    started = clock()
    result = AgentResult(ok=False, mode=mode)

    def finish(reason: str | None = None, decision=None) -> AgentResult:
        result.reason = reason
        result.decision = decision
        result.ok = reason is None and decision is not None
        result.latency_ms = int((clock() - started) * 1000)
        return result

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(mode)},
        {"role": "user", "content": user_prompt(incident_context(incident))},
    ]
    specs = [*tools.specs(), submit_spec(mode)]

    for turn_number in range(1, limits.max_turns + 1):
        remaining = limits.timeout_seconds - (clock() - started)
        if remaining <= 0:
            return finish(TIMEOUT)
        last_turn = turn_number == limits.max_turns
        try:
            turn: ModelTurn = client.complete(
                messages,
                specs,
                force_tool=SUBMIT_TOOL if last_turn else None,
                timeout_s=remaining,
            )
        except AgentTimeoutError:
            return finish(TIMEOUT)
        except AgentProviderError:
            return finish(PROVIDER_ERROR)
        except Exception as err:  # an adapter bug must not break the flow either
            log(LOGGER, logging.ERROR, "Agent adapter failed", error=type(err).__name__)
            return finish(PROVIDER_ERROR)

        result.turns += 1
        result.input_tokens += turn.input_tokens
        result.output_tokens += turn.output_tokens

        submitted = next((c for c in turn.tool_calls if c.name == SUBMIT_TOOL), None)
        if submitted is not None:
            return _validated(result, submitted, mode, limits, finish)

        if not turn.tool_calls:
            messages.append({"role": "assistant", "content": turn.text or ""})
            messages.append({"role": "user", "content": NUDGE})
            continue

        calls = turn.tool_calls[:MAX_CALLS_PER_TURN]
        messages.append(
            {
                "role": "assistant",
                "content": turn.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls
                ],
            }
        )
        for call in calls:
            result.tools_used.append(call.name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": tools.run(call.name, call.arguments),
                }
            )

    return finish(INVALID_OUTPUT)  # it never submitted a decision


def _validated(result, call: ToolCall, mode: Mode, limits: AgentLimits, finish) -> AgentResult:
    contract = AgentDecision if mode == "classify" else AgentDiagnosis
    try:
        decision = _clean(contract.model_validate(call.arguments))
    except ValidationError:
        return finish(INVALID_OUTPUT)
    if isinstance(decision, AgentDecision) and decision.confidence < limits.min_confidence:
        return finish(LOW_CONFIDENCE, decision)
    return finish(None, decision)
