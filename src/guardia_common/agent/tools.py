"""Read-only tools of the agent (T8).

Each tool takes no arguments from the model: the incident is fixed when the
ToolBox is built, so a model that has been misled cannot ask for the data of
another process, another table, or another account. Every output is
sanitized and cut to a size limit before the model sees it.

Never returned: the input or output of any state (customer data), ARNs, or
the raw execution history.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from guardia_common.logger import get_logger, log
from guardia_common.models import from_dynamo
from guardia_common.redaction import redact_value, sanitize_cause, truncate
from guardia_common.root_cause import extract_root_cause, fetch_history
from guardia_common.schedule import parse_timestamp
from guardia_common.tables import RESOURCE_ARN_INDEX

from .models import ToolSpec

LOGGER = get_logger("guardia.agent.tools")
NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}
MAX_PATH_STATES = 60
MAX_RECENT_RUNS = 10
MAX_RUN_DETAILS = 5  # DescribeExecution calls, only for failed runs
MAX_SIMILAR = 10
SIMILAR_LOOKBACK = 25
SHORT_CAUSE = 300

Tool = Callable[[], Any]


class ToolBox:
    def __init__(self, tools: dict[str, tuple[ToolSpec, Tool]], max_output_chars: int = 4000):
        self._tools = tools
        self._max = max_output_chars

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    def run(self, name: str, arguments: dict | None = None) -> str:  # noqa: ARG002
        """The tool's output as text for the model: sanitized and cut to size.
        A failing tool answers with its error class only, never with its message."""
        entry = self._tools.get(name)
        if entry is None:
            return json.dumps({"error": "unknown_tool", "available": sorted(self._tools)})
        try:
            result = redact_value(entry[1]())
            text = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception as err:  # a tool must never break the agent loop
            log(LOGGER, logging.WARNING, "Agent tool failed", tool=name, error=type(err).__name__)
            return json.dumps({"error": "tool_failed", "reason": type(err).__name__})
        return truncate(text, self._max)


def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=NO_ARGUMENTS)


def _iso(value: Any) -> str | None:
    moment = value if isinstance(value, datetime) else parse_timestamp(value)
    return moment.isoformat() if moment else None


def _duration_s(started: Any, stopped: Any) -> int | None:
    begin = started if isinstance(started, datetime) else parse_timestamp(started)
    end = stopped if isinstance(stopped, datetime) else parse_timestamp(stopped)
    return int((end - begin).total_seconds()) if begin and end else None


def build_toolbox(
    item: dict, *, sfn: Any, incidents: Any, catalog: Any, max_output_chars: int = 4000
) -> ToolBox:
    """The tools for one incident (`item` is its stored row)."""
    item = from_dynamo(item)
    execution_arn = item.get("execution_arn")
    resource_arn = item.get("resource_arn")
    process_type = item.get("process_type")

    def history() -> list[dict]:
        return fetch_history(sfn, execution_arn) if execution_arn else []

    def get_failure_details() -> dict:
        details = {
            "failed_state": item.get("failed_state"),
            "error": item.get("error"),
            "root_error": item.get("root_error"),
            "root_cause": item.get("root_cause"),
            "cause": sanitize_cause(item.get("cause"), SHORT_CAUSE),
        }
        root = extract_root_cause(history())
        if root:
            details["from_history"] = {
                "error": root.error,
                "message": root.message,
                "state": root.state_name,
                "location": root.location,
                "task_failures": root.task_failures,
            }
        return {k: v for k, v in details.items() if v is not None}

    def get_execution_path() -> dict:
        names: list[str] = []
        for event in history():
            entered = event.get("stateEnteredEventDetails")
            if entered and entered.get("name") and (not names or names[-1] != entered["name"]):
                names.append(entered["name"])
        return {"states": names[-MAX_PATH_STATES:], "failed_state": item.get("failed_state")}

    def get_recent_runs() -> dict:
        if not resource_arn:
            return {"runs": []}
        listed = sfn.list_executions(stateMachineArn=resource_arn, maxResults=MAX_RECENT_RUNS)
        runs, described = [], 0
        for run in listed.get("executions", [])[:MAX_RECENT_RUNS]:
            entry = {
                "name": run.get("name"),
                "status": run.get("status"),
                "started": _iso(run.get("startDate")),
                "duration_s": _duration_s(run.get("startDate"), run.get("stopDate")),
            }
            if run.get("status") == "FAILED" and described < MAX_RUN_DETAILS:
                described += 1
                detail = sfn.describe_execution(executionArn=run["executionArn"])
                entry["error"] = detail.get("error")
                entry["cause"] = sanitize_cause(detail.get("cause"), SHORT_CAUSE)
            runs.append(entry)
        return {"runs": runs}

    def search_error_catalog() -> dict:
        rows, kwargs = [], {}
        while True:
            page = catalog.scan(**kwargs)
            rows.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        wanted = (process_type, "*")
        return {
            "entries": [
                {
                    key: row.get(key)
                    for key in ("process_type", "error_pattern", "classification", "action")
                }
                | {"max_wait_s": from_dynamo(row.get("max_wait_s", 0))}
                for row in rows
                if row.get("process_type") in wanted
            ]
        }

    def get_similar_incidents() -> dict:
        if not resource_arn:
            return {"incidents": []}
        page = incidents.query(
            IndexName=RESOURCE_ARN_INDEX,
            KeyConditionExpression="resource_arn = :r",
            ExpressionAttributeValues={":r": resource_arn},
            ScanIndexForward=False,
            Limit=SIMILAR_LOOKBACK,
        )
        key = "root_error" if item.get("root_error") else "error"
        wanted = item.get(key)
        similar = [
            {
                "opened_at": row.get("opened_at"),
                "status": row.get("status"),
                "kind": row.get("kind"),
                "error": row.get(key),
                "attempts": from_dynamo(row.get("attempts", 0)),
                "resolved_by": row.get("resolved_by"),
            }
            for row in page.get("Items", [])
            if row.get("incident_id") != item.get("incident_id") and row.get(key) == wanted
        ]
        return {"incidents": similar[:MAX_SIMILAR]}

    tools: dict[str, tuple[ToolSpec, Tool]] = {
        "get_failure_details": (
            _spec(
                "get_failure_details",
                "Estado que falló, error y causa raíz de la ejecución, tomados de su historial.",
            ),
            get_failure_details,
        ),
        "get_execution_path": (
            _spec(
                "get_execution_path",
                "Nombres de los estados que recorrió la ejecución, sin entradas ni salidas.",
            ),
            get_execution_path,
        ),
        "get_recent_runs": (
            _spec(
                "get_recent_runs",
                "Las últimas ejecuciones del mismo proceso: estado, duración y error.",
            ),
            get_recent_runs,
        ),
        "search_error_catalog": (
            _spec(
                "search_error_catalog",
                "Entradas del catálogo de errores del mismo tipo de proceso: patrón, "
                "clasificación y acción.",
            ),
            search_error_catalog,
        ),
        "get_similar_incidents": (
            _spec(
                "get_similar_incidents",
                "Incidentes anteriores de este proceso con el mismo error y cómo terminaron.",
            ),
            get_similar_incidents,
        ),
    }
    return ToolBox(tools, max_output_chars)
