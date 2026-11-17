"""On-call rotation lookup (task 6.1 · requirements 7.1, 7.4).

`OnCallSchedule` has one row per week: PK `week_start` (the Monday, ISO date)
with `primary` and `backup`, each carrying `display_name`, `email`, and
`teams_user_id`. The week changes on Monday 00:00 Colombia time.

Only `display_name` and `teams_user_id` leave this module: Teams is the only
escalation channel, so emails stay in the table and never travel through the
Step Functions state or the logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import from_dynamo
from .schedule import get_tz

WARNING_NO_SCHEDULE = "NO_ONCALL_SCHEDULE"  # no row for the week
WARNING_NO_PRIMARY = "NO_PRIMARY_ONCALL"  # row exists, but no usable on-call engineer (7.4)
WARNING_NO_BACKUP = "NO_BACKUP_ONCALL"


class OnCallPerson(BaseModel):
    """A person who can be mentioned in Teams: both fields are required to mention them."""

    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(min_length=1)
    teams_user_id: str = Field(min_length=1)

    def as_recipient(self) -> dict:
        return {"display_name": self.display_name, "teams_user_id": self.teams_user_id}


@dataclass
class OnCallResolution:
    week_start: str
    primary: OnCallPerson | None = None
    backup: OnCallPerson | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "week_start": self.week_start,
            "primary": self.primary.as_recipient() if self.primary else None,
            "backup": self.backup.as_recipient() if self.backup else None,
            "warnings": list(self.warnings),
        }


def week_start(moment: datetime, tz_name: str = "America/Bogota") -> str:
    """ISO date of the Monday of the week that contains `moment` in `tz_name`."""
    local = moment.astimezone(get_tz(tz_name)).date()
    return (local - timedelta(days=local.weekday())).isoformat()


def _person(raw: Any) -> OnCallPerson | None:
    if not isinstance(raw, dict):
        return None
    try:
        return OnCallPerson.model_validate(from_dynamo(raw))
    except ValidationError:
        return None  # without a Teams identifier the person cannot be mentioned


def resolve_on_call(table: Any, now: datetime, tz_name: str = "America/Bogota") -> OnCallResolution:
    """Requirement 7.1: who is on call today. Requirement 7.4: if the on-call
    engineer is missing, return only the backup and a configuration warning."""
    start = week_start(now, tz_name)
    item = table.get_item(Key={"week_start": start}).get("Item")
    if not item:
        return OnCallResolution(week_start=start, warnings=[WARNING_NO_SCHEDULE])

    primary, backup = _person(item.get("primary")), _person(item.get("backup"))
    warnings = []
    if primary is None:
        warnings.append(WARNING_NO_PRIMARY)
    if backup is None:
        warnings.append(WARNING_NO_BACKUP)
    return OnCallResolution(week_start=start, primary=primary, backup=backup, warnings=warnings)
