from __future__ import annotations

import unittest

from app import lifting_rules as lr
from app import lifting_split as lsp


class SplitShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.split = lsp.build()

    def test_it_is_a_four_day_upper_lower_split(self) -> None:
        self.assertEqual(self.split["sessions_per_week"], 4)
        self.assertEqual([d["slot"] for d in self.split["days"]], list(lsp.SLOTS))

    def test_upper_and_lower_alternate_through_the_week(self) -> None:
        kinds = ["lower" if d["is_lower"] else "upper" for d in self.split["days"]]
        self.assertEqual(kinds, ["upper", "lower", "upper", "lower"])

    def test_every_day_has_six_exercises(self) -> None:
        for day in self.split["days"]:
            self.assertEqual(day["exercise_count"], 6, day["name"])

    def test_every_day_passes_the_athletes_rules(self) -> None:
        self.assertEqual(self.split["violations"], [], self.split["violations"])
        self.assertTrue(self.split["ok"])

    def test_no_upper_day_uses_more_than_one_press(self) -> None:
        for day in self.split["days"]:
            self.assertLessEqual(day["rule_status"]["press_count"], 1, day["name"])

    def test_no_day_stacks_three_back_movements(self) -> None:
        for day in self.split["days"]:
            self.assertLessEqual(day["rule_status"]["back_count"],
                                 lr.MAX_BACK_MOVEMENTS, day["name"])

    def test_no_face_pulls_anywhere_in_the_split(self) -> None:
        titles = " ".join(e["title"] for d in self.split["days"] for e in d["exercises"])
        self.assertNotIn("face pull", titles.lower())


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.split = lsp.build()

    def test_the_week_leaves_no_major_group_untrained(self) -> None:
        coverage = self.split["coverage"]
        self.assertTrue(coverage["complete"], coverage["missing"])
        self.assertEqual(coverage["missing"], [])

    def test_every_required_group_is_actually_present(self) -> None:
        counts = self.split["coverage"]["sets_by_group"]
        for group in lsp.REQUIRED_COVERAGE:
            self.assertGreater(counts.get(group, 0), 0, group)

    def test_a_gap_in_coverage_is_reported_rather_than_hidden(self) -> None:
        # Drop both leg days; the week must say what it now misses.
        upper_only = [d for d in lsp.DEFAULT_SPLIT if d["slot"].startswith("upper")]
        coverage = lsp.build(upper_only)["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertIn("quad", coverage["missing"])
        self.assertIn("calf", coverage["missing"])


class RunnerSpecificTests(unittest.TestCase):
    """Leg days exist to support the running, not to compete with it."""

    def setUp(self) -> None:
        self.lower = [d for d in lsp.build()["days"] if d["is_lower"]]

    def test_both_leg_days_train_calves(self) -> None:
        for day in self.lower:
            groups = [e["group"] for e in day["exercises"]]
            self.assertIn("calf", groups, day["name"])

    def test_the_week_includes_eccentric_hamstring_work(self) -> None:
        titles = " ".join(e["title"].lower() for d in self.lower for e in d["exercises"])
        self.assertIn("nordic", titles)

    def test_the_week_includes_single_leg_loading(self) -> None:
        titles = " ".join(e["title"].lower() for d in self.lower for e in d["exercises"])
        self.assertTrue("split squat" in titles or "single leg" in titles, titles)

    def test_leg_days_lean_posterior_rather_than_quad_heavy(self) -> None:
        groups = [e["group"] for d in self.lower for e in d["exercises"]]
        self.assertGreaterEqual(groups.count("hinge"), groups.count("quad"))

    def test_every_exercise_states_why_it_is_there(self) -> None:
        for day in lsp.build()["days"]:
            for exercise in day["exercises"]:
                self.assertTrue(exercise["why"], exercise["title"])
                self.assertTrue(exercise["role"], exercise["title"])


class PlaneSeparationTests(unittest.TestCase):
    def test_the_two_upper_days_are_not_the_same_session(self) -> None:
        upper = [d for d in lsp.build()["days"] if not d["is_lower"]]
        first = {e["title"] for e in upper[0]["exercises"]}
        second = {e["title"] for e in upper[1]["exercises"]}
        self.assertEqual(first & second, set())

    def test_one_upper_day_pulls_vertically_and_the_other_horizontally(self) -> None:
        upper = [d for d in lsp.build()["days"] if not d["is_lower"]]
        patterns = [{e["pattern"] for e in d["exercises"]} for d in upper]
        self.assertTrue(any("pull_horizontal" in p for p in patterns))
        self.assertTrue(any("pull_vertical" in p for p in patterns))


class CustomSplitTests(unittest.TestCase):
    def test_an_edited_split_is_summarised_the_same_way(self) -> None:
        edited = [{"slot": "upper_1", "name": "My Upper", "focus": "test",
                   "exercises": [{"title": "Dumbbell Row"}, {"title": "Bench Press"}]}]
        built = lsp.build(edited)
        self.assertEqual(built["days"][0]["name"], "My Upper")
        self.assertEqual(built["days"][0]["exercise_count"], 2)

    def test_an_edited_split_that_breaks_a_rule_is_reported(self) -> None:
        bad = [{"slot": "upper_1", "name": "Two presses", "focus": "test",
                "exercises": [{"title": "Bench Press"}, {"title": "Overhead Press"}]}]
        violations = lsp.build(bad)["violations"]
        self.assertTrue(any(v["rule"] == "one_press_per_session" for v in violations))
        self.assertFalse(lsp.build(bad)["ok"])

    def test_malformed_input_falls_back_rather_than_raising(self) -> None:
        for bad in (None, [], "split", 42, [None, 7]):
            built = lsp.build(bad)
            self.assertIn("days", built)


if __name__ == "__main__":
    unittest.main()
