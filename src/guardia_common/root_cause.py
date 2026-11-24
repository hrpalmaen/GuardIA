"""Root cause of a failed Step Functions execution, from its event history.

The final error of an execution often hides what really failed. Two real cases
from the Genesys conversations process:

* A Map iteration fails with `Runtime.OutOfMemory`; the catch path then fails
  in `UpdateCheckpointTask` with `States.Runtime` (a JSONPath that does not
  exist on the error path). `DescribeExecution` only shows `States.Runtime`.
* `IngestionControllerTask` fails with a Genesys `500 Server Error`; the
  workflow catches it and ends with the generic
  `IngestionControllerError: IngestionController failed`.

Rule: the root cause is the **last task-level failure** in the history (the one
whose retries were exhausted and that propagated), with the state where it
happened. If no task failed, the execution-level failure is used.

`fetch_history` works with any client that has the boto3 `stepfunctions`
interface: the real client in AWS or the local replay client.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .redaction import sanitize_cause
from .schedule import parse_timestamp

# Task-level failures: event type → name of its details field.
TASK_FAILURE_EVENTS = {
    "TaskFailed": "taskFailedEventDetails",
    "TaskTimedOut": "taskTimedOutEventDetails",
    "TaskStartFailed": "taskStartFailedEventDetails",
    "TaskSubmitFailed": "taskSubmitFailedEventDetails",
    "LambdaFunctionFailed": "lambdaFunctionFailedEventDetails",
    "LambdaFunctionTimedOut": "lambdaFunctionTimedOutEventDetails",
    "LambdaFunctionStartFailed": "lambdaFunctionStartFailedEventDetails",
    "LambdaFunctionScheduleFailed": "lambdaFunctionScheduleFailedEventDetails",
    "ActivityFailed": "activityFailedEventDetails",
    "ActivityTimedOut": "activityTimedOutEventDetails",
    "ActivityScheduleFailed": "activityScheduleFailedEventDetails",
}
EXECUTION_FAILURE_EVENTS = {
    "ExecutionFailed": "executionFailedEventDetails",
    "ExecutionTimedOut": "executionTimedOutEventDetails",
    "ExecutionAborted": "executionAbortedEventDetails",
}
# Step Functions caps a Standard execution history at 25,000 events: with
# 1,000 events per page, 25 pages always read the whole history.
MAX_HISTORY_PAGES = 25
PAGE_SIZE = 1000


@dataclass(frozen=True)
class RootCause:
    error: str | None
    message: str | None
    state_name: str | None
    event_type: str
    event_id: int
    timestamp: str | None
    task_failures: int  # all task-level failures in the history, including retried ones
    location: str | None = None  # innermost frame of the Lambda stack trace

    def as_dict(self) -> dict:
        return asdict(self)


def fetch_history(sfn: Any, execution_arn: str) -> list[dict]:
    """All events of an execution, oldest first (GetExecutionHistory, paginated)."""
    events: list[dict] = []
    kwargs: dict[str, Any] = {"executionArn": execution_arn, "maxResults": PAGE_SIZE}
    for _ in range(MAX_HISTORY_PAGES):
        page = sfn.get_execution_history(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events


def _state_name(event: dict, by_id: dict[int, dict]) -> str | None:
    seen: set[int] = set()
    current: dict | None = event
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        entered = current.get("stateEnteredEventDetails")
        if entered and current["type"].endswith("StateEntered"):
            return entered.get("name")
        current = by_id.get(current.get("previousEventId"))
    return None


def _lambda_error(cause: str | None) -> tuple[str | None, str | None, str | None]:
    """A Lambda cause is JSON with errorType, errorMessage, and stackTrace."""
    if not cause:
        return None, None, None
    try:
        data = json.loads(cause)
    except ValueError:
        return None, cause, None
    if not isinstance(data, dict):
        return None, cause, None
    stack = data.get("stackTrace") or []
    location = stack[-1].strip().splitlines()[0].strip() if stack else None
    return data.get("errorType"), data.get("errorMessage") or cause, location


def _iso(value: Any) -> str | None:
    moment = parse_timestamp(value) if not isinstance(value, datetime) else value
    return moment.isoformat() if moment else None


def extract_root_cause(events: list[dict]) -> RootCause | None:
    if not events:
        return None
    by_id = {e["id"]: e for e in events}
    task_failures = [e for e in events if e["type"] in TASK_FAILURE_EVENTS]

    if task_failures:
        event = max(task_failures, key=lambda e: e["id"])
        details = event.get(TASK_FAILURE_EVENTS[event["type"]]) or {}
    else:
        final = [e for e in events if e["type"] in EXECUTION_FAILURE_EVENTS]
        if not final:
            return None
        event = max(final, key=lambda e: e["id"])
        details = event.get(EXECUTION_FAILURE_EVENTS[event["type"]]) or {}

    error_type, message, location = _lambda_error(details.get("cause"))
    return RootCause(
        error=details.get("error") or error_type,
        message=sanitize_cause(message),
        state_name=_state_name(event, by_id),
        event_type=event["type"],
        event_id=event["id"],
        timestamp=_iso(event.get("timestamp")),
        task_failures=len(task_failures),
        location=location,
    )


# --------------------------------------------------------------------------- #
# Whether a redrive would run the root cause again (used by RedriveOrRestart)
# --------------------------------------------------------------------------- #
_CONTAINER_STATES = {"MapStateEntered", "ParallelStateEntered"}


def redrive_reruns_root_cause(events: list[dict]) -> bool:
    """A redrive restarts from the state that ended the execution, not from the
    root cause, so it only helps when that state *is* the root cause, or the Map
    or Parallel that contains it (whose failed iterations are rerun).

    Both real Genesys failures are caught first and end somewhere else, so a
    redrive repeats the ending and fails again:

    * EMTSAS: the IngestionController error is caught and the run ends in the
      `Fail` state, which a redrive "reenters and fails again".
    * EMT12: the Map error is caught and the run ends in `UpdateCheckpointTask`
      with a JSONPath that only exists on the success path.

    In those cases the answer is False and the execution must be relaunched
    with its original input. With no failure in the history (for example an
    execution-level timeout), the redrive resumes where it stopped: True.
    """
    endings = [e for e in events if e["type"] in EXECUTION_FAILURE_EVENTS]
    if not endings:
        return False
    end_id = max(e["id"] for e in endings)
    entered = [
        e
        for e in events
        if e["id"] < end_id
        and e["type"].endswith("StateEntered")
        and e.get("stateEnteredEventDetails")
    ]
    if not entered:
        return True
    last = max(entered, key=lambda e: e["id"])
    if last["type"] == "FailStateEntered":
        return False

    root = extract_root_cause(events)
    if root is None or root.state_name is None or root.event_type in EXECUTION_FAILURE_EVENTS:
        return True
    if last["stateEnteredEventDetails"].get("name") == root.state_name:
        return True
    return last["type"] in _CONTAINER_STATES and root.event_id > last["id"]
