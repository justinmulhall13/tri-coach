from __future__ import annotations

import unittest

from app import web_lookup


class _Citation:
    def __init__(self, title: str, url: str):
        self.title = title
        self.url = url


class _Block:
    type = "text"

    def __init__(self, text: str, citations=()):
        self.text = text
        self.citations = list(citations)


class _Message:
    def __init__(self, content):
        self.content = content


class WebLookupTests(unittest.TestCase):
    def test_explicit_lookup_and_event_switch_enable_search(self) -> None:
        self.assertTrue(web_lookup.should_enable("Look up the official marathon course"))
        self.assertTrue(web_lookup.should_enable("switch to Toronto Waterfront Marathon"))
        self.assertFalse(web_lookup.should_enable("How should I pace today's easy run?"))

    def test_search_is_bounded_and_localized(self) -> None:
        tool = web_lookup.tool_definition()
        self.assertEqual(tool["type"], "web_search_20250305")
        self.assertEqual(tool["max_uses"], 3)
        self.assertEqual(tool["user_location"]["timezone"], "America/Vancouver")

    def test_citation_links_are_not_dropped_from_visible_reply(self) -> None:
        message = _Message([
            _Block("The official date is October 18, 2026.", [
                _Citation("Official race site", "https://example.test/race"),
            ]),
            _Block("", [_Citation("Duplicate", "https://example.test/race")]),
        ])
        rendered = web_lookup.text_with_citations(message)
        self.assertIn("October 18, 2026", rendered)
        self.assertEqual(rendered.count("https://example.test/race"), 1)


if __name__ == "__main__":
    unittest.main()
