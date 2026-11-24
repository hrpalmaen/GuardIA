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
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from components.function import GuardiaFunction

from .data_stack import DataStack


@dataclass
class FlowContext:
    scope: Stack
    data: DataStack
    layer: lambda_.ILayerVersion
    thresholds: Mapping[str, str]
    functions: dict[str, GuardiaFunction] = field(default_factory=dict)

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
