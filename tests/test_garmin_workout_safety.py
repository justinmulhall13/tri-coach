from __future__ import annotations

import json
import pathlib
import unittest
from unittest.mock import patch

from app import coaching_contract, garmin_workout
from app import plan


class GarminWorkoutSafetyTests(unittest.TestCase):
    def test_active_event_profile_drives_race_targets_after_cross_profile_switch(self) -> None:
        active_profile = {
            "id": "marathon-2026",
            "mode": "MARATHON",
            "event": "Autumn Marathon",
            "pacing_targets": {
                "bike_hr_bpm": [125, 135],
                "run_hr_bpm": [145, 155],
                "run_lap_1_min_per_km": "7:05",
            },
        }
        with patch.object(coaching_contract, "event_context", return_value=active_profile):
            bike = garmin_workout.build_workout({
                "discipline": "bike", "title": "Race support ride",
                "duration_min": 30, "intensity": "race pace",
                "structure": {"main": "30 min race effort"},
            }, "2026-10-17")
            run = garmin_workout.build_workout({
                "discipline": "run", "title": "Race pace run",
                "duration_min": 20, "intensity": "race pace",
                "structure": {"main": "20 min race effort"},
            }, "2026-10-17", thr_pace=270)

        bike_step = bike["workoutSegments"][0]["workoutSteps"][0]
        run_step = run["workoutSegments"][0]["workoutSteps"][0]
        self.assertEqual(
            (bike_step["targetValueOne"], bike_step["targetValueTwo"]), (125.0, 135.0),
        )
        self.assertEqual(
            (run_step["targetValueOne"], run_step["targetValueTwo"]), (145.0, 155.0),
        )
        self.assertIn("lap 1 ceiling 7:05/km", run_step["description"])
        self.assertNotIn("6:15/km", run_step["description"])

    def test_race_day_never_builds_or_uploads_as_one_cycling_workout(self) -> None:
        race = {
            "discipline": "race",
            "title": "T100 Vancouver",
            "duration_min": 345,
            "intensity": "race",
        }

        self.assertIsNone(garmin_workout.build_workout(race, "2026-08-16"))
        with patch.object(garmin_workout.garmin_source, "get_client") as get_client:
            result = garmin_workout.push(race, "2026-08-16")

        self.assertIn("multisport", result["error"])
        self.assertIn("triathlon activity profile", result["error"])
        get_client.assert_not_called()

    def test_race_week_brick_bike_uses_event_hr_not_generic_zone_four(self) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "brick",
            "title": "Race-week brick (short)",
            "duration_min": 50,
            "intensity": "race pace",
            "structure": {
                "warmup": "10 min easy",
                "main": "30 min @ HR 135-145 (race effort)",
                "cooldown": "",
                "run": "10 min run off the bike @ race pace",
            },
        }, "2026-08-15", thr_pace=270)

        bike_step = workout["workoutSegments"][0]["workoutSteps"][1]
        self.assertIsNone(bike_step["zoneNumber"])
        self.assertEqual((bike_step["targetValueOne"], bike_step["targetValueTwo"]), (140.0, 150.0))

    def test_legacy_outdoor_brick_watts_are_ignored_and_removed(self) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "brick",
            "title": "Race-week brick (short)",
            "duration_min": 50,
            "intensity": "race pace",
            "structure": {
                "warmup": "10 min easy @ 180 W",
                "main": "30 min @ 230–253 W (race effort)",
                "cooldown": "5 min easy @ 160 W",
                "run": "10 min run off the bike @ race pace",
            },
        }, "2026-08-15", thr_pace=270)

        bike_step = workout["workoutSegments"][0]["workoutSteps"][1]
        self.assertEqual((bike_step["targetValueOne"], bike_step["targetValueTwo"]), (140.0, 150.0))
        warmup, cooldown = (workout["workoutSegments"][0]["workoutSteps"][i] for i in (0, 2))
        self.assertEqual(warmup["description"], "10 min easy")
        self.assertEqual(cooldown["description"], "5 min easy")
        self.assertEqual(warmup["targetType"]["workoutTargetTypeKey"], "no.target")
        self.assertEqual(cooldown["targetType"]["workoutTargetTypeKey"], "no.target")
        self.assertNotIn("HR 140-150", warmup["description"] + cooldown["description"])
        encoded = json.dumps(workout)
        self.assertNotIn(" W", encoded)
        self.assertNotIn("Peloton", encoded)

    def test_legacy_outdoor_bike_watts_are_removed_from_entire_workout(self) -> None:
        with patch.object(garmin_workout.zones, "hr_range", return_value=(150, 165)):
            workout = garmin_workout.build_workout({
                "discipline": "bike",
                "title": "Outdoor threshold bike",
                "duration_min": 50,
                "intensity": "threshold",
                "structure": {
                    "warmup": "8 min easy @ 180 W",
                    "main": "3x12 min @ 274-294 W, w/ 3 min easy",
                    "cooldown": "6 min easy @ 170 W",
                },
            }, "2026-08-15")

        encoded = json.dumps(workout)
        self.assertNotIn(" W", encoded)
        self.assertNotIn("Peloton", encoded)
        repeat = workout["workoutSegments"][0]["workoutSteps"][1]
        work = repeat["workoutSteps"][0]
        self.assertEqual((work["targetValueOne"], work["targetValueTwo"]), (150.0, 165.0))
        warmup, cooldown = (workout["workoutSegments"][0]["workoutSteps"][i] for i in (0, 2))
        self.assertEqual(warmup["description"], "8 min easy")
        self.assertEqual(cooldown["description"], "6 min easy")
        self.assertEqual(warmup["targetType"]["workoutTargetTypeKey"], "no.target")
        self.assertEqual(cooldown["targetType"]["workoutTargetTypeKey"], "no.target")

    def test_spelled_out_outdoor_watts_are_removed_from_every_block(self) -> None:
        with patch.object(garmin_workout.zones, "hr_range", return_value=(150, 165)):
            workout = garmin_workout.build_workout({
                "discipline": "bike",
                "title": "Outdoor threshold bike",
                "duration_min": 45,
                "intensity": "threshold",
                "structure": {
                    "warmup": "10 min at 150 watts",
                    "main": "3x8 min at 250-270 watts, w/ 3 min easy",
                    "cooldown": "5 min at 140 watts",
                },
            }, "2026-08-15")

        encoded = json.dumps(workout)
        self.assertNotRegex(encoded.lower(), r"\bwatts?\b")
        self.assertNotIn(" W", encoded)
        self.assertIn("Warmup: 10 min", workout["description"])
        self.assertIn("Cooldown: 5 min", workout["description"])

    def test_warmup_hr_never_becomes_the_main_work_target(self) -> None:
        with patch.object(garmin_workout.zones, "hr_range", return_value=(150, 165)):
            workout = garmin_workout.build_workout({
                "discipline": "bike",
                "title": "Outdoor threshold bike",
                "duration_min": 45,
                "intensity": "threshold",
                "structure": {
                    "warmup": "10 min @ HR 100-120",
                    "main": "3x8 min @ 250-270 W, w/ 3 min easy",
                    "cooldown": "5 min easy",
                },
            }, "2026-08-15")

        repeat = workout["workoutSegments"][0]["workoutSteps"][1]
        work = repeat["workoutSteps"][0]
        self.assertEqual((work["targetValueOne"], work["targetValueTwo"]), (150.0, 165.0))
        self.assertNotEqual((work["targetValueOne"], work["targetValueTwo"]), (100.0, 120.0))

    def test_brick_run_uses_event_hr_before_threshold_derived_pace(self) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "brick",
            "title": "Race-week brick (short)",
            "duration_min": 50,
            "intensity": "race pace",
            "structure": {
                "warmup": "10 min easy",
                "main": "30 min @ HR 140-150 (race effort)",
                "cooldown": "",
                "run": "10 min run off the bike @ race pace",
            },
        }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual(run_step["targetType"]["workoutTargetTypeKey"], "heart.rate.zone")
        self.assertEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (152.0, 158.0))

    def test_brick_run_falls_back_to_threshold_pace_without_event_hr(self) -> None:
        pacing = coaching_contract.EVENT_PROFILE["pacing_targets"]
        with patch.dict(pacing, {"run_hr_bpm": []}):
            workout = garmin_workout.build_workout({
                "discipline": "brick",
                "title": "Brick",
                "duration_min": 50,
                "intensity": "race pace",
                "structure": {"main": "30 min race effort", "run": "20 min run @ race pace"},
            }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual(run_step["targetType"]["workoutTargetTypeKey"], "pace.zone")

    def test_easy_brick_run_uses_easy_ceiling_not_event_race_hr(self) -> None:
        with (
            patch.object(garmin_workout.zones, "pace_range", return_value=(420.0, 390.0)),
            patch.object(garmin_workout.zones, "hr_range", return_value=(110, 130)),
        ):
            workout = garmin_workout.build_workout({
                "discipline": "brick",
                "title": "Easy aerobic brick",
                "duration_min": 50,
                "intensity": "Z2",
                "structure": {"main": "40 min easy bike", "run": "10 min easy run off the bike"},
            }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual(run_step["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertEqual(run_step["targetValueOne"], 0.0)
        self.assertEqual(run_step["targetValueTwo"], round(1000.0 / 390.0, 3))
        self.assertNotEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (152.0, 158.0))
        self.assertIn("CEILING 6:30/km", run_step["description"])

    def test_easy_brick_run_honors_an_explicit_hr_target(self) -> None:
        with patch.object(garmin_workout.zones, "hr_range", return_value=(110, 130)):
            workout = garmin_workout.build_workout({
                "discipline": "brick",
                "title": "Easy aerobic brick",
                "duration_min": 50,
                "intensity": "Z2",
                "structure": {"main": "40 min easy bike", "run": "10 min easy @ HR 120-140"},
            }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (120.0, 140.0))

    def test_run_leg_explicit_hr_overrides_conflicting_race_bike_label(self) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "brick",
            "title": "Race-specific bike with easy run",
            "duration_min": 80,
            "intensity": "race pace",
            "structure": {
                "main": "60 min @ HR 140-150 race effort",
                "run": "20 min easy @ HR 135-145",
            },
        }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (135.0, 145.0))
        self.assertNotEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (152.0, 158.0))

    def test_run_leg_easy_label_overrides_conflicting_race_bike_label(self) -> None:
        with patch.object(garmin_workout.zones, "pace_range", return_value=(420.0, 390.0)):
            workout = garmin_workout.build_workout({
                "discipline": "brick",
                "title": "Race-specific bike with easy run",
                "duration_min": 80,
                "intensity": "race pace",
                "structure": {"main": "60 min race effort", "run": "20 min easy"},
            }, "2026-08-15", thr_pace=270)

        run_step = workout["workoutSegments"][1]["workoutSteps"][0]
        self.assertEqual((run_step["targetValueOne"], run_step["targetValueTwo"]),
                         (0.0, round(1000.0 / 390.0, 3)))
        self.assertNotEqual((run_step["targetValueOne"], run_step["targetValueTwo"]), (152.0, 158.0))

    def test_race_brick_surge_uses_event_hr_not_generic_zone(self) -> None:
        workout = garmin_workout.build_workout({
            "discipline": "brick",
            "title": "Race brick with surges",
            "duration_min": 70,
            "intensity": "race pace",
            "structure": {
                "main": "60 min race effort with 2x5 min surges, w/ 3 min easy",
                "run": "10 min run @ race pace",
            },
        }, "2026-08-15", thr_pace=270)

        bike_steps = workout["workoutSegments"][0]["workoutSteps"]
        repeat = next(step for step in bike_steps if step["type"] == "RepeatGroupDTO")
        surge = repeat["workoutSteps"][0]
        self.assertIsNone(surge["zoneNumber"])
        self.assertEqual((surge["targetValueOne"], surge["targetValueTwo"]), (140.0, 150.0))
        self.assertIn("HR 140-150", surge["description"])

    def test_easy_run_encodes_only_a_faster_than_ceiling(self) -> None:
        with (
            patch.object(garmin_workout.zones, "pace_range", return_value=(420.0, 390.0)),
            patch.object(garmin_workout.zones, "hr_range", return_value=(120, 145)),
        ):
            workout = garmin_workout.build_workout({
                "discipline": "run",
                "title": "Easy run",
                "duration_min": 40,
                "intensity": "easy",
                "structure": {"main": "40 min easy"},
            }, "2026-08-15", thr_pace=270)

        step = workout["workoutSegments"][0]["workoutSteps"][0]
        self.assertEqual(step["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertEqual(step["targetValueOne"], 0.0)
        self.assertEqual(step["targetValueTwo"], round(1000.0 / 390.0, 3))
        self.assertIn("CEILING 6:30/km", step["description"])

    def test_visible_easy_ceiling_matches_watch_target_and_omits_hr_range(self) -> None:
        with (
            patch.object(garmin_workout.zones, "pace_range", return_value=(420.0, 390.0)),
            patch.object(garmin_workout.zones, "hr_range", return_value=(120, 145)),
        ):
            visible = plan._easy_run_ceiling()
            target, note = garmin_workout._easy_run_target_and_note("Easy run")
        self.assertEqual(visible, "pace ceiling 6:30/km; do not go faster")
        self.assertEqual(note, "CEILING 6:30/km; do not go faster")
        self.assertEqual(target["targetValueTwo"], round(1000.0 / 390.0, 3))
        self.assertNotIn("120", note)
        self.assertNotIn("145", note)

    def test_race_push_control_is_hidden_in_workout_detail(self) -> None:
        html = (pathlib.Path(__file__).parents[1] / "static" / "index.html").read_text()
        self.assertIn('["rest","race"].includes((x.disc||"").toLowerCase())', html)


if __name__ == "__main__":
    unittest.main()
