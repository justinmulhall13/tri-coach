"""WHOOP-style click-through breakdowns for the four dashboard rings.

Each ring (recovery, sleep, strain, t100) returns a common payload the frontend
renders identically:

  {score, unit, label, color, contributors[], stages[], charts[], insight}

- `contributors` — WHOOP "contributor" rows: a value, the athlete's own baseline,
  a delta + trend arrow, and/or a Garmin qualifier (POOR/FAIR/GOOD/EXCELLENT).
- `stages`     — a stacked bar (sleep stages, or time-in-HR-zones for strain).
- `charts`     — small trend charts (bars/line) over the last week or two.

Everything is real Garmin data or transparently derived from it. Missing values
are passed through as null — never fabricated.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import config, db, fitness_trend, garmin_source, rings

_safe = garmin_source._safe

# Garmin sleep qualifiers → a 0..1 "goodness" for coloring/bar fill.
_QUAL = {"POOR": 0.25, "FAIR": 0.5, "GOOD": 0.8, "EXCELLENT": 1.0}


def _trend(delta: float | None) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "flat"
    return "up" if delta > 0 else "down"


def _contrib(label: str, icon: str, value: Any, *, baseline: float | None = None,
             higher_better: bool = True, unit: str = "", qualifier: str | None = None,
             pct: float | None = None, fmt: str | None = None) -> dict[str, Any]:
    """One contributor row. `delta`/`good` computed vs baseline when both present."""
    delta = None
    good = None
    if isinstance(value, (int, float)) and isinstance(baseline, (int, float)):
        delta = round(value - baseline, 1)
        good = (delta >= 0) if higher_better else (delta <= 0)
    return {
        "label": label, "icon": icon,
        "value": value, "unit": unit, "fmt": fmt,
        "baseline": baseline, "delta": delta,
        "trend": _trend(delta), "good": good,
        "qualifier": qualifier,
        "pct": pct if pct is not None else (_QUAL.get(qualifier or "") and _QUAL[qualifier] * 100),
    }


def _col(rows: list[dict], col: str) -> list[tuple[str, float]]:
    return [(r["date"], r[col]) for r in rows
            if isinstance(r.get(col), (int, float)) and r[col] is not None]


def _baseline_of(rows: list[dict], col: str, today: str) -> float | None:
    past = [v for d, v in _col(rows, col) if d != today]
    return round(sum(past) / len(past), 1) if len(past) >= 5 else None


def _series(rows: list[dict], col: str, days: int = 7) -> list[dict[str, Any]]:
    """Last `days` daily points for a wellness column (for a trend chart)."""
    vals = dict(_col(rows, col))
    today = config.local_today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i))
        iso = d.isoformat()
        v = vals.get(iso)
        out.append({"label": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()],
                    "date": iso, "value": v})
    return out


def _recovery_color(score: float | None) -> str:
    if not isinstance(score, (int, float)):
        return "#8aa0b6"
    return "#34d399" if score >= 66 else "#fbbf24" if score >= 34 else "#f87171"


# --- Recovery / Readiness -----------------------------------------------------
def _recovery() -> dict[str, Any]:
    rd = _safe(garmin_source.get_readiness) or {}
    rows = db.get_wellness(60)
    today = config.local_today().isoformat()

    tr = (rd.get("training_readiness") or {})      # morning recovery (stable)
    cur = (rd.get("current_readiness") or {})       # live, post-exercise
    score = tr.get("score")
    hrv = (rd.get("hrv") or {})
    sleep = (rd.get("sleep") or {})
    bb = (rd.get("body_battery") or {})
    stress = (rd.get("stress") or {})
    resp = _safe(lambda: garmin_source.get_client().get_respiration_data(today)) or {}

    contributors = [
        _contrib("Heart rate variability", "〰", hrv.get("last_night_ms"),
                 baseline=hrv.get("weekly_ms") or _baseline_of(rows, "hrv_ms", today),
                 higher_better=True, unit="ms"),
        _contrib("Resting heart rate", "♥", rd.get("resting_hr_bpm"),
                 baseline=_baseline_of(rows, "rhr_bpm", today), higher_better=False, unit="bpm"),
        _contrib("Respiratory rate", "🫁", resp.get("avgSleepRespirationValue"),
                 higher_better=False, unit="rpm"),
        _contrib("Sleep score", "🌙", sleep.get("score"),
                 baseline=_baseline_of(rows, "sleep_score", today), higher_better=True, unit="/100"),
        _contrib("Body battery", "🔋", bb.get("current"),
                 baseline=_baseline_of(rows, "body_battery", today), higher_better=True, unit="/100"),
        _contrib("Stress (avg)", "⚡", stress.get("avg"),
                 baseline=_baseline_of(rows, "stress", today), higher_better=False, unit="/100"),
    ]

    ready_series = _series(rows, "readiness_score", 7)
    for p in ready_series:
        p["color"] = _recovery_color(p["value"])

    charts = [
        {"kind": "bars", "title": "Readiness — last 7 days", "unit": "/100", "max": 100, "points": ready_series},
        {"kind": "line", "title": "HRV — last 7 days", "unit": "ms", "points": _series(rows, "hrv_ms", 7)},
        {"kind": "line", "title": "Resting HR — last 7 days", "unit": "bpm", "points": _series(rows, "rhr_bpm", 7)},
    ]

    # Show the live post-exercise readiness explicitly so there's no confusion
    # between "how recovered you woke up" (the ring) and "right now".
    cur_score = cur.get("score")
    if isinstance(cur_score, (int, float)) and cur_score != score:
        contributors.insert(0, _contrib(
            "Current readiness (now)", "⏱", cur_score, baseline=score, higher_better=True, unit="/100",
            fmt=f"morning recovery was {score}"
                + (" · lower now is normal post-workout" if cur.get("is_post_exercise") else "")))

    label = (tr.get("level") or "—").title()
    if isinstance(cur_score, (int, float)) and cur_score != score:
        label = f"Woke {label} ({score}) · now {cur_score}"
    return {
        "name": "recovery", "title": "Recovery",
        "score": score, "unit": "/100", "color": _recovery_color(score),
        "label": label,
        "contributors": contributors, "stages": [], "charts": charts,
        "insight": _recovery_insight(rd, rows, today),
        "missing": rd.get("missing"),
    }


def _recovery_insight(rd, rows, today) -> str | None:
    hrv = (rd.get("hrv") or {})
    base = _baseline_of(rows, "hrv_ms", today)
    v = hrv.get("last_night_ms")
    if isinstance(v, (int, float)) and isinstance(base, (int, float)) and base:
        pct = round((v - base) / base * 100)
        if pct >= 8:
            return f"HRV is {pct}% above your recent norm — your nervous system is well recovered and ready to absorb hard work."
        if pct <= -8:
            return f"HRV is {abs(pct)}% below your norm — a sign of accumulated fatigue or stress. Favor easy work today."
    status = (hrv.get("status") or "").title()
    return f"HRV status: {status}." if status and status != "None" else None


# --- Sleep --------------------------------------------------------------------
def _sleep() -> dict[str, Any]:
    today = config.local_today().isoformat()
    raw = _safe(lambda: garmin_source.get_client().get_sleep_data(today)) or {}
    dto = (raw.get("dailySleepDTO") or {}) if isinstance(raw, dict) else {}
    scores = dto.get("sleepScores") or {}
    rows = db.get_wellness(60)

    def q(key):  # qualifier for a subscore
        s = scores.get(key) or {}
        return s.get("qualifierKey") if isinstance(s, dict) else None

    def qval(key):
        s = scores.get(key) or {}
        return s.get("value") if isinstance(s, dict) else None

    overall = (scores.get("overall") or {}).get("value") if isinstance(scores.get("overall"), dict) else None
    need = dto.get("sleepNeed") or {}
    actual_min = need.get("actual")
    baseline_min = need.get("baseline")
    hours_vs_need = round(actual_min / baseline_min * 100) if actual_min and baseline_min else None

    total = dto.get("sleepTimeSeconds") or 0
    deep = dto.get("deepSleepSeconds") or 0
    rem = dto.get("remSleepSeconds") or 0
    light = dto.get("lightSleepSeconds") or 0
    awake = dto.get("awakeSleepSeconds") or 0

    def hrs(s):
        return round(s / 3600, 2) if s else 0

    contributors = [
        _contrib("Hours vs. needed", "🌙",
                 hours_vs_need, higher_better=True, unit="%",
                 pct=min(100, hours_vs_need) if hours_vs_need else None,
                 fmt=(f"{hrs(total)} h slept · {round(baseline_min/60,1)} h needed" if baseline_min else None)),
        _contrib("Deep sleep", "🟦", qval("deepPercentage"), higher_better=True, unit="%",
                 qualifier=q("deepPercentage")),
        _contrib("REM sleep", "🟪", qval("remPercentage"), higher_better=True, unit="%",
                 qualifier=q("remPercentage")),
        _contrib("Light sleep", "🟦", qval("lightPercentage"), higher_better=True, unit="%",
                 qualifier=q("lightPercentage")),
        _contrib("Restfulness", "🛏", None, qualifier=q("restlessness"), pct=(_QUAL.get(q("restlessness") or "") or 0) * 100),
        _contrib("Sleep stress", "⚡", dto.get("avgSleepStress"), higher_better=False, unit="",
                 qualifier=q("stress")),
    ]

    stages = [
        {"label": "Deep", "hours": hrs(deep), "color": "#3b82f6"},
        {"label": "REM", "hours": hrs(rem), "color": "#a855f7"},
        {"label": "Light", "hours": hrs(light), "color": "#60a5fa"},
        {"label": "Awake", "hours": hrs(awake), "color": "#6b7280"},
    ]

    hours_series = _series(rows, "sleep_h", 7)
    score_series = _series(rows, "sleep_score", 7)
    charts = [
        {"kind": "bars", "title": "Hours of sleep — last 7 days", "unit": "h", "points": hours_series},
        {"kind": "bars", "title": "Sleep score — last 7 days", "unit": "/100", "max": 100, "points": score_series},
    ]

    resp = _safe(lambda: garmin_source.get_client().get_respiration_data(today)) or {}
    return {
        "name": "sleep", "title": "Sleep",
        "score": overall, "unit": "/100", "color": "#7ca8e6",
        "label": f"{hrs(total)} h",
        "contributors": contributors, "stages": stages, "charts": charts,
        "insight": _sleep_insight(need, overall),
        "respiratory_rate": resp.get("avgSleepRespirationValue"),
    }


def _sleep_insight(need, overall) -> str | None:
    fb = (need or {}).get("feedback")
    actual, base = (need or {}).get("actual"), (need or {}).get("baseline")
    if actual and base:
        gap = base - actual
        if gap >= 45:
            return f"You came up ~{round(gap/60,1)} h short of your sleep need — expect that to weigh on recovery and readiness today."
        if gap <= 5:
            return "You met your sleep need — a strong base for today's training and recovery."
    return f"Sleep-need trend: {fb.title()}." if fb else None


# --- Day strain ---------------------------------------------------------------
def _strain() -> dict[str, Any]:
    c = garmin_source.get_client()
    today = config.local_today().isoformat()
    tl = _safe(garmin_source.get_training_load) or {}
    strain = rings.day_strain(c, today, tl.get("chronic_load"))
    inp = strain.get("inputs", {})

    chronic = max(tl.get("chronic_load") or 450, 100)
    contributors = [
        _contrib("Training load today", "📈", inp.get("training_load_today"), unit="",
                 pct=min(100, (inp.get("training_load_today", 0) / (chronic * 1.4)) * 100) if chronic else None,
                 fmt=f"vs ~{round(chronic)}/day chronic"),
        _contrib("Intensity minutes", "🔥", inp.get("intensity_min"), unit="min",
                 pct=min(100, (inp.get("intensity_min", 0) / 90) * 100)),
        _contrib("Steps", "👟", inp.get("steps"), unit="",
                 pct=min(100, (inp.get("steps", 0) / 10000) * 100), fmt="goal 10,000"),
        _contrib("Active calories", "⚡", inp.get("active_kcal"), unit="kcal",
                 pct=min(100, (inp.get("active_kcal", 0) / 700) * 100)),
        _contrib("Floors climbed", "🪜", inp.get("floors"), unit=""),
    ]

    # Time in HR zones — aggregate today's activities.
    zones = _todays_zones(c, today)
    stages = zones

    # 14-day daily training load bars.
    load14 = _safe(lambda: garmin_source.get_recent_load(14)) or {}
    by_day: dict[str, float] = {}
    for a in (load14.get("activities") or []):
        by_day[a.get("date")] = by_day.get(a.get("date"), 0) + (a.get("load") or 0)
    tdate = config.local_today()
    load_points = []
    for i in range(13, -1, -1):
        d = tdate - datetime.timedelta(days=i)
        load_points.append({"label": ["M", "T", "W", "T", "F", "S", "S"][d.weekday()],
                            "date": d.isoformat(), "value": round(by_day.get(d.isoformat(), 0))})
    charts = [{"kind": "bars", "title": "Training load — last 14 days", "unit": "", "points": load_points}]

    return {
        "name": "strain", "title": "Day strain",
        "score": strain.get("score"), "unit": "/100", "color": "#fb923c",
        "label": strain.get("label"),
        "contributors": contributors, "stages": stages, "charts": charts,
        "insight": _strain_insight(strain, zones),
    }


_ZONE_META = [("Zone 1", "#60a5fa"), ("Zone 2", "#34d399"), ("Zone 3", "#fbbf24"),
              ("Zone 4", "#fb923c"), ("Zone 5", "#f87171")]


def _todays_zones(c, today: str) -> list[dict[str, Any]]:
    """Sum time-in-HR-zone across today's activities."""
    acts = _safe(lambda: garmin_source.get_recent_load(2)) or {}
    ids = [a.get("activity_id") for a in (acts.get("activities") or [])
           if (a.get("date") or "") == today and a.get("activity_id")]
    totals = [0.0] * 5
    for aid in ids:
        z = _safe(lambda a=aid: c.get_activity_hr_in_timezones(a)) or []
        for row in z:
            n = row.get("zoneNumber")
            if isinstance(n, int) and 1 <= n <= 5:
                totals[n - 1] += (row.get("secsInZone") or 0)
    if not any(totals):
        return []
    return [{"label": _ZONE_META[i][0], "hours": round(totals[i] / 3600, 2), "color": _ZONE_META[i][1]}
            for i in range(5)]


def _strain_insight(strain, zones) -> str | None:
    if not zones:
        return "No HR-based training recorded today yet — strain is coming from daily movement (steps, calories)."
    hard = sum(z["hours"] for z in zones[2:])  # Z3+
    if hard >= 0.5:
        return f"{round(hard,1)} h in Zone 3+ today — meaningful cardiovascular load. Fuel and recover accordingly."
    return "Today's training sat mostly in the easy aerobic zones — good for base and recovery."


# --- T100 readiness -----------------------------------------------------------
def _t100() -> dict[str, Any]:
    if not config.supports_t100_features():
        return {
            "error": "T100 readiness is unavailable for the active event profile",
            "available": False,
        }
    load14 = _safe(lambda: garmin_source.get_recent_load(14)) or {}
    tl = _safe(garmin_source.get_training_load) or {}
    rd = _safe(garmin_source.get_readiness) or {}
    tr_score = (rd.get("training_readiness") or {}).get("score")
    race = config.race_phase()
    t = rings.t100_readiness(load14, tl, tr_score, race.get("days_remaining", 0))
    comp = t.get("components", {})

    def leg(name):
        cc = comp.get(name, {})
        return _contrib(f"{name.title()} volume", {"swim": "🏊", "bike": "🚴", "run": "🏃"}[name],
                        cc.get("km_14d"), unit="km", higher_better=True, pct=cc.get("pct"),
                        fmt=f"target {cc.get('target')} km / 14 d"
                             + ("  · lowest volume bucket"
                                if name == t.get("lowest_volume_bucket") else ""))

    contributors = [leg("swim"), leg("bike"), leg("run"),
                    _contrib("Load balance (ACWR)", "⚖", (comp.get("load_balance") or {}).get("acwr"),
                             unit="", pct=(comp.get("load_balance") or {}).get("pct"),
                             fmt="sweet spot 0.8–1.3"),
                    _contrib("Readiness", "✅", tr_score, unit="/100", higher_better=True,
                             pct=tr_score if isinstance(tr_score, (int, float)) else None)]

    # Volume vs target bars (one per sport, with % of target).
    vol_points = []
    for name in ("swim", "bike", "run"):
        cc = comp.get(name, {})
        vol_points.append({"label": name.title(), "value": cc.get("pct") or 0,
                           "color": {"swim": "#22d3ee", "bike": "#f59e0b", "run": "#fb7185"}[name],
                           "sub": f"{cc.get('km_14d')}/{cc.get('target')} km"})
    charts = [{"kind": "bars", "title": "14-day volume vs target", "unit": "%", "max": 100, "points": vol_points}]

    # Fitness / Fatigue / Form (PMC) mini trend.
    pmc = _safe(lambda: fitness_trend.get_pmc(30))
    if isinstance(pmc, dict) and pmc.get("series"):
        form_pts = [{"label": s["date"][5:], "date": s["date"], "value": s["tsb"]} for s in pmc["series"]]
        charts.append({"kind": "line", "title": "Form (TSB) — last 30 days", "unit": "", "zero": True,
                       "points": form_pts})

    insight = None
    if isinstance(pmc, dict) and pmc.get("interpretation"):
        insight = pmc["interpretation"].get("note")
    low_bucket = t.get("lowest_volume_bucket")
    if low_bucket:
        wc = comp.get(low_bucket, {})
        lead = f"Lowest volume bucket: {low_bucket} at {wc.get('pct')}% of its 14-day target. "
        insight = lead + (insight or "")

    return {
        "name": "t100", "title": "T100 readiness",
        "score": t.get("score"), "unit": "/100", "color": "#c084fc",
        "label": f"{t.get('label')} · {race.get('days_remaining','?')} days out",
        "contributors": contributors, "stages": [], "charts": charts,
        "insight": insight or None,
    }


_BUILDERS = {"recovery": _recovery, "readiness": _recovery, "sleep": _sleep,
             "strain": _strain, "t100": _t100}


def get_detail(name: str) -> dict[str, Any]:
    fn = _BUILDERS.get((name or "").lower())
    if not fn:
        return {"error": f"unknown ring '{name}'"}
    return fn()
