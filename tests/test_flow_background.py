"""The moving background and the glass that sits on it.

These pin the things that are easy to break by accident: the field stays
greyscale, text stays readable over its lightest and darkest point, there is
always a way to stop the motion, and every route to "no glass" lands on the
same opaque set.
"""
from __future__ import annotations

import re
import unittest

from app import main


def _rgb(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if not m:
        raise ValueError(value)
    parts = [float(p) for p in m.group(1).split(",")]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def _over(top: tuple[float, float, float, float], under: tuple[float, float, float]) -> tuple[float, float, float]:
    a = top[3]
    return tuple(top[i] * a + under[i] * (1 - a) for i in range(3))  # type: ignore[return-value]


def _lum(c: tuple[float, ...]) -> float:
    def lin(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def _contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class FlowBackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()
        style = self.html[self.html.index("<style>"):self.html.index("</style>")]
        dark_at = style.index(":root.dark{")
        self.light = self._tokens(style[style.index(":root{"):dark_at])
        self.dark = {**self.light, **self._tokens(style[dark_at:style.index(":root.solid{")])}
        self.style = style

    @staticmethod
    def _tokens(css: str) -> dict[str, str]:
        return {name: value.strip() for name, value in re.findall(r"(--[a-z0-9-]+):([^;}]+)", css)}

    # -- the field ---------------------------------------------------------

    def test_the_canvas_is_first_in_the_body_and_hidden_from_readers(self) -> None:
        body = self.html[self.html.index("<body>") + len("<body>"):]
        self.assertTrue(body.lstrip().startswith('<canvas id="flow" aria-hidden="true"></canvas>'))

    def test_the_canvas_is_never_faded_or_transformed_in_css(self) -> None:
        # iPhone Safari drops frames or goes black when a WebGL canvas is
        # transformed or faded by CSS. All movement happens in the shader.
        rule = re.search(r"#flow\{([^}]*)\}", self.style)
        self.assertIsNotNone(rule)
        for banned in ("opacity", "transform", "animation", "transition", "filter"):
            self.assertNotIn(banned, rule.group(1))
        self.assertIn("pointer-events:none", rule.group(1))
        self.assertIn("z-index:-1", rule.group(1))

    def test_the_field_is_greyscale_in_both_themes(self) -> None:
        for tokens in (self.light, self.dark):
            for name in ("--flow-lo", "--flow-hi"):
                r, g, b, _ = _rgb(tokens[name])
                self.assertLessEqual(max(r, g, b) - min(r, g, b), 8, f"{name} {tokens[name]}")

    def test_the_engine_stops_when_nobody_is_looking(self) -> None:
        engine = self.html[self.html.index("const Flow=(()=>{"):]
        engine = engine[:engine.index("\n})();") + 6]
        for needed in ('"visibilitychange"', '"pagehide"', '"webglcontextlost"',
                       '"webglcontextrestored"', "(prefers-reduced-motion: reduce)",
                       'powerPreference:"low-power"'):
            self.assertIn(needed, engine)
        # Nothing in the shader or the loop is random: a still frame is repeatable.
        self.assertNotIn("Math.random", engine)

    def test_auto_style_follows_the_session(self) -> None:
        self.assertIn("Flow.sport(flowSportFor(_woSessions));", self.html)
        self.assertIn("function flowSportFor(sessions){", self.html)

    # -- the control (WCAG 2.2.2: moving content needs a way to stop it) ----

    def test_the_background_sheet_can_stop_and_hide_the_motion(self) -> None:
        self.assertIn('id="flow-btn"', self.html[self.html.index("<header>"):self.html.index("</header>")])
        sheet = self.html[self.html.index('id="flow-overlay"'):]
        sheet = sheet[:sheet.index("</div>\n</div>\n") if "</div>\n</div>\n" in sheet else 3000]
        for value in ("auto", "water", "wind", "cloud", "plain"):
            self.assertIn(f'data-v="{value}"', sheet)
        self.assertIn('id="bg-motion"', sheet)
        self.assertIn('data-v="off"', sheet)
        self.assertIn('id="bg-glass"', sheet)
        self.assertIn('data-v="solid"', sheet)

    def test_the_sheet_copy_is_plain(self) -> None:
        sheet = self.html[self.html.index('id="flow-overlay"'):][:2400]
        self.assertNotIn("—", sheet)

    # -- glass off -----------------------------------------------------------

    def test_every_way_of_turning_glass_off_lands_on_the_opaque_set(self) -> None:
        solid = self.style[self.style.index(":root.solid{"):][:520]
        contrast = self.style[self.style.index("@media(prefers-contrast:more){"):][:620]
        reduced = self.style[self.style.index("@media(prefers-reduced-transparency:reduce){"):][:520]
        for block in (solid, contrast, reduced):
            for needed in ("--surface:var(--solid)", "--surface2:var(--solid2)", "--surface3:var(--solid3)",
                           "--glass:var(--glass-solid)", "--blur:none", "--blur-lg:none", "--blur-edge:none",
                           "--hero-bg:var(--hero-solid)"):
                self.assertIn(needed, block)
        self.assertIn("#flow{display:none;}", contrast)

    def test_the_glass_choice_is_applied_before_first_paint(self) -> None:
        head = self.html[:self.html.index("<style>")]
        self.assertIn('localStorage.getItem("glass")==="solid"', head)

    def test_sheets_are_solid(self) -> None:
        rule = re.search(r"\n\s*\.sheet\{([^}]*)\}", self.style)
        self.assertIsNotNone(rule)
        for needed in ("--surface:var(--solid)", "--surface2:var(--solid2)", "--surface3:var(--solid3)"):
            self.assertIn(needed, rule.group(1))

    def test_content_cards_are_tinted_never_blurred(self) -> None:
        rule = re.search(r"\n\s*\.card\{([^}]*)\}", self.style)
        self.assertIsNotNone(rule)
        self.assertIn("background:var(--surface)", rule.group(1))
        self.assertNotIn("backdrop-filter", rule.group(1))

    def test_the_coach_view_does_not_fade_an_ancestor_of_its_glass(self) -> None:
        self.assertNotIn("main.view-in{animation", self.style)
        self.assertIn("main.view-in:not(#main-coach)", self.style)

    # -- theme: light unless the athlete picks dark ----------------------------

    def test_light_is_the_default_whatever_the_system_says(self) -> None:
        self.assertNotIn("prefers-color-scheme", self.html)
        self.assertIn(":root.dark{color-scheme:dark;", self.style)

    def test_the_theme_choice_is_applied_before_first_paint(self) -> None:
        head = self.html[:self.html.index("<style>")]
        self.assertIn('localStorage.getItem("theme")==="dark"', head)
        self.assertIn('id="theme-meta"', head)

    def test_settings_offers_light_and_dark(self) -> None:
        sheet = self.html[self.html.index('id="flow-overlay"'):][:2400]
        self.assertIn('id="flow-title">Settings<', sheet)
        seg = sheet[sheet.index('id="app-theme"'):][:260]
        self.assertIn('data-v="light"', seg)
        self.assertIn('data-v="dark"', seg)

    def test_the_no_glass_routes_also_win_in_dark(self) -> None:
        # :root.dark outranks a bare :root, so each opaque block names it too.
        for query in ("@media(prefers-contrast:more){", "@media(prefers-reduced-transparency:reduce){"):
            block = self.style[self.style.index(query):][:120]
            self.assertIn(":root,:root.dark{", block)

    # -- contrast, computed from the tokens ---------------------------------

    def _backgrounds(self, t: dict[str, str]) -> dict[str, tuple[float, float, float]]:
        out: dict[str, tuple[float, float, float]] = {}
        for end in ("--flow-lo", "--flow-hi"):
            field = _rgb(t[end])[:3]
            pane = _over(_rgb(t["--surface"]), field)
            out[f"field {end}"] = field
            out[f"pane over {end}"] = pane
            out[f"wash on pane over {end}"] = _over(_rgb(t["--surface2"]), pane)
        return out

    def test_muted_text_is_readable_on_a_pane_over_any_part_of_the_field(self) -> None:
        for theme, t in (("light", self.light), ("dark", self.dark)):
            muted = _rgb(t["--muted"])[:3]
            for where, bg in self._backgrounds(t).items():
                if where.startswith("field"):
                    continue
                ratio = _contrast(muted, bg)
                self.assertGreaterEqual(ratio, 4.5, f"{theme} muted on {where}: {ratio:.2f}")

    def test_labels_on_the_bare_field_use_a_colour_that_survives_it(self) -> None:
        for theme, t in (("light", self.light), ("dark", self.dark)):
            for name in ("--ink", "--ink2"):
                ink = _rgb(t[name])[:3]
                for end in ("--flow-lo", "--flow-hi"):
                    ratio = _contrast(ink, _rgb(t[end])[:3])
                    self.assertGreaterEqual(ratio, 4.5, f"{theme} {name} on {end}: {ratio:.2f}")

    def test_views_with_text_on_the_bare_field_do_not_use_the_quiet_grey(self) -> None:
        self.assertIn("#main-calendar{--muted:var(--ink2);}", self.style)
        self.assertRegex(self.style, r"\.coach-empty\{[^}]*color:var\(--ink2\)")
        self.assertRegex(self.style, r"\.coach-thinking-status\{color:var\(--ink2\)")

    def test_solid_panes_keep_muted_text_readable(self) -> None:
        for theme, t in (("light", self.light), ("dark", self.dark)):
            muted = _rgb(t["--muted"])[:3]
            for name in ("--solid", "--solid2", "--solid3", "--glass-solid"):
                ratio = _contrast(muted, _rgb(t[name])[:3])
                self.assertGreaterEqual(ratio, 4.5, f"{theme} muted on {name}: {ratio:.2f}")


if __name__ == "__main__":
    unittest.main()
