"""Drive GuardIA-Sim, the simulated Genesys processes of the test account (T3).

    export AWS_PROFILE=analytics AWS_DEFAULT_REGION=us-east-1
    python scripts/sim.py status
    python scripts/sim.py fail EMTSAS http500 --times 1   # next run fails once with the real 500
    python scripts/sim.py fail EMT12 oom --times 3         # real OOM, then States.Runtime
    python scripts/sim.py fail EMT12 stuck --wait-minutes 40
    python scripts/sim.py fail EMT22 unknown               # outside the ErrorCatalog (AI agent, T8)
    python scripts/sim.py run EMTSAS                       # start a run now, as the cron would
    python scripts/sim.py missed EMT22 --minutes 70        # the next scheduled run does not start
    python scripts/sim.py deadline EMT12 --in 40           # deadline_local = now + 40 min (Bogota)
    python scripts/sim.py deadline EMT12 --clear
    python scripts/sim.py teams-down --minutes 15          # Teams alerts webhook unreachable
    python scripts/sim.py clear [ORG]                      # undo scenarios now

Nothing here is GuardIA: it only changes the simulator, its scenarios table,
the role of one simulated cron rule, a ProcessRegistry operator field, and
the stage of the Teams alerts secret (never reading or printing its value).
Every timed change has a restore time that the GuardIA-Sim janitor honors.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "infra"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import TEAMS_ALERTS_SECRET_NAME  # noqa: E402
from sim.processes import SCHEDULED_INPUT, sim_processes  # noqa: E402
from sim_lambdas.janitor.handler import (  # noqa: E402
    MISSED_POLICY_NAME,
    TEAMS_DOWN_KEY,
    missed_key,
    restore,
)
from sim_lambdas.stub_task.handler import SCENARIO_STEPS  # noqa: E402

BOGOTA = ZoneInfo("America/Bogota")
INVALID_WEBHOOK = "https://invalid.example/guardia-sim-teams-down"
SCENARIO_HOURS = 6  # a failure scenario left behind expires on its own (TTL)
ORGS = [p.org for p in sim_processes()]


@dataclass
class Sim:
    """What sim.py needs, resolved from the stack outputs."""

    scenarios: Any  # DynamoDB Table
    registry: Any  # DynamoDB Table (GuardIA ProcessRegistry)
    sfn: Any
    iam: Any
    secrets: Any
    state_machines: dict[str, str]
    rule_roles: dict[str, str]
    secret_id: str = TEAMS_ALERTS_SECRET_NAME

    @classmethod
    def from_aws(cls, session: Any) -> Sim:
        sim, data = _outputs(session, "GuardIA-Sim"), _outputs(session, "GuardIA-Data")
        dynamodb = session.resource("dynamodb")
        return cls(
            scenarios=dynamodb.Table(sim["ScenariosTableName"]),
            registry=dynamodb.Table(data["ProcessRegistryTableName"]),
            sfn=session.client("stepfunctions"),
            iam=session.client("iam"),
            secrets=session.client("secretsmanager"),
            state_machines={o: sim[f"{o}StateMachineArn"] for o in ORGS},
            rule_roles={o: sim[f"{o}RuleRoleName"] for o in ORGS},
        )


def _outputs(session: Any, stack: str) -> dict[str, str]:
    described = session.client("cloudformation").describe_stacks(StackName=stack)
    return {o["OutputKey"]: o["OutputValue"] for o in described["Stacks"][0].get("Outputs", [])}


def _org(value: str) -> str:
    org = value.upper()
    if org not in ORGS:
        raise SystemExit(f"unknown organization {value!r}; expected one of {ORGS}")
    return org


# ------------------------------------------------------------------- commands
def fail(sim: Sim, org: str, scenario: str, times: int, wait_minutes: int, now: float) -> str:
    if scenario not in SCENARIO_STEPS:
        raise SystemExit(f"unknown scenario {scenario!r}; expected one of {sorted(SCENARIO_STEPS)}")
    sim.scenarios.put_item(
        Item={
            "org": org, "scenario": scenario, "times_left": times,
            "wait_seconds": wait_minutes * 60,
            "expires_at": int(now + SCENARIO_HOURS * 3600),
        }
    )  # fmt: skip
    return f"{org}: the next {times} run(s) fail with {scenario} in {SCENARIO_STEPS[scenario]}"


def run(sim: Sim, org: str, now: float) -> str:
    name = f"sim-{datetime.fromtimestamp(now, BOGOTA):%Y%m%d-%H%M%S}"
    started = sim.sfn.start_execution(
        stateMachineArn=sim.state_machines[org], name=name, input=json.dumps(SCHEDULED_INPUT)
    )
    return f"{org}: started {started['executionArn'].rsplit(':', 1)[-1]}"


def missed(sim: Sim, org: str, minutes: int, now: float) -> str:
    role = sim.rule_roles[org]
    deny = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Deny", "Action": "states:StartExecution", "Resource": "*"}],
    }
    sim.iam.put_role_policy(
        RoleName=role, PolicyName=MISSED_POLICY_NAME, PolicyDocument=json.dumps(deny)
    )
    sim.scenarios.put_item(
        Item={"org": missed_key(org), "role_name": role, "restore_at": int(now + minutes * 60)}
    )
    return f"{org}: scheduled runs will not start for {minutes} min (the rule stays enabled)"


def deadline(sim: Sim, org: str, in_minutes: int | None, now: float) -> str:
    key = {"resource_arn": sim.state_machines[org]}
    if in_minutes is None:
        sim.registry.update_item(Key=key, UpdateExpression="REMOVE deadline_local")
        return f"{org}: deadline back to the default"
    local = datetime.fromtimestamp(now, BOGOTA) + timedelta(minutes=in_minutes)
    sim.registry.update_item(
        Key=key,
        UpdateExpression="SET deadline_local = :d",
        ConditionExpression="attribute_exists(resource_arn)",
        ExpressionAttributeValues={":d": f"{local:%H:%M}"},
    )
    return f"{org}: deadline {local:%H:%M} Bogota (new incidents only)"


def teams_down(sim: Sim, minutes: int, now: float) -> str:
    existing = sim.scenarios.get_item(Key={"org": TEAMS_DOWN_KEY}).get("Item")
    if existing:
        previous = existing["previous_version"]
    else:
        stages = sim.secrets.describe_secret(SecretId=sim.secret_id)["VersionIdsToStages"]
        previous = next(v for v, labels in stages.items() if "AWSCURRENT" in labels)
        sim.secrets.put_secret_value(SecretId=sim.secret_id, SecretString=INVALID_WEBHOOK)
    sim.scenarios.put_item(
        Item={
            "org": TEAMS_DOWN_KEY, "secret_id": sim.secret_id, "previous_version": previous,
            "restore_at": int(now + minutes * 60),
        }
    )  # fmt: skip
    return f"Teams alerts webhook points to an invalid address for {minutes} min"


def clear(sim: Sim, org: str | None) -> list[str]:
    done = []
    for item in sim.scenarios.scan().get("Items", []):
        key = item["org"]
        if org and not key.startswith(org):
            continue
        if "restore_at" in item:
            done.append(restore(item, sim.iam, sim.secrets))
        else:
            done.append(f"{key}: scenario {item.get('scenario')} removed")
        sim.scenarios.delete_item(Key={"org": key})
    return done or ["nothing to clear"]


def status(sim: Sim, now: float) -> list[str]:
    lines = []
    for item in sorted(sim.scenarios.scan().get("Items", []), key=lambda i: i["org"]):
        if "restore_at" in item:
            left = max(0, int((float(item["restore_at"]) - now) // 60))
            lines.append(f"  {item['org']:<22} restores in {left} min")
        else:
            lines.append(
                f"  {item['org']:<22} {item['scenario']}, {int(item['times_left'])} run(s) left"
            )
    lines.insert(0, "Scenarios:" if lines else "Scenarios: none")
    lines.append("Last runs:")
    for org, arn in sim.state_machines.items():
        runs = sim.sfn.list_executions(stateMachineArn=arn, maxResults=3)["executions"]
        summary = ", ".join(f"{r['status']} {r['startDate']:%m-%d %H:%M}" for r in runs) or "-"
        lines.append(f"  {org:<10} {summary}")
    return lines


# ----------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("fail")
    p.add_argument("org", type=_org)
    p.add_argument("scenario", choices=sorted(SCENARIO_STEPS))
    p.add_argument("--times", type=int, default=1)
    p.add_argument("--wait-minutes", type=int, default=40, help="stuck only")
    p = sub.add_parser("run")
    p.add_argument("org", type=_org)
    p = sub.add_parser("missed")
    p.add_argument("org", type=_org)
    p.add_argument("--minutes", type=int, default=70)
    p = sub.add_parser("deadline")
    p.add_argument("org", type=_org)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--in", dest="in_minutes", type=int)
    group.add_argument("--clear", action="store_true")
    p = sub.add_parser("teams-down")
    p.add_argument("--minutes", type=int, default=15)
    p = sub.add_parser("clear")
    p.add_argument("org", nargs="?", type=_org)
    args = parser.parse_args(argv)

    import boto3

    sim, now = Sim.from_aws(boto3.Session()), time.time()
    if args.command == "status":
        out = status(sim, now)
    elif args.command == "fail":
        out = [fail(sim, args.org, args.scenario, args.times, args.wait_minutes, now)]
    elif args.command == "run":
        out = [run(sim, args.org, now)]
    elif args.command == "missed":
        out = [missed(sim, args.org, args.minutes, now)]
    elif args.command == "deadline":
        out = [deadline(sim, args.org, None if args.clear else args.in_minutes, now)]
    elif args.command == "teams-down":
        out = [teams_down(sim, args.minutes, now)]
    else:
        out = clear(sim, args.org)
    print("\n".join(out))


if __name__ == "__main__":
    main()
