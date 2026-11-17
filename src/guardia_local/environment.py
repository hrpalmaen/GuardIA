"""Local AWS environment for tests and demos, on moto (no AWS account).

`LocalEnvironment.create()` must run inside `moto.mock_aws()`. It creates:

* The GuardIA DynamoDB tables (`guardia_common.tables`) and SSM thresholds.
* The extraction state machines from the fixtures, with their `guardia:*` tags
  and the EventBridge rule that launches them, so DiscoverProcesses finds them
  through the real APIs (tagging, rules).
* A placeholder `GuardIA-Incident-Flow`, which OpenIncident starts.
* The replay Step Functions client and the in-memory bus, on a virtual clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import boto3

from guardia_common.tables import create_local_tables

from .bus import LocalEventBus
from .clock import VirtualClock
from .fixtures import ExecutionFixtures
from .stepfunctions import ReplayStepFunctionsClient

REGION = "us-east-1"
ACCOUNT = "123456789012"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/guardia-local"
_PLACEHOLDER = json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}})


def seed_thresholds(ssm: Any, thresholds: dict[str, str], prefix: str) -> None:
    for name, value in thresholds.items():
        ssm.put_parameter(Name=f"{prefix}/{name}", Value=value, Type="String", Overwrite=True)


def create_state_machine(
    sfn: Any,
    events: Any,
    name: str,
    tags: dict[str, str],
    schedule: str | None = None,
    rule_state: str = "ENABLED",
) -> str:
    """A state machine as the process owners deploy it: tags plus a cron rule."""
    arn = sfn.create_state_machine(
        name=name,
        definition=_PLACEHOLDER,
        roleArn=ROLE_ARN,
        tags=[{"key": k, "value": v} for k, v in tags.items()],
    )["stateMachineArn"]
    if schedule:
        rule = f"{name}-schedule"[:64]
        events.put_rule(Name=rule, ScheduleExpression=schedule, State=rule_state)
        events.put_targets(Rule=rule, Targets=[{"Id": "process", "Arn": arn, "RoleArn": ROLE_ARN}])
    return arn


@dataclass
class LocalEnvironment:
    clock: VirtualClock
    fixtures: ExecutionFixtures
    tables: dict[str, Any]
    ssm: Any
    moto_sfn: Any  # moto client: state machines, GuardIA-Incident-Flow executions
    sfn: ReplayStepFunctionsClient  # what the handlers receive
    events: Any  # moto EventBridge: rules that launch the processes
    scheduler: Any
    tagging: Any
    bus: LocalEventBus  # delivery of status changes and watchdog detections
    flow_arn: str

    @classmethod
    def create(
        cls,
        fixtures: ExecutionFixtures,
        *,
        start: datetime,
        thresholds: dict[str, str],
        ssm_prefix: str,
    ) -> LocalEnvironment:
        clock = VirtualClock(start)
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        ssm = boto3.client("ssm", region_name=REGION)
        moto_sfn = boto3.client("stepfunctions", region_name=REGION)
        events = boto3.client("events", region_name=REGION)

        seed_thresholds(ssm, thresholds, ssm_prefix)
        for machine in fixtures.state_machines.values():
            create_state_machine(moto_sfn, events, machine.name, machine.tags, machine.schedule)
        flow_arn = moto_sfn.create_state_machine(
            name="GuardIA-Incident-Flow", definition=_PLACEHOLDER, roleArn=ROLE_ARN
        )["stateMachineArn"]

        return cls(
            clock=clock,
            fixtures=fixtures,
            tables=create_local_tables(dynamodb),
            ssm=ssm,
            moto_sfn=moto_sfn,
            sfn=ReplayStepFunctionsClient(fixtures, fallback=moto_sfn, clock=clock),
            events=events,
            scheduler=boto3.client("scheduler", region_name=REGION),
            tagging=boto3.client("resourcegroupstaggingapi", region_name=REGION),
            bus=LocalEventBus(account=ACCOUNT, region=REGION, clock=clock),
            flow_arn=flow_arn,
        )

    def replace_fixtures(self, fixtures: ExecutionFixtures) -> None:
        """Swap the replayed executions (e.g. a scripted night). Handlers keep
        the same client, so their Deps stay valid."""
        self.fixtures = fixtures
        self.sfn.use_fixtures(fixtures)
