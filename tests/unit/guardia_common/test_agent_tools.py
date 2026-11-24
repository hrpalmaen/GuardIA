"""The agent's read-only tools (T8 · requirements 4.5, 12.2, 12.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from guardia_common.agent.tools import build_toolbox
from guardia_common.tables import RESOURCE_ARN_INDEX
from tests.unit.fakes import FakeTable, new_incident_item

TOKEN = "eyJhbGciOiJIUzI1NiJ9" + "B" * 80 + "y" * 10
STATE_MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:genesys-master-orgA"
EXECUTION = "arn:aws:states:us-east-1:123456789012:execution:genesys-master-orgA:run-1"


def history(*, with_secret: bool = False) -> list[dict]:
    cause = json.dumps(
        {
            "errorType": "HTTPError",
            "errorMessage": "500 Server Error"
            + (f' "accessToken": "{TOKEN}"' if with_secret else ""),
            "stackTrace": ['File "/var/task/ingest.py", line 10, in call'],
        }
    )

    def entered(event_id: int, name: str, **extra) -> dict:
        details = {"name": name, **extra}
        return {"id": event_id, "type": "TaskStateEntered", "stateEnteredEventDetails": details}

    return [
        {"id": 1, "type": "ExecutionStarted",
         "executionStartedEventDetails": {"input": '{"customer": "Ana"}'}},
        entered(2, "Prepare", input='{"x": 1}'),
        entered(3, "Prepare"),
        entered(4, "Ingest", input="secret-input"),
        {"id": 5, "type": "TaskFailed", "previousEventId": 4,
         "taskFailedEventDetails": {"error": "HTTPError", "cause": cause}},
        {"id": 6, "type": "ExecutionFailed",
         "executionFailedEventDetails": {"error": "IngestError", "cause": "x"}},
    ]  # fmt: skip


class FakeSfn:
    def __init__(self, events=None, runs=None, details=None):
        self.events = history() if events is None else events
        self.runs = runs or []
        self.details = details or {}
        self.calls: list[tuple] = []

    def get_execution_history(self, **kwargs):
        self.calls.append(("history", kwargs["executionArn"]))
        return {"events": self.events}

    def list_executions(self, **kwargs):
        self.calls.append(("list", kwargs["stateMachineArn"], kwargs["maxResults"]))
        return {"executions": self.runs}

    def describe_execution(self, executionArn):  # noqa: N803
        self.calls.append(("describe", executionArn))
        return self.details.get(executionArn, {})


class FakeCatalog:
    def __init__(self, rows):
        self.rows = rows

    def scan(self, **kwargs):
        return {"Items": self.rows}


def item(**extra):
    return new_incident_item(
        "inc-1",
        execution_arn=EXECUTION,
        resource_arn=STATE_MACHINE,
        root_error="HTTPError",
        **extra,
    )


def toolbox(sfn=None, incidents=None, catalog=None, max_chars=4000, **extra):
    return build_toolbox(
        item(**extra),
        sfn=sfn or FakeSfn(),
        incidents=incidents or FakeTable("incident_id"),
        catalog=catalog or FakeCatalog([]),
        max_output_chars=max_chars,
    )


def call(box, name):
    return json.loads(box.run(name, {}))


def test_the_five_tools_take_no_arguments_from_the_model():
    specs = toolbox().specs()

    assert {s.name for s in specs} == {
        "get_failure_details",
        "get_execution_path",
        "get_recent_runs",
        "search_error_catalog",
        "get_similar_incidents",
    }
    for spec in specs:
        assert (
            spec.parameters["properties"] == {} and spec.parameters["additionalProperties"] is False
        )


@pytest.mark.req("4.5")
def test_failure_details_come_from_the_history_and_leave_out_the_step_input():
    result = call(toolbox(failed_state="Ingest"), "get_failure_details")

    assert result["failed_state"] == "Ingest"
    assert result["from_history"]["error"] == "HTTPError"
    assert result["from_history"]["message"] == "500 Server Error"
    assert result["from_history"]["state"] == "Ingest"
    assert "secret-input" not in json.dumps(result) and "Ana" not in json.dumps(result)


@pytest.mark.req("12.3")
def test_a_credential_in_the_history_never_reaches_the_model():
    box = toolbox(sfn=FakeSfn(history(with_secret=True)))

    assert TOKEN not in box.run("get_failure_details")


@pytest.mark.req("12.2")
def test_the_history_is_read_for_this_incidents_execution_only():
    sfn = FakeSfn()

    toolbox(sfn=sfn).run("get_failure_details", {"executionArn": "arn:other:execution"})

    assert sfn.calls == [("history", EXECUTION)]


def test_the_execution_path_is_only_state_names_without_repeats():
    result = call(toolbox(failed_state="Ingest"), "get_execution_path")

    assert result == {"states": ["Prepare", "Ingest"], "failed_state": "Ingest"}


def test_an_incident_without_execution_has_no_history_to_read():
    sfn = FakeSfn()
    box = build_toolbox(
        {**item(), "execution_arn": None},
        sfn=sfn,
        incidents=FakeTable("incident_id"),
        catalog=FakeCatalog([]),
    )

    assert call(box, "get_execution_path")["states"] == []
    assert sfn.calls == []


def test_recent_runs_describe_only_the_failed_ones_and_sanitize_their_cause():
    start = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
    runs = [
        {"name": "ok-1", "status": "SUCCEEDED", "executionArn": "arn:ok", "startDate": start,
         "stopDate": start + timedelta(minutes=30)},
        {"name": "bad-1", "status": "FAILED", "executionArn": "arn:bad", "startDate": start,
         "stopDate": start + timedelta(minutes=5)},
    ]  # fmt: skip
    details = {"arn:bad": {"error": "HTTPError", "cause": f'500 "accessToken": "{TOKEN}"'}}
    sfn = FakeSfn(runs=runs, details=details)

    text = toolbox(sfn=sfn).run("get_recent_runs")
    result = json.loads(text)

    assert [r["status"] for r in result["runs"]] == ["SUCCEEDED", "FAILED"]
    assert result["runs"][0]["duration_s"] == 1800
    assert result["runs"][1]["error"] == "HTTPError"
    assert TOKEN not in text and "arn:" not in text
    assert ("list", STATE_MACHINE, 10) in sfn.calls
    assert ("describe", "arn:ok") not in sfn.calls


def test_the_catalog_tool_returns_only_rows_of_this_process_type_and_the_wildcard():
    rows = [
        {"process_type": "master", "error_pattern": "429", "classification": "auto_recoverable",
         "action": "wait_and_redrive", "max_wait_s": 300},
        {"process_type": "*", "error_pattern": "403", "classification": "requires_escalation",
         "action": "escalate"},
        {"process_type": "other", "error_pattern": "OOM", "classification": "requires_escalation",
         "action": "escalate"},
    ]  # fmt: skip

    result = call(toolbox(catalog=FakeCatalog(rows)), "search_error_catalog")

    assert [e["error_pattern"] for e in result["entries"]] == ["429", "403"]
    assert result["entries"][0]["max_wait_s"] == 300


def test_similar_incidents_share_the_error_and_exclude_this_one():
    incidents = FakeTable("incident_id")
    rows = [(1, "HTTPError", "RESOLVED"), (2, "HTTPError", "ESCALATED"),
            (3, "OtherError", "RESOLVED"), (4, "HTTPError", "RESOLVED")]  # fmt: skip
    for number, error, status in rows:
        row = new_incident_item(
            f"inc-{number}", resource_arn=STATE_MACHINE, root_error=error, status=status,
            resolved_by="guardia_recovery" if status == "RESOLVED" else None,
        )  # fmt: skip
        row["opened_at"] = f"2026-11-0{number}T04:00:00-05:00"
        incidents.put_row(row)
    this = incidents.rows[("inc-1",)]
    box = build_toolbox(
        this, sfn=FakeSfn(), incidents=incidents, catalog=FakeCatalog([]), max_output_chars=4000
    )

    result = call(box, "get_similar_incidents")

    assert [r["status"] for r in result["incidents"]] == ["RESOLVED", "ESCALATED"]  # newest first
    assert all(r["error"] == "HTTPError" for r in result["incidents"])
    assert RESOURCE_ARN_INDEX == "by_resource_arn"


@pytest.mark.req("12.3")
def test_an_unknown_tool_is_answered_with_the_available_ones():
    result = call(toolbox(), "drop_table")

    assert result["error"] == "unknown_tool" and "get_recent_runs" in result["available"]


def test_a_failing_tool_answers_its_error_class_only():
    class Broken(FakeSfn):
        def list_executions(self, **kwargs):
            raise RuntimeError(
                f'AccessDenied for arn:aws:iam::123456789012:role/x "accessToken": "{TOKEN}"'
            )

    text = toolbox(sfn=Broken()).run("get_recent_runs")

    assert json.loads(text) == {"error": "tool_failed", "reason": "RuntimeError"}
    assert TOKEN not in text and "123456789012" not in text


def test_the_output_is_cut_to_the_limit():
    rows = [
        {"process_type": "*", "error_pattern": f"pattern-{n}" * 20,
         "classification": "auto_recoverable", "action": "redrive"}
        for n in range(50)
    ]  # fmt: skip

    text = toolbox(catalog=FakeCatalog(rows), max_chars=500).run("search_error_catalog")

    assert len(text) < 600 and "truncated" in text
