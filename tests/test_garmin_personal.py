"""Garmin's official Personal API serves the reads it publishes; garminconnect the rest.

No test here touches the network: the HTTP session is replaced with a script of
canned responses shaped like Garmin's published OpenAPI schemas.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from app import config, garmin_personal as gp, garmin_source, main, zones

_KEY = "test-key-not-a-real-one"
_TODAY = datetime.date(2026, 11, 9)


def _jwt(seconds_left: float = 24 * 3600) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': time.time() + seconds_left})}.sig"


class _Resp:
    def __init__(self, status: int = 200, body=None, text: str | None = None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    """Answers GETs from a path -> response table and counts token exchanges."""

    def __init__(self, routes: dict | None = None, token=None):
        self.routes = routes or {}
        self.token = token or (lambda: _Resp(text=_jwt()))
        self.posts: list[dict] = []
        self.gets: list[tuple[str, dict | None, dict]] = []

    def post(self, url, headers=None, timeout=None):
        self.posts.append({"url": url, "headers": headers})
        out = self.token()
        if isinstance(out, Exception):
            raise out
        return out

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.replace(gp.BASE_URL, "")
        self.gets.append((path, params, headers))
        out = self.routes.get(path, _Resp(404))
        if callable(out):
            out = out()
        if isinstance(out, Exception):
            raise out
        return out


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        gp._reset_for_tests()
        env = patch.dict(os.environ, {gp.KEY_ENV_VAR: _KEY})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(gp._reset_for_tests)

    def use(self, session: _Session) -> _Session:
        p = patch.object(gp, "_session", session)
        p.start()
        self.addCleanup(p.stop)
        return session


class TokenTests(_Base):
    def test_exchange_sends_the_key_once_and_reuses_the_token(self) -> None:
        s = self.use(_Session({"/hrv-service/hrv/2026-11-09": _Resp(body={"hrvSummary": {}})}))
        gp.get("/hrv-service/hrv/2026-11-09")
        gp.get("/hrv-service/hrv/2026-11-09")
        self.assertEqual(len(s.posts), 1)
        self.assertEqual(s.posts[0]["url"], gp.BASE_URL + gp.TOKEN_PATH)
        self.assertEqual(s.posts[0]["headers"]["Authorization"], f"ApiKey {_KEY}")
        self.assertTrue(s.gets[0][2]["Authorization"].startswith("Bearer "))
        self.assertNotIn(_KEY, s.gets[0][2]["Authorization"])

    def test_token_inside_the_refresh_window_is_renewed(self) -> None:
        tokens = iter([_Resp(text=_jwt(30 * 60)), _Resp(text=_jwt())])
        s = self.use(_Session({"/x": _Resp(body={})}, token=lambda: next(tokens)))
        gp.get("/x")
        gp.get("/x")
        self.assertEqual(len(s.posts), 2)

    def test_still_valid_token_is_used_when_renewal_fails(self) -> None:
        tokens = iter([_Resp(text=_jwt(30 * 60)), requests.ConnectionError("down")])
        s = self.use(_Session({"/x": _Resp(body={"ok": 1})}, token=lambda: next(tokens)))
        gp.get("/x")
        self.assertEqual(gp.get("/x"), {"ok": 1})
        self.assertEqual(len(s.posts), 2)

    def test_rejected_key_is_not_retried_and_never_leaks(self) -> None:
        s = self.use(_Session(token=lambda: _Resp(401, text=f"bad key {_KEY}")))
        for _ in range(3):
            with self.assertRaises(gp.PersonalApiError) as ctx:
                gp.get("/x")
            self.assertNotIn(_KEY, str(ctx.exception))
        self.assertEqual(len(s.posts), 1)
        self.assertEqual(s.gets, [])
        status = gp.status()
        self.assertIn("expired or been revoked", status["paused"])
        self.assertNotIn(_KEY, json.dumps(status))

    def test_replacing_the_key_clears_the_hold(self) -> None:
        s = self.use(_Session({"/x": _Resp(body={})}, token=lambda: _Resp(401)))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")
        s.token = lambda: _Resp(text=_jwt())
        with patch.dict(os.environ, {gp.KEY_ENV_VAR: "a-new-key"}):
            self.assertEqual(gp.get("/x"), {})
        self.assertEqual(s.posts[-1]["headers"]["Authorization"], "ApiKey a-new-key")

    def test_token_without_exp_is_refused(self) -> None:
        self.use(_Session(token=lambda: _Resp(text="not-a-jwt")))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")

    def test_no_key_means_disabled(self) -> None:
        with patch.dict(os.environ, {gp.KEY_ENV_VAR: "  "}):
            self.assertFalse(gp.enabled())
            self.assertEqual(gp.status(), {"enabled": False, "token_expires_at": None,
                                           "paused": None, "reads": {}})
            with self.assertRaises(gp.PersonalApiError):
                gp.get("/x")


class GetTests(_Base):
    def test_204_is_no_data(self) -> None:
        self.use(_Session({"/x": _Resp(204)}))
        self.assertIsNone(gp.get("/x"))

    def test_401_forces_one_new_exchange(self) -> None:
        answers = iter([_Resp(401), _Resp(body={"ok": True})])
        s = self.use(_Session({"/x": lambda: next(answers)}))
        self.assertEqual(gp.get("/x"), {"ok": True})
        self.assertEqual(len(s.posts), 2)

    def test_second_401_gives_up(self) -> None:
        s = self.use(_Session({"/x": _Resp(401)}))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")
        self.assertEqual(len(s.gets), 2)

    def test_rate_limit_pauses_every_official_call(self) -> None:
        s = self.use(_Session({"/x": _Resp(429, headers={"Retry-After": "120"}),
                               "/y": _Resp(body={})}))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/y")
        self.assertEqual(len(s.gets), 1)
        self.assertEqual(gp.status()["paused"], "Garmin rate-limited the official API")

    def test_timeout_pauses_so_later_reads_fall_back_at_once(self) -> None:
        s = self.use(_Session({"/x": requests.Timeout("slow")}))
        for _ in range(3):
            with self.assertRaises(gp.PersonalApiError):
                gp.get("/x")
        self.assertEqual(len(s.gets), 1)

    def test_a_4xx_on_one_path_does_not_pause_the_rest(self) -> None:
        s = self.use(_Session({"/x": _Resp(400), "/y": _Resp(body={"ok": 1})}))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")
        self.assertEqual(gp.get("/y"), {"ok": 1})
        self.assertEqual(len(s.gets), 2)

    def test_non_json_body_is_an_error(self) -> None:
        self.use(_Session({"/x": _Resp(200, text="<html>")}))
        with self.assertRaises(gp.PersonalApiError):
            gp.get("/x")


_DAY = "2026-11-09"
_SUMMARY = {
    "calendarDate": _DAY, "restingHeartRate": 47, "averageStressLevel": 28,
    "maxStressLevel": 91, "totalSteps": 8123, "dailyStepGoal": 9000,
    "moderateIntensityMinutes": 12, "vigorousIntensityMinutes": 40,
    "activeKilocalories": 812.0, "floorsAscended": 6.0,
}
_SLEEP = {
    "calendarDate": _DAY, "sleepTimeSeconds": 27000, "deepSleepSeconds": 5400,
    "remSleepSeconds": 6300, "lightSleepSeconds": 15300, "awakeSleepSeconds": 600,
    "avgSleepStress": 14.0, "sleepScores": {"overall": {"value": 84, "qualifierKey": "GOOD"}},
    "sleepNeed": {"baseline": 480, "actual": 470, "feedback": "NO_CHANGE"},
}
_ROUTES = {
    "/usersummary-service/usersummary/daily": _Resp(body=_SUMMARY),
    "/sleep-service/sleep/dailySleep": _Resp(body=_SLEEP),
    f"/hrv-service/hrv/{_DAY}": _Resp(body={"hrvSummary": {
        "calendarDate": _DAY, "lastNightAvg": 71, "weeklyAvg": 68, "status": "BALANCED"}}),
    "/wellness-service/wellness/bodyBattery/reports/daily": _Resp(body=[
        {"date": "2026-11-08", "charged": 50, "drained": 60, "bodyBatteryValuesArray": [[1, 40]]},
        {"date": _DAY, "charged": 62, "drained": 21,
         "bodyBatteryValuesArray": [[1, 35], [2, 88], [3, 74]]}]),
    f"/wellness-service/wellness/daily/spo2acclimation/{_DAY}": _Resp(body={
        "calendarDate": _DAY, "averageSpO2": 96.0, "lowestSpO2": 91}),
    "/biometric-service/heartRateZones/v2": _Resp(body=[{
        "sport": "DEFAULT", "zone1Floor": 95, "zone2Floor": 114, "zone3Floor": 133,
        "zone4Floor": 152, "zone5Floor": 171, "maxHeartRateUsed": 190,
        "restingHeartRateUsed": 47, "lactateThresholdHeartRateUsed": 168}]),
    "/activitylist-service/activities/search/activities": _Resp(body=[{
        "activityId": 1, "activityName": "Morning Run", "startTimeLocal": f"{_DAY} 07:00:00",
        "activityType": {"typeKey": "running"}, "distance": 10000.0, "duration": 3000.0,
        "averageHR": 148.0, "maxHR": 171.0, "activityTrainingLoad": 142.5,
        "averageSpeed": 3.333}]),
}


class AdapterTests(_Base):
    """Each adapter returns what the garminconnect method of the same name returns."""

    def setUp(self) -> None:
        super().setUp()
        self.session = self.use(_Session(dict(_ROUTES)))
        self.legacy = MagicMock(name="garminconnect")
        self.factory = MagicMock(return_value=self.legacy)
        self.c = gp.HybridClient(self.factory)

    def test_sleep_is_nested_the_way_callers_read_it(self) -> None:
        out = self.c.get_sleep_data(_DAY)
        self.assertEqual(out["dailySleepDTO"]["sleepTimeSeconds"], 27000)
        self.assertEqual(out["dailySleepDTO"]["sleepNeed"]["baseline"], 480)
        self.assertEqual(self.session.gets[-1][1], {"date": _DAY})
        self.factory.assert_not_called()

    def test_already_nested_sleep_is_left_alone(self) -> None:
        self.session.routes["/sleep-service/sleep/dailySleep"] = _Resp(
            body={"dailySleepDTO": _SLEEP})
        self.assertEqual(self.c.get_sleep_data(_DAY), {"dailySleepDTO": _SLEEP})

    def test_no_sleep_yet_is_an_empty_record_not_a_fallback(self) -> None:
        self.session.routes["/sleep-service/sleep/dailySleep"] = _Resp(204)
        self.assertEqual(self.c.get_sleep_data(_DAY), {"dailySleepDTO": {}})
        self.factory.assert_not_called()

    def test_hrv(self) -> None:
        self.assertEqual(self.c.get_hrv_data(_DAY)["hrvSummary"]["lastNightAvg"], 71)

    def test_body_battery_passes_the_range(self) -> None:
        out = self.c.get_body_battery("2026-11-08", _DAY)
        self.assertEqual(out[-1]["charged"], 62)
        self.assertEqual(self.session.gets[-1][1],
                         {"startDate": "2026-11-08", "endDate": _DAY})

    def test_resting_hr_stress_and_stats_share_one_request(self) -> None:
        rhr = self.c.get_rhr_day(_DAY)
        self.assertEqual(
            rhr["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"], 47)
        self.assertEqual(self.c.get_all_day_stress(_DAY),
                         {"calendarDate": _DAY, "avgStressLevel": 28, "maxStressLevel": 91})
        self.assertEqual(self.c.get_stats(_DAY)["vigorousIntensityMinutes"], 40)
        daily = [g for g in self.session.gets if g[0].endswith("/usersummary/daily")]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0][1], {"calendarDate": _DAY})

    def test_unmeasured_day_reports_nothing_rather_than_a_number(self) -> None:
        self.session.routes["/usersummary-service/usersummary/daily"] = _Resp(body={
            "calendarDate": _DAY, "restingHeartRate": None,
            "averageStressLevel": -1, "maxStressLevel": -2})
        self.assertEqual(
            self.c.get_rhr_day(_DAY)["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"], [])
        stress = self.c.get_all_day_stress(_DAY)
        self.assertIsNone(stress["avgStressLevel"])
        self.assertIsNone(stress["maxStressLevel"])

    def test_spo2(self) -> None:
        self.assertEqual(self.c.get_spo2_data(_DAY)["lowestSpO2"], 91)

    def test_fitness_age_accepts_the_object_garmin_really_sends(self) -> None:
        path = f"/fitnessage-service/fitnessage/v2/{_DAY}"
        self.session.routes[path] = _Resp(body={"invalidReason": "USER_UNDER_AGE"})
        self.assertEqual(self.c.get_fitnessage_data(_DAY), {"invalidReason": "USER_UNDER_AGE"})
        self.session.routes[path] = _Resp(body=[
            {"fitnessAge": 30.0, "lastUpdated": "2026-10-01"},
            {"fitnessAge": 27.4, "lastUpdated": "2026-11-08"}])
        self.assertEqual(self.c.get_fitnessage_data(_DAY)["fitnessAge"], 27.4)
        self.factory.assert_not_called()

    def test_fitness_age_falls_back_when_the_official_read_fails(self) -> None:
        self.session.routes[f"/fitnessage-service/fitnessage/v2/{_DAY}"] = _Resp(500)
        self.legacy.get_fitnessage_data.return_value = {"fitnessAge": 27.4}
        self.assertEqual(self.c.get_fitnessage_data(_DAY), {"fitnessAge": 27.4})
        self.legacy.get_fitnessage_data.assert_called_once_with(_DAY)

    def test_weight_days_flatten_to_the_dated_list(self) -> None:
        self.session.routes["/weight-service/weight/range/2025-11-09/2026-11-09"] = _Resp(body={
            "dailyWeightSummaries": [
                {"summaryDate": "2026-11-01", "allWeightMetrics": [
                    {"calendarDate": "2026-11-01", "weight": 79000.0},
                    {"calendarDate": "2026-11-01", "weight": 78800.0}]},
                {"summaryDate": "2026-11-05",
                 "latestWeight": {"calendarDate": "2026-11-05", "weight": 78500.0}}],
            "totalAverage": {"weight": 78766.0}})
        out = self.c.get_body_composition("2025-11-09", "2026-11-09")
        self.assertEqual([e["weight"] for e in out["dateWeightList"]],
                         [79000.0, 78800.0, 78500.0])
        self.assertEqual(self.session.gets[-1][1], {"includeAll": "true"})

    def test_heart_rate_zones_use_the_official_v2_read(self) -> None:
        rows = self.c.connectapi("/biometric-service/heartRateZones/")
        self.assertEqual(rows[0]["zone3Floor"], 133)
        self.factory.assert_not_called()

    def test_any_other_raw_request_goes_to_garminconnect(self) -> None:
        self.legacy.connectapi.return_value = {"ok": 1}
        self.assertEqual(self.c.connectapi("/something-else/", params={"a": 1}), {"ok": 1})
        self.legacy.connectapi.assert_called_once_with("/something-else/", params={"a": 1})

    def test_activities_pass_paging(self) -> None:
        out = self.c.get_activities(0, 100)
        self.assertEqual(out[0]["activityTrainingLoad"], 142.5)
        self.assertEqual(self.session.gets[-1][1], {"start": 0, "limit": 100})


class FallbackTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.legacy = MagicMock(name="garminconnect")
        self.factory = MagicMock(return_value=self.legacy)
        self.c = gp.HybridClient(self.factory)

    def test_official_failure_uses_garminconnect_with_the_same_arguments(self) -> None:
        self.use(_Session({}))                       # every path 404s
        self.legacy.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 1}}
        self.legacy.get_body_battery.return_value = [{"charged": 5}]
        self.legacy.get_activities.return_value = [{"activityId": 9}]
        self.legacy.connectapi.return_value = [{"sport": "DEFAULT"}]
        self.assertEqual(self.c.get_sleep_data(_DAY)["dailySleepDTO"]["sleepTimeSeconds"], 1)
        self.assertEqual(self.c.get_body_battery("2026-11-08", _DAY), [{"charged": 5}])
        self.assertEqual(self.c.get_activities(0, 30), [{"activityId": 9}])
        self.assertEqual(self.c.connectapi("/biometric-service/heartRateZones/"),
                         [{"sport": "DEFAULT"}])
        self.legacy.get_sleep_data.assert_called_once_with(_DAY)
        self.legacy.get_body_battery.assert_called_once_with("2026-11-08", _DAY)
        self.legacy.get_activities.assert_called_once_with(0, 30)
        self.legacy.connectapi.assert_called_once_with("/biometric-service/heartRateZones/")
        reads = gp.status()["reads"]
        self.assertEqual(reads["sleep"]["source"], "garminconnect")
        self.assertIn("404", reads["sleep"]["error"])

    def test_wrong_shape_is_a_failure_not_bad_data(self) -> None:
        self.use(_Session({"/sleep-service/sleep/dailySleep": _Resp(body=["unexpected"])}))
        self.legacy.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 2}}
        self.assertEqual(self.c.get_sleep_data(_DAY)["dailySleepDTO"]["sleepTimeSeconds"], 2)

    def test_reads_garmin_does_not_publish_go_straight_to_garminconnect(self) -> None:
        s = self.use(_Session({}))
        self.legacy.get_training_readiness.return_value = [{"score": 70}]
        self.assertEqual(self.c.get_training_readiness(_DAY), [{"score": 70}])
        self.c.upload_workout({"workoutName": "x"})
        self.c.get_activity(42)
        self.legacy.upload_workout.assert_called_once_with({"workoutName": "x"})
        self.legacy.get_activity.assert_called_once_with(42)
        self.assertEqual(s.gets, [])
        self.assertEqual(s.posts, [])

    def test_official_reads_work_while_the_unofficial_login_is_broken(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        broken = gp.HybridClient(MagicMock(side_effect=RuntimeError("login failed")))
        self.assertEqual(broken.get_hrv_data(_DAY)["hrvSummary"]["status"], "BALANCED")
        with self.assertRaises(RuntimeError):
            broken.get_training_readiness(_DAY)

    def test_status_names_the_source_of_each_read(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        self.c.get_hrv_data(_DAY)
        status = gp.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["reads"]["hrv"]["source"], "official")
        self.assertIsNotNone(status["token_expires_at"])
        self.assertNotIn("Bearer", json.dumps(status))


class GetClientTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._saved = (garmin_source._client, garmin_source._legacy,
                       dict(garmin_source._legacy_failure))
        garmin_source._client = garmin_source._legacy = None
        garmin_source._legacy_failure.update(until=0.0, error="")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        garmin_source._client, garmin_source._legacy = self._saved[0], self._saved[1]
        garmin_source._legacy_failure.update(self._saved[2])

    def test_key_set_gives_the_hybrid_without_logging_in(self) -> None:
        with patch.object(garmin_source, "_login") as login:
            client = garmin_source.get_client()
            self.assertIsInstance(client, gp.HybridClient)
            self.assertIs(garmin_source.get_client(), client)
            login.assert_not_called()

    def test_no_key_gives_the_garminconnect_client_as_before(self) -> None:
        sentinel = object()
        with patch.dict(os.environ, {gp.KEY_ENV_VAR: ""}), \
                patch.object(garmin_source, "_login", return_value=sentinel) as login:
            self.assertIs(garmin_source.get_client(), sentinel)
            self.assertIs(garmin_source.get_client(), sentinel)
            login.assert_called_once()

    def test_failed_unofficial_login_is_not_repeated_by_every_thread(self) -> None:
        with patch.object(garmin_source, "_login", side_effect=RuntimeError("no token")) as login:
            for _ in range(4):
                with self.assertRaises(RuntimeError):
                    garmin_source._legacy_client()
            login.assert_called_once()

    def test_readiness_reads_the_official_api_end_to_end(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        legacy = MagicMock(name="garminconnect")
        legacy.get_training_readiness.return_value = [{
            "score": 78, "level": "HIGH", "feedbackShort": "READY",
            "inputContext": "AFTER_WAKEUP_RESET", "calendarDate": _DAY,
            "timestampLocal": f"{_DAY}T06:40:00"}]
        with patch.object(config, "local_today", return_value=_TODAY), \
                patch.object(garmin_source, "_login", return_value=legacy):
            r = garmin_source.get_readiness()
        self.assertEqual(r["training_readiness"]["score"], 78)      # garminconnect
        self.assertEqual(r["sleep"]["hours"], 7.5)                   # official from here down
        self.assertEqual(r["sleep"]["score"], 84)
        self.assertEqual(r["hrv"], {"last_night_ms": 71, "weekly_ms": 68, "status": "BALANCED"})
        self.assertEqual(r["body_battery"]["current"], 74)
        self.assertEqual(r["body_battery"]["charged"], 62)
        self.assertEqual(r["resting_hr_bpm"], 47)
        self.assertEqual(r["stress"], {"avg": 28, "max": 91})
        self.assertEqual(r["spo2"], {"avg": 96.0, "lowest": 91})
        self.assertEqual(r["missing"], [])
        for name in ("get_sleep_data", "get_hrv_data", "get_body_battery", "get_rhr_day",
                     "get_all_day_stress", "get_spo2_data"):
            getattr(legacy, name).assert_not_called()

    def test_readiness_survives_a_dead_unofficial_login(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        with patch.object(config, "local_today", return_value=_TODAY), \
                patch.object(garmin_source, "_login", side_effect=RuntimeError("sso changed")):
            r = garmin_source.get_readiness()
        self.assertEqual(r["sleep"]["hours"], 7.5)
        self.assertEqual(r["hrv"]["last_night_ms"], 71)
        self.assertEqual(r["resting_hr_bpm"], 47)
        self.assertIn("training_readiness", r["missing"])

    def test_zones_come_from_the_official_read(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        legacy = MagicMock(name="garminconnect")
        legacy.get_lactate_threshold.return_value = {
            "speed_and_heart_rate": {"speed": 0.38, "heartRate": 169}}
        legacy.get_cycling_ftp.return_value = {"functionalThresholdPower": 250}
        with patch.object(garmin_source, "_login", return_value=legacy):
            out = zones._fetch()
        self.assertEqual(out["zones"]["DEFAULT"][2], (114, 132))
        self.assertEqual(out["max_hr"], 190)
        self.assertEqual(out["ftp_w"], 250)
        self.assertEqual(out["errors"], [])
        legacy.connectapi.assert_not_called()

    def test_weight_reads_the_official_range(self) -> None:
        routes = dict(_ROUTES)
        routes["/weight-service/weight/range/2025-11-09/2026-11-09"] = _Resp(body={
            "dailyWeightSummaries": [{"allWeightMetrics": [
                {"calendarDate": "2026-11-05", "weight": 78500.0}]}]})
        self.use(_Session(routes))
        saved = dict(garmin_source._WEIGHT_CACHE)
        garmin_source._WEIGHT_CACHE.update(val=None, ts=0.0)
        self.addCleanup(lambda: garmin_source._WEIGHT_CACHE.update(saved))
        with patch.object(config, "local_today", return_value=_TODAY), \
                patch.object(garmin_source, "_login") as login:
            w = garmin_source.get_weight_kg()
            login.assert_not_called()
        self.assertEqual((w["kg"], w["as_of"]), (78.5, "2026-11-05"))


class HealthTests(_Base):
    def test_health_reports_the_backend_without_secrets(self) -> None:
        self.use(_Session(dict(_ROUTES)))
        gp.HybridClient(MagicMock()).get_hrv_data(_DAY)
        garmin = main.health()["sources"]["garmin"]
        self.assertEqual(garmin["path"], "official Personal API, garminconnect for the rest")
        self.assertEqual(garmin["personal_api"]["reads"]["hrv"]["source"], "official")
        self.assertNotIn(_KEY, json.dumps(garmin))

    def test_health_without_a_key_is_unchanged(self) -> None:
        with patch.dict(os.environ, {gp.KEY_ENV_VAR: ""}):
            garmin = main.health()["sources"]["garmin"]
        self.assertEqual(garmin["path"], "direct garminconnect")
        self.assertFalse(garmin["personal_api"]["enabled"])


if __name__ == "__main__":
    unittest.main()
