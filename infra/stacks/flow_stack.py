"""GuardIA-Flow: the `GuardIA-Incident-Flow` state machine and its Lambdas (D23).

This module only composes the flow. The branches live in their own modules so
several people can build them at the same time (T1):

    ClassifyIncident ─► RouteByClassification
                          ├─ auto_recoverable ─► recovery    (flow_recovery, T4)
                          └─ otherwise ────────► escalation  (flow_escalation, T5)

T4 replaces the routing Choice with RemediationPolicy, and T8 adds
InvokeAgent for `unknown` incidents.
"""

from collections.abc import Mapping

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from components.function import GuardiaFunction, shared_layer
from config import DEFAULT_THRESHOLDS

from .data_stack import DataStack
from .flow_context import FlowContext
from .flow_escalation import build_escalation
from .flow_recovery import build_recovery

FLOW_NAME = "GuardIA-Incident-Flow"


class FlowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        thresholds: Mapping[str, str] = DEFAULT_THRESHOLDS,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = FlowContext(
            scope=self, data=data, layer=shared_layer(self, retain=retain_data),
            thresholds=thresholds,
        )  # fmt: skip
        self.functions = ctx.functions

        self._update_status_function(ctx)
        classify_fn = self._classify_function(ctx)

        classify = tasks.LambdaInvoke(
            self,
            "ClassifyIncident-Task",
            state_name="ClassifyIncident",
            lambda_function=classify_fn,
            payload=sfn.TaskInput.from_object(
                {"incident_id": sfn.JsonPath.string_at("$.incident_id")}
            ),
            payload_response_only=True,
            result_path="$.classification",
            retry_on_service_exceptions=True,
        )
        escalation = build_escalation(ctx)
        recovery = build_recovery(ctx, escalation)
        route = (
            sfn.Choice(self, "RouteByClassification")
            .when(
                sfn.Condition.string_equals("$.classification.classification", "auto_recoverable"),
                recovery,
            )
            .otherwise(escalation)
        )
        definition = classify.next(route)

        # Errors only, without execution data: inputs carry error causes.
        self.log_group = logs.LogGroup(
            self,
            "FlowLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "IncidentFlow",
            state_machine_name=FLOW_NAME,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            # Escalated incidents wait up to 6 h after the deadline (6.5).
            timeout=Duration.hours(24),
            logs=sfn.LogOptions(
                destination=self.log_group,
                level=sfn.LogLevel.ERROR,
                include_execution_data=False,
            ),
        )

    def _update_status_function(self, ctx: FlowContext) -> None:
        function = ctx.add_function(
            "UpdateIncidentStatus",
            GuardiaFunction(
                self,
                "UpdateIncidentStatus",
                name="update_incident_status",
                layer=ctx.layer,
                environment=ctx.data.table_env("Incidents", "IncidentEvents"),
                description="GuardIA UpdateIncidentStatus (T1): status transitions",
            ),
        ).function
        ctx.data.grant_items(function, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem")
        ctx.data.grant_append_events(function)

    def _classify_function(self, ctx: FlowContext):
        function = ctx.add_function(
            "ClassifyIncident",
            GuardiaFunction(
                self,
                "ClassifyIncident",
                name="classify_incident",
                layer=ctx.layer,
                environment=ctx.data.table_env("Incidents", "ErrorCatalog", "IncidentEvents"),
                description="GuardIA ClassifyIncident (4.1): deterministic ErrorCatalog",
            ),
        ).function
        ctx.data.grant_items(function, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem")
        ctx.data.grant_items(function, "ErrorCatalog", "dynamodb:Scan")
        ctx.data.grant_append_events(function)
        return function
