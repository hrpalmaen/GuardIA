"""GuardIA-Sim stand-in for every Lambda step of the simulated orchestrators (T3).

Test-only: it is deployed by GuardIA-Sim, never by GuardIA. Each state of the
rebuilt state machines calls it with `{"org", "step", "execution_id",
"redrive_count", ...}`. Without an active scenario it returns the minimal
output the choices need. With one, it fails the way the real step failed:

* `http500` (IngestionController): `Exception` with the sanitized Genesys 500
  message of the real EMTSAS failure.
* `unknown` (IngestionController): `SchemaValidationError`, which the
  ErrorCatalog does not know (for the AI agent, T8).
* `oom` (ProcessConversationSplitter): a real `Runtime.OutOfMemory`, by filling
  its 128 MB.
* `stuck` (IngestionController): asks the run to wait `wait_seconds`, so it
  stays RUNNING (STUCK_RUN).

A scenario fails `times` runs. A run is the execution plus its redrive count,
so `times=1` fails the first run and lets its redrive or relaunch succeed, and
every step of a failing run fails consistently (all Map iterations, say).

Environment:
    SCENARIOS_TABLE   GuardIA-Sim scenarios (PK org)
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

SCENARIO_STEPS = {
    "http500": "IngestionController",
    "unknown": "IngestionController",
    "stuck": "IngestionController",
    "oom": "ProcessConversationSplitter",
}
# The sanitized message of the real EMTSAS failure (tests/fixtures/executions).
HTTP500_MESSAGE = (
    "Falló consulta de hits para [INBOUND] después de 3 intentos: 500 Server Error: "
    "Internal Server Error for url: "
    "https://api.cac1.pure.cloud/api/v2/analytics/conversations/details/query"
)
UNKNOWN_MESSAGE = (
    "campo inesperado 'wrapUpCodeV2' en la respuesta de la API de conversaciones; "
    "el esquema esperado es v1"
)
DEFAULT_STUCK_SECONDS = 2400
TASKS_PER_RUN = 3
_OOM_CHUNK = 16 * 1024 * 1024


class SchemaValidationError(Exception):
    """A failure GuardIA's ErrorCatalog does not know (for the AI agent, T8)."""


@cache
def _table() -> Any:
    return boto3.resource("dynamodb").Table(os.environ["SCENARIOS_TABLE"])


def run_key(event: dict) -> str:
    return f"{event['execution_id']}#{event.get('redrive_count') or 0}"


def should_fail(table: Any, org: str, scenario: str, key: str) -> bool:
    """True if this run fails. The first step of a run that finds tries left
    claims one (conditional write); later steps of the same run see its key."""
    try:
        table.update_item(
            Key={"org": org},
            UpdateExpression="SET times_left = times_left - :one ADD failed_runs :run",
            ConditionExpression=(
                "scenario = :s AND times_left > :zero AND NOT contains(failed_runs, :k)"
            ),
            ExpressionAttributeValues={
                ":one": 1, ":zero": 0, ":s": scenario, ":run": {key}, ":k": key,
            },
        )  # fmt: skip
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    item = table.get_item(Key={"org": org}, ConsistentRead=True).get("Item") or {}
    return item.get("scenario") == scenario and key in item.get("failed_runs", set())


def _active_scenario(table: Any, org: str, step: str) -> dict | None:
    item = table.get_item(Key={"org": org}, ConsistentRead=True).get("Item")
    if not item or SCENARIO_STEPS.get(item.get("scenario")) != step:
        return None
    return item


def _fill_memory() -> None:
    chunks = []
    while True:  # Lambda kills the process at its memory limit: Runtime.OutOfMemory
        chunks.append(bytearray(_OOM_CHUNK))


def _normal_output(event: dict) -> dict:
    step = event["step"]
    if step == "IngestionController":
        tasks = [
            {"taskId": i + 1, "processType": "INBOUND", "reportType": "conversations",
             "totalHits": 10 * (i + 1)}
            for i in range(TASKS_PER_RUN)
        ]  # fmt: skip
        return {"tasks": tasks, "totalTasks": len(tasks), "executionSource": "scheduled"}
    if step == "FetchRawConversations":
        return {"recordsProcessed": int((event.get("task") or {}).get("totalHits", 0))}
    if step == "ProcessConversationSplitter":
        return {"messagesSent": int(event.get("records", 0))}
    if step == "UpdateCheckpointStatus":
        return {"checkpoint": "updated", "messagesSent": event.get("messagesSent")}
    raise ValueError(f"unknown simulated step {step!r}")


def stub_task(event: dict, table: Any) -> dict:
    org, step = event["org"], event["step"]
    scenario = _active_scenario(table, org, step)
    if scenario and should_fail(table, org, scenario["scenario"], run_key(event)):
        kind = scenario["scenario"]
        if kind == "http500":
            raise Exception(HTTP500_MESSAGE)  # noqa: TRY002 - the real step raised Exception
        if kind == "unknown":
            raise SchemaValidationError(UNKNOWN_MESSAGE)
        if kind == "oom":
            _fill_memory()
        if kind == "stuck":
            wait = int(scenario.get("wait_seconds") or DEFAULT_STUCK_SECONDS)
            return {**_normal_output(event), "simWaitSeconds": wait}
    return _normal_output(event)


def handler(event: dict, context: Any) -> dict:
    return stub_task(event, _table())
