"""Collection slots and local-time helpers.

Slots are defined in UTC as multiples of the interval since the Unix epoch.
Because Europe/Amsterdam (like most zones) only has whole-hour offsets, a
UTC quarter-hour is also a local quarter-hour, and DST changes never create
duplicate or skipped UTC slots. A local day simply has 92 or 100 slots.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def floor_slot(now: datetime, interval_minutes: int) -> datetime:
    step = interval_minutes * 60
    ts = int(now.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(ts - ts % step, tz=timezone.utc)


def next_slot(now: datetime, interval_minutes: int) -> datetime:
    return floor_slot(now, interval_minutes) + timedelta(minutes=interval_minutes)


def due_slot(now: datetime, interval_minutes: int, tolerance_seconds: int) -> datetime | None:
    """The slot that may run at `now`, or None if the current slot's window has passed.

    Only the current slot is ever eligible, so a process that was asleep or
    down never runs a burst of past slots.
    """
    slot = floor_slot(now, interval_minutes)
    if (now - slot).total_seconds() <= tolerance_seconds:
        return slot
    return None


def slots_between(start: datetime, end: datetime, interval_minutes: int) -> list[datetime]:
    """All slots s with start <= s < end."""
    first = floor_slot(start, interval_minutes)
    if first < start:
        first += timedelta(minutes=interval_minutes)
    out = []
    s = first
    step = timedelta(minutes=interval_minutes)
    while s < end:
        out.append(s)
        s += step
    return out


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC instants of local midnight at the start and end of `day` (DST-aware)."""
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(timezone.utc)
    return start, end


def slots_in_local_day(day: date, tz: ZoneInfo, interval_minutes: int) -> list[datetime]:
    start, end = local_day_bounds(day, tz)
    return slots_between(start, end, interval_minutes)


def local_minute_of_day(dt: datetime, tz: ZoneInfo) -> int:
    loc = dt.astimezone(tz)
    return loc.hour * 60 + loc.minute


def local_from_parts(day: date, minute_of_day: int, tz: ZoneInfo) -> datetime | None:
    """Local wall-clock time on `day`; None if it does not exist (spring-forward gap).

    For ambiguous times (fall-back) the first occurrence (fold=0) is used.
    """
    t = time(minute_of_day // 60, minute_of_day % 60)
    dt = datetime.combine(day, t, tzinfo=tz)
    roundtrip = dt.astimezone(timezone.utc).astimezone(tz)
    if (roundtrip.hour, roundtrip.minute) != (t.hour, t.minute):
        return None
    return dt


def period_key(now: datetime, period: str, reset_tz: ZoneInfo) -> str:
    loc = now.astimezone(reset_tz)
    return loc.strftime("%Y-%m") if period == "monthly" else loc.strftime("%Y-%m-%d")


def period_bounds(now: datetime, period: str, reset_tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC start and end of the allowance period containing `now`."""
    loc = now.astimezone(reset_tz)
    if period == "monthly":
        start = datetime(loc.year, loc.month, 1, tzinfo=reset_tz)
        end = datetime(loc.year + (loc.month == 12), loc.month % 12 + 1, 1, tzinfo=reset_tz)
    else:
        start = datetime(loc.year, loc.month, loc.day, tzinfo=reset_tz)
        end = datetime.combine(start.date() + timedelta(days=1), time(0), tzinfo=reset_tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
