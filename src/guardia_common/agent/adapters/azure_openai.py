"""Azure OpenAI adapter (test account). Plain `urllib`, like `teams_client`: the
OpenAI SDK is not added to the layer.

The endpoint, the API version, the deployment name, and the key come from one
secret (`guardia/agent-azure-openai`). The code only knows the secret's name.
Errors never include the URL or the key.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guardia_common.agent.models import (
    AgentConfigError,
    AgentProviderError,
    AgentTimeoutError,
    ModelTurn,
    ToolCall,
    ToolSpec,
)

MAX_RETRY_AFTER_S = 10.0
REQUIRED = ("endpoint", "api_version", "deployment", "api_key")


@dataclass(frozen=True)
class AzureSettings:
    endpoint: str
    api_version: str
    deployment: str
    api_key: str = ""

    def __repr__(self) -> str:  # never print the key or the address
        return "AzureSettings(...)"

    @classmethod
    def from_secret_string(cls, text: str | None) -> AzureSettings:
        try:
            data = json.loads(text or "")
        except ValueError:
            raise AgentConfigError("the Azure OpenAI secret is not JSON") from None
        if not isinstance(data, dict):
            raise AgentConfigError("the Azure OpenAI secret is not a JSON object")
        missing = [key for key in REQUIRED if not str(data.get(key) or "").strip()]
        if missing:
            raise AgentConfigError(f"the Azure OpenAI secret has no value for: {missing}")
        if not str(data["endpoint"]).startswith("https://"):
            raise AgentConfigError("the Azure OpenAI endpoint must start with https://")
        return cls(*(str(data[key]).strip() for key in REQUIRED))


_settings_cache: dict[str, AzureSettings] = {}


def load_settings(secrets_client: Any, secret_id: str) -> AzureSettings:
    """Read once per container."""
    if secret_id not in _settings_cache:
        value = secrets_client.get_secret_value(SecretId=secret_id)
        _settings_cache[secret_id] = AzureSettings.from_secret_string(value.get("SecretString"))
    return _settings_cache[secret_id]


def clear_settings_cache() -> None:
    _settings_cache.clear()


# ------------------------------------------------------------------ message format
def _tool_payload(tool: ToolSpec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


def _message_payload(message: dict) -> dict:
    role = message["role"]
    if role == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
                for call in message["tool_calls"]
            ],
        }
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message["tool_call_id"],
            "content": message["content"],
        }
    return {"role": role, "content": message.get("content") or ""}


def _arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"_unparsed": True}  # the contract rejects it if it was `submit_decision`
    return parsed if isinstance(parsed, dict) else {"_unparsed": True}


class AzureOpenAIClient:
    def __init__(
        self,
        settings: AzureSettings,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._opener = opener
        self._sleep = sleep
        self._clock = clock

    @property
    def url(self) -> str:
        s = self._settings
        return (
            f"{s.endpoint.rstrip('/')}/openai/deployments/{urllib.parse.quote(s.deployment)}"
            f"/chat/completions?api-version={urllib.parse.quote(s.api_version)}"
        )

    def request_body(
        self, messages: list[dict], tools: list[ToolSpec], force_tool: str | None
    ) -> dict:
        choice: str | dict = (
            {"type": "function", "function": {"name": force_tool}} if force_tool else "auto"
        )
        return {
            "messages": [_message_payload(m) for m in messages],
            "tools": [_tool_payload(t) for t in tools],
            "tool_choice": choice,
            "temperature": 0,
        }

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
        timeout_s: float = 30.0,
    ) -> ModelTurn:
        started = self._clock()
        body = json.dumps(self.request_body(messages, tools, force_tool)).encode("utf-8")
        for attempt in (1, 2):
            remaining = timeout_s - (self._clock() - started)
            if remaining <= 0:
                raise AgentTimeoutError("no time left to call Azure OpenAI")
            try:
                return self._parse(self._post(body, remaining))
            except _RateLimited as limited:
                # One retry, only if the wait fits in the time left.
                wait = min(limited.retry_after, MAX_RETRY_AFTER_S)
                if attempt == 2 or wait >= timeout_s - (self._clock() - started):
                    raise AgentProviderError("Azure OpenAI answered HTTP 429") from None
                self._sleep(wait)
        raise AgentProviderError("Azure OpenAI did not answer")  # pragma: no cover

    def _post(self, body: bytes, timeout: float) -> dict:
        request = urllib.request.Request(  # noqa: S310 - https enforced in the settings
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "api-key": self._settings.api_key},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            if err.code == 429:
                raise _RateLimited(_retry_after(err)) from None
            raise AgentProviderError(f"Azure OpenAI answered HTTP {err.code}") from None
        except TimeoutError:
            raise AgentTimeoutError("Azure OpenAI timed out") from None
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError | socket.timeout):
                raise AgentTimeoutError("Azure OpenAI timed out") from None
            raise AgentProviderError("Azure OpenAI unreachable") from None  # no `from err`: URL
        except OSError:
            raise AgentProviderError("Azure OpenAI unreachable") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise AgentProviderError("Azure OpenAI answered something that is not JSON") from None
        if not isinstance(data, dict):
            raise AgentProviderError("Azure OpenAI answered an unexpected document")
        return data

    @staticmethod
    def _parse(data: dict) -> ModelTurn:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise AgentProviderError("Azure OpenAI answered without a message") from None
        calls = [
            ToolCall(
                id=str(call.get("id") or f"call-{index}"),
                name=str((call.get("function") or {}).get("name") or ""),
                arguments=_arguments((call.get("function") or {}).get("arguments")),
            )
            for index, call in enumerate(message.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return ModelTurn(
            text=message.get("content"),
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


class _RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        return max(0.0, float(err.headers.get("Retry-After", "1")))
    except (AttributeError, TypeError, ValueError):
        return 1.0
