"""A proposed Hevy routine names every exercise, not "Exercise 2".

The validated routine keeps only the template id for a resolved exercise, and
the card used to name it from recent history alone — so any movement the
athlete had never logged (weighted pull-up, Pallof press) rendered as a
numbered placeholder, and with a cold history cache every exercise did.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import coach, hevy_connector

ROUTINE = {"title": "Day 1", "exercises": [
    {"exercise_template_id": "fs", "sets": [{"type": "normal", "reps": 5}]},
    {"exercise_template_id": "wpu", "sets": [{"type": "normal", "reps": 5}]},
    {"exercise_template_id": "pal", "sets": [{"type": "normal", "reps": 10}]},
]}
NOTES = {"resolution": [
    {"title": "Front Squat", "exercise_template_id": "fs",
     "resolution": "matched", "matched_title": "Front Squat"},
    {"title": "Weighted Pull-up", "exercise_template_id": "wpu",
     "resolution": "matched", "matched_title": "Pull Up (Weighted)"},
    {"title": "Pallof Press", "exercise_template_id": "pal",
     "resolution": "matched", "matched_title": "Pallof Press"},
]}


def _titles(view):
    return [e["title"] for e in view["exercises"]]


class CardNameTests(unittest.TestCase):
    def test_cold_caches_still_name_every_exercise_from_resolution(self) -> None:
        with (patch.object(hevy_connector, "known_exercises", return_value=({}, {})),
              patch.object(hevy_connector, "catalog_names", return_value=({}, {}))):
            view = coach._hevy_routine_view(ROUTINE, NOTES)
        self.assertEqual(_titles(view), ["Front Squat", "Pull Up (Weighted)", "Pallof Press"])

    def test_never_logged_exercises_are_named_from_the_catalogue(self) -> None:
        history = ({"fs": "Front Squat"}, {})
        catalog = ({"fs": "Front Squat", "wpu": "Pull Up (Weighted)", "pal": "Pallof Press"}, {})
        with (patch.object(hevy_connector, "known_exercises", return_value=history),
              patch.object(hevy_connector, "catalog_names", return_value=catalog)):
            view = coach._hevy_routine_view(ROUTINE, {})
        self.assertEqual(_titles(view), ["Front Squat", "Pull Up (Weighted)", "Pallof Press"])

    def test_no_placeholder_survives_when_any_source_knows_the_name(self) -> None:
        with (patch.object(hevy_connector, "known_exercises", return_value=({}, {})),
              patch.object(hevy_connector, "catalog_names", return_value=({}, {}))):
            view = coach._hevy_routine_view(ROUTINE, NOTES)
        self.assertFalse([t for t in _titles(view) if t.startswith("Exercise ")])


if __name__ == "__main__":
    unittest.main()
