from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import (activity_detail, athlete_guide, coach, coaching_contract, db,
                 fueling_reference, nutrition, plan)


class PersistedEventProfileSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "profiles.db"
        db._local = threading.local()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    @staticmethod
    def _marathon_block() -> str:
        return """```event_profile
{"event_name":"Marathon","event_date":"2026-10-18","distances":{"run_km":42.195},"goal":{"target":"finish"},"mode":"MARATHON","provenance":{"event_name":"confirmed_by_user","event_date":"confirmed_by_user","distances":"confirmed_by_user","goal":"confirmed_by_user","mode":"confirmed_by_user"}}
```"""

    def test_confirmed_profile_survives_long_chat_and_drives_every_new_turn(self) -> None:
        self.assertEqual(coaching_contract.current_mode(), "TRIATHLON")
        original_profile = coaching_contract.event_profile_id()
        db.add_chat("user", "old event private context")

        staged = coach._explicit_mode_switch("switch to marathon")
        self.assertIn("staged, not active", staged["reply"])
        self.assertEqual(coaching_contract.current_mode(), "TRIATHLON")

        details = coach._explicit_mode_switch(self._marathon_block())
        self.assertIn("staged, not active", details["reply"])
        confirmed = coach._explicit_mode_switch("confirm switch")
        self.assertNotIn("error", confirmed)
        self.assertIn("MARATHON is now active", confirmed["reply"])

        # Simulate a process/app restart before any later conversation turn.
        db._local.conn.close()
        db._local = threading.local()
        self.assertEqual(coaching_contract.current_mode(), "MARATHON")

        for i in range(20):
            db.add_chat("user", f"unrelated message {i}")
        status = coach._explicit_mode_switch("what mode am I in?")
        self.assertIn("MARATHON", status["reply"])
        self.assertIn("2026-10-18 (confirmed_by_user)", status["reply"])
        self.assertNotIn("Needed", status["reply"])

        active = coaching_contract.event_context()
        self.assertEqual(active["mode"], "MARATHON")
        self.assertEqual(active["date"], "2026-10-18")
        self.assertEqual(active["field_provenance"]["event_date"], "confirmed_by_user")
        self.assertNotEqual(active["id"], original_profile)
        self.assertFalse(any(row["content"] == "old event private context" for row in db.get_chat(200)))

        phase = coaching_contract.race_phase(datetime.date(2026, 8, 16))
        self.assertEqual(phase["days_remaining"], 63)
        self.assertEqual(phase["weeks_remaining"], 9.0)

        system = coach._message_kwargs(100, [
            {"role": "user", "content": "what is my mode"},
        ])["system"][0]["text"]
        self.assertIn("CURRENT MODE: MARATHON", system)
        self.assertIn('"event_date": "2026-10-18"', system)

        live = {
            "readiness": {}, "load": {"activities": [], "by_sport": {}},
            "training_load": {}, "fitness": {}, "pmc": {}, "zones": {},
            "weight": {"kg": 86.0, "as_of": "2026-08-16", "source": "self-reported"},
        }
        now = datetime.datetime(2026, 8, 16, 12, tzinfo=datetime.timezone.utc)
        with (
            patch.object(coach, "_live_context", return_value=live),
            patch.object(coach.config, "local_now", return_value=now),
            patch.object(coach.rings, "t100_readiness") as t100_readiness,
            patch.object(coach.suggest, "todays_suggestion", return_value={}),
            patch("app.baselines.get_baselines", return_value={}),
            patch("app.insights.get_insights", return_value={}),
        ):
            context = json.loads(coach._context_block("what is my mode?"))
        self.assertEqual(context["coaching_contract"]["current_mode"], "MARATHON")
        self.assertEqual(context["coaching_contract"]["event_profile"]["date"], "2026-10-18")
        self.assertEqual(context["race"]["days_remaining"], 63)
        self.assertEqual(context["race"]["weeks_remaining"], 9.0)
        self.assertNotIn("vancouver_athlete_guide", context)
        t100_readiness.assert_not_called()

    def test_profile_block_is_saved_before_finish_chat_persists_reply(self) -> None:
        coach._explicit_mode_switch("switch to marathon")
        raw = ("I have staged the supplied facts.\n" + self._marathon_block())
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=raw)],
            model="test-model", stop_reason="end_turn",
        )
        with patch.object(coach.db, "add_chat") as add_chat:
            result = coach._finish_chat("Here are the confirmed details", "2026-08-17", msg)
        self.assertEqual(db.get_pending_event_profile()["event_date"], "2026-10-18")
        self.assertIn("confirm switch", result["reply"].lower())
        self.assertEqual(add_chat.call_count, 2)

    def test_repeated_exact_switch_activates_a_complete_matching_pending_profile(self) -> None:
        coach._explicit_mode_switch("switch to marathon")
        coach._explicit_mode_switch(self._marathon_block())
        result = coach._explicit_mode_switch("switch to marathon mode")
        self.assertIn("MARATHON is now active", result["reply"])
        self.assertEqual(coaching_contract.current_mode(), "MARATHON")
        self.assertEqual(coaching_contract.event_context()["date"], "2026-10-18")
        self.assertIsNone(db.get_pending_event_profile())

    def test_failed_confirm_surfaces_error_and_keeps_prior_active_profile(self) -> None:
        coach._explicit_mode_switch("switch to marathon")
        coach._explicit_mode_switch(self._marathon_block())
        before = coaching_contract.event_context()
        with patch.object(db, "_upsert_event_profile", side_effect=RuntimeError("disk full")):
            result = coach._explicit_mode_switch("confirm switch")
        self.assertIn("error", result)
        self.assertIn("disk full", result["error"])
        self.assertEqual(coaching_contract.event_context()["id"], before["id"])
        self.assertIsNotNone(db.get_pending_event_profile())

    def test_model_prose_cannot_claim_an_uncommitted_switch(self) -> None:
        claims = (
            "TL;DR: MARATHON is now active.",
            "TL;DR: I switched you to Marathon.",
            "TL;DR: You're officially in Marathon mode.",
            "TL;DR: The switch is completed.",
        )
        for claim in claims:
            with self.subTest(claim=claim):
                msg = SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=claim)],
                    model="test-model", stop_reason="end_turn",
                )
                result = coach._finish_chat("great", "2026-08-17", msg)
                self.assertIn("not active yet", result["reply"])
        self.assertEqual(coaching_contract.current_mode(), "TRIATHLON")

    def test_persisted_profile_drives_specialist_context_without_t100_leaks(self) -> None:
        coach._explicit_mode_switch("switch to marathon")
        coach._explicit_mode_switch(self._marathon_block())
        coach._explicit_mode_switch("confirm switch")

        fuel = fueling_reference.context()
        placement = next(rule for rule in fuel["fuel_audit_contract"] if "Front-load" in rule)
        self.assertIn("no stated bike leg", placement)
        self.assertFalse(any("T100" in source for source in fuel["sources"]))
        self.assertIsNone(athlete_guide.context_for("show the bike aid stations"))
        self.assertIn("marathon_2026_10_18", activity_detail._analysis_cache_key(42))
        self.assertIn("marathon-2026-10-18", plan.seed()["error"])
        self.assertEqual(plan._hr("race"), "HR unknown (event race target required)")

        chef_system = nutrition._system_prompt()
        self.assertIn("CURRENT MODE: MARATHON", chef_system)
        self.assertIn('"event_date": "2026-10-18"', chef_system)
        self.assertNotIn("T100 Vancouver", chef_system)

    def test_web_found_fields_remain_unknown_until_the_separate_confirmation(self) -> None:
        coach._explicit_mode_switch("switch to marathon")
        message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=(
                "Official details found.\n" + self._marathon_block()
            ))],
            model="test-model", stop_reason="end_turn",
        )

        coach._finish_chat(
            "Look up the official marathon details", "2026-08-17", message,
            web_enabled=True,
        )
        pending = db.get_pending_event_profile()
        self.assertEqual(pending["provenance"]["event_name"], "confirmed_by_user")
        self.assertEqual(pending["provenance"]["mode"], "confirmed_by_user")
        self.assertEqual(pending["provenance"]["event_date"], "unknown")
        self.assertEqual(pending["provenance"]["distances"], "unknown")
        self.assertEqual(pending["provenance"]["goal"], "unknown")

        coach._explicit_mode_switch("confirm switch")
        active = db.get_active_event_profile_record()
        self.assertTrue(all(
            source == "confirmed_by_user" for source in active["provenance"].values()
        ))

    def test_database_reopen_does_not_overwrite_a_confirmed_existing_profile(self) -> None:
        revised = coaching_contract.default_event_profile_record()
        revised["goal"] = {"target": "revised confirmed goal"}
        db.stage_event_profile(revised)
        db.activate_pending_event_profile()

        db._local.conn.close()
        db._local = threading.local()
        reopened = db.get_active_event_profile_record()
        self.assertNotEqual(reopened["id"], "t100-vancouver-2026")
        self.assertEqual(reopened["goal"], {"target": "revised confirmed goal"})

    def test_server_owned_profile_id_prevents_reserved_and_distance_collisions(self) -> None:
        base = {
            "event_name": "Same Day Race", "event_date": "2026-10-18",
            "goal": {"target": "finish"}, "mode": "RUNNING",
            "provenance": {field: "confirmed_by_user"
                           for field in coaching_contract.PROFILE_FIELDS},
        }
        full = coaching_contract.prepare_event_profile_for_activation({
            **base, "id": "t100-vancouver-2026", "distances": {"run_km": 42.195},
        })
        half = coaching_contract.prepare_event_profile_for_activation({
            **base, "id": full["id"], "distances": {"run_km": 21.0975},
        })

        self.assertNotEqual(full["id"], "t100-vancouver-2026")
        self.assertNotEqual(full["id"], half["id"])


if __name__ == "__main__":
    unittest.main()
