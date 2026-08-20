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
        add("warn", "🫀", "HRV below your baseline",
            f"{hrv.get('today')} ms vs your {b.get('mean')} ms norm ({hrv.get('delta'):+} ms). "
            "Autonomic recovery is lagging — favour aerobic over intensity.")
    elif hrv_run >= 3:
        add("warn", "🫀", f"HRV down {hrv_run} days running",
            "A sustained downward drift often precedes fatigue. Watch load the next 48h.")
    elif hrv.get("status") == "good":
        add("good", "🫀", "HRV above baseline", "Autonomic system is fresh — green to push.")

    # --- Resting HR elevated ---
    rhr = markers.get("resting_hr") or {}
    if rhr.get("status") == "bad":
        b = rhr.get("baseline") or {}
        add("warn", "❤️", "Resting HR elevated",
            f"{rhr.get('today')} bpm vs {b.get('mean')} bpm norm ({rhr.get('delta'):+}). "
            "Classic early flag for fatigue or a bug coming on.")

    # --- Sleep debt (3-night average vs baseline) ---
    sleep = markers.get("sleep") or {}
    sl_series = [r["sleep_h"] for r in wellness if isinstance(r.get("sleep_h"), (int, float))]
    base_sleep = (sleep.get("baseline") or {}).get("mean")
    if base_sleep and len(sl_series) >= 3:
        avg3 = sum(sl_series[-3:]) / 3
        if avg3 < base_sleep - 1:
            add("warn", "😴", "Sleep debt building",
                f"Last 3 nights averaged {avg3:.1f} h vs your {base_sleep} h norm. "
                "Recovery capacity is compromised — protect tonight's sleep.")

    # --- Form (TSB) danger / race-timing ---
    cur = (pmc.get("current") or {}) if isinstance(pmc, dict) else {}
    tsb = cur.get("tsb")
    ramp = cur.get("ramp_7d")
    dleft = race.get("days_remaining")
    if isinstance(tsb, (int, float)):
        if tsb <= -30:
            add("high", "📉", "Form deeply negative",
                f"TSB {tsb:.0f} — you're carrying heavy fatigue. Sustainable only briefly; plan a down day.")
        if isinstance(dleft, int) and 0 <= dleft <= 10 and tsb < 0:
            add("high", "🏁", "Not freshening for race day",
                f"{dleft} days out with Form still {tsb:.0f}. Taper needs to bite — cut volume now.")
    if isinstance(ramp, (int, float)) and ramp < -3 and race.get("phase") != "taper":
        add("warn", "📉", "Fitness bleeding",
            f"CTL down {ramp:.1f}/week outside a taper — you're detraining. Add consistency.")
    elif isinstance(ramp, (int, float)) and ramp > 7:
        add("warn", "⚠️", "Ramping fast",
            f"Fitness climbing {ramp:.1f}/week — strong, but a fast ramp raises injury/overreach risk.")

    # --- ACWR ---
    tl_freshness = tl.get("freshness") if isinstance(tl, dict) else None
    acwr = tl.get("load_ratio") if isinstance(tl, dict) else None
    if isinstance(tl_freshness, dict) and tl_freshness.get("is_current") is not True:
        acwr = None
    if isinstance(acwr, (int, float)):
        if acwr > 1.5:
            add("high", "🚧", "Load ramp in the danger zone",
                f"ACWR {acwr:.2f} (>1.5). Acute load far outruns chronic — highest injury-risk band.")
        elif acwr < 0.8:
            add("info", "🌱", "Load ratio low",
                f"ACWR {acwr:.2f} — room to build safely if recovery allows.")

    # --- Race weakness stalling ---
    # The T100 volume model has Vancouver-specific targets. Ignore even a stale
    # caller-supplied payload after the active event profile changes.
    t100 = (rg.get("t100") or {}) if config.supports_t100_features() else {}
    low_bucket = t100.get("lowest_volume_bucket")
    comp = (t100.get("components") or {}).get(low_bucket) or {}
    if low_bucket and isinstance(comp.get("pct"), (int, float)) and comp["pct"] < 60:
        if low_bucket == "run":
            detail = (f"Run is at {comp['pct']}% of its 14-day bucket. Do not jump run volume; "
                      "the Achilles is limiting. Add any needed low-intensity load on the bike.")
        else:
            detail = (f"{low_bucket.title()} is at {comp['pct']}% of its 14-day bucket. "
                      f"Bias safe {low_bucket} work only if recovery and the active plan allow it.")
        add("info", "🎯", f"{low_bucket.title()} volume bucket below target", detail)

    signals.sort(key=lambda s: _RANK.get(s["severity"], 9))
    if not signals:
        add("good", "✅", "All systems green",
            "No adverse trends in recovery, form, or load. Execute the plan.")

    top = signals[:6]
    worst = min((s["severity"] for s in top), key=lambda s: _RANK.get(s, 9)) if top else "good"
    return {"signals": top, "count": len(signals), "worst": worst,
            "baseline_ready": base.get("ready", False) if isinstance(base, dict) else False}
