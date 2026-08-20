from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import hevy_connector


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


class HevyAPIConnectorTests(unittest.TestCase):
    def test_official_api_key_header_and_bounded_pagination(self) -> None:
        connector = hevy_connector.HevyAPIConnector("secret", base_url="https://example.test/v1")
        with patch.object(hevy_connector, "urlopen", return_value=_Response({"workouts": []})) as open_:
            result = connector.get_workouts(page=0, page_size=99)
        self.assertEqual(result, {"workouts": []})
        request = open_.call_args.args[0]
        self.assertEqual(request.get_header("Api-key"), "secret")
        self.assertIn("page=1", request.full_url)
        self.assertIn("pageSize=10", request.full_url)

    def test_create_is_single_attempt_and_suppresses_same_process_duplicate(self) -> None:
        connector = hevy_connector.HevyAPIConnector("secret", base_url="https://example.test/v1")
        payload = {"id": "routine-1", "title": "Run Legs", "exercises": []}
        with patch.object(hevy_connector, "urlopen", return_value=_Response(payload)) as open_:
            first = connector.create_routine({"title": "Run Legs"}, idempotency_key="draft-7")
            second = connector.create_routine({"title": "Run Legs"}, idempotency_key="draft-7")
        self.assertEqual(first, second)
        self.assertEqual(open_.call_count, 1)
        request = open_.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"routine": {"title": "Run Legs"}})

    def test_missing_key_remains_truthfully_disconnected(self) -> None:
        state = hevy_connector.UnavailableHevyConnector().status()
        self.assertFalse(state["connected"])
        self.assertEqual(state["transport"], "unconfigured")

    def test_lifting_context_includes_exact_recent_hevy_sets_only_when_connected(self) -> None:
        class Fake:
            def status(self):
                return {"connected": True, "transport": "test"}

            def get_workouts(self, *, page, page_size):
                return {"workouts": [{"id": "w1", "title": "Upper", "exercises": [
                    {"title": "Row", "exercise_template_id": "row-1", "sets": [
                        {"type": "normal", "reps": 8, "weight_kg": 60},
                    ]},
                ]}]}

            def get_workout(self, workout_id):
                return {"id": workout_id, "title": "Upper", "exercises": [
                    {"title": "Row", "exercise_template_id": "row-1", "sets": [
                        {"type": "normal", "reps": 8, "weight_kg": 60},
                    ]},
                ]}

        old = hevy_connector.connector()
        try:
            hevy_connector.configure(Fake())
            hevy_connector._CONTEXT_CACHE.update({"at": 0.0, "data": None})
            context = hevy_connector.context_for("Build me an upper body day")
        finally:
            hevy_connector.configure(old)
            hevy_connector._CONTEXT_CACHE.update({"at": 0.0, "data": None})
        self.assertEqual(context["recent_workouts"][0]["exercises"][0]["sets"][0]["weight_kg"], 60)
        self.assertEqual(context["provenance"], "self-reported via Hevy when connected")


if __name__ == "__main__":
    unittest.main()
