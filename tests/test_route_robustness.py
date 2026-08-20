from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main


# Bodies a client can send that previously produced an unhandled exception and
# therefore a 500. A malformed request is the caller's mistake and must be
# reported as one.
MALFORMED = ({}, {"x": 1}, {"date": ""}, {"text": ""}, {"date": None})


class MalformedBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "robust.db"
        db._local = threading.local()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def test_a_completion_without_a_date_is_a_bad_request(self) -> None:
        for body in MALFORMED:
            response = self.client.post("/api/completions", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("date", response.json()["error"])

    def test_a_valid_completion_still_works(self) -> None:
        response = self.client.post("/api/completions",
                                    json={"date": "2026-08-24", "status": "done"})
        self.assertEqual(response.status_code, 200)

    def test_a_constraint_without_text_is_a_bad_request(self) -> None:
        for body in MALFORMED:
            response = self.client.post("/api/constraints", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("text", response.json()["error"])

    def test_a_valid_constraint_still_works(self) -> None:
        response = self.client.post("/api/constraints",
                                    json={"date": "2026-08-24", "text": "Left achilles sore"})
        self.assertEqual(response.status_code, 200)

    def test_whitespace_only_input_counts_as_missing(self) -> None:
        self.assertEqual(
            self.client.post("/api/constraints", json={"text": "   "}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/completions", json={"date": "  "}).status_code, 400)


class StrengthBlockBoundsTests(unittest.TestCase):
    """`weeks` reaches date arithmetic, so an unclamped value overflowed."""

    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "bounds.db"
        db._local = threading.local()
        self.client = TestClient(main.app)
        self._garmin = patch.object(main.garmin_source, "get_readiness", return_value=None)
        self._garmin.start()
        self.addCleanup(self._garmin.stop)

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def _post(self, **body):
        return self.client.post("/api/plan/strength-block", json=body)

    def test_an_enormous_week_count_is_clamped_not_an_overflow(self) -> None:
        for weeks in (10 ** 6, 10 ** 9, 52 * 1000):
            response = self._post(start="2026-08-24", weeks=weeks, sessions_per_week=4)
            self.assertEqual(response.status_code, 200, weeks)
            self.assertLessEqual(response.json()["weeks"], main.MAX_BLOCK_WEEKS)

    def test_a_negative_or_zero_week_count_becomes_one_week(self) -> None:
        for weeks in (0, -3):
            response = self._post(start="2026-08-24", weeks=weeks, sessions_per_week=4)
            self.assertEqual(response.status_code, 200, weeks)
            self.assertEqual(response.json()["weeks"], 1)

    def test_an_unparseable_start_is_a_bad_request(self) -> None:
        for start in ("bad", "2026-13-45", "next monday"):
            self.assertEqual(self._post(start=start, weeks=2).status_code, 400, start)

    def test_a_non_numeric_session_count_is_a_bad_request(self) -> None:
        self.assertEqual(self._post(start="2026-08-24", sessions_per_week="lots").status_code, 400)

    def test_an_ordinary_block_still_succeeds(self) -> None:
        response = self._post(start="2026-08-24", weeks=3, sessions_per_week=4)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["weeks"], 3)


if __name__ == "__main__":
    unittest.main()
