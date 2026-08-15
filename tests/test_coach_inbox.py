from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import coaching_contract, main


def _body(response) -> dict:
    return json.loads(response.body)


class CoachInboxTests(unittest.TestCase):
    @property
    def sig_key(self) -> str:
        return coaching_contract.scoped_meta_key("coach_event_sig")

    @property
    def unread_key(self) -> str:
        return coaching_contract.scoped_meta_key("coach_unread")

    def test_unchanged_event_still_returns_waiting_unread_message(self) -> None:
        inbox = {"id": "event-1", "reply": "Sleep update", "created_at": "now",
                 "event_profile_id": coaching_contract.event_profile_id()}
        values = {
            self.sig_key: "same-signature",
            self.unread_key: json.dumps(inbox),
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
        values = {self.sig_key: "old-signature", self.unread_key: None}

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
        self.assertEqual(values[self.sig_key], "new-signature")
        stored = json.loads(values[self.unread_key])
        self.assertEqual(stored["event_profile_id"], coaching_contract.event_profile_id())

    def test_unread_from_another_profile_is_rejected(self) -> None:
        inbox = {"id": "old", "reply": "Old event update", "event_profile_id": "old-event"}
        with patch.object(main.db, "get_meta", return_value=json.dumps(inbox)):
            self.assertIsNone(main._coach_unread())

    def test_legacy_signature_migration_does_not_create_a_launch_update(self) -> None:
        legacy_payload = {
            "sleep_date": "2026-08-15", "activity_date": "2026-08-14",
            "activity_ids": ["1"], "completion": None,
        }
        current_payload = {
            **legacy_payload,
            "event_profile_id": coaching_contract.event_profile_id(),
        }
        current_sig = json.dumps(current_payload, sort_keys=True, separators=(",", ":"))
        values = {
            self.sig_key: None,
            "coach_event_sig": json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")),
            self.unread_key: "",
        }

        with (
            patch.object(main, "_training_signature", return_value=current_sig),
            patch.object(main.db, "get_meta", side_effect=lambda key: values.get(key)),
            patch.object(main.db, "set_meta", side_effect=lambda key, value: values.__setitem__(key, value)),
            patch.object(main.coach, "morning_brief") as brief,
        ):
            payload = _body(main.coach_brief())

        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "no new sleep or workout event")
        self.assertEqual(values[self.sig_key], current_sig)
        brief.assert_not_called()

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
