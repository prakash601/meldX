"""Owner-timezone date semantics (product plan §4.2, v0.0.2).

Acceptance targets: India midnight boundaries differ correctly from UTC, and
DST edge behavior for a non-India zone is explicit rather than accidental.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from meldx.app import dates


@pytest.fixture
def ist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_TIMEZONE", "Asia/Kolkata")


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_TIMEZONE", "America/New_York")


def test_india_day_bounds_differ_from_utc(ist: None) -> None:
    # IST is UTC+5:30, so the local day STARTS at 18:30 UTC the previous day.
    start, end = dates.local_day_bounds(date(2026, 11, 23))
    assert start == datetime(2026, 11, 22, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 11, 23, 18, 30, tzinfo=UTC)


def test_due_instant_near_ist_midnight_lands_on_the_owner_local_day(ist: None) -> None:
    # IST day bounds: Nov 22 local = [Nov 21 18:30Z, Nov 22 18:30Z).
    # 18:29Z is still Nov 22 IST; 18:31Z is already Nov 23 IST.
    assert dates.is_due_today(datetime(2026, 11, 22, 18, 29, tzinfo=UTC), None, date(2026, 11, 22))
    assert not dates.is_due_today(datetime(2026, 11, 22, 18, 31, tzinfo=UTC), None, date(2026, 11, 22))


def test_dst_spring_forward_day_is_23_hours(new_york: None) -> None:
    # 2027-03-14: 02:00 EST jumps to 03:00 EDT — midnight exists on both sides,
    # so the local day is one hour short.
    start, end = dates.local_day_bounds(date(2027, 3, 14))
    assert end - start == timedelta(hours=23)
    assert start == datetime(2027, 3, 14, 5, 0, tzinfo=UTC)  # 00:00 EST
    assert end == datetime(2027, 3, 15, 4, 0, tzinfo=UTC)  # 00:00 EDT


def test_dst_fall_back_day_is_25_hours(new_york: None) -> None:
    start, end = dates.local_day_bounds(date(2027, 11, 7))
    assert end - start == timedelta(hours=25)


def test_naive_iso_means_owner_timezone(ist: None) -> None:
    assert dates.parse_instant("2026-11-24T09:00:00") == datetime(2026, 11, 24, 3, 30, tzinfo=UTC)


def test_naive_iso_changes_with_the_owner_zone(new_york: None) -> None:
    assert dates.parse_instant("2026-11-24T09:00:00") == datetime(2026, 11, 24, 14, 0, tzinfo=UTC)


def test_tomorrow_9am_is_owner_local(ist: None) -> None:
    parsed = dates.parse_instant("tomorrow 9am")
    tomorrow = dates.today_date() + timedelta(days=1)
    expected = datetime.combine(tomorrow, time(9), tzinfo=dates.owner_zone()).astimezone(UTC)
    assert parsed == expected


def test_bare_time_means_owner_local_today(ist: None) -> None:
    parsed = dates.parse_instant("9am")
    expected = datetime.combine(dates.today_date(), time(9), tzinfo=dates.owner_zone()).astimezone(UTC)
    assert parsed == expected


def test_parse_date_iso_and_natural(ist: None) -> None:
    assert dates.parse_date("2026-11-25") == date(2026, 11, 25)
    assert dates.parse_date("tomorrow") == dates.today_date() + timedelta(days=1)
    assert dates.parse_date("today") == dates.today_date()


def test_parse_failures_raise_contract_errors(ist: None) -> None:
    with pytest.raises(ValueError, match="Cannot parse due date"):
        dates.parse_instant("garbage o'clock")
    with pytest.raises(ValueError, match="Cannot parse due date"):
        dates.parse_date("garbage")


def test_overdue_uses_date_or_instant(ist: None) -> None:
    today = dates.today_date()
    assert dates.is_overdue(None, today - timedelta(days=1))
    assert dates.is_overdue(datetime.now(UTC) - timedelta(hours=1), None)
    assert not dates.is_overdue(None, today)
    assert not dates.is_overdue(datetime.now(UTC) + timedelta(hours=1), None)
    # A missing deadline is never overdue.
    assert not dates.is_overdue(None, None)


def test_unknown_timezone_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError, match="Unknown OWNER_TIMEZONE"):
        dates.owner_zone()
