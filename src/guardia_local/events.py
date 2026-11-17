"""EventBridge events built from real executions.

`status_change_event` produces the `Step Functions Execution Status Change`
event that AWS emits when an execution ends, from a DescribeExecution response.
OpenIncident receives exactly this shape in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

DETAIL_TYPE = "Step Functions Execution Status Change"


def _epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return int(moment.timestamp() * 1000)


def status_change_event(describe: dict) -> dict:
    arn = describe["executionArn"]
    _, _, _, region, account, *_ = arn.split(":")
    stop = describe.get("stopDate") or describe.get("startDate")
    return {
        "version": "0",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{arn}|{describe['status']}")),
        "detail-type": DETAIL_TYPE,
        "source": "aws.states",
        "account": account,
        "time": (datetime.fromisoformat(stop) if isinstance(stop, str) else stop).isoformat(),
        "region": region,
        "resources": [arn],
        "detail": {
            "executionArn": arn,
            "stateMachineArn": describe["stateMachineArn"],
            "name": describe["name"],
            "status": describe["status"],
            "startDate": _epoch_ms(describe.get("startDate")),
            "stopDate": _epoch_ms(describe.get("stopDate")),
            "input": describe.get("input"),
            "output": describe.get("output"),
            "error": describe.get("error"),
            "cause": describe.get("cause"),
        },
    }
