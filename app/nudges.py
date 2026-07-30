"""Proactive nudges — the coach reaches out instead of waiting to be opened.

Deterministic (no LLM, so they're instant and cheap): reads today's + tomorrow's
plan, readiness, recent sleep, what's been done + eaten, and the time of day, then
returns a ranked list of short, actionable pings — e.g. "Big brick tomorrow — carb
up tonight." These render in-app and (later) get sent as phone pushes.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import config, db, garmin_source


def _is_big(day: dict[str, Any]) -> tuple[bool, str]:
    if not day or day.get("is_rest"):
        return False, ""
    disc = (day.get("discipline") or "").lower()
    dur = day.get("duration_min") or 0
    txt = f"{day.get('intensity','')} {day.get('title','')}".lower()
    hard = any(k in txt for k in ("threshold", "vo2", "race", "tempo", "interval", "css"))
    if disc == "brick" or dur >= 120:
        return True, "long"
    if hard and dur >= 60:
        return True, "quality"
    return False, ""


def _matched_today(today: str) -> set[str]:
    """Sports the athlete has actually done today (Garmin + manual)."""
    try:
        load = garmin_source.get_recent_load(2)
        return {a.get("sport") for a in (load.get("activities") or []) if (a.get("date") or "") == today}
    except Exception:
        return set()


def get_nudges() -> dict[str, Any]:
    now = config.local_now()
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    hour = now.hour
    evening = (hour, now.minute) >= (17, 0)
    # Quiet hours: morning nudges only from 6am so frequent polling never pings pre-dawn.
    morning = 6 <= hour < 11

    today_plan = db.get_plan_day(today.isoformat()) or {}
    tmr_plan = db.get_plan_day(tomorrow.isoformat()) or {}
    nudges: list[dict[str, Any]] = []

    def add(kind, icon, title, detail, priority):
        nudges.append({"id": kind, "kind": kind, "icon": icon, "title": title,
                       "detail": detail, "priority": priority})

    # readiness / recovery (best-effort). Only pull it in the windows where a
    # readiness-based nudge can actually fire — so frequent polling doesn't hammer
    # Garmin's API on every check (afternoon "carb-up" checks need zero Garmin calls).
    readiness = {}
    if morning or evening:
        try:
            readiness = garmin_source.get_readiness() or {}
        except Exception:
            readiness = {}
    rec = (readiness.get("training_readiness") or {}).get("score")
    hrv_status = ((readiness.get("hrv") or {}).get("status") or "").upper()
    sleep_h = (readiness.get("sleep") or {}).get("hours")

    # 1) Big session TOMORROW → fuel tonight (the flagship nudge)
    big, kind = _is_big(tmr_plan)
    if big and (evening or hour >= 12):
        try:
            from . import nutrition
            tgt = nutrition.daily_targets({"discipline": tmr_plan.get("discipline"),
                                           "duration_min": tmr_plan.get("duration_min"),
                                           "intensity": tmr_plan.get("intensity"),
                                           "title": tmr_plan.get("title"),
                                           "is_rest": tmr_plan.get("is_rest")})
            lo, hi = tgt["carb_range"]
            carbs = f" Aim ~{lo}–{hi} g carbs across today into tomorrow."
        except Exception:
            carbs = ""
        title = f"Big {tmr_plan.get('discipline','session')} tomorrow — carb up tonight"
        add("fuel_tomorrow", "🍚", title,
            f"{tmr_plan.get('title','')} ({tmr_plan.get('duration_min','?')} min).{carbs} "
            "Don't go into it depleted.", "high")

    # 2) Low recovery this morning → hold intensity
    if morning and isinstance(rec, (int, float)) and rec < 40:
        add("low_recovery", "🔋", "Recovery's low — keep today honest",
            f"You woke at {round(rec)}. Favor easy/aerobic and protect the week; "
            "don't force intensity on a bad recovery day.", "high")
    elif morning and hrv_status in ("UNBALANCED", "LOW", "POOR"):
        add("hrv_low", "〰", "HRV suppressed this morning",
            "Autonomic system is stressed — bias easy today unless it rebounds.", "med")

    # 3) Sleep debt → protect tonight
    if isinstance(sleep_h, (int, float)) and sleep_h < 6 and (evening or morning):
        add("sleep_debt", "😴", "Protect your sleep tonight",
            f"Last night was ~{sleep_h} h. Recovery capacity is compromised — "
            "get to bed early, especially before a build/quality day.", "med")

    # 4) Planned session still not logged by evening
    b, _ = _is_big(today_plan)
    if evening and today_plan and not today_plan.get("is_rest"):
        done_sports = _matched_today(today.isoformat())
        disc = (today_plan.get("discipline") or "").lower()
        want = {"brick": {"bike", "run"}, "recovery": {"bike", "run", "swim"}}.get(disc, {disc})
        if not (done_sports & want):
            add("unlogged", "⏳", f"Today's {disc} isn't logged yet",
                f"{today_plan.get('title','')} is still open. Did you do it, or should "
                "Coach Steve reshape the rest of the week?", "med")

    # 5) Nightly review is ready
    if (hour, now.minute) >= (20, 30):
        add("nightly", "🌙", "Your nightly review is ready",
            "Open Coach Steve for tonight's recap and tomorrow's priority.", "low")

    order = {"high": 0, "med": 1, "low": 2}
    nudges.sort(key=lambda n: order.get(n["priority"], 3))
    return {"date": today.isoformat(), "local_time": now.strftime("%H:%M"), "nudges": nudges}
