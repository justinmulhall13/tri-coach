from __future__ import annotations

import unittest

from app import lifting_rules as lr


def _ex(title: str) -> dict:
    return {"title": title}


# The session the coach originally proposed, which broke the shoulder rule by
# pairing a press with triceps work and stacking three back movements.
BAD_SESSION = [
    _ex("Dumbbell Row"), _ex("Floor Press (Barbell)"), _ex("Lat Pulldown (Cable)"),
    _ex("T Bar Row"), _ex("Face Pull"), _ex("Triceps Rope Pushdown"),
]

# The corrected shape: one press, no triceps, rear delt instead of face pull.
GOOD_SESSION = [
    _ex("Dumbbell Row"), _ex("Incline Bench Press (Dumbbell)"), _ex("Rear Delt Fly"),
    _ex("Chest Fly (Cable)"), _ex("Bicep Curl (Dumbbell)"), _ex("Dead Bug"),
]


class PressRuleTests(unittest.TestCase):
    def test_two_presses_in_one_session_is_an_injury_violation(self) -> None:
        session = [_ex("Bench Press (Barbell)"), _ex("Dumbbell Row"),
                   _ex("Overhead Press (Dumbbell)"), _ex("Lat Pulldown (Cable)"),
                   _ex("Bicep Curl (Dumbbell)"), _ex("Dead Bug")]
        violations = lr.check(session)
        press = [v for v in violations if v["rule"] == "one_press_per_session"]
        self.assertEqual(len(press), 1)
        self.assertEqual(press[0]["severity"], "injury")

    def test_a_single_press_is_allowed(self) -> None:
        rules = [v["rule"] for v in lr.check(GOOD_SESSION)]
        self.assertNotIn("one_press_per_session", rules)

    def test_pressing_with_triceps_isolation_is_an_injury_violation(self) -> None:
        session = [_ex("Bench Press (Barbell)"), _ex("Dumbbell Row"),
                   _ex("Triceps Rope Pushdown"), _ex("Lat Pulldown (Cable)"),
                   _ex("Bicep Curl (Dumbbell)"), _ex("Dead Bug")]
        violations = [v for v in lr.check(session) if v["rule"] == "no_triceps_with_press"]
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["severity"], "injury")

    def test_triceps_alone_without_a_press_is_fine(self) -> None:
        session = [_ex("Dumbbell Row"), _ex("Triceps Rope Pushdown"),
                   _ex("Lat Pulldown (Cable)"), _ex("Bicep Curl (Dumbbell)"),
                   _ex("Rear Delt Fly"), _ex("Dead Bug")]
        rules = [v["rule"] for v in lr.check(session)]
        self.assertNotIn("no_triceps_with_press", rules)


class BannedMovementTests(unittest.TestCase):
    def test_face_pulls_are_refused_with_a_reason(self) -> None:
        violations = [v for v in lr.check(BAD_SESSION) if v["rule"] == "banned_movement"]
        self.assertEqual(len(violations), 1)
        self.assertIn("rear delt fly", violations[0]["detail"])
        self.assertEqual(violations[0]["severity"], "injury")

    def test_the_rear_delt_replacement_is_accepted(self) -> None:
        rules = [v["rule"] for v in lr.check(GOOD_SESSION)]
        self.assertNotIn("banned_movement", rules)

    def test_face_pull_is_matched_however_it_is_written(self) -> None:
        for title in ("Face Pull", "face pulls", "Cable FacePull", "FACE  PULL"):
            violations = [v for v in lr.check([_ex(title)])
                          if v["rule"] == "banned_movement"]
            self.assertEqual(len(violations), 1, title)


class VolumeAndShapeTests(unittest.TestCase):
    def test_three_back_movements_is_flagged(self) -> None:
        violations = [v for v in lr.check(BAD_SESSION) if v["rule"] == "back_volume"]
        self.assertEqual(len(violations), 1)

    def test_a_session_that_is_not_six_exercises_is_flagged(self) -> None:
        violations = [v for v in lr.check(GOOD_SESSION[:3]) if v["rule"] == "exercise_count"]
        self.assertEqual(len(violations), 1)
        self.assertIn("3 exercises", violations[0]["detail"])

    def test_an_empty_session_is_not_reported_as_the_wrong_length(self) -> None:
        self.assertEqual(lr.check([]), [])

    def test_the_corrected_session_passes_every_rule(self) -> None:
        self.assertEqual(lr.check(GOOD_SESSION), [], lr.check(GOOD_SESSION))


class AlternationTests(unittest.TestCase):
    def test_two_pulls_back_to_back_are_flagged(self) -> None:
        session = [_ex("Dumbbell Row"), _ex("Lat Pulldown (Cable)"),
                   _ex("Bench Press (Barbell)"), _ex("Bicep Curl (Dumbbell)"),
                   _ex("Rear Delt Fly"), _ex("Dead Bug")]
        violations = [v for v in lr.check(session) if v["rule"] == "consecutive_same_group"]
        self.assertTrue(violations)
        self.assertIn("both pull", violations[0]["detail"])

    def test_arrange_separates_same_group_work(self) -> None:
        session = [_ex("Dumbbell Row"), _ex("Lat Pulldown (Cable)"), _ex("T Bar Row"),
                   _ex("Bench Press (Barbell)"), _ex("Chest Fly (Cable)"), _ex("Dead Bug")]
        ordered = lr.arrange(session)
        groups = [lr.group_of(e) for e in ordered]
        adjacent = [(a, b) for a, b in zip(groups, groups[1:])
                    if a == b and a not in {"other", "core"}]
        self.assertEqual(adjacent, [], groups)

    def test_arrange_never_drops_or_invents_an_exercise(self) -> None:
        # Silently dropping work is how the coach produced a 3-exercise session.
        ordered = lr.arrange(BAD_SESSION)
        self.assertEqual(sorted(e["title"] for e in ordered),
                         sorted(e["title"] for e in BAD_SESSION))

    def test_arrange_is_stable_for_an_already_legal_session(self) -> None:
        ordered = lr.arrange(GOOD_SESSION)
        self.assertEqual(len(ordered), len(GOOD_SESSION))
        self.assertEqual(lr.check(ordered), [])

    def test_an_impossible_order_still_returns_every_exercise(self) -> None:
        # Six presses cannot be alternated; the session must still come back whole
        # and check() must still report the problem.
        session = [_ex(f"Bench Press {i}") for i in range(6)]
        ordered = lr.arrange(session)
        self.assertEqual(len(ordered), 6)
        self.assertTrue(lr.check(ordered))


class SummaryTests(unittest.TestCase):
    def test_the_summary_separates_injury_from_programming_problems(self) -> None:
        summary = lr.summary(BAD_SESSION)
        self.assertFalse(summary["ok"])
        injury_rules = {v["rule"] for v in summary["injury_violations"]}
        self.assertIn("no_triceps_with_press", injury_rules)
        self.assertIn("banned_movement", injury_rules)
        self.assertNotIn("back_volume", injury_rules)

    def test_a_clean_session_reports_ok(self) -> None:
        summary = lr.summary(GOOD_SESSION)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["press_count"], 1)
        self.assertEqual(summary["exercise_count"], 6)

    def test_hostile_input_does_not_raise(self) -> None:
        for bad in (None, 42, True, "session", {}, [None, 7, "x"], object()):
            self.assertIsInstance(lr.check(bad), list)
            self.assertIsInstance(lr.arrange(bad), list)
            self.assertIn("ok", lr.summary(bad))


if __name__ == "__main__":
    unittest.main()
