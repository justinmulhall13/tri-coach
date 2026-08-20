from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from app import db, main


class LiftingSplitRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "split.db"
        db._local = threading.local()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    def test_the_default_split_is_served_before_anything_is_saved(self) -> None:
        payload = self.client.get("/api/lifting/split").json()
        self.assertEqual(payload["source"], "default")
        self.assertEqual(payload["sessions_per_week"], 4)
        self.assertEqual(len(payload["days"]), 4)
        self.assertTrue(payload["ok"])

    def test_each_day_carries_its_reasoning_for_the_tab(self) -> None:
        days = self.client.get("/api/lifting/split").json()["days"]
        for day in days:
            self.assertTrue(day["why"], day["name"])
            self.assertTrue(all(e["why"] for e in day["exercises"]), day["name"])

    def test_an_edited_split_persists_and_is_marked_as_edited(self) -> None:
        edited = [{"slot": "upper_1", "name": "My Upper", "focus": "test",
                   "exercises": [{"title": "Dumbbell Row"}]}]
        saved = self.client.put("/api/lifting/split", json={"days": edited}).json()
        self.assertEqual(saved["source"], "edited")
        again = self.client.get("/api/lifting/split").json()
        self.assertEqual(again["days"][0]["name"], "My Upper")

    def test_an_edit_that_breaks_a_rule_is_saved_but_reported(self) -> None:
        # Silently rejecting an edit is as bad as silently accepting one.
        bad = [{"slot": "upper_1", "name": "Two presses", "focus": "test",
                "exercises": [{"title": "Bench Press"}, {"title": "Overhead Press"}]}]
        saved = self.client.put("/api/lifting/split", json={"days": bad}).json()
        self.assertFalse(saved["ok"])
        self.assertTrue(any(v["rule"] == "one_press_per_session"
                            for v in saved["violations"]))

    def test_an_empty_split_is_refused(self) -> None:
        for body in ({}, {"days": []}, {"days": "nope"}):
            self.assertEqual(
                self.client.put("/api/lifting/split", json=body).status_code, 400)

    def test_resetting_restores_the_default(self) -> None:
        self.client.put("/api/lifting/split", json={"days": [
            {"slot": "upper_1", "name": "X", "exercises": [{"title": "Row"}]}]})
        self.assertTrue(self.client.delete("/api/lifting/split").json()["reset"])
        self.assertEqual(self.client.get("/api/lifting/split").json()["source"], "default")

    def test_the_split_is_isolated_per_event_profile(self) -> None:
        from unittest.mock import patch
        from app import coaching_contract
        marathon = {"id": "m-1", "mode": "MARATHON", "event": "M", "date": "2026-10-18",
                    "disciplines_and_distances": {"run_km": 42.2}, "goal": {"target": "x"}}
        with patch.object(coaching_contract, "EVENT_PROFILE", marathon):
            self.client.put("/api/lifting/split", json={"days": [
                {"slot": "upper_1", "name": "Marathon Upper", "exercises": [{"title": "Row"}]}]})
        # A different profile must not inherit it.
        self.assertEqual(self.client.get("/api/lifting/split").json()["source"], "default")


class SplitUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_the_split_is_the_first_card_on_the_lifting_tab(self) -> None:
        tab = self.html[self.html.index('<main id="main-lift"'):]
        tab = tab[:tab.index("</main>")]
        self.assertLess(tab.index('id="lift-split"'), tab.index('id="lift-head"'))

    def test_it_is_four_across_on_desktop_and_two_by_two_on_a_phone(self) -> None:
        self.assertIn(".splitgrid{display:grid;grid-template-columns:repeat(4,1fr)", self.html)
        self.assertIn("@media (max-width:820px){ .splitgrid{grid-template-columns:repeat(2,1fr);} }",
                      self.html)

    def test_each_day_is_clickable_and_opens_its_own_sheet(self) -> None:
        self.assertIn('b.onclick=()=>openSplitDay(b.dataset.slot)', self.html)
        self.assertIn('id="split-overlay"', self.html)

    def test_talking_about_a_day_routes_into_the_main_coach_chat(self) -> None:
        self.assertIn('switchView("coach")', self.html)
        self.assertIn("About my ${day.name} day", self.html)

    def test_day_names_and_exercises_are_escaped(self) -> None:
        for sink in ('${esc(String(day.name))}', '${esc(String(ex.title))}',
                     '${esc(String(day.focus||""))}'):
            self.assertIn(sink, self.html, sink)


if __name__ == "__main__":
    unittest.main()
