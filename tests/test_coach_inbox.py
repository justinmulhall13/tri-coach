from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import main


def _body(response) -> dict:
    return json.loads(response.body)


class CoachInboxTests(unittest.TestCase):
    def test_unchanged_event_still_returns_waiting_unread_message(self) -> None:
        inbox = {"id": "event-1", "reply": "Sleep update", "created_at": "now"}
        values = {
            "coach_event_sig": "same-signature",
            "coach_unread": json.dumps(inbox),
        }
        with (
            patch.object(main, "_training_signature", return_value="same-signature"),
            patch.object(main.db, "get_meta", side_effect=lambda key: values.get(key)),
        ):
            payload = _body(main.coach_brief())

        self.assertTrue(payload["skipped"])
        self.assertTrue(payload["unread"])
        self.assertEqual(payload["inbox"]["id"], "event-1")

    def test_new_training_event_creates_durable_unread_message(self) -> None:
        values = {"coach_event_sig": "old-signature", "coach_unread": None}

        def get_meta(key):
            return values.get(key)

        def set_meta(key, value):
            values[key] = value

        with (
            patch.object(main, "_training_signature", return_value="new-signature"),
            patch.object(main.db, "get_meta", side_effect=get_meta),
            patch.object(main.db, "set_meta", side_effect=set_meta),
            patch.object(main.coach, "invalidate_context_cache"),
            patch.object(main.coach, "morning_brief", return_value={
                "reply": "Your workout synced.", "model": "test", "celebrate": False,
            }),
        ):
            payload = _body(main.coach_brief())

        self.assertTrue(payload["unread"])
        self.assertEqual(payload["inbox"]["reply"], "Your workout synced.")
        self.assertEqual(values["coach_event_sig"], "new-signature")

    def test_read_ack_cannot_clear_a_newer_message(self) -> None:
        inbox = {"id": "new-event", "reply": "New sleep update"}
        with (
            patch.object(main, "_coach_unread", return_value=inbox),
            patch.object(main.db, "set_meta") as set_meta,
        ):
            payload = _body(main.coach_inbox_read({"id": "old-event"}))

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["unread"])
        set_meta.assert_not_called()

    def test_read_ack_requires_the_message_id(self) -> None:
        inbox = {"id": "event-1", "reply": "Sleep update"}
        with (
            patch.object(main, "_coach_unread", return_value=inbox),
            patch.object(main.db, "set_meta") as set_meta,
        ):
            response = main.coach_inbox_read({})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(_body(response)["ok"])
        set_meta.assert_not_called()


if __name__ == "__main__":
    unittest.main()
