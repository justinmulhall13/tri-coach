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


class _SearchResult:
    def __init__(self, title: str, url: str):
        self.title = title
        self.url = url


class _SearchBlock:
    type = "web_search_tool_result"

    def __init__(self, content):
        self.content = content


class WebLookupTests(unittest.TestCase):
    def test_explicit_lookup_and_event_switch_enable_search(self) -> None:
        self.assertTrue(web_lookup.should_enable("Look up the official marathon course"))
        self.assertTrue(web_lookup.should_enable("switch to Toronto Waterfront Marathon"))
        self.assertTrue(web_lookup.should_enable(
            "change my goal to the Chicago marathon and find the date"))
        self.assertTrue(web_lookup.should_enable(
            "start training for the Victoria half marathon"))
        self.assertTrue(web_lookup.should_enable("make Ironman Canada my race"))
        self.assertFalse(web_lookup.should_enable("How should I pace today's easy run?"))
        self.assertTrue(web_lookup.should_enable("Suggest a high protein dinner recipe"))
        self.assertTrue(web_lookup.should_enable("Is this shoe worth buying?"))

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
        self.assertEqual(web_lookup.citation_sources(message), [{
            "title": "Official race site", "url": "https://example.test/race",
        }])

    def test_raw_search_results_survive_a_response_that_ends_before_prose(self) -> None:
        message = _Message([_SearchBlock([
            _SearchResult("Recipe", "https://example.test/recipe"),
        ])])
        self.assertEqual(web_lookup.citation_sources(message), [{
            "title": "Recipe", "url": "https://example.test/recipe",
        }])


if __name__ == "__main__":
    unittest.main()
