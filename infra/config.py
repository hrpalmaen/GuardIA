"""Configuration of the GuardIA stacks.

`DEFAULT_THRESHOLDS` are the values published to SSM Parameter Store (decision
D8 and the component table in design.md). Handlers read them from SSM at
runtime and never hardcode them.

`ENVIRONMENTS` holds what changes between the test account and production
(decision D17). The environment is chosen with `cdk deploy -c env=dev|prod`;
the code and the stacks are the same in both.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

SSM_PREFIX = "/guardia/config"

DEFAULT_THRESHOLDS: dict[str, str] = {
    # SLA
    "deadline-local": "06:00",
    "timezone": "America/Bogota",
    # Level 1 · remediation budget
    # Observe-only until the owners approve automatic redrive (rollout, Fase E).
    "remediation-enabled": "false",
    "remediation-max-attempts": "2",
    "remediation-window-minutes": "20",
    "max-wait-seconds": "600",
    # Level 2 · escalation
    "reminder-interval-minutes": "10",
    "parallel-escalation-minutes-left": "30",
    "post-deadline-wait-hours": "6",
    # Grouping and watchdog
    "grouping-window-minutes": "5",
    "grouping-min-incidents": "3",
    "watchdog-interval-minutes": "5",
    "discovery-interval-minutes": "15",
    # Level 0 · missed and stuck executions (requirement 3)
    "missed-run-tolerance-minutes": "10",
    "watchdog-lookback-minutes": "60",
    "stuck-median-multiplier": "2",
    "stuck-period-fraction": "0.5",
    "median-window-executions": "14",
    "median-min-executions": "5",
    # Heartbeat
    "heartbeat-missing-minutes": "10",
}

# Secrets Manager names. Created empty by GuardIA-Data; the webhook address is
# set by hand with `aws secretsmanager put-secret-value` so it never goes
# through the code (D20: both hold the same address for now).
TEAMS_ALERTS_SECRET_NAME = "guardia/teams-alerts-webhook"  # noqa: S105 - a name
TEAMS_SUMMARY_SECRET_NAME = "guardia/teams-summary-webhook"  # noqa: S105 - a name


class UnknownEnvironmentError(ValueError):
    pass


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    deploy_sim: bool
    # True keeps the data key, tables, secrets, and layer when the stack is
    # deleted (RETAIN). False deletes them with the stack (DESTROY), so a test
    # environment can be torn down and created again without manual cleanup.
    retain_data: bool = True
    threshold_overrides: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.threshold_overrides) - set(DEFAULT_THRESHOLDS)
        if unknown:
            raise ValueError(f"unknown thresholds for '{self.name}': {sorted(unknown)}")

    @property
    def thresholds(self) -> dict[str, str]:
        return {**DEFAULT_THRESHOLDS, **self.threshold_overrides}


ENVIRONMENTS: dict[str, EnvironmentConfig] = {
    # Test account: simulated processes (GuardIA-Sim), redrive enabled against
    # the simulator, shorter reminders so a scenario lasts minutes, and
    # simulated data that `cdk destroy` removes.
    "dev": EnvironmentConfig(
        name="dev",
        deploy_sim=True,
        retain_data=False,
        threshold_overrides={
            "remediation-enabled": "true",
            "reminder-interval-minutes": "2",
        },
    ),
    # Production: never the simulator, observe-only at first, and the audit
    # data survives a deleted stack.
    "prod": EnvironmentConfig(name="prod", deploy_sim=False, retain_data=True),
}


def environment(name: str | None) -> EnvironmentConfig:
    """The configuration for `-c env=<name>`. There is no default, so nobody
    deploys to an account with the wrong settings by omission."""
    if not name:
        raise UnknownEnvironmentError("choose the environment with -c env=dev or -c env=prod")
    try:
        return ENVIRONMENTS[name]
    except KeyError as exc:
        raise UnknownEnvironmentError(
            f"unknown environment '{name}'; expected one of {sorted(ENVIRONMENTS)}"
        ) from exc
