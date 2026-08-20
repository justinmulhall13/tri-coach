from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main


ROUTINE = {
    "title": "Lower A",
    "exercises": [{"exercise_template_id": "D04AC939", "rest_seconds": 150,
                   "sets": [{"type": "normal", "reps": 5}]}],
}


class PlanStrengthRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "routes.db"
        db._local = threading.local()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def _attach(self, **kw):
        body = {"date": "2026-08-24", "slot": "lower", "title": "Lower A",
                "routine": ROUTINE, "effort_level": "moderate",
                "effort_cue": "Leave 1-2 reps in the tank", **kw}
        return self.client.post("/api/plan/strength", json=body)

    def test_a_lift_can_be_attached_and_read_back(self) -> None:
        self.assertEqual(self._attach().status_code, 200)
        response = self.client.get("/api/plan/strength",
                                   params={"start": "2026-08-01", "end": "2026-08-31"})
        days = response.json()["days"]
        self.assertEqual(len(days), 1)
        # Stored in validated form, which is what guarantees it stays creatable.
        self.assertEqual(days[0]["routine"]["exercises"], ROUTINE["exercises"])
        self.assertEqual(days[0]["routine"]["title"], ROUTINE["title"])
        self.assertEqual(days[0]["effort_cue"], "Leave 1-2 reps in the tank")

    def test_the_literal_path_is_not_parsed_as_a_date(self) -> None:
        # /api/plan/{date} is declared later; if ordering regressed this 404s
        # or tries to read a plan day called "strength".
        response = self.client.get("/api/plan/strength",
                                   params={"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("days", response.json())

    def test_a_routine_that_could_never_be_created_is_refused(self) -> None:
        response = self._attach(routine={"title": "Bad", "exercises": [
            {"exercise_template_id": "", "sets": [{"type": "normal", "reps": 5}]}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid routine")

    def test_a_model_guessed_weight_is_refused_at_the_calendar_too(self) -> None:
        response = self._attach(routine={"title": "Bad", "exercises": [{
            "exercise_template_id": "D04AC939",
            "sets": [{"type": "normal", "reps": 5, "weight_kg": 100,
                      "weight_provenance": "model_guess"}]}]})
        self.assertEqual(response.status_code, 400)

    def test_a_missing_date_is_refused(self) -> None:
        self.assertEqual(self._attach(date="").status_code, 400)

    def test_an_unsupported_slot_is_refused(self) -> None:
        self.assertEqual(self._attach(slot="cardio").status_code, 400)

    def test_removing_reports_whether_anything_was_there(self) -> None:
        self._attach()
        first = self.client.delete("/api/plan/strength/2026-08-24").json()
        second = self.client.delete("/api/plan/strength/2026-08-24").json()
        self.assertTrue(first["removed"])
        self.assertFalse(second["removed"])

    def test_the_effort_endpoint_reports_a_decision_and_its_reasoning(self) -> None:
        with patch.object(main.garmin_source, "get_readiness",
                          return_value={"training_readiness": {"score": 80}}):
            payload = self.client.get("/api/plan/strength-effort").json()
        self.assertIn(payload["level"], ("heavy", "moderate", "light", "skip"))
        self.assertTrue(payload["reasons"])

    def test_the_effort_endpoint_survives_an_unavailable_garmin(self) -> None:
        with patch.object(main.garmin_source, "get_readiness",
                          side_effect=RuntimeError("garmin down")):
            response = self.client.get("/api/plan/strength-effort")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["readiness_score"])


if __name__ == "__main__":
    unittest.main()
