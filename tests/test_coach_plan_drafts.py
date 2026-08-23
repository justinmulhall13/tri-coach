from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import coach, coaching_contract, config, db, main

# These tests assert on fixed calendar dates. Pin "today" so the suite cannot
# rot once the real clock passes the hardcoded schedule dates.
_TODAY = datetime.date(2026, 8, 17)
_TODAY_ISO = _TODAY.isoformat()


def _message(schedule: list[dict]) -> SimpleNamespace:
    text = (
        "TL;DR: Fixed schedule ready.\n"
        "• Week: Dates are locked to safe sessions.\n"
        f"```weekplan\n{json.dumps(schedule)}\n```"
    )
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="test-model",
        stop_reason="end_turn",
    )


def _schedule() -> list[dict]:
    return [
        {
            "date": "2026-08-18", "title": "Easy swim", "discipline": "swim",
            "duration_min": 35, "intensity": "easy", "tsb_target": 10,
            "structure": {"warmup": "200 easy", "main": "1000 smooth", "cooldown": "100 easy"},
            "is_rest": 0, "why": "Fixed non-impact aerobic work.",
        },
        {
            "date": "2026-08-19", "title": "Rest", "discipline": "rest",
            "duration_min": 0, "intensity": "rest", "tsb_target": 10,
            "structure": {"warmup": "", "main": "Full rest", "cooldown": ""},
            "is_rest": 1, "why": "Absorb the prior load.",
        },
    ]


def _seed_day(day: dict) -> dict:
    return {
        **day,
        "week_index": 0,
        "phase": "post-race",
        "source": "seed",
    }


def _body(response) -> dict:
    return json.loads(response.body)


class CoachPlanDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._clock = patch.object(config, "local_today", return_value=_TODAY)
        self._clock.start()
        self.addCleanup(self._clock.stop)
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "drafts.db"
        db._local = threading.local()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def _new_connection(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._local = threading.local()

    def test_generated_week_is_fetchable_identically_in_a_new_session(self) -> None:
        schedule = _schedule()
        result = coach._finish_chat("Build next week", _TODAY_ISO, _message(schedule))
        self.assertIsInstance(result["plan_id"], int)
        self.assertEqual(result["proposed_week"], schedule)
        self.assertIn(f"plan #{result['plan_id']}", result["reply"])

        self._new_connection()
        latest = _body(main.coach_plan_draft_latest())["draft"]
        by_id = _body(main.coach_plan_draft_get(result["plan_id"]))["draft"]

        self.assertEqual(latest["plan_id"], result["plan_id"])
        self.assertEqual(latest["schedule"], schedule)
        self.assertEqual(by_id["schedule"], schedule)

    def test_drafts_are_isolated_by_active_event_profile(self) -> None:
        first = {
            "id": "first-draft-event", "mode": "RUNNING", "event": "First",
            "date": "2026-10-01", "disciplines_and_distances": {"run_km": 10},
            "goal": {"target": "finish"},
        }
        second = {
            "id": "second-draft-event", "mode": "RUNNING", "event": "Second",
            "date": "2026-11-01", "disciplines_and_distances": {"run_km": 21.1},
            "goal": {"target": "finish"},
        }
        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            created = db.create_plan_draft(_schedule(), source_message="first event")
            self.assertEqual(db.get_latest_plan_draft()["plan_id"], created["plan_id"])
        with patch.object(coaching_contract, "EVENT_PROFILE", second):
            self.assertIsNone(db.get_latest_plan_draft())
            self.assertIsNone(db.get_plan_draft(created["plan_id"]))

    def test_draft_write_failure_never_returns_or_logs_the_schedule(self) -> None:
        with (
            patch.object(coach.db, "create_plan_draft", side_effect=OSError("disk full")),
            patch.object(coach.db, "add_chat") as add_chat,
        ):
            with self.assertRaises(coach.PlanDraftPersistenceError):
                coach._finish_chat("Build next week", _TODAY_ISO, _message(_schedule()))
        add_chat.assert_not_called()

    def test_model_week_normalizes_current_day_tsb_to_the_race_day_default(self) -> None:
        schedule = _schedule()
        schedule[0] = {**schedule[0], "tsb_target": -5}
        schedule[1] = {k: v for k, v in schedule[1].items() if k != "tsb_target"}

        result = coach._finish_chat(
            "Plan my week", _TODAY_ISO, _message(schedule),
        )

        self.assertEqual([day["tsb_target"] for day in result["proposed_week"]], [10, 10])
        stored = db.get_plan_draft(result["plan_id"])
        self.assertEqual([day["tsb_target"] for day in stored["schedule"]], [10, 10])

    def test_strict_week_boundary_still_rejects_invalid_tsb_from_api_clients(self) -> None:
        schedule = _schedule()
        schedule[0] = {**schedule[0], "tsb_target": -5}
        with self.assertRaisesRegex(coach.WeekPlanValidationError, "race-day target"):
            coach.validate_weekplan(schedule)

    def test_policy_failure_does_not_leak_internal_exception_into_chat(self) -> None:
        event = coach._public_chat_failure(
            coach.WeekPlanValidationError(
                "week plan row 2026-08-23 tsb_target must be the race-day target +5 to +15"
            )
        )

        self.assertEqual(event["source"], "policy")
        self.assertNotIn("WeekPlanValidationError", event["error"])
        self.assertNotIn("tsb_target", event["error"])
        self.assertIn("Nothing changed", event["error"])

    def test_fixed_date_schedule_rejects_recovery_activation_language(self) -> None:
        bad = _schedule()
        bad[0] = {**bad[0], "why": "Run once feet normal."}
        with patch.object(coach.db, "create_plan_draft") as create:
            with self.assertRaises(coach.WeekPlanValidationError):
                coach._finish_chat("Build next week", _TODAY_ISO, _message(bad))
        create.assert_not_called()
        self.assertIn("once feet normal", coach._SYSTEM.lower())

    def test_safety_abort_language_remains_valid(self) -> None:
        safe = _schedule()
        safe[0] = {**safe[0], "structure": {
            **safe[0]["structure"], "main": "1000 smooth; stop if Achilles pain rises",
        }}
        coach.validate_weekplan(safe)

    def test_fixed_dates_strip_exact_conflicting_visible_gating_language(self) -> None:
        schedule = _schedule()
        message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=(
                "TL;DR: Calendar picked.\n"
                "• Run: Once feet feel normal, not on a fixed day.\n"
                "• Swim: Tuesday stays easy.\n"
                f"```weekplan\n{json.dumps(schedule)}\n```"
            ))],
            model="test-model",
            stop_reason="end_turn",
        )

        result = coach._finish_chat("Pick the calendar", _TODAY_ISO, message)

        self.assertEqual(result["proposed_week"], schedule)
        self.assertNotIn("once feet feel normal", result["reply"].lower())
        self.assertNotIn("not on a fixed day", result["reply"].lower())
        self.assertIn("Tuesday stays easy", result["reply"])

    def test_draft_can_be_edited_then_activated_by_id(self) -> None:
        schedule = _schedule()
        for day in schedule:
            db.upsert_plan_day(_seed_day(day))
        draft = db.create_plan_draft(schedule, source_message="test")
        edited = [{**schedule[0], "title": "Edited easy swim"}, schedule[1]]

        edit_response = main.coach_plan_draft_edit(draft["plan_id"], {"week": edited})
        self.assertEqual(edit_response.status_code, 200)
        self.assertEqual(_body(edit_response)["draft"]["schedule"], edited)

        with (
            patch.object(config, "local_today", return_value=_TODAY),
            patch.object(main, "_bg_sync"),
        ):
            activate_response = main.coach_plan_draft_activate(draft["plan_id"])

        activated = _body(activate_response)
        self.assertTrue(activated["ok"])
        self.assertEqual(activated["plan_id"], draft["plan_id"])
        self.assertEqual(db.get_plan_day("2026-08-18")["title"], "Edited easy swim")
        self.assertEqual(db.get_plan_draft(draft["plan_id"])["status"], "active")
        self.assertIsNone(db.get_latest_plan_draft(status="draft"))

    def test_activation_upserts_dates_when_event_has_no_seeded_plan(self) -> None:
        schedule = _schedule()
        self.assertEqual(db.get_plan(), [])
        draft = db.create_plan_draft(schedule, source_message="first arbitrary-event plan")

        with patch.object(config, "local_today", return_value=_TODAY):
            result = coach.activate_weekplan(draft["plan_id"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], len(schedule))
        self.assertEqual([day["title"] for day in db.get_plan()], ["Easy swim", "Rest"])
        self.assertTrue(all(day["source"] == "coach" for day in db.get_plan()))

    def test_bike_and_lift_activate_as_two_independent_workout_records(self) -> None:
        schedule = [{
            "date": "2026-08-18", "title": "Easy spin", "discipline": "bike",
            "duration_min": 60, "intensity": "Z2", "tsb_target": 10,
            "structure": {"warmup": "10 min easy", "main": "50 min Z2", "cooldown": ""},
            "secondary_sessions": [{
                "title": "Upper 2", "discipline": "strength", "duration_min": 40,
                "intensity": "1-2 RIR",
                "structure": {"warmup": "5 min mobility", "main": "Six exercises",
                              "cooldown": "5 min easy"},
                "why": "Maintain upper-body muscle without changing the ride.",
            }],
            "is_rest": 0, "why": "Keep aerobic work non-impact.",
        }]
        draft = db.create_plan_draft(schedule, source_message="two sessions")

        result = coach.activate_weekplan(draft["plan_id"])

        bike = db.get_plan_day("2026-08-18")
        lift = db.get_plan_strength("2026-08-18")
        self.assertEqual(bike["discipline"], "bike")
        self.assertEqual(bike["duration_min"], 60)
        self.assertEqual(lift["title"], "Upper 2")
        self.assertEqual(lift["duration_min"], 40)
        self.assertEqual(lift["structure"]["main"], "Six exercises")
        self.assertEqual(result["updated_strength_days"][0]["date"], "2026-08-18")

    def test_brick_requires_an_explicit_run_off_the_bike_leg(self) -> None:
        bad = [{
            "date": "2026-08-18", "title": "Bike + Upper", "discipline": "brick",
            "duration_min": 100, "intensity": "AM bike; PM lift", "tsb_target": 10,
            "structure": {"warmup": "10 min", "main": "AM bike; PM lift", "cooldown": ""},
            "is_rest": 0, "why": "Two sessions today.",
        }]
        with self.assertRaisesRegex(coach.WeekPlanValidationError, "explicit structure.run"):
            coach.validate_weekplan(bad)

    def test_true_bike_run_brick_remains_one_brick_record(self) -> None:
        brick = [{
            "date": "2026-08-18", "title": "Bike to run", "discipline": "brick",
            "duration_min": 80, "intensity": "easy", "tsb_target": 10,
            "structure": {"warmup": "10 min bike", "main": "50 min bike",
                          "run": "20 min run off the bike", "cooldown": ""},
            "is_rest": 0, "why": "Practice the immediate transition.",
        }]
        draft = db.create_plan_draft(brick, source_message="real brick")

        coach.activate_weekplan(draft["plan_id"])

        self.assertEqual(db.get_plan_day("2026-08-18")["discipline"], "brick")
        self.assertIsNone(db.get_plan_strength("2026-08-18"))

    def test_activation_rolls_back_every_day_and_status_on_write_failure(self) -> None:
        schedule = _schedule()
        for day in schedule:
            db.upsert_plan_day(_seed_day(day))
        draft = db.create_plan_draft(schedule, source_message="transaction test")
        prepared, skipped = coach._weekplan_for_activation(schedule)
        prepared[0]["title"] = "Must roll back"
        prepared[1]["structure"] = {"not_json": object()}

        with self.assertRaises(TypeError):
            db.activate_plan_draft(draft["plan_id"], prepared, skipped=skipped)

        self.assertEqual(db.get_plan_day("2026-08-18")["title"], "Easy swim")
        self.assertEqual(db.get_plan_draft(draft["plan_id"])["status"], "draft")
        self.assertEqual(db.get_plan_history(), [])

    def test_ui_references_plan_id_and_recovers_latest_draft(self) -> None:
        html = (pathlib.Path(__file__).parents[1] / "static" / "index.html").read_text()
        self.assertIn("/api/coach/plan-drafts/latest", html)
        self.assertIn("addWeekPlan(r.proposed_week,r.plan_id)", html)
        self.assertIn("{plan_id:Number(id)}", html)
        self.assertNotIn("thinking.textContent=visibleCoachDraft(draft)", html)
        self.assertIn('class="cwork cprimary"', html)
        self.assertIn("openStrengthDetail(d)", html)
        self.assertIn("w.secondary_sessions", html)
        self.assertNotIn("renderDayLift(planDay.date)", html)


if __name__ == "__main__":
    unittest.main()
