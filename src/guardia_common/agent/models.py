"""Contracts of the agent: what it must answer, and what a provider adapter returns.

`AgentDecision` is closed on purpose (`extra="forbid"`, fixed actions): a
response that does not fit is discarded and the incident is escalated (4.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUBMIT_TOOL = "submit_decision"
Mode = Literal["classify", "diagnose"]
MODES: tuple[str, ...] = ("classify", "diagnose")
MAX_EVIDENCE_CHARS = 200

# Why an agent run produced no usable recommendation (AGENT_FAILED.reason).
TIMEOUT = "timeout"
INVALID_OUTPUT = "invalid_output"
PROVIDER_ERROR = "provider_error"
LOW_CONFIDENCE = "low_confidence"
NOT_CONFIGURED = "not_configured"


def _clean_evidence(items: list[str]) -> list[str]:
    for item in items:
        if len(item) > MAX_EVIDENCE_CHARS:
            raise ValueError(f"each evidence item must have at most {MAX_EVIDENCE_CHARS} chars")
    return items


class AgentDecision(BaseModel):
    """What the agent recommends for an incident the catalog does not know (4.5)."""

    model_config = ConfigDict(extra="forbid")

    classification: Literal["auto_recoverable", "requires_escalation"]
    suggested_action: Literal["redrive", "wait_and_redrive", "escalate"]
    wait_seconds: int = Field(default=0, ge=0, le=600)
    confidence: float = Field(ge=0, le=1)
    summary_es: str = Field(min_length=1, max_length=800)  # diagnosis for the Teams card
    evidence: list[str] = Field(default_factory=list, max_length=5)

    _evidence = field_validator("evidence")(_clean_evidence)

    @model_validator(mode="after")
    def _a_recovery_needs_a_recoverable_incident(self) -> AgentDecision:
        if (
            self.suggested_action in ("redrive", "wait_and_redrive")
            and self.classification != "auto_recoverable"
        ):
            raise ValueError("redrive and wait_and_redrive require classification auto_recoverable")
        return self


class AgentDiagnosis(BaseModel):
    """What the agent writes for an escalation whose classification is already known."""

    model_config = ConfigDict(extra="forbid")

    summary_es: str = Field(min_length=1, max_length=800)
    evidence: list[str] = Field(default_factory=list, max_length=5)

    _evidence = field_validator("evidence")(_clean_evidence)


# --------------------------------------------------------------- provider contract
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON schema of the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelTurn:
    """One answer of the model: tool calls, or the final text, and the tokens used."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class AgentError(RuntimeError):
    """Base class. Messages never contain a URL, a key, or any credential."""


class AgentProviderError(AgentError):
    """The provider failed or answered something that is not usable."""


class AgentTimeoutError(AgentProviderError):
    """The provider did not answer in the time left."""


class AgentConfigError(AgentError):
    """The provider is not configured (secret missing or malformed)."""


class ModelClient(Protocol):
    """Implemented by each adapter (Azure OpenAI, Bedrock, the scripted fake).

    `messages` use the neutral format of `port.run_agent`; the adapter translates it.
    `force_tool` makes the model answer by calling that tool (last allowed turn).
    """

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
        timeout_s: float = 30.0,
    ) -> ModelTurn: ...


@dataclass(frozen=True)
class AgentLimits:
    """Published to SSM (`agent-*`); the code only reads them."""

    timeout_seconds: float = 30.0
    max_turns: int = 6
    max_tool_output_chars: int = 4000
    min_confidence: float = 0.7

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, str]) -> AgentLimits:
        return cls(
            timeout_seconds=float(thresholds["agent-timeout-seconds"]),
            max_turns=int(thresholds["agent-max-turns"]),
            max_tool_output_chars=int(thresholds["agent-max-tool-output-chars"]),
            min_confidence=float(thresholds["agent-min-confidence"]),
        )


@dataclass
class AgentResult:
    ok: bool
    mode: str
    decision: AgentDecision | AgentDiagnosis | None = None
    reason: str | None = None
    turns: int = 0
    tools_used: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def decision_dict(self) -> dict | None:
        return self.decision.model_dump(mode="json") if self.decision else None
