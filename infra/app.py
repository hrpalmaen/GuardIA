#!/usr/bin/env python3
"""GuardIA CDK app.

    cdk deploy -c env=dev --profile analytics --all

Stacks, in dependency order: GuardIA-Data → GuardIA-Flow → GuardIA-Detection.

`env` (dev, workshop, or prod) selects the configuration in `config.ENVIRONMENTS`
(decision D17). It is required: there is no default environment.

The on-call engineer (D19) comes from GUARDIA_ONCALL_EMAIL, GUARDIA_ONCALL_NAME
and GUARDIA_ONCALL_BACKUP_EMAIL (see .env.example). `cdk synth` fails without
the email.
"""

import os

import aws_cdk as cdk

from config import environment, oncall_from_environment
from stacks.data_stack import DataStack
from stacks.detection_stack import DetectionStack
from stacks.flow_stack import FlowStack

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
    agent_provider=config.agent_provider,
)
flow = FlowStack(
    app,
    "GuardIA-Flow",
    env=aws_env,
    data=data,
    oncall=oncall_from_environment(),
    agent=config.agent,
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

cdk.Tags.of(app).add("project", "guardia")
cdk.Tags.of(app).add("guardia:environment", config.name)

app.synth()
