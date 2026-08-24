from __future__ import annotations

import pathlib
import unittest

from app import coach, nutrition


class CoachStyleTests(unittest.TestCase):
    def test_prompts_request_conversation_not_a_generated_report(self) -> None:
        self.assertIn("normal conversation", coach._SYSTEM)
        self.assertIn("Do not use TL;DR", coach._SYSTEM)
        self.assertNotIn('Line 1 is exactly "TL;DR', coach._SYSTEM)
        self.assertIn("normal conversation", nutrition._SYSTEM)

    def test_ui_never_hides_coach_rows_behind_more_details(self) -> None:
        html = (pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
        self.assertNotIn('summary.textContent="Detailed breakdown"', html)
        self.assertNotIn('details.className="coach-details"', html)
        self.assertIn('class="cp"', html)
        self.assertIn('class="coach-list"', html)


if __name__ == "__main__":
    unittest.main()
