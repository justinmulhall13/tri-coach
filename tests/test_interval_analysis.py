from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from app import garmin_workout, interval_analysis


class IntervalExecutionTests(unittest.TestCase):
    def test_active_interval_is_scored_by_duration_not_session_average(self) -> None:
        start = datetime.datetime(2026, 8, 11, 13, 34, 15, tzinfo=datetime.timezone.utc)
        epoch_ms = int(start.timestamp() * 1000)
        detail = {
            "metricDescriptors": [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
            ],
            "activityDetailMetrics": [
                {"metrics": [epoch_ms + offset * 1000, hr]}
                for offset, hr in zip((0, 10, 20, 30, 40, 50), (130, 140, 145, 151, 149, 130))
            ],
        }
        typed = {"splits": [{
            "type": "INTERVAL_ACTIVE",
            "startTimeGMT": "2026-08-11T13:34:15+00:00",
            "duration": 60,
            "averageHR": 141,
            "maxHR": 151,
        }]}
        activity = {
            "activityId": 42,
            "summaryDTO": {"averageHR": 127},
            "metadataDTO": {"associatedWorkoutId": 99},
        }
        workout = {
            "workoutName": "Bike openers",
            "description": "4x5min at race HR140-150",
            "workoutSegments": [{"workoutSteps": [{
                "stepType": {"stepTypeKey": "interval"},
                "targetType": {"workoutTargetTypeKey": "heart.rate.zone"},
                "targetValueOne": 116,
                "targetValueTwo": 134,
            }]}],
        }

        result = interval_analysis.analyze(activity, typed, detail, workout)

        self.assertIsNotNone(result)
        self.assertEqual(result["whole_session_avg_hr"], 127)
        self.assertEqual(result["prescribed_work_target_bpm"], [140, 150])
        self.assertEqual(result["intervals"][0]["time_below_target_min"], 0.33)
        self.assertEqual(result["intervals"][0]["time_in_target_min"], 0.5)
        self.assertEqual(result["intervals"][0]["time_above_target_min"], 0.17)
        self.assertEqual(result["intervals"][0]["at_or_above_target_floor_pct"], 67)
        self.assertEqual(
            result["structured_target_mismatch"]["encoded_on_device_bpm"],
            [116, 134],
        )

    def test_non_interval_activity_has_no_interval_grade(self) -> None:
        self.assertIsNone(interval_analysis.analyze({}, {"splits": []}, {}, {}))


class ExplicitWorkoutTargetTests(unittest.TestCase):
    @patch("app.garmin_workout.zones.hr_range", return_value=(97, 115))
    def test_explicit_bike_hr_range_is_encoded_on_watch(self, _mock_range) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "bike",
            "title": "Bike openers - race HR efforts",
            "duration_min": 50,
            "intensity": "race",
            "structure": {
                "warmup": "10 min easy",
                "main": "4x5min @HR140-150, w/ 3 min easy",
                "cooldown": "10 min easy",
            },
        }, "2026-08-11")
        repeat = workout["workoutSegments"][0]["workoutSteps"][1]
        work_step = repeat["workoutSteps"][0]

        self.assertEqual(work_step["targetValueOne"], 140.0)
        self.assertEqual(work_step["targetValueTwo"], 150.0)
        self.assertEqual(work_step["description"], "HR 140-150")


if __name__ == "__main__":
    unittest.main()
