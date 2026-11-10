"""Garmin's daily calorie burn reaches the coach as measured data.

The burn is read from Garmin's daily summary and never estimated. Today is a
running total, so it stays out of the average. The calorie target is computed
in code from the measured average and the athlete's own goal.
"""
from __future__ import annotations

import datetime
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import coach, garmin_source, hevy_connector


class _FakeGarmin:
    def __init__(self, rows: dict[str, dict | None]) -> None:
        self.rows = rows
        self.asked: list[str] = []

    def get_user_summary(self, date: str) -> dict | None:
        self.asked.append(date)
        row = self.rows.get(date)
        if row == "boom":
            raise RuntimeError("Garmin is down")
        return row


def _row(total: float | None, active: float | None, resting: float | None) -> dict:
    return {"totalKilocalories": total, "activeKilocalories": active, "bmrKilocalories": resting}


class DailyEnergyTests(unittest.TestCase):
    def _read(self, rows: dict, days: int = 3) -> tuple[dict, _FakeGarmin]:
        fake = _FakeGarmin(rows)
        with (
            patch.object(garmin_source, "get_client", return_value=fake),
            patch.object(garmin_source.config, "local_today", return_value=datetime.date(2026, 11, 10)),
        ):
            return garmin_source.get_daily_energy(days), fake

    def test_finished_days_are_averaged_and_today_is_kept_apart(self) -> None:
        out, fake = self._read({
            "2026-11-10": _row(1331.0, 413.0, 918.0),
            "2026-11-09": _row(3330.0, 956.0, 2374.0),
            "2026-11-08": _row(3318.0, 944.0, 2374.0),
            "2026-11-07": _row(3000.0, 626.0, 2374.0),
        })
        self.assertEqual(sorted(fake.asked), ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"])
        self.assertEqual([d["date"] for d in out["days"]], ["2026-11-09", "2026-11-08", "2026-11-07"])
        self.assertEqual(out["days"][0], {"date": "2026-11-09", "total_kcal": 3330,
                                          "active_kcal": 956, "resting_kcal": 2374})
        self.assertEqual(out["average"], {"days": 3, "total_kcal": 3216,
                                          "active_kcal": 842, "resting_kcal": 2374})
        self.assertEqual(out["today"]["total_kcal"], 1331)
        self.assertIs(out["today"]["partial"], True)
        self.assertEqual(out["missing_dates"], [])
        self.assertEqual(out["source"], "Garmin daily summary")

    def test_missing_days_are_listed_and_never_filled_in(self) -> None:
        out, _ = self._read({
            "2026-11-10": None,
            "2026-11-09": _row(3330.0, 956.0, 2374.0),
            "2026-11-08": _row(None, None, None),
            "2026-11-07": "boom",
        })
        self.assertEqual([d["date"] for d in out["days"]], ["2026-11-09"])
        self.assertEqual(out["average"]["days"], 1)
        self.assertEqual(out["average"]["total_kcal"], 3330)
        self.assertIsNone(out["today"])
        self.assertEqual(out["missing_dates"], ["2026-11-08", "2026-11-07"])

    def test_no_data_means_no_average(self) -> None:
        out, _ = self._read({})
        self.assertEqual(out["days"], [])
        self.assertIsNone(out["average"])
        self.assertIsNone(out["today"])


class EnergyContextTests(unittest.TestCase):
    ENERGY = {"days": [], "today": None, "missing_dates": [],
              "average": {"days": 7, "total_kcal": 3216, "active_kcal": 842, "resting_kcal": 2374}}

    def test_target_is_the_measured_average_plus_the_athletes_goal(self) -> None:
        from app import nutrition
        for goal, want in (("maintain", 3220), ("deficit", 2670), ("surplus", 3570)):
            with patch.object(nutrition, "get_goal", return_value=goal):
                out = coach._energy_context(self.ENERGY)
            self.assertEqual(out["goal"], goal)
            self.assertEqual(out["goal_adjustment_kcal"], nutrition._GOAL_KCAL[goal])
            self.assertEqual(out["target_kcal"], want)
            self.assertEqual(out["average"]["total_kcal"], 3216)

    def test_no_average_means_no_target(self) -> None:
        out = coach._energy_context({"days": [], "today": None, "average": None})
        self.assertNotIn("target_kcal", out)
        self.assertIsNone(coach._energy_context(None))

    def test_the_live_context_asks_garmin_for_it(self) -> None:
        src = Path(coach.__file__).read_text()
        self.assertIn('"energy": garmin_source.get_daily_energy,', src)

    def test_the_prompt_tells_steve_to_answer_from_it(self) -> None:
        self.assertIn("daily_energy.target_kcal", coach._SYSTEM)
        self.assertIn("daily_energy.average.total_kcal", coach._SYSTEM)
        self.assertNotIn("never negotiable", coach._SYSTEM)

    def test_the_coach_context_carries_daily_energy(self) -> None:
        live = {
            "readiness": {}, "load": {"activities": [], "by_sport": {}},
            "training_load": {}, "fitness": {}, "pmc": {}, "zones": {},
            "weight": {"kg": 85.4, "as_of": "2026-08-15", "source": "self-reported", "provider": "Garmin"},
            "energy": self.ENERGY,
        }
        now = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.timezone.utc)
        previous = hevy_connector.connector()
        hevy_connector.reset()
        self.addCleanup(hevy_connector.configure, previous)
        from app import nutrition
        with (
            patch.object(coach, "_live_context", return_value=live),
            patch.object(coach.config, "local_now", return_value=now),
            patch.object(coach.rings, "t100_readiness", return_value={}),
            patch.object(coach.suggest, "todays_suggestion", return_value={}),
            patch("app.baselines.get_baselines", return_value={}),
            patch("app.insights.get_insights", return_value={}),
            patch.object(coach.db, "get_plan", return_value=[]),
            patch.object(coach.db, "get_constraints", return_value=[]),
            patch.object(coach.db, "get_constraint_history", return_value=[]),
            patch.object(coach.db, "get_plan_day", return_value=None),
            patch.object(coach, "_strength_load", return_value={"activities": []}),
            patch.object(nutrition, "get_goal", return_value="maintain"),
        ):
            payload = json.loads(coach._context_block("How many calories should I aim for"))
            without = dict(live)
            without.pop("energy")
            with patch.object(coach, "_live_context", return_value=without):
                bare = json.loads(coach._context_block("How many calories should I aim for"))
        self.assertEqual(payload["daily_energy"]["average"]["total_kcal"], 3216)
        self.assertEqual(payload["daily_energy"]["target_kcal"], 3220)
        self.assertIsNone(bare["daily_energy"])


if __name__ == "__main__":
    unittest.main()
