from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from guardia_common.audit import AuditRepository
from guardia_common.models import CatalogEntry, ProcessRecord
from tests.unit.conftest import bogota


@pytest.mark.req("1.4")
@pytest.mark.parametrize("value,expected", [
    ("auto", "auto"), ("escalate-only", "escalate-only"),
    ("AUTO", "escalate-only"), ("yes", "escalate-only"), (None, "escalate-only"),
])  # fmt: skip
def test_invalid_remediation_is_treated_as_escalate_only(value, expected):
    record = ProcessRecord(
        resource_arn="arn", process_type="master", organization="O", remediation=value
    )
    assert record.remediation == expected


@pytest.mark.parametrize("row", [
    {"process_type": "*", "error_pattern": "x", "classification": "maybe", "action": "escalate"},
    {"process_type": "*", "error_pattern": "x", "classification": "auto_recoverable",
     "action": "reboot_cluster"},
    {"process_type": "*", "error_pattern": "([", "classification": "auto_recoverable",
     "action": "redrive"},
    {"process_type": "*", "error_pattern": "x", "classification": "auto_recoverable",
     "action": "redrive", "max_wait_s": -1},
])  # fmt: skip
def test_catalog_entry_rejects_invalid_rows(row):
    with pytest.raises(ValidationError):
        CatalogEntry.model_validate(row)


@pytest.mark.req("10.1")
def test_audit_events_never_overwrite_each_other(aws):
    repo = AuditRepository(aws["tables"]["IncidentEvents"], actor="test")
    first = repo.append("inc-1", "A", now=bogota(4, 0))
    second = repo.append("inc-1", "B", now=bogota(4, 0))  # same millisecond

    assert first.sk != second.sk
    assert second.sk.endswith("#0001")
    assert aws["tables"]["IncidentEvents"].scan()["Count"] == 2


@pytest.mark.req("10.2")
def test_audit_events_expire_after_13_months(aws):
    event = AuditRepository(aws["tables"]["IncidentEvents"], actor="test").append(
        "inc-1", "A", now=bogota(4, 0)
    )
    assert event.expires_at == int(bogota(4, 0, month=12).replace(year=2027).timestamp())


def test_audit_propagates_unexpected_errors():
    class Broken:
        def put_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "Put")

    with pytest.raises(ClientError):
        AuditRepository(Broken(), actor="test").append("inc-1", "A")
