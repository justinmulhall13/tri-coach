from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import coaching_contract, db


ROUTINE = {
    "title": "Lower A",
    "exercises": [{"exercise_template_id": "D04AC939", "rest_seconds": 150,
                   "sets": [{"type": "normal", "reps": 5}]}],
}


class PlanStrengthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "strength.db"
        db._local = threading.local()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def _day(self, **kw) -> dict:
        return {"date": "2026-08-24", "slot": "lower", "title": "Lower A",
                "routine": ROUTINE, "effort_level": "moderate",
                "effort_cue": "Leave 1-2 reps in the tank", **kw}

    def test_a_lift_can_share_a_date_with_a_run(self) -> None:
        db.upsert_plan_day({
            "date": "2026-08-24", "week_index": 0, "phase": "build",
            "discipline": "run", "title": "Easy run", "duration_min": 45,
            "intensity": "Z2", "structure": {}, "is_rest": 0, "source": "seed",
            "tsb_target": 5, "why": "Aerobic maintenance.",
        })
        db.upsert_plan_strength(self._day())
        # Both exist for the same date, and neither displaced the other.
        self.assertEqual(db.get_plan_day("2026-08-24")["discipline"], "run")
        self.assertEqual(db.get_plan_strength("2026-08-24")["slot"], "lower")

    def test_the_routine_is_stored_verbatim_not_regenerated(self) -> None:
        db.upsert_plan_strength(self._day())
        self.assertEqual(db.get_plan_strength("2026-08-24")["routine"], ROUTINE)

    def test_the_effort_cue_is_persisted_with_the_day(self) -> None:
        stored = db.upsert_plan_strength(self._day())
        self.assertEqual(stored["effort_level"], "moderate")
        self.assertEqual(stored["effort_cue"], "Leave 1-2 reps in the tank")

    def test_reattaching_a_lift_replaces_it_rather_than_duplicating(self) -> None:
        db.upsert_plan_strength(self._day())
        db.upsert_plan_strength(self._day(title="Lower B", slot="lower"))
        self.assertEqual(db.get_plan_strength("2026-08-24")["title"], "Lower B")
        self.assertEqual(len(db.get_plan_strength_range("2026-01-01", "2026-12-31")), 1)

    def test_a_hevy_id_survives_a_later_edit_that_does_not_supply_one(self) -> None:
        db.upsert_plan_strength(self._day(hevy_routine_id="routine-99"))
        db.upsert_plan_strength(self._day(title="Lower A revised"))
        stored = db.get_plan_strength("2026-08-24")
        self.assertEqual(stored["title"], "Lower A revised")
        # Losing this would orphan the routine that actually exists in Hevy.
        self.assertEqual(stored["hevy_routine_id"], "routine-99")

    def test_a_range_query_returns_days_in_order(self) -> None:
        for date in ("2026-08-26", "2026-08-24", "2026-08-28"):
            db.upsert_plan_strength(self._day(date=date))
        dates = [d["date"] for d in db.get_plan_strength_range("2026-08-24", "2026-08-27")]
        self.assertEqual(dates, ["2026-08-24", "2026-08-26"])

    def test_an_unsupported_slot_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            db.upsert_plan_strength(self._day(slot="cardio"))

    def test_a_day_without_a_date_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            db.upsert_plan_strength(self._day(date=""))

    def test_deleting_reports_whether_anything_was_removed(self) -> None:
        db.upsert_plan_strength(self._day())
        self.assertTrue(db.delete_plan_strength("2026-08-24"))
        self.assertFalse(db.delete_plan_strength("2026-08-24"))
        self.assertIsNone(db.get_plan_strength("2026-08-24"))

    def test_strength_days_are_isolated_by_active_event_profile(self) -> None:
        first = {"id": "event-one", "mode": "RUNNING", "event": "First",
                 "date": "2026-10-01", "disciplines_and_distances": {"run_km": 10},
                 "goal": {"target": "finish"}}
        second = {**first, "id": "event-two", "event": "Second"}
        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            db.upsert_plan_strength(self._day())
            self.assertIsNotNone(db.get_plan_strength("2026-08-24"))
        with patch.object(coaching_contract, "EVENT_PROFILE", second):
            self.assertIsNone(db.get_plan_strength("2026-08-24"))
            self.assertEqual(db.get_plan_strength_range("2026-01-01", "2026-12-31"), [])


if __name__ == "__main__":
    unittest.main()
