"""Synth tests for GuardIA-Observability (T6 · tasks 8.1–8.3)."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from config import TEAMS_ALERTS_SECRET_NAME, OnCallConfig, environment
from stacks.data_stack import DataStack
from stacks.detection_stack import DetectionStack
from stacks.flow_stack import FlowStack
from stacks.observability_stack import ALARM_TOPIC_NAME, DASHBOARD_NAME, ObservabilityStack

NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
ENV = cdk.Environment(account="123456789012", region="us-east-1")
ONCALL = OnCallConfig(email="oncall@example.com")
TEAM_EMAIL = "team@example.com"


@pytest.fixture(scope="module")
def built() -> dict:
    config = environment("dev")
    app = cdk.App(context=NO_BUNDLING)
    data = DataStack(app, "D", env=ENV, thresholds=config.thresholds, retain_data=False)
    flow = FlowStack(app, "F", env=ENV, data=data, oncall=ONCALL, retain_data=False)
    detection = DetectionStack(
        app, "X", env=ENV, data=data, flow=flow.state_machine,
        thresholds=config.thresholds, retain_data=False,
    )  # fmt: skip
    functions = {**flow.functions, **detection.functions}
    observability = ObservabilityStack(
        app,
        "O",
        env=ENV,
        data=data,
        functions=functions,
        state_machines={
            "IncidentFlow": flow.state_machine,
            "EscalationFlow": flow.escalation_state_machine,
        },
        rules=detection.rules,
        alarm_email=TEAM_EMAIL,
        thresholds=config.thresholds,
        retain_data=False,
    )
    return {
        "template": Template.from_stack(observability),
        "functions": functions,
        "rules": detection.rules,
    }


def _alarms(template: Template) -> dict[str, dict]:
    """Alarm properties keyed by AlarmName."""
    return {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in template.find_resources("AWS::CloudWatch::Alarm").values()
    }


def _topic_logical_id(template: Template) -> str:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"TopicName": ALARM_TOPIC_NAME}}
    )
    assert len(topics) == 1
    return next(iter(topics))


# --------------------------------------------------------------------- topic
@pytest.mark.req("13.2")
def test_alarm_topic_emails_the_team_and_triggers_notify_alarm(built):
    template = built["template"]
    topic = _topic_logical_id(template)
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {"Protocol": "email", "Endpoint": TEAM_EMAIL, "TopicArn": {"Ref": topic}},
    )
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "lambda", "TopicArn": {"Ref": topic}}
    )


@pytest.mark.req("13.2")
def test_every_alarm_notifies_the_topic(built):
    template = built["template"]
    topic = _topic_logical_id(template)
    for name, props in _alarms(template).items():
        assert props.get("AlarmActions") == [{"Ref": topic}], name


# ------------------------------------------------------------------- lambdas
@pytest.mark.req("13.3")
def test_each_lambda_has_an_errors_and_a_dlq_alarm(built):
    alarms = _alarms(built["template"])
    assert built["functions"], "Flow and Detection must expose their functions (T1)"
    for key in built["functions"]:
        errors = alarms[f"GuardIA-{key}-Errors"]
        assert errors["MetricName"] == "Errors" and errors["Namespace"] == "AWS/Lambda"
        assert errors["Threshold"] == 0 and errors["ComparisonOperator"] == "GreaterThanThreshold"
        assert errors["Period"] == 300 and errors["TreatMissingData"] == "notBreaching"
        dlq = alarms[f"GuardIA-{key}-Dlq"]
        assert dlq["MetricName"] == "ApproximateNumberOfMessagesVisible"
        assert dlq["Namespace"] == "AWS/SQS" and dlq["Threshold"] == 0


def test_notify_alarm_has_no_errors_alarm_to_avoid_a_loop(built):
    alarms = _alarms(built["template"])
    assert "GuardIA-NotifyAlarm-Dlq" in alarms
    assert "GuardIA-NotifyAlarm-Errors" not in alarms


# ------------------------------------------------------------ state machines
@pytest.mark.req("13.3")
@pytest.mark.parametrize("key", ["IncidentFlow", "EscalationFlow"])
def test_failed_executions_of_both_flows_alarm(built, key):
    props = _alarms(built["template"])[f"GuardIA-{key}-ExecutionsFailed"]
    assert props["MetricName"] == "ExecutionsFailed" and props["Namespace"] == "AWS/States"
    assert props["Threshold"] == 0


@pytest.mark.req("13.3")
def test_each_detection_rule_alarms_on_failed_invocations(built):
    alarms = _alarms(built["template"])
    assert built["rules"]
    for key in built["rules"]:
        props = alarms[f"GuardIA-{key}-FailedInvocations"]
        assert props["MetricName"] == "FailedInvocations" and props["Namespace"] == "AWS/Events"
        assert [d["Name"] for d in props["Dimensions"]] == ["RuleName"]


# ----------------------------------------------------------------- heartbeat
@pytest.mark.req("13.2")
def test_heartbeat_alarm_treats_missing_data_as_breaching(built):
    props = _alarms(built["template"])["GuardIA-Heartbeat"]
    assert props["Namespace"] == "GuardIA" and props["MetricName"] == "Heartbeat"
    assert props["Dimensions"] == [{"Name": "Component", "Value": "MissedRunWatchdog"}]
    assert props["TreatMissingData"] == "breaching"
    assert props["ComparisonOperator"] == "LessThanThreshold" and props["Threshold"] == 1
    # 10 minutes without a heartbeat: two 5-minute periods.
    assert props["Period"] == 300 and props["EvaluationPeriods"] == 2


# --------------------------------------------------------------- notify alarm
@pytest.mark.req("12.3")
def test_notify_alarm_can_only_read_the_alerts_webhook_secret(built):
    template = built["template"]
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if "NotifyAlarm" in json.dumps(policy["Properties"]["Roles"]):
            statements += policy["Properties"]["PolicyDocument"]["Statement"]
    secret_statements = [s for s in statements if "secretsmanager" in json.dumps(s["Action"])]
    assert len(secret_statements) == 1
    assert secret_statements[0]["Action"] == "secretsmanager:GetSecretValue"
    assert secret_statements[0]["Resource"] != "*"
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Description": Match.string_like_regexp("NotifyAlarm"),
            "Environment": {
                "Variables": Match.object_like({"TEAMS_ALERTS_SECRET_ID": TEAMS_ALERTS_SECRET_NAME})
            },
        },
    )


# ----------------------------------------------------------------- dashboard
@pytest.mark.req("13.1")
def test_dashboard_exists(built):
    built["template"].has_resource_properties(
        "AWS::CloudWatch::Dashboard", {"DashboardName": DASHBOARD_NAME}
    )
