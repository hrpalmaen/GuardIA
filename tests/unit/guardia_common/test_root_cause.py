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


# ------------------------------------------------------------ redrive usefulness
from guardia_common.root_cause import redrive_reruns_root_cause  # noqa: E402


def _ev(event_id: int, type_: str, prev: int | None = None, **details) -> dict:
    event = {"id": event_id, "type": type_, "previousEventId": prev or event_id - 1}
    if type_.endswith("StateEntered"):
        event["stateEnteredEventDetails"] = {"name": details.pop("name")}
    if details:
        key = {"TaskFailed": "taskFailedEventDetails"}.get(type_, "executionFailedEventDetails")
        event[key] = details
    return event


@pytest.mark.req("5.1")
def test_redrive_would_reenter_the_fail_state_of_emtsas(real_executions):
    events = real_executions.by_id(EMTSAS_500).history_events()
    assert redrive_reruns_root_cause(events) is False


@pytest.mark.req("5.1")
def test_redrive_would_repeat_the_jsonpath_failure_of_emt12(real_executions):
    events = real_executions.by_id(EMT12_OOM).history_events()
    assert redrive_reruns_root_cause(events) is False


def test_redrive_reruns_an_uncaught_task_failure():
    events = [
        _ev(1, "ExecutionStarted"),
        _ev(2, "TaskStateEntered", name="Fetch"),
        _ev(3, "TaskFailed", error="Genesys.ApiError", cause="429"),
        _ev(4, "ExecutionFailed", error="Genesys.ApiError", cause="429"),
    ]
    assert redrive_reruns_root_cause(events) is True


def test_redrive_reruns_failed_iterations_of_an_uncaught_map():
    events = [
        _ev(1, "ExecutionStarted"),
        _ev(2, "MapStateEntered", name="FetchRawDataMap"),
        _ev(3, "TaskStateEntered", name="FetchRawConversationsTask"),
        _ev(4, "TaskFailed", error="Runtime.OutOfMemory"),
        _ev(5, "ExecutionFailed", error="States.ExceedToleratedFailureThreshold"),
    ]
    # the last state entered before the end is the failed task inside the map
    assert redrive_reruns_root_cause(events) is True
    # or the map itself, when the failure happened inside it
    assert redrive_reruns_root_cause([events[0], events[1], events[3], events[4]]) is True


def test_execution_timeout_resumes_where_it_stopped():
    events = [
        _ev(1, "ExecutionStarted"),
        _ev(2, "TaskStateEntered", name="Fetch"),
        {"id": 3, "type": "ExecutionTimedOut", "previousEventId": 2,
         "executionTimedOutEventDetails": {"error": "States.Timeout"}},
    ]  # fmt: skip
    assert redrive_reruns_root_cause(events) is True


@pytest.mark.parametrize("fixture_id", REAL_SUCCEEDED)
def test_succeeded_executions_have_nothing_to_redrive(real_executions, fixture_id):
    assert redrive_reruns_root_cause(real_executions.by_id(fixture_id).history_events()) is False
