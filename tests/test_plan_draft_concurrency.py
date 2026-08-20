from __future__ import annotations

import datetime
import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import config, db


_TODAY = datetime.date(2026, 8, 17)


def _schedule(title: str = "Easy swim") -> list[dict]:
    return [
        {"date": "2026-08-18", "title": title, "discipline": "swim",
         "duration_min": 35, "intensity": "easy", "tsb_target": 10,
         "structure": {"warmup": "200 easy", "main": "1000 smooth", "cooldown": "100 easy"},
         "is_rest": 0, "why": "Aerobic work."},
        {"date": "2026-08-19", "title": "Rest", "discipline": "rest",
         "duration_min": 0, "intensity": "rest", "tsb_target": 10,
         "structure": {"warmup": "", "main": "Full rest", "cooldown": ""},
         "is_rest": 1, "why": "Absorb load."},
    ]


class PlanDraftConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "concurrency.db"
        db._local = threading.local()
        clock = patch.object(config, "local_today", return_value=_TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        # Materialise the schema on the main thread before workers race on it.
        db.get_plan()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def test_simultaneous_activation_applies_the_draft_exactly_once(self) -> None:
        draft = db.create_plan_draft(_schedule(), source_message="race")
        barrier = threading.Barrier(8)

        def activate() -> dict:
            barrier.wait()
            return db.activate_plan_draft(draft["plan_id"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(activate) for _ in range(8)]]

        applied = [r for r in results if r.get("ok") and not r.get("already_active")]
        self.assertEqual(len(applied), 1, results)
        # Everyone else must report the truth, never a second successful apply.
        for result in results:
            self.assertTrue(result.get("ok") or result.get("error"), result)
        self.assertEqual(db.get_plan_draft(draft["plan_id"])["status"], "active")

    def test_a_concurrent_edit_cannot_split_stored_and_applied_schedules(self) -> None:
        """The activated days must match the draft as stored, not a half-applied
        mixture of an edit landing mid-activation."""
        draft = db.create_plan_draft(_schedule("Original swim"), source_message="race")
        barrier = threading.Barrier(2)

        def activate() -> dict:
            barrier.wait()
            return db.activate_plan_draft(draft["plan_id"])

        def edit() -> None:
            barrier.wait()
            try:
                db.update_plan_draft(draft["plan_id"], _schedule("Edited swim"))
            except Exception:  # noqa: BLE001 - losing the race is a valid outcome
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            activation = pool.submit(activate)
            pool.submit(edit)
            result = activation.result()

        if result.get("ok") and not result.get("already_active"):
            stored = db.get_plan_draft(draft["plan_id"])["schedule"]
            titles = {d["title"] for d in stored}
            applied = {db.get_plan_day(d["date"])["title"] for d in stored}
            # Whichever version won, the plan must reflect that one version only.
            self.assertTrue(applied <= titles, f"applied={applied} stored={titles}")

    def test_a_superseded_draft_loses_the_race_to_the_newest_one(self) -> None:
        """Creating a draft supersedes the previous one, so even a simultaneous
        activation of the stale draft must be refused rather than interleaved."""
        first = db.create_plan_draft(_schedule("First swim"), source_message="a")
        second = db.create_plan_draft(_schedule("Second swim"), source_message="b")
        barrier = threading.Barrier(2)

        def activate(plan_id: int) -> dict:
            barrier.wait()
            return db.activate_plan_draft(plan_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in (
                pool.submit(activate, first["plan_id"]),
                pool.submit(activate, second["plan_id"]),
            )]

        applied = [r for r in results if r.get("ok")]
        refused = [r for r in results if not r.get("ok")]
        self.assertEqual(len(applied), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0]["status"], "superseded")
        # Only the newest draft may reach the plan, and it does so whole.
        self.assertEqual(db.get_plan_day("2026-08-18")["title"], "Second swim")
        self.assertEqual(db.get_plan_day("2026-08-19")["title"], "Rest")

    def test_an_invalid_plan_id_never_raises_into_the_route(self) -> None:
        for bad in (None, "abc", "", [], {}, 3.7):
            result = db.activate_plan_draft(bad)
            self.assertFalse(result["ok"], bad)
            self.assertIn("error", result)

    def test_a_draft_with_corrupt_stored_json_fails_loudly(self) -> None:
        draft = db.create_plan_draft(_schedule(), source_message="race")
        with db._conn() as conn:
            conn.execute("UPDATE plan_drafts SET schedule_json=? WHERE id=?",
                         ("{not json", draft["plan_id"]))
        with self.assertRaises(ValueError):
            db.activate_plan_draft(draft["plan_id"])
        # And the draft must not be left marked active after a failed apply.
        self.assertEqual(db.get_plan_draft(draft["plan_id"])["status"], "draft")

    def test_concurrent_draft_creation_produces_distinct_ids(self) -> None:
        barrier = threading.Barrier(8)

        def create(index: int) -> int:
            barrier.wait()
            return db.create_plan_draft(_schedule(f"Swim {index}"),
                                        source_message=f"m{index}")["plan_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = [f.result() for f in [pool.submit(create, i) for i in range(8)]]
        self.assertEqual(len(set(ids)), 8, ids)

    def test_the_latest_draft_is_unambiguous_after_a_creation_race(self) -> None:
        barrier = threading.Barrier(6)

        def create(index: int) -> int:
            barrier.wait()
            return db.create_plan_draft(_schedule(f"Swim {index}"),
                                        source_message=f"m{index}")["plan_id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = [f.result() for f in [pool.submit(create, i) for i in range(6)]]
        latest = db.get_latest_plan_draft()
        self.assertEqual(latest["plan_id"], max(ids))


if __name__ == "__main__":
    unittest.main()
