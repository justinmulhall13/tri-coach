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


def _rules_for_date(rules: list[dict[str, Any]], date: datetime.date) -> list[dict[str, Any]]:
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    day_name = names[date.weekday()]
    result = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not (str(rule.get("start_date") or "") <= date.isoformat()
                <= str(rule.get("end_date") or "")):
            continue
        days = rule.get("days") or []
        if days and day_name not in days:
            continue
        result.append(rule)
    return result


def _rule_range(rule: dict[str, Any]) -> tuple[int, int] | None:
    start = _to_minutes(str(rule.get("start_time") or ""))
    end = _to_minutes(str(rule.get("end_time") or ""))
    if start is None and end is None:
        return None
    start = 0 if start is None else start
    end = 24 * 60 if end is None else end
    if end <= start:
        end += 24 * 60
    return start, end


def default_start(day: dict[str, Any], existing_events: list[dict[str, Any]] | None = None,
                  schedule_constraints: list[dict[str, Any]] | None = None) -> str | None:
    """Return HH:MM for this day, or None for a rest/all-day day."""
    if day.get("is_rest") or (day.get("discipline") in ("rest",)):
        return None
    dur = int(day.get("duration_min") or 60)
    try:
        d = datetime.date.fromisoformat(day["date"])
    except Exception:
        return _hhmm(WEEKEND_START)
    busy = _busy_ranges(existing_events or [], day["date"])
    rules = _rules_for_date(schedule_constraints or [], d)
    if any(rule.get("kind") in {"busy", "travel"} and _rule_range(rule) is None
           for rule in rules):
        return None
    busy.extend(
        window for rule in rules if rule.get("kind") in {"busy", "travel"}
        for window in [_rule_range(rule)] if window is not None
    )
    buffer = max(
        (int(rule.get("duration_min") or 0) for rule in rules
         if rule.get("kind") == "buffer"),
        default=0,
    )
    effective_duration = dur + buffer
    facility_windows = [
        window for rule in rules if rule.get("kind") == "facility_hours"
        for window in [_rule_range(rule)] if window is not None
    ]
    facility_open = max((window[0] for window in facility_windows),
                        default=MORNING_FLOOR)
    facility_close = min((window[1] for window in facility_windows),
                         default=24 * 60)
    available = [
        window for rule in rules if rule.get("kind") == "available"
        for window in [_rule_range(rule)] if window is not None
    ]

    if d.weekday() >= 5:  # Sat/Sun — no work
        candidate = max(WEEKEND_START, facility_open)
    else:
        # Try to finish PRE_WORK_BUFFER before work; floor at MORNING_FLOOR.
        pre = WORK_START - PRE_WORK_BUFFER - effective_duration
        candidate = pre if pre >= facility_open else max(EVENING_START, facility_open)

    if available:
        # Athlete-confirmed windows are hard bounds. Choose the first feasible
        # slot nearest the normal preference, not a guessed time outside them.
        candidates = []
        for start, end in available:
            earliest = max(start, facility_open)
            latest = min(end, facility_close) - effective_duration
            if earliest <= latest:
                candidates.append(min(max(candidate, earliest), latest))
        if not candidates:
            return None
        candidate = min(candidates, key=lambda value: abs(value - candidate))

    # Nudge later in 15-min steps if it collides with a live or remembered busy
    # block. The effective duration includes the athlete's shower/commute buffer.
    for _ in range(96):
        inside_available = (not available or any(
            candidate >= start and candidate + effective_duration <= end
            for start, end in available
        ))
        if (candidate >= facility_open
                and candidate + effective_duration <= facility_close
                and inside_available
                and not _overlaps(candidate, effective_duration, busy)):
            return _hhmm(candidate)
        candidate += 15
    return None
