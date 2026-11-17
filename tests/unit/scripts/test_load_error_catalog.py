from __future__ import annotations

import json

import pytest
from load_error_catalog import load_catalog, read_catalog


@pytest.mark.req("4.1")
def test_catalog_file_matches_schema_and_design_content():
    entries = read_catalog()
    by_action = {(e.classification, e.action, e.max_wait_s) for e in entries}

    assert len(entries) == 4
    assert ("auto_recoverable", "wait_and_redrive", 600) in by_action  # 429
    assert ("auto_recoverable", "redrive", 0) in by_action  # TIMED_OUT
    assert sum(e.action == "escalate" for e in entries) == 2  # 403/auth, 404


@pytest.mark.req("4.1")
def test_load_is_idempotent(aws):
    table = aws["tables"]["ErrorCatalog"]
    load_catalog(table, read_catalog())
    load_catalog(table, read_catalog())
    assert table.scan()["Count"] == 4


def test_duplicate_keys_are_rejected(tmp_path):
    row = {
        "process_type": "*",
        "error_pattern": "x",
        "classification": "auto_recoverable",
        "action": "redrive",
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([row, row]))
    with pytest.raises(ValueError, match="Duplicate"):
        read_catalog(path)
