"""Merged strength picture built from two sources that disagree by design.

Garmin and Hevy each hold half of the truth about lifting, and they go stale
independently:

* **Garmin** records that a strength session happened, how long it ran and at
  what heart rate.  It never records which exercises were performed or how much
  was lifted.
* **Hevy** records exercises, sets and exact weights.  It only knows about the
  sessions the athlete remembered to log there.

Reading either source alone produces a confident wrong answer.  Hevy alone says
"no lifting since June" while Garmin plainly shows sessions through July; Garmin
alone can say "lifting twice a week" without a single usable working weight.

This module keeps the two separate, dates each one, and derives the single fact
that actually governs a prescription: whether the newest *weight* evidence is
older than the newest *session* evidence.  When it is, the athlete has trained
in ways the numbers cannot see, and week one must be calibrated by effort rather
than prescribed from stale loads.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import strength_weights as sw

# Below this many sessions per week, a returning lifter is treated as partially
# detrained: enough stimulus to keep tissue tolerance, not enough to hold peak
# working weights.
MAINTENANCE_SESSIONS_PER_WEEK = 1.5

# Weight evidence older than this is not a safe basis for a top set even when
# training continued, because the load actually used in between is unknown.
STALE_ANCHOR_DAYS = 21


def _as_date(value: Any) -> datetime.date | None:
    text = str(value or "")[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _garmin_strength_sessions(garmin_load: Any) -> list[dict[str, Any]]:
    """Strength sessions Garmin measured, newest first.

    ``garmin_source`` already routes stretching and yoga to a ``mobility``
    bucket, so filtering on the ``strength`` bucket does not sweep in a
    15-minute mobility session as if it were a lift.
    """
    if not isinstance(garmin_load, dict):
        return []
    sessions = []
    for activity in garmin_load.get("activities") or []:
        if not isinstance(activity, dict) or activity.get("sport") != "strength":
            continue
        date = _as_date(activity.get("date"))
        if date is None:
            continue
        sessions.append({
            "date": date.isoformat(),
            "name": activity.get("name"),
            "minutes": activity.get("minutes"),
            "hr_avg": activity.get("hr_avg"),
            "source": activity.get("source") or "measured",
        })
    sessions.sort(key=lambda item: item["date"], reverse=True)
    return sessions


def _hevy_workouts(hevy_context: Any) -> list[dict[str, Any]]:
    if not isinstance(hevy_context, dict):
        return []
    recent = hevy_context.get("recent_workouts")
    # ``recent_workouts`` is "unknown" when Hevy is disconnected and a dict with
    # an ``error`` key when a fetch failed. Neither is a list of workouts, and
    # neither may be read as "the athlete did not lift".
    return [w for w in recent if isinstance(w, dict)] if isinstance(recent, list) else []


def _anchors_from_hevy(workouts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Best verified set per exercise, expressed in the athlete's own unit."""
    observed: dict[str, list[float]] = {}
    best: dict[str, dict[str, Any]] = {}
    for workout in workouts:
        date = str(workout.get("start_time") or "")[:10]
        for exercise in workout.get("exercises") or []:
            if not isinstance(exercise, dict):
                continue
            template_id = str(exercise.get("exercise_template_id") or "")
            if not template_id:
                continue
            for item in exercise.get("sets") or []:
                if not isinstance(item, dict):
                    continue
                weight_kg, reps = item.get("weight_kg"), item.get("reps")
                if not isinstance(weight_kg, (int, float)) or isinstance(weight_kg, bool):
                    continue
                if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
                    continue
                if weight_kg <= 0:
                    continue
                weight_lb = sw.history_weight_lb(weight_kg)
                observed.setdefault(template_id, []).append(weight_lb)
                e1rm = sw.epley_e1rm_lb(weight_lb, reps)
                current = best.get(template_id)
                if current is None or e1rm > current["e1rm_lb"]:
                    best[template_id] = {
                        "exercise_template_id": template_id,
                        "title": exercise.get("title"),
                        "weight_kg": float(weight_kg),
                        "weight_lb": weight_lb,
                        "reps": reps,
                        "date": date,
                        "e1rm_lb": round(e1rm, 1),
                    }
    for template_id, anchor in best.items():
        anchor["increment_lb"] = sw.infer_increment_lb(observed.get(template_id, []))
    return best


def _sessions_per_week(sessions: list[dict[str, Any]], today: datetime.date,
                       *, window_days: int = 28) -> float:
    cutoff = today - datetime.timedelta(days=window_days)
    recent = [s for s in sessions if (_as_date(s["date"]) or cutoff) > cutoff]
    return round(len(recent) / (window_days / 7.0), 2)


def build(*, garmin_load: Any, hevy_context: Any, today: datetime.date) -> dict[str, Any]:
    """Assemble the strength context the coach model reads.

    The returned record never claims an absence of training from a single
    source. Every date-bearing claim names the source it came from.
    """
    garmin_sessions = _garmin_strength_sessions(garmin_load)
    hevy_connected = bool(
        isinstance(hevy_context, dict)
        and isinstance(hevy_context.get("connection"), dict)
        and hevy_context["connection"].get("connected")
    )
    workouts = _hevy_workouts(hevy_context)
    anchors = _anchors_from_hevy(workouts)

    last_session = _as_date(garmin_sessions[0]["date"]) if garmin_sessions else None
    hevy_dates = [d for d in (_as_date(w.get("start_time")) for w in workouts) if d]
    last_logged = max(hevy_dates) if hevy_dates else None

    # The newest evidence of *any* lift, from whichever source saw it.
    newest = max([d for d in (last_session, last_logged) if d], default=None)

    untracked = [
        s for s in garmin_sessions
        if last_logged is not None and (_as_date(s["date"]) or last_logged) > last_logged
    ]
    # Garmin saw sessions that Hevy never captured, so the loads actually used
    # in those sessions are unknown and cannot be progressed from.
    calibration_required = bool(untracked) or last_logged is None
    anchor_age_days = (today - last_logged).days if last_logged else None
    anchors_stale = anchor_age_days is None or anchor_age_days > STALE_ANCHOR_DAYS

    per_week = _sessions_per_week(garmin_sessions, today)
    if newest is None:
        status = "unknown"
    elif per_week >= MAINTENANCE_SESSIONS_PER_WEEK:
        status = "maintained"
    elif per_week > 0:
        status = "partially_detrained"
    else:
        status = "detrained"

    summary = _summary_line(
        status=status, per_week=per_week, newest=newest, today=today,
        last_logged=last_logged, untracked_count=len(untracked),
        hevy_connected=hevy_connected,
    )

    return {
        "summary": summary,
        "training_status": status,
        "sessions_per_week_28d": per_week,
        "calibration_required": calibration_required,
        "session_evidence": {
            "provider": "Garmin",
            "holds": "session occurrence, duration and heart rate; never exercises or weights",
            "last_session_date": last_session.isoformat() if last_session else None,
            "days_since": (today - last_session).days if last_session else None,
            "recent_sessions": garmin_sessions[:10],
        },
        "weight_evidence": {
            "provider": "Hevy",
            "holds": "exercises, sets and exact weights; only for sessions logged there",
            "connected": hevy_connected,
            "last_logged_date": last_logged.isoformat() if last_logged else None,
            "anchor_age_days": anchor_age_days,
            "anchors_stale": anchors_stale,
            "unit": "lb",
            "anchors": sorted(anchors.values(), key=lambda a: -a["e1rm_lb"]),
        },
        "untracked_sessions": untracked,
        "prescription_rules": [
            "Garmin proves a session happened. It never proves which weights were used.",
            "Never report an absence of lifting from one source while the other shows sessions.",
            "When calibration_required is true, prescribe week one by reps in reserve, not by "
            "a stale weight, and ask the athlete to log it so future weights have an anchor.",
            "A working weight must be an exact Hevy anchor or a bounded derivation from one. "
            "Never state a weight the athlete has not actually lifted without labelling it a target.",
            "Report every weight in pounds. Hevy's kilogram floats are storage detail, not a unit.",
        ],
    }


def _summary_line(*, status: str, per_week: float, newest: datetime.date | None,
                  today: datetime.date, last_logged: datetime.date | None,
                  untracked_count: int, hevy_connected: bool) -> str:
    if newest is None:
        return (
            "No strength session found in either Garmin or Hevy for this window. "
            "That is an absence of evidence, not proof the athlete has not lifted."
        )
    days = (today - newest).days
    parts = [
        f"Last strength session {days} day{'' if days == 1 else 's'} ago "
        f"({newest.isoformat()}); {per_week} sessions/week over 28 days; status {status}."
    ]
    if untracked_count:
        parts.append(
            f"{untracked_count} of those session{'' if untracked_count == 1 else 's'} "
            f"happened after the last Hevy log ({last_logged.isoformat() if last_logged else 'none'}), "
            "so the weights used are unknown."
        )
    elif last_logged is None:
        parts.append(
            "Hevy holds no logged workout in this window, so no working weight is known."
            if hevy_connected else
            "Hevy is not connected, so no working weight is known."
        )
    return " ".join(parts)
