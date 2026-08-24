"""Sleep, recovery, day strain, and readiness for the active goal.

Sleep and recovery come straight from Garmin (never fabricated; null when
missing). Goal readiness always uses the same event-aware model. A profile
switch changes the event inputs, never which hidden scoring system runs.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import config
from . import event_readiness as _event_readiness, garmin_source


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def day_strain(client: Any, today: str, chronic_load: float | None) -> dict[str, Any]:
    """0–100 strain from today's training load (vs chronic daily load),
    intensity minutes, steps, and active calories."""
    stats = _safe(lambda: client.get_stats(today)) or {}
    acts = _safe(lambda: client.get_activities(0, 30)) or []
    today_load = sum((a.get("activityTrainingLoad") or 0) for a in acts
                     if (a.get("startTimeLocal") or "")[:10] == today)
    n_acts = sum(1 for a in acts if (a.get("startTimeLocal") or "")[:10] == today
                 and (a.get("activityTrainingLoad") or 0) > 0)

    vig = stats.get("vigorousIntensityMinutes") or 0
    mod = stats.get("moderateIntensityMinutes") or 0
    steps = stats.get("totalSteps") or 0
    step_goal = stats.get("dailyStepGoal") or 10000
    active_kcal = stats.get("activeKilocalories") or 0
    floors = stats.get("floorsAscended") or 0

    # components (each 0..1)
    chronic = max(chronic_load or 450, 100)
    load_c = min(1.0, (today_load / chronic) / 1.4)      # 1.4× chronic daily = maxed
    im_c = min(1.0, (vig * 2 + mod) / 90)
    steps_c = min(1.0, steps / max(step_goal, 1))
    kcal_c = min(1.0, active_kcal / 700)

    strain = round(100 * (0.55 * load_c + 0.20 * im_c + 0.10 * steps_c + 0.15 * kcal_c))
    label = ("All out" if strain >= 85 else "Hard" if strain >= 65 else
             "Moderate" if strain >= 40 else "Light" if strain >= 15 else "Resting")
    return {
        "score": strain, "label": label,
        "inputs": {"training_load_today": round(today_load), "activities": n_acts,
                   "intensity_min": vig * 2 + mod, "steps": steps,
                   "active_kcal": round(active_kcal), "floors": round(floors, 1)},
    }


def t100_readiness(load14: dict[str, Any], training_load: dict[str, Any],
                   readiness_score: float | None, days_left: int) -> dict[str, Any]:
    """Legacy API alias for older callers.

    It deliberately does not select a T100 formula. Even an older client gets
    readiness for the currently persisted event profile.
    """
    long_load = _safe(lambda: garmin_source.get_recent_load(
        _event_readiness.LONG_RUN_WINDOW_DAYS)) or {}
    return _event_readiness.readiness(
        load14=load14, training_load=training_load,
        readiness_score=readiness_score, days_left=days_left,
        profile=config.active_event_profile(),
        activities=long_load.get("activities"),
    )


def get_rings() -> dict[str, Any]:
    c = garmin_source.get_client()
    today = config.local_today().isoformat()

    readiness = _safe(garmin_source.get_readiness) or {}
    tl = _safe(garmin_source.get_training_load) or {}
    race = config.race_phase()
    load14 = _safe(lambda: garmin_source.get_recent_load(14)) or {}
    readiness, tl = garmin_source.reconcile_freshness(readiness, tl, load14)
    decision_tl = garmin_source.current_training_load(tl)

    sleep_score = ((readiness.get("sleep") or {}).get("score"))
    tr = readiness.get("training_readiness") or {}       # morning recovery (stable)
    cur = readiness.get("current_readiness") or {}        # latest Garmin intraday snapshot
    tr_score = (tr.get("score") if (tr.get("freshness") or {}).get("is_current") is True else None)
    cur_is_current = (cur.get("freshness") or {}).get("is_current") is True
    strain = day_strain(c, today, decision_tl.get("chronic_load"))
    payload = {
        "date": today,
        "sleep": {"score": sleep_score, "label": ((readiness.get("sleep") or {}).get("hours") and
                  f"{readiness['sleep']['hours']} h") or "—"},
        # "Recovery" = morning post-sleep readiness; a verified-current intraday
        # snapshot may be shown underneath without replacing the morning score.
        "readiness": {"score": tr_score, "label": (tr.get("level") or "—").title(),
                      "freshness": tr.get("freshness"),
                      "current": cur.get("score") if cur_is_current else None,
                      "current_last_known": cur.get("score"),
                      "current_label": (cur.get("level") or "").title(),
                      "current_post_exercise": bool(cur.get("is_post_exercise") and cur_is_current),
                      "current_freshness": cur.get("freshness")},
        "strain": strain,
    }
    # One readiness ring follows the persisted active event. The long run
    # decides running-distance preparedness, so search farther back than 14 d.
    days_left = race.get("days_remaining", 0)
    profile = config.active_event_profile()
    long_load = _safe(lambda: garmin_source.get_recent_load(
        _event_readiness.LONG_RUN_WINDOW_DAYS)) or {}
    payload["event_ready"] = _event_readiness.readiness(
        load14=load14, training_load=decision_tl, readiness_score=tr_score,
        days_left=days_left, profile=profile,
        activities=long_load.get("activities"),
    )
    return payload
