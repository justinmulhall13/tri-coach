from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import hevy_connector, main


def _json(response):
    return json.loads(response.body)


def _routine():
    return {
        "title": "Achilles-safe strength",
        "exercises": [{
            "exercise_template_id": "template-1",
            "sets": [{"type": "normal", "rep_range": {"start": 8, "end": 10}}],
        }],
    }


class _FakeHevy:
    def __init__(self, *, fail_create: bool = False):
        self.fail_create = fail_create
        self.workout_calls = []
        self.search_calls = []
        self.template_calls = []
        self.create_calls = []

    def status(self):
        return {"provider": "hevy", "connected": True, "transport": "test"}

    def get_workouts(self, *, page, page_size):
        self.workout_calls.append((page, page_size))
        return {"workouts": [{"id": "workout-1", "title": "Upper"}]}

    def search_exercise_templates(self, query):
        self.search_calls.append(query)
        return [{"id": "template-1", "title": "Split Squat"}]

    def get_exercise_template(self, template_id):
        self.template_calls.append(template_id)
        return {"id": template_id, "title": "Split Squat"}

    def create_routine(self, routine, *, idempotency_key):
        self.create_calls.append((routine, idempotency_key))
        if self.fail_create:
            raise hevy_connector.HevyAPIError("ambiguous create failure")
        return {"id": "routine-1", **routine}


class HevyAPIRouteTests(unittest.TestCase):
    def setUp(self):
        self.old_connector = hevy_connector.connector()

    def tearDown(self):
        hevy_connector.configure(self.old_connector)

    def test_hevy_api_is_protected_by_the_shared_api_auth_gate(self):
        with patch.object(main.config, "ACCESS_TOKEN", "test-secret"):
            response = TestClient(main.app).get("/api/integrations/hevy/workouts/recent")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "unauthorized"})

    def test_recent_workouts_and_template_search_are_bounded_reads(self):
        fake = _FakeHevy()
        hevy_connector.configure(fake)

        workouts = main.hevy_recent_workouts(limit=99)
        templates = main.hevy_exercise_template_search(q="  split squat  ")

        self.assertEqual(workouts.status_code, 200)
        self.assertEqual(_json(workouts)["workouts"][0]["id"], "workout-1")
        self.assertEqual(fake.workout_calls, [(1, 10)])
        self.assertEqual(templates.status_code, 200)
        self.assertEqual(_json(templates)["exercise_templates"][0]["id"], "template-1")
        self.assertEqual(fake.search_calls, ["split squat"])
        self.assertEqual(fake.create_calls, [])

    def test_unconfirmed_create_never_reaches_hevy(self):
        fake = _FakeHevy()
        hevy_connector.configure(fake)

        response = main.hevy_create_routine({
            "routine": _routine(), "operation_id": "hevy-op-1", "confirmed": False,
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(_json(response)["created"])
        self.assertEqual(fake.template_calls, [])
        self.assertEqual(fake.create_calls, [])

    def test_confirmed_create_verifies_templates_and_writes_exactly_once(self):
        fake = _FakeHevy()
        hevy_connector.configure(fake)

        response = main.hevy_create_routine({
            "routine": _routine(), "operation_id": "hevy-op-2", "confirmed": True,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(_json(response)["created"])
        self.assertEqual(fake.template_calls, ["template-1"])
        self.assertEqual(len(fake.create_calls), 1)
        self.assertEqual(fake.create_calls[0][1], "hevy-op-2")

    def test_ambiguous_create_failure_is_not_retried(self):
        fake = _FakeHevy(fail_create=True)
        hevy_connector.configure(fake)

        response = main.hevy_create_routine({
            "routine": _routine(), "operation_id": "hevy-op-3", "confirmed": True,
        })

        payload = _json(response)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(payload["created"])
        self.assertFalse(payload["retry_safe"])
        self.assertIn("Check Hevy before retrying", payload["instruction"])
        self.assertEqual(len(fake.create_calls), 1)

    def test_coach_ui_requires_explicit_create_button(self):
        html = (main.STATIC_DIR / "index.html").read_text()

        self.assertIn("function addHevyRoutine(routine,operationId,view)", html)
        self.assertIn("Send to Hevy", html)
        self.assertIn("proposed_hevy_routine", html)
        self.assertIn("r.hevy_operation_id", html)
        self.assertIn("operation_id:operation,confirmed:true", html)
        self.assertIn("if(attempted) return", html)

    def test_the_illustrated_card_renders_from_the_view_not_the_api_payload(self):
        html = (main.STATIC_DIR / "index.html").read_text()

        # The card is driven by the server-built view, and an unrecognised
        # movement pattern must fall back to a neutral figure rather than
        # injecting whatever the model supplied.
        self.assertIn("r.hevy_routine_view", html)
        self.assertIn("EXFIG[ex.pattern]||EXFIG.other", html)
        # Every model-derived string on the card goes through esc().
        self.assertIn("${esc(String(ex.title||\"Exercise\"))}", html)
        self.assertIn("${esc(String(ex.summary||\"\"))}", html)


if __name__ == "__main__":
    unittest.main()
