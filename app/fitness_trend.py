"""Fitness / Fatigue / Form — the Performance Management Chart (Intervals.icu style).

Built from Garmin per-activity training load (an EPOC-based stress score),
summed per day, then run through the classic exponentially-weighted model:

  CTL (Fitness) — 42-day EWMA of daily load; slow-moving, what you've absorbed.
  ATL (Fatigue) — 7-day EWMA; fast-moving, recent stress.
  TSB (Form)    — yesterday's CTL minus yesterday's ATL; freshness / readiness
                  to perform. Positive = fresh/tapered, deeply negative = buried.

Everything is derived from real logged activities. Days with no training count
as 0 load (rest still moves the curves), which is correct for the model.
"""
from __future__ import annotations

import datetime
import math
from typing import Any

from . import config, garmin_source

CTL_TC = 42   # days
ATL_TC = 7
_CTL_K = 1 - math.exp(-1 / CTL_TC)
_ATL_K = 1 - math.exp(-1 / ATL_TC)

# Pull well beyond the chart window so CTL/ATL are warmed up by the time the
# visible series starts.
_HISTORY_DAYS = 120


def _daily_load(days: int) -> dict[str, float]:
    """Map ISO date -> summed activity training load over the window."""
    c = garmin_source.get_client()
    raw = garmin_source._safe(lambda: c.get_activities(0, 300)) or []
    cutoff = (config.local_today() - datetime.timedelta(days=days)).isoformat()
    out: dict[str, float] = {}
    for a in raw:
        if not isinstance(a, dict):
            continue
        d = (a.get("startTimeLocal") or "")[:10]
        if not d or d < cutoff:
            continue
        load = a.get("activityTrainingLoad")
        if isinstance(load, (int, float)) and load > 0:
            out[d] = out.get(d, 0.0) + float(load)
    return out


def _interpret(tsb: float, days_left: int | None) -> dict[str, str]:
    """Plain-language read of current form, race-aware near the taper."""
    if tsb <= -30:
        band, note = "deep fatigue", "You're buried — this is only sustainable briefly. Recover soon."
    elif tsb <= -10:
        band, note = "productive fatigue", "Solid training stress. Fine mid-block; don't live here forever."
    elif tsb < 5:
        band, note = "neutral", "Balanced — building without digging a hole."
    elif tsb < 20:
        band, note = "fresh", "Freshening up. Great for racing, watch for detraining if it's not race week."
    else:
        band, note = "very fresh", "Well tapered — or under-training. Right on race week, too much otherwise."
    if isinstance(days_left, int) and 0 <= days_left <= 14:
        note = ("Race in %d days — you want Form climbing toward roughly +5 to +15 on the day. " % days_left) + note
    return {"band": band, "note": note}


def get_pmc(days: int = 90) -> dict[str, Any]:
    """CTL/ATL/TSB series for the last `days`, plus today's values + read."""
    loads = _daily_load(_HISTORY_DAYS)
    if not loads:
        return {"error": "no activity training-load data available yet"}

    start = config.local_today() - datetime.timedelta(days=_HISTORY_DAYS)
    today = config.local_today()
    ctl = atl = 0.0
    series: list[dict[str, Any]] = []
    visible_from = today - datetime.timedelta(days=days)

    d = start
    while d <= today:
        load = loads.get(d.isoformat(), 0.0)
        tsb = ctl - atl                       # form = yesterday's fitness - fatigue
        ctl = ctl + _CTL_K * (load - ctl)
        atl = atl + _ATL_K * (load - atl)
        if d >= visible_from:
            series.append({"date": d.isoformat(), "ctl": round(ctl, 1),
                           "atl": round(atl, 1), "tsb": round(tsb, 1),
                           "load": round(load)})
        d += datetime.timedelta(days=1)

    cur = series[-1]
    # 7-day fitness ramp (CTL change/week) — are you building or bleeding fitness?
    ramp = None
    if len(series) > 7:
        ramp = round(cur["ctl"] - series[-8]["ctl"], 1)

    race = config.race_phase()
    return {
        "days": days,
        "series": series,
        "current": {"ctl": cur["ctl"], "atl": cur["atl"], "tsb": cur["tsb"], "ramp_7d": ramp},
        "interpretation": _interpret(cur["tsb"], race.get("days_remaining")),
        "labels": {"ctl": "Fitness", "atl": "Fatigue", "tsb": "Form"},
    }
