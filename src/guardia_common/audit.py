"""Append-only repository for `IncidentEvents` (PK incident_id, SK timestamp#seq).

Only conditional PutItem is used, matching the table policy from task 1.2.
The decorator that instruments every Lambda belongs to task 7.1 and builds
on `AuditRepository.append`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta

from .models import IncidentEvent
from .schedule import get_tz

RETENTION_MONTHS = 13  # requirement 10.2
_MAX_SEQ_TRIES = 50


class AuditRepository:
    def __init__(self, table: Any, actor: str, tz_name: str = "America/Bogota"):
        self._table = table
        self._actor = actor
        self._tz_name = tz_name

    def append(
        self,
        incident_id: str,
        event_type: str,
        detail: dict | None = None,
        now: datetime | None = None,
    ) -> IncidentEvent:
        now = (now or datetime.now(UTC)).astimezone(get_tz(self._tz_name))
        stamp = now.isoformat(timespec="milliseconds")
        expires_at = int((now + relativedelta(months=RETENTION_MONTHS)).timestamp())

        for seq in range(_MAX_SEQ_TRIES):
            event = IncidentEvent(
                incident_id=incident_id,
                sk=f"{stamp}#{seq:04d}",
                event_type=event_type,
                actor=self._actor,
                timestamp=stamp,
                detail=detail or {},
                expires_at=expires_at,
            )
            try:
                self._table.put_item(
                    Item=event.to_item(),
                    ConditionExpression=(
                        "attribute_not_exists(incident_id) AND attribute_not_exists(sk)"
                    ),
                )
                return event
            except ClientError as err:
                if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise RuntimeError(f"Could not append audit event for {incident_id}")
