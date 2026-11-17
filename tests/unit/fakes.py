"""In-memory stand-ins for the AWS clients used by ResolveOnCall and SendEscalation.

They keep these tests independent of moto internals and fast. The matching
wiring test (`test_escalation_wiring.py`) still goes through moto.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from config import DEFAULT_THRESHOLDS
from guardia_common import config as guardia_config
from guardia_common.models import Incident

BOGOTA = ZoneInfo("America/Bogota")
WEBHOOK = "https://prod-00.example.logic.azure.com/workflows/abc123?sig=SUPER-SECRET-SIGNATURE"

PRIMARY = {"display_name": "Ana Guardia", "teams_user_id": "ana-aad-id", "email": "ana@example.com"}
BACKUP = {
    "display_name": "Beto Respaldo",
    "teams_user_id": "beto-aad-id",
    "email": "beto@example.com",
}


def bogota(hour: int, minute: int = 0, day: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=BOGOTA)


def _conditional_failure(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition"}}, operation
    )


class FakeTable:
    """Just enough of a DynamoDB Table: get_item, put_item, update_item (SET)."""

    def __init__(self, *key_names: str):
        self.key_names = key_names
        self.rows: dict[tuple, dict] = {}

    def _key(self, source: dict) -> tuple:
        return tuple(source[name] for name in self.key_names)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 (boto3 signature)
        row = self.rows.get(self._key(Key))
        return {"Item": copy.deepcopy(row)} if row else {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        key = self._key(Item)
        if ConditionExpression and "attribute_not_exists" in ConditionExpression:
            if key in self.rows:
                raise _conditional_failure("PutItem")
        self.rows[key] = copy.deepcopy(Item)

    def update_item(  # noqa: N803
        self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None
    ):
        key = self._key(Key)
        if ConditionExpression and "attribute_exists" in ConditionExpression:
            if key not in self.rows:
                raise _conditional_failure("UpdateItem")
        assignments = UpdateExpression.removeprefix("SET ").split(",")
        for assignment in assignments:
            name, placeholder = (part.strip() for part in assignment.split("="))
            self.rows[key][name] = ExpressionAttributeValues[placeholder]

    # helpers for assertions
    def put_row(self, row: dict) -> None:
        self.rows[self._key(row)] = copy.deepcopy(row)

    def only(self) -> dict:
        assert len(self.rows) == 1, f"expected one row, found {len(self.rows)}"
        return next(iter(self.rows.values()))


class _Paginator:
    def __init__(self, values: dict[str, str]):
        self._values = values

    def paginate(self, Path, Recursive=False):  # noqa: N803
        yield {"Parameters": [{"Name": f"{Path}/{k}", "Value": v} for k, v in self._values.items()]}


class FakeSsm:
    def __init__(self, overrides: dict[str, str] | None = None):
        self._values = {**DEFAULT_THRESHOLDS, **(overrides or {})}

    def get_paginator(self, name):
        assert name == "get_parameters_by_path"
        return _Paginator(self._values)


class FakeSecrets:
    def __init__(self, secret_string: str = WEBHOOK):
        self._secret = secret_string

    def get_secret_value(self, SecretId):  # noqa: N803
        return {"SecretString": self._secret}


def event_rows(table: FakeTable, incident_id: str) -> list[dict]:
    return sorted(
        (row for row in table.rows.values() if row["incident_id"] == incident_id),
        key=lambda row: row["sk"],
    )


def event_types(table: FakeTable, incident_id: str) -> list[str]:
    return [row["event_type"] for row in event_rows(table, incident_id)]


def new_incident_item(
    incident_id: str = "inc-1",
    deadline_iso: str = "2026-11-12T06:00:00-05:00",
    cause: str | None = None,
    **extra,
) -> dict:
    incident = Incident(
        incident_id=incident_id,
        execution_arn="arn:aws:states:us-east-1:123456789012:execution:genesys-master-orgA:run-1",
        organization="OrgA",
        process_type="master",
        kind="FAILED",
        error="Genesys.AuthError",
        cause=cause,
        deadline_iso=deadline_iso,
        remediation="escalate-only",
        resource_arn="arn:aws:states:us-east-1:123456789012:stateMachine:genesys-master-orgA",
        status="OPEN",
        opened_at="2026-11-12T04:00:00-05:00",
    )
    return {**incident.to_item(), **extra}


def fresh_ssm(overrides: dict[str, str] | None = None) -> FakeSsm:
    guardia_config.clear_cache()
    return FakeSsm(overrides)


def card_text(card: dict) -> str:
    """The whole card as one string, for 'does not contain' checks."""
    return json.dumps(card, ensure_ascii=False)
