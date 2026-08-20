"""Place a repeating lifting block around the running that already exists.

The athlete asked for the same Upper day and the same Lower day every week for a
whole prep. Keeping the *sessions* fixed is the easy half; the hard half is
choosing which calendar days they land on, because a lift is only free when it
does not tax a run that matters.

Two rules do most of the work:

* A lift never lands on a day whose own run is a key session and whose next day
  is also one — there is no recovery window there at all.
* Slots strictly alternate along the calendar. Four sessions in seven days forces
  at least one pair of consecutive days, and alternating is what stops that pair
  being two leg days.
* Of the two possible alternations, the one keeping **lower** days away from the
  morning before a key run wins, because that is the placement that costs a run.

Everything else is spacing: given N sessions in a week, spread them.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import strength_effort

SLOTS = ("lower", "upper", "full")

# More than this in a week stops being a block and starts being the whole plan.
MAX_SESSIONS_PER_WEEK = 6


def _as_date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _index_plan(plan_days: Any) -> dict[datetime.date, dict[str, Any]]:
    indexed: dict[datetime.date, dict[str, Any]] = {}
    # A truthy non-sequence survives `or []`, so the type is checked directly.
    if not isinstance(plan_days, (list, tuple)):
        return indexed
    for day in plan_days:
        if not isinstance(day, dict):
            continue
        date = _as_date(day.get("date"))
        if date is not None:
            indexed[date] = day
    return indexed


def _day_freedom(date: datetime.date, plan: dict[datetime.date, dict[str, Any]]) -> int:
    """How free a day is for lifting. Higher is better; 0 means unusable.

    A key run on the day itself costs less than one the next morning, because
    lifting after a hard run shares fatigue the athlete has already spent,
    while lifting the day before compromises a session not yet run.
    """
    today_key = strength_effort.is_key_run(plan.get(date))
    tomorrow_key = strength_effort.is_key_run(plan.get(date + datetime.timedelta(days=1)))
    if today_key and tomorrow_key:
        return 0
    if tomorrow_key:
        return 1
    if today_key:
        return 2
    rest = str((plan.get(date) or {}).get("discipline") or "").lower() in {"rest", "recovery"}
    return 4 if rest else 3


def _spread(candidates: list[datetime.date], count: int) -> list[datetime.date]:
    """Pick ``count`` dates that stay as far apart as possible.

    Greedy: take the best-spaced remaining date each time, which for a week is
    both optimal enough and easy to reason about when it picks oddly.
    """
    chosen: list[datetime.date] = []
    remaining = sorted(candidates)
    while remaining and len(chosen) < count:
        if not chosen:
            chosen.append(remaining.pop(0))
            continue
        best, best_gap = None, -1
        for date in remaining:
            gap = min(abs((date - picked).days) for picked in chosen)
            if gap > best_gap:
                best, best_gap = date, gap
        chosen.append(best)
        remaining.remove(best)
    return sorted(chosen)


def place_week(*, week_start: datetime.date, sessions: int,
               plan: dict[datetime.date, dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the lifting days for one week and assign upper/lower to them."""
    dates = [week_start + datetime.timedelta(days=offset) for offset in range(7)]
    freedom = {date: _day_freedom(date, plan) for date in dates}
    usable = [date for date in dates if freedom[date] > 0]
    if not usable:
        return []
    picked = _spread(usable, min(sessions, len(usable)))

    # Slots strictly alternate along the calendar, so two lower days can never
    # land back to back. Four sessions in seven days forces at least one pair of
    # consecutive days; alternating is what stops that pair being two leg days.
    #
    # Both parities are tried and the one that puts fewer lower days directly
    # before a key run wins, since that is the placement that actually costs a
    # run. Ties keep the parity that starts on the freer day.
    def _score(start_with_lower: bool) -> tuple[int, int]:
        penalty = free_bonus = 0
        for index, date in enumerate(picked):
            is_lower = (index % 2 == 0) == start_with_lower
            if is_lower and freedom[date] <= 1:   # freedom 1 means a key run tomorrow
                penalty += 1
            if is_lower:
                free_bonus += freedom[date]
        return penalty, -free_bonus

    start_with_lower = min((True, False), key=_score)
    placements = []
    for index, date in enumerate(picked):
        is_lower = (index % 2 == 0) == start_with_lower
        placements.append({
            "date": date.isoformat(),
            "slot": "lower" if is_lower else "upper",
            "freedom": freedom[date],
        })
    return placements


def build(*, start: datetime.date, weeks: int, sessions_per_week: int,
          plan_days: Any, readiness: Any = None, strength: Any = None,
          until: datetime.date | None = None) -> dict[str, Any]:
    """Lay out a repeating block and attach today's effort call to each day.

    Returns placements only. Nothing is written: the athlete agrees the block
    first, exactly as with a single proposed session.
    """
    if weeks < 1:
        raise ValueError("a block needs at least one week")
    sessions = max(1, min(int(sessions_per_week), MAX_SESSIONS_PER_WEEK))
    plan = _index_plan(plan_days)

    # Anchor to the Monday on or before `start` so weeks line up with the plan.
    week_start = start - datetime.timedelta(days=start.weekday())
    placements: list[dict[str, Any]] = []
    for week in range(weeks):
        current = week_start + datetime.timedelta(weeks=week)
        for placement in place_week(week_start=current, sessions=sessions, plan=plan):
            date = _as_date(placement["date"])
            if date is None or date < start:
                continue
            if until is not None and date > until:
                continue
            effort = strength_effort.decide(
                today=date, plan_days=plan_days if isinstance(plan_days, (list, tuple)) else [],
                readiness=readiness, strength=strength,
            )
            placements.append({
                **placement,
                "week_index": week,
                "effort_level": effort["level"],
                "effort_cue": effort["cue"],
                "reps_in_reserve": effort["reps_in_reserve"],
            })
    counts = {slot: sum(1 for p in placements if p["slot"] == slot) for slot in SLOTS}
    return {
        "start": start.isoformat(),
        "weeks": weeks,
        "sessions_per_week": sessions,
        "placements": placements,
        "counts": {slot: count for slot, count in counts.items() if count},
        "rules": [
            "A lift is never placed where a key run falls both that day and the next.",
            "Upper and lower alternate, so two leg days never land back to back.",
            "Of the two alternations, the one keeping lower days off the morning "
            "before a key run is chosen.",
            "The Upper and Lower sessions themselves stay identical all block; the load "
            "progresses, the movements do not.",
        ],
    }
