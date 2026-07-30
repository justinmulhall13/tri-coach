"""Default start-times for workouts, scheduled around a fixed 9-4 workday.

The plan stores a duration but no time of day. When we first sync a day to Google
we pick a sensible timed block: mornings before work on weekdays (or evenings if
the session is too long to finish before 09:00), any morning on weekends. The
user can always drag it afterwards — once `start_time` is stored, this module is
no longer consulted for that day.
"""
from __future__ import annotations

import datetime
from typing import Any

WORK_START = 9 * 60      # 09:00 in minutes-from-midnight
WORK_END = 16 * 60       # 16:00 (4 PM)
MORNING_FLOOR = 5 * 60 + 30   # don't start a weekday session before 05:30
PRE_WORK_BUFFER = 15     # finish this many minutes before work starts
EVENING_START = 17 * 60  # after-work fallback slot
WEEKEND_START = 8 * 60   # 08:00


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(minutes, 23 * 60 + 59))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _to_minutes(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _busy_ranges(existing_events: list[dict[str, Any]], date: str) -> list[tuple[int, int]]:
    """Minutes-from-midnight (start, end) for timed events on `date`."""
    ranges: list[tuple[int, int]] = []
    for e in existing_events or []:
        if e.get("all_day"):
            continue
        s = e.get("start") or ""
        en = e.get("end") or ""
        if not s.startswith(date):
            continue
        sm = _minutes_of_iso(s)
        em = _minutes_of_iso(en) if en.startswith(date) else 24 * 60
        if sm is not None:
            ranges.append((sm, em if em is not None else sm + 60))
    return ranges


def _minutes_of_iso(iso: str) -> int | None:
    # "2026-07-24T06:30:00-07:00" -> 390
    if "T" not in iso:
        return None
    try:
        clock = iso.split("T", 1)[1][:5]
        return _to_minutes(clock)
    except Exception:
        return None


def _overlaps(start: int, dur: int, busy: list[tuple[int, int]]) -> bool:
    end = start + dur
    return any(start < b_end and end > b_start for b_start, b_end in busy)


def default_start(day: dict[str, Any], existing_events: list[dict[str, Any]] | None = None) -> str | None:
    """Return HH:MM for this day, or None for a rest/all-day day."""
    if day.get("is_rest") or (day.get("discipline") in ("rest",)):
        return None
    dur = int(day.get("duration_min") or 60)
    try:
        d = datetime.date.fromisoformat(day["date"])
    except Exception:
        return _hhmm(WEEKEND_START)
    busy = _busy_ranges(existing_events or [], day["date"])

    if d.weekday() >= 5:  # Sat/Sun — no work
        candidate = WEEKEND_START
    else:
        # Try to finish PRE_WORK_BUFFER before work; floor at MORNING_FLOOR.
        pre = WORK_START - PRE_WORK_BUFFER - dur
        candidate = pre if pre >= MORNING_FLOOR else EVENING_START

    # Nudge later in 30-min steps if it collides with an existing timed event.
    for _ in range(12):
        if not _overlaps(candidate, dur, busy):
            break
        candidate += 30
    return _hhmm(candidate)
