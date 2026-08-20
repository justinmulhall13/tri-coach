from __future__ import annotations

import unittest
from unittest.mock import patch

from app import hevy_actions, hevy_connector


CATALOG = [
    {"id": "SQBAR", "title": "Squat (Barbell)", "equipment": "barbell"},
    {"id": "SQBAND", "title": "Squat (Band)", "equipment": "resistance_band"},
    {"id": "REARDELT", "title": "Rear Delt Reverse Fly (Dumbbell)", "equipment": "dumbbell"},
    {"id": "ROWDB", "title": "Dumbbell Row", "equipment": "dumbbell"},
]


class FakeConnector:
    def __init__(self, *, catalog=None, create_ok: bool = True) -> None:
        self.catalog = CATALOG if catalog is None else catalog
        self.created: list[dict] = []
        self._create_ok = create_ok

    def status(self):
        return {"connected": True}

    def search_exercise_templates(self, query):
        # Mirrors the real adapter: a naive substring match on the whole query,
        # which is exactly why it cannot find "Rear Delt Reverse Fly".
        needle = (query or "").casefold().strip()
        return [t for t in self.catalog
                if needle and needle in t["title"].casefold()]

    def all_exercise_templates(self):
        return list(self.catalog)

    def create_exercise_template(self, exercise, *, idempotency_key):
        if not self._create_ok:
            raise RuntimeError("create refused")
        self.created.append(exercise)
        return {"id": "CREATED1", **exercise}


def _routine(*titles) -> dict:
    return {"title": "Upper", "exercises": [
        {"title": t, "sets": [{"type": "normal", "reps": 8}]} for t in titles]}


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = hevy_connector.connector()
        self.addCleanup(hevy_connector.configure, self.previous)

    def _resolve(self, routine, fake, history=None, **kw):
        # Patch the history source too: without it this reaches the live API,
        # which is both slow and non-deterministic.
        with patch.object(hevy_connector, "history_template_counts",
                          return_value=history if history is not None else {"SQBAR": 5}):
            hevy_connector.configure(fake)
            return hevy_actions.resolve_routine_exercises(routine, connector=fake, **kw)

    def test_an_exercise_named_only_by_title_gets_a_real_template_id(self) -> None:
        routine, reports = self._resolve(_routine("Dumbbell Row"), FakeConnector())
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "ROWDB")
        self.assertEqual(reports[0]["resolution"], "matched")

    def test_the_variant_the_athlete_uses_is_preferred(self) -> None:
        # Without the history bias this resolves to Squat (Band).
        routine, _ = self._resolve(_routine("Squat"), FakeConnector())
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "SQBAR")

    def test_the_most_used_variant_wins_when_several_are_in_history(self) -> None:
        # All three logged; frequency is what separates them, not the alphabet.
        routine, _ = self._resolve(
            _routine("Squat"), FakeConnector(),
            history={"SQBAND": 1, "SQBAR": 40})
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "SQBAR")

    def test_a_renamed_movement_is_found_even_though_substring_search_misses_it(self) -> None:
        # "Rear Delt Fly" is not a substring of "Rear Delt Reverse Fly", so the
        # search endpoint returns nothing; ranking the full catalogue finds it.
        fake = FakeConnector()
        self.assertEqual(fake.search_exercise_templates("Rear Delt Fly"), [])
        routine, reports = self._resolve(_routine("Rear Delt Fly"), fake)
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "REARDELT")
        self.assertEqual(reports[0]["resolution"], "matched")
        self.assertEqual(fake.created, [])

    def test_a_renamed_movement_is_found_rather_than_duplicated(self) -> None:
        fake = FakeConnector()
        routine, reports = self._resolve(_routine("Rear Delt Fly"), fake)
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "REARDELT")
        self.assertEqual(fake.created, [])

    def test_a_genuinely_missing_exercise_is_created_not_dropped(self) -> None:
        fake = FakeConnector(catalog=[])
        routine, reports = self._resolve(_routine("Copenhagen Plank"), fake)
        self.assertEqual(routine["exercises"][0]["exercise_template_id"], "CREATED1")
        self.assertEqual(reports[0]["resolution"], "created")
        self.assertEqual(len(fake.created), 1)

    def test_the_session_keeps_every_exercise_even_when_one_fails(self) -> None:
        # The old behaviour turned a 6-exercise session into 3 without saying why.
        fake = FakeConnector(catalog=[], create_ok=False)
        routine, reports = self._resolve(
            _routine("Dumbbell Row", "Nonsense Move", "Squat"), fake)
        self.assertEqual(len(routine["exercises"]), 3)
        self.assertEqual(reports[1]["resolution"], "failed")
        self.assertIn("create refused", reports[1]["reason"])

    def test_an_explicit_template_id_is_left_alone(self) -> None:
        routine = {"title": "Upper", "exercises": [
            {"exercise_template_id": "GIVEN1", "title": "Squat",
             "sets": [{"type": "normal", "reps": 5}]}]}
        resolved, reports = self._resolve(routine, FakeConnector())
        self.assertEqual(resolved["exercises"][0]["exercise_template_id"], "GIVEN1")
        self.assertEqual(reports, [])

    def test_creation_can_be_withheld(self) -> None:
        fake = FakeConnector(catalog=[])
        _, reports = self._resolve(_routine("Nonsense Move"), fake, create_missing=False)
        self.assertEqual(reports[0]["resolution"], "missing")
        self.assertEqual(fake.created, [])

    def test_a_malformed_routine_does_not_raise(self) -> None:
        for bad in (None, 42, "routine", {}, {"exercises": "nope"}):
            routine, reports = self._resolve(bad, FakeConnector())
            self.assertEqual(reports, [])

    def test_a_resolved_routine_then_passes_validation(self) -> None:
        routine, _ = self._resolve(_routine("Dumbbell Row", "Squat"), FakeConnector())
        validated, errors = hevy_actions.validate_routine(routine)
        self.assertEqual(errors, [])
        self.assertIsNotNone(validated)


if __name__ == "__main__":
    unittest.main()
