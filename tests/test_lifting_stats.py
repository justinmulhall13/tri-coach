from __future__ import annotations

import datetime
import unittest

from app import lifting_stats as ls
from app import strength_weights as sw


TODAY = datetime.date(2026, 8, 20)


def _set(lb: float, reps: int) -> dict:
    return {"weight_kg": sw.lb_to_kg(lb), "reps": reps}


def _workout(date: str, title: str, exercises: list) -> dict:
    return {"id": f"w-{date}", "start_time": f"{date}T14:00:00Z", "title": title,
            "exercises": [{"exercise_template_id": t.upper()[:8], "title": t,
                           "sets": sets} for t, sets in exercises]}


LOG = [
    _workout("2026-08-18", "Upper", [
        ("Squat (Barbell)", [_set(315, 5), _set(315, 5)]),
        ("Dumbbell Row", [_set(100, 10)]),
    ]),
    _workout("2026-08-11", "Upper", [
        ("Squat (Barbell)", [_set(295, 5), _set(295, 5)]),
        ("Dumbbell Row", [_set(95, 10)]),
        ("Bench Press (Barbell)", [_set(185, 5)]),
    ]),
    _workout("2026-05-01", "Old", [
        ("Squat (Barbell)", [_set(255, 5)]),
        ("Dumbbell Row", [_set(80, 10)]),
        ("Zercher Squat", [_set(185, 8)]),
    ]),
]


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = ls.exercise_records(LOG)
        self.by_name = {r["exercise"]: r for r in self.records}

    def test_the_best_set_becomes_the_record(self) -> None:
        squat = self.by_name["Squat (Barbell)"]
        self.assertEqual(squat["best_weight_lb"], 315.0)
        self.assertEqual(squat["pr_date"], "2026-08-18")

    def test_an_estimated_max_is_labelled_as_an_estimate(self) -> None:
        self.assertTrue(self.by_name["Squat (Barbell)"]["e1rm_is_estimate"])

    def test_gain_is_measured_from_the_earliest_logged_set(self) -> None:
        squat = self.by_name["Squat (Barbell)"]
        self.assertEqual(squat["first_date"], "2026-05-01")
        self.assertGreater(squat["change_lb"], 0)

    def test_records_are_reported_in_pounds(self) -> None:
        self.assertNotIn("142.88", str(self.records))

    def test_high_rep_sets_do_not_become_strength_estimates(self) -> None:
        log = [_workout("2026-08-18", "X", [("Machine Row", [_set(100, 30)])])]
        self.assertEqual(ls.exercise_records(log), [])

    def test_bodyweight_and_malformed_sets_are_skipped(self) -> None:
        log = [_workout("2026-08-18", "X", [("Plank", [
            {"weight_kg": None, "reps": 5}, {"weight_kg": 50, "reps": None},
            {"weight_kg": True, "reps": 5}, {"weight_kg": -20, "reps": 5}])])]
        self.assertEqual(ls.exercise_records(log), [])


class GainTests(unittest.TestCase):
    def test_a_single_session_fluke_is_not_a_gain(self) -> None:
        records = ls.exercise_records(LOG)
        names = {g["exercise"] for g in ls.biggest_gains(records)}
        # Zercher Squat appears in one session; it must not top the leaderboard.
        self.assertNotIn("Zercher Squat", names)

    def test_a_genuinely_improved_lift_is_reported(self) -> None:
        records = ls.exercise_records(LOG)
        names = {g["exercise"] for g in ls.biggest_gains(records)}
        self.assertIn("Squat (Barbell)", names)

    def test_a_warmup_within_one_session_is_never_reported_as_progress(self) -> None:
        # Real data produced a "+443% gain" on bench press where both ends were
        # the same afternoon: the first set was the empty bar.
        log = [_workout("2025-12-12", "Push", [
            ("Bench Press (Barbell)", [_set(45, 10), _set(135, 5), _set(185, 5)])])]
        record = ls.exercise_records(log)[0]
        self.assertEqual(record["change_lb"], 0.0)
        self.assertEqual(record["sessions_logged"], 1)
        self.assertEqual(ls.biggest_gains(ls.exercise_records(log)), [])

    def test_progress_is_measured_between_sessions_not_within_one(self) -> None:
        log = [
            _workout("2026-01-05", "A", [("Bench Press (Barbell)",
                                          [_set(45, 10), _set(155, 5)])]),
            _workout("2026-02-05", "B", [("Bench Press (Barbell)",
                                          [_set(45, 10), _set(175, 5)])]),
            _workout("2026-03-05", "C", [("Bench Press (Barbell)",
                                          [_set(45, 10), _set(185, 5)])]),
        ]
        record = ls.exercise_records(log)[0]
        # 155 -> 185 between sessions, not 45 -> 185 inside one.
        self.assertEqual(record["first_e1rm_lb"], round(sw.epley_e1rm_lb(155, 5), 1))
        self.assertEqual(record["e1rm_lb"], round(sw.epley_e1rm_lb(185, 5), 1))
        self.assertIn("Bench Press (Barbell)",
                      {g["exercise"] for g in ls.biggest_gains(ls.exercise_records(log))})

    def test_gains_are_ordered_by_percentage(self) -> None:
        gains = ls.biggest_gains(ls.exercise_records(LOG))
        self.assertEqual(gains, sorted(gains, key=lambda g: -g["change_pct"]))


class BalanceTests(unittest.TestCase):
    def test_a_push_heavy_block_is_flagged_for_the_shoulder(self) -> None:
        log = [_workout("2026-08-18", "Push", [
            ("Bench Press (Barbell)", [_set(185, 5)] * 4),
            ("Overhead Press (Dumbbell)", [_set(60, 8)] * 3),
            ("Dumbbell Row", [_set(100, 10)]),
        ])]
        balance = ls.push_pull_balance(log, today=TODAY)
        self.assertLess(balance["pull_per_push"], 1.0)
        self.assertIn("push-dominant", balance["verdict"])

    def test_a_pull_heavy_block_reads_as_the_safe_side(self) -> None:
        log = [_workout("2026-08-18", "Pull", [
            ("Dumbbell Row", [_set(100, 10)] * 4),
            ("Lat Pulldown (Cable)", [_set(150, 10)] * 3),
            ("Bench Press (Barbell)", [_set(185, 5)]),
        ])]
        self.assertIn("pull-dominant", ls.push_pull_balance(log, today=TODAY)["verdict"])

    def test_no_pressing_volume_is_stated_rather_than_divided_by_zero(self) -> None:
        log = [_workout("2026-08-18", "Pull", [("Dumbbell Row", [_set(100, 10)])])]
        balance = ls.push_pull_balance(log, today=TODAY)
        self.assertIsNone(balance["pull_per_push"])
        self.assertIn("no pressing volume", balance["verdict"])


class ConsistencyTests(unittest.TestCase):
    def test_sessions_are_counted_per_week(self) -> None:
        result = ls.consistency(LOG, today=TODAY)
        self.assertEqual(result["last_session"], "2026-08-18")
        self.assertGreaterEqual(result["active_weeks"], 2)

    def test_an_empty_log_does_not_raise(self) -> None:
        result = ls.consistency([], today=TODAY)
        self.assertEqual(result["sessions"], 0)
        self.assertIsNone(result["last_session"])


class StaleTests(unittest.TestCase):
    def test_a_lift_untouched_for_months_is_surfaced(self) -> None:
        stale = {s["exercise"] for s in ls.stale_exercises(
            ls.exercise_records(LOG), today=TODAY)}
        self.assertIn("Zercher Squat", stale)

    def test_a_recently_trained_lift_is_not_stale(self) -> None:
        stale = {s["exercise"] for s in ls.stale_exercises(
            ls.exercise_records(LOG), today=TODAY)}
        self.assertNotIn("Squat (Barbell)", stale)


class SessionAndTrendTests(unittest.TestCase):
    def test_recent_sessions_report_tonnage_and_rule_status(self) -> None:
        sessions = ls.recent_sessions(LOG)
        self.assertEqual(sessions[0]["date"], "2026-08-18")
        self.assertGreater(sessions[0]["tonnage_lb"], 0)
        self.assertIn("ok", sessions[0]["rule_status"])

    def test_a_logged_session_breaking_the_shoulder_rule_is_visible(self) -> None:
        log = [_workout("2026-08-18", "Bad", [
            ("Bench Press (Barbell)", [_set(185, 5)]),
            ("Overhead Press (Dumbbell)", [_set(60, 8)]),
        ])]
        status = ls.recent_sessions(log)[0]["rule_status"]
        self.assertFalse(status["ok"])
        self.assertTrue(status["injury_violations"])

    def test_tonnage_trend_is_bucketed_by_week_oldest_first(self) -> None:
        trend = ls.tonnage_trend(LOG, today=TODAY)
        self.assertEqual([t["week_of"] for t in trend],
                         sorted(t["week_of"] for t in trend))


class BuildTests(unittest.TestCase):
    def test_the_payload_carries_every_section(self) -> None:
        payload = ls.build(LOG, today=TODAY)
        for key in ("recent_sessions", "records", "biggest_gains",
                    "push_pull_balance", "consistency", "stale_exercises",
                    "tonnage_trend"):
            self.assertIn(key, payload)
        self.assertTrue(payload["has_data"])

    def test_an_empty_log_reports_no_data_rather_than_failing(self) -> None:
        payload = ls.build([], today=TODAY)
        self.assertFalse(payload["has_data"])
        self.assertEqual(payload["records"], [])

    def test_hostile_input_does_not_raise(self) -> None:
        for bad in (None, 42, "log", {}, [None, 7, "x"], [{"start_time": "nope"}]):
            payload = ls.build(bad, today=TODAY)
            self.assertIn("has_data", payload)


if __name__ == "__main__":
    unittest.main()
