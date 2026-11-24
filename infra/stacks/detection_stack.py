"""GuardIA-Detection: Level 0 in the cloud (tasks 2.2 and 3.2, D23).

* DiscoverProcesses every `discovery-interval-minutes` (EventBridge Scheduler).
* MissedRunWatchdog every `watchdog-interval-minutes`.
* OpenIncident behind two rules on the default bus: Step Functions status
  changes to a failure status (`STATUS_CHANGE_PATTERN`) and watchdog
  detections (`WATCHDOG_PATTERN`). Events of processes that are not in
  ProcessRegistry, including GuardIA's own flow, are ignored by OpenIncident.

Permissions on the monitored processes are read-only here (list, describe,
history). Redrive and relaunch come with RemediationPolicy (PR 4), limited to
`guardia:remediation=auto`.
"""

from collections.abc import Mapping

from aws_cdk import Duration, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from components.function import GuardiaFunction, shared_layer
from config import DEFAULT_THRESHOLDS
from guardia_common.incidents import STATUS_CHANGE_PATTERN, WATCHDOG_PATTERN

from .data_stack import DataStack

RULE_PATTERNS = {
    "StatusChangeToOpenIncident": STATUS_CHANGE_PATTERN,
    "WatchdogToOpenIncident": WATCHDOG_PATTERN,
}


def event_pattern(pattern: Mapping) -> events.EventPattern:
    """The shared dict pattern (also used by the local bus) as a CDK pattern."""
    known = {"source", "detail-type", "detail"}
    if set(pattern) - known:
        raise ValueError(f"unsupported pattern keys: {sorted(set(pattern) - known)}")
    return events.EventPattern(
        source=pattern.get("source"),
        detail_type=pattern.get("detail-type"),
        detail=pattern.get("detail"),
    )


class DetectionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        flow: sfn.IStateMachine,
        thresholds: Mapping[str, str] = DEFAULT_THRESHOLDS,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        layer = shared_layer(self, retain=retain_data)
        arn = f"arn:{self.partition}"
        state_machines = f"{arn}:states:{self.region}:{self.account}:stateMachine:*"
        executions = f"{arn}:states:{self.region}:{self.account}:execution:*"

        # ------------------------------------------------------------ discovery
        self.discover = GuardiaFunction(
            self,
            "DiscoverProcesses",
            name="discover_processes",
            layer=layer,
            environment=data.table_env("ProcessRegistry"),
            timeout_seconds=120,
            description="GuardIA DiscoverProcesses (2.1): tagged processes and schedules",
        )
        discover_fn = self.discover.function
        data.grant_items(
            discover_fn, "ProcessRegistry", "dynamodb:Scan", "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        )  # fmt: skip
        data.grant_read_thresholds(discover_fn)
        discover_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="FindTaggedProcessesAndSchedules",
                actions=["tag:GetResources", "scheduler:ListSchedules"],
                resources=["*"],  # these list actions do not support resource ARNs
            )
        )
        discover_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadSchedules",
                actions=[
                    "events:ListRuleNamesByTarget",
                    "events:DescribeRule",
                    "scheduler:GetSchedule",
                ],
                resources=[
                    f"{arn}:events:{self.region}:{self.account}:rule/*",
                    f"{arn}:scheduler:{self.region}:{self.account}:schedule/*/*",
                ],
            )
        )
        discover_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadExecutions", actions=["states:ListExecutions"], resources=[state_machines]
            )
        )
        self._every(
            "DiscoverProcessesSchedule",
            discover_fn,
            minutes=int(thresholds["discovery-interval-minutes"]),
            description="GuardIA: discover tagged processes",
        )

        # ------------------------------------------------------------ watchdog
        self.watchdog = GuardiaFunction(
            self,
            "MissedRunWatchdog",
            name="missed_run_watchdog",
            layer=layer,
            environment={
                **data.table_env("ProcessRegistry", "Incidents"),
                "WATCHDOG_EVENT_BUS": "default",
            },
            timeout_seconds=120,
            description="GuardIA MissedRunWatchdog (3.4): MISSED_RUN and STUCK_RUN",
        )
        watchdog_fn = self.watchdog.function
        data.grant_items(watchdog_fn, "ProcessRegistry", "dynamodb:Scan")
        data.grant_items(watchdog_fn, "Incidents", "dynamodb:GetItem")
        data.grant_read_thresholds(watchdog_fn)
        watchdog_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadExecutions", actions=["states:ListExecutions"], resources=[state_machines]
            )
        )
        events.EventBus.from_event_bus_name(self, "DefaultBus", "default").grant_put_events_to(
            watchdog_fn
        )
        self._every(
            "MissedRunWatchdogSchedule",
            watchdog_fn,
            minutes=int(thresholds["watchdog-interval-minutes"]),
            description="GuardIA: missed and stuck executions",
        )

        # ------------------------------------------------------- open incident
        self.open_incident = GuardiaFunction(
            self,
            "OpenIncident",
            name="open_incident",
            layer=layer,
            environment={
                **data.table_env("Incidents", "ProcessRegistry", "IncidentEvents"),
                "INCIDENT_FLOW_ARN": flow.state_machine_arn,
            },
            timeout_seconds=60,
            description="GuardIA OpenIncident (3.1): one incident per execution",
        )
        open_fn = self.open_incident.function
        data.grant_items(
            open_fn, "Incidents", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"
        )
        data.grant_items(open_fn, "ProcessRegistry", "dynamodb:GetItem")
        data.grant_append_events(open_fn)
        data.grant_read_thresholds(open_fn)
        open_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadFailedExecution",
                actions=["states:DescribeExecution", "states:GetExecutionHistory"],
                resources=[executions],
            )
        )
        flow.grant_start_execution(open_fn)

        # Every Lambda of this stack, for the Observability alarms (T6).
        self.functions: dict[str, GuardiaFunction] = {
            "DiscoverProcesses": self.discover,
            "MissedRunWatchdog": self.watchdog,
            "OpenIncident": self.open_incident,
        }

        self.rules = {
            name: events.Rule(
                self,
                name,
                description=f"GuardIA: {name}",
                event_pattern=event_pattern(pattern),
                targets=[
                    targets.LambdaFunction(
                        open_fn,
                        dead_letter_queue=self.open_incident.dlq,
                        retry_attempts=3,
                        max_event_age=Duration.hours(2),
                    )
                ],
            )
            for name, pattern in RULE_PATTERNS.items()
        }

    def _every(self, construct_id: str, fn, *, minutes: int, description: str) -> None:
        scheduler.Schedule(
            self,
            construct_id,
            schedule=scheduler.ScheduleExpression.rate(Duration.minutes(minutes)),
            target=scheduler_targets.LambdaInvoke(fn, retry_attempts=2),
            description=description,
        )
