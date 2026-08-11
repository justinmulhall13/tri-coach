from __future__ import annotations

import unittest

from app import athlete_guide, fueling_reference, garmin_source


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


if __name__ == "__main__":
    unittest.main()
