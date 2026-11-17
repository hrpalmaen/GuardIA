"""Shared fixtures for unit tests: moto-backed AWS resources and a fixed clock."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import pytest
from moto import mock_aws

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from guardia_common import config as guardia_config
from guardia_common.tables import create_local_tables

REGION = "us-east-1"
ACCOUNT = "123456789012"
BOGOTA = ZoneInfo("America/Bogota")
ROOT = Path(__file__).resolve().parents[2]

SM_PREFIX = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine"
EXEC_PREFIX = f"arn:aws:states:{REGION}:{ACCOUNT}:execution"

PROCESSES = [
    {"resource_arn": f"{SM_PREFIX}:genesys-master-orgA", "process_type": "master",
     "organization": "OrgA", "remediation": "auto"},
    {"resource_arn": f"{SM_PREFIX}:genesys-conversations-orgB", "process_type": "conversations",
     "organization": "OrgB", "remediation": "escalate-only"},
    {"resource_arn": f"{SM_PREFIX}:genesys-master-orgC", "process_type": "master",
     "organization": "OrgC", "remediation": "auto", "deadline_local": "07:30"},
]  # fmt: skip


def bogota(hour: int, minute: int = 0, day: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=BOGOTA)


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def status_event(
    status: str = "FAILED",
    machine: str = "genesys-master-orgA",
    run: str = "run-1",
    error: str | None = "Genesys.ApiError",
    cause: str | None = '{"status": 429, "message": "Too Many Requests"}',
    start: datetime | None = None,
    stop: datetime | None = None,
) -> dict:
    return {
        "version": "0",
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "executionArn": f"{EXEC_PREFIX}:{machine}:{run}",
            "stateMachineArn": f"{SM_PREFIX}:{machine}",
            "name": run,
            "status": status,
            "startDate": epoch_ms(start or bogota(3, 30)),
            "stopDate": epoch_ms(stop or bogota(4, 0)),
            "input": json.dumps({"organization": machine.rsplit("-", 1)[-1]}),
            "error": error,
            "cause": cause,
        },
    }


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    guardia_config.clear_cache()
    from lambdas.classify_incident import handler as classify_handler

    classify_handler.clear_catalog_cache()


@pytest.fixture
def aws():
    with mock_aws():
        tables = create_local_tables(boto3.resource("dynamodb", region_name=REGION))
        for row in PROCESSES:
            tables["ProcessRegistry"].put_item(Item=row)

        ssm = boto3.client("ssm", region_name=REGION)
        for name, value in DEFAULT_THRESHOLDS.items():
            ssm.put_parameter(Name=f"{SSM_PREFIX}/{name}", Value=value, Type="String")

        sfn = boto3.client("stepfunctions", region_name=REGION)
        flow_arn = sfn.create_state_machine(
            name="GuardIA-Incident-Flow",
            definition=json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}),
            roleArn=f"arn:aws:iam::{ACCOUNT}:role/guardia-flow",
        )["stateMachineArn"]

        yield {"tables": tables, "ssm": ssm, "sfn": sfn, "flow_arn": flow_arn}


@pytest.fixture
def catalog_loaded(aws):
    from load_error_catalog import load_catalog, read_catalog

    load_catalog(aws["tables"]["ErrorCatalog"], read_catalog())
    return aws


# --------------------------------------------------------------------------- #
# Real executions (tests/fixtures/executions, sanitized by scripts/sanitize_executions.py)
# --------------------------------------------------------------------------- #
EMTSAS_500 = "emtsas-20260918-0500-failed"  # Genesys 500 in IngestionController
EMT12_OOM = "emt12-20260923-2230-failed"  # OOM in the Map, hidden by States.Runtime
REAL_SUCCEEDED = (
    "emt12-20261113-1630-succeeded",
    "emt22-20261113-1600-succeeded",
    "emtsas-20261113-1600-succeeded",
)


@pytest.fixture(scope="session")
def real_executions():
    from guardia_local.fixtures import ExecutionFixtures

    return ExecutionFixtures.load()


def registry_rows(fixtures) -> list[dict]:
    """ProcessRegistry rows as DiscoverProcesses will write them from the guardia:* tags."""
    return [
        {
            "resource_arn": sm.arn,
            "process_type": sm.tags["guardia:process-type"],
            "organization": sm.tags["guardia:organization"],
            "remediation": sm.tags["guardia:remediation"],
        }
        for sm in fixtures.state_machines.values()
    ]
