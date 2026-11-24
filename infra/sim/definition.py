"""State machine definitions of GuardIA-Sim, rebuilt from the sanitized histories (D18).

One template serves the three organizations, because the three histories walk
the same orchestrator (only the `BI-<ORG>-GCRAW-` prefix changes). What each
part is based on:

* State names and types: the `*StateEntered` events of the three histories.
* `IngestionControllerTask` stores the full Lambda invoke result in
  `$.ingestionData`: the input quoted in the EMT12 `States.Runtime` cause.
* Its errors are caught → `NotifyIngestionError` (SNS) →
  `FailIngestionController` (`IngestionControllerError`, "IngestionController
  failed"): the EMTSAS history and its final error.
* `CheckTasksToProcess` → `FetchRawDataMap` over `$.ingestionData.Payload.tasks`:
  the EMT12 input shape and `MapStateStarted.length`.
* Iteration `FetchRawConversationsTask` → `CheckRecordsProcessed` →
  `ProcessConversationSplitterTask` or `SkipSplitterNoRecords`: EMT12 and EMT22.
* Map errors are caught into `$.mapError` → `NotifyMapError` (SNS) →
  `UpdateCheckpointTask`, whose `messagesSent.$: $.mapResults[0].messagesSent`
  only exists on the success path: the EMT12 `States.Runtime` cause (H10).

Assumptions, not in any history (also listed in the deployment plan, section 4):

* No Step Functions `Retry`: the visible retries are inside the Lambdas.
* `CheckTasksToProcess` default (no tasks) ends in `NoTasksToProcess` (Succeed).
* Map concurrency 5; the Lambda timeouts are the stub's own.
* Every Lambda step calls the single stub function with `{org, step, run}` so
  it can look up the scenario (the real steps call one function each, with no
  payload). The run key is the execution id plus `$$.Execution.RedriveCount`,
  so a scenario can fail a run once and let its redrive succeed.
* `SimStuckWait` is simulator-only: the stub asks for it to make a run stay
  RUNNING (STUCK_RUN), which no real step does on purpose.

`${StubFunctionArn}` and `${AlertsTopicArn}` are filled in by the stack.
"""

from __future__ import annotations

import json
from typing import Any

STUB = "${StubFunctionArn}"
TOPIC = "${AlertsTopicArn}"
MAP_CONCURRENCY = 5


def prefix(org: str) -> str:
    return f"BI-{org}-GCRAW-"


def state_machine_name(org: str) -> str:
    return f"BI-{org}-GCRAW-Orchestrator-ConversationsDetail"


def _run() -> dict[str, str]:
    return {
        "execution_id.$": "$$.Execution.Id",
        "redrive_count.$": "$$.Execution.RedriveCount",
    }


def _invoke(org: str, step: str, extra: dict | None = None) -> dict[str, Any]:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {
            "FunctionName": STUB,
            "Payload": {"org": org, "step": step, **_run(), **(extra or {})},
        },
    }


def build_definition(org: str) -> dict[str, Any]:
    p = prefix(org)
    iteration = {
        "StartAt": f"{p}FetchRawConversationsTask",
        "States": {
            f"{p}FetchRawConversationsTask": {
                **_invoke(org, "FetchRawConversations", {"task.$": "$.task"}),
                "OutputPath": "$.Payload",
                "Next": f"{p}CheckRecordsProcessed",
            },
            f"{p}CheckRecordsProcessed": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Variable": "$.recordsProcessed",
                        "NumericGreaterThan": 0,
                        "Next": f"{p}ProcessConversationSplitterTask",
                    }
                ],
                "Default": f"{p}SkipSplitterNoRecords",
            },
            f"{p}ProcessConversationSplitterTask": {
                **_invoke(org, "ProcessConversationSplitter", {"records.$": "$.recordsProcessed"}),
                "OutputPath": "$.Payload",
                "End": True,
            },
            f"{p}SkipSplitterNoRecords": {
                "Type": "Pass",
                "Result": {"messagesSent": 0},
                "End": True,
            },
        },
    }
    states: dict[str, Any] = {
        f"{p}IngestionControllerTask": {
            **_invoke(org, "IngestionController"),
            "ResultPath": "$.ingestionData",
            "Catch": [
                {
                    "ErrorEquals": ["States.ALL"],
                    "ResultPath": "$.ingestionError",
                    "Next": f"{p}NotifyIngestionError",
                }
            ],
            "Next": f"{p}CheckTasksToProcess",
        },
        f"{p}NotifyIngestionError": {
            "Type": "Task",
            "Resource": "arn:aws:states:::sns:publish",
            "Parameters": {
                "TopicArn": TOPIC,
                "Subject": f"{org} - IngestionController Error",
                "Message.$": "States.Format('IngestionController FAILED: {}', "
                "$.ingestionError.Error)",
            },
            "ResultPath": None,
            "Next": f"{p}FailIngestionController",
        },
        f"{p}FailIngestionController": {
            "Type": "Fail",
            "Error": "IngestionControllerError",
            "Cause": "IngestionController failed",
        },
        f"{p}CheckTasksToProcess": {
            "Type": "Choice",
            "Choices": [
                {
                    "Variable": "$.ingestionData.Payload.simWaitSeconds",
                    "IsPresent": True,
                    "Next": f"{p}SimStuckWait",
                },
                {
                    "Variable": "$.ingestionData.Payload.totalTasks",
                    "NumericGreaterThan": 0,
                    "Next": f"{p}FetchRawDataMap",
                },
            ],
            "Default": f"{p}NoTasksToProcess",
        },
        f"{p}SimStuckWait": {
            "Type": "Wait",
            "SecondsPath": "$.ingestionData.Payload.simWaitSeconds",
            "Next": f"{p}FetchRawDataMap",
        },
        f"{p}NoTasksToProcess": {"Type": "Succeed"},
        f"{p}FetchRawDataMap": {
            "Type": "Map",
            "ItemsPath": "$.ingestionData.Payload.tasks",
            "ItemSelector": {"task.$": "$$.Map.Item.Value"},
            "MaxConcurrency": MAP_CONCURRENCY,
            "ItemProcessor": {
                "ProcessorConfig": {"Mode": "INLINE"},
                **iteration,
            },
            "ResultPath": "$.mapResults",
            "Catch": [
                {
                    "ErrorEquals": ["States.ALL"],
                    "ResultPath": "$.mapError",
                    "Next": f"{p}NotifyMapError",
                }
            ],
            "Next": f"{p}UpdateCheckpointTask",
        },
        f"{p}NotifyMapError": {
            "Type": "Task",
            "Resource": "arn:aws:states:::sns:publish",
            "Parameters": {
                "TopicArn": TOPIC,
                "Subject": f"{org} - Map Error",
                "Message.$": "States.Format('Map FAILED: {}', $.mapError.Error)",
            },
            "ResultPath": None,
            "Next": f"{p}UpdateCheckpointTask",
        },
        # The real defect (H10): on the error path there is no $.mapResults.
        f"{p}UpdateCheckpointTask": {
            **_invoke(
                org, "UpdateCheckpointStatus", {"messagesSent.$": "$.mapResults[0].messagesSent"}
            ),
            "ResultPath": "$.checkpoint",
            "End": True,
        },
    }
    return {
        "Comment": f"GuardIA-Sim: {org} conversations orchestrator rebuilt from its history",
        "StartAt": f"{p}IngestionControllerTask",
        "States": states,
    }


def definition_json(org: str) -> str:
    return json.dumps(build_definition(org), indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------- checks vs history
STATE_TYPES = {
    "TaskStateEntered": "Task",
    "ChoiceStateEntered": "Choice",
    "MapStateEntered": "Map",
    "PassStateEntered": "Pass",
    "FailStateEntered": "Fail",
    "WaitStateEntered": "Wait",
    "SucceedStateEntered": "Succeed",
}


def all_states(definition: dict[str, Any]) -> dict[str, dict]:
    found = {}
    for name, state in definition["States"].items():
        found[name] = state
        if state["Type"] == "Map":
            found.update(all_states(state["ItemProcessor"]))
    return found


def check_against_history(definition: dict[str, Any], events: list[dict]) -> list[str]:
    """Differences between the rebuilt definition and what a history shows:
    every state the history entered must exist here with the same type."""
    states = all_states(definition)
    problems = []
    for event in events:
        kind = STATE_TYPES.get(event["type"])
        details = event.get("stateEnteredEventDetails") or {}
        name = details.get("name")
        if kind is None or name is None:
            continue
        if name not in states:
            problems.append(f"state {name} is missing")
        elif states[name]["Type"] != kind:
            problems.append(f"state {name} is {states[name]['Type']}, history says {kind}")
    return sorted(set(problems))
