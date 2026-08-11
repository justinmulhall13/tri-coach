from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import athlete_guide, coach, fueling_reference, garmin_source


class AthleteGuideContextTests(unittest.TestCase):
    def test_race_fueling_selects_both_aid_station_sections(self) -> None:
        ctx = athlete_guide.context_for(
            "Audit my Vancouver bike bottles and run flask with water and gels at aid stations."
        )
        self.assertIsNotNone(ctx)
        sections = ctx["sections"]
        self.assertIn("bike_aid", sections)
        self.assertIn("run_aid", sections)
        self.assertIn("15 passes", " ".join(sections["run_aid"]["facts"]))
        self.assertEqual(sections["bike_aid"]["pages"], [39])

    def test_non_race_chat_does_not_inject_guide(self) -> None:
        self.assertIsNone(athlete_guide.context_for("How did I sleep last night?"))
        self.assertIsNone(athlete_guide.context_for("Can you rebuild my plan after the race?"))
        self.assertIsNone(athlete_guide.context_for("Should I fuel tomorrow's easy run?"))


class FuelingReferenceTests(unittest.TestCase):
    def test_salt_and_sodium_are_not_interchangeable(self) -> None:
        self.assertEqual(fueling_reference.sodium_from_salt_mg(1000), 393)
        self.assertEqual(fueling_reference.sodium_from_salt_mg(1100), 433)
        self.assertEqual(fueling_reference.sodium_from_salt_tsp(0.5), 1134)


class SyncedActivityDedupeTests(unittest.TestCase):
    @staticmethod
    def _ride(activity_id: int, start: str, minutes: float, hr: int) -> dict:
        return {
            "date": "2026-08-11", "start_local": start, "name": f"Ride {activity_id}",
            "sport": "bike", "minutes": minutes, "km": 25.1, "hr_avg": hr,
            "hr_max": 153, "load": 40, "activity_id": activity_id,
        }

    def test_same_ride_from_two_recorders_counts_once(self) -> None:
        rows = garmin_source._dedupe_synced_activities([
            self._ride(1, "2026-08-11 09:00:00", 43.7, 126),
            self._ride(2, "2026-08-11 09:02:00", 44.1, 127),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deduplicated_sync_count"], 2)
        self.assertEqual(rows[0]["deduplicated_activity_ids"], [1, 2])

    def test_two_real_same_day_rides_stay_separate(self) -> None:
        rows = garmin_source._dedupe_synced_activities([
            self._ride(1, "2026-08-11 09:00:00", 44.0, 126),
            self._ride(2, "2026-08-11 17:00:00", 44.0, 126),
        ])
        self.assertEqual(len(rows), 2)


class GoalRaceCelebrationTests(unittest.TestCase):
    @staticmethod
    def _race() -> dict:
        return {
            "name": "T100 Vancouver",
            "date": "2026-08-16",
            "distances": {"swim_km": 2, "bike_km": 80, "run_km": 18},
        }

    def test_native_multisport_finish_is_recognized(self) -> None:
        activities = [
            {"date": "2026-08-16", "sport": "swim", "km": 2.0, "minutes": 38,
             "multisport_parent": 77, "leg": 1, "hr_avg": 132},
            {"date": "2026-08-16", "sport": "other", "km": 0.1, "minutes": 5,
             "multisport_parent": 77, "leg": 2},
            {"date": "2026-08-16", "sport": "bike", "km": 80.1, "minutes": 170,
             "multisport_parent": 77, "leg": 3, "hr_avg": 145},
            {"date": "2026-08-16", "sport": "run", "km": 18.0, "minutes": 105,
             "multisport_parent": 77, "leg": 5, "hr_avg": 158},
        ]

        finish = coach._goal_race_completion(activities, self._race())

        self.assertTrue(finish["completed"])
        self.assertEqual(finish["multisport_parent"], 77)
        self.assertEqual([leg["sport"] for leg in finish["legs"]], ["swim", "bike", "run"])
        self.assertEqual(finish["total_elapsed_min"], 318.0)

    def test_short_race_day_shakeout_does_not_trigger_finish(self) -> None:
        activities = [
            {"date": "2026-08-16", "sport": "swim", "km": 0.2, "minutes": 6},
            {"date": "2026-08-16", "sport": "bike", "km": 4, "minutes": 10},
            {"date": "2026-08-16", "sport": "run", "km": 1, "minutes": 6},
        ]
        self.assertIsNone(coach._goal_race_completion(activities, self._race()))

    def test_finish_brief_is_marked_as_a_one_time_celebration(self) -> None:
        context = json.dumps({"goal_race_completion": {
            "completed": True,
            "celebration_pending": True,
            "race_date": "2026-08-16",
            "race_name": "T100 Vancouver",
            "legs": [],
        }})
        message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="YOU DID IT — T100 Vancouver finished!")],
            model="test-model",
            stop_reason="end_turn",
        )
        with (
            patch.object(coach.config, "ANTHROPIC_API_KEY", "test-key"),
            patch.object(coach, "_context_block", return_value=context),
            patch.object(coach, "_stream_reply", return_value=message),
            patch.object(coach.db, "add_chat"),
            patch.object(coach.db, "set_meta") as set_meta,
        ):
            result = coach.morning_brief()

        self.assertTrue(result["celebrate"])
        set_meta.assert_called_once()
        self.assertEqual(set_meta.call_args.args[0], "race_finish_celebrated_2026-08-16")


if __name__ == "__main__":
    unittest.main()
