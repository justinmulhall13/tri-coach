"""Statistics for the lifting tab, computed from logged Hevy workouts.

Everything here is derived from sets the athlete actually performed. Where a
number is an estimate rather than a measurement — an e1RM is a formula, not a
lift — it is labelled as one, because a tab full of confident numbers invites
training decisions the data does not support.

Two of these exist specifically for this athlete: ``push_pull_balance`` and
``rule_compliance``. A bad shoulder and a one-press-per-session rule make the
push/pull ratio a health metric rather than a curiosity.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from . import lifting_rules, strength_visual
from . import strength_weights as sw

# An e1RM extrapolated from a high-rep set is mostly fiction, so sets beyond
# this are recorded as volume but never as strength estimates.
MAX_E1RM_REPS = 12

# Exercises untouched for longer than this are surfaced as drifting away.
STALE_AFTER_DAYS = 28

_MUSCLE_LABEL = {
    "push": "Push", "pull": "Pull", "legs": "Legs", "core": "Core",
    "shoulder": "Shoulders", "conditioning": "Conditioning", "other": "Other",
}


def _as_date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _sets(workouts: Any):
    """Yield ``(date, exercise, set)`` for every usable logged set."""
    if not isinstance(workouts, (list, tuple)):
        return
    for workout in workouts:
        if not isinstance(workout, dict):
            continue
        date = _as_date(workout.get("start_time"))
        if date is None:
            continue
        for exercise in workout.get("exercises") or []:
            if not isinstance(exercise, dict):
                continue
            for item in exercise.get("sets") or []:
                if isinstance(item, dict):
                    yield date, exercise, item


def _weight_reps(item: dict[str, Any]) -> tuple[float, int] | None:
    weight, reps = item.get("weight_kg"), item.get("reps")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
        return None
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        return None
    return float(weight), reps


def recent_sessions(workouts: Any, *, limit: int = 12,
                    records: Any = None) -> list[dict[str, Any]]:
    """Recent sessions with the sets actually performed.

    The tab exists to reflect what is in Hevy, so a session carries its real
    sets — weight, reps, and whether a set matched the all-time best for that
    exercise — rather than only a summary. Editing happens in Hevy; this is the
    record of what happened.
    """
    sessions: list[dict[str, Any]] = []
    if not isinstance(workouts, (list, tuple)):
        return sessions
    # All-time best per exercise, used to mark a set as a personal record.
    # A PR means beating a previous best. An exercise performed once has no
    # previous best, so badging its only session would put "PR" on every new
    # movement and teach the athlete to ignore the badge.
    best_by_exercise: dict[str, float] = {}
    if isinstance(records, (list, tuple)):
        for record in records:
            if (isinstance(record, dict) and record.get("exercise")
                    and record.get("sessions_logged", 0) >= 2):
                best_by_exercise[str(record["exercise"])] = float(record.get("e1rm_lb") or 0)

    for workout in workouts:
        if not isinstance(workout, dict):
            continue
        date = _as_date(workout.get("start_time"))
        if date is None:
            continue
        rendered: list[dict[str, Any]] = []
        tonnage = 0.0
        set_count = 0
        pr_count = 0
        for exercise in workout.get("exercises") or []:
            if not isinstance(exercise, dict):
                continue
            title = str(exercise.get("title") or "").strip() or "Exercise"
            items: list[dict[str, Any]] = []
            top_lb = top_reps = 0
            for item in exercise.get("sets") or []:
                if not isinstance(item, dict):
                    continue
                set_count += 1
                pair = _weight_reps(item)
                if not pair:
                    # Bodyweight or timed work still belongs in the record.
                    items.append({"weight_lb": None, "reps": item.get("reps"),
                                  "duration_seconds": item.get("duration_seconds"),
                                  "type": item.get("type") or "normal", "is_pr": False})
                    continue
                weight_lb = sw.history_weight_lb(pair[0])
                tonnage += weight_lb * pair[1]
                is_pr = False
                if pair[1] <= MAX_E1RM_REPS and title in best_by_exercise:
                    e1rm = sw.epley_e1rm_lb(weight_lb, pair[1])
                    is_pr = abs(e1rm - best_by_exercise[title]) < 0.05
                    if is_pr:
                        pr_count += 1
                if weight_lb > top_lb:
                    top_lb, top_reps = weight_lb, pair[1]
                items.append({"weight_lb": weight_lb, "reps": pair[1],
                              "type": item.get("type") or "normal", "is_pr": is_pr})
            rendered.append({
                "title": title,
                "pattern": strength_visual.classify(title),
                "sets": items,
                "set_count": len(items),
                "top_set": {"weight_lb": top_lb, "reps": top_reps} if top_lb else None,
            })
        sessions.append({
            "workout_id": workout.get("id"),
            "date": date.isoformat(),
            "title": str(workout.get("title") or "Workout"),
            "exercise_count": len(rendered),
            "set_count": set_count,
            "tonnage_lb": round(tonnage),
            "pr_count": pr_count,
            "exercises": rendered,
            # The programming rules apply to logged sessions too, so a session
            # that broke them is visible rather than only caught at generation.
            "rule_status": lifting_rules.summary(
                [{"title": e["title"]} for e in rendered]),
        })
    sessions.sort(key=lambda item: item["date"], reverse=True)
    return sessions[:limit]


def exercise_records(workouts: Any) -> list[dict[str, Any]]:
    """Per-exercise best set, estimated 1RM, and how it has moved over time.

    Progress is measured between the best set of the *earliest session* and the
    best set overall. Comparing raw first-to-best sets instead measures the
    warmup: on real data that reported a 443% "gain" on bench press where both
    ends were the same December afternoon.
    """
    per_day: dict[str, dict[datetime.date, dict[str, Any]]] = defaultdict(dict)
    seen: dict[str, datetime.date] = {}
    counts: dict[str, int] = defaultdict(int)

    for date, exercise, item in _sets(workouts):
        title = str(exercise.get("title") or "").strip()
        if not title:
            continue
        counts[title] += 1
        if title not in seen or date > seen[title]:
            seen[title] = date
        pair = _weight_reps(item)
        if not pair or pair[1] > MAX_E1RM_REPS:
            continue
        weight_lb = sw.history_weight_lb(pair[0])
        e1rm = sw.epley_e1rm_lb(weight_lb, pair[1])
        best_today = per_day[title].get(date)
        if best_today is None or e1rm > best_today["e1rm_lb"]:
            per_day[title][date] = {"weight_lb": weight_lb, "reps": pair[1],
                                    "e1rm_lb": round(e1rm, 1),
                                    "date": date.isoformat()}

    records = []
    for title, days in per_day.items():
        if not days:
            continue
        top = max(days.values(), key=lambda r: r["e1rm_lb"])
        opening = days[min(days)]
        sessions = len(days)
        # A gain needs two different sessions to compare; one session can only
        # show a best, never progress.
        change = top["e1rm_lb"] - opening["e1rm_lb"] if sessions >= 2 else 0.0
        pct = (change / opening["e1rm_lb"] * 100) if (sessions >= 2 and opening["e1rm_lb"]) else 0.0
        records.append({
            "exercise": title,
            "best_weight_lb": top["weight_lb"],
            "best_reps": top["reps"],
            "e1rm_lb": top["e1rm_lb"],
            "e1rm_is_estimate": True,
            "pr_date": top["date"],
            "first_e1rm_lb": opening["e1rm_lb"],
            "first_date": opening["date"],
            "sessions_logged": sessions,
            "change_lb": round(change, 1),
            "change_pct": round(pct, 1),
            "sets_logged": counts[title],
            "last_trained": seen[title].isoformat(),
            "pattern": strength_visual.classify(title),
        })
    records.sort(key=lambda r: -r["e1rm_lb"])
    return records


def biggest_gains(records: Any, *, limit: int = 5,
                  min_sessions: int = 3) -> list[dict[str, Any]]:
    """Exercises that genuinely improved, across separate sessions.

    ``min_sessions`` is the guard that matters: two sessions can be a good day
    against a bad one, and a single session is not progress at all.
    """
    if not isinstance(records, (list, tuple)):
        return []
    eligible = [
        r for r in records
        if isinstance(r, dict)
        and r.get("sessions_logged", 0) >= min_sessions
        and r.get("change_lb", 0) > 0
        and r.get("first_date") != r.get("pr_date")
    ]
    return sorted(eligible, key=lambda r: -r["change_pct"])[:limit]


def push_pull_balance(workouts: Any, *, days: int = 90,
                      today: datetime.date | None = None) -> dict[str, Any]:
    """Working-set balance across movement groups.

    For a shoulder that does not tolerate pressing volume, a push-heavy ratio
    is an early warning, not a statistic.
    """
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    groups: dict[str, int] = defaultdict(int)
    for date, exercise, item in _sets(workouts):
        if date < cutoff:
            continue
        groups[lifting_rules.group_of({"title": exercise.get("title")})] += 1
    push, pull = groups.get("push", 0), groups.get("pull", 0)
    ratio = round(pull / push, 2) if push else None
    if ratio is None:
        verdict = "no pressing volume logged in this window"
    elif ratio >= 1.5:
        verdict = "pull-dominant, which is the safe side for this shoulder"
    elif ratio >= 1.0:
        verdict = "balanced"
    else:
        verdict = "push-dominant: add pulling volume to protect the shoulder"
    return {
        "window_days": days,
        "sets_by_group": {_MUSCLE_LABEL.get(k, k.title()): v
                          for k, v in sorted(groups.items(), key=lambda kv: -kv[1])},
        "push_sets": push, "pull_sets": pull,
        "pull_per_push": ratio,
        "verdict": verdict,
    }


def consistency(workouts: Any, *, weeks: int = 12,
                today: datetime.date | None = None) -> dict[str, Any]:
    """Sessions per week and the current run of active weeks."""
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(weeks=weeks)
    dates = sorted({d for d, _, _ in _sets(workouts) if d >= cutoff})
    by_week: dict[tuple[int, int], int] = defaultdict(int)
    for date in dates:
        by_week[date.isocalendar()[:2]] += 1
    streak = 0
    cursor = today
    while True:
        key = cursor.isocalendar()[:2]
        if by_week.get(key):
            streak += 1
            cursor -= datetime.timedelta(days=7)
        else:
            break
    return {
        "window_weeks": weeks,
        "sessions": len(dates),
        "sessions_per_week": round(len(dates) / weeks, 2) if weeks else 0.0,
        "active_weeks": len(by_week),
        "current_week_streak": streak,
        "last_session": dates[-1].isoformat() if dates else None,
    }


def stale_exercises(records: Any, *, today: datetime.date | None = None,
                    days: int = STALE_AFTER_DAYS) -> list[dict[str, Any]]:
    """Lifts drifting out of the rotation, newest-stale first."""
    today = today or datetime.date.today()
    out = []
    for record in records if isinstance(records, (list, tuple)) else []:
        if not isinstance(record, dict):
            continue
        last = _as_date(record.get("last_trained"))
        if last is None:
            continue
        idle = (today - last).days
        if idle >= days:
            out.append({"exercise": record.get("exercise"), "days_since": idle,
                        "last_trained": last.isoformat(),
                        "e1rm_lb": record.get("e1rm_lb")})
    out.sort(key=lambda r: r["days_since"])
    return out


def tonnage_trend(workouts: Any, *, weeks: int = 8,
                  today: datetime.date | None = None) -> list[dict[str, Any]]:
    """Weekly total volume in pounds, oldest first, for a sparkline."""
    today = today or datetime.date.today()
    start = today - datetime.timedelta(weeks=weeks)
    buckets: dict[str, float] = defaultdict(float)
    for date, _exercise, item in _sets(workouts):
        if date < start:
            continue
        pair = _weight_reps(item)
        if not pair:
            continue
        monday = date - datetime.timedelta(days=date.weekday())
        buckets[monday.isoformat()] += sw.history_weight_lb(pair[0]) * pair[1]
    return [{"week_of": week, "tonnage_lb": round(total)}
            for week, total in sorted(buckets.items())]


def build(workouts: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Everything the lifting tab renders, from one pass over the log."""
    today = today or datetime.date.today()
    records = exercise_records(workouts)
    return {
        "generated_for": today.isoformat(),
        "unit": "lb",
        "has_data": bool(records),
        "recent_sessions": recent_sessions(workouts, records=records),
        "records": records,
        "biggest_gains": biggest_gains(records),
        "push_pull_balance": push_pull_balance(workouts, today=today),
        "consistency": consistency(workouts, today=today),
        "stale_exercises": stale_exercises(records, today=today),
        "tonnage_trend": tonnage_trend(workouts, today=today),
        "notes": [
            "Estimated 1RMs are Epley formula values from sets of "
            f"{MAX_E1RM_REPS} reps or fewer, not tested maxes.",
            "Every weight is shown in pounds; Hevy stores kilograms.",
        ],
    }
