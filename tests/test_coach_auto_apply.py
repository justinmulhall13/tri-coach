"""A coach reply that changes the plan changes it: no Accept tap, one Undo.

The athlete asked that any answer which should change the plan actually does.
These tests pin the three parts of that contract: the block is applied when the
reply is finished, a missing block is chased once, and Undo restores the whole
day (time, lift and extra sessions included).
"""
from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import coach, config, db, main

_TODAY = datetime.date(2026, 8, 17)


def _msg(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           model="test-model", stop_reason="end_turn")


def _day(date: str, title: str, discipline: str = "run", start: str | None = "07:00") -> dict:
    return {"date": date, "week_index": 0, "phase": "build", "discipline": discipline,
            "title": title, "structure": {"warmup": "", "main": title, "cooldown": ""},
            "duration_min": 45, "intensity": "Z2", "tsb_target": 10, "why": "base",
            "is_rest": False, "source": "seed", "start_time": start}


class AutoApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._clock = patch.object(config, "local_today", return_value=_TODAY)
        self._clock.start()
        self.addCleanup(self._clock.stop)
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "auto.db"
        db._local = threading.local()
        for date, title in (("2026-08-17", "Easy run"), ("2026-08-18", "Threshold run"),
                            ("2026-08-19", "Easy swim")):
            db.upsert_plan_day(_day(date, title))
        sync = patch.object(main, "_bg_sync")
        self.bg_sync = sync.start()
        self.addCleanup(sync.stop)

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    def test_adjustment_is_applied_and_returns_undo_ids(self) -> None:
        adj = {"date": "2026-08-18", "title": "Easy run (legs sore)", "discipline": "run",
               "duration_min": 40, "intensity": "Z2", "start_time": "18:00",
               "structure": {"warmup": "", "main": "40 min easy", "cooldown": ""}}
        result = coach._finish_chat("Legs are sore, make tomorrow easy", "2026-08-17",
                                    _msg("Easy tomorrow.\n```adjustment\n" + json.dumps(adj) + "\n```"))
        applied = result["applied_change"]
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["kind"], "day")
        self.assertEqual(applied["days"][0]["title"], "Easy run (legs sore)")
        self.assertIsNone(result["proposed_adjustment"])
        self.assertEqual(db.get_plan_day("2026-08-18")["title"], "Easy run (legs sore)")

        undo = json.loads(main.plan_undo({"history_ids": applied["undo_ids"]}).body)
        self.assertTrue(undo["ok"])
        restored = db.get_plan_day("2026-08-18")
        self.assertEqual(restored["title"], "Threshold run")
        self.assertEqual(restored["start_time"], "07:00")
        self.bg_sync.assert_called()

    def test_past_dated_adjustment_never_rewrites_today(self) -> None:
        adj = {"date": "2026-08-10", "title": "Long run", "discipline": "run"}
        result = coach._finish_chat("I did my long run last week", "2026-08-17",
                                    _msg("Nice.\n```adjustment\n" + json.dumps(adj) + "\n```"))
        self.assertFalse(result["applied_change"]["ok"])
        self.assertEqual(db.get_plan_day("2026-08-17")["title"], "Easy run")

    def test_week_undo_restores_lift_and_secondary_sessions(self) -> None:
        conn = db._conn()
        now = "2026-08-17T00:00:00"
        db._write_plan_strength(conn, db._active_profile_id(),
                                {"date": "2026-08-19", "slot": "full", "title": "Full body A",
                                 "duration_min": 50, "routine": None}, now)
        db._replace_plan_secondary(conn, db._active_profile_id(), "2026-08-19",
                                   [{"discipline": "bike", "title": "Commute",
                                     "duration_min": 20}], now)
        conn.commit()
        week = [
            {"date": "2026-08-18", "title": "Rest", "discipline": "rest", "duration_min": 0,
             "intensity": "rest", "tsb_target": 10, "is_rest": 1, "why": "Recover.",
             "structure": {"warmup": "", "main": "Full rest", "cooldown": ""}},
            {"date": "2026-08-19", "title": "Threshold run", "discipline": "run",
             "duration_min": 50, "intensity": "threshold", "tsb_target": 10, "is_rest": 0,
             "why": "Moved from Tue.", "start_time": "17:30",
             "structure": {"warmup": "15 easy", "main": "3x8 min threshold", "cooldown": "10 easy"}},
        ]
        result = coach._finish_chat("Swap Tue and Wed", "2026-08-17",
                                    _msg("Swapped.\n```weekplan\n" + json.dumps(week) + "\n```"))
        applied = result["applied_change"]
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["kind"], "week")
        self.assertIsNone(db.get_plan_strength("2026-08-19"))
        self.assertEqual(db.get_plan_secondary("2026-08-19"), [])

        main.plan_undo({"history_ids": applied["undo_ids"]})
        self.assertEqual(db.get_plan_day("2026-08-18")["title"], "Threshold run")
        wed = db.get_plan_day("2026-08-19")
        self.assertEqual((wed["title"], wed["start_time"]), ("Easy swim", "07:00"))
        self.assertEqual(db.get_plan_strength("2026-08-19")["title"], "Full body A")
        self.assertEqual([s["title"] for s in db.get_plan_secondary("2026-08-19")], ["Commute"])

    def test_placeholder_blocks_keep_the_days_real_text(self) -> None:
        # A move-only edit wrote "existing swim warmup" over the set, and the
        # watch then had nothing to run.
        db.edit_plan_day("2026-08-19", {"structure": {
            "warmup": "300 easy + 4×50 drill, 15s rest",
            "main": "1500 continuous steady; 4×100 pull buoy, 15s rest",
            "cooldown": "100 easy backstroke"}}, source="seed", record_history=False)
        adj = {"date": "2026-08-19", "title": "Easy swim", "discipline": "swim",
               "start_time": "17:00",
               "structure": {"warmup": "existing swim warmup", "main": "Same set as planned",
                             "cooldown": "200 easy"}}
        coach.accept_adjustment(adj)
        st = db.get_plan_day("2026-08-19")["structure"]
        self.assertEqual(st["warmup"], "300 easy + 4×50 drill, 15s rest")
        self.assertEqual(st["main"], "1500 continuous steady; 4×100 pull buoy, 15s rest")
        self.assertEqual(st["cooldown"], "200 easy")

    def test_undo_rejects_bad_ids(self) -> None:
        self.assertEqual(main.plan_undo({"history_ids": "1"}).status_code, 400)
        self.assertEqual(main.plan_undo({"history_ids": [True]}).status_code, 400)
        self.assertEqual(main.plan_undo({"history_ids": [99999]}).status_code, 404)


class MissingBlockFollowupTests(unittest.TestCase):
    def test_change_request_without_block_is_chased(self) -> None:
        self.assertTrue(coach._needs_block_followup("I'm sick, skip tomorrow", "Rest up."))
        self.assertTrue(coach._needs_block_followup("How was my week?",
                                                    "I've moved Thursday's run to Friday."))

    def test_question_or_reply_with_block_is_not_chased(self) -> None:
        self.assertFalse(coach._needs_block_followup("How was my swim?", "Solid pacing."))
        self.assertFalse(coach._needs_block_followup(
            "Move my run", 'Done.\n```adjustment\n{"title": "Run"}\n```'))

    def test_followup_returns_only_the_block(self) -> None:
        block = '```adjustment\n{"title": "Easy run"}\n```'
        stream = SimpleNamespace(
            __enter__=None, get_final_message=lambda: _msg("Sure.\n" + block))

        class _S:
            def __enter__(self):
                return stream

            def __exit__(self, *exc):
                return None

        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: _S()))
        with patch.object(coach, "_message_kwargs", return_value={}):
            self.assertEqual(coach._followup_block(client, [], "Rest up."), block)

    def test_followup_none_adds_nothing(self) -> None:
        class _S:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: _msg("NONE"))

            def __exit__(self, *exc):
                return None

        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: _S()))
        with patch.object(coach, "_message_kwargs", return_value={}):
            self.assertEqual(coach._followup_block(client, [], "Rest up."), "")


if __name__ == "__main__":
    unittest.main()
