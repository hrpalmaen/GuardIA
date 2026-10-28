#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.data_stack import DataStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

DataStack(app, "GuardIA-Data", env=env)

cdk.Tags.of(app).add("project", "guardia")

app.synth()
