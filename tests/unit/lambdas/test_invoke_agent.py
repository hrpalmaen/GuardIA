"""InvokeAgent (T8 · tasks 4.2, 4.3 · requirements 4.5, 4.6, 12.2, 12.3), with a scripted model."""

from __future__ import annotations

import json

import pytest

from guardia_common.agent.adapters.fake import ScriptedClient, submit_turn, tools_turn
from guardia_common.agent.models import AgentConfigError, AgentProviderError, AgentTimeoutError
from guardia_common.models import from_dynamo
from lambdas.invoke_agent.handler import Deps, IncidentNotFoundError, invoke_agent
from tests.unit.fakes import FakeSsm, FakeTable, bogota, event_rows, event_types, new_incident_item
from tests.unit.guardia_common.test_agent_tools import FakeCatalog, FakeSfn

TOKEN = "eyJhbGciOiJIUzI1NiJ9" + "C" * 80 + "x" * 10
CLASSIFY = {
    "classification": "auto_recoverable",
    "suggested_action": "wait_and_redrive",
    "wait_seconds": 90,
    "confidence": 0.9,
    "summary_es": "El servicio limitó las peticiones; conviene esperar y reintentar.",
    "evidence": ["429 en las últimas 2 ejecuciones"],
}
DIAGNOSE = {"summary_es": "Falló la ingesta por un campo nuevo.", "evidence": ["campo nuevo"]}


def make(turns, factory_error=None, **incident_extra):
    incidents = FakeTable("incident_id")
    incidents.put_row(new_incident_item("inc-1", classification="unknown", **incident_extra))
    events = FakeTable("incident_id", "sk")
    client = ScriptedClient(turns)

    def factory():
        if factory_error:
            raise factory_error
        return client

    deps = Deps(
        incidents=incidents,
        events=events,
        catalog=FakeCatalog([]),
        ssm=FakeSsm(),
        sfn=FakeSfn(),
        client_factory=factory,
        provider="azure_openai",
    )
    return deps, client


def run(deps, mode="classify", **event):
    return invoke_agent({"incident_id": "inc-1", "mode": mode, **event}, deps, now=bogota(4, 5))


@pytest.mark.req("4.5")
def test_classify_stores_the_recommendation_and_audits_it():
    deps, _ = make([tools_turn("get_failure_details"), submit_turn(**CLASSIFY)])

    out = run(deps)

    assert out["ok"] is True and out["reason"] is None
    assert out["decision"]["suggested_action"] == "wait_and_redrive"
    stored = deps.incidents.only()
    assert stored["agent_classification"] == "auto_recoverable"
    assert stored["agent_action"] == "wait_and_redrive"
    assert float(stored["agent_confidence"]) == 0.9 and stored["agent_wait_seconds"] == 90
    assert stored["agent_summary"].startswith("El servicio")
    assert stored["classification"] == "unknown"  # the catalog's answer is not rewritten
    assert event_types(deps.events, "inc-1") == ["AGENT_DECIDED"]


@pytest.mark.req("10.1")
def test_the_audit_records_the_usage_and_the_validated_decision_but_not_the_prompt():
    deps, client = make([tools_turn("get_failure_details"), submit_turn(**CLASSIFY)])

    run(deps)

    (row,) = from_dynamo(event_rows(deps.events, "inc-1"))
    detail = row["detail"]
    assert detail["mode"] == "classify" and detail["provider"] == "azure_openai"
    assert detail["turns"] == 2 and detail["tools_used"] == ["get_failure_details"]
    assert detail["input_tokens"] == 200 and detail["output_tokens"] == 60
    assert detail["decision"] == {**CLASSIFY}
    text = json.dumps(row)
    assert "instrucciones" not in text.lower() and "Incidente (datos" not in text
    assert row["actor"] == "InvokeAgent"
    assert client.calls  # the model was called, and none of its messages is in the audit


@pytest.mark.req("12.3")
def test_the_models_credentials_in_its_answer_are_redacted_before_they_are_stored():
    leaky = {**CLASSIFY, "summary_es": f"Falló con Bearer {TOKEN}"}
    deps, _ = make([submit_turn(**leaky)])

    run(deps)

    assert TOKEN not in json.dumps(deps.incidents.only(), default=str)
    assert TOKEN not in json.dumps(event_rows(deps.events, "inc-1"), default=str)


@pytest.mark.req("4.6")
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (AgentTimeoutError("slow"), "timeout"),
        (AgentProviderError("HTTP 500"), "provider_error"),
    ],
)
def test_a_model_failure_never_raises_it_audits_and_marks_the_incident(failure, reason):
    deps, _ = make([failure])

    out = run(deps)

    assert out["ok"] is False and out["reason"] == reason and out["decision"] is None
    assert deps.incidents.only()["agent_failure"] == reason
    assert "agent_classification" not in deps.incidents.only()
    (row,) = event_rows(deps.events, "inc-1")
    assert row["event_type"] == "AGENT_FAILED" and row["detail"]["reason"] == reason


@pytest.mark.req("4.6")
def test_an_invalid_answer_is_discarded():
    deps, _ = make([submit_turn(**{**CLASSIFY, "suggested_action": "drop_database"})])

    out = run(deps)

    assert out["ok"] is False and out["reason"] == "invalid_output"
    assert deps.incidents.only()["agent_failure"] == "invalid_output"


@pytest.mark.req("4.6")
def test_a_low_confidence_answer_is_audited_with_its_decision_but_not_stored_as_a_recommendation():
    deps, _ = make([submit_turn(**{**CLASSIFY, "confidence": 0.4})])

    out = run(deps)

    assert out["ok"] is False and out["reason"] == "low_confidence"
    stored = deps.incidents.only()
    assert stored["agent_failure"] == "low_confidence"
    assert "agent_classification" not in stored and "agent_summary" not in stored
    (row,) = from_dynamo(event_rows(deps.events, "inc-1"))
    assert row["detail"]["decision"]["confidence"] == 0.4


@pytest.mark.req("4.6")
def test_without_a_configured_provider_the_flow_still_gets_an_answer():
    deps, _ = make([], factory_error=AgentConfigError("no provider"))

    out = run(deps)

    assert out["ok"] is False and out["reason"] == "not_configured"
    assert deps.incidents.only()["agent_failure"] == "not_configured"


@pytest.mark.req("4.5")
def test_diagnose_stores_only_the_summary_and_keeps_the_classification():
    deps, _ = make([submit_turn(**DIAGNOSE)], classification_note="x")
    deps.incidents.only()["classification"] = "requires_escalation"
    deps.incidents.only()["catalog_action"] = "escalate"

    out = run(deps, mode="diagnose")

    stored = deps.incidents.only()
    assert out["ok"] is True and out["decision"]["summary_es"].startswith("Falló la ingesta")
    assert stored["agent_summary"] == DIAGNOSE["summary_es"]
    assert stored["classification"] == "requires_escalation"
    assert "agent_classification" not in stored and "agent_action" not in stored


def test_a_failed_diagnosis_leaves_no_marker_that_could_steer_the_policy():
    deps, _ = make([AgentProviderError("down")])

    out = run(deps, mode="diagnose")

    assert out["ok"] is False
    assert "agent_failure" not in deps.incidents.only()
    assert event_types(deps.events, "inc-1") == ["AGENT_FAILED"]


def test_it_does_not_pay_twice_for_the_same_diagnosis():
    deps, client = make([], agent_summary="Ya diagnosticado.")

    out = run(deps, mode="diagnose")

    assert out["ok"] is True and out["skipped"] == "already_diagnosed"
    assert out["decision"]["summary_es"] == "Ya diagnosticado."
    assert client.calls == [] and event_types(deps.events, "inc-1") == []


def test_a_retried_classification_does_not_call_the_model_again():
    deps, client = make([], agent_classification="auto_recoverable", agent_action="redrive")

    out = run(deps)

    assert out["ok"] is True and out["skipped"] == "already_classified" and client.calls == []


@pytest.mark.req("13.1")
@pytest.mark.parametrize("failure", ["timeout", "provider_error", "not_configured"])
def test_the_escalation_does_not_wait_again_for_a_provider_that_just_failed(failure):
    deps, client = make([], agent_failure=failure)

    out = run(deps, mode="diagnose")

    assert out["ok"] is False and out["skipped"] == "provider_down" and out["reason"] == failure
    assert client.calls == []


def test_a_low_confidence_classification_still_gets_a_diagnosis_for_the_card():
    deps, _ = make([submit_turn(**DIAGNOSE)], agent_failure="low_confidence")

    assert run(deps, mode="diagnose")["ok"] is True


def test_the_input_is_validated():
    deps, _ = make([])

    with pytest.raises(ValueError, match="incident_id"):
        invoke_agent({"mode": "classify"}, deps)
    with pytest.raises(ValueError, match="mode"):
        invoke_agent({"incident_id": "inc-1", "mode": "execute"}, deps)
    with pytest.raises(IncidentNotFoundError):
        invoke_agent({"incident_id": "nope", "mode": "classify"}, deps)


def test_the_limits_come_from_ssm():
    deps, client = make([submit_turn(**CLASSIFY)])
    deps.ssm = FakeSsm({"agent-min-confidence": "0.95"})

    out = run(deps)

    assert out["reason"] == "low_confidence"  # 0.9 < 0.95
    assert client.calls[0]["timeout_s"] <= 30
