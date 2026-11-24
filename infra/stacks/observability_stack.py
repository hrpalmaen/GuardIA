"""GuardIA-Observability: GuardIA watching itself (tasks 8.1–8.3 · requirements 13.1–13.3).

Deployed last (D23). Everything alarms to one SNS topic, `guardia-alarms`, with
two subscribers that do not depend on each other (13.2, D21):

* the team's email (`GUARDIA_ALARM_EMAIL`), confirmed once after the first deploy;
* `NotifyAlarm`, which posts a card to the Teams alerts channel.

Alarms (all evaluated every 5 minutes):

* each GuardIA Lambda: `Errors > 0`, and messages visible in its DLQ;
* each state machine (incident and escalation flows): `ExecutionsFailed > 0`.
  A Teams outage on the first escalation message ends GuardIA-Escalation-Flow
  in `NotificationFailed`; that is how it reaches the team (D21, D28);
* each Detection rule: `FailedInvocations > 0` (EventBridge could not invoke
  OpenIncident);
* the heartbeat: `GuardIA/Heartbeat` from MissedRunWatchdog missing for
  `heartbeat-missing-minutes`, with missing data treated as breaching (8.2).

`NotifyAlarm` gets only the DLQ alarm, not `Errors`: its `Errors` alarm would
notify NotifyAlarm itself in a loop while Teams is down (D28).

The dashboard `GuardIA` shows the alarms, the flows, errors per Lambda, the
DLQs, and the heartbeat (8.1). Detection, first action, and resolution time
metrics are not emitted yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from components.function import GuardiaFunction, shared_layer
from config import DEFAULT_THRESHOLDS, TEAMS_ALERTS_SECRET_NAME
from guardia_common.metrics import HEARTBEAT_DIMENSIONS, HEARTBEAT_METRIC, NAMESPACE

from .data_stack import DataStack

ALARM_TOPIC_NAME = "guardia-alarms"
DASHBOARD_NAME = "GuardIA"
ALARM_PREFIX = "GuardIA"
PERIOD = Duration.minutes(5)


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        functions: Mapping[str, GuardiaFunction],
        state_machines: Mapping[str, sfn.IStateMachine],
        rules: Mapping[str, events.IRule],
        alarm_email: str,
        thresholds: Mapping[str, str] = DEFAULT_THRESHOLDS,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.alarms: dict[str, cloudwatch.Alarm] = {}

        # ------------------------------------------------------------- topic
        self.topic = sns.Topic(
            self,
            "AlarmsTopic",
            topic_name=ALARM_TOPIC_NAME,
            display_name="GuardIA alarms",
            enforce_ssl=True,
        )
        self.topic.add_subscription(subs.EmailSubscription(alarm_email))
        self._action = cw_actions.SnsAction(self.topic)

        # ------------------------------------------------- Teams, independent path
        self.notify_alarm = GuardiaFunction(
            self,
            "NotifyAlarm",
            name="notify_alarm",
            layer=shared_layer(self, retain=retain_data),
            environment={
                "TEAMS_ALERTS_SECRET_ID": TEAMS_ALERTS_SECRET_NAME,
                "GUARDIA_TIMEZONE": thresholds["timezone"],
            },
            description="GuardIA NotifyAlarm (8.2): GuardIA alarms to Teams",
        )
        data.grant_read_secret(self.notify_alarm.function, TEAMS_ALERTS_SECRET_NAME)
        self.topic.add_subscription(subs.LambdaSubscription(self.notify_alarm.function))

        # ------------------------------------------------------------ Lambdas
        for key, function in functions.items():
            self._alarm(
                f"{key}-Errors",
                function.function.metric_errors(period=PERIOD, statistic="Sum"),
                f"The Lambda {key} failed at least once in 5 minutes.",
            )
            self._dlq_alarm(key, function)
        self._dlq_alarm("NotifyAlarm", self.notify_alarm)  # no Errors alarm: see module doc

        # ----------------------------------------------------- state machines
        for key, machine in state_machines.items():
            self._alarm(
                f"{key}-ExecutionsFailed",
                machine.metric_failed(period=PERIOD, statistic="Sum"),
                f"An execution of {key} failed (includes NotificationFailed when Teams is down).",
            )

        # ------------------------------------------------------ detection rules
        for key, rule in rules.items():
            self._alarm(
                f"{key}-FailedInvocations",
                cloudwatch.Metric(
                    namespace="AWS/Events",
                    metric_name="FailedInvocations",
                    dimensions_map={"RuleName": rule.rule_name},
                    period=PERIOD,
                    statistic="Sum",
                ),
                f"EventBridge could not deliver events of rule {key} to OpenIncident.",
            )

        # ----------------------------------------------------------- heartbeat
        interval = int(thresholds["watchdog-interval-minutes"])
        missing = int(thresholds["heartbeat-missing-minutes"])
        self.heartbeat_metric = cloudwatch.Metric(
            namespace=NAMESPACE,
            metric_name=HEARTBEAT_METRIC,
            dimensions_map=dict(HEARTBEAT_DIMENSIONS),
            period=Duration.minutes(interval),
            statistic="Sum",
        )
        self._alarm(
            "Heartbeat",
            self.heartbeat_metric,
            f"MissedRunWatchdog has not finished a cycle in {missing} minutes: "
            "GuardIA may have stopped watching.",
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=max(1, math.ceil(missing / interval)),
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )

        self.dashboard = self._dashboard(functions, state_machines)

    # ------------------------------------------------------------------ helpers
    def _alarm(
        self,
        key: str,
        metric: cloudwatch.IMetric,
        description: str,
        *,
        threshold: float = 0,
        comparison: cloudwatch.ComparisonOperator = (
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
        ),
        evaluation_periods: int = 1,
        treat_missing_data: cloudwatch.TreatMissingData = cloudwatch.TreatMissingData.NOT_BREACHING,
    ) -> cloudwatch.Alarm:
        if key in self.alarms:
            raise ValueError(f"alarm '{key}' is already defined")
        alarm = cloudwatch.Alarm(
            self,
            f"Alarm-{key}",
            alarm_name=f"{ALARM_PREFIX}-{key}",
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            comparison_operator=comparison,
            evaluation_periods=evaluation_periods,
            treat_missing_data=treat_missing_data,
        )
        alarm.add_alarm_action(self._action)
        self.alarms[key] = alarm
        return alarm

    def _dlq_alarm(self, key: str, function: GuardiaFunction) -> None:
        self._alarm(
            f"{key}-Dlq",
            function.dlq.metric_approximate_number_of_messages_visible(
                period=PERIOD, statistic="Maximum"
            ),
            f"Asynchronous invocations of {key} failed every retry and are in its DLQ.",
        )

    def _dashboard(
        self,
        functions: Mapping[str, GuardiaFunction],
        state_machines: Mapping[str, sfn.IStateMachine],
    ) -> cloudwatch.Dashboard:
        dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=DASHBOARD_NAME,
            default_interval=Duration.hours(12),
        )
        flow_metrics: list[cloudwatch.IMetric] = []
        for key, machine in state_machines.items():
            flow_metrics += [
                machine.metric_started(period=PERIOD, statistic="Sum", label=f"{key} started"),
                machine.metric_succeeded(period=PERIOD, statistic="Sum", label=f"{key} succeeded"),
                machine.metric_failed(period=PERIOD, statistic="Sum", label=f"{key} failed"),
            ]
        all_functions = {**functions, "NotifyAlarm": self.notify_alarm}
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    "# GuardIA\n"
                    "Incidents opened = executions started of IncidentFlow (one per incident). "
                    "Alarms go to the `guardia-alarms` topic: team email and Teams."
                ),
                width=24,
                height=2,
            )
        )
        dashboard.add_widgets(
            cloudwatch.AlarmStatusWidget(
                title="Alarms", alarms=list(self.alarms.values()), width=24, height=6
            )
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Flows", left=flow_metrics, width=12),
            cloudwatch.GraphWidget(
                title="Heartbeat (MissedRunWatchdog)",
                left=[self.heartbeat_metric.with_(label="Heartbeat")],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda errors",
                left=[
                    f.function.metric_errors(period=PERIOD, statistic="Sum", label=key)
                    for key, f in all_functions.items()
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Messages in DLQs",
                left=[
                    f.dlq.metric_approximate_number_of_messages_visible(
                        period=PERIOD, statistic="Maximum", label=key
                    )
                    for key, f in all_functions.items()
                ],
                width=12,
            ),
        )
        return dashboard
