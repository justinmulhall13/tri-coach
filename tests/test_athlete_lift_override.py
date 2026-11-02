"""The athlete's explicit lifting request beats the coach's shoulder defaults.

The coach refused "explosive landmine press + incline bench" as two presses
and kept refusing after "just run the one I told you". The shoulder rules
shape sessions the coach designs; they never veto what the athlete asks for.
"""
from __future__ import annotations

import unittest

from app import coach


class AthleteOverrideTests(unittest.TestCase):
    def test_shoulder_rules_are_defaults_not_a_veto(self) -> None:
        self.assertNotIn("never negotiable", coach._SYSTEM)
        self.assertIn("the defaults for sessions YOU design", coach._SYSTEM)

    def test_an_explicit_request_is_built_as_asked(self) -> None:
        self.assertIn("build\n  EXACTLY what he asked", coach._SYSTEM)
        self.assertIn("just run what I told you", coach._SYSTEM)


if __name__ == "__main__":
    unittest.main()
