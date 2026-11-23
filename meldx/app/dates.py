"""Owner-timezone date and datetime helpers (product plan §4.2).

One module owns every interpretation of "today", "tomorrow 9am", and local-day
boundaries so the service, REST, and web always agree.

Rules:
- Instants are stored in UTC. Naive input means the OWNER's timezone.
- Calendar dates are stored as dates, never as midnight instants that would
  drift across timezones.
- "Today" is the half-open local-day interval [midnight, next midnight) in
  OWNER_TIMEZONE, converted to UTC for timestamp comparisons.
- Natural language is anchored to the owner's local now (RELATIVE_BASE), NOT
  converted after parsing in the server's zone: "tomorrow 9am" is 09:00
  tomorrow in OWNER_TIMEZONE.

A nonexistent local time in a DST gap (e.g. 02:30 during a US spring-forward)
resolves forward per PEP 495 fold semantics; tests document this.
"""
import os
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser

DEFAULT_TIMEZONE = "Asia/Kolkata"


def owner_timezone() -> str:
    return os.environ.get("OWNER_TIMEZONE") or DEFAULT_TIMEZONE


def owner_zone() -> ZoneInfo:
    name = owner_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown OWNER_TIMEZONE: {name!r}") from None


def owner_now() -> datetime:
    """Current instant expressed in the owner's timezone."""
    return datetime.now(UTC).astimezone(owner_zone())


def today_date() -> date:
    """The owner's calendar today — not the server's or UTC's."""
    return owner_now().date()


def local_day_bounds(day: date | None = None) -> tuple[datetime, datetime]:
    """Half-open [start, end) owner-local day interval, converted to UTC."""
    day = day or today_date()
    zone = owner_zone()
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def parse_instant(value: str) -> datetime:
    """Parse an instant. ISO 8601 or natural language, owner-local when naive.

    Raises ValueError with the due-date contract message when unparseable.
    """
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # Anchor "today"/"tomorrow"/"friday" to the OWNER's local now, then
        # attach the owner zone below — parsing in the server zone and
        # converting afterwards would shift the phrase by the UTC offset.
        base = owner_now().replace(tzinfo=None)
        dt = dateparser.parse(value, settings={"RELATIVE_BASE": base})
        if dt is None:
            raise ValueError(f"Cannot parse due date: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=owner_zone())
    return dt.astimezone(UTC)


def parse_date(value: str) -> date:
    """Parse an owner-local calendar date: ISO YYYY-MM-DD or natural language
    ("today", "tomorrow", "friday"). Raises ValueError when unparseable."""
    return parse_instant(value).astimezone(owner_zone()).date()


def is_due_today(
    task_due_at: datetime | None, task_due_date: date | None, day: date | None = None
) -> bool:
    """Deadline falls on the owner-local day: date-only equality, or the
    instant inside the local-day interval."""
    if task_due_date is not None:
        return task_due_date == (day or today_date())
    if task_due_at is not None:
        start, end = local_day_bounds(day)
        return start <= task_due_at < end
    return False


def is_overdue(
    task_due_at: datetime | None,
    task_due_date: date | None,
    *,
    now: datetime | None = None,
    day: date | None = None,
) -> bool:
    """The deadline has passed (owner-local). A missing deadline is never
    overdue."""
    day = day or today_date()
    now = now or datetime.now(UTC)
    if task_due_date is not None:
        return task_due_date < day
    if task_due_at is not None:
        return task_due_at < now
    return False
