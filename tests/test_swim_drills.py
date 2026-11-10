"""Swim drills are structured 100s: 50 drill, 50 swim, one named drill each."""
from __future__ import annotations

import datetime
import unittest

from app import garmin_workout, half_iron_plan, swim_drills


def _end(step: dict) -> tuple[str, float]:
    return step["endCondition"]["conditionTypeKey"], float(step["endConditionValue"])


class DrillSetTests(unittest.TestCase):
    def test_full_set_names_all_four_drills(self) -> None:
        parts = swim_drills.drill_set().split("; ")
        self.assertEqual(len(parts), 4)
        for part, name in zip(parts, ("6-second switch", "fingertip drag", "catch-up",
                                      "3-point touch")):
            self.assertTrue(part.startswith(f"100m {name} (50 drill: "), part)
            self.assertIn(" / 50 swim: ", part)
            self.assertTrue(part.endswith(", 20s rest"), part)

    def test_short_sets_rotate(self) -> None:
        self.assertIn("6-second switch", swim_drills.drill_set(2, start=0))
        self.assertIn("fingertip drag", swim_drills.drill_set(2, start=0))
        self.assertIn("catch-up", swim_drills.drill_set(2, start=2))
        self.assertIn("3-point touch", swim_drills.drill_set(2, start=2))

    def test_watch_gets_one_100_per_drill_with_its_rest(self) -> None:
        steps, _ = garmin_workout._swim_steps("300 easy; " + swim_drills.drill_set(),
                                              kind="warmup")
        self.assertEqual([_end(s) for s in steps],
                         [("distance", 300.0)] + [("distance", 100.0), ("time", 20.0)] * 4)
        for step, (name, _, _) in zip(steps[1::2], swim_drills.DRILLS):
            self.assertIn(name, step["description"])
            # A cue must never read as a stroke or a piece of equipment.
            self.assertNotIn("equipmentType", step)
            self.assertNotIn(step.get("strokeType", {}).get("strokeTypeKey"),
                             ("backstroke", "breaststroke", "fly"))


class TidyTests(unittest.TestCase):
    def test_bare_drill_set_is_broken_up(self) -> None:
        out = swim_drills.tidy({
            "warmup": "300 easy + 6×50 drill (catch-up, fingertip drag), 15s rest",
            "main": "6×100 smooth, 20s rest", "cooldown": "100 easy"}, "swim")
        parts = out["warmup"].split("; ")
        self.assertEqual(parts[0], "300 easy")
        self.assertEqual(len(parts), 4)                    # 300 m of drill -> three 100s
        self.assertTrue(all(p.endswith(", 15s rest") for p in parts[1:]))
        self.assertEqual(out["main"], "6×100 smooth, 20s rest")

    def test_no_rest_is_invented(self) -> None:
        out = swim_drills.tidy({"main": "800 continuous smooth + 4×50 drill"}, "swim")
        self.assertNotIn("rest", out["main"])
        self.assertEqual(out["main"].count("100m "), 2)

    def test_idempotent_and_scoped(self) -> None:
        once = swim_drills.tidy({"warmup": "200 easy + 8x50 drills, 20s rest"}, "swim")
        self.assertEqual(swim_drills.tidy(once, "swim"), once)
        self.assertEqual(once["warmup"].count("100m "), 4)
        run = {"warmup": "12 min easy + drills", "main": "4×50 drill"}
        self.assertEqual(swim_drills.tidy(run, "run"), run)
        mixed = {"warmup": "400 easy + 8×50 drill/build"}
        self.assertEqual(swim_drills.tidy(mixed, "swim"), mixed)


class PlanTests(unittest.TestCase):
    def test_no_generated_swim_carries_a_bare_drill_set(self) -> None:
        race = datetime.date(2027, 5, 30)
        day = datetime.date(2026, 11, 2)
        seen = 0
        while day <= race:
            session, _ = half_iron_plan.workout_for_date(day, race)
            if session.get("discipline") == "swim":
                text = " ".join(str(v) for v in session["structure"].values())
                self.assertNotRegex(text, r"\d\s*[×x]\s*\d+\s*drill")
                seen += "50 drill: " in text
            day += datetime.timedelta(days=1)
        self.assertGreater(seen, 20)


if __name__ == "__main__":
    unittest.main()
