"""Personal baselines for recovery markers (Athlytic / HRV4Training style).

Absolute HRV / resting-HR / sleep numbers mean little without *your* normal
range. We snapshot each day's markers into `wellness_daily` (from whatever the
readiness pull returned — never fabricated), then read a rolling 60-day norm so
every number can be shown as a deviation from your own baseline, not a
population average.

Baselines degrade gracefully: with too few samples we say we're still building
one rather than pretending precision.
"""
from __future__ import annotations

import datetime
import statistics
from typing import Any

from . import config, db

WINDOW_DAYS = 60
MIN_SAMPLES = 7

# marker key -> (wellness column, "higher is better")
_MARKERS = {
    "hrv": ("hrv_ms", True),
    "resting_hr": ("rhr_bpm", False),
    "sleep": ("sleep_h", True),
}


def record_from_readiness(rd: dict[str, Any]) -> None:
    """Persist today's snapshot from a /readiness payload. Best-effort."""
    if not isinstance(rd, dict) or rd.get("error"):
        return
    date = rd.get("date") or config.local_today().isoformat()
    db.upsert_wellness(date, {
        "hrv_ms": (rd.get("hrv") or {}).get("last_night_ms"),
        "rhr_bpm": rd.get("resting_hr_bpm"),
        "sleep_h": (rd.get("sleep") or {}).get("hours"),
        "sleep_score": (rd.get("sleep") or {}).get("score"),
        "readiness_score": (rd.get("training_readiness") or {}).get("score"),
        "body_battery": (rd.get("body_battery") or {}).get("current"),
        "stress": (rd.get("stress") or {}).get("avg"),
    })


def _stats(values: list[float]) -> dict[str, Any] | None:
    if len(values) < MIN_SAMPLES:
        return None
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    return {"mean": round(mean, 1), "sd": round(sd, 2), "n": len(values)}


def get_baselines() -> dict[str, Any]:
    """Rolling baselines + today's deviation for each marker."""
    rows = db.get_wellness(WINDOW_DAYS)
    today = config.local_today().isoformat()
    out: dict[str, Any] = {"window_days": WINDOW_DAYS, "samples": len(rows), "markers": {}}

    for name, (col, higher_better) in _MARKERS.items():
        history = [(r["date"], r[col]) for r in rows
                   if isinstance(r.get(col), (int, float)) and r[col] is not None]
        past = [v for d, v in history if d != today]
        today_val = next((v for d, v in history if d == today), None)
        base = _stats(past)
        marker: dict[str, Any] = {"today": today_val, "baseline": base}
        if base and today_val is not None:
            delta = round(today_val - base["mean"], 1)
            z = round(delta / base["sd"], 2) if base["sd"] else 0.0
            good = (delta >= 0) if higher_better else (delta <= 0)
            # only call it meaningful past ~0.5 SD
            status = "neutral"
            if base["sd"] and abs(z) >= 0.5:
                status = "good" if good else "bad"
            marker.update({"delta": delta, "z": z, "status": status,
                           "higher_better": higher_better})
        out["markers"][name] = marker

    out["ready"] = out["samples"] >= MIN_SAMPLES
    return out


def backfill(days: int = 30) -> dict[str, Any]:
    """One-shot historical seed: scan Garmin day-by-day for markers we don't yet
    have stored. Slow (many API calls) — meant to be triggered manually once.
    Only fills gaps; never overwrites existing rows."""
    from . import garmin_source

    c = garmin_source.get_client()
    rows = db.get_wellness(days + 1)
    # Only skip a day once it already has BOTH the recovery-marker set and a
    # readiness score — older rows predate the readiness/stress columns, so we
    # still revisit them to fill the trend-chart gaps (upsert COALESCEs, never wipes).
    complete = {r["date"] for r in rows
                if r.get("hrv_ms") is not None and r.get("readiness_score") is not None}
    today = config.local_today()
    filled = 0
    for i in range(1, days + 1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        if d in complete:
            continue
        hrv = garmin_source._safe(lambda dd=d: c.get_hrv_data(dd)) or {}
        hs = (hrv.get("hrvSummary") or {}) if isinstance(hrv, dict) else {}
        rhr = None
        try:
            rraw = garmin_source._safe(lambda dd=d: c.get_rhr_day(dd)) or {}
            rhr = rraw["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
        except (KeyError, TypeError, IndexError):
            rhr = None
        sleep = garmin_source._safe(lambda dd=d: c.get_sleep_data(dd)) or {}
        dto = (sleep.get("dailySleepDTO") or {}) if isinstance(sleep, dict) else {}
        secs = dto.get("sleepTimeSeconds")
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") if isinstance(scores, dict) else None

        # training readiness — store the MORNING wake-up value (stable, WHOOP-style
        # recovery), not a later post-exercise reading, so the trend is comparable.
        tr_raw = garmin_source._safe(lambda dd=d: c.get_training_readiness(dd))
        tr_list = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if isinstance(tr_raw, dict) else [])
        tr_list = [x for x in tr_list if isinstance(x, dict) and x.get("score") is not None]
        wake = next((x for x in tr_list if x.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
        if wake is None and tr_list:
            wake = max(tr_list, key=lambda x: x.get("score") or 0)
        ready_score = (wake or {}).get("score")
        # daily average stress
        ss = garmin_source._safe(lambda dd=d: c.get_all_day_stress(dd)) or {}
        stress = ss.get("avgStressLevel") if isinstance(ss, dict) else None

        db.upsert_wellness(d, {
            "hrv_ms": hs.get("lastNightAvg"),
            "rhr_bpm": int(rhr) if isinstance(rhr, (int, float)) else None,
            "sleep_h": round(secs / 3600, 2) if isinstance(secs, (int, float)) and secs else None,
            "sleep_score": overall.get("value") if isinstance(overall, dict) else None,
            "readiness_score": ready_score,
            "stress": stress,
        })
        filled += 1
    return {"requested_days": days, "filled": filled, "total_samples": len(db.get_wellness(WINDOW_DAYS))}
