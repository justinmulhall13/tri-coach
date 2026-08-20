from __future__ import annotations

import subprocess
import sys
import unittest


class ImportSideEffectTests(unittest.TestCase):
    """Importing the app must not touch Google Calendar.

    The startup reconcile used to run at import, so every test run — and any
    script that imported `app.main` — reconciled the real calendar from a
    developer machine. With Fly reconciling the same plan, that is how
    duplicate and conflicting events appear.
    """

    def test_importing_main_does_not_reconcile_the_calendar(self) -> None:
        code = (
            "import sys, types\n"
            "calls = []\n"
            "import app.calendar_sync as cs\n"
            "cs.reconcile = lambda *a, **k: calls.append(1)\n"
            "import app.main\n"
            "import time; time.sleep(0.3)\n"  # a thread would have fired by now
            "print('CALLS', len(calls))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=120)
        self.assertIn("CALLS 0", result.stdout,
                      f"stdout={result.stdout} stderr={result.stderr[-400:]}")

    def test_the_reconcile_is_still_wired_to_asgi_startup(self) -> None:
        # Moving it out of import must not mean losing it entirely.
        from app import main
        handlers = getattr(main.app.router, "on_startup", [])
        self.assertTrue(any(getattr(h, "__name__", "") == "_startup_sync"
                            for h in handlers), handlers)


if __name__ == "__main__":
    unittest.main()
