"""Replay Step Functions client: serves real executions from fixtures.

Drop-in for `boto3.client("stepfunctions")` in the handlers' `Deps`:

* `describe_execution`, `get_execution_history`, and `list_executions` answer
  from the fixtures, with the same pagination (`maxResults`, `nextToken`,
  `reverseOrder`) and the same `ExecutionDoesNotExist` error as the real API.
* With a `clock`, an execution is seen as it was at that moment: invisible
  before it starts, RUNNING (with the history so far) until it stops.
* Every other method, and executions not in the fixtures, go to `fallback`
  (for example a moto client that hosts `GuardIA-Incident-Flow`).
* Request parameters are validated against the botocore service model, so a
  wrong parameter name fails locally exactly as it would against AWS.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from typing import Any

import botocore.session
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import ParamValidator

from .fixtures import ExecutionFixture, ExecutionFixtures

_SERVICE_MODEL = botocore.session.get_session().get_service_model("stepfunctions")
_VALIDATOR = ParamValidator()


def _validate_request(operation: str, params: dict) -> None:
    shape = _SERVICE_MODEL.operation_model(operation).input_shape
    report = _VALIDATOR.validate(params, shape)
    if report.has_errors():
        raise ParamValidationError(report=report.generate_report())


def _not_found(operation: str, arn: str) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ExecutionDoesNotExist",
                "Message": f"Execution Does Not Exist: '{arn}'",
            }
        },
        operation,
    )


def _encode_token(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _decode_token(token: str | None) -> int:
    return int(base64.b64decode(token)) if token else 0


_FINAL_FIELDS = ("stopDate", "output", "outputDetails", "error", "cause")


class ReplayStepFunctionsClient:
    def __init__(
        self,
        fixtures: ExecutionFixtures,
        fallback: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._fixtures = fixtures
        self._fallback = fallback
        self._clock = clock

    def use_fixtures(self, fixtures: ExecutionFixtures) -> None:
        self._fixtures = fixtures

    # ------------------------------------------------------------------ time view
    def _visible(self, execution: ExecutionFixture) -> bool:
        return self._clock is None or execution.start_date <= self._clock()

    def _running(self, execution: ExecutionFixture) -> bool:
        stop = execution.stop_date
        return self._clock is not None and stop is not None and self._clock() < stop

    def _get(self, arn: str) -> ExecutionFixture | None:
        execution = self._fixtures.executions.get(arn)
        return execution if execution is not None and self._visible(execution) else None

    def _describe(self, execution: ExecutionFixture) -> dict:
        response = execution.describe_response()
        if self._running(execution):
            for name in _FINAL_FIELDS:
                response.pop(name, None)
            response["status"] = "RUNNING"
        return response

    # ------------------------------------------------------------------ replayed
    def describe_execution(self, **params: Any) -> dict:
        _validate_request("DescribeExecution", params)
        execution = self._get(params["executionArn"])
        if execution is None:
            return self._delegate("describe_execution", "DescribeExecution", params)
        response = self._describe(execution)
        if params.get("includedData") == "METADATA_ONLY":
            response.pop("input", None)
            response.pop("output", None)
        return response

    def get_execution_history(self, **params: Any) -> dict:
        _validate_request("GetExecutionHistory", params)
        execution = self._get(params["executionArn"])
        if execution is None:
            return self._delegate("get_execution_history", "GetExecutionHistory", params)

        events = execution.history_events()
        if self._clock is not None:
            now = self._clock()
            events = [event for event in events if event["timestamp"] <= now]
        if params.get("reverseOrder"):
            events.reverse()
        start = _decode_token(params.get("nextToken"))
        size = params.get("maxResults") or 100  # API default page size
        page = events[start : start + size]
        response: dict[str, Any] = {"events": page}
        if start + size < len(events):
            response["nextToken"] = _encode_token(start + size)
        return response

    def list_executions(self, **params: Any) -> dict:
        """Fixture executions of the state machine plus the fallback's (for
        example executions GuardIA relaunched in moto), newest first."""
        _validate_request("ListExecutions", params)
        arn = params.get("stateMachineArn")
        status = params.get("statusFilter")
        items = [
            self._list_item(execution)
            for execution in self._fixtures.executions.values()
            if execution.state_machine_arn == arn and self._visible(execution)
        ]
        items = [item for item in items if status is None or item["status"] == status]
        items += self._fallback_executions(params)
        items.sort(key=lambda item: item["startDate"], reverse=True)

        start = _decode_token(params.get("nextToken"))
        size = params.get("maxResults") or 100
        response: dict[str, Any] = {"executions": items[start : start + size]}
        if start + size < len(items):
            response["nextToken"] = _encode_token(start + size)
        return response

    def _list_item(self, execution: ExecutionFixture) -> dict:
        described = self._describe(execution)
        item = {
            key: described[key]
            for key in ("executionArn", "stateMachineArn", "name", "status", "startDate")
        }
        if "stopDate" in described:
            item["stopDate"] = described["stopDate"]
        return item

    def _fallback_executions(self, params: dict) -> list[dict]:
        if self._fallback is None:
            return []
        query = {k: v for k, v in params.items() if k in ("stateMachineArn", "statusFilter")}
        items: list[dict] = []
        try:
            paginator = self._fallback.get_paginator("list_executions")
            for page in paginator.paginate(**query):
                items += page.get("executions", [])
        except ClientError as err:
            if err.response["Error"]["Code"] != "StateMachineDoesNotExist":
                raise
        return items

    # ----------------------------------------------------------------- delegated
    def _delegate(self, method: str, operation: str, params: dict) -> dict:
        if self._fallback is None:
            raise _not_found(operation, params["executionArn"])
        return getattr(self._fallback, method)(**params)

    @property
    def exceptions(self) -> Any:
        if self._fallback is None:
            raise AttributeError("exceptions requires a fallback client")
        return self._fallback.exceptions

    def __getattr__(self, name: str) -> Any:
        if self._fallback is None:
            raise AttributeError(f"{name} is not replayed and there is no fallback client")
        return getattr(self._fallback, name)
