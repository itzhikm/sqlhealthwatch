"""Time helpers. Everything persisted is UTC; only quiet hours and the daily tier time are local."""

from __future__ import annotations

from datetime import datetime, time, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp, matching the DATETIME2 columns in the repository."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_hhmm(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(hour=int(hours), minute=int(minutes or 0))


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Quiet hours are local wall-clock and may wrap past midnight (e.g. 22:00 -> 06:00)."""
    start_t, end_t = parse_hhmm(start), parse_hhmm(end)
    current = now.time()
    if start_t == end_t:
        return False
    if start_t < end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t
