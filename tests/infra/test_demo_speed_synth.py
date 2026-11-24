"""A fast demo shortens the waits, not the time budget of the recovery (D33)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from config import DEFAULT_THRESHOLDS, OnCallConfig, environment
from stacks.data_stack import DataStack
from stacks.flow_stack import FLOW_NAME, FlowStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")
ONCALL = OnCallConfig(email="oncall@example.com", name="Ana Guardia", backup_email=None)


def _states(thresholds: dict[str, str]) -> dict:
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV, retain_data=False)
    flow = FlowStack(
        app, "F", env=ENV, data=data, oncall=ONCALL, thresholds=thresholds, retain_data=False
    )
    template = Template.from_stack(flow)
    for machine in template.find_resources("AWS::StepFunctions::StateMachine").values():
        if machine["Properties"].get("StateMachineName") != FLOW_NAME:
            continue
        body = machine["Properties"]["DefinitionString"]
        parts = body["Fn::Join"][1] if not isinstance(body, str) else [body]
        return json.loads("".join(p if isinstance(p, str) else "ARN" for p in parts))["States"]
    raise AssertionError("no incident flow")


@pytest.mark.req("5.4")
def test_default_thresholds_keep_the_three_minute_poll_and_ten_polls() -> None:
    states = _states(dict(DEFAULT_THRESHOLDS))

    assert states["WaitBeforePoll"]["Seconds"] == 180
    assert states["RecoveryFinished?"]["Choices"][2]["NumericLessThan"] == 10


@pytest.mark.req("5.4")
def test_a_five_second_poll_keeps_the_same_thirty_minute_budget() -> None:
    states = _states(environment("dev").thresholds)

    assert states["WaitBeforePoll"]["Seconds"] == 5
    assert states["RecoveryFinished?"]["Choices"][2]["NumericLessThan"] == 360


def test_a_poll_interval_below_one_second_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _states({**DEFAULT_THRESHOLDS, "redrive-poll-interval-seconds": "0"})
