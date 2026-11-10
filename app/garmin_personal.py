"""Garmin Connect Personal API: Garmin's official, read-only API for your own data.

Garmin opened this in October 2026. You create a key in Garmin Connect, trade it
for a bearer token that lasts about a day, and GET your own data with it. There
is no login to script, so these reads cannot break the way the unofficial
`garminconnect` login does when Garmin changes its sign-in.

It does not cover everything Coach uses. Garmin publishes sleep, HRV, Body
Battery, the daily summary (resting HR, stress, steps, intensity minutes), SpO2,
heart-rate zones, weight and the activity list. It does not publish Training
Readiness, Training Status, VO2max, lactate threshold, FTP, race predictions,
splits or per-second detail, and it cannot write, so a workout cannot be sent to
the watch with it. Those stay on `garminconnect`. So does the single-activity
read: the official record leaves out the id of the workout an activity was run
from, which interval analysis needs.

`HybridClient` therefore stands in for the `garminconnect` client. A read Garmin
publishes officially goes to the official API first and falls back to
`garminconnect` on any error; everything else goes straight to `garminconnect`.
Each adapter returns the shape the `garminconnect` method returns, so no caller
changes. With no `GARMIN_PERSONAL_API_KEY` set this module is never used.

The key and the bearer token are held in memory only and never appear in an
error message, a log line or `status()`.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import threading
import time
from typing import Any, Callable

import requests

from . import config

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_ENV_VAR = "GARMIN_PERSONAL_API_KEY"

_TIMEOUT_S = 10
# Garmin's own client swaps the token once less than an hour of it is left.
_REFRESH_WINDOW_S = 60 * 60
# A rejected key will be rejected again; stop asking for a while.
_AUTH_BACKOFF_S = 10 * 60
# After a timeout, a 5xx or a 429, skip the official API for a while so a slow
# Garmin does not make every read wait out the timeout before falling back.
_PAUSE_S = 60
_PAUSE_MAX_S = 15 * 60
# Resting HR, stress and the day's stats are one official record. Readiness asks
# for all three at once, so they share a single request.
_DAILY_TTL_S = 60

_log = logging.getLogger(__name__)
_session = requests.Session()

_token_lock = threading.Lock()
_token: dict[str, Any] = {"jwt": None, "exp": 0.0, "key": None}
# `_exchange` holds off the token exchange; `_pause` holds off every official call.
_exchange: dict[str, Any] = {"until": 0.0, "reason": None}
_pause: dict[str, Any] = {"until": 0.0, "reason": None}

_daily_lock = threading.Lock()
_daily_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_status_lock = threading.Lock()
_reads: dict[str, dict[str, Any]] = {}


class PersonalApiError(RuntimeError):
    """An official read failed. The message never contains the key or the token."""


def _key() -> str:
    return (config._get(KEY_ENV_VAR) or "").strip()


def enabled() -> bool:
    return bool(_key())


def _jwt_exp(jwt: str) -> float:
    """The token's own `exp` claim. Garmin returns a bare JWT, with no expiry beside it."""
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise PersonalApiError("token exchange did not return a JWT with an exp claim") from exc


def _hold(gate: dict[str, Any], seconds: float, reason: str) -> None:
    gate["until"] = time.time() + seconds
    gate["reason"] = reason


def _bearer(force: bool = False) -> str:
    """A usable bearer token, exchanging the key for a new one when needed."""
    key = _key()
    if not key:
        raise PersonalApiError(f"{KEY_ENV_VAR} is not set")
    with _token_lock:
        now = time.time()
        if _token["key"] != key:               # the key was replaced; its token is no use
            _token.update(jwt=None, exp=0.0, key=key)
            _exchange.update(until=0.0, reason=None)
        left = _token["exp"] - now
        # A token with time left still works when a renewal cannot be had.
        usable = bool(_token["jwt"]) and not force and left > 60
        if usable and left > _REFRESH_WINDOW_S:
            return _token["jwt"]
        if now < _exchange["until"]:
            if usable:
                return _token["jwt"]
            raise PersonalApiError(_exchange["reason"] or "token exchange is on hold")
        try:
            resp = _session.post(
                BASE_URL + TOKEN_PATH,
                headers={"Accept": "*/*", "Authorization": f"ApiKey {key}"},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            _hold(_exchange, _PAUSE_S, f"token exchange failed ({type(exc).__name__})")
            if usable:
                return _token["jwt"]
            raise PersonalApiError(_exchange["reason"]) from None
        if resp.status_code in (401, 403):
            _token.update(jwt=None, exp=0.0)
            _hold(_exchange, _AUTH_BACKOFF_S,
                  f"Garmin rejected the personal API key ({resp.status_code}): "
                  "it has expired or been revoked")
            raise PersonalApiError(_exchange["reason"])
        if not resp.ok:
            _hold(_exchange, _PAUSE_S, f"token exchange returned {resp.status_code}")
            if usable:
                return _token["jwt"]
            raise PersonalApiError(_exchange["reason"])
        jwt = (resp.text or "").strip()
        _token.update(jwt=jwt, exp=_jwt_exp(jwt))
        _exchange.update(until=0.0, reason=None)
        return jwt


def get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET one official endpoint. Returns parsed JSON, or None when Garmin has no data (204)."""
    if time.time() < _pause["until"]:
        raise PersonalApiError(_pause["reason"] or "official API is paused")
    for attempt in (0, 1):
        jwt = _bearer(force=attempt == 1)
        try:
            resp = _session.get(
                BASE_URL + path, params=params,
                headers={"Accept": "application/json", "Authorization": f"Bearer {jwt}"},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            _hold(_pause, _PAUSE_S, f"official API unreachable ({type(exc).__name__})")
            raise PersonalApiError(f"GET {path} failed ({type(exc).__name__})") from None
        if resp.status_code == 401 and attempt == 0:
            continue                           # token was revoked early; exchange again once
        if resp.status_code == 204:
            return None
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After") or _PAUSE_S)
            except (TypeError, ValueError):
                wait = _PAUSE_S
            _hold(_pause, max(1.0, min(wait, _PAUSE_MAX_S)), "Garmin rate-limited the official API")
            raise PersonalApiError(f"GET {path} was rate-limited (429)")
        if resp.status_code >= 500:
            _hold(_pause, _PAUSE_S, f"official API returned {resp.status_code}")
            raise PersonalApiError(f"GET {path} returned {resp.status_code}")
        if not resp.ok:
            raise PersonalApiError(f"GET {path} returned {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise PersonalApiError(f"GET {path} did not return JSON") from None
    raise PersonalApiError(f"GET {path} returned 401")


def _dict(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PersonalApiError(f"{what}: expected an object, got {type(value).__name__}")
    return value


def _list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PersonalApiError(f"{what}: expected a list, got {type(value).__name__}")
    return value


def _daily(cdate: str) -> dict[str, Any]:
    """The day's summary record, shared by resting HR, stress and stats."""
    with _daily_lock:
        hit = _daily_cache.get(cdate)
        if hit and time.time() - hit[0] < _DAILY_TTL_S:
            return hit[1]
        record = _dict(get("/usersummary-service/usersummary/daily", {"calendarDate": cdate}),
                       "daily summary")
        if len(_daily_cache) > 64:
            _daily_cache.clear()
        _daily_cache[cdate] = (time.time(), record)
        return record


def _note(name: str, source: str, error: Exception | None = None) -> None:
    with _status_lock:
        previous = _reads.get(name) or {}
        entry: dict[str, Any] = {
            "source": source,
            "at": config.local_now().isoformat(timespec="seconds"),
        }
        if error is not None:
            entry["error"] = str(error)[:200]
        _reads[name] = entry
    if error is not None and previous.get("error") != entry.get("error"):
        _log.warning("Garmin official read %s fell back to garminconnect: %s", name, entry["error"])


class HybridClient:
    """Stands in for the `garminconnect` client.

    The methods defined here are the reads Garmin publishes officially. Any other
    attribute is looked up on the real `garminconnect` client, which is only
    logged in the first time something needs it.
    """

    def __init__(self, legacy_factory: Callable[[], Any]) -> None:
        self._legacy_factory = legacy_factory

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._legacy_factory(), name)

    def _read(self, name: str, official: Callable[[], Any],
              legacy: Callable[[Any], Any]) -> Any:
        try:
            value = official()
        except Exception as exc:  # noqa: BLE001 - any official failure means "use the old path"
            _note(name, "garminconnect", exc)
            return legacy(self._legacy_factory())
        _note(name, "official")
        return value

    # --- wellness -------------------------------------------------------------
    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        def official() -> dict[str, Any]:
            dto = _dict(get("/sleep-service/sleep/dailySleep", {"date": cdate}), "sleep")
            # The official record is the DTO itself; garminconnect nests it.
            return dto if "dailySleepDTO" in dto else {"dailySleepDTO": dto}
        return self._read("sleep", official, lambda c: c.get_sleep_data(cdate))

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        def official() -> dict[str, Any] | None:
            return _dict(get(f"/hrv-service/hrv/{cdate}"), "hrv") or None
        return self._read("hrv", official, lambda c: c.get_hrv_data(cdate))

    def get_body_battery(self, startdate: str, enddate: str | None = None) -> list[Any]:
        end = enddate or startdate

        def official() -> list[Any]:
            return _list(get("/wellness-service/wellness/bodyBattery/reports/daily",
                             {"startDate": startdate, "endDate": end}), "body battery")
        return self._read("body_battery", official, lambda c: c.get_body_battery(startdate, end))

    def get_rhr_day(self, cdate: str) -> dict[str, Any]:
        def official() -> dict[str, Any]:
            day = _daily(cdate)
            value = day.get("restingHeartRate")
            rows = ([{"value": value, "calendarDate": day.get("calendarDate") or cdate}]
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
                    else [])
            return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": rows}}}
        return self._read("resting_hr", official, lambda c: c.get_rhr_day(cdate))

    def get_all_day_stress(self, cdate: str) -> dict[str, Any]:
        def level(value: Any) -> Any:
            # Garmin sends a negative number when it could not measure the day.
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            return value if ok else None

        def official() -> dict[str, Any]:
            day = _daily(cdate)
            return {"calendarDate": day.get("calendarDate"),
                    "avgStressLevel": level(day.get("averageStressLevel")),
                    "maxStressLevel": level(day.get("maxStressLevel"))}
        return self._read("stress", official, lambda c: c.get_all_day_stress(cdate))

    def get_spo2_data(self, cdate: str) -> dict[str, Any]:
        def official() -> dict[str, Any]:
            return _dict(get(f"/wellness-service/wellness/daily/spo2acclimation/{cdate}"), "spo2")
        return self._read("spo2", official, lambda c: c.get_spo2_data(cdate))

    def get_stats(self, cdate: str) -> dict[str, Any]:
        return self._read("daily_stats", lambda: _daily(cdate), lambda c: c.get_stats(cdate))

    def get_fitnessage_data(self, cdate: str) -> dict[str, Any]:
        def official() -> dict[str, Any]:
            # Published as a list, answered as an object; keep the newest record.
            raw = get(f"/fitnessage-service/fitnessage/v2/{cdate}")
            if isinstance(raw, dict):
                return raw
            rows = [row for row in _list(raw, "fitness age") if isinstance(row, dict)]
            return max(rows, key=lambda row: str(row.get("lastUpdated") or "")) if rows else {}
        return self._read("fitness_age", official, lambda c: c.get_fitnessage_data(cdate))

    # --- body -----------------------------------------------------------------
    def get_body_composition(self, startdate: str, enddate: str | None = None) -> dict[str, Any]:
        end = enddate or startdate

        def official() -> dict[str, Any]:
            data = _dict(get(f"/weight-service/weight/range/{startdate}/{end}",
                             {"includeAll": "true"}), "weight")
            entries: list[Any] = []
            for day in _list(data.get("dailyWeightSummaries"), "weight days"):
                if not isinstance(day, dict):
                    continue
                rows = day.get("allWeightMetrics") or []
                if not rows and isinstance(day.get("latestWeight"), dict):
                    rows = [day["latestWeight"]]
                entries.extend(r for r in rows if isinstance(r, dict))
            return {"dateWeightList": entries, "totalAverage": data.get("totalAverage")}
        return self._read("weight", official, lambda c: c.get_body_composition(startdate, end))

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        """Raw `garminconnect` request. Only the heart-rate zones read has an official twin."""
        if not kwargs and path.rstrip("/") == "/biometric-service/heartRateZones":
            def official() -> list[Any]:
                return _list(get("/biometric-service/heartRateZones/v2"), "heart-rate zones")
            return self._read("hr_zones", official, lambda c: c.connectapi(path))
        return self._legacy_factory().connectapi(path, **kwargs)

    # --- activities -----------------------------------------------------------
    def get_activities(self, start: int = 0, limit: int = 20,
                       activitytype: str | None = None) -> list[Any]:
        def official() -> list[Any]:
            params: dict[str, Any] = {"start": start, "limit": limit}
            if activitytype:
                params["activityType"] = activitytype
            return _list(get("/activitylist-service/activities/search/activities", params),
                         "activities")

        def legacy(c: Any) -> Any:
            return (c.get_activities(start, limit, activitytype) if activitytype
                    else c.get_activities(start, limit))
        return self._read("activities", official, legacy)


def status() -> dict[str, Any]:
    """What `/api/health` shows. Never includes the key or the token."""
    on = enabled()
    now = time.time()
    with _token_lock:
        exp = _token["exp"] if _token["jwt"] else 0.0
        paused = next((gate["reason"] for gate in (_exchange, _pause)
                       if now < gate["until"]), None)
    with _status_lock:
        reads = {name: dict(entry) for name, entry in sorted(_reads.items())}
    expires = (datetime.datetime.fromtimestamp(exp, tz=config.local_now().tzinfo)
               .isoformat(timespec="seconds") if exp else None)
    return {
        "enabled": on,
        "token_expires_at": expires if on else None,
        "paused": paused if on else None,
        "reads": reads if on else {},
    }


def _reset_for_tests() -> None:
    with _token_lock:
        _token.update(jwt=None, exp=0.0, key=None)
        _exchange.update(until=0.0, reason=None)
        _pause.update(until=0.0, reason=None)
    with _daily_lock:
        _daily_cache.clear()
    with _status_lock:
        _reads.clear()
