"""GuardIA-Sim janitor: undoes the timed scenarios when they expire (T3).

Test-only. `scripts/sim.py` changes two things outside the simulated state
machines, always with a restore time; this Lambda (every 5 minutes) or
`sim.py clear` puts them back:

* `missed`: an explicit Deny on `states:StartExecution` added to the role of
  one cron rule. The rule stays ENABLED, so DiscoverProcesses still sees the
  schedule and GuardIA's watchdog reports the run that did not start.
* `teams-down`: the Teams alerts secret moved to a version with an invalid
  address; restoring moves AWSCURRENT back to the saved version. The
  webhook address is never read or printed.

Rows: `org = "<ORG>#missed"` or `"#teams-down"`, with `restore_at` (epoch s).

Environment:
    SCENARIOS_TABLE
"""

from __future__ import annotations

import os
import time
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

MISSED_POLICY_NAME = "GuardIASimMissedRun"
TEAMS_DOWN_KEY = "#teams-down"


def missed_key(org: str) -> str:
    return f"{org}#missed"


def restore(item: dict, iam: Any, secrets: Any) -> str:
    """Undo one timed scenario; safe to run twice."""
    if item["org"].endswith("#missed"):
        try:
            iam.delete_role_policy(RoleName=item["role_name"], PolicyName=MISSED_POLICY_NAME)
        except ClientError as err:
            if err.response["Error"]["Code"] != "NoSuchEntity":
                raise
        return f"rule role {item['role_name']} can start executions again"
    if item["org"] == TEAMS_DOWN_KEY:
        current = _current_version(secrets, item["secret_id"])
        if current != item["previous_version"]:
            secrets.update_secret_version_stage(
                SecretId=item["secret_id"],
                VersionStage="AWSCURRENT",
                MoveToVersionId=item["previous_version"],
                RemoveFromVersionId=current,
            )
        return "Teams alerts secret restored"
    raise ValueError(f"not a timed scenario: {item['org']}")


def _current_version(secrets: Any, secret_id: str) -> str:
    stages = secrets.describe_secret(SecretId=secret_id)["VersionIdsToStages"]
    return next(version for version, labels in stages.items() if "AWSCURRENT" in labels)


def janitor(table: Any, iam: Any, secrets: Any, now: float | None = None) -> list[str]:
    now = now if now is not None else time.time()
    done = []
    for item in table.scan().get("Items", []):
        restore_at = item.get("restore_at")
        if restore_at is None or float(restore_at) > now:
            continue
        done.append(restore(item, iam, secrets))
        table.delete_item(Key={"org": item["org"]})
    return done


@cache
def _clients() -> tuple[Any, Any, Any]:
    return (
        boto3.resource("dynamodb").Table(os.environ["SCENARIOS_TABLE"]),
        boto3.client("iam"),
        boto3.client("secretsmanager"),
    )


def handler(event: dict, context: Any) -> dict:
    return {"restored": janitor(*_clients())}
