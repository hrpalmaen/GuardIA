"""Time rules: SLA deadline and remaining minutes.

Task 3.3 (expected executions and median duration) also lives in this module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Colombia has had no DST since 1993. Used only if the runtime lacks tzdata.
_FIXED_FALLBACK = {"America/Bogota": timezone(timedelta(hours=-5), "America/Bogota")}


def get_tz(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in _FIXED_FALLBACK:
            return _FIXED_FALLBACK[name]
        raise


def to_local_iso(moment: datetime, tz_name: str) -> str:
    """ISO 8601 with offset, as tech.md requires for stored timestamps."""
    return moment.astimezone(get_tz(tz_name)).isoformat(timespec="seconds")


def parse_timestamp(value) -> datetime | None:
    """Epoch milliseconds (Step Functions events) or ISO 8601. Naive ISO is UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def sla_deadline(reference: datetime, deadline_local: str, tz_name: str) -> datetime:
    """First `deadline_local` (HH:MM, in `tz_name`) strictly after `reference`.

    A run that starts at 03:30 has its deadline at 06:00 the same day; a run
    that starts at 06:00 or later is due at 06:00 the next day.
    """
    hour, minute = (int(part) for part in deadline_local.split(":"))
    tz = get_tz(tz_name)
    local = reference.astimezone(tz)
    deadline = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if deadline <= local:
        deadline += timedelta(days=1)  # aware arithmetic keeps the wall-clock time
    return deadline


def minutes_until(deadline: datetime, now: datetime) -> int:
    """Whole minutes from `now` to `deadline`; negative once it has passed."""
    return int((deadline - now).total_seconds() // 60)
