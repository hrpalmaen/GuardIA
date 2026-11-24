"""GuardIA-Flow: the `GuardIA-Incident-Flow` state machine and its Lambdas (D23).

PR 2 deploys the first state only, ClassifyIncident, so OpenIncident has a flow
to start and the classification can be seen in the cloud. The policy,
remediation, and escalation states are added by the next PRs (5.1, 5.4, 5.5,
6.4, 6.5) on this same state machine.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from components.function import GuardiaFunction, shared_layer

from .data_stack import DataStack

FLOW_NAME = "GuardIA-Incident-Flow"


class FlowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        layer = shared_layer(self, retain=retain_data)

        self.classify = GuardiaFunction(
            self,
            "ClassifyIncident",
            name="classify_incident",
            layer=layer,
            environment=data.table_env("Incidents", "ErrorCatalog", "IncidentEvents"),
            description="GuardIA ClassifyIncident (4.1): deterministic ErrorCatalog",
        )
        classify_fn = self.classify.function
        data.grant_items(classify_fn, "Incidents", "dynamodb:GetItem", "dynamodb:UpdateItem")
        data.grant_items(classify_fn, "ErrorCatalog", "dynamodb:Scan")
        data.grant_append_events(classify_fn)

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
        # Placeholder end until RemediationPolicy (PR 4) and escalation (PR 5).
        definition = classify.next(sfn.Succeed(self, "Classified"))

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
