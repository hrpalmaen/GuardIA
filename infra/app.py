#!/usr/bin/env python3
"""GuardIA CDK app.

    cdk deploy -c env=dev --profile analytics GuardIA-Data

`env` (dev or prod) selects the configuration in `config.ENVIRONMENTS`
(decision D17). It is required: there is no default environment.
"""

import os

import aws_cdk as cdk

from config import environment
from stacks.data_stack import DataStack

app = cdk.App()
config = environment(app.node.try_get_context("env"))

aws_env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

DataStack(
    app,
    "GuardIA-Data",
    env=aws_env,
    thresholds=config.thresholds,
    retain_data=config.retain_data,
)

cdk.Tags.of(app).add("project", "guardia")
cdk.Tags.of(app).add("guardia:environment", config.name)

app.synth()
