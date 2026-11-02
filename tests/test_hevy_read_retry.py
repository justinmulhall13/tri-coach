"""A one-off bad Hevy response does not fail a routine create.

Hevy sometimes answers a read with a gateway error page instead of JSON. The
confirmed create used to stop on the first such reply with "Hevy API returned
invalid JSON", although the same request succeeded a moment later. Reads are
retried; writes still never are.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app import hevy_connector


class _Response:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._raw


def _ok(payload: dict) -> _Response:
    return _Response(json.dumps(payload).encode())


def _gateway_error() -> HTTPError:
    return HTTPError("https://example.test", 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))


class HevyReadRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connector = hevy_connector.HevyAPIConnector("secret",
                                                         base_url="https://example.test/v1")
        self._sleep = patch.object(hevy_connector.time, "sleep")
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()

    def test_an_html_reply_is_retried_and_the_read_succeeds(self) -> None:
        replies = [_Response(b"<html>502</html>"), _ok({"id": "t1", "title": "Squat"})]
        with patch.object(hevy_connector, "urlopen", side_effect=replies) as open_:
            template = self.connector.get_exercise_template("t1")
        self.assertEqual(template["id"], "t1")
        self.assertEqual(open_.call_count, 2)

    def test_a_gateway_error_is_retried(self) -> None:
        replies = [_gateway_error(), _ok({"id": "t1"})]
        with patch.object(hevy_connector, "urlopen", side_effect=replies):
            self.assertEqual(self.connector.get_exercise_template("t1")["id"], "t1")

    def test_a_persistent_failure_still_surfaces_after_three_tries(self) -> None:
        with patch.object(hevy_connector, "urlopen",
                          side_effect=lambda *a, **k: _Response(b"<html>")) as open_:
            with self.assertRaisesRegex(hevy_connector.HevyAPIError, "invalid JSON"):
                self.connector.get_exercise_template("t1")
        self.assertEqual(open_.call_count, 3)

    def test_a_client_error_is_not_retried(self) -> None:
        missing = HTTPError("https://example.test", 404, "Not Found", {}, io.BytesIO(b""))
        with patch.object(hevy_connector, "urlopen", side_effect=[missing]) as open_:
            with self.assertRaises(hevy_connector.HevyAPIError):
                self.connector.get_exercise_template("t1")
        self.assertEqual(open_.call_count, 1)

    def test_a_write_is_never_retried(self) -> None:
        with patch.object(hevy_connector, "urlopen",
                          side_effect=[_Response(b"<html>")]) as open_:
            with self.assertRaises(hevy_connector.HevyAPIError):
                self.connector.create_routine({"title": "A"}, idempotency_key="op-1")
        self.assertEqual(open_.call_count, 1)


if __name__ == "__main__":
    unittest.main()
