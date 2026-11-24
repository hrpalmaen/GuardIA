"""Tasks 5.2 and 5.3: RedriveOrRestart and PollExecution.

moto does not implement RedriveExecution, so Step Functions is replaced by a small
fake; DynamoDB stays on moto like the other Lambda tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident
from lambdas.poll_execution import handler as poll_module
from lambdas.poll_execution.handler import Deps as PollDeps
from lambdas.poll_execution.handler import poll_execution
from lambdas.redrive_or_restart import handler as redrive_module
from lambdas.redrive_or_restart.handler import (
    AttemptsExhaustedError,
    Deps,
    IncidentNotFoundError,
    RemediationNotAllowedError,
    redrive_or_restart,
)
from tests.unit.conftest import EXEC_PREFIX, SM_PREFIX, bogota, status_event

MASTER = f"{SM_PREFIX}:genesys-master-orgA"
EXECUTION_ARN = f"{EXEC_PREFIX}:genesys-master-orgA:run-1"


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeSfn:
    """Just the Step Functions calls the two Lambdas make."""

    class exceptions:  # noqa: N801 - mirrors boto3's `client.exceptions`
        class ExecutionAlreadyExists(Exception):  # noqa: N818
            pass

    # An uncaught task failure: a redrive reruns the failed state.
    UNCAUGHT = [
        {"id": 1, "type": "ExecutionStarted", "previousEventId": 0},
        {"id": 2, "type": "TaskStateEntered", "previousEventId": 1,
         "stateEnteredEventDetails": {"name": "Fetch"}},
        {"id": 3, "type": "TaskFailed", "previousEventId": 2,
         "taskFailedEventDetails": {"error": "Genesys.ApiError", "cause": "429"}},
        {"id": 4, "type": "ExecutionFailed", "previousEventId": 3,
         "executionFailedEventDetails": {"error": "Genesys.ApiError"}},
    ]  # fmt: skip

    def __init__(
        self, *, remediation="auto", machine_type="STANDARD", redrive_error=None, history=None
    ):
        self.remediation = remediation
        self.machine_type = machine_type
        self.redrive_error = redrive_error
        self.history = history if history is not None else self.UNCAUGHT
        self.statuses: dict[str, dict] = {}
        self.redriven: list[str] = []
        self.started: list[dict] = []

    def list_tags_for_resource(self, resourceArn):  # noqa: N803
        if self.remediation is None:
            return {"tags": []}
        return {"tags": [{"key": "guardia:remediation", "value": self.remediation}]}

    def describe_state_machine(self, stateMachineArn):  # noqa: N803
        return {"type": self.machine_type}

    def describe_execution(self, executionArn):  # noqa: N803
        return self.statuses.get(executionArn, {"status": "FAILED"})

    def get_execution_history(self, executionArn, **_):  # noqa: N803
        return {"events": self.history}

    def redrive_execution(self, executionArn):  # noqa: N803
        if self.redrive_error:
            raise _client_error(self.redrive_error, "RedriveExecution")
        self.redriven.append(executionArn)
        return {}

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002, N803
        if any(s["name"] == name for s in self.started):
            raise self.exceptions.ExecutionAlreadyExists()
        self.started.append({"arn": stateMachineArn, "name": name, "input": input})
        return {"executionArn": f"{EXEC_PREFIX}:{stateMachineArn.rsplit(':', 1)[-1]}:{name}"}


@pytest.fixture
def open_(catalog_loaded):
    aws = catalog_loaded
    t = aws["tables"]

    def _open(machine="genesys-master-orgA", run="run-1", **kwargs) -> str:
        opened = open_incident(
            status_event(machine=machine, run=run, **kwargs),
            OpenDeps(t["Incidents"], t["ProcessRegistry"], t["IncidentEvents"],
                     aws["sfn"], aws["ssm"], aws["flow_arn"]),
            now=bogota(4, 0),
        )  # fmt: skip
        return opened["incident_id"]

    return _open


@pytest.fixture
def tables(aws):
    return aws["tables"]


def _deps(tables, sfn):
    return Deps(tables["Incidents"], tables["IncidentEvents"], sfn)


def _incident(tables, incident_id):
    return tables["Incidents"].get_item(Key={"incident_id": incident_id})["Item"]


def _event_types(tables, incident_id):
    rows = tables["IncidentEvents"].query(KeyConditionExpression=Key("incident_id").eq(incident_id))
    return [r["event_type"] for r in rows["Items"]]


# --------------------------------------------------------------------------- #
# 5.2 RedriveOrRestart
# --------------------------------------------------------------------------- #
@pytest.mark.req("5.1")
def test_redrive_standard_execution(open_, tables):
    incident_id = open_()
    sfn = FakeSfn()

    result = redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert result["recovered"] and result["strategy"] == "redrive" and result["attempt"] == 1
    assert result["recovery_execution_arn"] == EXECUTION_ARN
    assert sfn.redriven == [EXECUTION_ARN] and not sfn.started
    row = _incident(tables, incident_id)
    assert row["attempts"] == 1 and row["status"] == "REMEDIATING"
    assert row["recovery_execution_arn"] == EXECUTION_ARN
    assert "RECOVERY_STARTED" in _event_types(tables, incident_id)


@pytest.mark.req("5.1")
def test_restart_with_original_input_when_redrive_is_not_eligible(open_, tables):
    incident_id = open_()
    sfn = FakeSfn(redrive_error="ExecutionNotRedrivable")

    result = redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert result["strategy"] == "restart"
    assert not sfn.redriven
    assert sfn.started == [
        {"arn": MASTER, "name": f"{incident_id}-r1", "input": '{"organization": "orgA"}'}
    ]
    assert result["recovery_execution_arn"].endswith(f":{incident_id}-r1")


@pytest.mark.req("5.1")
def test_restart_when_state_machine_is_express(open_, tables):
    incident_id = open_()
    sfn = FakeSfn(machine_type="EXPRESS")

    result = redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert result["strategy"] == "restart" and not sfn.redriven


@pytest.mark.req("5.1")
def test_restart_when_execution_is_not_in_a_redrivable_status(open_, tables):
    incident_id = open_()
    sfn = FakeSfn()
    sfn.statuses[EXECUTION_ARN] = {"status": "SUCCEEDED"}

    result = redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert result["strategy"] == "restart" and not sfn.redriven


@pytest.mark.req("5.1")
def test_unexpected_redrive_error_is_not_swallowed(open_, tables):
    incident_id = open_()
    sfn = FakeSfn(redrive_error="AccessDeniedException")

    with pytest.raises(ClientError):
        redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))
    assert not sfn.started


@pytest.mark.req("12.1")
def test_rejects_resource_without_the_remediation_tag(open_, tables):
    incident_id = open_()
    sfn = FakeSfn(remediation=None)

    with pytest.raises(RemediationNotAllowedError):
        redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert not sfn.redriven and not sfn.started
    assert _incident(tables, incident_id)["attempts"] == 0
    assert "REMEDIATION_REJECTED" in _event_types(tables, incident_id)


@pytest.mark.req("12.4")
def test_escalate_only_incident_never_executes_an_action(open_, tables):
    incident_id = open_(machine="genesys-conversations-orgB")
    # Even if the tag on the state machine said `auto`, the registered mode wins.
    sfn = FakeSfn(remediation="auto")

    with pytest.raises(RemediationNotAllowedError):
        redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert not sfn.redriven and not sfn.started


@pytest.mark.req("12.4")
def test_tag_set_to_escalate_only_on_the_state_machine_is_rejected(open_, tables):
    incident_id = open_()
    sfn = FakeSfn(remediation="escalate-only")

    with pytest.raises(RemediationNotAllowedError):
        redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))
    assert not sfn.redriven and not sfn.started


@pytest.mark.req("5.5")
def test_third_attempt_is_refused(open_, tables):
    incident_id = open_()
    sfn = FakeSfn()
    deps = _deps(tables, sfn)

    first = redrive_or_restart({"incident_id": incident_id}, deps, now=bogota(4, 5))
    second = redrive_or_restart({"incident_id": incident_id}, deps, now=bogota(4, 12))
    assert (first["attempt"], second["attempt"]) == (1, 2)

    with pytest.raises(AttemptsExhaustedError):
        redrive_or_restart({"incident_id": incident_id}, deps, now=bogota(4, 19))
    assert len(sfn.redriven) == 2


def test_missed_run_has_nothing_to_recover(aws, tables):
    opened = open_incident(
        {"kind": "MISSED_RUN", "resource_arn": MASTER, "expected_at": bogota(3, 0).isoformat()},
        OpenDeps(tables["Incidents"], tables["ProcessRegistry"], tables["IncidentEvents"],
                 aws["sfn"], aws["ssm"], aws["flow_arn"]),
        now=bogota(4, 0),
    )  # fmt: skip
    sfn = FakeSfn()

    result = redrive_or_restart({"incident_id": opened["incident_id"]}, _deps(tables, sfn))

    assert result["recovered"] is False and not sfn.started
    assert _incident(tables, opened["incident_id"])["attempts"] == 0


def test_unknown_incident_and_missing_id(tables):
    deps = _deps(tables, FakeSfn())
    with pytest.raises(IncidentNotFoundError):
        redrive_or_restart({"incident_id": "nope"}, deps)
    with pytest.raises(ValueError, match="incident_id"):
        redrive_or_restart({}, deps)


# --------------------------------------------------------------------------- #
# 5.3 PollExecution
# --------------------------------------------------------------------------- #
def _poll(tables, sfn_status: dict):
    sfn = FakeSfn()
    sfn.statuses[EXECUTION_ARN] = sfn_status
    return poll_execution(
        {"incident_id": "inc-1", "recovery_execution_arn": EXECUTION_ARN},
        PollDeps(tables["IncidentEvents"], sfn),
        now=bogota(4, 30),
    )


@pytest.mark.req("5.4")
def test_poll_succeeded(tables):
    stopped = datetime(2026, 11, 12, 9, 28, tzinfo=UTC)
    result = _poll(tables, {"status": "SUCCEEDED", "stopDate": stopped})

    assert result["status"] == "SUCCEEDED"
    assert result["done"] and result["succeeded"]
    assert "error" not in result
    assert _event_types(tables, "inc-1") == ["RECOVERY_FINISHED"]


@pytest.mark.req("5.4")
@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_poll_failed_is_final_but_not_resolved(tables, status):
    result = _poll(tables, {"status": status, "error": "States.TaskFailed", "cause": "secret"})

    assert result["done"] and not result["succeeded"]
    assert result["error"] == "States.TaskFailed"
    assert "cause" not in result
    assert _event_types(tables, "inc-1") == ["RECOVERY_FINISHED"]


@pytest.mark.req("5.4")
@pytest.mark.parametrize("status", ["RUNNING", "PENDING_REDRIVE"])
def test_poll_running_keeps_waiting(tables, status):
    result = _poll(tables, {"status": status})

    assert result["status"] == status
    assert not result["done"] and not result["succeeded"]
    assert _event_types(tables, "inc-1") == []


def test_poll_requires_its_inputs(tables):
    deps = PollDeps(tables["IncidentEvents"], FakeSfn())
    with pytest.raises(ValueError, match="requires"):
        poll_execution({"incident_id": "inc-1"}, deps)


# --------------------------------------------------------------------------- #
# Entry points build their clients from the environment
# --------------------------------------------------------------------------- #
@pytest.mark.req("5.1")
def test_lambda_handlers_use_environment(open_, aws, monkeypatch):
    monkeypatch.setenv("INCIDENTS_TABLE", "Incidents")
    monkeypatch.setenv("INCIDENT_EVENTS_TABLE", "IncidentEvents")
    redrive_module._aws_deps.cache_clear()
    poll_module._aws_deps.cache_clear()
    incident_id = open_()  # uses the real moto client before boto3.client is patched
    monkeypatch.setattr(redrive_module.boto3, "client", lambda name, **kw: FakeSfn())
    monkeypatch.setattr(poll_module.boto3, "client", lambda name, **kw: FakeSfn())
    try:
        out = redrive_module.handler({"incident_id": incident_id}, None)
        polled = poll_module.handler(
            {"incident_id": incident_id, "recovery_execution_arn": out["recovery_execution_arn"]},
            None,
        )
    finally:
        redrive_module._aws_deps.cache_clear()
        poll_module._aws_deps.cache_clear()

    assert out["recovered"] and polled["status"] == "FAILED"


@pytest.mark.req("5.1")
def test_restart_when_the_failure_was_caught_and_ended_in_a_fail_state(
    open_, tables, real_executions
):
    """EMTSAS: a redrive would reenter the Fail state and fail again (AWS docs)."""
    from tests.unit.conftest import EMTSAS_500

    sfn = FakeSfn(history=real_executions.by_id(EMTSAS_500).history_events())
    incident_id = open_()
    out = redrive_or_restart({"incident_id": incident_id}, _deps(tables, sfn), now=bogota(4, 5))

    assert out["strategy"] == "restart"
    assert sfn.redriven == []
    assert len(sfn.started) == 1
