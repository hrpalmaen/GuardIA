"""Amazon Bedrock adapter (workshop account), through the Converse API with `toolConfig`.

Uses the `bedrock-runtime` client that comes with boto3: no new dependencies and
no keys, only the Lambda's role (`bedrock:InvokeModel` on the configured model).
The model must support tool use in Converse and, for the forced final turn,
`toolChoice`; check the supported-models table before choosing it.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from guardia_common.agent.models import (
    AgentProviderError,
    AgentTimeoutError,
    ModelTurn,
    ToolCall,
    ToolSpec,
)

MAX_OUTPUT_TOKENS = 1024
TIMEOUT_CODES = {"ModelTimeoutException"}
EMPTY_TEXT = "(sin texto)"  # Converse rejects empty text blocks


def _tool_config(tools: list[ToolSpec], force_tool: str | None) -> dict:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {"json": dict(tool.parameters)},
                }
            }
            for tool in tools
        ],
        "toolChoice": {"tool": {"name": force_tool}} if force_tool else {"auto": {}},
    }


def to_converse(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """(system blocks, messages) in the Converse format. Consecutive tool results
    and user messages are merged: Converse needs roles to alternate."""
    system: list[dict] = []
    converted: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for message in messages:
        role = message["role"]
        if role == "system":
            system.append({"text": message["content"]})
        elif role == "user":
            add("user", [{"text": message["content"] or EMPTY_TEXT}])
        elif role == "assistant":
            blocks: list[dict] = []
            if message.get("content"):
                blocks.append({"text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": call["id"],
                            "name": call["name"],
                            "input": call.get("arguments") or {},
                        }
                    }
                )
            add("assistant", blocks or [{"text": EMPTY_TEXT}])
        elif role == "tool":
            add(
                "user",
                [
                    {
                        "toolResult": {
                            "toolUseId": message["tool_call_id"],
                            "content": [{"text": message["content"] or EMPTY_TEXT}],
                        }
                    }
                ],
            )
    return system, converted


class BedrockClient:
    def __init__(self, runtime: Any, model_id: str, *, max_tokens: int = MAX_OUTPUT_TOKENS):
        """`runtime` is `boto3.client("bedrock-runtime")`; build it with a short
        `read_timeout` and `retries={"max_attempts": 1}` (the loop owns the time)."""
        self._runtime = runtime
        self._model_id = model_id
        self._max_tokens = max_tokens

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
        timeout_s: float = 30.0,  # noqa: ARG002 - the client's read_timeout bounds each call
    ) -> ModelTurn:
        system, converted = to_converse(messages)
        try:
            response = self._runtime.converse(
                modelId=self._model_id,
                system=system,
                messages=converted,
                toolConfig=_tool_config(tools, force_tool),
                inferenceConfig={"temperature": 0, "maxTokens": self._max_tokens},
            )
        except (ReadTimeoutError, ConnectTimeoutError):
            raise AgentTimeoutError("Bedrock timed out") from None
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "unknown")
            if code in TIMEOUT_CODES:
                raise AgentTimeoutError("Bedrock timed out") from None
            raise AgentProviderError(f"Bedrock answered {code}") from None
        return self._parse(response)

    @staticmethod
    def _parse(response: dict) -> ModelTurn:
        try:
            blocks = response["output"]["message"]["content"]
        except (KeyError, TypeError):
            raise AgentProviderError("Bedrock answered without a message") from None
        text = "".join(block["text"] for block in blocks if "text" in block) or None
        calls = [
            ToolCall(
                id=str(block["toolUse"]["toolUseId"]),
                name=str(block["toolUse"]["name"]),
                arguments=block["toolUse"].get("input") or {},
            )
            for block in blocks
            if "toolUse" in block
        ]
        usage = response.get("usage") or {}
        return ModelTurn(
            text=text,
            tool_calls=calls,
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
        )
