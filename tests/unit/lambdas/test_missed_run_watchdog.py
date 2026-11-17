"""MissedRunWatchdog (task 3.4) on a scripted night: the real successful
executions are replayed every hour on the virtual clock, and detections reach
the real OpenIncident through the local bus, as they will through EventBridge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from moto import mock_aws

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from guardia_common.incidents import WATCHDOG_SOURCE
from guardia_local.environment import LocalEnvironment
from guardia_local.timeline import scheduled_replays
from lambdas.discover_processes.handler import Deps as DiscoverDeps
from lambdas.discover_processes.handler import discover_processes
from lambdas.missed_run_watchdog.handler import Deps, run_watchdog
from lambdas.open_incident.handler import Deps as OpenDeps
from lambdas.open_incident.handler import open_incident

SM = (
    "arn:aws:states:us-east-1:123456789012:stateMachine:"
    "BI-{}-GCRAW-Orchestrator-ConversationsDetail"
)
EMT12, EMT22, EMTSAS = (SM.format(org) for org in ("EMT12", "EMT22", "EMTSAS"))
SUCCESS = {
    EMT12: "emt12-20261113-1630-succeeded",
    EMT22: "emt22-20261113-1600-succeeded",
    EMTSAS: "emtsas-20261113-1600-succeeded",
}
DISCOVERED_AT = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)  # 23:00 Bogota
NIGHT_END = datetime(2026, 11, 16, 11, 0, tzinfo=UTC)


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, tzinfo=UTC)


@pytest.fixture
def env(real_executions):
    with mock_aws():
        env = LocalEnvironment.create(
            real_executions, start=DISCOVERED_AT, thresholds=DEFAULT_THRESHOLDS,
            ssm_prefix=SSM_PREFIX,
        )  # fmt: skip
        discover_processes(
            DiscoverDeps(env.tables["ProcessRegistry"], env.tagging, env.events, env.scheduler,
                         env.sfn, env.ssm),
            now=DISCOVERED_AT,
        )  # fmt: skip
        tables = env.tables
        open_deps = OpenDeps(tables["Incidents"], tables["ProcessRegistry"],
                             tables["IncidentEvents"], env.sfn, env.ssm, env.flow_arn)  # fmt: skip
        env.bus.raise_errors = True
        env.bus.put_rule(
            "watchdog-to-open-incident",
            {"source": [WATCHDOG_SOURCE]},
            lambda event: open_incident(event, open_deps, now=env.clock()),
        )
        yield env


def night(env, **scenario):
    """Every process runs at each scheduled time of the night (guardia_local.timeline)."""
    runs = scheduled_replays(env.fixtures, SUCCESS, DISCOVERED_AT, NIGHT_END, **scenario)
    env.replace_fixtures(env.fixtures.only(runs))


def watchdog(env, at: datetime) -> dict:
    env.clock.set(at)
    deps = Deps(env.tables["ProcessRegistry"], env.tables["Incidents"], env.sfn, env.bus, env.ssm)
    return run_watchdog(deps, now=at)


def incidents(env) -> list[dict]:
    return env.tables["Incidents"].scan()["Items"]


# --------------------------------------------------------------------------- 3.1
@pytest.mark.req("3.1")
def test_night_with_every_run_on_time_opens_nothing(env):
    night(env)
    for minute in range(0, 6 * 60, 5):  # every 5 minutes from 23:05 to 05:00 Bogota
        out = watchdog(env, DISCOVERED_AT + timedelta(minutes=minute + 5))
        assert out["published"] == [], out["published"]
    assert incidents(env) == []


@pytest.mark.req("3.1")
def test_missed_run_opens_an_incident_within_5_minutes_after_the_tolerance(env):
    night(env, skip={(EMT22, utc(8))})  # 03:00 Bogota
    assert watchdog(env, utc(8, 5))["published"] == []  # still inside the tolerance
    out = watchdog(env, utc(8, 10))  # first cycle after 03:10

    assert out["published"] == [
        {"kind": "MISSED_RUN", "resource_arn": EMT22, "expected_at": "2026-11-16T08:00:00+00:00"}
    ]
    [incident] = incidents(env)
    assert incident["kind"] == "MISSED_RUN" and incident["organization"] == "EMT22"
    assert incident["expected_at"] == "2026-11-16T03:00:00-05:00"
    assert incident["deadline_iso"] == "2026-11-16T06:00:00-05:00"
    assert incident["flow_execution_arn"]


@pytest.mark.req("3.1")
def test_run_delayed_beyond_the_tolerance_is_a_missed_run(env):
    night(env, delay={(EMTSAS, utc(9)): timedelta(minutes=12)})
    out = watchdog(env, utc(9, 15))
    assert [(d["kind"], d["resource_arn"]) for d in out["published"]] == [("MISSED_RUN", EMTSAS)]


@pytest.mark.req("3.1")
def test_run_delayed_inside_the_tolerance_is_on_time(env):
    night(env, delay={(EMTSAS, utc(9)): timedelta(minutes=8)})
    assert watchdog(env, utc(9, 15))["published"] == []


@pytest.mark.req("3.1")
def test_missed_run_with_an_open_incident_is_not_published_again(env):
    night(env, skip={(EMT22, utc(8))})
    first = watchdog(env, utc(8, 10))
    second = watchdog(env, utc(8, 15))

    assert len(first["published"]) == 1
    assert second["published"] == [] and second["already_open"] == 1
    assert len(incidents(env)) == 1


# --------------------------------------------------------------------------- 3.2 / 3.3
@pytest.mark.req("3.3")
def test_stuck_run_without_history_uses_half_the_period(env):
    # EMT12 at 03:30 Bogota runs 45 min instead of 2 min; hourly cron → limit 30 min.
    stuck = (EMT12, utc(8, 30))
    night(env, durations={stuck: timedelta(minutes=45)})

    assert watchdog(env, utc(9, 0))["published"] == []  # exactly 30 min: not yet
    out = watchdog(env, utc(9, 5))
    assert [(d["kind"], d["resource_arn"]) for d in out["published"]] == [("STUCK_RUN", EMT12)]
    assert out["published"][0]["execution_arn"].endswith("-20261116T0830")

    [incident] = incidents(env)
    assert incident["kind"] == "STUCK_RUN" and incident["started_at"].startswith("2026-11-16T03:30")
    assert watchdog(env, utc(9, 10))["already_open"] == 1  # one incident, not one per cycle


@pytest.mark.req("3.2")
def test_stuck_run_with_history_uses_twice_the_median(env):
    env.tables["ProcessRegistry"].update_item(
        Key={"resource_arn": EMT12},
        UpdateExpression="SET median_duration_s = :m, duration_samples = :n",
        ExpressionAttributeValues={":m": 150, ":n": 14},
    )
    night(env, durations={(EMT12, utc(8, 30)): timedelta(minutes=20)})
    assert watchdog(env, utc(8, 35))["published"] == []  # 5 min = 2 × 150 s
    assert [d["kind"] for d in watchdog(env, utc(8, 40))["published"]] == ["STUCK_RUN"]


def test_finished_run_is_never_stuck(env):
    night(env)  # every run takes about 2 minutes
    assert all(d["kind"] != "STUCK_RUN" for d in watchdog(env, utc(9, 50))["published"])


def test_process_removed_from_the_registry_is_not_watched(env):
    night(env, skip={(EMT22, utc(8))})
    env.tables["ProcessRegistry"].delete_item(Key={"resource_arn": EMT22})
    assert watchdog(env, utc(8, 10))["published"] == []
