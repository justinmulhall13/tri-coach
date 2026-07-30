"""Direct Garmin pull layer (garminconnect).

Every value returned here comes from the Garmin API. When a metric is
unavailable we return None and record it in a `missing` list — we never
fabricate. This is the corrected data layer (the upstream garmin_mcp.py
called a non-existent RHR method and referenced missing VO2/zone helpers).
"""
from __future__ import annotations

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import config

_client: Any = None
_lock = threading.Lock()


def _today() -> str:
    return config.local_today().isoformat()


def _days_back(n: int) -> str:
    return (config.local_today() - datetime.timedelta(days=n)).isoformat()


_WEIGHT_CACHE: dict[str, Any] = {"val": None, "ts": 0.0}


def get_weight_kg() -> dict[str, Any] | None:
    """Latest weigh-in from Garmin Connect (grams → kg). Cached ~1h so logging a
    new weight in Garmin flows straight into fueling math. None if never logged."""
    import time
    now = time.time()
    if _WEIGHT_CACHE["val"] is not None and (now - _WEIGHT_CACHE["ts"]) < 3600:
        return _WEIGHT_CACHE["val"]
    out = None
    try:
        c = get_client()
        bc = c.get_body_composition(_days_back(365), _today()) or {}
        entries = [e for e in (bc.get("dateWeightList") or []) if e.get("weight")]
        entries.sort(key=lambda e: e.get("calendarDate") or "")
        latest = entries[-1] if entries else (bc.get("totalAverage") or None)
        w = (latest or {}).get("weight")
        if isinstance(w, (int, float)) and w > 0:
            out = {"kg": round(w / 1000.0, 1), "lb": round(w / 1000.0 * 2.2046, 1),
                   "as_of": (latest or {}).get("calendarDate"), "source": "garmin"}
    except Exception:
        out = None
    _WEIGHT_CACHE["val"] = out
    _WEIGHT_CACHE["ts"] = now
    return out


def get_client() -> Any:
    """Authenticated Garmin client, token-first then credential fallback."""
    global _client
    with _lock:
        if _client is not None:
            return _client
        from garminconnect import Garmin

        import os
        token_store = os.path.normpath(os.path.expanduser(config.GARMIN_TOKENSTORE))

        # 1) Resume from cached OAuth token (no creds needed once logged in).
        try:
            c = Garmin()
            c.login(token_store)
            c.get_full_name()
            _client = c
            return _client
        except Exception:
            _client = None

        # 2) Credential login (refreshes the cached token).
        email = config._get("GARMIN_EMAIL")
        password = config._get("GARMIN_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "Garmin token resume failed and GARMIN_EMAIL/PASSWORD not set. "
                "Run the one-time login: ./.venv/bin/python -m app.login"
            )
        c = Garmin(email=email, password=password)
        c.login(token_store)
        _client = c
        return _client


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        return None


def get_readiness() -> dict[str, Any]:
    """This-morning recovery panel. Each field tagged; missing → listed."""
    c = get_client()
    today, yest = _today(), _days_back(1)
    missing: list[str] = []

    with ThreadPoolExecutor(max_workers=7) as ex:
        f_tr = ex.submit(_safe, lambda: c.get_training_readiness(today))
        # Garmin keys "last night's" sleep under the wake-up date (today), not
        # the date you fell asleep — querying `yest` returns the night before.
        f_sleep = ex.submit(_safe, lambda: c.get_sleep_data(today))
        f_hrv = ex.submit(_safe, lambda: c.get_hrv_data(today))
        f_bb = ex.submit(_safe, lambda: c.get_body_battery(yest, today))
        f_rhr = ex.submit(_safe, lambda: c.get_rhr_day(today))
        f_stress = ex.submit(_safe, lambda: c.get_all_day_stress(today))
        f_spo2 = ex.submit(_safe, lambda: c.get_spo2_data(today))

    # training readiness — Garmin returns MULTIPLE readings through the day, each
    # tagged with an inputContext. The score is LIVE: it's high after sleep and
    # drops after a workout. We split them:
    #   • "Recovery"  = the post-sleep wake-up value (stable for the day, WHOOP-style)
    #   • "current"   = the most recent reading (reflects post-exercise depletion)
    tr_raw = f_tr.result()
    tr_list = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if isinstance(tr_raw, dict) else [])
    tr_list = [x for x in tr_list if isinstance(x, dict) and x.get("score") is not None]
    wake = next((x for x in tr_list if x.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
    if wake is None and tr_list:                       # fallback: the day's peak reading
        wake = max(tr_list, key=lambda x: x.get("score") or 0)
    current = max(tr_list, key=lambda x: x.get("timestamp") or "") if tr_list else {}
    wake = wake or {}
    # `training_readiness` is the MORNING recovery — the value that should drive
    # training decisions and displays ("how recovered you woke up").
    readiness = {
        "score": wake.get("score"),
        "level": wake.get("level"),
        "feedback": wake.get("feedbackShort"),
        "sleep_score_factor_pct": wake.get("sleepScoreFactorPercent"),
        "as_of": wake.get("timestampLocal"),
    }
    current_readiness = {
        "score": current.get("score"),
        "level": current.get("level"),
        "feedback": current.get("feedbackShort"),
        "as_of": current.get("timestampLocal"),
        "input_context": current.get("inputContext"),
        "is_post_exercise": current.get("inputContext") == "AFTER_POST_EXERCISE_RESET",
    }
    if readiness["score"] is None:
        missing.append("training_readiness")

    # sleep (last night)
    dto = (f_sleep.result() or {}).get("dailySleepDTO") or {}
    secs = dto.get("sleepTimeSeconds")
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") if isinstance(scores, dict) else None
    sleep = {
        "hours": round(secs / 3600, 2) if isinstance(secs, (int, float)) and secs else None,
        "score": overall.get("value") if isinstance(overall, dict) else None,
        "deep_h": round((dto.get("deepSleepSeconds") or 0) / 3600, 2) if dto else None,
        "rem_h": round((dto.get("remSleepSeconds") or 0) / 3600, 2) if dto else None,
        "date": today,
    }
    if sleep["hours"] is None:
        missing.append("sleep")

    # HRV
    hs = (f_hrv.result() or {}).get("hrvSummary") or {}
    hrv = {"last_night_ms": hs.get("lastNightAvg"), "weekly_ms": hs.get("weeklyAvg"), "status": hs.get("status")}
    if hrv["last_night_ms"] is None:
        missing.append("hrv")

    # body battery
    bb_raw = f_bb.result()
    bb_latest = bb_raw[-1] if isinstance(bb_raw, list) and bb_raw else {}
    # The per-day "highestBatteryLevel"/"lowestBatteryLevel" keys are often null;
    # derive current/high/low from the intraday samples instead.
    levels = [pt[1] for pt in (bb_latest.get("bodyBatteryValuesArray") or [])
              if isinstance(pt, list) and len(pt) > 1 and isinstance(pt[1], (int, float))]
    body_battery = {
        "current": levels[-1] if levels else None,
        "charged": bb_latest.get("charged"),
        "drained": bb_latest.get("drained"),
        "highest": max(levels) if levels else bb_latest.get("highestBatteryLevel"),
        "lowest": min(levels) if levels else bb_latest.get("lowestBatteryLevel"),
    }
    if body_battery["charged"] is None:
        missing.append("body_battery")

    # resting HR (correct endpoint)
    rhr_val = None
    try:
        rhr_val = f_rhr.result()["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
        rhr_val = int(rhr_val) if isinstance(rhr_val, (int, float)) else None
    except (KeyError, TypeError, IndexError):
        rhr_val = None
    if rhr_val is None:
        missing.append("resting_hr")

    # stress
    ss = f_stress.result() or {}
    stress = {"avg": ss.get("avgStressLevel"), "max": ss.get("maxStressLevel")}
    if stress["avg"] is None:
        missing.append("stress")

    # spo2 (optional)
    sp = f_spo2.result() or {}
    spo2 = {"avg": sp.get("averageSpO2"), "lowest": sp.get("lowestSpO2")}

    return {
        "date": today,
        "training_readiness": readiness,      # morning post-sleep recovery (stable)
        "current_readiness": current_readiness,  # live value; drops after training
        "sleep": sleep,
        "hrv": hrv,
        "body_battery": body_battery,
        "resting_hr_bpm": rhr_val,
        "stress": stress,
        "spo2": spo2,
        "missing": missing,
        "one_line": _readiness_one_line(readiness, sleep, hrv, body_battery),
    }


def _readiness_one_line(readiness, sleep, hrv, bb) -> str:
    """Deterministic plain-language read (the coach LLM can override later)."""
    score = readiness.get("score")
    hrv_status = (hrv.get("status") or "").upper()
    sl = sleep.get("hours")
    bad_signals = []
    if isinstance(score, (int, float)) and score < 40:
        bad_signals.append("low readiness")
    if hrv_status in ("UNBALANCED", "LOW", "POOR"):
        bad_signals.append("suppressed HRV")
    if isinstance(sl, (int, float)) and sl < 6:
        bad_signals.append("short sleep")
    if bad_signals:
        return f"Back off today — {', '.join(bad_signals)}."
    if isinstance(score, (int, float)) and score >= 70:
        return "Recovered, green light."
    if score is None:
        return "Readiness data missing — judge by feel and recent load."
    return "Moderate — train as planned but keep intensity honest."


def get_fitness_markers() -> dict[str, Any]:
    """VO2max (run + bike, scanned to latest available), FTP, race predictions."""
    c = get_client()
    today = config.local_today()
    vo2_run = vo2_bike = vo2_as_of = None
    for i in range(0, 21):
        d = (today - datetime.timedelta(days=i)).isoformat()
        raw = _safe(lambda dd=d: c.get_max_metrics(dd))
        if isinstance(raw, list) and raw:
            generic = (raw[0] or {}).get("generic") or {}
            cycling = (raw[0] or {}).get("cycling") or {}
            run = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            bike = (cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue")) if isinstance(cycling, dict) else None
            if run and vo2_run is None:
                vo2_run, vo2_as_of = round(run, 1), generic.get("calendarDate") or d
            if bike and vo2_bike is None:
                vo2_bike = round(bike, 1)
            if vo2_run is not None and vo2_bike is not None:
                break

    ftp = _safe(lambda: c.get_cycling_ftp())
    ftp_w = None
    if isinstance(ftp, dict):
        ftp_w = ftp.get("functionalThresholdPower") or ftp.get("ftp")
    elif isinstance(ftp, (int, float)):
        ftp_w = int(ftp)

    rp = _safe(lambda: c.get_race_predictions())
    item = rp[-1] if isinstance(rp, list) and rp else (rp if isinstance(rp, dict) else {})
    def hms(s):
        if not isinstance(s, (int, float)) or s <= 0:
            return None
        h, r = divmod(int(s), 3600); m, sec = divmod(r, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    race_pred = {k2: hms(item.get(k1)) for k1, k2 in
                 [("time5K", "5K"), ("time10K", "10K"), ("timeHalfMarathon", "Half"), ("timeMarathon", "Marathon")]
                 if item.get(k1)}

    return {
        "vo2_max_running": vo2_run,
        "vo2_max_cycling": vo2_bike,
        "vo2_as_of": vo2_as_of,
        "cycling_ftp_w": ftp_w,
        "race_predictions": race_pred or None,
    }


def get_training_load() -> dict[str, Any]:
    """Acute/chronic load, ACWR, and load-focus distribution vs Garmin targets."""
    c = get_client()
    raw = _safe(lambda: c.get_training_status(_today()))
    out: dict[str, Any] = {"acute_load": None, "chronic_load": None, "load_ratio": None,
                           "acwr_status": None, "load_focus": None}
    if not isinstance(raw, dict):
        out["error"] = "training status unavailable (needs ~7 days on a compatible device)"
        return out
    try:
        latest = (raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
        first = next(iter(latest.values()), {}) if latest else {}
        atl = first.get("acuteTrainingLoadDTO") or {}
        out.update({
            "acute_load": atl.get("dailyTrainingLoadAcute"),
            "chronic_load": atl.get("dailyTrainingLoadChronic"),
            "load_ratio": atl.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": atl.get("acwrStatus"),
        })
    except Exception:
        pass
    try:
        bal = (raw.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        fb = next(iter(bal.values()), {}) if bal else {}
        if fb:
            out["load_focus"] = {
                "aerobic_low": fb.get("monthlyLoadAerobicLow"),
                "aerobic_high": fb.get("monthlyLoadAerobicHigh"),
                "anaerobic": fb.get("monthlyLoadAnaerobic"),
                "aerobic_low_target": [fb.get("monthlyLoadAerobicLowTargetMin"), fb.get("monthlyLoadAerobicLowTargetMax")],
                "aerobic_high_target": [fb.get("monthlyLoadAerobicHighTargetMin"), fb.get("monthlyLoadAerobicHighTargetMax")],
                "anaerobic_target": [fb.get("monthlyLoadAnaerobicTargetMin"), fb.get("monthlyLoadAnaerobicTargetMax")],
                "feedback": fb.get("trainingBalanceFeedbackPhrase"),
            }
    except Exception:
        pass
    return out


_SPORT_BUCKET = (
    ("swim", ("swim",)),
    ("bike", ("cycl", "bik", "virtual_ride")),
    ("run", ("run",)),
    ("strength", ("strength",)),
    ("brick", ("multi_sport", "multisport")),
)

# multisport child-leg details are immutable once recorded → cache per activity id
# so we don't re-fetch children on every get_recent_load call.
_MS_LEGS_CACHE: dict[Any, Any] = {}


def _multisport_legs(c: Any, parent_id: Any) -> list[dict[str, Any]] | None:
    """Per-leg HR/power/distance for a multisport (brick) activity — the parent
    summary has no aggregate averageHR, so we read it from the child activities."""
    if parent_id in _MS_LEGS_CACHE:
        return _MS_LEGS_CACHE[parent_id]
    legs: list[dict[str, Any]] = []
    try:
        detail = c.get_activity(parent_id)
        child_ids = ((detail or {}).get("metadataDTO") or {}).get("childIds") or []
    except Exception:
        child_ids = []
    for cid in child_ids:
        d = _safe(lambda cc=cid: c.get_activity(cc)) or {}
        s = d.get("summaryDTO") or {}
        tk = (d.get("activityTypeDTO") or {}).get("typeKey")
        leg = {
            "activity_id": cid,
            "sport": _bucket(tk, (d.get("activityDTO") or {}).get("activityName")), "type_key": tk,
            "minutes": round((s.get("duration") or 0) / 60, 1),
            "km": round((s.get("distance") or 0) / 1000, 2) if s.get("distance") else None,
            "hr_avg": int(s["averageHR"]) if isinstance(s.get("averageHR"), (int, float)) else None,
            "hr_max": int(s["maxHR"]) if isinstance(s.get("maxHR"), (int, float)) else None,
            "avg_power_w": int(s["averagePower"]) if isinstance(s.get("averagePower"), (int, float)) else None,
        }
        # A real T1/T2 transition is short. A "transition" leg with sustained
        # distance is actually riding/running (e.g. the athlete kept biking after a
        # mis-formatted workout dropped them into T2 early) — reclassify by speed so
        # it counts toward the right sport's volume, not "other".
        if "transition" in (tk or "") and leg["km"] and leg["minutes"] >= 5 and leg["km"] >= 2:
            kmh = leg["km"] / (leg["minutes"] / 60)
            if kmh >= 18:
                leg["sport"], leg["ride_through"] = "bike", True
            elif kmh >= 7:
                leg["sport"], leg["ride_through"] = "run", True
        spd = s.get("averageSpeed")
        if leg["sport"] == "run" and isinstance(spd, (int, float)) and spd > 0:
            sec = 1000.0 / spd
            leg["pace_min_km"] = f"{int(sec // 60)}:{int(sec % 60):02d}"
        legs.append(leg)
    result = legs or None
    _MS_LEGS_CACHE[parent_id] = result
    return result


import re as _re
# Stretch / yoga / mobility work must NOT count as a strength session (a 15-min
# Peloton stretch shouldn't tick off a planned lift). Matched on activity name or
# type key, since these often sync from Garmin as "strength_training".
_MOBILITY_RE = _re.compile(r"\b(stretch|yoga|mobility|pilates|foam[\s-]?roll|flexibility|meditat|breathwork|warm[\s-]?up|cool[\s-]?down)\b", _re.I)


def _is_mobility(name: str | None, type_key: str | None) -> bool:
    return bool(_MOBILITY_RE.search(f"{name or ''} {type_key or ''}"))


def _bucket(type_key: str | None, name: str | None = None) -> str:
    tk = (type_key or "").lower()
    for bname, keys in _SPORT_BUCKET:
        if any(k in tk for k in keys):
            # A "strength" bucket that's really stretching/yoga → mobility instead.
            if bname == "strength" and _is_mobility(name, type_key):
                return "mobility"
            return bname
    if _is_mobility(name, type_key):
        return "mobility"
    return "other"


def get_recent_load(days: int = 14) -> dict[str, Any]:
    """Recent activities + weekly volume/load by discipline (ramp check)."""
    c = get_client()
    raw = _safe(lambda: c.get_activities(0, 100)) or []
    cutoff = _days_back(days)
    acts: list[dict[str, Any]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        start = (a.get("startTimeLocal") or "")[:10]
        if start < cutoff:
            continue
        tk = (a.get("activityType") or {}).get("typeKey")
        sport = _bucket(tk, a.get("activityName"))
        entry = {
            "date": start,
            "name": a.get("activityName"),
            "sport": sport,
            "type_key": tk,
            "km": round((a.get("distance") or 0) / 1000, 2) if a.get("distance") else None,
            "minutes": round((a.get("duration") or 0) / 60, 1),
            "hr_avg": int(a["averageHR"]) if isinstance(a.get("averageHR"), (int, float)) else None,
            "hr_max": int(a["maxHR"]) if isinstance(a.get("maxHR"), (int, float)) else None,
            "load": a.get("activityTrainingLoad"),
            "activity_id": a.get("activityId"),
        }
        # Sport-specific detail so the coach can grade the session vs its target.
        if sport == "run":
            spd = a.get("averageSpeed")
            if isinstance(spd, (int, float)) and spd > 0:
                sec = 1000.0 / spd
                entry["pace_min_km"] = f"{int(sec // 60)}:{int(sec % 60):02d}"
        if sport == "bike":
            pw = a.get("avgPower") or a.get("averagePower")
            if isinstance(pw, (int, float)):
                entry["avg_power_w"] = int(pw)
            npw = a.get("normPower") or a.get("normalizedPower")
            if isinstance(npw, (int, float)):
                entry["norm_power_w"] = int(npw)
        # Multisport (brick): don't record it as one "other" blob — break it into
        # its individual legs (bike, run, transition), each a first-class row with
        # its own sport, HR, distance and drill-down id, so volume-by-sport and the
        # coach both see the real segments.
        if "multi" in (tk or "").lower():
            legs = _multisport_legs(c, entry["activity_id"])
            if legs:
                pname = a.get("activityName") or "Multisport"
                for i, l in enumerate(legs, 1):
                    # reclassified ride/run-through → label by its real sport
                    if "transition" in (l.get("type_key") or "") and not l.get("ride_through"):
                        label = "transition"
                    elif l.get("ride_through"):
                        label = f"{l['sport']} (ride-through)"
                    else:
                        label = l["sport"]
                    leg_entry = {
                        "date": start, "name": f"{pname} · {label}",
                        "sport": l["sport"], "type_key": l["type_key"],
                        "km": l["km"], "minutes": l["minutes"],
                        "hr_avg": l["hr_avg"], "hr_max": l["hr_max"],
                        "load": None, "activity_id": l.get("activity_id"),
                        "multisport_parent": entry["activity_id"], "leg": i, "leg_label": label,
                    }
                    if l.get("avg_power_w"):
                        leg_entry["avg_power_w"] = l["avg_power_w"]
                    if l.get("pace_min_km"):
                        leg_entry["pace_min_km"] = l["pace_min_km"]
                    acts.append(leg_entry)
                continue  # skip the parent "other" row
        acts.append(entry)

    # Merge coach/manually-logged activities (e.g. "I biked this morning" before the
    # watch has synced). Dedup: if Garmin already has that sport on that day, assume
    # it's the same session and drop the manual one to avoid double-counting.
    try:
        from . import db
        garmin_keys = {(a["date"], a["sport"]) for a in acts}
        for m in db.get_manual_activities(cutoff, _today()):
            if (m["date"], m["sport"]) in garmin_keys:
                continue
            # A logged stretch/yoga is mobility, not the sport it was filed under.
            msport = "mobility" if _is_mobility(m.get("name"), m["sport"]) else m["sport"]
            acts.append({
                "date": m["date"], "name": m.get("name") or f"{m['sport']} (logged)",
                "sport": msport, "type_key": m["sport"],
                "km": m.get("km"), "minutes": m.get("minutes") or 0,
                "hr_avg": m.get("hr_avg"), "hr_max": None, "load": None,
                "activity_id": None, "manual": True, "manual_id": m["id"],
                "notes": m.get("notes"),
            })
    except Exception:
        pass

    acts.sort(key=lambda x: x["date"], reverse=True)

    by_sport: dict[str, Any] = {}
    for a in acts:
        b = by_sport.setdefault(a["sport"], {"sessions": 0, "km": 0.0, "hours": 0.0, "load": 0.0})
        b["sessions"] += 1
        b["km"] += a["km"] or 0
        b["hours"] += a["minutes"] / 60
        b["load"] += a["load"] or 0
    for b in by_sport.values():
        b["km"] = round(b["km"], 1)
        b["hours"] = round(b["hours"], 1)
        b["load"] = round(b["load"], 0)

    return {"period_days": days, "count": len(acts), "by_sport": by_sport, "activities": acts}
