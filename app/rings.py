"""The four rings: Sleep, Training Readiness, Day Strain, T100 Readiness.

Sleep and Readiness come straight from Garmin (never fabricated; null when
missing). Strain and T100 are computed here — transparently, with their
sub-components returned so the UI can show how the number was built.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import config, garmin_source


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
    """0–100 race preparedness vs T100 demands (2k swim / 80k bike / 18k run).

    Volume ratios use 14-day targets consistent with the plan's weekly shape;
    swim is weighted heaviest because it's the athlete's weakest leg.
    """
    by = (load14 or {}).get("by_sport") or {}
    swim_km = (by.get("swim") or {}).get("km") or 0
    bike_km = (by.get("bike") or {}).get("km") or 0
    run_km = (by.get("run") or {}).get("km") or 0

    targets = {"swim": 8.0, "bike": 170.0, "run": 42.0}   # km / 14 days
    swim_c = min(1.0, swim_km / targets["swim"])
    bike_c = min(1.0, bike_km / targets["bike"])
    run_c = min(1.0, run_km / targets["run"])

    acwr = (training_load or {}).get("load_ratio")
    acwr_c = 1.0 if (isinstance(acwr, (int, float)) and 0.8 <= acwr <= 1.3) else \
             0.7 if isinstance(acwr, (int, float)) else 0.5

    ready_c = min(1.0, (readiness_score or 50) / 100)

    score = round(100 * (0.34 * swim_c + 0.22 * bike_c + 0.18 * run_c +
                         0.16 * acwr_c + 0.10 * ready_c))
    label = ("Race ready" if score >= 85 else "On track" if score >= 65 else
             "Behind" if score >= 40 else "Way behind")
    weakest = min((("swim", swim_c), ("bike", bike_c), ("run", run_c)), key=lambda x: x[1])
    return {
        "score": score, "label": label, "days_left": days_left,
        "components": {
            "swim": {"pct": round(swim_c * 100), "km_14d": swim_km, "target": targets["swim"]},
            "bike": {"pct": round(bike_c * 100), "km_14d": bike_km, "target": targets["bike"]},
            "run": {"pct": round(run_c * 100), "km_14d": run_km, "target": targets["run"]},
            "load_balance": {"pct": round(acwr_c * 100), "acwr": acwr},
        },
        "weakest_leg": weakest[0],
    }


def get_rings() -> dict[str, Any]:
    c = garmin_source.get_client()
    today = config.local_today().isoformat()

    readiness = _safe(garmin_source.get_readiness) or {}
    tl = _safe(garmin_source.get_training_load) or {}
    load14 = _safe(lambda: garmin_source.get_recent_load(14)) or {}
    race = config.race_phase()

    sleep_score = ((readiness.get("sleep") or {}).get("score"))
    tr = readiness.get("training_readiness") or {}       # morning recovery (stable)
    cur = readiness.get("current_readiness") or {}        # live value (post-exercise)
    tr_score = tr.get("score")
    strain = day_strain(c, today, tl.get("chronic_load"))
    t100 = t100_readiness(load14, tl, tr_score, race.get("days_remaining", 0))

    return {
        "date": today,
        "sleep": {"score": sleep_score, "label": ((readiness.get("sleep") or {}).get("hours") and
                  f"{readiness['sleep']['hours']} h") or "—"},
        # "Recovery" = morning post-sleep readiness; current live readiness shown underneath.
        "readiness": {"score": tr_score, "label": (tr.get("level") or "—").title(),
                      "current": cur.get("score"),
                      "current_label": (cur.get("level") or "").title(),
                      "current_post_exercise": bool(cur.get("is_post_exercise"))},
        "strain": strain,
        "t100": t100,
    }
