"""Level 0 end to end on the virtual clock: replayed runs emit their status
change when they end, the production rule patterns route failures and watchdog
detections to OpenIncident, and ClassifyIncident (first state of
GuardIA-Incident-Flow) classifies each new incident with the real catalog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from load_error_catalog import load_catalog, read_catalog
from moto import mock_aws

from config import DEFAULT_THRESHOLDS, SSM_PREFIX
from guardia_local.clock import LocalScheduler
from guardia_local.environment import LocalEnvironment
from guardia_local.flow import DetectionPipeline
from guardia_local.timeline import scheduled_replays, status_changes
from lambdas.classify_incident.handler import Deps as ClassifyDeps
from lambdas.classify_incident.handler import clear_catalog_cache
from lambdas.discover_processes.handler import Deps as DiscoverDeps
from lambdas.discover_processes.handler import discover_processes
from lambdas.missed_run_watchdog.handler import Deps as WatchdogDeps
from lambdas.missed_run_watchdog.handler import run_watchdog
from lambdas.open_incident.handler import Deps as OpenDeps

SM = "arn:aws:states:us-east-1:123456789012:stateMachine:BI-{}-GCRAW-Orchestrator-ConversationsDetail"  # noqa: E501
EMT12, EMT22, EMTSAS = (SM.format(org) for org in ("EMT12", "EMT22", "EMTSAS"))
SUCCESS = {
    EMT12: "emt12-20261113-1630-succeeded",
    EMT22: "emt22-20261113-1600-succeeded",
    EMTSAS: "emtsas-20261113-1600-succeeded",
}
START, END = datetime(2026, 11, 16, 4, 0, tzinfo=UTC), datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, tzinfo=UTC)


@pytest.fixture
def night(real_executions):
    """Runs the night with the real failures replayed at 00:00 and 02:30 Bogota."""

    def run(skip=(), redeliver=False):
        clear_catalog_cache()
        env = LocalEnvironment.create(
            real_executions, start=START, thresholds=DEFAULT_THRESHOLDS, ssm_prefix=SSM_PREFIX
        )
        failures = {
            (EMTSAS, utc(5)): "emtsas-20260918-0500-failed",
            (EMT12, utc(7, 30)): "emt12-20260923-2230-failed",
        }
        runs = scheduled_replays(env.fixtures, SUCCESS, START, END, skip=skip, replace=failures)
        env.replace_fixtures(env.fixtures.only(runs))
        t = env.tables
        load_catalog(t["ErrorCatalog"], read_catalog())
        results = []
        DetectionPipeline(
            OpenDeps(t["Incidents"], t["ProcessRegistry"], t["IncidentEvents"], env.sfn,
                     env.ssm, env.flow_arn),
            env.clock,
            ClassifyDeps(t["Incidents"], t["ErrorCatalog"], t["IncidentEvents"]),
            on_result=lambda event, result, now: results.append(result),
        ).attach(env.bus)  # fmt: skip
        env.bus.raise_errors = True

        scheduler = LocalScheduler(env.clock)
        discover = DiscoverDeps(t["ProcessRegistry"], env.tagging, env.events, env.scheduler,
                                env.sfn, env.ssm)  # fmt: skip
        scheduler.every(timedelta(minutes=15), "discover",
                        lambda now: discover_processes(discover, now=now))  # fmt: skip
        watchdog = WatchdogDeps(t["ProcessRegistry"], t["Incidents"], env.sfn, env.bus, env.ssm)
        scheduler.every(timedelta(minutes=5), "watchdog",
                        lambda now: run_watchdog(watchdog, now=now),
                        first=START + timedelta(minutes=5))  # fmt: skip
        changes = status_changes(runs)
        for stop, event in changes:
            scheduler.at(stop, "status", lambda now, event=event: env.bus.emit(event))
        if redeliver:
            failure = next(e for _, e in changes if e["detail"]["status"] == "FAILED")
            scheduler.at(utc(5, 1), "redelivery", lambda now: env.bus.emit(failure))
        scheduler.run_until(END)
        return env, results

    with mock_aws():
        yield run


def by_org(env) -> dict[str, dict]:
    return {row["organization"]: row for row in env.tables["Incidents"].scan()["Items"]}


@pytest.mark.req("2.1")
@pytest.mark.req("2.2")
def test_real_failures_open_and_classify_incidents_and_successes_open_nothing(night):
    env, results = night()
    rows = by_org(env)

    assert set(rows) == {"EMTSAS", "EMT12"} and len(results) == 2
    emtsas, emt12 = rows["EMTSAS"], rows["EMT12"]
    assert (emtsas["kind"], emtsas["classification"]) == ("FAILED", "auto_recoverable")
    assert emtsas["failed_state"] == "BI-EMTSAS-GCRAW-IngestionControllerTask"
    assert (emt12["root_error"], emt12["classification"]) == (
        "Runtime.OutOfMemory", "requires_escalation",
    )  # fmt: skip
    assert emt12["opened_at"].startswith("2026-11-16T02:36")  # when the run ended


@pytest.mark.req("2.4")
def test_a_redelivered_failure_does_not_open_a_second_incident(night):
    env, results = night(redeliver=True)
    assert [r["created"] for r in results] == [True, False, True]
    assert len(by_org(env)) == 2


@pytest.mark.req("4.4")
def test_watchdog_detections_go_through_the_same_pipeline(night):
    env, _ = night(skip={(EMT22, utc(8))})
    emt22 = by_org(env)["EMT22"]
    assert (emt22["kind"], emt22["classification"]) == ("MISSED_RUN", "requires_escalation")
