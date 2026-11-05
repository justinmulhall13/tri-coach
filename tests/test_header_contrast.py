from __future__ import annotations

import unittest

from app import main


class HeaderContrastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_brand_is_a_plain_wordmark_with_the_date(self) -> None:
        header = self.html[self.html.index("<header>"):self.html.index("</header>")]
        self.assertIn('<span class="logo">Tri Coach</span>', header)
        self.assertIn('id="hdate"', header)
        self.assertNotIn('M8 30c6-15 12-20 24-20', self.html)

    def test_the_header_has_no_duplicate_countdown_or_day_chip(self) -> None:
        # The race card already shows days to go; the header repeated it.
        for gone in ('id="todaychip"', 'id="cd-n"', 'id="phase"'):
            self.assertNotIn(gone, self.html)

    def test_dark_microphone_flips_with_the_theme(self) -> None:
        self.assertIn(".coach-mic{background:transparent;border:0;color:var(--muted);}", self.html)
        self.assertIn(".coach-mic.listening{background:var(--chip);color:var(--chip-ink);", self.html)


if __name__ == "__main__":
    unittest.main()
