from __future__ import annotations

import datetime
import unittest

from app import strength_effort as se


TODAY = datetime.date(2026, 8, 20)
TOMORROW = datetime.date(2026, 8, 21)


def _day(date: datetime.date, discipline: str, **kw) -> dict:
    return {"date": date.isoformat(), "discipline": discipline, **kw}


def _readiness(score: int | None) -> dict:
    return {"training_readiness": {"score": score}}


class KeyRunTests(unittest.TestCase):
    def test_a_quality_run_is_a_key_session(self) -> None:
        for intensity in ("threshold", "race pace", "VO2", "tempo", "intervals"):
            self.assertTrue(se.is_key_run(_day(TODAY, "run", intensity=intensity)), intensity)

    def test_a_long_easy_run_is_a_key_session_on_duration_alone(self) -> None:
        self.assertTrue(se.is_key_run(_day(TODAY, "run", intensity="Z2", duration_min=120)))

    def test_a_short_easy_run_is_not_a_key_session(self) -> None:
        self.assertFalse(se.is_key_run(_day(TODAY, "run", intensity="Z2", duration_min=40)))

    def test_race_day_is_always_a_key_session(self) -> None:
        self.assertTrue(se.is_key_run(_day(TODAY, "race")))

    def test_a_swim_or_rest_day_is_never_a_key_run(self) -> None:
        self.assertFalse(se.is_key_run(_day(TODAY, "swim", duration_min=180)))
        self.assertFalse(se.is_key_run(_day(TODAY, "rest")))
        self.assertFalse(se.is_key_run(None))


class RunLoadCeilingTests(unittest.TestCase):
    def _decide(self, days, **kw):
        return se.decide(today=TODAY, plan_days=days, **kw)

    def test_a_clear_day_is_the_slot_to_lift_hard(self) -> None:
        result = self._decide([_day(TODAY, "swim", duration_min=45)])
        self.assertEqual(result["level"], "heavy")
        self.assertEqual(result["reps_in_reserve"], "0-1")
        self.assertIn("Go hard", result["cue"])

    def test_a_key_run_tomorrow_keeps_today_light(self) -> None:
        result = self._decide([_day(TOMORROW, "run", intensity="threshold", duration_min=60)])
        self.assertEqual(result["level"], "light")
        self.assertEqual(result["reps_in_reserve"], "3-4")

    def test_a_key_run_today_allows_only_a_submaximal_lift(self) -> None:
        result = self._decide([_day(TODAY, "run", intensity="race pace", duration_min=50)])
        self.assertEqual(result["level"], "moderate")

    def test_key_runs_on_both_days_mean_no_lift(self) -> None:
        result = self._decide([
            _day(TODAY, "run", intensity="threshold", duration_min=60),
            _day(TOMORROW, "run", intensity="Z2", duration_min=120),
        ])
        self.assertEqual(result["level"], "skip")
        self.assertIsNone(result["reps_in_reserve"])
        self.assertIn("Skip the lift", result["cue"])

    def test_the_run_derived_ceiling_is_reported_separately(self) -> None:
        result = self._decide([_day(TOMORROW, "run", intensity="threshold")],
                              readiness=_readiness(20))
        self.assertEqual(result["ceiling_from_run_load"], "light")


class ReadinessTests(unittest.TestCase):
    CLEAR = [_day(TODAY, "swim", duration_min=45)]

    def _decide(self, readiness, days=None, **kw):
        return se.decide(today=TODAY, plan_days=days or self.CLEAR,
                         readiness=readiness, **kw)

    def test_good_readiness_leaves_the_ceiling_alone(self) -> None:
        self.assertEqual(self._decide(_readiness(80))["level"], "heavy")

    def test_fair_readiness_lowers_the_ceiling_one_step(self) -> None:
        self.assertEqual(self._decide(_readiness(45))["level"], "moderate")

    def test_poor_readiness_lowers_the_ceiling_two_steps(self) -> None:
        self.assertEqual(self._decide(_readiness(20))["level"], "light")

    def test_readiness_can_never_raise_the_ceiling_the_running_set(self) -> None:
        # Excellent readiness the day before a key run still does not permit heavy.
        result = self._decide(_readiness(95),
                              days=[_day(TOMORROW, "run", intensity="threshold")])
        self.assertEqual(result["level"], "light")

    def test_a_missing_readiness_score_neither_helps_nor_hurts(self) -> None:
        self.assertEqual(self._decide(None)["level"], "heavy")
        self.assertEqual(self._decide({})["level"], "heavy")
        self.assertEqual(self._decide(_readiness(None))["level"], "heavy")

    def test_intraday_readiness_is_ignored_in_favour_of_the_morning_score(self) -> None:
        result = self._decide({"training_readiness": {"score": 80},
                               "current_readiness": {"score": 11}})
        self.assertEqual(result["level"], "heavy")
        self.assertEqual(result["readiness_score"], 80)


class CalibrationTests(unittest.TestCase):
    CLEAR = [_day(TODAY, "swim", duration_min=45)]

    def test_unknown_working_weights_prevent_a_heavy_day(self) -> None:
        result = se.decide(today=TODAY, plan_days=self.CLEAR,
                           readiness=_readiness(90),
                           strength={"calibration_required": True})
        self.assertEqual(result["level"], "moderate")
        self.assertIn("Calibration day", result["cue"])
        self.assertIn("log it", result["cue"])

    def test_calibration_never_makes_an_easy_day_harder(self) -> None:
        result = se.decide(today=TODAY,
                           plan_days=[_day(TOMORROW, "run", intensity="threshold")],
                           strength={"calibration_required": True})
        self.assertEqual(result["level"], "light")

    def test_known_weights_allow_a_heavy_day(self) -> None:
        result = se.decide(today=TODAY, plan_days=self.CLEAR,
                           readiness=_readiness(90),
                           strength={"calibration_required": False})
        self.assertEqual(result["level"], "heavy")


class ExplanationTests(unittest.TestCase):
    def test_every_decision_states_its_reasoning(self) -> None:
        result = se.decide(
            today=TODAY,
            plan_days=[_day(TOMORROW, "run", intensity="threshold", duration_min=60)],
            readiness=_readiness(30), strength={"calibration_required": True},
        )
        joined = " ".join(result["reasons"])
        self.assertIn("key run is scheduled tomorrow", joined)
        self.assertIn("readiness 30 is poor", joined)
        self.assertGreaterEqual(len(result["reasons"]), 2)

    def test_malformed_plan_days_do_not_raise(self) -> None:
        for bad in (None, [], ["x", None, 42], [{"date": "not-a-date"}]):
            self.assertIn(se.decide(today=TODAY, plan_days=bad)["level"], se.LEVELS)


if __name__ == "__main__":
    unittest.main()
