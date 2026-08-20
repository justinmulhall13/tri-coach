from __future__ import annotations

import datetime
import unittest

from app import strength_context as sc


TODAY = datetime.date(2026, 8, 20)


def _garmin(dates: list[str]) -> dict:
    return {
        "activities": [
            {"date": d, "sport": "strength", "name": "Weight Training",
             "minutes": 50.0, "hr_avg": 99, "source": "measured"}
            for d in dates
        ] + [
            {"date": "2026-08-16", "sport": "run", "name": "Morning Run", "minutes": 92.0},
            {"date": "2026-08-14", "sport": "mobility", "name": "Evening Stretch", "minutes": 15.0},
        ]
    }


def _hevy(connected: bool = True, workouts: list | str | None = None) -> dict:
    return {
        "connection": {"connected": connected},
        "recent_workouts": workouts if workouts is not None else [],
    }


# One real logged session: 315 lb x5 barbell squat and 50 lb x8 incline press.
REAL_WORKOUT = {
    "start_time": "2026-06-16T14:45:00Z",
    "title": "Lower 1",
    "exercises": [
        {"exercise_template_id": "D04AC939", "title": "Squat (Barbell)", "sets": [
            {"weight_kg": 61.235042773811365, "reps": 5},
            {"weight_kg": 142.881766472226530, "reps": 5},
        ]},
        {"exercise_template_id": "07B38369", "title": "Incline Bench Press (Dumbbell)", "sets": [
            {"weight_kg": 22.679645471781990, "reps": 8},
            {"weight_kg": 34.019468207672980, "reps": 10},
        ]},
    ],
}


class TheReportedBugTests(unittest.TestCase):
    """Garmin shows six sessions after the last Hevy log (the real scenario)."""

    def setUp(self) -> None:
        self.context = sc.build(
            garmin_load=_garmin([
                "2026-07-31", "2026-07-24", "2026-07-20",
                "2026-07-12", "2026-06-27", "2026-06-22",
            ]),
            hevy_context=_hevy(workouts=[REAL_WORKOUT]),
            today=TODAY,
        )

    def test_recent_lifting_is_reported_from_garmin_not_denied_from_hevy(self) -> None:
        self.assertEqual(self.context["session_evidence"]["last_session_date"], "2026-07-31")
        self.assertEqual(self.context["session_evidence"]["days_since"], 20)
        self.assertNotIn("No strength session found", self.context["summary"])

    def test_sessions_after_the_last_hevy_log_are_flagged_as_untracked(self) -> None:
        dates = [s["date"] for s in self.context["untracked_sessions"]]
        self.assertEqual(dates, ["2026-07-31", "2026-07-24", "2026-07-20",
                                 "2026-07-12", "2026-06-27", "2026-06-22"])

    def test_unknown_loads_force_a_calibration_week_instead_of_stale_weights(self) -> None:
        self.assertTrue(self.context["calibration_required"])
        self.assertTrue(self.context["weight_evidence"]["anchors_stale"])
        self.assertEqual(self.context["weight_evidence"]["anchor_age_days"], 65)

    def test_the_summary_names_both_sources_and_their_disagreement(self) -> None:
        summary = self.context["summary"]
        self.assertIn("2026-07-31", summary)
        self.assertIn("2026-06-16", summary)
        self.assertIn("weights used are unknown", summary)

    def test_status_reflects_the_reduced_july_frequency(self) -> None:
        # Only 2026-07-31 and 2026-07-24 fall inside the 28-day window.
        self.assertEqual(self.context["sessions_per_week_28d"], 0.5)
        self.assertEqual(self.context["training_status"], "partially_detrained")


class AnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchors = sc.build(
            garmin_load=_garmin([]), hevy_context=_hevy(workouts=[REAL_WORKOUT]),
            today=TODAY,
        )["weight_evidence"]["anchors"]

    def test_anchors_are_reported_in_pounds_not_raw_kilograms(self) -> None:
        squat = next(a for a in self.anchors if a["exercise_template_id"] == "D04AC939")
        self.assertEqual(squat["weight_lb"], 315.0)
        self.assertEqual(squat["reps"], 5)
        self.assertEqual(squat["date"], "2026-06-16")

    def test_the_best_set_wins_rather_than_the_last_set(self) -> None:
        squat = next(a for a in self.anchors if a["exercise_template_id"] == "D04AC939")
        self.assertEqual(squat["weight_lb"], 315.0)  # not the 135 lb warmup

    def test_increment_is_inferred_per_exercise_from_its_own_history(self) -> None:
        press = next(a for a in self.anchors if a["exercise_template_id"] == "07B38369")
        self.assertEqual(press["increment_lb"], 5.0)  # 50 and 75 lb

    def test_anchors_are_ordered_by_estimated_strength(self) -> None:
        self.assertEqual([a["exercise_template_id"] for a in self.anchors],
                         ["D04AC939", "07B38369"])

    def test_bodyweight_and_malformed_sets_are_skipped_not_counted_as_zero(self) -> None:
        anchors = sc.build(
            garmin_load=_garmin([]),
            hevy_context=_hevy(workouts=[{
                "start_time": "2026-06-16T00:00:00Z",
                "exercises": [{"exercise_template_id": "108D7A14", "sets": [
                    {"weight_kg": None, "reps": 5},
                    {"weight_kg": 0, "reps": 5},
                    {"weight_kg": True, "reps": 5},
                    {"weight_kg": 50.0, "reps": None},
                ]}],
            }]),
            today=TODAY,
        )["weight_evidence"]["anchors"]
        self.assertEqual(anchors, [])


class AbsenceOfEvidenceTests(unittest.TestCase):
    def test_a_disconnected_hevy_never_reads_as_zero_lifting(self) -> None:
        context = sc.build(
            garmin_load=_garmin(["2026-08-18"]),
            hevy_context={"connection": {"connected": False}, "recent_workouts": "unknown"},
            today=TODAY,
        )
        self.assertEqual(context["session_evidence"]["last_session_date"], "2026-08-18")
        self.assertTrue(context["calibration_required"])
        self.assertFalse(context["weight_evidence"]["connected"])

    def test_a_hevy_fetch_error_is_not_mistaken_for_an_empty_history(self) -> None:
        context = sc.build(
            garmin_load=_garmin(["2026-08-18"]),
            hevy_context={"connection": {"connected": True},
                          "recent_workouts": {"error": "HTTP 503", "value": "unknown"}},
            today=TODAY,
        )
        self.assertEqual(context["weight_evidence"]["anchors"], [])
        self.assertTrue(context["calibration_required"])

    def test_no_evidence_anywhere_is_stated_as_absence_of_evidence(self) -> None:
        context = sc.build(garmin_load=_garmin([]), hevy_context=_hevy(), today=TODAY)
        self.assertEqual(context["training_status"], "unknown")
        self.assertIn("absence of evidence", context["summary"])

    def test_missing_sources_do_not_raise(self) -> None:
        context = sc.build(garmin_load=None, hevy_context=None, today=TODAY)
        self.assertEqual(context["training_status"], "unknown")
        self.assertEqual(context["weight_evidence"]["anchors"], [])

    def test_mobility_work_is_not_counted_as_a_strength_session(self) -> None:
        context = sc.build(garmin_load=_garmin([]), hevy_context=_hevy(), today=TODAY)
        self.assertEqual(context["session_evidence"]["recent_sessions"], [])


class StatusTests(unittest.TestCase):
    def test_consistent_recent_lifting_reads_as_maintained(self) -> None:
        context = sc.build(
            garmin_load=_garmin(["2026-08-19", "2026-08-17", "2026-08-14", "2026-08-12",
                                 "2026-08-09", "2026-08-07", "2026-08-04", "2026-08-01"]),
            hevy_context=_hevy(workouts=[REAL_WORKOUT]), today=TODAY,
        )
        self.assertEqual(context["training_status"], "maintained")
        self.assertGreaterEqual(context["sessions_per_week_28d"], sc.MAINTENANCE_SESSIONS_PER_WEEK)

    def test_a_long_gap_reads_as_detrained(self) -> None:
        context = sc.build(
            garmin_load=_garmin(["2026-05-01"]),
            hevy_context=_hevy(workouts=[REAL_WORKOUT]), today=TODAY,
        )
        self.assertEqual(context["training_status"], "detrained")
        self.assertEqual(context["sessions_per_week_28d"], 0.0)


if __name__ == "__main__":
    unittest.main()
