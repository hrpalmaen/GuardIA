from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guardia_common.schedule import minutes_until, parse_timestamp, sla_deadline
from tests.unit.conftest import bogota


@pytest.mark.req("2.3")
@pytest.mark.parametrize("reference,expected", [
    (bogota(3, 30), bogota(6, 0)),
    (bogota(5, 59), bogota(6, 0)),
    (bogota(6, 0), bogota(6, 0, day=13)),
    (bogota(22, 0), bogota(6, 0, day=13)),
    (bogota(23, 30, day=31, month=12).replace(year=2026), datetime(2027, 1, 1, 6, 0,
                                                                   tzinfo=bogota(0).tzinfo)),
])  # fmt: skip
def test_sla_deadline_is_next_deadline_in_bogota(reference, expected):
    assert sla_deadline(reference, "06:00", "America/Bogota") == expected


@pytest.mark.req("2.3")
def test_reference_in_utc_is_converted_to_bogota():
    # 08:30 UTC = 03:30 Bogota
    reference = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    assert sla_deadline(reference, "06:00", "America/Bogota").isoformat() == (
        "2026-11-12T06:00:00-05:00"
    )


def test_minutes_until():
    assert minutes_until(bogota(6, 0), bogota(4, 0)) == 120
    assert minutes_until(bogota(6, 0), bogota(5, 30)) == 30
    assert minutes_until(bogota(6, 0), bogota(6, 10)) == -10


def test_parse_timestamp_formats():
    assert parse_timestamp(None) is None
    assert parse_timestamp(1762936200000) == datetime(2025, 11, 12, 8, 30, tzinfo=UTC)
    assert parse_timestamp("2026-11-12T04:00:00-05:00") == bogota(4, 0)
    assert parse_timestamp("2026-11-12T09:00:00Z") == bogota(4, 0)
