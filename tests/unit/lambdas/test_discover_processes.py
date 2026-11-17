"""DiscoverProcesses (task 2.1) against the real state machines of the fixtures,
created in moto with their guardia:* tags and the rule that launches them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from moto import mock_aws

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from guardia_common.models import ProcessRecord
from guardia_local.environment import ROLE_ARN, LocalEnvironment, create_state_machine
from lambdas.discover_processes.handler import Deps, discover_processes

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)  # 00:00 Bogota
EMT12 = "arn:aws:states:us-east-1:123456789012:stateMachine:BI-EMT12-GCRAW-Orchestrator-ConversationsDetail"  # noqa: E501
COMPLETE_TAGS = {
    "guardia:monitor": "true",
    "guardia:process-type": "master",
    "guardia:organization": "EMT99",
    "guardia:remediation": "auto",
}


@pytest.fixture
def env(real_executions):
    with mock_aws():
        yield LocalEnvironment.create(
            real_executions, start=NOW, thresholds=DEFAULT_THRESHOLDS, ssm_prefix=SSM_PREFIX
        )


def deps(env) -> Deps:
    return Deps(
        registry=env.tables["ProcessRegistry"],
        tagging=env.tagging,
        events=env.events,
        scheduler=env.scheduler,
        sfn=env.sfn,
        ssm=env.ssm,
    )


def registry(env) -> dict[str, ProcessRecord]:
    items = env.tables["ProcessRegistry"].scan()["Items"]
    return {item["resource_arn"]: ProcessRecord.from_item(item) for item in items}


def codes(result) -> set[tuple[str, str]]:
    return {(w["resource_arn"].rsplit(":", 1)[-1], w["code"]) for w in result["warnings"]}


# --------------------------------------------------------------------------- 1.1
@pytest.mark.req("1.1")
def test_real_processes_are_registered_with_the_rule_that_launches_them(env):
    result = discover_processes(deps(env), now=NOW)
    rows = registry(env)

    assert len(result["monitored"]) == 3 and set(rows) == set(env.fixtures.state_machines)
    emt12 = rows[EMT12]
    assert (emt12.organization, emt12.process_type, emt12.remediation) == (
        "EMT12", "conversations", "auto",
    )  # fmt: skip
    assert [(s.expression, s.timezone, s.source) for s in emt12.schedules] == [
        ("cron(30 * * * ? *)", "UTC", "events-rule")
    ]
    assert emt12.first_seen_iso == emt12.last_seen_iso == "2026-11-16T00:00:00-05:00"
    assert result["warnings"] == []


@pytest.mark.req("1.1")
def test_scheduler_schedules_are_read_with_their_time_zone(env):
    env.scheduler.create_schedule(
        Name="emt12-nightly",
        ScheduleExpression="cron(0 3 * * ? *)",
        ScheduleExpressionTimezone="America/Bogota",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={"Arn": EMT12, "RoleArn": ROLE_ARN},
    )
    discover_processes(deps(env), now=NOW)
    schedules = {(s.source, s.expression, s.timezone) for s in registry(env)[EMT12].schedules}
    assert ("scheduler", "cron(0 3 * * ? *)", "America/Bogota") in schedules


def test_disabled_rules_are_ignored_and_a_process_without_schedule_is_warned(env):
    create_state_machine(env.moto_sfn, env.events, "paused", COMPLETE_TAGS,
                         schedule="cron(0 * * * ? *)", rule_state="DISABLED")  # fmt: skip
    result = discover_processes(deps(env), now=NOW)
    paused = next(r for a, r in registry(env).items() if a.endswith(":paused"))

    assert paused.schedules == []
    assert ("paused", "no_schedule") in codes(result)


# --------------------------------------------------------------------------- 1.2
@pytest.mark.req("1.2")
@pytest.mark.parametrize("missing", [
    "guardia:process-type", "guardia:organization", "guardia:remediation",
])  # fmt: skip
def test_resource_missing_a_mandatory_tag_is_excluded_with_a_warning(env, missing):
    tags = {k: v for k, v in COMPLETE_TAGS.items() if k != missing}
    create_state_machine(env.moto_sfn, env.events, "incomplete", tags, "cron(0 * * * ? *)")
    result = discover_processes(deps(env), now=NOW)

    assert not any(arn.endswith(":incomplete") for arn in registry(env))
    assert ("incomplete", "missing_tags") in codes(result)
    assert missing in result["warnings"][0]["detail"]


def test_resource_without_the_monitor_tag_is_not_discovered(env):
    tags = {**COMPLETE_TAGS, "guardia:monitor": "false"}
    create_state_machine(env.moto_sfn, env.events, "not-monitored", tags)
    discover_processes(deps(env), now=NOW)
    assert not any(arn.endswith(":not-monitored") for arn in registry(env))


# --------------------------------------------------------------------------- 1.3
@pytest.mark.req("1.3")
def test_new_process_is_monitored_in_the_next_cycle(env):
    discover_processes(deps(env), now=NOW)
    create_state_machine(env.moto_sfn, env.events, "master-emt99", COMPLETE_TAGS,
                         "cron(0 8 * * ? *)")  # fmt: skip
    discover_processes(deps(env), now=NOW + timedelta(minutes=15))

    new = next(r for a, r in registry(env).items() if a.endswith(":master-emt99"))
    assert new.organization == "EMT99" and new.schedules[0].expression == "cron(0 8 * * ? *)"
    assert new.first_seen_iso == "2026-11-16T00:15:00-05:00"


def test_first_seen_is_kept_and_last_seen_moves(env):
    discover_processes(deps(env), now=NOW)
    discover_processes(deps(env), now=NOW + timedelta(minutes=15))
    emt12 = registry(env)[EMT12]
    assert emt12.first_seen_iso == "2026-11-16T00:00:00-05:00"
    assert emt12.last_seen_iso == "2026-11-16T00:15:00-05:00"


def test_untagged_process_is_removed_from_the_registry(env):
    discover_processes(deps(env), now=NOW)
    env.moto_sfn.untag_resource(resourceArn=EMT12, tagKeys=["guardia:monitor"])
    result = discover_processes(deps(env), now=NOW + timedelta(minutes=15))

    assert EMT12 not in registry(env) and result["removed"] == [EMT12]


def test_fields_set_by_an_operator_are_kept(env):
    env.tables["ProcessRegistry"].put_item(
        Item={
            "resource_arn": EMT12,
            "process_type": "conversations",
            "organization": "EMT12",
            "deadline_local": "07:30",
            "timezone": "America/Bogota",
        }  # fmt: skip
    )
    discover_processes(deps(env), now=NOW)
    emt12 = registry(env)[EMT12]
    assert (emt12.deadline_local, emt12.timezone) == ("07:30", "America/Bogota")


# --------------------------------------------------------------------------- 1.4
@pytest.mark.req("1.4")
def test_invalid_remediation_is_treated_as_escalate_only(env):
    create_state_machine(env.moto_sfn, env.events, "typo",
                         {**COMPLETE_TAGS, "guardia:remediation": "automatic"})  # fmt: skip
    result = discover_processes(deps(env), now=NOW)

    typo = next(r for a, r in registry(env).items() if a.endswith(":typo"))
    assert typo.remediation == "escalate-only"
    assert ("typo", "invalid_remediation") in codes(result)


# --------------------------------------------------------------------------- 3.2 / 3.3 inputs
@pytest.mark.req("3.3")
def test_with_few_executions_there_is_no_median(env):
    discover_processes(deps(env), now=NOW)
    emt12 = registry(env)[EMT12]
    assert emt12.duration_samples == 1 and emt12.median_duration_s is None


@pytest.mark.req("3.2")
def test_median_of_the_last_successful_executions(env):
    success = env.fixtures.by_id("emt12-20261113-1630-succeeded")
    runs = [
        success.replayed_as(
            f"run-{i}",
            success.start_date + timedelta(hours=i + 1),
            duration=timedelta(seconds=seconds),
        )  # fmt: skip
        for i, seconds in enumerate([100, 120, 140, 160, 900])
    ]
    env.replace_fixtures(env.fixtures.with_executions(runs))
    discover_processes(deps(env), now=NOW)

    emt12 = registry(env)[EMT12]
    # 6 successes: the original (143 s) and the 5 above; the failed run does not count.
    assert emt12.duration_samples == 6
    assert emt12.median_duration_s == pytest.approx((140 + 143) / 2, abs=1)
