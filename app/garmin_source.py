"""Direct Garmin pull layer (built on the public `garminconnect` library).

Every value returned here comes from the Garmin API. When a metric is
unavailable we return None and record it in a `missing` list — we never
fabricate. Resting-HR, VO2 and HR-zone reads are resolved defensively against
the fields Garmin actually returns.
"""
from __future__ import annotations

import copy
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
_WEIGHT_CACHE_TTL_S = 300


def get_weight_kg() -> dict[str, Any] | None:
    """Latest dated Garmin weight entry, converted from grams to kilograms.

    A short cache avoids repeatedly scanning a year of body-composition data,
    while allowing a newly logged weight to reach Coach within five minutes.
    The athlete manually maintains this value in Garmin, so its provenance is
    self-reported via Garmin rather than a device measurement. ``None`` means no
    dated entry was available; an aggregate average is intentionally not
    presented as the latest weight.
    """
    import time
    now = time.time()
    # A fresh cached miss is meaningful too. Without this timestamp check, a
    # missing entry or transient Garmin error would trigger another 365-day scan
    # every time Coach and Fuel ask during the same request.
    if _WEIGHT_CACHE["ts"] > 0 and (now - _WEIGHT_CACHE["ts"]) < _WEIGHT_CACHE_TTL_S:
        return _WEIGHT_CACHE["val"]
    out = None
    try:
        c = get_client()
        bc = c.get_body_composition(_days_back(365), _today()) or {}
        entries = [e for e in (bc.get("dateWeightList") or [])
                   if e.get("weight") and e.get("calendarDate")]
        entries.sort(key=lambda e: e.get("calendarDate") or "")
        latest = entries[-1] if entries else None
        w = (latest or {}).get("weight")
        if isinstance(w, (int, float)) and w > 0:
            kg = round(w / 1000.0, 3)
            lb = round(kg * 2.2046, 1)
            out = {"kg": kg, "lb": lb,
                   "as_of": (latest or {}).get("calendarDate"),
                   "source": "self-reported", "provider": "Garmin",
                   "source_detail": "athlete-maintained Garmin weight entry",
                   "conversion": (f"{w:g} g / 1000 g/kg = {kg:g} kg; "
                                  f"{kg:g} kg x 2.2046 lb/kg = {lb:g} lb")}
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


def _iso_now() -> str:
    """Local fetch timestamp used to distinguish retrieval time from data time."""
    return config.local_now().isoformat(timespec="seconds")


def _as_local_datetime(value: Any) -> datetime.datetime | None:
    """Parse Garmin ISO or epoch timestamps into the configured local timezone."""
    if value is None or isinstance(value, bool):
        return None
    local_tz = config.local_now().tzinfo
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if abs(float(value)) > 10_000_000_000 else float(value)
        try:
            return datetime.datetime.fromtimestamp(seconds, tz=local_tz)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _source_date(record: dict[str, Any] | None) -> str | None:
    """Extract a provider date; never substitute the date that was queried."""
    if not isinstance(record, dict):
        return None
    for key in ("calendarDate", "date"):
        value = record.get(key)
        if isinstance(value, datetime.date):
            return value.isoformat()
        if value:
            try:
                return datetime.date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                pass
    for key in ("timestampLocal", "timestamp", "lastUpdatedTimestamp"):
        parsed = _as_local_datetime(record.get(key))
        if parsed is not None:
            return parsed.date().isoformat()
    return None


def _source_timestamp(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in ("timestampLocal", "timestamp", "lastUpdatedTimestamp"):
        value = record.get(key)
        parsed = _as_local_datetime(value)
        if parsed is not None:
            return parsed.isoformat(timespec="seconds")
    return None


def _freshness(*, source_date: str | None, expected_date: str, fetched_at: str,
               endpoint: str, source_timestamp: str | None = None) -> dict[str, Any]:
    if source_date == expected_date:
        state, reason = "current", "Garmin source date matches the local date"
    elif source_date:
        state, reason = "stale", f"Garmin returned {source_date} for local date {expected_date}"
    else:
        state, reason = "unknown", "Garmin did not provide a verifiable source date"
    result: dict[str, Any] = {
        "state": state,
        "is_current": state == "current",
        "source_date": source_date,
        "expected_date": expected_date,
        "source_timestamp": source_timestamp,
        "fetched_at": fetched_at,
        "provider": "Garmin",
        "endpoint": endpoint,
        "reason": reason,
    }
    source_dt = _as_local_datetime(source_timestamp)
    fetched_dt = _as_local_datetime(fetched_at)
    if source_dt is not None and fetched_dt is not None:
        result["age_minutes_at_fetch"] = round(max(0.0, (fetched_dt - source_dt).total_seconds()) / 60, 1)
    return result


def _record_sort_key(record: dict[str, Any]) -> tuple[str, float, int]:
    stamp = _as_local_datetime(
        record.get("timestampLocal") or record.get("timestamp") or record.get("lastUpdatedTimestamp")
    )
    return (_source_date(record) or "", stamp.timestamp() if stamp else 0.0,
            1 if record.get("primaryTrainingDevice") else 0)


def _newest_record(mapping: Any) -> dict[str, Any]:
    values = list(mapping.values()) if isinstance(mapping, dict) else (
        mapping if isinstance(mapping, list) else []
    )
    rows = [row for row in values if isinstance(row, dict)]
    return max(rows, key=_record_sort_key) if rows else {}


def get_readiness() -> dict[str, Any]:
    """This-morning recovery panel. Each field tagged; missing → listed."""
    c = get_client()
    today, yest = _today(), _days_back(1)
    fetched_at = _iso_now()
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
    # tagged with an inputContext. Garmin may add a lower post-workout reset,
    # but the endpoint can lag activity sync. We split and date-check them:
    #   • "Recovery"  = the post-sleep wake-up value (stable for the day, WHOOP-style)
    #   • "current"   = the most recent reading (reflects post-exercise depletion)
    tr_raw = f_tr.result()
    tr_list = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if isinstance(tr_raw, dict) else [])
    tr_list = [x for x in tr_list if isinstance(x, dict) and x.get("score") is not None]
    # The endpoint is date-scoped, but Garmin can briefly return its last known
    # record while a new day or post-workout reset is still syncing. Only a
    # provider-dated record for the requested local day is eligible as current.
    current_day = [x for x in tr_list if _source_date(x) == today]
    wake = next((x for x in current_day if x.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
    if wake is None and current_day:                   # fallback: the day's peak reading
        wake = max(current_day, key=lambda x: x.get("score") or 0)
    current = max(current_day, key=_record_sort_key) if current_day else {}
    wake = wake or {}
    fallback = max(tr_list, key=_record_sort_key) if tr_list else {}
    wake_source = wake or fallback
    current_source = current or fallback
    wake_date = _source_date(wake_source)
    current_date = _source_date(current_source)
    wake_timestamp = _source_timestamp(wake_source)
    current_timestamp = _source_timestamp(current_source)
    # `training_readiness` is the MORNING recovery — the value that should drive
    # training decisions and displays ("how recovered you woke up").
    readiness = {
        "score": wake.get("score"),
        "level": wake.get("level"),
        "feedback": wake.get("feedbackShort"),
        "sleep_score_factor_pct": wake.get("sleepScoreFactorPercent"),
        "as_of": wake_timestamp,
        "source_date": wake_date,
        "freshness": _freshness(
            source_date=wake_date, expected_date=today, fetched_at=fetched_at,
            endpoint="training-readiness", source_timestamp=wake_timestamp,
        ),
    }
    current_readiness = {
        "score": current.get("score"),
        "level": current.get("level"),
        "feedback": current.get("feedbackShort"),
        "as_of": current_timestamp,
        "source_date": current_date,
        "input_context": current.get("inputContext"),
        "is_post_exercise": current.get("inputContext") == "AFTER_POST_EXERCISE_RESET",
        "freshness": _freshness(
            source_date=current_date, expected_date=today, fetched_at=fetched_at,
            endpoint="training-readiness", source_timestamp=current_timestamp,
        ),
    }
    if readiness["score"] is None:
        missing.append("training_readiness")
    if current_readiness["score"] is None:
        missing.append("current_readiness")

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
        "fetched_at": fetched_at,
        "source": "measured",
        "provider": "Garmin",
        "training_readiness": readiness,      # morning post-sleep recovery (stable)
        "current_readiness": current_readiness,  # latest snapshot; post-exercise when verified
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
    today = _today()
    fetched_at = _iso_now()
    raw = _safe(lambda: c.get_training_status(today))
    out: dict[str, Any] = {"acute_load": None, "chronic_load": None, "load_ratio": None,
                           "acwr_status": None, "load_focus": None,
                           "source": "measured", "provider": "Garmin",
                           "fetched_at": fetched_at}
    if not isinstance(raw, dict):
        out["error"] = "training status unavailable (needs ~7 days on a compatible device)"
        out["freshness"] = _freshness(
            source_date=None, expected_date=today, fetched_at=fetched_at,
            endpoint="training-status",
        )
        out["load_focus_freshness"] = _freshness(
            source_date=None, expected_date=today, fetched_at=fetched_at,
            endpoint="training-load-balance",
        )
        return out
    status_record: dict[str, Any] = {}
    try:
        latest = (raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
        status_record = _newest_record(latest)
        atl = status_record.get("acuteTrainingLoadDTO") or {}
        out.update({
            "acute_load": atl.get("dailyTrainingLoadAcute"),
            "chronic_load": atl.get("dailyTrainingLoadChronic"),
            "load_ratio": atl.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": atl.get("acwrStatus"),
        })
    except Exception:
        pass
    status_date = _source_date(status_record)
    status_timestamp = _source_timestamp(status_record)
    out["as_of"] = status_date
    out["source_timestamp"] = status_timestamp
    out["freshness"] = _freshness(
        source_date=status_date, expected_date=today, fetched_at=fetched_at,
        endpoint="training-status", source_timestamp=status_timestamp,
    )
    focus_record: dict[str, Any] = {}
    try:
        bal = (raw.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        focus_record = _newest_record(bal)
        if focus_record:
            out["load_focus"] = {
                "aerobic_low": focus_record.get("monthlyLoadAerobicLow"),
                "aerobic_high": focus_record.get("monthlyLoadAerobicHigh"),
                "anaerobic": focus_record.get("monthlyLoadAnaerobic"),
                "aerobic_low_target": [focus_record.get("monthlyLoadAerobicLowTargetMin"), focus_record.get("monthlyLoadAerobicLowTargetMax")],
                "aerobic_high_target": [focus_record.get("monthlyLoadAerobicHighTargetMin"), focus_record.get("monthlyLoadAerobicHighTargetMax")],
                "anaerobic_target": [focus_record.get("monthlyLoadAnaerobicTargetMin"), focus_record.get("monthlyLoadAnaerobicTargetMax")],
                "feedback": focus_record.get("trainingBalanceFeedbackPhrase"),
            }
    except Exception:
        pass
    focus_date = _source_date(focus_record)
    focus_timestamp = _source_timestamp(focus_record)
    out["load_focus_as_of"] = focus_date
    out["load_focus_freshness"] = _freshness(
        source_date=focus_date, expected_date=today, fetched_at=fetched_at,
        endpoint="training-load-balance", source_timestamp=focus_timestamp,
    )
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


def _start_delta_minutes(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Absolute start delta, or None when either provider omitted local time."""
    left, right = a.get("start_local"), b.get("start_local")
    if not left or not right:
        return None
    try:
        la = datetime.datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        rb = datetime.datetime.fromisoformat(str(right).replace("Z", "+00:00"))
        return abs((la - rb).total_seconds()) / 60
    except (TypeError, ValueError):
        return None


def _same_synced_session(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Conservative duplicate test for one workout synced by two recorders.

    Same-day doubles remain separate unless their start times and physiology are
    nearly identical. If start time is missing, require the normalized name too.
    """
    if a.get("manual") or b.get("manual"):
        return False
    if a.get("date") != b.get("date") or a.get("sport") != b.get("sport"):
        return False
    if a.get("multisport_parent") != b.get("multisport_parent"):
        return False

    start_delta = _start_delta_minutes(a, b)
    if start_delta is not None and start_delta > 15:
        return False
    if start_delta is None:
        normalize = lambda value: _re.sub(r"\W+", "", str(value or "").lower())
        if normalize(a.get("name")) != normalize(b.get("name")):
            return False

    duration_a, duration_b = float(a.get("minutes") or 0), float(b.get("minutes") or 0)
    if max(duration_a, duration_b) <= 0:
        return False
    if abs(duration_a - duration_b) > max(2.0, 0.06 * max(duration_a, duration_b)):
        return False

    comparable = 1  # duration
    km_a, km_b = a.get("km"), b.get("km")
    if isinstance(km_a, (int, float)) and isinstance(km_b, (int, float)):
        comparable += 1
        if abs(km_a - km_b) > max(0.3, 0.04 * max(km_a, km_b, 1)):
            return False
    hr_a, hr_b = a.get("hr_avg"), b.get("hr_avg")
    if isinstance(hr_a, (int, float)) and isinstance(hr_b, (int, float)):
        comparable += 1
        if abs(hr_a - hr_b) > 3:
            return False
    max_a, max_b = a.get("hr_max"), b.get("hr_max")
    if isinstance(max_a, (int, float)) and isinstance(max_b, (int, float)):
        comparable += 1
        if abs(max_a - max_b) > 4:
            return False
    return comparable >= 2 and (start_delta is not None or comparable >= 3)


def _dedupe_synced_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse confidently duplicated provider rows and retain an audit marker."""
    unique: list[dict[str, Any]] = []
    for activity in activities:
        existing = next((row for row in unique if _same_synced_session(row, activity)), None)
        if existing is None:
            unique.append(activity)
            continue
        existing["deduplicated_sync_count"] = int(existing.get("deduplicated_sync_count") or 1) + 1
        ids = existing.setdefault("deduplicated_activity_ids", [existing.get("activity_id")])
        if activity.get("activity_id") not in ids:
            ids.append(activity.get("activity_id"))
        names = existing.setdefault("deduplicated_names", [existing.get("name")])
        if activity.get("name") and activity.get("name") not in names:
            names.append(activity.get("name"))
        # Keep the richer copy without ever adding the duplicated load/volume.
        for key in ("km", "hr_avg", "hr_max", "load", "avg_power_w", "norm_power_w",
                    "pace_min_km", "start_local", "activity_end_local"):
            if existing.get(key) is None and activity.get(key) is not None:
                existing[key] = activity[key]
    return unique


def _activity_end_local(start_local: Any, duration_seconds: Any) -> str | None:
    start = _as_local_datetime(start_local)
    if start is None or not isinstance(duration_seconds, (int, float)) or duration_seconds < 0:
        return None
    return (start + datetime.timedelta(seconds=float(duration_seconds))).isoformat(timespec="seconds")


def _activity_revision(activities: list[dict[str, Any]]) -> str:
    """Stable fingerprint of the returned activity set, excluding fetch time."""
    import hashlib
    import json
    rows = sorted((
        str(a.get("activity_id") or a.get("manual_id") or ""),
        str(a.get("date") or ""),
        str(a.get("start_local") or ""),
        str(a.get("sport") or ""),
        str(a.get("minutes") or ""),
        str(a.get("km") or ""),
        str(a.get("load") or ""),
    ) for a in activities)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:20]


def _latest_garmin_activity(activities: list[dict[str, Any]], fetched_at: str) -> dict[str, Any] | None:
    rows = [a for a in activities if a.get("provider") == "Garmin" and a.get("date")]
    if not rows:
        return None
    latest = max(rows, key=lambda a: (
        _as_local_datetime(a.get("activity_end_local") or a.get("start_local"))
        or datetime.datetime.min.replace(tzinfo=config.local_now().tzinfo),
        str(a.get("activity_id") or ""),
    ))
    return {
        "activity_id": latest.get("activity_id"),
        "date": latest.get("date"),
        "start_local": latest.get("start_local"),
        "end_local": latest.get("activity_end_local"),
        "sport": latest.get("sport"),
        "observed_at": fetched_at,
        "source": "measured",
        "provider": "Garmin",
    }


def get_recent_load(days: int = 14) -> dict[str, Any]:
    """Recent activities + weekly volume/load by discipline (ramp check)."""
    c = get_client()
    fetched_at = _iso_now()
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
            "start_local": a.get("startTimeLocal"),
            "activity_end_local": _activity_end_local(
                a.get("startTimeLocal"), a.get("elapsedDuration") or a.get("duration")
            ),
            "name": a.get("activityName"),
            "sport": sport,
            "type_key": tk,
            "km": round((a.get("distance") or 0) / 1000, 2) if a.get("distance") else None,
            "minutes": round((a.get("duration") or 0) / 60, 1),
            "hr_avg": int(a["averageHR"]) if isinstance(a.get("averageHR"), (int, float)) else None,
            "hr_max": int(a["maxHR"]) if isinstance(a.get("maxHR"), (int, float)) else None,
            "load": a.get("activityTrainingLoad"),
            "activity_id": a.get("activityId"),
            "source": "measured", "provider": "Garmin",
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
                        "date": start, "start_local": a.get("startTimeLocal"),
                        # Child summaries do not always carry independent local
                        # starts. The parent end is still sufficient to prove
                        # that a pre-race/pre-workout metric has been superseded.
                        "activity_end_local": entry.get("activity_end_local"),
                        "name": f"{pname} · {label}",
                        "sport": l["sport"], "type_key": l["type_key"],
                        "km": l["km"], "minutes": l["minutes"],
                        "hr_avg": l["hr_avg"], "hr_max": l["hr_max"],
                        "load": None, "activity_id": l.get("activity_id"),
                        "multisport_parent": entry["activity_id"], "leg": i, "leg_label": label,
                        "source": "measured", "provider": "Garmin",
                    }
                    if l.get("avg_power_w"):
                        leg_entry["avg_power_w"] = l["avg_power_w"]
                    if l.get("pace_min_km"):
                        leg_entry["pace_min_km"] = l["pace_min_km"]
                    acts.append(leg_entry)
                continue  # skip the parent "other" row
        acts.append(entry)

    acts = _dedupe_synced_activities(acts)

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
                "activity_end_local": None,
                "notes": m.get("notes"),
                "source": "self-reported", "provider": None,
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

    return {
        "period_days": days,
        "count": len(acts),
        "by_sport": by_sport,
        "activities": acts,
        "fetched_at": fetched_at,
        "source": "measured and self-reported where tagged per activity",
        "activity_revision": _activity_revision(acts),
        "latest_garmin_activity": _latest_garmin_activity(acts, fetched_at),
    }


def _check_metric_after_activity(metric: dict[str, Any], latest: dict[str, Any] | None,
                                 *, expected_date: str) -> None:
    """Downgrade a same-day metric when a newer Garmin activity supersedes it."""
    freshness = metric.get("freshness") if isinstance(metric, dict) else None
    if not isinstance(freshness, dict) or not latest:
        return
    source_date = freshness.get("source_date")
    activity_date = latest.get("date")
    if source_date and activity_date and str(source_date) < str(activity_date):
        freshness.update({
            "state": "stale", "is_current": False,
            "reason": f"A newer Garmin activity exists on {activity_date}",
            "superseded_by_activity": latest,
        })
        return
    if source_date != expected_date or activity_date != expected_date:
        return
    metric_dt = _as_local_datetime(freshness.get("source_timestamp"))
    activity_dt = _as_local_datetime(latest.get("end_local") or latest.get("start_local"))
    if activity_dt is None:
        return
    if metric_dt is None:
        freshness.update({
            "state": "unknown", "is_current": False,
            "reason": "A same-day Garmin activity exists but this metric has no timestamp to verify it includes that activity",
            "superseded_by_activity": latest,
        })
    elif metric_dt < activity_dt:
        freshness.update({
            "state": "stale", "is_current": False,
            "reason": "The Garmin metric predates the latest synced activity",
            "superseded_by_activity": latest,
        })


def reconcile_freshness(readiness: dict[str, Any] | None,
                        training_load: dict[str, Any] | None,
                        recent_load: dict[str, Any] | None
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cross-check live-looking metrics against the newest synced activity.

    Morning readiness deliberately stays valid as the dated wake-up baseline.
    Only the latest/current readiness snapshot and load metrics are downgraded
    when a later workout means Garmin has not recalculated them yet.
    """
    rd = copy.deepcopy(readiness) if isinstance(readiness, dict) else {}
    tl = copy.deepcopy(training_load) if isinstance(training_load, dict) else {}
    load = recent_load if isinstance(recent_load, dict) else {}
    latest = load.get("latest_garmin_activity")
    if not isinstance(latest, dict):
        latest = _latest_garmin_activity(load.get("activities") or [], load.get("fetched_at") or _iso_now())
    today = _today()
    _check_metric_after_activity(rd.get("current_readiness") or {}, latest, expected_date=today)
    _check_metric_after_activity({"freshness": tl.get("freshness")}, latest, expected_date=today)
    _check_metric_after_activity({"freshness": tl.get("load_focus_freshness")}, latest,
                                 expected_date=today)
    check = {
        "checked_at": _iso_now(),
        "latest_garmin_activity": latest,
        "policy": (
            "Morning readiness remains the wake-up baseline; current readiness and training "
            "load must not predate the latest synced Garmin activity."
        ),
    }
    rd["freshness_check"] = check
    tl["freshness_check"] = check
    return rd, tl


def current_training_load(training_load: dict[str, Any] | None) -> dict[str, Any]:
    """Return only load fields verified current enough for coaching decisions."""
    out = copy.deepcopy(training_load) if isinstance(training_load, dict) else {}
    if (out.get("freshness") or {}).get("is_current") is not True:
        for key in ("acute_load", "chronic_load", "load_ratio", "acwr_status"):
            out[key] = None
    if (out.get("load_focus_freshness") or {}).get("is_current") is not True:
        out["load_focus"] = None
    return out
