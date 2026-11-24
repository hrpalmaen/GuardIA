#!/usr/bin/env python3
"""GuardIA CDK app.

    cdk deploy -c env=dev --profile analytics --all

<<<<<<< HEAD
Stacks, in dependency order: GuardIA-Data → GuardIA-Flow → GuardIA-Detection
→ GuardIA-Observability.
=======
Stacks, in dependency order: GuardIA-Data → GuardIA-Flow → GuardIA-Detection,
plus GuardIA-Sim (test account only, after GuardIA-Data).
>>>>>>> origin/main

`env` (dev, workshop, or prod) selects the configuration in `config.ENVIRONMENTS`
(decision D17). It is required: there is no default environment.

The on-call engineer (D19) comes from GUARDIA_ONCALL_EMAIL, GUARDIA_ONCALL_NAME
and GUARDIA_ONCALL_BACKUP_EMAIL (see .env.example). `cdk synth` fails without
the email. GuardIA's own alarms go to GUARDIA_ALARM_EMAIL (T6); `cdk synth`
also fails without it.
"""

import os

import aws_cdk as cdk

<<<<<<< HEAD
from config import alarm_email_from_environment, environment, oncall_from_environment
from stacks.data_stack import DataStack
from stacks.detection_stack import DetectionStack
from stacks.flow_stack import FlowStack
from stacks.observability_stack import ObservabilityStack
=======
from config import TEAMS_ALERTS_SECRET_NAME, environment, oncall_from_environment
from stacks.data_stack import DataStack
from stacks.detection_stack import DetectionStack
from stacks.flow_stack import FlowStack
from stacks.sim_stack import SimStack
>>>>>>> origin/main

app = cdk.App()
config = environment(app.node.try_get_context("env"))
alarm_email = alarm_email_from_environment()

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
detection = DetectionStack(
    app,
    "GuardIA-Detection",
    env=aws_env,
    data=data,
    flow=flow.state_machine,
    thresholds=config.thresholds,
    retain_data=config.retain_data,
)
duplicated = set(flow.functions) & set(detection.functions)
if duplicated:
    raise ValueError(f"function keys used by two stacks: {sorted(duplicated)}")
ObservabilityStack(
    app,
    "GuardIA-Observability",
    env=aws_env,
    data=data,
    functions={**flow.functions, **detection.functions},
    state_machines={
        "IncidentFlow": flow.state_machine,
        "EscalationFlow": flow.escalation_state_machine,
    },
    rules=detection.rules,
    alarm_email=alarm_email,
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
