from __future__ import annotations

import subprocess
import sys
import unittest

from app import hevy_connector, lifting_rules, main


class LazyConnectorTests(unittest.TestCase):
    """The connector must not capture the environment at import time.

    `app.config` is what loads `.env`, so a connector built while this module is
    imported can read an environment that has no HEVY_API_KEY yet. That produced
    a permanently disconnected Hevy whose only symptom was the UI saying so.
    """

    def _status_with_import_order(self, first: str, second: str) -> str:
        code = (
            "import os\n"
            "os.environ.pop('HEVY_API_KEY', None)\n"
            f"from app import {first}\n"
            "os.environ['HEVY_API_KEY'] = 'test-key-set-after-import'\n"
            f"from app import {second}\n"
            "from app import hevy_connector\n"
            "print('TRANSPORT', hevy_connector.connector().__class__.__name__)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=120)
        return result.stdout + result.stderr

    def test_a_key_arriving_after_import_is_still_picked_up(self) -> None:
        out = self._status_with_import_order("hevy_connector", "config")
        self.assertIn("TRANSPORT HevyAPIConnector", out, out[-400:])

    def test_an_explicitly_configured_connector_is_never_replaced(self) -> None:
        previous = hevy_connector.connector()
        self.addCleanup(hevy_connector.configure, previous)
        hevy_connector.reset()
        # reset() is explicit, so the environment must not override it — this is
        # what keeps the suite from making live API calls.
        self.assertIsInstance(hevy_connector.connector(),
                              hevy_connector.UnavailableHevyConnector)
        self.assertFalse(hevy_connector.status()["connected"])


class RulesSingleSourceTests(unittest.TestCase):
    """The tab renders the rules the server enforces, not a copy."""

    def test_the_removed_triceps_rule_appears_nowhere(self) -> None:
        html = (main.STATIC_DIR / "index.html").read_text()
        self.assertNotIn("No triceps isolation", html)
        self.assertNotIn("triceps isolation", " ".join(lifting_rules.summary([])["rules"]))

    def test_the_tab_fetches_rules_rather_than_hardcoding_them(self) -> None:
        html = (main.STATIC_DIR / "index.html").read_text()
        self.assertIn('j("/api/lifting/rules")', html)

    def test_the_rules_endpoint_serves_the_enforced_rules(self) -> None:
        from fastapi.testclient import TestClient
        rules = TestClient(main.app).get("/api/lifting/rules").json()["rules"]
        self.assertTrue(any("1 pressing movement" in r for r in rules))
        self.assertFalse(any("triceps" in r.lower() for r in rules), rules)


if __name__ == "__main__":
    unittest.main()


class FreshBootVerificationTests(unittest.TestCase):
    """time.monotonic() counts from boot, so a 0.0 'never verified' sentinel is
    indistinguishable from 'verified moments ago' on a machine that cold-starts.
    Fly machines auto-stop, so this reported a valid Hevy key as unverified for
    the first five minutes after every boot."""

    class _Recorder(hevy_connector.HevyAPIConnector):
        def __init__(self) -> None:
            super().__init__("test-key")
            self.calls: list[str] = []

        def _request(self, method, path, *, params=None, body=None, idempotency_key=None):
            self.calls.append(path)
            return {"data": {"id": "u1", "name": "athlete"}}

    def test_verification_runs_immediately_after_boot(self) -> None:
        import time
        from unittest.mock import patch
        client = self._Recorder()
        # 33 seconds of uptime: what a freshly booted Fly machine reports.
        with patch.object(time, "monotonic", return_value=33.0):
            state = client.status()
        self.assertEqual(client.calls, ["user/info"])
        self.assertTrue(state["connected"], state)

    def test_a_verified_credential_is_not_rechecked_every_call(self) -> None:
        import time
        from unittest.mock import patch
        client = self._Recorder()
        with patch.object(time, "monotonic", return_value=33.0):
            client.status()
            client.status()
        self.assertEqual(client.calls, ["user/info"])

    def test_verification_is_retried_once_the_ttl_lapses(self) -> None:
        import time
        from unittest.mock import patch
        client = self._Recorder()
        with patch.object(time, "monotonic", return_value=33.0):
            client.status()
        with patch.object(time, "monotonic",
                          return_value=33.0 + client.VERIFY_TTL_S + 1):
            client.status()
        self.assertEqual(client.calls, ["user/info", "user/info"])

    def test_a_transport_failure_reports_rather_than_raising(self) -> None:
        class Broken(hevy_connector.HevyAPIConnector):
            def _request(self, *a, **k):
                raise TimeoutError("connection timed out")

        state = Broken("test-key").status()
        self.assertFalse(state["connected"])
        self.assertIn("TimeoutError", state["reason"])
