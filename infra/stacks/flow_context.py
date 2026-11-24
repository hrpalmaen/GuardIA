"""What the branches of GuardIA-Incident-Flow share (T1).

`FlowStack` composes the flow; each branch is built in its own module so the
recovery (T4), escalation (T5), and agent (T8) work can happen in parallel:

    flow_recovery.build_recovery(ctx, escalation)  -> sfn.IChainable
    flow_escalation.build_escalation(ctx)          -> sfn.IChainable

Branches register their Lambdas with `ctx.add_function`, so Observability
(T6) finds every function of the flow in `FlowStack.functions`, and change
the incident status only through `ctx.update_status`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from components.function import GuardiaFunction
from config import AgentConfig, OnCallConfig

from .data_stack import DataStack


@dataclass
class FlowContext:
    scope: Stack
    data: DataStack
    layer: lambda_.ILayerVersion
    thresholds: Mapping[str, str]
    functions: dict[str, GuardiaFunction] = field(default_factory=dict)
    oncall: OnCallConfig | None = None  # D19; required by the escalation branch (T5)
    agent: AgentConfig | None = None  # T8; None: no agent, `unknown` escalates

    def add_function(self, key: str, function: GuardiaFunction) -> GuardiaFunction:
        if key in self.functions:
            raise ValueError(f"flow function '{key}' is already registered")
        self.functions[key] = function
        return function

    def update_status(
        self,
        state_name: str,
        status: str,
        *,
        event_type: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> tasks.LambdaInvoke:
        """A state that sets the incident status (UpdateIncidentStatus). Its
        output is discarded, so the flow state is not changed."""
        payload: dict[str, object] = {
            "incident_id": sfn.JsonPath.string_at("$.incident_id"),
            "status": status,
        }
        if event_type:
            payload["event_type"] = event_type
        if details:
            payload["details"] = dict(details)
        return tasks.LambdaInvoke(
            self.scope,
            f"{state_name}-Task",
            state_name=state_name,
            lambda_function=self.functions["UpdateIncidentStatus"].function,
            payload=sfn.TaskInput.from_object(payload),
            result_path=sfn.JsonPath.DISCARD,
            retry_on_service_exceptions=True,
        )

    def invoke(
        self,
        state_name: str,
        function: GuardiaFunction,
        payload: Mapping[str, object],
        result_path: str,
    ) -> tasks.LambdaInvoke:
        """A Lambda state that keeps the flow state and adds the Lambda's answer
        at `result_path` (the contract of the flow state keys)."""
        return tasks.LambdaInvoke(
            self.scope,
            f"{state_name}-Task",
            state_name=state_name,
            lambda_function=function.function,
            payload=sfn.TaskInput.from_object(dict(payload)),
            payload_response_only=True,
            result_path=result_path,
            retry_on_service_exceptions=True,
        )

    def grant_task_response(self, function: lambda_.IFunction) -> None:
        """Lets `function` answer a `.waitForTaskToken` state (SendTaskSuccess).

        The resource is `*` on purpose: IAM has no resource type for the task
        response actions (the token is what authorizes the call), and naming
        the state machine here would make the function depend on it while the
        state machine depends on the function.
        """
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AnswerTaskToken",
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],
            )
        )
