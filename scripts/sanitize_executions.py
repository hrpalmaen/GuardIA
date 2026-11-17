"""Turns raw Step Functions exports into sanitized fixtures (phase 0).

Input: a folder with the files exported per execution (outside the repo):
    <state-machine>_<execution-name>_estado_final.json       DescribeExecution
    <state-machine>_<execution-name>_historial_completo.json GetExecutionHistory
(`_errores.json` and `_states.csv` are derived from the history and are not used.)

Output: `tests/fixtures/executions/` (see `guardia_local/fixtures.py`).

What it changes, and nothing else:
* Credentials are redacted with the same rules GuardIA uses in production
  (`guardia_common.redaction`).
* The AWS account id is replaced with 123456789012.
* Campaign names (customer data) are replaced with stable pseudonyms.
* DescribeExecution drops `output` (GuardIA never reads it).
* History events drop state `input`/`output`, which is what GetExecutionHistory
  returns with `includeExecutionData=False`, and `parameters.Payload` of task
  events (the full state input). Error and cause fields are kept.

The result is deterministic: running the script again produces the same files.

    python scripts/sanitize_executions.py --source ../step
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from guardia_common.redaction import redact_text, redact_value  # noqa: E402

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "executions"
FIXTURE_ACCOUNT = "123456789012"
DESCRIBE_SUFFIX = "_estado_final.json"
HISTORY_SUFFIX = "_historial_completo.json"
# BI-<ORG>-GCRAW-Orchestrator-<Kind>
MACHINE_NAME = re.compile(r"^BI-(?P<org>[A-Z0-9]+)-GCRAW-Orchestrator-(?P<kind>[A-Za-z]+)$")
PROCESS_TYPES = {"ConversationsDetail": "conversations"}
# Assumption until the process owners confirm idempotency (requirements, assumption 2).
DEFAULT_REMEDIATION = "auto"
_CAMPAIGN = re.compile(r'(\\?"campaignName\\?"\s*:\s*\\?")((?:[^"\\]|\\(?!"))+)(\\?")')
_STATE_DATA_KEYS = ("input", "output", "inputDetails", "outputDetails")


class Pseudonyms:
    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def campaigns(self, text: str) -> str:
        def repl(match: re.Match) -> str:
            name = self._names.setdefault(match.group(2), f"CAMPAIGN-{len(self._names) + 1:03d}")
            return f"{match.group(1)}{name}{match.group(3)}"

        return _CAMPAIGN.sub(repl, text)


def _clean_text(text: str, account: str, names: Pseudonyms) -> str:
    return names.campaigns(redact_text(text)).replace(account, FIXTURE_ACCOUNT)


def _clean(value, account: str, names: Pseudonyms):
    value = redact_value(value)
    if isinstance(value, dict):
        return {k: _clean(v, account, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, account, names) for v in value]
    if isinstance(value, str):
        return _clean_text(value, account, names)
    return value


def _strip_state_data(event: dict) -> dict:
    event = dict(event)
    for key, details in list(event.items()):
        if not key.endswith("EventDetails") or not isinstance(details, dict):
            continue
        details = {k: v for k, v in details.items() if k not in _STATE_DATA_KEYS}
        if "parameters" in details:
            try:
                params = json.loads(details["parameters"])
            except ValueError:
                params = None
            if isinstance(params, dict):
                params.pop("Payload", None)
                details["parameters"] = json.dumps(
                    params, ensure_ascii=False, separators=(",", ":")
                )
        event[key] = details
    return event


def _fixture_id(org: str, start: str, status: str) -> str:
    moment = datetime.fromisoformat(start)
    return f"{org.lower()}-{moment:%Y%m%d-%H%M}-{status.lower()}"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def sanitize(source: Path, out: Path) -> dict:
    names = Pseudonyms()
    machines: dict[str, dict] = {}
    executions: list[dict] = []

    for describe_file in sorted(source.glob(f"*{DESCRIBE_SUFFIX}")):
        raw = describe_file.read_text(encoding="utf-8").strip()
        history_file = describe_file.with_name(
            describe_file.name.replace(DESCRIBE_SUFFIX, HISTORY_SUFFIX)
        )
        if not raw or not history_file.exists() or not history_file.stat().st_size:
            print(f"skipped (empty): {describe_file.name}")
            continue

        describe = json.loads(raw)
        account = describe["executionArn"].split(":")[4]
        machine_name = describe["stateMachineArn"].rsplit(":", 1)[-1]
        match = MACHINE_NAME.match(machine_name)
        if not match:
            print(f"skipped (unknown state machine name): {machine_name}")
            continue

        describe.pop("output", None)
        describe.pop("outputDetails", None)
        describe = _clean(describe, account, names)
        history = json.loads(history_file.read_text(encoding="utf-8"))
        events = [_clean(_strip_state_data(e), account, names) for e in history["events"]]

        fixture_id = _fixture_id(match["org"], describe["startDate"], describe["status"])
        _write(out / fixture_id / "describe.json", describe)
        _write(out / fixture_id / "history.json", {"events": events})

        machines[describe["stateMachineArn"]] = {
            "arn": describe["stateMachineArn"],
            "name": machine_name,
            "tags": {
                "guardia:monitor": "true",
                "guardia:process-type": PROCESS_TYPES.get(match["kind"], match["kind"].lower()),
                "guardia:organization": match["org"],
                "guardia:remediation": DEFAULT_REMEDIATION,
            },
        }
        executions.append({
            "id": fixture_id,
            "execution_arn": describe["executionArn"],
            "state_machine_arn": describe["stateMachineArn"],
            "status": describe["status"],
            "start_date": describe["startDate"],
            "stop_date": describe.get("stopDate"),
            "events": len(events),
        })  # fmt: skip
        print(f"{fixture_id}: {describe['status']}, {len(events)} events")

    manifest = {
        "description": "Real executions of the Genesys extraction processes, sanitized by "
        "scripts/sanitize_executions.py. Tags are those GuardIA expects; "
        "guardia:remediation=auto is an assumption until the owners confirm idempotency.",
        "state_machines": sorted(machines.values(), key=lambda m: m["name"]),
        "executions": sorted(executions, key=lambda e: e["id"]),
    }
    _write(out / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="folder with the raw exports")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    manifest = sanitize(args.source, args.out)
    print(f"{len(manifest['executions'])} executions written to {args.out}")


if __name__ == "__main__":
    main()
