"""Configuration of the GuardIA stacks.

`DEFAULT_THRESHOLDS` are the values published to SSM Parameter Store (decision
D8 and the component table in design.md). Handlers read them from SSM at
runtime and never hardcode them.

`ENVIRONMENTS` holds what changes between the test account and production
(decision D17). The environment is chosen with `cdk deploy -c env=dev|workshop|prod`;
the code and the stacks are the same in all of them.
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
    "default-retry-after-seconds": "60",  # D26: a 429 that does not say how long to wait
    "redrive-poll-interval-seconds": "180",  # between two checks of a redrive in progress
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
    # AI agent (T8, requirements 4.5 and 4.6)
    "agent-timeout-seconds": "30",
    "agent-max-turns": "6",
    "agent-max-tool-output-chars": "4000",
    "agent-min-confidence": "0.7",
}

# Secrets Manager names. Created empty by GuardIA-Data; the webhook address is
# set by hand with `aws secretsmanager put-secret-value` so it never goes
# through the code (D20: both hold the same address for now).
TEAMS_ALERTS_SECRET_NAME = "guardia/teams-alerts-webhook"  # noqa: S105 - a name
TEAMS_SUMMARY_SECRET_NAME = "guardia/teams-summary-webhook"  # noqa: S105 - a name
# Azure OpenAI settings of the agent (T8): endpoint, API version, deployment, and
# key, loaded by `scripts/load_agent_secret.py` from the local `.env`.
AGENT_AZURE_SECRET_NAME = "guardia/agent-azure-openai"  # noqa: S105 - a name

AZURE_OPENAI = "azure_openai"
BEDROCK = "bedrock"
AGENT_PROVIDERS = (AZURE_OPENAI, BEDROCK)


# Environments with simulated processes run fast, so a scenario takes seconds in a live
# demo (D33): short polls and waits, and half-minute reminders. Production keeps the defaults.
DEMO_THRESHOLDS: dict[str, str] = {
    "remediation-enabled": "true",
    "reminder-interval-minutes": "0.5",
    "redrive-poll-interval-seconds": "5",
    "default-retry-after-seconds": "5",
    "missed-run-tolerance-minutes": "1",
}


class UnknownEnvironmentError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    """Which model the agent uses in an environment (T8). No endpoint, no key: they
    live in a secret (Azure) or in the Lambda's role (Bedrock)."""

    provider: str
    model: str | None = None

    def lambda_environment(self) -> dict[str, str]:
        if self.provider == AZURE_OPENAI:
            return {"AGENT_PROVIDER": self.provider, "AGENT_SECRET_ID": AGENT_AZURE_SECRET_NAME}
        return {"AGENT_PROVIDER": self.provider, "AGENT_MODEL": self.model or ""}


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    deploy_sim: bool
    # True keeps the data key, tables, secrets, and layer when the stack is
    # deleted (RETAIN). False deletes them with the stack (DESTROY), so a test
    # environment can be torn down and created again without manual cleanup.
    retain_data: bool = True
    threshold_overrides: Mapping[str, str] = field(default_factory=dict)
    # AI agent (T8). None: there is no agent and an `unknown` incident escalates.
    agent_provider: str | None = None
    agent_model: str | None = None  # Bedrock model id; Azure's deployment lives in the secret

    def __post_init__(self) -> None:
        unknown = set(self.threshold_overrides) - set(DEFAULT_THRESHOLDS)
        if unknown:
            raise ValueError(f"unknown thresholds for '{self.name}': {sorted(unknown)}")
        if self.agent_provider is not None and self.agent_provider not in AGENT_PROVIDERS:
            raise ValueError(
                f"agent_provider of '{self.name}' must be one of {list(AGENT_PROVIDERS)} or None"
            )
        if self.agent_provider == BEDROCK and not self.agent_model:
            raise ValueError(f"'{self.name}' uses Bedrock: set agent_model")

    @property
    def agent(self) -> AgentConfig | None:
        if self.agent_provider is None:
            return None
        return AgentConfig(provider=self.agent_provider, model=self.agent_model)

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
        threshold_overrides=DEMO_THRESHOLDS,
        agent_provider=AZURE_OPENAI,
    ),
    # Workshop account: simulated processes again, and the agent on Amazon
    # Bedrock (no keys: the Lambda's role). Confirm the model is enabled in that
    # account and supports tool use in the Converse API before deploying.
    "workshop": EnvironmentConfig(
        name="workshop",
        deploy_sim=True,
        retain_data=False,
        threshold_overrides=DEMO_THRESHOLDS,
        agent_provider=BEDROCK,
        agent_model="amazon.nova-pro-v1:0",
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
