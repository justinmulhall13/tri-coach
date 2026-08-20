from __future__ import annotations

import unittest

from app import hevy_actions, hevy_connector
from app import strength_weights as sw


SQUAT = "D04AC939"
ANCHOR_KG = 142.881766472226530  # 315 lb, the athlete's real 2026-06-09 top set


class FakeHevy:
    """Connector that only ever reports the one set the athlete really did."""

    def __init__(self, *, anchor_kg: float = ANCHOR_KG) -> None:
        self.created = 0
        self.sent: dict | None = None
        self._anchor_kg = anchor_kg

    def status(self) -> dict:
        return {"connected": True}

    def get_exercise_template(self, template_id: str) -> dict:
        return {"id": template_id, "title": "Squat (Barbell)"}

    def get_workouts(self, *, page: int, page_size: int) -> dict:
        return {"workouts": [{
            "id": "w1", "start_time": "2026-06-09T14:41:51Z",
            "exercises": [{
                "exercise_template_id": SQUAT,
                "sets": [{"type": "normal", "reps": 5, "weight_kg": self._anchor_kg}],
            }],
        }]}

    def create_routine(self, routine: dict, *, idempotency_key: str) -> dict:
        self.created += 1
        self.sent = routine
        return {"id": "r1", **routine}


def _derived_set(pct: float = 0.85, **overrides) -> dict:
    derived = sw.derive_working_weight(
        anchor_weight_kg=ANCHOR_KG, anchor_reps=5, anchor_date="2026-06-09",
        exercise_template_id=SQUAT, pct=pct, increment_lb=5.0,
    )
    item = {
        "type": "normal", "reps": 5,
        "weight_kg": derived["weight_kg"],
        "weight_provenance": derived["weight_provenance"],
        "derivation": derived["derivation"],
    }
    item.update(overrides)
    return item


def _routine(item: dict) -> dict:
    return {
        "title": "Lower - running specific",
        "exercises": [{"exercise_template_id": SQUAT, "rest_seconds": 150, "sets": [item]}],
    }


def _create(routine: dict, fake: FakeHevy, *, operation_id: str = "op-1") -> dict:
    previous = hevy_connector.connector()
    hevy_connector.configure(fake)
    try:
        return hevy_actions.create_confirmed_routine(
            routine, operation_id=operation_id, confirmed=True,
        )
    finally:
        hevy_connector.configure(previous)


class ValidationTests(unittest.TestCase):
    def test_a_derived_weight_with_a_full_record_validates(self) -> None:
        routine, errors = hevy_actions.validate_routine(_routine(_derived_set()))
        self.assertEqual(errors, [])
        self.assertIsNotNone(routine)
        item = routine["exercises"][0]["sets"][0]
        self.assertEqual(item["weight_provenance"], "hevy_derived")
        self.assertEqual(item["derivation"]["anchor_weight_kg"], ANCHOR_KG)

    def test_a_derived_claim_without_a_derivation_record_is_refused(self) -> None:
        item = _derived_set()
        item.pop("derivation")
        routine, errors = hevy_actions.validate_routine(_routine(item))
        self.assertIsNone(routine)
        self.assertTrue(any("no derivation record" in error for error in errors))

    def test_a_weight_derived_from_a_different_exercise_is_refused(self) -> None:
        item = _derived_set()
        item["derivation"] = {**item["derivation"], "exercise_template_id": "DDCC3821"}
        routine, errors = hevy_actions.validate_routine(_routine(item))
        self.assertIsNone(routine)
        self.assertTrue(any("different exercise" in error for error in errors))

    def test_an_unlabelled_weight_is_still_refused(self) -> None:
        item = _derived_set()
        item["weight_provenance"] = "model_guess"
        routine, errors = hevy_actions.validate_routine(_routine(item))
        self.assertIsNone(routine)
        self.assertTrue(any("exact Hevy value" in error for error in errors))


class WriteBoundaryTests(unittest.TestCase):
    def test_a_sound_derivation_is_created_once(self) -> None:
        fake = FakeHevy()
        result = _create(_routine(_derived_set()), fake)
        self.assertTrue(result["created"])
        self.assertEqual(fake.created, 1)

    def test_hevy_never_receives_the_internal_provenance_keys(self) -> None:
        fake = FakeHevy()
        _create(_routine(_derived_set()), fake)
        sent = fake.sent["exercises"][0]["sets"][0]
        self.assertIn("weight_kg", sent)
        self.assertNotIn("weight_provenance", sent)
        self.assertNotIn("derivation", sent)

    def test_the_created_weight_is_the_derived_pounds_not_the_anchor(self) -> None:
        fake = FakeHevy()
        _create(_routine(_derived_set(pct=0.85)), fake)
        sent_kg = fake.sent["exercises"][0]["sets"][0]["weight_kg"]
        self.assertEqual(sw.history_weight_lb(sent_kg), 270.0)

    def test_an_anchor_absent_from_history_blocks_the_write(self) -> None:
        # The connector reports a different set than the one claimed as anchor.
        fake = FakeHevy(anchor_kg=sw.lb_to_kg(225.0))
        result = _create(_routine(_derived_set()), fake)
        self.assertFalse(result["created"])
        self.assertEqual(fake.created, 0)
        self.assertIn("could not be traced", result["error"])
        self.assertIn("not found in fetched Hevy history", result["details"][0]["reason"])

    def test_a_weight_contradicting_its_own_derivation_blocks_the_write(self) -> None:
        item = _derived_set(pct=0.85)
        item["weight_kg"] = ANCHOR_KG  # claims a deload, ships the full anchor
        result = _create(_routine(item), fake := FakeHevy())
        self.assertFalse(result["created"])
        self.assertEqual(fake.created, 0)
        self.assertIn("does not match its own derivation", result["details"][0]["reason"])

    def test_an_out_of_band_progression_blocks_the_write(self) -> None:
        item = _derived_set()
        item["derivation"] = {**item["derivation"], "pct": 1.5}
        item["weight_kg"] = sw.lb_to_kg(472.5)
        result = _create(_routine(item), fake := FakeHevy())
        self.assertFalse(result["created"])
        self.assertEqual(fake.created, 0)
        self.assertIn("safe band", result["details"][0]["reason"])

    def test_a_failure_reports_the_offending_weight_in_pounds(self) -> None:
        fake = FakeHevy(anchor_kg=sw.lb_to_kg(225.0))
        result = _create(_routine(_derived_set()), fake)
        self.assertEqual(result["details"][0]["weight_lb"], 270.0)

    def test_an_exact_history_weight_still_works_alongside_derivation(self) -> None:
        item = {"type": "normal", "reps": 5, "weight_kg": ANCHOR_KG,
                "weight_provenance": "hevy_history"}
        result = _create(_routine(item), fake := FakeHevy())
        self.assertTrue(result["created"])
        self.assertNotIn("weight_provenance", fake.sent["exercises"][0]["sets"][0])


if __name__ == "__main__":
    unittest.main()
