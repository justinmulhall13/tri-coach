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


class SplitEditingUITests(unittest.TestCase):
    """The split is edited in place. Routing every tweak through the coach meant
    leaving the page to change one exercise."""

    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_the_day_sheet_edits_saves_and_pushes_without_leaving(self) -> None:
        for control in ('id="split-editor"', 'id="split-add"', 'id="split-save"',
                        'id="split-push"'):
            self.assertIn(control, self.html, control)

    def test_asking_the_coach_is_available_but_not_the_only_path(self) -> None:
        self.assertIn('id="split-ask"', self.html)
        # Saving must not bounce to the coach tab.
        save = self.html[self.html.index('$("split-save").onclick'):]
        save = save[:save.index('$("split-push")')]
        self.assertNotIn("switchView", save)

    def test_pushing_a_day_requires_explicit_confirmation(self) -> None:
        self.assertIn('JSON.stringify({confirmed:true})', self.html)

    def test_every_helper_the_tab_calls_is_actually_defined(self) -> None:
        # A block rewrite once deleted _liftFig while leaving eight call sites,
        # which would have thrown a ReferenceError on opening the tab.
        for helper in ("_liftFig", "_liftNum", "renderLiftSplit", "openSplitDay",
                       "renderSplitEditor", "_splitEditedDay", "_saveSplit",
                       "_splitStatus", "closeSplitDay"):
            self.assertIn(f"function {helper}(", self.html, helper)


class SplitPushTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "push.db"
        db._local = threading.local()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    def test_a_push_without_confirmation_is_refused(self) -> None:
        response = self.client.post("/api/lifting/split/upper_1/push", json={})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["created"])

    def test_an_unknown_day_is_a_404_not_a_crash(self) -> None:
        response = self.client.post("/api/lifting/split/nonsense/push",
                                    json={"confirmed": True})
        self.assertEqual(response.status_code, 404)

    def test_a_day_that_cannot_be_fully_matched_is_not_partially_created(self) -> None:
        from unittest.mock import patch
        # Half the exercises unresolvable: the day must be refused whole rather
        # than shipped with movements quietly missing.
        with patch.object(main.hevy_actions, "resolve_routine_exercises",
                          return_value=({"title": "Upper 1", "exercises": []},
                                        [{"title": "Chest Fly",
                                          "exercise_template_id": None,
                                          "resolution": "failed"}])):
            response = self.client.post("/api/lifting/split/upper_1/push",
                                        json={"confirmed": True})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()["created"])
        self.assertIn("Chest Fly", response.json()["unresolved"])

    def test_reps_become_hevy_sets_without_inventing_a_weight(self) -> None:
        sets = main._reps_to_sets({"reps": "8-10"}, 3)
        self.assertEqual(len(sets), 3)
        self.assertEqual(sets[0]["rep_range"], {"start": 8, "end": 10})
        self.assertNotIn("weight_kg", sets[0])

    def test_per_side_rep_text_is_parsed_rather_than_defaulted(self) -> None:
        sets = main._reps_to_sets({"reps": "12-15 each side"}, 2)
        self.assertEqual(sets[0]["rep_range"], {"start": 12, "end": 15})
        self.assertEqual(len(sets), 2)

    def test_unparseable_reps_fall_back_rather_than_raising(self) -> None:
        self.assertEqual(main._reps_to_sets({"reps": "as many as you like"}, 1)[0]["reps"], 10)
        self.assertEqual(main._reps_to_sets({}, 0)[0]["reps"], 10)
