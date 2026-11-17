"""Time rules: SLA deadline, remaining minutes, expected executions, and
maximum duration (task 3.3 · requirements 3.1–3.3).

Schedules are the expressions of the EventBridge rules or Scheduler schedules
that launch each process, in AWS syntax: `cron(min hour dom month dow year)`
or `rate(n unit)`. EventBridge rules are evaluated in UTC; Scheduler schedules
carry their own time zone.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from croniter.croniter import CroniterError

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


# --------------------------------------------------------------------------- #
# Task 3.3 · expected executions and maximum duration
# --------------------------------------------------------------------------- #
class UnsupportedScheduleError(ValueError):
    """The expression cannot be evaluated (e.g. `at(...)`, a fixed year, or invalid cron)."""


_AWS_EXPRESSION = re.compile(r"^\s*(cron|rate)\((.*)\)\s*$", re.IGNORECASE)
_RATE = re.compile(r"^\s*(\d+)\s+(minute|minutes|hour|hours|day|days)\s*$", re.IGNORECASE)
_RATE_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


def _aws_dow_to_cron(field: str) -> str:
    """AWS numbers the days of the week 1–7 from Sunday; croniter uses 0–6.
    `6L` (last Friday in AWS) becomes `L5`; `2#1` (first Monday) becomes `1#1`."""

    def number(token: str) -> str:
        return str(int(token) - 1) if token.isdigit() else token

    items = []
    for item in field.split(","):
        if item.endswith("L") and item[:-1].isdigit():
            items.append(f"L{number(item[:-1])}")
            continue
        base, hash_sep, nth = item.partition("#")
        span, slash, step = base.partition("/")
        span = "-".join(number(part) for part in span.split("-"))
        items.append(f"{span}{slash}{step}{hash_sep}{nth}")
    return ",".join(items)


def aws_cron_to_croniter(fields: str) -> str:
    """`30 * * * ? *` (AWS, 6 fields) → `30 * * * *` (croniter, 5 fields)."""
    parts = fields.split()
    if len(parts) != 6:
        raise UnsupportedScheduleError(f"AWS cron needs 6 fields, got {len(parts)}: {fields!r}")
    minute, hour, dom, month, dow, year = parts
    if year != "*":
        raise UnsupportedScheduleError(f"fixed year is not supported: {fields!r}")
    dom = "*" if dom == "?" else dom
    dow = "*" if dow == "?" else _aws_dow_to_cron(dow)
    expression = f"{minute} {hour} {dom} {month} {dow}"
    if not croniter.is_valid(expression):
        raise UnsupportedScheduleError(f"invalid cron expression: {fields!r}")
    return expression


@dataclass(frozen=True)
class Schedule:
    """One trigger of a process: the expression and the zone it is evaluated in."""

    expression: str
    timezone: str = "UTC"

    def _parsed(self) -> tuple[str, str]:
        match = _AWS_EXPRESSION.match(self.expression)
        if not match:
            raise UnsupportedScheduleError(f"unsupported schedule: {self.expression!r}")
        return match.group(1).lower(), match.group(2)

    @property
    def is_cron(self) -> bool:
        return self._parsed()[0] == "cron"

    def _cron(self, start: datetime) -> croniter:
        try:
            expression = aws_cron_to_croniter(self._parsed()[1])
            return croniter(expression, start.astimezone(get_tz(self.timezone)))
        except CroniterError as exc:
            raise UnsupportedScheduleError(str(exc)) from exc

    def expected_runs(self, start: datetime, end: datetime) -> list[datetime]:
        """Scheduled times in the window (start, end], in UTC.

        Only cron schedules have fixed times; a `rate(...)` counts from when the
        rule was created, so it has no expected times and returns []."""
        if not self.is_cron or end <= start:
            return []
        it = self._cron(start)
        runs = []
        while (moment := it.get_next(datetime)) <= end:
            runs.append(moment.astimezone(UTC))
        return runs

    def period_at(self, moment: datetime) -> timedelta:
        """Gap between the scheduled run at or before `moment` and the next one.
        For an hourly cron it is 1 hour; for a nightly cron with gaps, the gap
        that contains `moment`."""
        kind, body = self._parsed()
        if kind == "rate":
            match = _RATE.match(body)
            if not match:
                raise UnsupportedScheduleError(f"unsupported rate: {self.expression!r}")
            unit = match.group(2).lower().rstrip("s")
            return timedelta(seconds=int(match.group(1)) * _RATE_SECONDS[unit])
        after = moment + timedelta(seconds=1)  # a run exactly at `moment` counts as previous
        previous = self._cron(after).get_prev(datetime)
        following = self._cron(after).get_next(datetime)
        return following - previous


def median_duration_s(durations_s: list[float], *, min_samples: int) -> float | None:
    """Median of the given durations, or None with fewer than `min_samples` (3.3)."""
    if len(durations_s) < min_samples:
        return None
    return float(statistics.median(durations_s))


def max_duration(
    *,
    median_s: float | None,
    multiplier: float,
    period: timedelta | None,
    period_fraction: float = 0.5,
) -> timedelta | None:
    """Longest an execution may stay RUNNING before it is a STUCK_RUN.

    3.2: `multiplier` × the median of the last executions.
    3.3: without enough history (median None), `period_fraction` of the periodicity.
    None when neither is known.
    """
    if median_s is not None:
        return timedelta(seconds=median_s * multiplier)
    if period is not None:
        return period * period_fraction
    return None
