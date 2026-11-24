"""The agent's contracts (T8 · requirements 4.5, 4.6, 12.2): closed, and strict about ranges."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from guardia_common.agent.models import AgentDecision, AgentDiagnosis, AgentLimits

VALID = {
    "classification": "auto_recoverable",
    "suggested_action": "redrive",
    "confidence": 0.85,
    "summary_es": "El paso falló con un 500 transitorio de Genesys.",
    "evidence": ["las 3 ejecuciones anteriores terminaron bien"],
}


def decision(**overrides) -> AgentDecision:
    return AgentDecision.model_validate({**VALID, **overrides})


@pytest.mark.req("4.5")
def test_a_valid_decision_is_accepted_and_wait_defaults_to_zero():
    result = decision()

    assert result.suggested_action == "redrive" and result.wait_seconds == 0


@pytest.mark.req("4.6")
@pytest.mark.parametrize(
    "overrides",
    [
        {"suggested_action": "delete_everything"},  # an action outside the list
        {"classification": "unknown"},  # the agent must pick one of two
        {"extra_field": "x"},  # extra="forbid"
        {"wait_seconds": 601},
        {"wait_seconds": -1},
        {"confidence": 1.01},
        {"confidence": -0.1},
        {"summary_es": ""},
        {"summary_es": "x" * 801},
        {"evidence": ["a"] * 6},
        {"evidence": ["x" * 201]},
    ],
)
def test_a_response_outside_the_contract_is_rejected(overrides):
    with pytest.raises(ValidationError):
        decision(**overrides)


@pytest.mark.req("12.2")
@pytest.mark.parametrize("action", ["redrive", "wait_and_redrive"])
def test_a_recovery_needs_an_auto_recoverable_incident(action):
    with pytest.raises(ValidationError):
        decision(classification="requires_escalation", suggested_action=action)


def test_escalating_an_auto_recoverable_incident_is_allowed():
    assert decision(suggested_action="escalate").suggested_action == "escalate"


def test_the_diagnosis_has_only_a_summary_and_evidence():
    assert AgentDiagnosis.model_validate({"summary_es": "texto"}).evidence == []
    with pytest.raises(ValidationError):
        AgentDiagnosis.model_validate({"summary_es": "texto", "classification": "auto_recoverable"})


def test_the_limits_come_from_the_ssm_thresholds():
    limits = AgentLimits.from_thresholds(
        {
            "agent-timeout-seconds": "12",
            "agent-max-turns": "3",
            "agent-max-tool-output-chars": "500",
            "agent-min-confidence": "0.9",
        }
    )

    assert limits == AgentLimits(12.0, 3, 500, 0.9)
