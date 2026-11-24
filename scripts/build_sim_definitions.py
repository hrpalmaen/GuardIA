"""Writes the GuardIA-Sim state machine definitions and checks them against the histories.

    python scripts/build_sim_definitions.py          # write infra/sim/definitions/*.asl.json
    python scripts/build_sim_definitions.py --check  # fail if a file is stale or a check fails

Every state that a sanitized history in tests/fixtures/executions entered must
exist in the rebuilt definition of its organization, with the same type. The
template and its assumptions are documented in infra/sim/definition.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "infra"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from guardia_local.fixtures import ExecutionFixtures  # noqa: E402
from sim.definition import build_definition, check_against_history, definition_json  # noqa: E402
from sim.processes import DEFINITIONS, sim_processes  # noqa: E402


def problems_by_fixture() -> dict[str, list[str]]:
    fixtures = ExecutionFixtures.load()
    found = {}
    for execution in fixtures.executions.values():
        org = execution.state_machine_arn.rsplit(":", 1)[-1].split("-")[1]
        found[execution.fixture_id] = check_against_history(
            build_definition(org), execution.history_events()
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="do not write; fail if stale")
    args = parser.parse_args(argv)

    failed = False
    for fixture_id, problems in problems_by_fixture().items():
        for problem in problems:
            print(f"{fixture_id}: {problem}")
            failed = True

    DEFINITIONS.mkdir(parents=True, exist_ok=True)
    for process in sim_processes():
        text = definition_json(process.org)
        path: Path = process.definition_file
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                print(f"{path.relative_to(ROOT)} is stale: run scripts/build_sim_definitions.py")
                failed = True
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
