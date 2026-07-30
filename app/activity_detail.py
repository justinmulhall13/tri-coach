"""Deep-dive for a single activity: stats, GPS route, splits, HR/speed/power
series, and a cached Coach Steve analysis of the session + its training effect.
"""
from __future__ import annotations

import json
from typing import Any

from . import db, garmin_source


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _r(v, n=0):
    """Round a numeric value to n decimals (int when n=0); pass through None/non-numeric."""
    if not isinstance(v, (int, float)):
        return v
    return round(v, n) if n else int(round(v))


def _mps_to_pace(mps: float | None) -> str | None:
    if not isinstance(mps, (int, float)) or mps <= 0:
        return None
    s = 1000.0 / mps
    return f"{int(s // 60)}:{int(s % 60):02d}"


def get_detail(activity_id: int) -> dict[str, Any]:
    c = garmin_source.get_client()
    raw = _safe(lambda: c.get_activity(activity_id)) or {}
    summ = raw.get("summaryDTO") or {}
    tk = ((raw.get("activityTypeDTO") or raw.get("activityType") or {}).get("typeKey")) or ""
    sport = garmin_source._bucket(tk)

    dist_m = summ.get("distance") or 0
    dur_s = summ.get("duration") or 0
    stats = {
        "name": (raw.get("activityName") or "Activity"),
        "sport": sport, "type_key": tk,
        "date": (summ.get("startTimeLocal") or "")[:16].replace("T", " "),
        "distance_km": round(dist_m / 1000, 2) if dist_m else None,
        "duration_min": round(dur_s / 60, 1) if dur_s else None,
        "pace_min_km": _mps_to_pace(summ.get("averageSpeed")),
        "speed_kmh": round((summ.get("averageSpeed") or 0) * 3.6, 1) or None,
        "hr_avg": _r(summ.get("averageHR")), "hr_max": _r(summ.get("maxHR")),
        "avg_power_w": _r(summ.get("averagePower")), "max_power_w": _r(summ.get("maxPower")),
        "norm_power_w": _r(summ.get("normalizedPower")),
        "cadence": _r(summ.get("averageRunCadence") or summ.get("averageBikeCadence")),
        "elev_gain_m": _r(summ.get("elevationGain")), "elev_loss_m": _r(summ.get("elevationLoss")),
        "calories": _r(summ.get("calories")),
        "training_load": _r(summ.get("activityTrainingLoad")),
        "aerobic_te": _r(summ.get("trainingEffect"), 1), "anaerobic_te": _r(summ.get("anaerobicTrainingEffect"), 1),
        "avg_stroke_dist_m": _r(summ.get("avgStrokeDistance"), 2),
        "strokes": _r(summ.get("strokes")),
    }

    # GPS + series (downsampled server-side by maxchart/maxpoly)
    det = _safe(lambda: c.get_activity_details(activity_id, maxchart=300, maxpoly=600)) or {}
    poly = ((det.get("geoPolylineDTO") or {}).get("polyline")) or []
    route = [[p["lat"], p["lon"]] for p in poly
             if isinstance(p, dict) and p.get("valid") and p.get("lat") is not None]

    series: dict[str, list] = {}
    descs = det.get("metricDescriptors") or []
    idx = {d.get("key"): d.get("metricsIndex") for d in descs if isinstance(d, dict)}
    rows = det.get("activityDetailMetrics") or []
    def col(key):
        i = idx.get(key)
        if i is None:
            return None
        vals = [(r.get("metrics") or [None])[i] if isinstance(r, dict) else None for r in rows]
        return vals if any(v is not None for v in vals) else None
    hr = col("directHeartRate"); spd = col("directSpeed"); pwr = col("directPower")
    dist = col("sumDistance"); elev = col("directElevation")
    if dist:
        series["dist_km"] = [round((v or 0) / 1000, 2) for v in dist]
    if hr:
        series["hr"] = hr
    if spd:
        series["speed_kmh"] = [round((v or 0) * 3.6, 1) for v in spd]
        if sport == "run":
            series["pace"] = [_mps_to_pace(v) for v in spd]
    if pwr:
        series["power"] = pwr
    if elev:
        series["elev"] = elev

    # Splits
    laps_out = []
    sp = _safe(lambda: c.get_activity_splits(activity_id)) or {}
    for i, lap in enumerate(sp.get("lapDTOs") or [], 1):
        laps_out.append({
            "n": i,
            "km": round((lap.get("distance") or 0) / 1000, 2),
            "min": round((lap.get("duration") or 0) / 60, 2),
            "pace": _mps_to_pace(lap.get("averageMovingSpeed") or lap.get("averageSpeed")),
            "hr": _r(lap.get("averageHR")),
            "power": _r(lap.get("averagePower")),
            "elev_gain": _r(lap.get("elevationGain")),
        })

    return {"activity_id": activity_id, "stats": stats, "route": route,
            "series": series, "splits": laps_out}


_ANALYZE_PROMPT = """Here is the full data for one of my completed activities (stats, splits, \
and series summaries). Give me your read on this session in 4–6 sentences:
1. What kind of session this actually was (based on the numbers, not the name).
2. Execution quality — pacing/power/HR discipline, split consistency, decoupling if visible.
3. Training effect — what this did for me physiologically and how it fits my T100 build \
and current load focus.
Direct, evidence-based, reference specific numbers. No adjustment block, no preamble."""


def analyze(activity_id: int) -> dict[str, Any]:
    """Coach Steve's take on one activity — cached per activity id."""
    cache_key = f"analysis_{activity_id}"
    cached = db.get_meta(cache_key)
    if cached:
        return {"analysis": cached, "cached": True}

    from . import coach  # late import to avoid cycles
    detail = get_detail(activity_id)
    slim = {"stats": detail["stats"], "splits": detail["splits"][:20]}
    ser = detail.get("series") or {}
    if ser.get("hr"):
        hr = [v for v in ser["hr"] if v]
        if hr:
            slim["hr_series_summary"] = {"min": min(hr), "max": max(hr),
                                         "first_third_avg": round(sum(hr[:len(hr)//3])/max(len(hr)//3,1)),
                                         "last_third_avg": round(sum(hr[-(len(hr)//3):])/max(len(hr)//3,1))}
    if ser.get("power"):
        pw = [v for v in ser["power"] if v]
        if pw:
            slim["power_series_summary"] = {"min": min(pw), "max": max(pw),
                                            "avg": round(sum(pw)/len(pw))}

    context = coach._context_block()
    messages = [{"role": "user", "content":
                 f"<context>\n{context}\n</context>\n\n<activity>\n{json.dumps(slim, default=str)}\n</activity>\n\n{_ANALYZE_PROMPT}"}]
    try:
        msg = coach._stream_reply(3000, messages)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if text:
        db.set_meta(cache_key, text)
    return {"analysis": text, "cached": False}
