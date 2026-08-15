from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import coaching_contract, db


def _profile(profile_id: str, mode: str) -> dict:
    return {
        "id": profile_id,
        "mode": mode,
        "event": profile_id,
        "date": "2027-01-01",
        "disciplines_and_distances": {"run_km": 10},
    }


def _day(date: str, title: str) -> dict:
    return {
        "date": date,
        "week_index": 0,
        "phase": "build",
        "discipline": "run",
        "title": title,
        "structure": {"main": "30 min easy"},
        "duration_min": 30,
        "intensity": "easy",
        "tsb_target": 10,
        "why": "test",
        "is_rest": 0,
        "source": "seed",
    }


class EventScopedPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "scoped.db"
        db._local = threading.local()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def test_chat_constraints_and_plan_do_not_cross_event_profiles(self) -> None:
        first = _profile("first-event", "TRIATHLON")
        second = _profile("second-event", "RUNNING")

        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            db.add_chat("user", "first course assumption")
            db.add_constraint("2026-08-15", "first event constraint")
            db.upsert_plan_day(_day("2026-08-15", "first plan"))

        with patch.object(coaching_contract, "EVENT_PROFILE", second):
            self.assertEqual(db.get_chat(), [])
            self.assertEqual(db.get_constraint_history(), [])
            self.assertEqual(db.get_plan(), [])
            db.add_chat("user", "second event context")
            db.add_constraint("2026-08-15", "second event constraint")
            db.upsert_plan_day(_day("2027-01-01", "second plan"))
            self.assertEqual(db.get_chat()[0]["content"], "second event context")

        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            self.assertEqual(db.get_chat()[0]["content"], "first course assumption")
            self.assertEqual(db.get_constraint_history()[0]["text"], "first event constraint")
            self.assertEqual(db.get_plan()[0]["title"], "first plan")

    def test_two_profiles_keep_independent_plan_rows_on_the_same_date(self) -> None:
        first = _profile("first-event", "TRIATHLON")
        second = _profile("second-event", "RUNNING")
        shared_date = "2026-08-15"

        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            db.upsert_plan_day(_day(shared_date, "first profile workout"))
        with patch.object(coaching_contract, "EVENT_PROFILE", second):
            db.upsert_plan_day(_day(shared_date, "second profile workout"))
            self.assertEqual(db.get_plan_day(shared_date)["title"], "second profile workout")
        with patch.object(coaching_contract, "EVENT_PROFILE", first):
            self.assertEqual(db.get_plan_day(shared_date)["title"], "first profile workout")

        rows = db._conn().execute(
            "SELECT event_profile_id,title FROM plan_days WHERE date=? ORDER BY event_profile_id",
            (shared_date,),
        ).fetchall()
        self.assertEqual([(row["event_profile_id"], row["title"]) for row in rows], [
            ("first-event", "first profile workout"),
            ("second-event", "second profile workout"),
        ])

    def test_legacy_date_primary_key_migrates_without_losing_data(self) -> None:
        legacy = sqlite3.connect(db._DB_PATH)
        legacy.execute(
            """CREATE TABLE plan_days (
                date TEXT PRIMARY KEY,
                week_index INTEGER NOT NULL,
                phase TEXT NOT NULL,
                discipline TEXT NOT NULL,
                title TEXT NOT NULL,
                structure TEXT NOT NULL,
                duration_min INTEGER,
                intensity TEXT,
                why TEXT,
                is_rest INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'seed',
                updated_at TEXT NOT NULL
            )"""
        )
        legacy.execute(
            """INSERT INTO plan_days
               (date,week_index,phase,discipline,title,structure,duration_min,
                intensity,why,is_rest,source,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-08-15", 4, "taper", "bike", "preserve me", '{"main":"HR 140-150"}',
             45, "race pace", "migration test", 0, "seed", "2026-08-10T12:00:00"),
        )
        legacy.commit()
        legacy.close()

        profile = _profile("legacy-event", "TRIATHLON")
        with patch.object(coaching_contract, "EVENT_PROFILE", profile):
            migrated = db.get_plan_day("2026-08-15")

        self.assertIsNotNone(migrated)
        self.assertEqual(migrated["title"], "preserve me")
        self.assertEqual(migrated["structure"], {"main": "HR 140-150"})
        self.assertEqual(migrated["event_profile_id"], "legacy-event")
        key_columns = [row["name"] for row in sorted(
            (row for row in db._conn().execute("PRAGMA table_info(plan_days)").fetchall() if row["pk"]),
            key=lambda row: row["pk"],
        )]
        self.assertEqual(key_columns, ["event_profile_id", "date"])


if __name__ == "__main__":
    unittest.main()
