from __future__ import annotations

import unittest

from app import hevy_actions, hevy_connector


def _routine(weight=False):
    set_data = {"type": "normal", "rep_range": {"start": 8, "end": 12}}
    if weight:
        set_data.update({"weight_kg": 60, "weight_provenance": "model_guess"})
    return {
        "title": "Running-specific legs",
        "exercises": [{
            "exercise_template_id": "DDCC3821",
            "rest_seconds": 120,
            "sets": [set_data, {"type": "normal", "reps": 8}],
        }],
    }


class HevyActionTests(unittest.TestCase):
    def test_model_guessed_working_weight_is_rejected(self) -> None:
        routine, errors = hevy_actions.validate_routine(_routine(weight=True))
        self.assertIsNone(routine)
        self.assertTrue(any("exact Hevy value" in error for error in errors))

    def test_write_requires_explicit_confirmation(self) -> None:
        result = hevy_actions.create_confirmed_routine(
            _routine(), operation_id="draft-1", confirmed=False,
        )
        self.assertFalse(result["created"])
        self.assertIn("confirmation", result["error"])

    def test_confirmed_write_verifies_template_then_creates_once(self) -> None:
        class Fake:
            def __init__(self):
                self.created = 0

            def status(self):
                return {"connected": True}

            def get_exercise_template(self, template_id):
                return {"id": template_id, "title": "Squat"}

            def create_routine(self, routine, *, idempotency_key):
                self.created += 1
                return {"id": "r1", **routine}

        fake = Fake()
        old = hevy_connector.connector()
        try:
            hevy_connector.configure(fake)
            result = hevy_actions.create_confirmed_routine(
                _routine(), operation_id="draft-2", confirmed=True,
            )
        finally:
            hevy_connector.configure(old)
        self.assertTrue(result["created"])
        self.assertEqual(fake.created, 1)
        self.assertEqual(result["verified_template_ids"], ["DDCC3821"])

    def test_custom_metric_survives_and_fractional_integer_is_rejected(self) -> None:
        routine = _routine()
        routine["exercises"][0]["sets"] = [{"type": "normal", "custom_metric": 2.5}]
        clean, errors = hevy_actions.validate_routine(routine)
        self.assertEqual(errors, [])
        self.assertEqual(clean["exercises"][0]["sets"][0]["custom_metric"], 2.5)

        routine["exercises"][0]["sets"] = [{"type": "normal", "reps": 8.5}]
        clean, errors = hevy_actions.validate_routine(routine)
        self.assertIsNone(clean)
        self.assertTrue(any("needs reps" in error for error in errors))

    def test_oversized_set_list_and_invalid_folder_are_explicit_errors(self) -> None:
        routine = _routine()
        routine["exercises"][0]["sets"] = [
            {"type": "normal", "reps": 8} for _ in range(13)
        ]
        clean, errors = hevy_actions.validate_routine(routine)
        self.assertIsNone(clean)
        self.assertTrue(any("more than 12 sets" in error for error in errors))

        routine = _routine()
        routine["folder_id"] = "not-a-number"
        clean, errors = hevy_actions.validate_routine(routine)
        self.assertIsNone(clean)
        self.assertIn("folder_id must be a finite number or null", errors)

    def test_claimed_hevy_weight_must_match_exact_recent_history(self) -> None:
        routine = _routine()
        routine["exercises"][0]["sets"] = [{
            "type": "normal", "reps": 8, "weight_kg": 999,
            "weight_provenance": "hevy_history",
        }]

        class Fake:
            def __init__(self):
                self.created = 0

            def status(self):
                return {"connected": True}

            def get_exercise_template(self, template_id):
                return {"id": template_id, "title": "Squat"}

            def get_workouts(self, *, page, page_size):
                return {"workouts": [{"id": "w1", "exercises": [{
                    "exercise_template_id": "DDCC3821",
                    "sets": [{"type": "normal", "reps": 8, "weight_kg": 60}],
                }]}]}

            def create_routine(self, routine, *, idempotency_key):
                self.created += 1
                return {"id": "r1", **routine}

        fake = Fake()
        old = hevy_connector.connector()
        try:
            hevy_connector.configure(fake)
            result = hevy_actions.create_confirmed_routine(
                routine, operation_id="draft-bad-weight", confirmed=True,
            )
        finally:
            hevy_connector.configure(old)
        self.assertFalse(result["created"])
        self.assertIn("could not be traced to Hevy history", result["error"])
        self.assertIn("not found", result["details"][0]["reason"])
        self.assertEqual(fake.created, 0)

    def test_template_response_id_must_match_requested_id(self) -> None:
        class Fake:
            def status(self):
                return {"connected": True}

            def get_exercise_template(self, _template_id):
                return {"id": "different", "title": "Wrong"}

        old = hevy_connector.connector()
        try:
            hevy_connector.configure(Fake())
            result = hevy_actions.create_confirmed_routine(
                _routine(), operation_id="draft-template-mismatch", confirmed=True,
            )
        finally:
            hevy_connector.configure(old)
        self.assertFalse(result["created"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
