"""GuardIA-Flow: the `GuardIA-Incident-Flow` state machine and its Lambdas (D23).

This module only composes the flows. The branches live in their own modules
so several people can build them at the same time (T1):

    GuardIA-Incident-Flow:
        ClassifyIncident ─► RemediationPolicy ─► recovery / escalation  (flow_recovery, T4)

    GuardIA-Escalation-Flow (D25):
        the escalation chain                                           (flow_escalation, T5)

Escalation is its own state machine because the incident flow needs it in
several places (the policy decides to escalate, a recovery is rejected, and in
parallel with the recovery when little time is left), and Step Functions
states cannot be shared between those places. Each place runs it with
`escalate(...)` and waits for it (`.sync`). The execution is named after the
incident, so an incident is never escalated twice.

T8 adds InvokeAgent for `unknown` incidents before RemediationPolicy.
"""

from collections.abc import Mapping

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from components.function import GuardiaFunction, shared_layer
from config import DEFAULT_THRESHOLDS, OnCallConfig

from .data_stack import DataStack
from .flow_context import FlowContext
from .flow_escalation import build_escalation
from .flow_recovery import build_recovery

FLOW_NAME = "GuardIA-Incident-Flow"
ESCALATION_FLOW_NAME = "GuardIA-Escalation-Flow"
# Escalated incidents wait up to 6 h after the deadline (6.5).
FLOW_TIMEOUT = Duration.hours(24)


class FlowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        oncall: OnCallConfig,
        thresholds: Mapping[str, str] = DEFAULT_THRESHOLDS,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = FlowContext(
            scope=self, data=data, layer=shared_layer(self, retain=retain_data),
            thresholds=thresholds, oncall=oncall,
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
        self.escalation_state_machine = self._state_machine(
            "EscalationFlow", ESCALATION_FLOW_NAME, build_escalation(ctx)
        )
        definition = classify.next(build_recovery(ctx, self._escalate))
        self.state_machine = self._state_machine("IncidentFlow", FLOW_NAME, definition)

    def _state_machine(self, construct_id: str, name: str, definition) -> sfn.StateMachine:
        # Errors only, without execution data: inputs carry error causes.
        log_group = logs.LogGroup(
            self,
            "FlowLogs" if construct_id == "IncidentFlow" else f"{construct_id}Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return sfn.StateMachine(
            self,
            construct_id,
            state_machine_name=name,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=FLOW_TIMEOUT,
            logs=sfn.LogOptions(
                destination=log_group, level=sfn.LogLevel.ERROR, include_execution_data=False
            ),
        )

    def _escalate(self, state_name: str, next_state: sfn.IChainable) -> sfn.TaskStateBase:
        """A state that runs GuardIA-Escalation-Flow for this incident and waits for it.
        If the incident was already escalated, the existing escalation is left alone."""
        task = tasks.StepFunctionsStartExecution(
            self,
            f"{state_name}-Task",
            state_name=state_name,
            state_machine=self.escalation_state_machine,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            name=sfn.JsonPath.string_at("$.incident_id"),
            input=sfn.TaskInput.from_object(
                {
                    "incident_id": sfn.JsonPath.string_at("$.incident_id"),
                    "classification": sfn.JsonPath.object_at("$.classification"),
                    "policy": sfn.JsonPath.object_at("$.policy"),
                }
            ),
            associate_with_parent=True,
            result_path=sfn.JsonPath.DISCARD,
        )
        task.add_catch(
            next_state,
            errors=["StepFunctions.ExecutionAlreadyExistsException"],
            result_path=sfn.JsonPath.DISCARD,
        )
        task.next(next_state)
        return task

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
