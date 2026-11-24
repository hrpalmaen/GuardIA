"""StoreTaskToken (T5 · tasks 6.4 and 6.5 · requirement 8.3)."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from lambdas.store_task_token.handler import Deps, store_task_token
from tests.unit.fakes import FakeTable, new_incident_item


def make_deps() -> Deps:
    incidents = FakeTable("incident_id")
    incidents.put_row(new_incident_item("inc-1"))
    return Deps(incidents=incidents)


@pytest.mark.req("8.3")
def test_saves_the_token_in_the_incident():
    deps = make_deps()

    result = store_task_token({"incident_id": "inc-1", "task_token": "token-1"}, deps)

    assert result == {"incident_id": "inc-1", "stored": True}
    assert deps.incidents.only()["task_token"] == "token-1"  # noqa: S105


@pytest.mark.req("8.3")
def test_each_loop_replaces_the_previous_token():
    deps = make_deps()

    store_task_token({"incident_id": "inc-1", "task_token": "old"}, deps)
    store_task_token({"incident_id": "inc-1", "task_token": "new"}, deps)

    assert deps.incidents.only()["task_token"] == "new"  # noqa: S105


def test_the_token_is_never_logged(capsys):
    store_task_token({"incident_id": "inc-1", "task_token": "SECRET-TOKEN-VALUE"}, make_deps())

    captured = capsys.readouterr()
    assert "SECRET-TOKEN-VALUE" not in captured.out + captured.err


def test_does_not_create_an_incident_that_does_not_exist():
    deps = make_deps()

    with pytest.raises(ClientError):
        store_task_token({"incident_id": "ghost", "task_token": "t"}, deps)


@pytest.mark.parametrize("event", [{}, {"incident_id": "inc-1"}, {"task_token": "t"}])
def test_requires_both_fields(event):
    with pytest.raises(ValueError, match="requires"):
        store_task_token(event, make_deps())
