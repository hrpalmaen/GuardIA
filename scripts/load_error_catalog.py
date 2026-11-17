"""Idempotent loader for `data/error_catalog.json` into the ErrorCatalog table (task 1.4).

Every row is validated with `CatalogEntry` before anything is written; an
invalid file writes nothing. Re-running the script overwrites rows with the
same (process_type, error_pattern) key and leaves the table in the same state.

    python scripts/load_error_catalog.py --table GuardIA-ErrorCatalog
    python scripts/load_error_catalog.py --table GuardIA-ErrorCatalog --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from guardia_common.models import CatalogEntry  # noqa: E402

DEFAULT_FILE = ROOT / "data" / "error_catalog.json"


def read_catalog(path: Path = DEFAULT_FILE) -> list[CatalogEntry]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    entries = [CatalogEntry.model_validate(row) for row in rows]
    keys = [(e.process_type, e.error_pattern) for e in entries]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ValueError(f"Duplicate catalog keys: {sorted(duplicates)}")
    return entries


def load_catalog(table, entries: list[CatalogEntry]) -> int:
    with table.batch_writer(overwrite_by_pkeys=["process_type", "error_pattern"]) as batch:
        for entry in entries:
            batch.put_item(Item=entry.to_item())
    return len(entries)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", required=True, help="ErrorCatalog table name")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--dry-run", action="store_true", help="only validate the file")
    args = parser.parse_args(argv)

    entries = read_catalog(args.file)
    if args.dry_run:
        print(f"{len(entries)} valid catalog entries in {args.file}")
        return

    import boto3

    count = load_catalog(boto3.resource("dynamodb").Table(args.table), entries)
    print(f"Loaded {count} entries into {args.table}")


if __name__ == "__main__":
    main()
