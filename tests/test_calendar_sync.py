from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import calendar_agent, calendar_source, calendar_sync, config, db, main


TODAY = datetime.date(2026, 8, 23)


def _primary(date: str, title: str = "Easy run", **extra) -> dict:
    return {
        "date": date, "week_index": 0, "phase": "build",
        "discipline": "run", "title": title, "duration_min": 45,
        "intensity": "Z2", "structure": {"main": "45 min easy"},
        "is_rest": 0, "source": "coach", "tsb_target": 10,
        "why": "Aerobic maintenance.", "start_time": "07:00", **extra,
    }


def _strength(date: str, title: str = "Upper 1", **extra) -> dict:
    return {
        "date": date, "slot": "upper", "title": title,
        "routine": {"title": title, "exercises": []},
        "duration_min": 50, "intensity": "2 RIR",
        "structure": {"main": "Six exercises"}, "start_time": "17:00",
        "effort_level": "moderate", "effort_cue": "Leave two reps", **extra,
    }


class CalendarSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "calendar.db"
        db._local = threading.local()
        self._clock = patch.object(config, "local_today", return_value=TODAY)
        self._clock.start()

    def tearDown(self) -> None:
        self._clock.stop()
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def test_moving_a_lift_never_moves_the_same_day_run(self) -> None:
        db.upsert_plan_day(_primary("2026-08-24"))
        db.upsert_plan_day(_primary("2026-08-25", title="Tempo run"))
        db.upsert_plan_strength(_strength("2026-08-24"))

        result = calendar_sync.move_strength_session(
            "2026-08-24", new_date="2026-08-25", new_start="18:30",
            do_reconcile=False,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(db.get_plan_day("2026-08-24")["title"], "Easy run")
        self.assertEqual(db.get_plan_day("2026-08-25")["title"], "Tempo run")
        self.assertIsNone(db.get_plan_strength("2026-08-24"))
        self.assertEqual(db.get_plan_strength("2026-08-25")["title"], "Upper 1")
        self.assertEqual(db.get_plan_strength("2026-08-25")["start_time"], "18:30")

    def test_primary_and_strength_get_distinct_google_events(self) -> None:
        date = "2026-08-24"
        db.upsert_plan_day(_primary(date))
        db.upsert_plan_strength(_strength(date))

        def upsert(day, session_kind="primary"):
            return {"event_id": "g-primary" if session_kind == "primary" else "g-strength"}

        with (
            patch.object(calendar_source, "_creds_status", return_value={
                "credentials_present": True, "token_present": True,
            }),
            patch.object(calendar_source, "upsert_workout_event", side_effect=upsert) as write,
        ):
            result = calendar_sync.push_days([date])

        self.assertEqual(result["pushed"], 2)
        self.assertEqual(len(write.call_args_list), 2)
        self.assertEqual(write.call_args_list[0].kwargs, {})
        self.assertEqual(write.call_args_list[0].args[0]["date"], date)
        self.assertEqual(write.call_args_list[1].kwargs, {"session_kind": "strength"})
        self.assertEqual(write.call_args_list[1].args[0]["date"], date)
        self.assertEqual(db.get_plan_day(date)["gcal_event_id"], "g-primary")
        self.assertEqual(db.get_plan_strength(date)["gcal_event_id"], "g-strength")

    def test_strength_google_event_has_an_independent_identity(self) -> None:
        body = calendar_source._event_body(_strength("2026-08-24"), "strength")
        private = body["extendedProperties"]["private"]
        self.assertEqual(private[calendar_source.TRICOACH_DATE_KEY], "2026-08-24")
        self.assertEqual(private[calendar_source.TRICOACH_KIND_KEY], "strength")
        self.assertEqual(body["summary"], "💪 Upper 1")

    def test_calendar_agent_context_can_target_a_named_lift(self) -> None:
        db.upsert_plan_day(_primary("2026-08-24"))
        db.upsert_plan_strength(_strength("2026-08-24"))
        with patch.object(calendar_source, "get_events",
                          return_value={"available": True, "events": []}):
            context = calendar_agent._context()
        self.assertIn("STRENGTH SESSIONS", context)
        self.assertIn("Upper 1", context)

    def test_transient_write_failure_never_prunes_the_tracked_events(self) -> None:
        date = "2026-08-24"
        db.upsert_plan_day(_primary(date, gcal_event_id="run-event"))
        db.upsert_plan_strength(_strength(date, gcal_event_id="lift-event"))
        owned = [
            {"id": "run-event", "plan_date": date, "session_kind": "primary"},
            {"id": "lift-event", "plan_date": date, "session_kind": "strength"},
        ]
        with (
            patch.object(calendar_source, "_creds_status", return_value={
                "credentials_present": True, "token_present": True,
            }),
            patch.object(calendar_source, "get_events",
                         return_value={"available": True, "events": []}),
            patch.object(calendar_source, "upsert_workout_event",
                         return_value={"error": "temporary outage"}),
            patch.object(calendar_source, "fetch_workout_events",
                         return_value={"events": owned}),
            patch.object(calendar_source, "delete_workout_event") as delete,
        ):
            result = calendar_sync.reconcile(days=2)

        self.assertEqual(len(result["errors"]), 2)
        delete.assert_not_called()

    def test_google_can_move_a_lift_without_touching_primary_workouts(self) -> None:
        db.upsert_plan_day(_primary("2026-08-24"))
        db.upsert_plan_day(_primary("2026-08-25", title="Tempo run"))
        db.upsert_plan_strength(_strength(
            "2026-08-24", gcal_event_id="lift-event",
            pos_updated_at="2026-08-23T10:00:00+00:00",
        ))
        event = {
            "id": "lift-event", "plan_date": "2026-08-24",
            "session_kind": "strength",
            "start": "2026-08-25T18:00:00-07:00", "all_day": False,
            "updated": "2026-08-23T11:00:00+00:00",
        }
        with (
            patch.object(calendar_source, "_creds_status", return_value={
                "credentials_present": True, "token_present": True,
            }),
            patch.object(calendar_source, "fetch_workout_events",
                         return_value={"events": [event]}),
        ):
            result = calendar_sync.pull_from_google()

        self.assertEqual(result["pulled"], 1)
        self.assertEqual(db.get_plan_day("2026-08-24")["title"], "Easy run")
        self.assertEqual(db.get_plan_day("2026-08-25")["title"], "Tempo run")
        self.assertIsNone(db.get_plan_strength("2026-08-24"))
        self.assertEqual(db.get_plan_strength("2026-08-25")["start_time"], "18:00")

    def test_older_google_position_cannot_undo_a_recent_app_drag(self) -> None:
        date = "2026-08-24"
        db.upsert_plan_day(_primary(
            date, start_time="08:00", gcal_event_id="run-event",
            pos_updated_at="2026-08-23T12:00:00+00:00",
        ))
        stale = {
            "id": "run-event", "plan_date": date, "session_kind": "primary",
            "start": "2026-08-24T07:00:00-07:00", "all_day": False,
            "updated": "2026-08-23T11:59:00+00:00",
        }
        with (
            patch.object(calendar_source, "_creds_status", return_value={
                "credentials_present": True, "token_present": True,
            }),
            patch.object(calendar_source, "fetch_workout_events",
                         return_value={"events": [stale]}),
        ):
            result = calendar_sync.pull_from_google()

        self.assertEqual(result["pulled"], 0)
        self.assertEqual(db.get_plan_day(date)["start_time"], "08:00")

    def test_invalid_or_missing_drop_target_never_erases_the_source_workout(self) -> None:
        date = "2026-08-24"
        db.upsert_plan_day(_primary(date))

        bad_time = calendar_sync.move_session(
            date, new_start="25:90", do_reconcile=False)
        missing_day = calendar_sync.move_session(
            date, new_date="2026-09-15", new_start="07:00",
            do_reconcile=False,
        )

        self.assertIn("new_start", bad_time["error"])
        self.assertIn("no plan day", missing_day["error"])
        self.assertEqual(db.get_plan_day(date)["title"], "Easy run")
        self.assertFalse(db.get_plan_day(date)["is_rest"])

    def test_drag_route_waits_for_google_and_returns_its_status(self) -> None:
        moved = {"ok": True, "changed": ["2026-08-24"], "days": []}
        synced = {"connected": True, "pushed": 1, "errors": None}
        with (
            patch.object(calendar_sync, "move_session", return_value=moved) as move,
            patch.object(calendar_sync, "push_days", return_value=synced) as push,
        ):
            response = main.workout_move({
                "date": "2026-08-24", "new_start": "08:00",
                "session_kind": "primary",
            })

        body = json.loads(response.body)
        move.assert_called_once_with(
            "2026-08-24", new_date=None, new_start="08:00", do_reconcile=False)
        push.assert_called_once_with(["2026-08-24"])
        self.assertEqual(body["google_sync"], synced)


if __name__ == "__main__":
    unittest.main()
