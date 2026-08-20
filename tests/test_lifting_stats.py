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


class SessionDetailTests(unittest.TestCase):
    """The tab reflects Hevy, so a session carries the sets actually performed."""

    def setUp(self) -> None:
        self.records = ls.exercise_records(LOG)
        self.sessions = ls.recent_sessions(LOG, records=self.records)

    def test_each_exercise_carries_its_real_sets(self) -> None:
        squat = next(e for e in self.sessions[0]["exercises"]
                     if e["title"] == "Squat (Barbell)")
        self.assertEqual([s["weight_lb"] for s in squat["sets"]], [315.0, 315.0])
        self.assertEqual([s["reps"] for s in squat["sets"]], [5, 5])

    def test_weights_are_in_pounds_not_stored_kilograms(self) -> None:
        self.assertNotIn("142.88", str(self.sessions))

    def test_the_top_set_of_each_exercise_is_identified(self) -> None:
        squat = next(e for e in self.sessions[0]["exercises"]
                     if e["title"] == "Squat (Barbell)")
        self.assertEqual(squat["top_set"], {"weight_lb": 315.0, "reps": 5})

    def test_a_set_matching_the_all_time_best_is_marked_a_pr(self) -> None:
        squat = next(e for e in self.sessions[0]["exercises"]
                     if e["title"] == "Squat (Barbell)")
        self.assertTrue(any(s["is_pr"] for s in squat["sets"]))
        self.assertGreaterEqual(self.sessions[0]["pr_count"], 1)

    def test_an_older_lighter_session_has_no_pr(self) -> None:
        old = next(s for s in self.sessions if s["date"] == "2026-05-01")
        self.assertEqual(old["pr_count"], 0)

    def test_a_first_ever_exercise_is_not_badged_as_a_pr(self) -> None:
        # Zercher Squat appears once. Technically it is an all-time best, but a
        # PR badge on every new movement is noise.
        log = [_workout("2026-08-18", "New", [("Zercher Squat", [_set(185, 8)])])]
        session = ls.recent_sessions(log, records=ls.exercise_records(log))[0]
        self.assertEqual(session["pr_count"], 0)

    def test_beating_a_previous_best_is_badged(self) -> None:
        log = [
            _workout("2026-07-01", "A", [("Squat (Barbell)", [_set(275, 5)])]),
            _workout("2026-08-01", "B", [("Squat (Barbell)", [_set(315, 5)])]),
        ]
        sessions = ls.recent_sessions(log, records=ls.exercise_records(log))
        newest = next(s for s in sessions if s["date"] == "2026-08-01")
        older = next(s for s in sessions if s["date"] == "2026-07-01")
        self.assertEqual(newest["pr_count"], 1)
        self.assertEqual(older["pr_count"], 0)

    def test_bodyweight_and_timed_sets_are_kept_not_discarded(self) -> None:
        log = [_workout("2026-08-18", "Core", [
            ("Plank", [{"duration_seconds": 45}, {"reps": 10}])])]
        plank = ls.recent_sessions(log)[0]["exercises"][0]
        self.assertEqual(len(plank["sets"]), 2)
        self.assertIsNone(plank["sets"][0]["weight_lb"])
        self.assertIsNone(plank["top_set"])

    def test_each_exercise_carries_a_drawable_pattern(self) -> None:
        for exercise in self.sessions[0]["exercises"]:
            self.assertIn(exercise["pattern"], __import__(
                "app.strength_visual", fromlist=["PATTERNS"]).PATTERNS)


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
