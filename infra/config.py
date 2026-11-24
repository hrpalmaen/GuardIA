"""Configuration of the GuardIA stacks.

`DEFAULT_THRESHOLDS` are the values published to SSM Parameter Store (decision
D8 and the component table in design.md). Handlers read them from SSM at
runtime and never hardcode them.

`ENVIRONMENTS` holds what changes between the test account and production
(decision D17). The environment is chosen with `cdk deploy -c env=dev|prod`;
the code and the stacks are the same in both.
"""

from __future__ import annotations

import os
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


class MissingOnCallError(ValueError):
    pass


@dataclass(frozen=True)
class OnCallConfig:
    """The on-call engineer (decision D19), set at deploy time and never committed.

    `email` is the Teams mention id (UPN). The values end up in the environment
    of `ResolveOnCall`, so they are only as private as the AWS account.
    """

    email: str
    name: str | None = None
    backup_email: str | None = None

    def lambda_environment(self) -> dict[str, str]:
        values = {
            "GUARDIA_ONCALL_EMAIL": self.email,
            "GUARDIA_ONCALL_NAME": self.name,
            "GUARDIA_ONCALL_BACKUP_EMAIL": self.backup_email,
        }
        return {key: value for key, value in values.items() if value}


def oncall_from_environment(environ: Mapping[str, str] | None = None) -> OnCallConfig:
    """Reads the D19 variables (`.env`). Without the email `cdk synth` fails: an
    escalation with nobody to mention would only fail later, during an incident."""
    source = os.environ if environ is None else environ
    email = (source.get("GUARDIA_ONCALL_EMAIL") or "").strip()
    if not email or "@" not in email:
        raise MissingOnCallError(
            "GUARDIA_ONCALL_EMAIL is missing or is not an email. Copy .env.example to .env, "
            "fill in the on-call engineer (the Teams mention id), and load it before cdk: "
            "set -a; source .env; set +a"
        )
    return OnCallConfig(
        email=email,
        name=(source.get("GUARDIA_ONCALL_NAME") or "").strip() or None,
        backup_email=(source.get("GUARDIA_ONCALL_BACKUP_EMAIL") or "").strip() or None,
    )


class MissingAlarmEmailError(ValueError):
    pass


def alarm_email_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The team email subscribed to the `guardia-alarms` SNS topic (T6, D21, D28).

    Read from GUARDIA_ALARM_EMAIL (`.env`, never committed). Without it `cdk
    synth` fails: an alarm nobody receives is the same as no alarm. SNS sends a
    confirmation email that someone must accept once after the first deploy.
    """
    source = os.environ if environ is None else environ
    email = (source.get("GUARDIA_ALARM_EMAIL") or "").strip()
    if not email or "@" not in email or any(c.isspace() for c in email):
        raise MissingAlarmEmailError(
            "GUARDIA_ALARM_EMAIL is missing or is not an email. Add the team's email to .env "
            "(see .env.example) and load it before cdk: set -a; source .env; set +a"
        )
    return email
