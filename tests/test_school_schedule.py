"""School runs 08:00-12:40 every day, and replaced the old 9-4 workday.

Default placement must never drop a workout into school, on any day of the
week, and the calendar band must sit where the scheduler keeps workouts out.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import unittest

from app import schedule_time

MONDAY = datetime.date(2026, 9, 14)


def _day(offset: int, minutes: int) -> dict:
    return {"date": (MONDAY + datetime.timedelta(days=offset)).isoformat(),
            "discipline": "run", "duration_min": minutes}


def _clock(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


class SchoolPlacementTests(unittest.TestCase):
    def test_no_workout_is_ever_placed_inside_school_on_any_day(self) -> None:
        for offset in range(7):
            for minutes in (25, 45, 60, 90, 120, 173, 240):
                with self.subTest(day=offset, minutes=minutes):
                    start = schedule_time.default_start(_day(offset, minutes))
                    self.assertIsNotNone(start)
                    begin = _clock(start)
                    end = begin + minutes
                    overlaps = (begin < schedule_time.SCHOOL_END
                                and end > schedule_time.SCHOOL_START)
                    self.assertFalse(overlaps, f"{start} for {minutes} min")

    def test_a_session_that_fits_goes_before_school_with_a_buffer(self) -> None:
        self.assertEqual(schedule_time.default_start(_day(0, 60)), "06:45")

    def test_weekends_follow_the_same_school_day(self) -> None:
        # School is every day, so Saturday is placed exactly like Monday.
        self.assertEqual(schedule_time.default_start(_day(5, 60)),
                         schedule_time.default_start(_day(0, 60)))

    def test_a_session_too_long_for_the_morning_goes_straight_after_school(self) -> None:
        # A 173-minute long run would have to start before 05:30.
        self.assertEqual(schedule_time.default_start(_day(6, 173)), "13:00")

    def test_the_old_workday_is_gone(self) -> None:
        source = pathlib.Path(schedule_time.__file__).read_text()
        self.assertNotIn("WORK_START", source)
        self.assertNotIn("9-4", source)


class CalendarBandTests(unittest.TestCase):
    HTML = (pathlib.Path(__file__).parents[1] / "static" / "index.html").read_text()

    def test_the_calendar_draws_a_school_band_not_a_work_band(self) -> None:
        self.assertIn('class="schoolband"', self.HTML)
        self.assertNotIn("workband", self.HTML)

    def test_the_band_matches_the_scheduler(self) -> None:
        match = re.search(r"SCHOOL_START_MIN=([\d*+ ]+), SCHOOL_END_MIN=([\d*+ ]+);", self.HTML)
        self.assertIsNotNone(match)
        self.assertEqual(eval(match.group(1)), schedule_time.SCHOOL_START)  # noqa: S307 - literal arithmetic
        self.assertEqual(eval(match.group(2)), schedule_time.SCHOOL_END)    # noqa: S307


class ThemeContrastTests(unittest.TestCase):
    """--ink is the TEXT colour and turns near-white in dark mode. Used as a
    background it made the coach avatar, thinking orb and module icons white
    glyphs on a white disc."""

    HTML = CalendarBandTests.HTML

    def test_no_rule_uses_the_text_colour_as_a_background(self) -> None:
        self.assertNotRegex(self.HTML, r"background:var\(--ink\)")

    def test_solid_chips_flip_with_the_theme(self) -> None:
        dark = self.HTML[self.HTML.index("@media(prefers-color-scheme:dark)"):]
        self.assertIn("--chip:", dark)
        self.assertIn("--chip-ink:", dark)
        for selector in (".coach-avatar", ".coach-thinking-orb", ".mod-ico"):
            self.assertRegex(
                self.HTML,
                re.escape(selector) + r"\{background:var\(--chip\);color:var\(--chip-ink\)",
                selector)


if __name__ == "__main__":
    unittest.main()
