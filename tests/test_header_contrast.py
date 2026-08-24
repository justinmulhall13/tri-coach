from __future__ import annotations

import unittest

from app import main


class HeaderContrastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_brand_uses_a_legible_typographic_mark(self) -> None:
        self.assertIn('<span class="emblem" aria-hidden="true">TC</span>', self.html)
        self.assertIn('<span class="logo">TRI COACH</span>', self.html)
        self.assertNotIn('M8 30c6-15 12-20 24-20', self.html)

    def test_dark_day_chip_never_keeps_the_light_background(self) -> None:
        self.assertIn(
            '.todaychip{background:#20221e;color:#f1f0ea;',
            self.html,
        )

    def test_dark_microphone_has_a_dark_surface_and_light_icon(self) -> None:
        self.assertIn(
            '.coach-mic{background:#242620;color:#f1f0ea;',
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
