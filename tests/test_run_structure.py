"""A run is not split into same-pace blocks, and recoveries are jogs, not walks."""
from __future__ import annotations

import json
import unittest

from app import coach, db, garmin_workout, run_structure

EASY = "easy, no faster than 5:17/km"


class TidyTests(unittest.TestCase):
    def test_a_same_pace_warmup_joins_the_main_block_and_the_walk_becomes_a_jog(self) -> None:
        out = run_structure.tidy({
            "warmup": f"10 min {EASY}",
            "main": f"25 min {EASY}, then 4x20s relaxed strides w/ 60s walk",
            "cooldown": f"5 min {EASY}",
        }, "run")
        self.assertEqual(out["warmup"], "—")
        self.assertEqual(out["main"], f"35 min {EASY}, then 4x20s relaxed strides w/ 60s easy jog")
        # The jog after the strides is a different block from the strides, so it stays.
        self.assertEqual(out["cooldown"], f"5 min {EASY}")

    def test_an_easy_run_in_three_same_pace_parts_is_one_block(self) -> None:
        out = run_structure.tidy(
            {"warmup": "10 min easy", "main": f"25 min {EASY}", "cooldown": "5 min easy"}, "run")
        self.assertEqual(out, {"warmup": "—", "main": f"40 min {EASY}", "cooldown": "—"})

    def test_a_real_warmup_is_left_alone(self) -> None:
        for structure in (
            {"warmup": "10 min easy", "main": "20 min @ 4:41/km", "cooldown": "10 min easy"},
            {"warmup": "10 min easy build", "main": "30 min easy", "cooldown": "—"},
            {"warmup": "15 min easy + 4×20 s strides w/ full recovery",
             "main": "15 min easy; 3×1 km @ 4:10/km w/ 2 min jog", "cooldown": "10 min easy"},
            {"warmup": "10 min easy, no faster than 5:30/km", "main": f"25 min {EASY}", "cooldown": "—"},
        ):
            self.assertEqual(run_structure.tidy(structure, "run"), structure)

    def test_other_sports_and_odd_input_are_untouched(self) -> None:
        bike = {"warmup": "10 min easy", "main": "25 min easy", "cooldown": "5 min easy"}
        self.assertIs(run_structure.tidy(bike, "bike"), bike)
        self.assertIsNone(run_structure.tidy(None, "run"))
        self.assertEqual(run_structure.tidy({"main": "—"}, "run"), {"main": "—"})

    def test_walk_recoveries_are_jogs(self) -> None:
        for before, after in (
            ("6×20 s strides w/ 60s walk", "6×20 s strides w/ 60s easy jog"),
            ("8×200 m w/ walk back", "8×200 m w/ easy jog back"),
            ("6×30 s hills w/ walk or jog back", "6×30 s hills w/ easy jog back"),
            ("5×1 km w/ 2 min jog/walk", "5×1 km w/ 2 min easy jog"),
            ("3×1 km @ 4:10/km w/ 2 min jog", "3×1 km @ 4:10/km w/ 2 min jog"),
        ):
            self.assertEqual(run_structure.jog_recoveries(before), after)


class WiringTests(unittest.TestCase):
    def test_plan_rows_are_tidied_as_they_are_read(self) -> None:
        raw = {"warmup": f"10 min {EASY}", "main": f"25 min {EASY}, then 4x20s strides w/ 60s walk",
               "cooldown": f"5 min {EASY}"}
        day = db._row_to_day({"discipline": "run", "structure": json.dumps(raw), "is_rest": 0})
        self.assertEqual(day["structure"]["warmup"], "—")
        self.assertNotIn("walk", day["structure"]["main"])
        second = db._row_to_secondary({"discipline": "run", "structure": json.dumps(raw)})
        self.assertTrue(second["structure"]["main"].startswith("35 min easy"))
        swim = db._row_to_day({"discipline": "swim", "structure": json.dumps(raw), "is_rest": 0})
        self.assertEqual(swim["structure"], raw)

    def test_the_watch_gets_one_easy_block_then_strides_with_jog_recoveries(self) -> None:
        raw = {"warmup": f"10 min {EASY}", "main": f"25 min {EASY}, then 4x20s relaxed strides w/ 60s walk",
               "cooldown": f"5 min {EASY}"}
        day = db._row_to_day({"discipline": "run", "title": "Easy run + strides", "intensity": "easy",
                              "duration_min": 45, "structure": json.dumps(raw), "is_rest": 0})
        workout = garmin_workout.build_workout(day, "2026-11-11")
        steps = workout["workoutSegments"][0]["workoutSteps"]
        kinds = [(s.get("stepType") or {}).get("stepTypeKey") for s in steps]
        self.assertEqual(kinds, ["interval", "repeat", "cooldown"])
        self.assertEqual(steps[0]["endConditionValue"], 35 * 60)
        self.assertEqual(steps[0]["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertEqual(steps[1]["numberOfIterations"], 4)
        stride, recovery = steps[1]["workoutSteps"]
        self.assertEqual((stride["stepType"]["stepTypeKey"], stride["endConditionValue"]), ("interval", 20))
        self.assertEqual((recovery["stepType"]["stepTypeKey"], recovery["endConditionValue"]), ("recovery", 60))
        self.assertEqual(steps[2]["endConditionValue"], 5 * 60)
        # Same total as the session that was written: 45 min, nothing lost in the join.
        total = steps[0]["endConditionValue"] + 4 * 80 + steps[2]["endConditionValue"]
        self.assertEqual(total, 600 + 1500 + 4 * 80 + 300)
        self.assertNotIn("walk", json.dumps(workout).lower())

    def test_strides_with_no_stated_recovery_jog_back(self) -> None:
        parsed = garmin_workout._parse("4×20 s strides")
        self.assertEqual(parsed["rec"][2], "easy jog back")

    def test_the_coach_is_told_both_rules(self) -> None:
        self.assertIn("RUN STRUCTURE:", coach._SYSTEM)
        self.assertIn("never a walk", coach._SYSTEM)
        self.assertIn("at the same pace", coach._SYSTEM)


if __name__ == "__main__":
    unittest.main()
