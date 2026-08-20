from __future__ import annotations

import unittest

from app import strength_visual as sv
from app import strength_weights as sw


class ClassifyTests(unittest.TestCase):
    def test_the_athletes_real_exercises_classify_to_drawable_patterns(self) -> None:
        cases = {
            "Squat (Barbell)": "squat",
            "Squat (Smith Machine)": "squat",
            "Front Squat": "squat",
            "Zercher Squat": "squat",
            "Romanian Deadlift (Barbell)": "hinge",
            "Good Morning (Barbell)": "hinge",
            "Hip Thrust (Smith Machine)": "hinge",
            "Bulgarian Split Squat": "lunge",
            "Lat Pulldown (Cable)": "pull_vertical",
            "Pull Up (Weighted)": "pull_vertical",
            "T Bar Row": "pull_horizontal",
            "Face Pull": "pull_horizontal",
            "Incline Bench Press (Dumbbell)": "push_horizontal",
            "Floor Press (Barbell)": "push_horizontal",
            "JM Press (Barbell)": "push_horizontal",
            "Overhead Press (Dumbbell)": "push_vertical",
            "Half Kneeling Landmine Press": "push_vertical",
            "Calf Extension (Machine)": "calf",
            "Lateral Raise (Cable)": "raise",
            "Bicep Curl (Cable)": "curl",
            "Triceps Rope Pushdown": "triceps",
            "Dead Bug": "core",
            "Box Jump": "plyo",
            "Hang Clean": "olympic",
        }
        for title, expected in cases.items():
            self.assertEqual(sv.classify(title), expected, title)

    def test_a_movement_beats_the_muscle_it_happens_to_load(self) -> None:
        # Both are hamstring exercises; only one is a hinge.
        self.assertEqual(sv.classify("Romanian Deadlift (Barbell)",
                                     primary_muscle_group="hamstrings"), "hinge")
        self.assertEqual(sv.classify("Nordic Hamstrings Curls",
                                     primary_muscle_group="hamstrings"), "curl")

    def test_muscle_group_is_used_when_the_title_says_nothing(self) -> None:
        self.assertEqual(sv.classify("Machine Thing", primary_muscle_group="calves"), "calf")
        self.assertEqual(sv.classify("Some Widget", primary_muscle_group="chest"),
                         "push_horizontal")

    def test_an_unknown_exercise_falls_back_rather_than_guessing_wrong(self) -> None:
        self.assertEqual(sv.classify("Mystery Move"), "other")
        self.assertEqual(sv.classify(None), "other")

    def test_every_pattern_produced_is_one_the_ui_can_draw(self) -> None:
        for title in ("Squat", "Face Pull", "Box Jump", "Nonsense", ""):
            self.assertIn(sv.classify(title), sv.PATTERNS)


ANCHOR_KG = 142.881766472226530  # 315 lb


def _derived_set(pct: float = 0.85) -> dict:
    derived = sw.derive_working_weight(
        anchor_weight_kg=ANCHOR_KG, anchor_reps=5, anchor_date="2026-06-09",
        exercise_template_id="D04AC939", pct=pct, increment_lb=5.0,
    )
    return {"type": "normal", "reps": 5, "weight_kg": derived["weight_kg"],
            "weight_provenance": derived["weight_provenance"],
            "derivation": derived["derivation"]}


class BuildViewTests(unittest.TestCase):
    def _routine(self, sets: list[dict]) -> dict:
        return {"title": "Lower - running specific",
                "exercises": [{"exercise_template_id": "D04AC939",
                               "rest_seconds": 150, "sets": sets}]}

    def test_weights_are_shown_in_pounds_never_raw_kilograms(self) -> None:
        view = sv.build_view(self._routine([_derived_set()]),
                             titles={"D04AC939": "Squat (Barbell)"})
        item = view["exercises"][0]["sets"][0]
        self.assertEqual(item["weight"], "270 lb")
        self.assertNotIn("142.88", str(view))

    def test_the_card_states_the_evidence_behind_a_derived_weight(self) -> None:
        view = sv.build_view(self._routine([_derived_set()]),
                             titles={"D04AC939": "Squat (Barbell)"})
        note = view["exercises"][0]["sets"][0]["weight_note"]
        self.assertIn("85% of", note)
        self.assertIn("315 lb x5", note)
        self.assertIn("2026-06-09", note)

    def test_an_exact_history_weight_is_labelled_as_previously_lifted(self) -> None:
        view = sv.build_view(self._routine([
            {"type": "normal", "reps": 5, "weight_kg": ANCHOR_KG,
             "weight_provenance": "hevy_history"}]))
        self.assertEqual(view["exercises"][0]["sets"][0]["weight_note"],
                         "a weight you have lifted before")

    def test_a_bodyweight_set_shows_no_weight_rather_than_zero(self) -> None:
        view = sv.build_view(self._routine([{"type": "normal", "reps": 8}]))
        item = view["exercises"][0]["sets"][0]
        self.assertIsNone(item["weight"])
        self.assertEqual(item["prescription"], "8 reps")

    def test_a_rep_range_is_rendered_as_a_range(self) -> None:
        view = sv.build_view(self._routine([
            {"type": "normal", "rep_range": {"start": 8, "end": 10}}]))
        self.assertEqual(view["exercises"][0]["sets"][0]["prescription"], "8-10 reps")

    def test_identical_sets_collapse_into_one_readable_line(self) -> None:
        view = sv.build_view(self._routine([_derived_set(), _derived_set(), _derived_set()]))
        self.assertEqual(view["exercises"][0]["summary"], "3 x 5 reps @ 270 lb")

    def test_warmup_sets_do_not_inflate_the_working_set_count(self) -> None:
        view = sv.build_view(self._routine([
            {"type": "warmup", "reps": 5}, _derived_set(), _derived_set()]))
        self.assertEqual(view["exercises"][0]["set_count"], 2)

    def test_the_exercise_is_named_from_hevy_not_invented(self) -> None:
        view = sv.build_view(self._routine([_derived_set()]),
                             titles={"D04AC939": "Squat (Barbell)"})
        self.assertEqual(view["exercises"][0]["title"], "Squat (Barbell)")
        self.assertEqual(view["exercises"][0]["pattern"], "squat")

    def test_an_unnamed_exercise_gets_a_placeholder_not_a_fabricated_name(self) -> None:
        view = sv.build_view(self._routine([_derived_set()]))
        self.assertEqual(view["exercises"][0]["title"], "Exercise 1")

    def test_the_effort_cue_is_carried_for_display(self) -> None:
        view = sv.build_view(self._routine([_derived_set()]),
                             effort_cue="Leave 2 in the tank")
        self.assertEqual(view["effort_cue"], "Leave 2 in the tank")

    def test_malformed_exercises_are_skipped_without_raising(self) -> None:
        view = sv.build_view({"title": "x", "exercises": ["nonsense", None, 42]})
        self.assertEqual(view["exercises"], [])
        self.assertEqual(view["exercise_count"], 0)

    def test_an_empty_routine_produces_an_empty_card_not_an_error(self) -> None:
        view = sv.build_view({})
        self.assertEqual(view["title"], "Strength session")
        self.assertEqual(view["exercises"], [])


if __name__ == "__main__":
    unittest.main()
