from __future__ import annotations

import unittest

from app import lifting_rules as lr
from app import lifting_split as lsp


class SplitShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.split = lsp.build()

    def test_it_is_a_three_day_full_body_split(self) -> None:
        self.assertEqual(self.split["sessions_per_week"], 3)
        self.assertEqual([d["slot"] for d in self.split["days"]], list(lsp.SLOTS))
        self.assertEqual({d["tag"] for d in self.split["days"]}, {"Full body"})

    def test_every_day_trains_legs_and_a_pull(self) -> None:
        for day in self.split["days"]:
            groups = {e["group"] for e in day["exercises"]}
            self.assertTrue(groups & {"quad", "hinge"}, day["name"])
            self.assertIn("pull", groups, day["name"])

    def test_the_friday_day_spares_the_shoulder_from_pressing(self) -> None:
        friday = self.split["days"][-1]
        self.assertEqual(friday["rule_status"]["press_count"], 0)

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
        # Keep only the upper-body work; the week must say what it now misses.
        upper_only = [{**d, "exercises": [e for e in d["exercises"]
                                          if lr.group_of(e) in {"push", "pull", "shoulder"}]}
                      for d in lsp.DEFAULT_SPLIT]
        coverage = lsp.build(upper_only)["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertIn("quad", coverage["missing"])
        self.assertIn("calf", coverage["missing"])


class RunnerSpecificTests(unittest.TestCase):
    """Leg work exists to support the swim, bike and run, not to compete with them."""

    def setUp(self) -> None:
        self.lower = lsp.build()["days"]

    def test_calves_are_trained_at_least_twice_a_week(self) -> None:
        days = [d for d in self.lower
                if "calf" in [e["group"] for e in d["exercises"]]]
        self.assertGreaterEqual(len(days), 2)

    def test_leg_load_falls_through_the_week(self) -> None:
        # A (Monday) is heaviest; C (Friday, before the long ride) is plyometric.
        friday = " ".join(e["title"].lower() for e in self.lower[-1]["exercises"])
        self.assertIn("jump", friday)
        self.assertNotIn("squat", friday)

    def test_the_week_includes_eccentric_hamstring_work(self) -> None:
        titles = " ".join(e["title"].lower() for d in self.lower for e in d["exercises"])
        self.assertIn("nordic", titles)

    def test_the_week_includes_single_leg_loading(self) -> None:
        titles = " ".join(e["title"].lower() for d in self.lower for e in d["exercises"])
        self.assertTrue("split squat" in titles or "single leg" in titles, titles)

    def test_leg_work_leans_posterior_rather_than_quad_heavy(self) -> None:
        groups = [e["group"] for d in self.lower for e in d["exercises"]]
        self.assertGreaterEqual(groups.count("hinge"), groups.count("quad"))

    def test_every_exercise_states_why_it_is_there(self) -> None:
        for day in lsp.build()["days"]:
            for exercise in day["exercises"]:
                self.assertTrue(exercise["why"], exercise["title"])
                self.assertTrue(exercise["role"], exercise["title"])


class PlaneSeparationTests(unittest.TestCase):
    def test_no_two_days_are_the_same_session(self) -> None:
        days = lsp.build()["days"]
        for i, a in enumerate(days):
            for b in days[i + 1:]:
                self.assertEqual({e["title"] for e in a["exercises"]}
                                 & {e["title"] for e in b["exercises"]}, set())

    def test_the_week_pulls_both_vertically_and_horizontally(self) -> None:
        patterns = [{e["pattern"] for e in d["exercises"]} for d in lsp.build()["days"]]
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
