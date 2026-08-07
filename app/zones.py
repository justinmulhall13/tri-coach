"""The athlete's REAL physiological anchors, read from Garmin.

Everything that prescribes intensity — pushed watch workouts, the coach's
targets, zone-distribution analytics — resolves through here instead of
hardcoded guesses. Garmin stores per-sport HR zones (cycling sits lower than
running), a lactate-threshold HR/speed, and a cycling FTP; we read all three and
expose them as concrete bpm and pace ranges.

Cached for an hour — zones change when the athlete edits them on the watch, not
minute to minute.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import config

_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_TTL = 3600
_lock = threading.Lock()

# Fraction-of-threshold-pace bands per zone, used to turn LT pace into per-zone
# pace ranges for running. (Slower fraction, faster fraction.)
_PACE_BANDS = {
    1: (0.72, 0.80),   # recovery
    2: (0.80, 0.88),   # aerobic / easy
    3: (0.88, 0.95),   # tempo
    4: (0.95, 1.02),   # threshold
    5: (1.02, 1.12),   # VO2
}

# Fraction-of-FTP bands (indoor bike only — see rule: HR outdoors).
_FTP_BANDS = {1: (0.50, 0.60), 2: (0.60, 0.75), 3: (0.76, 0.90),
              4: (0.95, 1.03), 5: (1.06, 1.20)}

_INTENSITY_ZONE = (
    (("z1", "recovery", "easy spin"), 1),
    (("vo2", "anaerobic", "sprint"), 5),
    (("threshold", "css", "race pace", "race-specific"), 4),
    (("tempo",), 3),
    (("z2", "endurance", "easy", "aerobic", "long"), 2),
)


def zone_for(intensity: str, title: str = "") -> int:
    """Map an intensity/title phrase to a zone number (1-5)."""
    s = f"{intensity or ''} {title or ''}".lower()
    for keys, z in _INTENSITY_ZONE:
        if any(k in s for k in keys):
            return z
    return 2


def _sport_key(discipline: str) -> str:
    d = (discipline or "").lower()
    if d in ("bike", "cycling", "brick"):
        return "CYCLING"
    if d in ("swim", "pool_swim", "open_water_swim"):
        return "SWIMMING"
    return "DEFAULT"


def _fetch() -> dict[str, Any]:
    """Pull zones + thresholds from Garmin. Never raises."""
    out: dict[str, Any] = {"zones": {}, "lt_hr": None, "lt_speed_mps": None,
                           "ftp_w": None, "max_hr": None, "resting_hr": None,
                           "errors": []}
    try:
        from .garmin_source import get_client
        c = get_client()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"client: {type(e).__name__}")
        return out

    try:
        rows = c.connectapi("/biometric-service/heartRateZones/") or []
        for r in rows:
            sport = (r.get("sport") or "DEFAULT").upper()
            floors = [r.get(f"zone{i}Floor") for i in range(1, 6)]
            if not all(isinstance(f, (int, float)) for f in floors):
                continue
            mx = r.get("maxHeartRateUsed")
            # Zone i spans [floor_i, floor_{i+1}); zone 5 tops out at max HR.
            bands = {}
            for i in range(5):
                lo = int(floors[i])
                hi = int(floors[i + 1]) - 1 if i < 4 else int(mx or floors[4] + 15)
                bands[i + 1] = (lo, max(hi, lo + 1))
            out["zones"][sport] = bands
            if sport == "DEFAULT":
                out["max_hr"] = mx
                out["resting_hr"] = r.get("restingHeartRateUsed")
                out["lt_hr"] = r.get("lactateThresholdHeartRateUsed")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"zones: {type(e).__name__}")

    try:
        lt = c.get_lactate_threshold() or {}
        shr = lt.get("speed_and_heart_rate") or {}
        out["lt_hr"] = shr.get("heartRate") or out["lt_hr"]
        spd = shr.get("speed")
        if isinstance(spd, (int, float)) and spd > 0:
            # Garmin reports this scaled; a running LT sits ~2.5-6.0 m/s. Scale
            # up if it's obviously an order of magnitude low, then sanity-check.
            v = float(spd)
            while v < 1.5:
                v *= 10
            out["lt_speed_mps"] = round(v, 3) if 1.5 <= v <= 7.0 else None
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"lt: {type(e).__name__}")

    try:
        ftp = c.get_cycling_ftp() or {}
        out["ftp_w"] = ftp.get("functionalThresholdPower")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"ftp: {type(e).__name__}")

    return out


def get(force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _lock:
        if not force and _CACHE["data"] and (now - _CACHE["ts"]) < _TTL:
            return _CACHE["data"]
    data = _fetch()
    with _lock:
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
    return data


# --- Concrete targets ---------------------------------------------------------
def hr_range(intensity: str, discipline: str = "", title: str = "") -> tuple[int, int] | None:
    """Real bpm range for this intensity in this sport, from the athlete's own
    Garmin zones (cycling zones differ from running)."""
    d = get()
    z = zone_for(intensity, title)
    bands = d["zones"].get(_sport_key(discipline)) or d["zones"].get("DEFAULT")
    if not bands:
        return None
    return bands.get(z)


def pace_range(intensity: str, title: str = "") -> tuple[float, float] | None:
    """Run pace as (slow_sec_per_km, fast_sec_per_km) anchored on LT speed."""
    d = get()
    mps = d.get("lt_speed_mps")
    if not mps:
        return None
    thr_sec = 1000.0 / mps          # sec per km at threshold
    lo_f, hi_f = _PACE_BANDS[zone_for(intensity, title)]
    return (thr_sec / lo_f, thr_sec / hi_f)   # slower, faster


def watt_range(intensity: str, title: str = "") -> tuple[int, int] | None:
    """Indoor-bike watts. Only used for indoor/Peloton sessions — outdoor bike
    is prescribed by HR (the athlete's FTP is trainer-specific)."""
    d = get()
    ftp = d.get("ftp_w")
    if not ftp:
        return None
    lo, hi = _FTP_BANDS[zone_for(intensity, title)]
    return (round(ftp * lo), round(ftp * hi))


def fmt_pace(sec_per_km: float) -> str:
    return f"{int(sec_per_km // 60)}:{int(round(sec_per_km % 60)):02d}"


def summary() -> dict[str, Any]:
    """Human/coach-readable snapshot of the athlete's anchors."""
    d = get()
    out: dict[str, Any] = {
        "max_hr": d.get("max_hr"), "resting_hr": d.get("resting_hr"),
        "lt_hr": d.get("lt_hr"), "ftp_w": d.get("ftp_w"),
        "zones": d.get("zones"),
    }
    if d.get("lt_speed_mps"):
        out["threshold_pace_per_km"] = fmt_pace(1000.0 / d["lt_speed_mps"])
        out["pace_by_zone"] = {
            z: f"{fmt_pace(pace_range(k)[0])}–{fmt_pace(pace_range(k)[1])}/km"
            for z, k in ((1, "recovery"), (2, "easy"), (3, "tempo"),
                         (4, "threshold"), (5, "vo2")) if pace_range(k)
        }
    return out
