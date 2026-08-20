"""Today's suggested workout = stored plan (backbone) + readiness (modifier) + calendar (availability).

The plan is authoritative; readiness only *downregulates* it (or swaps to
recovery/rest) and the result always states why. Calendar busy-time is surfaced
so the coach and the athlete can judge whether the session fits the day.
Nothing here invents Garmin numbers — it reads what garmin_source returns and
flags missing data.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import db, garmin_source, config

# NOTE: Calendar/schedule-awareness is intentionally disabled for now
# (calendar_source is dormant). Re-add by restoring the availability block below
# and the calendar wiring in coach.py / main.py.


def _readiness_signal(readiness: dict[str, Any]) -> dict[str, Any]:
    """Reduce the readiness payload to a single modifier decision."""
    if not isinstance(readiness, dict) or readiness.get("error"):
        return {"level": "unknown", "reason": "readiness data unavailable", "downregulate": False}

    tr = (readiness.get("training_readiness") or {})
    freshness = tr.get("freshness") if isinstance(tr, dict) else None
    readiness_unverified = (isinstance(freshness, dict)
                            and freshness.get("is_current") is not True)
    score = None if readiness_unverified else tr.get("score")
    hrv = (readiness.get("hrv") or {})
    hrv_status = (hrv.get("status") or "").upper()
    sleep = (readiness.get("sleep") or {})
    hours = sleep.get("hours")

    bad, soft = [], []
    if isinstance(score, (int, float)):
        if score < 35:
            bad.append(f"training readiness {score}")
        elif score < 55:
            soft.append(f"moderate readiness {score}")
    if hrv_status in ("UNBALANCED", "LOW", "POOR"):
        bad.append(f"HRV {hrv_status.lower()}")
    if isinstance(hours, (int, float)):
        if hours < 5.5:
            bad.append(f"only {hours} h sleep")
        elif hours < 6.5:
            soft.append(f"{hours} h sleep")

    if bad:
        return {"level": "poor", "reason": "; ".join(bad), "downregulate": True, "hard": True}
    if soft:
        return {"level": "moderate", "reason": "; ".join(soft), "downregulate": True, "hard": False}
    if readiness_unverified:
        source_date = freshness.get("source_date")
        reason = (f"training readiness is stale (as of {source_date})" if source_date
                  else "training readiness freshness is unknown")
        return {"level": "unknown", "reason": reason, "downregulate": False}
    return {"level": "good", "reason": "recovery markers in range", "downregulate": False}


def _todays_completion(today: str, recent_load: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Report today's training state for the workout card.

    A workout is only *verified* complete when a real Garmin activity for today
    exists — a logged "Done" tap alone does NOT count (the athlete can tap it by
    mistake, or before actually training). If feedback was logged but no activity
    has synced, we return `verified=False` so the UI shows an unconfirmed state
    and asks the athlete to double-check rather than claiming it's done."""
    acts: list[dict[str, Any]] = []
    try:
        load = recent_load if recent_load is not None else garmin_source.get_recent_load(3)
        acts = [a for a in (load.get("activities") or []) if (a.get("date") or "") == today]
    except Exception:
        acts = []

    logged = None
    try:
        comps = db.get_completions(today, today)
        logged = next((c for c in comps if (c.get("status") or "") in ("done", "partial")), None)
    except Exception:
        logged = None

    if not acts and not logged:
        return None

    summary = [{"sport": a.get("sport"), "name": a.get("name"), "km": a.get("km"),
                "minutes": a.get("minutes"), "hr_avg": a.get("hr_avg")} for a in acts]
    return {
        "verified": bool(acts),                         # true only w/ a real Garmin activity
        "logged_only": bool(logged and not acts),        # tapped Done but nothing synced → possible mistake
        "status": (logged or {}).get("status") or "done",
        "activities": summary,
        "totals": {
            "sessions": len(acts),
            "minutes": round(sum(a.get("minutes") or 0 for a in acts)),
            "km": round(sum(a.get("km") or 0 for a in acts), 1),
        },
        "note": (logged or {}).get("notes") or "",
        "rpe": (logged or {}).get("rpe"),
    }


def todays_suggestion(readiness_data: dict[str, Any] | None = None,
                      recent_load: dict[str, Any] | None = None) -> dict[str, Any]:
    today = config.local_today().isoformat()
    plan_day = db.get_plan_day(today)

    # Pull live inputs (best effort).
    if readiness_data is not None:
        readiness = readiness_data
    else:
        try:
            readiness = garmin_source.get_readiness()
        except Exception as e:
            readiness = {"error": f"{type(e).__name__}: {e}"}

    signal = _readiness_signal(readiness)

    if not plan_day:
        return {
            "date": today,
            "has_plan": False,
            "message": "No plan entry for today. Seed the plan (POST /api/plan/seed).",
            "readiness_signal": signal,
            "completed": _todays_completion(today, recent_load),
        }

    base = {
        "discipline": plan_day["discipline"],
        "title": plan_day["title"],
        "structure": plan_day["structure"],
        "duration_min": plan_day["duration_min"],
        "intensity": plan_day["intensity"],
        "tsb_target": plan_day.get("tsb_target"),
        "why": plan_day["why"],
        "is_rest": plan_day["is_rest"],
    }

    # The plan is authoritative for what today's session IS — readiness never
    # silently rewrites it. `suggestion` always mirrors the plan (so the Today's
    # Workout card matches the weekly plan + calendar). When readiness is low we
    # compute an `adjusted` easier version and surface it as ADVICE (a warning +
    # the push-to-watch confirm dialog), but the athlete decides.
    suggestion = dict(base)
    adjusted: dict[str, Any] | None = None
    notes: list[str] = []

    if plan_day["is_rest"]:
        notes.append("Planned rest — keep it; recovery is the work today.")
    elif signal["downregulate"] and signal.get("hard"):
        adjusted = dict(base)
        adjusted.update({
            "discipline": "recovery",
            "title": "Recovery / easy aerobic (downregulated)",
            "intensity": "Z1",
            "duration_min": min(40, base["duration_min"] or 40),
            "structure": {"warmup": "—",
                          "main": "30–40 min very easy Z1, or full rest if you feel flat",
                          "cooldown": "—"},
            "why": f"Readiness is poor ({signal['reason']}). Pushing the planned "
                   f"'{base['title']}' today risks digging a hole. Cost: lose that planned "
                   "quality stimulus. Buy: recovery margin for the next executable hard day.",
            "tradeoff": {
                "cost": f"lose the planned {base['title']} stimulus",
                "benefit": "recovery margin for the next executable hard day",
            },
        })
        notes.append(f"Readiness is poor ({signal['reason']}) — consider easing to recovery. "
                     "Your call; the plan stands unless you change it.")
    elif signal["downregulate"]:
        cut = int((base["duration_min"] or 60) * 0.7)
        adjusted = dict(base)
        adjusted["duration_min"] = cut
        adjusted["why"] = (f"{base['why']} Readiness is moderate ({signal['reason']}), "
                           f"so an option is to trim volume to ~{cut} min. Cost: "
                           f"{(base['duration_min'] or 60) - cut} min less stimulus. Buy: lower "
                           "fatigue while preserving the session's intensity.")
        adjusted["tradeoff"] = {
            "cost": f"{(base['duration_min'] or 60) - cut} min less training stimulus",
            "benefit": "lower fatigue while preserving the session's intensity",
        }
        notes.append(f"Readiness is moderate ({signal['reason']}) — trimming ~30% volume is an "
                     "option, but the planned session stands unless you change it.")
    else:
        notes.append("Readiness is green — execute the plan as written.")

    # --- Logged constraints for today (athlete-entered) ---
    constraints = [c["text"] for c in db.get_constraints(today)]
    if constraints:
        notes.append("Logged constraints today: " + " | ".join(constraints))

    return {
        "date": today,
        "phase": plan_day["phase"],
        "has_plan": True,
        "from_plan": base,
        "suggestion": suggestion,     # == the planned session (advice never swaps it)
        "adjusted": adjusted,          # readiness-eased alternative, or None
        "readiness_signal": signal,
        "today_constraints": constraints,
        "notes": notes,
        "completed": _todays_completion(today, recent_load),
        "data_flags": (readiness.get("missing") if isinstance(readiness, dict) else None),
    }
