from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import coach


class _TextBlock:
    type = "text"

    def __init__(self, text: str, citations=()):
        self.text = text
        self.citations = list(citations)


class CoachExternalToolTests(unittest.TestCase):
    def test_web_tool_is_bounded_and_enabled_only_for_routed_turn(self):
        messages = [{"role": "user", "content": "hello"}]
        ordinary = coach._message_kwargs(200, messages)
        searched = coach._message_kwargs(200, messages, enable_web=True)

        self.assertNotIn("tools", ordinary)
        self.assertEqual(searched["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(searched["tools"][0]["max_uses"], 3)

    @patch("app.coach.db.add_chat")
    def test_final_reply_preserves_web_citation_and_hevy_proposal(self, add_chat):
        citation = SimpleNamespace(title="Official event site", url="https://example.test/event")
        routine = (
            '```hevy_routine\n'
            '{"title":"Running legs","notes":"No invented loads",'
            '"exercises":[{"exercise_template_id":"DDCC3821","rest_seconds":120,'
            '"sets":[{"type":"normal","rep_range":{"start":8,"end":10}}]}]}'
            '\n```'
        )
        message = SimpleNamespace(
            content=[_TextBlock("TL;DR: Drafted from current data.\n" + routine, [citation])],
            model="test-model",
            stop_reason="end_turn",
        )

        result = coach._finish_chat("Build my running leg day", "2026-08-17", message)

        self.assertIn("https://example.test/event", result["reply"])
        self.assertNotIn("hevy_routine", result["reply"])
        self.assertEqual(result["proposed_hevy_routine"]["title"], "Running legs")
        self.assertTrue(result["hevy_operation_id"].startswith("hevy-"))
        self.assertEqual(add_chat.call_count, 2)

    def test_unproven_working_weight_is_not_an_actionable_hevy_proposal(self):
        raw = (
            '```hevy_routine\n'
            '{"title":"Bad guess","exercises":[{"exercise_template_id":"DDCC3821",'
            '"sets":[{"type":"normal","reps":8,"weight_kg":100}]}]}'
            '\n```'
        )
        self.assertIsNone(coach._extract_hevy_routine(raw))


if __name__ == "__main__":
    unittest.main()
