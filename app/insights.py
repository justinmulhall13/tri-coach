"""Proactive signals (Bevel-Intelligence style) — surfaced without being asked.

Deterministic, cheap (no LLM), always-on. Reads the personal baselines, the
Fitness/Fatigue/Form model, ACWR + Garmin load focus, and race readiness, then
flags multi-day trends that a once-a-day glance would miss: a 3-day HRV slide,
elevated resting HR, sleep debt, dangerous form, an unsafe ramp, a stalling weak
leg. Severity-ranked so the UI shows what matters first — and fed into the coach
context so the morning brief speaks to the same signals.
"""
from __future__ import annotations

from typing import Any

from . import baselines, config, fitness_trend, garmin_source, rings

_RANK = {"high": 0, "warn": 1, "info": 2, "good": 3}




def _n(v):
    """A marker value without a trailing .0 (42.0 ms reads as 42 ms)."""
    return int(round(v)) if isinstance(v, (int, float)) else v


def _hm(hours):
    """Hours as h:mm (7.77 h is 7:46)."""
    if not isinstance(hours, (int, float)):
        return hours
    m = int(round(hours * 60))
    return f"{m // 60}:{m % 60:02d}"

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _trailing_run(values: list[float], *, down: bool) -> int:
    """How many consecutive steps at the end move in one direction."""
    n = 0
    for i in range(len(values) - 1, 0, -1):
        if (values[i] < values[i - 1]) if down else (values[i] > values[i - 1]):
            n += 1
        else:
            break
    return n


def get_insights(*, baseline_data: dict[str, Any] | None = None,
                 pmc_data: dict[str, Any] | None = None,
                 training_load_data: dict[str, Any] | None = None,
                 rings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build deterministic signals, optionally reusing data a caller already has.

    The dashboard still calls this with no arguments. Coach passes its existing
    readiness/load snapshot so generating prompt context does not hit Garmin a
    second time for the exact same metrics.
    """
    signals: list[dict[str, Any]] = []

    base = baseline_data if baseline_data is not None else (_safe(baselines.get_baselines, {}) or {})
    pmc = pmc_data if pmc_data is not None else (_safe(lambda: fitness_trend.get_pmc(90), {}) or {})
    if training_load_data is not None:
        tl = training_load_data
    else:
        raw_tl = _safe(garmin_source.get_training_load, {}) or {}
        recent = _safe(lambda: garmin_source.get_recent_load(14), {}) or {}
        _, tl = garmin_source.reconcile_freshness({}, raw_tl, recent)
    rg = rings_data if rings_data is not None else (_safe(rings.get_rings, {}) or {})
    from . import db
    wellness = _safe(lambda: db.get_wellness(14), []) or []
    race = config.race_phase()

    def add(sev, icon, title, detail):
        signals.append({"severity": sev, "icon": icon, "title": title, "detail": detail})

    markers = base.get("markers", {}) if isinstance(base, dict) else {}

    # --- HRV: today's deviation + multi-day slide ---
    hrv = markers.get("hrv") or {}
    hrv_series = [r["hrv_ms"] for r in wellness if isinstance(r.get("hrv_ms"), (int, float))]
    hrv_run = _trailing_run(hrv_series, down=True)
    if hrv.get("status") == "bad":
        b = hrv.get("baseline") or {}
        add("warn", "", "HRV is low",
            f"{_n(hrv.get('today'))} ms, usually {_n(b.get('mean'))}. Keep today aerobic.")
    elif hrv_run >= 3:
        add("warn", "", f"HRV down {hrv_run} days running",
            "Fatigue is building. Go easy for the next two days.")
    elif hrv.get("status") == "good":
        add("good", "", "HRV is high", "You're fresh. Good day for the hard work.")

    # --- Resting HR elevated ---
    rhr = markers.get("resting_hr") or {}
    if rhr.get("status") == "bad":
        b = rhr.get("baseline") or {}
        add("warn", "", "Resting HR is up",
            f"{_n(rhr.get('today'))} bpm, usually {_n(b.get('mean'))}. Fatigue or something coming on.")

    # --- Sleep debt (3-night average vs baseline) ---
    sleep = markers.get("sleep") or {}
    sl_series = [r["sleep_h"] for r in wellness if isinstance(r.get("sleep_h"), (int, float))]
    base_sleep = (sleep.get("baseline") or {}).get("mean")
    if base_sleep and len(sl_series) >= 3:
        avg3 = sum(sl_series[-3:]) / 3
        if avg3 < base_sleep - 1:
            add("warn", "", "Short on sleep",
                f"About {_hm(avg3)} a night lately, usually {_hm(base_sleep)}. Get to bed early tonight.")

    # --- Form (TSB) danger / race-timing ---
    cur = (pmc.get("current") or {}) if isinstance(pmc, dict) else {}
    tsb = cur.get("tsb")
    ramp = cur.get("ramp_7d")
    dleft = race.get("days_remaining")
    if isinstance(tsb, (int, float)):
        if tsb <= -30:
            add("high", "", "Carrying a lot of fatigue",
                f"Form is {tsb:.0f}. Plan an easy day soon.")
        if isinstance(dleft, int) and 0 <= dleft <= 10 and tsb < 0:
            add("high", "", "Not fresh for race day yet",
                f"{dleft} days out and form is {tsb:.0f}. Cut volume now.")
    if isinstance(ramp, (int, float)) and ramp < -3 and race.get("phase") != "taper":
        add("warn", "", "Fitness is slipping",
            "Fitness has dropped this week. Consistent sessions will turn it around.")
    elif isinstance(ramp, (int, float)) and ramp > 7:
        add("warn", "", "Building fast",
            "Fitness is climbing quickly. Watch for niggles and sleep well.")

    # --- ACWR ---
    tl_freshness = tl.get("freshness") if isinstance(tl, dict) else None
    acwr = tl.get("load_ratio") if isinstance(tl, dict) else None
    if isinstance(tl_freshness, dict) and tl_freshness.get("is_current") is not True:
        acwr = None
    if isinstance(acwr, (int, float)):
        if acwr > 1.5:
            add("high", "", "Load jumped too fast",
                f"This week is well above your usual (ratio {acwr:.1f}). Back off before something breaks.")
        elif acwr < 0.8:
            add("info", "", "Room to build",
                "Recent load is below your usual. Safe to add a bit if you feel good.")

    # --- Active-goal weakness stalling ---
    event_ready = rg.get("event_ready") or {}
    low_bucket = event_ready.get("lowest_volume_bucket")
    comp = (event_ready.get("components") or {}).get(low_bucket) or {}
    if low_bucket and isinstance(comp.get("pct"), (int, float)) and comp["pct"] < 60:
        if low_bucket == "run":
            detail = (f"Running is at {comp['pct']}% of target for the last two weeks. "
                      "Build back gradually with a proper long run.")
        else:
            detail = (f"{low_bucket.title()} is at {comp['pct']}% of target for the last two weeks. "
                      f"Add {low_bucket} time when recovery allows.")
        add("info", "", f"{low_bucket.title()} is behind", detail)

    signals.sort(key=lambda s: _RANK.get(s["severity"], 9))
    if not signals:
        add("good", "", "All good",
            "Recovery, form and load all look fine. Do the plan.")

    top = signals[:6]
    worst = min((s["severity"] for s in top), key=lambda s: _RANK.get(s, 9)) if top else "good"
    return {"signals": top, "count": len(signals), "worst": worst,
            "baseline_ready": base.get("ready", False) if isinstance(base, dict) else False}
