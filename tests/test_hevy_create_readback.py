"""A custom exercise that Hevy created must never be reported as a failure.

Hevy's create-exercise call can answer 2xx with a reply the client cannot use
(the new id as bare text, or no id at all). The exercise exists by then, so the
app has to pick it up rather than say "could not match" and create it again.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app import hevy_connector, hevy_exercises


class _Reply:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


def _post(raw: bytes, **kwargs: object) -> dict:
    client = hevy_connector.HevyAPIConnector("test-key")
    with mock.patch.object(hevy_connector, "urlopen", return_value=_Reply(raw)):
        return client._request("POST", "exercise_templates", body={"exercise": {}}, **kwargs)


class BareIdReplyTests(unittest.TestCase):
    def test_a_bare_text_id_is_an_id(self) -> None:
        self.assertEqual(_post(b"b459cba5-cd6d-463c", scalar_key="id"), {"id": "b459cba5-cd6d-463c"})

    def test_a_json_string_or_number_is_an_id(self) -> None:
        self.assertEqual(_post(b'"abc123"', scalar_key="id"), {"id": "abc123"})
        self.assertEqual(_post(b"4821", scalar_key="id"), {"id": "4821"})

    def test_an_object_reply_is_unchanged(self) -> None:
        self.assertEqual(_post(b'{"id": "x1"}', scalar_key="id"), {"id": "x1"})

    def test_other_calls_still_reject_a_non_object_reply(self) -> None:
        with self.assertRaises(hevy_connector.HevyAPIError):
            _post(b'"abc123"')
        with self.assertRaises(hevy_connector.HevyAPIError):
            _post(b"not json at all")

    def test_prose_is_not_mistaken_for_an_id(self) -> None:
        with self.assertRaises(hevy_connector.HevyAPIError):
            _post(b"Internal error, try later", scalar_key="id")

    def test_the_catalogue_is_dropped_even_when_the_create_errors(self) -> None:
        client = hevy_connector.HevyAPIConnector("test-key")
        client._template_catalog = [{"id": "old", "title": "Old"}]
        client._template_catalog_at = 10.0
        with mock.patch.object(client, "_create_once",
                               side_effect=hevy_connector.HevyAPIError("boom")):
            with self.assertRaises(hevy_connector.HevyAPIError):
                client.create_exercise_template({"title": "New"}, idempotency_key="k")
        self.assertIsNone(client._template_catalog)


class _Hevy:
    """Stands in for Hevy: the create lands, the reply is what the test says."""

    def __init__(self, reply: object, lands: bool = True) -> None:
        self.reply, self.lands = reply, lands
        self.templates: list[dict] = []
        self.creates = 0

    def create_exercise_template(self, exercise: dict, *, idempotency_key: str) -> dict:
        self.creates += 1
        if self.lands:
            self.templates.append({"id": f"new-{self.creates}", "title": exercise["title"],
                                   "is_custom": True})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply  # type: ignore[return-value]

    def all_exercise_templates(self) -> list[dict]:
        return list(self.templates)


class ReadBackTests(unittest.TestCase):
    TITLE = "Landmill clean and press rotation"

    def test_a_create_that_landed_with_a_broken_reply_is_used(self) -> None:
        hevy = _Hevy(hevy_connector.HevyAPIError("Hevy API returned an unexpected payload"))
        report = hevy_exercises.resolve(self.TITLE, templates=[], connector=hevy)
        self.assertEqual(report["exercise_template_id"], "new-1")
        self.assertEqual(report["resolution"], "created")
        self.assertTrue(report["created"])

    def test_a_create_that_returned_no_id_is_used(self) -> None:
        report = hevy_exercises.resolve(self.TITLE, templates=[], connector=_Hevy({}))
        self.assertEqual(report["exercise_template_id"], "new-1")

    def test_the_next_send_matches_it_and_does_not_create_a_second(self) -> None:
        hevy = _Hevy({})
        hevy_exercises.resolve(self.TITLE, templates=[], connector=hevy)
        again = hevy_exercises.resolve(self.TITLE, templates=hevy.all_exercise_templates(),
                                       connector=hevy)
        self.assertEqual(again["resolution"], "matched")
        self.assertEqual(hevy.creates, 1)

    def test_a_real_rejection_is_still_a_failure_with_its_reason(self) -> None:
        hevy = _Hevy(hevy_connector.HevyAPIError("Hevy API returned HTTP 403: limit"), lands=False)
        report = hevy_exercises.resolve(self.TITLE, templates=[], connector=hevy)
        self.assertIsNone(report["exercise_template_id"])
        self.assertEqual(report["resolution"], "failed")
        self.assertIn("403", report["reason"])

    def test_read_back_needs_the_exact_title(self) -> None:
        hevy = _Hevy(hevy_connector.HevyAPIError("x"), lands=False)
        hevy.templates = [{"id": "other", "title": "Landmine Squat and Press"}]
        report = hevy_exercises.resolve(self.TITLE, templates=[], connector=hevy)
        self.assertIsNone(report["exercise_template_id"])


if __name__ == "__main__":
    unittest.main()
