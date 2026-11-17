"""OpenIncident and ClassifyIncident on the real executions, through the replay client.

The handlers are the production code; only `Deps.sfn` changes.
"""

from __future__ import annotations

import pytest

from guardia_local.events import status_change_event
from guardia_local.stepfunctions import ReplayStepFunctionsClient
from lambdas.classify_incident.handler import Deps as ClassifyDeps
from lambdas.classify_incident.handler import classify_incident
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident
from tests.unit.conftest import (
    EMT12_OOM,
    EMTSAS_500,
    REAL_SUCCEEDED,
    bogota,
    registry_rows,
)


@pytest.fixture
def run(catalog_loaded, real_executions):
    aws = catalog_loaded
    t = aws["tables"]
    for row in registry_rows(real_executions):
        t["ProcessRegistry"].put_item(Item=row)
    sfn = ReplayStepFunctionsClient(real_executions, fallback=aws["sfn"])
    open_deps = OpenDeps(
        t["Incidents"], t["ProcessRegistry"], t["IncidentEvents"], sfn, aws["ssm"], aws["flow_arn"]
    )
    classify_deps = ClassifyDeps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"])

    def _run(fixture_id: str):
        describe = real_executions.by_id(fixture_id).describe_response()
        opened = open_incident(status_change_event(describe), open_deps, now=bogota(4, 0))
        if not opened["opened"]:
            return opened, None, None
        item = t["Incidents"].get_item(Key={"incident_id": opened["incident_id"]})["Item"]
        classified = classify_incident(
            {"incident_id": opened["incident_id"]}, classify_deps, now=bogota(4, 0)
        )
        return opened, item, classified

    return _run


@pytest.mark.req("4.1")
def test_genesys_500_is_auto_recoverable_by_its_root_cause(run):
    opened, item, out = run(EMTSAS_500)

    assert item["organization"] == "EMTSAS" and item["process_type"] == "conversations"
    assert item["error"] == "IngestionControllerError"  # the final error alone matches nothing
    assert item["root_error"] == "Exception"
    assert item["failed_state"] == "BI-EMTSAS-GCRAW-IngestionControllerTask"
    assert out["classification"] == "auto_recoverable"
    assert out["action"] == "redrive"
    assert opened["incident"]["root_cause"].startswith("Falló consulta de hits")


@pytest.mark.req("4.1")
def test_out_of_memory_escalates_even_though_the_final_error_is_states_runtime(run):
    _, item, out = run(EMT12_OOM)

    assert item["error"] == "States.Runtime"
    assert item["root_error"] == "Runtime.OutOfMemory"
    assert item["failed_state"] == "BI-EMT12-GCRAW-FetchRawConversationsTask"
    assert out["classification"] == "requires_escalation"
    assert out["action"] == "escalate"


@pytest.mark.req("2.3")
def test_stored_cause_is_sanitized_and_bounded(run):
    _, item, _ = run(EMT12_OOM)

    assert len(item["cause"]) < 400  # the raw cause is ~21 KB
    assert "<omitted>" in item["cause"]
    assert "accessToken" not in item["cause"]


@pytest.mark.req("2.3")
def test_root_cause_is_recorded_in_the_audit_trail(run, catalog_loaded):
    opened, _, _ = run(EMTSAS_500)
    events = catalog_loaded["tables"]["IncidentEvents"].query(
        KeyConditionExpression="incident_id = :i",
        ExpressionAttributeValues={":i": opened["incident_id"]},
    )["Items"]
    opened_event = next(e for e in events if e["event_type"] == "INCIDENT_OPENED")

    root = opened_event["detail"]["root_cause"]
    assert root["error"] == "Exception"
    assert root["state_name"] == "BI-EMTSAS-GCRAW-IngestionControllerTask"


@pytest.mark.req("2.2")
@pytest.mark.parametrize("fixture_id", REAL_SUCCEEDED)
def test_real_successful_executions_open_nothing(run, fixture_id):
    opened, _, _ = run(fixture_id)
    assert opened == {"opened": False, "reason": "ignored_event"}
