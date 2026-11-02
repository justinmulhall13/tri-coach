"""A reply that hits max_tokens is retried with more room, never shown cut off.

Adaptive thinking spends from the same budget as the visible text, so a long
prompt used to come back empty and the coach said "I ran long on that one —
ask me to continue". The athlete should just get the finished answer.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import coach


def _message(stop_reason: str, text: str = "") -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class _Stream:
    def __init__(self, msg: SimpleNamespace) -> None:
        self._msg = msg
        self.text_stream = [b.text for b in msg.content]

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get_final_message(self) -> SimpleNamespace:
        return self._msg


class _Client:
    def __init__(self, *replies: SimpleNamespace) -> None:
        self._replies = list(replies)
        self.budgets: list[int] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs) -> _Stream:
        self.budgets.append(kwargs["max_tokens"])
        return _Stream(self._replies.pop(0))


class TruncationRetryTests(unittest.TestCase):
    def _patches(self, client: _Client):
        return (patch.object(coach, "_anthropic_client", return_value=client),
                patch.object(coach, "_message_kwargs",
                             side_effect=lambda budget, messages, **_: {"max_tokens": budget}))

    def test_chat_retries_with_a_bigger_budget_until_it_finishes(self) -> None:
        client = _Client(_message("max_tokens"), _message("max_tokens", "half a"),
                         _message("end_turn", "The whole answer."))
        a, b = self._patches(client)
        with (a, b,
              patch.object(coach, "_explicit_mode_switch", return_value=None),
              patch.object(coach, "_key_error", return_value=None),
              patch.object(coach, "_prepare_chat", return_value=("2026-11-02", [])),
              patch.object(coach, "_finish_chat",
                           side_effect=lambda u, t, msg, **_: {"reply": msg.content[0].text})):
            events = list(coach.chat_events("Build my strength block"))
        self.assertEqual(client.budgets, [32000, 64000, 128000])
        self.assertEqual(events[-1], {"type": "done", "result": {
            "reply": "The whole answer.", "timing_ms": events[-1]["result"]["timing_ms"]}})
        self.assertNotIn("ran long", str(events))

    def test_brief_path_retries_truncated_replies(self) -> None:
        client = _Client(_message("max_tokens"), _message("end_turn", "Brief."))
        a, b = self._patches(client)
        with a, b:
            msg = coach._stream_reply(16000, [])
        self.assertEqual(client.budgets, [16000, 32000])
        self.assertEqual(msg.content[0].text, "Brief.")

    def test_budgets_stop_at_the_model_ceiling(self) -> None:
        self.assertEqual(coach._budgets(32000), [32000, 64000, 128000])
        self.assertEqual(coach._budgets(200000), [128000])

    def test_no_continue_prompt_is_left_in_the_coach(self) -> None:
        source = coach.__loader__.get_source(coach.__name__)
        self.assertNotIn("ask me to continue", source)


if __name__ == "__main__":
    unittest.main()
