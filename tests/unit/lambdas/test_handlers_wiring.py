"""The Lambda entry points build their AWS clients from environment variables."""

from __future__ import annotations

import pytest

from lambdas.classify_incident import handler as classify_module
from lambdas.open_incident import handler as open_module
from tests.unit.conftest import status_event


@pytest.fixture
def lambda_env(catalog_loaded, monkeypatch):
    monkeypatch.setenv("INCIDENTS_TABLE", "Incidents")
    monkeypatch.setenv("PROCESS_REGISTRY_TABLE", "ProcessRegistry")
    monkeypatch.setenv("INCIDENT_EVENTS_TABLE", "IncidentEvents")
    monkeypatch.setenv("ERROR_CATALOG_TABLE", "ErrorCatalog")
    monkeypatch.setenv("INCIDENT_FLOW_ARN", catalog_loaded["flow_arn"])
    open_module._aws_deps.cache_clear()
    classify_module._aws_deps.cache_clear()
    yield catalog_loaded
    open_module._aws_deps.cache_clear()
    classify_module._aws_deps.cache_clear()


@pytest.mark.req("2.1")
def test_open_then_classify_through_lambda_handlers(lambda_env):
    opened = open_module.handler(status_event(), None)
    classified = classify_module.handler({"incident_id": opened["incident_id"]}, None)

    assert opened["created"]
    assert classified["classification"] == "auto_recoverable"
    assert classified["action"] == "wait_and_redrive"
