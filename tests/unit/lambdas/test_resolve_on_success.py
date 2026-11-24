"""ResolveOnSuccess (T5 · task 6.4 · requirements 8.3, 2.2)."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from lambdas.resolve_on_success.handler import Deps, resolve_on_success
from tests.unit.conftest import epoch_ms
from tests.unit.fakes import FakeTable, bogota, event_rows, new_incident_item

MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:genesys-master-orgA"
EXECUTION = "arn:aws:states:us-east-1:123456789012:execution:genesys-master-orgA:run-2"


class FakeSfn:
    def __init__(self, error_code: str | None = None):
        self.calls: list[dict] = []
        self.error_code = error_code

    def send_task_success(self, taskToken, output):  # noqa: N803
        self.calls.append({"token": taskToken, "output": json.loads(output)})
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "SendTask")


def success_event(stop=None, machine=MACHINE) -> dict:
    return {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "status": "SUCCEEDED",
            "stateMachineArn": machine,
            "executionArn": EXECUTION,
            "stopDate": epoch_ms(stop or bogota(5, 0)),
        },
    }


def make(*incidents: dict, registry=True, sfn=None) -> Deps:
    table = FakeTable("incident_id")
    for item in incidents:
        table.put_row(item)
    process = FakeTable("resource_arn")
    if registry:
        process.put_row({"resource_arn": MACHINE, "organization": "OrgA", "process_type": "master"})
    return Deps(
        incidents=table,
        registry=process,
        events=FakeTable("incident_id", "sk"),
        sfn=sfn or FakeSfn(),
    )


def escalated(incident_id="inc-1", status="ESCALATED", token="tok-1", **extra) -> dict:  # noqa: S107
    return new_incident_item(incident_id, status=status, task_token=token, **extra)


@pytest.mark.req("8.3")
def test_a_later_success_signals_the_waiting_incident_with_its_token():
    deps = make(escalated())

    result = resolve_on_success(success_event(), deps, now=bogota(5, 0))

    assert result == {"signalled": ["inc-1"], "ignored": []}
    assert deps.sfn.calls == [
        {"token": "tok-1", "output": {"resolved_by": "later_execution", "execution_arn": EXECUTION}}
    ]
    (row,) = event_rows(deps.events, "inc-1")
    assert row["event_type"] == "RESOLVE_SIGNALLED"


@pytest.mark.req("8.3")
def test_an_sla_breached_incident_is_also_closed():
    deps = make(escalated(status="SLA_BREACHED"))

    assert resolve_on_success(success_event(), deps, now=bogota(7, 0))["signalled"] == ["inc-1"]


@pytest.mark.req("2.2")
def test_a_success_of_a_process_that_is_not_monitored_is_ignored():
    deps = make(escalated(), registry=False)

    result = resolve_on_success(success_event(), deps, now=bogota(5, 0))

    assert result["signalled"] == [] and result["ignored"][0]["reason"] == "process_not_monitored"
    assert deps.sfn.calls == []


@pytest.mark.req("2.2")
def test_no_incident_or_an_incident_that_is_not_waiting_is_ignored():
    open_status = make(escalated(status="OPEN"))
    resolved = make(escalated(status="RESOLVED"))
    nothing = make()

    for deps in (open_status, resolved, nothing):
        result = resolve_on_success(success_event(), deps, now=bogota(5, 0))
        assert result["signalled"] == []
        assert result["ignored"] == [{"incident_id": None, "reason": "no_open_incident"}]
        assert deps.sfn.calls == []


@pytest.mark.req("8.3")
def test_an_incident_of_another_organization_is_not_closed():
    deps = make(escalated(organization="OrgB"))

    result = resolve_on_success(success_event(), deps, now=bogota(5, 0))

    assert result["signalled"] == []
    assert result["ignored"] == [{"incident_id": "inc-1", "reason": "other_organization"}]
    assert event_rows(deps.events, "inc-1")[0]["event_type"] == "RESOLVE_IGNORED"
    assert deps.sfn.calls == []


def test_a_success_that_ended_before_the_incident_opened_does_not_close_it():
    deps = make(escalated())  # opened at 04:00

    result = resolve_on_success(success_event(stop=bogota(3, 0)), deps, now=bogota(5, 0))

    assert result["ignored"] == [
        {"incident_id": "inc-1", "reason": "succeeded_before_the_incident"}
    ]


def test_an_incident_without_a_token_yet_is_ignored_not_an_error():
    deps = make(escalated(token=None))
    deps.incidents.rows[("inc-1",)].pop("task_token", None)

    result = resolve_on_success(success_event(), deps, now=bogota(5, 0))

    assert result["ignored"] == [{"incident_id": "inc-1", "reason": "no_task_token_yet"}]


@pytest.mark.req("8.3")
@pytest.mark.parametrize("code", ["TaskTimedOut", "InvalidToken", "TaskDoesNotExist"])
def test_a_token_that_step_functions_no_longer_accepts_is_ignored_and_recorded(code):
    deps = make(escalated(), sfn=FakeSfn(error_code=code))

    result = resolve_on_success(success_event(), deps, now=bogota(5, 0))

    assert result == {
        "signalled": [],
        "ignored": [{"incident_id": "inc-1", "reason": "stale_task_token"}],
    }
    assert event_rows(deps.events, "inc-1")[0]["detail"]["reason"] == "stale_task_token"


def test_an_unexpected_step_functions_error_is_raised_so_eventbridge_retries():
    deps = make(escalated(), sfn=FakeSfn(error_code="AccessDeniedException"))

    with pytest.raises(ClientError):
        resolve_on_success(success_event(), deps, now=bogota(5, 0))


@pytest.mark.req("8.3")
def test_a_duplicate_event_signals_a_second_time_and_step_functions_rejects_it():
    deps = make(escalated())
    first = resolve_on_success(success_event(), deps, now=bogota(5, 0))
    deps.sfn.error_code = "TaskTimedOut"  # the wait already finished

    second = resolve_on_success(success_event(), deps, now=bogota(5, 1))

    assert first["signalled"] == ["inc-1"] and second["signalled"] == []
    assert second["ignored"][0]["reason"] == "stale_task_token"


def test_other_status_events_are_not_success_events():
    deps = make(escalated())
    event = success_event()
    event["detail"]["status"] = "FAILED"

    assert resolve_on_success(event, deps)["ignored"] == [
        {"incident_id": None, "reason": "not_a_success_event"}
    ]
