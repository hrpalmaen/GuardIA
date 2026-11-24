"""A scripted model, for tests and for local demos: no network, no credentials."""

from __future__ import annotations

import copy
import itertools

from guardia_common.agent.models import SUBMIT_TOOL, ModelTurn, ToolCall, ToolSpec

_ids = itertools.count(1)


def submit_turn(**arguments) -> ModelTurn:
    """The model answers with `submit_decision`."""
    return ModelTurn(
        tool_calls=[ToolCall(f"call-{next(_ids)}", SUBMIT_TOOL, arguments)],
        input_tokens=100,
        output_tokens=50,
    )


def tools_turn(*names: str) -> ModelTurn:
    """The model asks for these tools."""
    return ModelTurn(
        tool_calls=[ToolCall(f"call-{next(_ids)}", name, {}) for name in names],
        input_tokens=100,
        output_tokens=10,
    )


def text_turn(text: str) -> ModelTurn:
    return ModelTurn(text=text, input_tokens=100, output_tokens=10)


class ScriptedClient:
    """Plays `turns` in order. An `Exception` in the list is raised instead of returned."""

    def __init__(self, turns: list[ModelTurn | Exception]):
        self._turns = list(turns)
        self.calls: list[dict] = []

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
        timeout_s: float = 30.0,
    ) -> ModelTurn:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": [tool.name for tool in tools],
                "force_tool": force_tool,
                "timeout_s": timeout_s,
            }
        )
        if not self._turns:
            raise AssertionError("the scripted model has no more turns")
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn
