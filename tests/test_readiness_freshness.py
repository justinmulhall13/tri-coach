from __future__ import annotations

import datetime
import json
import time
import unittest
from unittest.mock import patch

from app import garmin_source, main, suggest


TODAY = datetime.date(2026, 8, 17)
NOW = datetime.datetime(2026, 8, 17, 15, 0, tzinfo=datetime.timezone.utc)


def _body(response) -> dict:
    return json.loads(response.body)


class _ReadinessClient:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def get_training_readiness(self, _date):
        return self.snapshots

    def get_sleep_data(self, date):
        return {"dailySleepDTO": {"calendarDate": date, "sleepTimeSeconds": 8 * 3600}}

    def get_hrv_data(self, date):
        return {"hrvSummary": {"calendarDate": date, "lastNightAvg": 55}}

    def get_body_battery(self, _start, end):
        return [{"date": end, "charged": 50, "drained": 10,
                 "bodyBatteryValuesArray": [[0, 80], [1, 70]]}]

    def get_rhr_day(self, date):
        return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
            {"calendarDate": date, "value": 50}
        ]}}}

    def get_all_day_stress(self, date):
        return {"calendarDate": date, "avgStressLevel": 20}

    def get_spo2_data(self, date):
        return {"calendarDate": date, "averageSpO2": 98}


class ReadinessFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_cache = dict(main._cache)
        self.original_revision = main._last_activity_revision

    def tearDown(self) -> None:
        main._cache.clear()
        main._cache.update(self.original_cache)
        main._last_activity_revision = self.original_revision

    def test_morning_baseline_and_current_postexercise_snapshot_stay_distinct(self) -> None:
        client = _ReadinessClient([
            {"calendarDate": "2026-08-17", "timestampLocal": "2026-08-17T07:00:00",
             "timestamp": "2026-08-17T07:00:00", "inputContext": "AFTER_WAKEUP_RESET",
             "score": 91, "level": "PRIME"},
            {"calendarDate": "2026-08-17", "timestampLocal": "2026-08-17T13:00:00",
             "timestamp": "2026-08-17T13:00:00", "inputContext": "AFTER_POST_EXERCISE_RESET",
             "score": 28, "level": "LOW"},
        ])
        with (
            patch.object(garmin_source, "get_client", return_value=client),
            patch.object(garmin_source.config, "local_today", return_value=TODAY),
            patch.object(garmin_source.config, "local_now", return_value=NOW),
        ):
            result = garmin_source.get_readiness()

        self.assertEqual(result["training_readiness"]["score"], 91)
        self.assertEqual(result["current_readiness"]["score"], 28)
        self.assertTrue(result["training_readiness"]["freshness"]["is_current"])
        self.assertTrue(result["current_readiness"]["freshness"]["is_current"])
        self.assertTrue(result["current_readiness"]["is_post_exercise"])

    def test_previous_day_readiness_is_not_relabelled_as_today(self) -> None:
        client = _ReadinessClient([
            {"calendarDate": "2026-08-16", "timestampLocal": "2026-08-16T07:00:00",
             "inputContext": "AFTER_WAKEUP_RESET", "score": 88, "level": "HIGH"},
        ])
        with (
            patch.object(garmin_source, "get_client", return_value=client),
            patch.object(garmin_source.config, "local_today", return_value=TODAY),
            patch.object(garmin_source.config, "local_now", return_value=NOW),
        ):
            result = garmin_source.get_readiness()

        self.assertIsNone(result["training_readiness"]["score"])
        self.assertIsNone(result["current_readiness"]["score"])
        self.assertEqual(result["training_readiness"]["freshness"]["state"], "stale")
        self.assertEqual(result["training_readiness"]["freshness"]["source_date"], "2026-08-16")

    def test_post_race_activity_invalidates_intraday_readiness_and_acwr_not_morning(self) -> None:
        current = {"state": "current", "is_current": True, "source_date": "2026-08-17",
                   "expected_date": "2026-08-17", "source_timestamp": "2026-08-17T08:00:00+00:00"}
        readiness = {
            "training_readiness": {"score": 90, "freshness": dict(current)},
            "current_readiness": {"score": 90, "freshness": dict(current)},
        }
        training_load = {
            "load_ratio": 0.95, "acute_load": 500, "chronic_load": 525,
            "load_focus": {"aerobic_low": 100},
            "freshness": dict(current), "load_focus_freshness": dict(current),
        }
        load = {"latest_garmin_activity": {
            "activity_id": 123, "date": "2026-08-17", "sport": "brick",
            "start_local": "2026-08-17T09:00:00+00:00",
            "end_local": "2026-08-17T14:45:00+00:00", "provider": "Garmin",
        }}
        with (
            patch.object(garmin_source.config, "local_today", return_value=TODAY),
            patch.object(garmin_source.config, "local_now", return_value=NOW),
        ):
            rd, tl = garmin_source.reconcile_freshness(readiness, training_load, load)

        self.assertTrue(rd["training_readiness"]["freshness"]["is_current"])
        self.assertFalse(rd["current_readiness"]["freshness"]["is_current"])
        self.assertEqual(rd["current_readiness"]["freshness"]["state"], "stale")
        self.assertFalse(tl["freshness"]["is_current"])
        self.assertIsNone(garmin_source.current_training_load(tl)["load_ratio"])
        self.assertEqual(readiness["current_readiness"]["freshness"]["state"], "current")

    def test_training_load_selects_newest_provider_dated_record(self) -> None:
        class Client:
            def get_training_status(self, _date):
                return {
                    "mostRecentTrainingStatus": {"latestTrainingStatusData": {
                        "old-device": {"calendarDate": "2026-08-16", "timestamp": "2026-08-16T08:00:00",
                                       "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 400,
                                                                  "dailyAcuteChronicWorkloadRatio": 0.7}},
                        "new-device": {"calendarDate": "2026-08-17", "timestamp": "2026-08-17T09:00:00",
                                       "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 620,
                                                                  "dailyTrainingLoadChronic": 550,
                                                                  "dailyAcuteChronicWorkloadRatio": 1.13}},
                    }},
                    "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {
                        "old-device": {"calendarDate": "2026-08-16", "monthlyLoadAerobicLow": 10},
                        "new-device": {"calendarDate": "2026-08-17", "monthlyLoadAerobicLow": 99},
                    }},
                }

        with (
            patch.object(garmin_source, "get_client", return_value=Client()),
            patch.object(garmin_source.config, "local_today", return_value=TODAY),
            patch.object(garmin_source.config, "local_now", return_value=NOW),
        ):
            result = garmin_source.get_training_load()

        self.assertEqual(result["acute_load"], 620)
        self.assertEqual(result["load_ratio"], 1.13)
        self.assertEqual(result["load_focus"]["aerobic_low"], 99)
        self.assertEqual(result["as_of"], "2026-08-17")
        self.assertTrue(result["freshness"]["is_current"])

    def test_activity_revision_clears_morning_and_coach_caches(self) -> None:
        main._last_activity_revision = "before-race"
        main._cache.update({"morning": {"readiness": "old"}, "ts": time.time(),
                            "date": "2026-08-17"})
        with patch.object(main.coach, "invalidate_context_cache") as invalidate:
            changed = main._observe_activity_revision({"activity_revision": "race-synced"})

        self.assertTrue(changed)
        self.assertIsNone(main._cache["morning"])
        self.assertIsNone(main._cache["date"])
        invalidate.assert_called_once_with()

    def test_morning_cache_never_crosses_local_date_rollover(self) -> None:
        main._cache.update({"morning": {"marker": "yesterday"}, "ts": time.time(),
                            "date": "2026-08-16"})
        fresh_readiness = {"training_readiness": {"score": 80, "freshness": {
            "is_current": True, "source_date": "2026-08-17"
        }}, "current_readiness": {}}
        fresh_load = {"activities": [], "by_sport": {}, "activity_revision": "empty"}
        with (
            patch.object(main.config, "local_today", return_value=TODAY),
            patch.object(main.config, "local_now", return_value=NOW),
            patch.object(main.garmin_source, "get_readiness", return_value=fresh_readiness) as read,
            patch.object(main.garmin_source, "get_fitness_markers", return_value={}),
            patch.object(main.garmin_source, "get_recent_load", return_value=fresh_load),
            patch.object(main.garmin_source, "reconcile_freshness",
                         return_value=(fresh_readiness, {})),
            patch.object(main, "_observe_activity_revision", return_value=False),
            patch.object(main.suggest, "todays_suggestion", return_value={}),
            patch.object(main.coach, "prime_context_cache"),
        ):
            response = main.morning()
            payload = _body(response)

        self.assertFalse(payload["cached"])
        self.assertNotIn("marker", payload)
        self.assertEqual(main._cache["date"], "2026-08-17")
        self.assertEqual(response.headers["cache-control"], "no-store")
        read.assert_called_once_with()

    def test_stale_readiness_cannot_emit_a_green_suggestion_signal(self) -> None:
        readiness = {"training_readiness": {"score": 90, "freshness": {
            "state": "stale", "is_current": False, "source_date": "2026-08-16"
        }}}
        signal = suggest._readiness_signal(readiness)
        self.assertEqual(signal["level"], "unknown")
        self.assertFalse(signal["downregulate"])
        self.assertIn("stale", signal["reason"])


if __name__ == "__main__":
    unittest.main()
