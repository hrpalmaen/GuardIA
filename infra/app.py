#!/usr/bin/env python3
"""GuardIA CDK app.

    cdk deploy -c env=dev --profile analytics --all

Stacks, in dependency order: GuardIA-Data → GuardIA-Flow → GuardIA-Detection,
plus GuardIA-Sim (test account only, after GuardIA-Data).

`env` (dev or prod) selects the configuration in `config.ENVIRONMENTS`
(decision D17). It is required: there is no default environment.
"""

import os

import aws_cdk as cdk

from config import TEAMS_ALERTS_SECRET_NAME, environment
from stacks.data_stack import DataStack
from stacks.detection_stack import DetectionStack
from stacks.flow_stack import FlowStack
from stacks.sim_stack import SimStack

app = cdk.App()
config = environment(app.node.try_get_context("env"))

aws_env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

data = DataStack(
    app,
    "GuardIA-Data",
    env=aws_env,
    thresholds=config.thresholds,
    retain_data=config.retain_data,
)
flow = FlowStack(
    app,
    "GuardIA-Flow",
    env=aws_env,
    data=data,
    thresholds=config.thresholds,
    retain_data=config.retain_data,
)
DetectionStack(
    app,
    "GuardIA-Detection",
    env=aws_env,
    data=data,
    flow=flow.state_machine,
    thresholds=config.thresholds,
    retain_data=config.retain_data,
)

if config.deploy_sim:
    # Test-only simulated processes (T3); never deployed to production.
    SimStack(
        app,
        "GuardIA-Sim",
        env=aws_env,
        config=config,
        teams_alerts_secret=data.secrets[TEAMS_ALERTS_SECRET_NAME],
    )

cdk.Tags.of(app).add("project", "guardia")
cdk.Tags.of(app).add("guardia:environment", config.name)

app.synth()
