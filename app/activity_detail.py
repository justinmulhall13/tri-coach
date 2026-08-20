"""Deep-dive for a single activity: stats, GPS route, splits, HR/speed/power
series, and a cached Coach Steve analysis of the session + its training effect.
"""
from __future__ import annotations

import json
from typing import Any

from . import config, db, garmin_source, interval_analysis


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
            "series": series, "splits": laps_out,
            "decoupling": _decoupling(series, sport),
            "best_efforts": _best_efforts(series, sport, (stats.get("duration_min") or 0) * 60)}


def _decoupling(series: dict[str, list], sport: str) -> dict[str, Any] | None:
    """Aerobic decoupling (Pw:HR for bike, Pa:HR for run): does output per heartbeat
    fade in the second half? Under ~5% is well-coupled aerobic fitness; a big
    positive number means the athlete faded (or started too hard)."""
    hr = series.get("hr") or []
    out = series.get("power") if sport == "bike" and series.get("power") else series.get("speed_kmh")
    if not hr or not out or len(hr) < 20 or len(out) < 20:
        return None
    n = min(len(hr), len(out))
    hr, out = hr[:n], out[:n]
    half = n // 2

    def ratio(a, b):
        pairs = [(o, h) for o, h in zip(a, b) if isinstance(o, (int, float)) and isinstance(h, (int, float)) and h > 40 and o > 0]
        if len(pairs) < 10:
            return None
        mo = sum(p[0] for p in pairs) / len(pairs)
        mh = sum(p[1] for p in pairs) / len(pairs)
        return (mo / mh) if mh else None

    r1 = ratio(out[:half], hr[:half])
    r2 = ratio(out[half:], hr[half:])
    if not r1 or not r2:
        return None
    pct = (r1 - r2) / r1 * 100.0
    if pct <= 5:
        verdict = "well coupled — aerobically solid"
    elif pct <= 10:
        verdict = "mild fade — watch pacing or fuelling"
    else:
        verdict = "significant fade — went out too hard, under-fuelled, or aerobically short"
    return {"metric": "Pw:HR" if sport == "bike" else "Pa:HR",
            "percent": round(pct, 1), "first_half": round(r1, 3),
            "second_half": round(r2, 3), "verdict": verdict}


def _best_efforts(series: dict[str, list], sport: str, total_s: float = 0.0) -> list[dict[str, Any]] | None:
    """Peak sustained efforts (the athlete's power/pace curve for this session):
    best rolling average over a set of durations."""
    vals = series.get("power") if sport == "bike" and series.get("power") else series.get("speed_kmh")
    if not vals:
        return None
    clean = [v if isinstance(v, (int, float)) else None for v in vals]
    n = len(clean)
    if n < 12:
        return None
    # The detail series is downsampled; approximate the per-sample interval so the
    # windows are labelled in real time rather than sample count.
    step = (total_s / n) if total_s and n else 6.0   # ~6 s/sample at maxchart=300
    out = []
    for label, secs in (("5s", 5), ("1min", 60), ("5min", 300), ("10min", 600), ("20min", 1200), ("60min", 3600)):
        w = max(1, int(round(secs / step)))
        if w > n:
            continue
        run, best, cnt = 0.0, None, 0
        for i, v in enumerate(clean):
            if v is not None:
                run += v; cnt += 1
            if i >= w:
                old = clean[i - w]
                if old is not None:
                    run -= old; cnt -= 1
            if i >= w - 1 and cnt >= max(3, w // 2):
                avg = run / cnt
                if best is None or avg > best:
                    best = avg
        if best is None:
            continue
        if sport == "bike" and series.get("power"):
            out.append({"window": label, "value": round(best), "unit": "W"})
        else:
            mps = best / 3.6
            sec_km = 1000.0 / mps if mps > 0 else None
            out.append({"window": label, "value": round(best, 1), "unit": "km/h",
                        "pace": f"{int(sec_km//60)}:{int(sec_km%60):02d}/km" if sec_km else None})
    return out or None


_T100_ANALYZE_PROMPT = """Here is the full data for one of my completed activities (stats, splits, \
and series summaries). Give me your read on this session in 4–6 sentences:
1. What kind of session this actually was (based on the numbers, not the name).
2. Execution quality — pacing/power/HR discipline, split consistency, decoupling if visible.
3. Training effect — what this did for me physiologically and how it fits my T100 build \
and current load focus.
For a structured interval session, grade each ACTIVE bout from `interval_execution` and never \
compare whole-session average HR with the work target.
Direct, evidence-based, reference specific numbers. No adjustment block, no preamble."""


_GENERIC_ANALYZE_PROMPT = """Here is the full data for one of my completed activities (stats, splits, \
and series summaries). Give me your read on this session in 4–6 sentences:
1. What kind of session this actually was (based on the numbers, not the name).
2. Execution quality: pacing, heart-rate discipline, split consistency, and decoupling if visible.
3. Training effect: what this did physiologically and how it fits the active event profile when relevant.
For a structured interval session, grade each ACTIVE bout from `interval_execution` and never \
compare whole-session average HR with the work target.
Do not import distances, targets, or plan assumptions from any other event profile.
Direct, evidence-based, reference specific numbers. No adjustment block, no preamble."""


def _analyze_prompt() -> str:
    return _T100_ANALYZE_PROMPT if config.supports_t100_features() else _GENERIC_ANALYZE_PROMPT


def _analysis_cache_key(activity_id: int) -> str:
    """Keep analyses from one event profile out of every subsequent profile."""
    profile_id = str(config.active_event_profile().get("id") or "no-event")
    safe_id = "".join(ch if ch.isalnum() else "_" for ch in profile_id)
    return f"analysis_v3_{safe_id}_{activity_id}"


def analyze(activity_id: int) -> dict[str, Any]:
    """Coach Steve's take on one activity — cached per activity id."""
    # v3 includes recorded interval boundaries, duration-weighted HR, and the
    # active profile id. Never reuse an analysis carrying another event's frame.
    cache_key = _analysis_cache_key(activity_id)
    cached = db.get_meta(cache_key)
    if cached:
        return {"analysis": cached, "cached": True}

    from . import coach  # late import to avoid cycles
    detail = get_detail(activity_id)
    slim = {"stats": detail["stats"], "splits": detail["splits"][:20]}
    interval_execution = interval_analysis.get(activity_id)
    if interval_execution:
        slim["interval_execution"] = interval_execution
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
                 f"<context>\n{context}\n</context>\n\n<activity>\n{json.dumps(slim, default=str)}\n</activity>\n\n{_analyze_prompt()}"}]
    try:
        msg = coach._stream_reply(3000, messages)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if text:
        db.set_meta(cache_key, text)
    return {"analysis": text, "cached": False}
