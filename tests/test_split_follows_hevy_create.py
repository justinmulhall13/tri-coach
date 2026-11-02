"""Creating "Full Body A" in Hevy from Coach updates Full Body A on the Lifting tab.

The athlete rebuilt Full Body A in chat and created it in Hevy, but the Lifting
tab kept showing the old default day.
"""
from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, hevy_actions, lifting_split, main

SUMMARY = {"title": "Full Body A", "exercises": [
    {"title": "Landmine Rotational Throw", "sets": 3, "reps": "6", "rest_seconds": 90},
    {"title": "Explosive Landmine Press", "sets": 3, "reps": "4-6", "rest_seconds": 150},
    {"title": "Front Squat", "sets": 3, "reps": "5-6", "rest_seconds": 150},
    {"title": "Incline Bench Press (Dumbbell)", "sets": 3, "reps": "6-8", "rest_seconds": 150},
]}


class ApplyRoutineTests(unittest.TestCase):
    def test_the_named_day_is_replaced_and_the_others_kept(self) -> None:
        days, slot = lifting_split.apply_routine(None, SUMMARY)
        self.assertEqual(slot, "full_a")
        self.assertEqual([e["title"] for e in days[0]["exercises"]],
                         [e["title"] for e in SUMMARY["exercises"]])
        self.assertEqual(days[1]["name"], "Full Body B")
        self.assertEqual(list(days[1]["exercises"]),
                         [dict(e) for e in lifting_split.DEFAULT_SPLIT[1]["exercises"]])

    def test_a_kept_exercise_keeps_its_reasoning(self) -> None:
        days, _ = lifting_split.apply_routine(None, SUMMARY)
        squat = next(e for e in days[0]["exercises"] if e["title"] == "Front Squat")
        self.assertTrue(squat["why"])

    def test_an_unrelated_routine_changes_nothing(self) -> None:
        self.assertIsNone(lifting_split.apply_routine(None, {**SUMMARY, "title": "Hotel gym"}))
        self.assertIsNone(lifting_split.apply_routine(None, {**SUMMARY, "title": "Full Body AB"}))

    def test_summary_reads_sets_and_rep_ranges(self) -> None:
        routine = {"title": "Full Body A", "exercises": [{
            "exercise_template_id": "fs", "rest_seconds": 150,
            "sets": [{"type": "warmup", "reps": 5},
                     {"type": "normal", "rep_range": {"start": 5, "end": 6}},
                     {"type": "normal", "rep_range": {"start": 5, "end": 6}}]}]}
        summary = hevy_actions.routine_summary(routine, {"fs": "Front Squat"})
        self.assertEqual(summary["exercises"], [
            {"title": "Front Squat", "sets": 2, "reps": "5-6", "rest_seconds": 150}])


class CreateRouteSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "sync.db"
        db._local = threading.local()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    def test_a_created_routine_shows_up_on_the_lifting_tab(self) -> None:
        created = {"ok": True, "created": True, "summary": SUMMARY}
        with patch.object(hevy_actions, "create_confirmed_routine", return_value=created):
            r = self.client.post("/api/integrations/hevy/routines", json={
                "confirmed": True, "operation_id": "op-1",
                "routine": {"title": "Full Body A", "exercises": []}})
        self.assertEqual(r.json()["split_updated"], "full_a")
        split = self.client.get("/api/lifting/split").json()
        self.assertEqual(split["source"], "coach")
        self.assertEqual(split["days"][0]["exercises"][1]["title"], "Explosive Landmine Press")


    def test_the_calendar_lift_days_follow_the_new_split(self) -> None:
        from app import plan
        from tests.test_half_iron_plan import START, _activate_703
        _activate_703()
        plan.seed(START)
        created = {"ok": True, "created": True, "summary": SUMMARY}
        with patch.object(hevy_actions, "create_confirmed_routine", return_value=created):
            self.client.post("/api/integrations/hevy/routines", json={
                "confirmed": True, "operation_id": "op-2",
                "routine": {"title": "Full Body A", "exercises": []}})
        monday = db.get_plan_strength("2027-01-04")
        self.assertIn("Explosive Landmine Press", monday["structure"]["main"])


    def _sync_with(self, routines: list[dict]) -> list[str]:
        class Fake:
            def status(self):
                return {"connected": True}

            def get_routines(self, *, page, page_size):
                return {"routines": routines}
        with (patch.object(hevy_actions.hevy_connector, "status",
                           return_value={"connected": True}),
              patch.object(hevy_actions.hevy_connector, "connector", return_value=Fake())):
            return hevy_actions.sync_split_from_routines(force=True)

    def test_a_routine_edited_in_hevy_flows_back_to_the_tab(self) -> None:
        db.save_lifting_split([dict(d) for d in lifting_split.DEFAULT_SPLIT], source="edited")
        routine = {"title": "Full Body A", "updated_at": "2999-01-01T00:00:00Z", "exercises": [
            {"exercise_template_id": "rdl", "title": "Single Leg Romanian Deadlift (Dumbbell)",
             "rest_seconds": 120, "sets": [{"type": "normal", "reps": 10}] * 2},
            {"exercise_template_id": "calf", "title": "Calf Extension (Machine)",
             "rest_seconds": 60, "sets": [{"type": "normal", "reps": r} for r in (8, 10, 12)]},
        ]}
        self.assertEqual(self._sync_with([routine]), ["full_a"])
        stored = db.get_lifting_split()
        self.assertEqual(stored["source"], "hevy")
        self.assertEqual([(e["title"], e["sets"], e["reps"]) for e in stored["days"][0]["exercises"]],
                         [("Single Leg Romanian Deadlift (Dumbbell)", 2, "10"),
                          ("Calf Extension (Machine)", 3, "8-12")])

    def test_a_tab_edit_newer_than_the_routine_is_kept(self) -> None:
        db.save_lifting_split([dict(d) for d in lifting_split.DEFAULT_SPLIT], source="edited")
        old = {"title": "Full Body A", "updated_at": "2000-01-01T00:00:00Z", "exercises": [
            {"exercise_template_id": "x", "title": "Old Thing", "sets": [{"type": "normal", "reps": 5}]}]}
        self.assertEqual(self._sync_with([old]), [])
        self.assertEqual(db.get_lifting_split()["source"], "edited")


if __name__ == "__main__":
    unittest.main()
