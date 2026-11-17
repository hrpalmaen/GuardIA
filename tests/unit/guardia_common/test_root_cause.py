from __future__ import annotations

import pytest

from guardia_common.root_cause import extract_root_cause, fetch_history
from guardia_local.stepfunctions import ReplayStepFunctionsClient
from tests.unit.conftest import EMT12_OOM, EMTSAS_500, REAL_SUCCEEDED


def _root(real_executions, fixture_id):
    return extract_root_cause(real_executions.by_id(fixture_id).history_events())


@pytest.mark.req("2.3")
def test_genesys_500_behind_generic_ingestion_controller_error(real_executions):
    execution = real_executions.by_id(EMTSAS_500)
    assert execution.describe["error"] == "IngestionControllerError"  # what DescribeExecution says

    root = _root(real_executions, EMTSAS_500)

    assert root.error == "Exception"
    assert "500 Server Error" in root.message
    assert root.state_name == "BI-EMTSAS-GCRAW-IngestionControllerTask"
    assert root.event_type == "TaskFailed"
    assert root.task_failures == 1
    assert root.location.startswith('File "/var/task/ingestion_controller.py"')


@pytest.mark.req("2.3")
def test_out_of_memory_hidden_by_states_runtime(real_executions):
    execution = real_executions.by_id(EMT12_OOM)
    assert execution.describe["error"] == "States.Runtime"

    root = _root(real_executions, EMT12_OOM)

    assert root.error == "Runtime.OutOfMemory"
    assert root.state_name == "BI-EMT12-GCRAW-FetchRawConversationsTask"
    assert root.task_failures == 6  # retried inside the process before giving up
    assert root.event_id == 979  # the last failure, the one that failed the Map iteration


@pytest.mark.req("2.2")
@pytest.mark.parametrize("fixture_id", REAL_SUCCEEDED)
def test_successful_executions_have_no_root_cause(real_executions, fixture_id):
    assert _root(real_executions, fixture_id) is None


def test_execution_level_failure_when_no_task_failed():
    events = [
        {"id": 1, "previousEventId": 0, "type": "ExecutionStarted", "timestamp": None},
        {"id": 2, "previousEventId": 1, "type": "ExecutionTimedOut", "timestamp": None,
         "executionTimedOutEventDetails": {"error": "States.Timeout", "cause": None}},
    ]  # fmt: skip
    root = extract_root_cause(events)
    assert root.error == "States.Timeout"
    assert root.event_type == "ExecutionTimedOut"
    assert root.task_failures == 0


def test_empty_history():
    assert extract_root_cause([]) is None


def test_fetch_history_reads_every_page(real_executions):
    execution = real_executions.by_id(EMT12_OOM)  # 990 events
    client = ReplayStepFunctionsClient(real_executions)

    events = fetch_history(client, execution.execution_arn)

    assert len(events) == len(execution.events)
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
