"""Nutrition tracker: meal plan, food library, water, week view and label reads.

The meal plan splits coaching targets, so every column must reconcile with the
day's targets exactly. The library and label reader only ever carry exact values
the athlete entered or a printed panel showed; nothing is estimated.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import config, db, main, nutrition

_TODAY = datetime.date(2026, 8, 17)
_TARGETS = {"kcal": 3600, "carb_g": 559, "protein_g": 155, "fat_g": 86}


def _fuel(total: int = 75) -> dict:
    return {"needed": True, "total_carb_g": [total, total], "carb_g_per_hr": total}


class _TempDb(unittest.TestCase):
    def setUp(self) -> None:
        patches = [patch.object(config, "local_today", return_value=_TODAY),
                   patch.object(config, "local_now",
                                return_value=datetime.datetime(2026, 8, 17, 12, 5)),
                   patch.object(nutrition, "weight_kg", return_value=86.0),
                   patch.object(nutrition, "weight_info", return_value={"kg": 86.0}),
                   # get_day must not reach Garmin or the plan engine in tests.
                   patch.object(nutrition, "_today_session",
                                return_value={"discipline": "run", "duration_min": 60,
                                              "intensity": "Z2", "start_time": "07:00"}),
                   patch.object(nutrition.garmin_source, "get_recent_load",
                                return_value={"activities": []})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._old = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "n.db"
        db._local = threading.local()
        db._conn()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH, db._local = self._old
        self._tmp.cleanup()


class MealPlanTests(unittest.TestCase):
    def _plan(self, session: dict, fueling: dict | None = None, logged=None) -> dict:
        with patch.object(nutrition, "weight_kg", return_value=86.0):
            return nutrition.meal_plan(session, _TARGETS, fueling or _fuel(), logged)

    def _assert_reconciles(self, plan: dict) -> None:
        slots = plan["slots"]
        for field in ("carb_g", "protein_g", "fat_g"):
            self.assertEqual(sum(s[field] for s in slots), _TARGETS[field], field)
        self.assertEqual(sum(s["kcal"] for s in slots),
                         _TARGETS["carb_g"] * 4 + _TARGETS["protein_g"] * 4 + _TARGETS["fat_g"] * 9)

    def test_rest_day_is_four_meals_without_session_slots(self) -> None:
        plan = self._plan({"is_rest": True, "discipline": "rest"}, {"needed": False})
        self.assertEqual([s["key"] for s in plan["slots"]], ["breakfast", "lunch", "snack", "dinner"])
        self.assertIsNone(plan["session_time"])
        self._assert_reconciles(plan)

    def test_morning_session_makes_breakfast_the_recovery_meal(self) -> None:
        plan = self._plan({"discipline": "run", "duration_min": 90, "intensity": "threshold",
                           "start_time": "06:00", "title": "Threshold run"})
        keys = [s["key"] for s in plan["slots"]]
        self.assertEqual(keys[:3], ["pre", "during", "post"])
        post = plan["slots"][2]
        self.assertEqual(post["label"], "Recovery breakfast")
        self.assertIn("breakfast", post["accepts"])
        self.assertEqual(post["carb_level"], "high")
        self.assertEqual(plan["slots"][1]["carb_g"], 75)
        self.assertEqual(plan["session_time_source"], "plan")
        self._assert_reconciles(plan)

    def test_missing_session_time_is_assumed_and_labelled(self) -> None:
        plan = self._plan({"discipline": "bike", "duration_min": 60, "intensity": "Z2"})
        self.assertEqual(plan["session_time"], "17:00")
        self.assertEqual(plan["session_time_source"], "assumed")
        self.assertEqual(plan["slots"][-1]["label"], "Recovery dinner")
        self._assert_reconciles(plan)

    def test_carbs_concentrate_around_the_session(self) -> None:
        plan = self._plan({"discipline": "bike", "duration_min": 120, "intensity": "tempo",
                           "start_time": "18:00"})
        by_key = {s["key"]: s for s in plan["slots"]}
        self.assertGreater(by_key["post"]["carb_g"], by_key["breakfast"]["carb_g"])
        self.assertLess(by_key["pre"]["fat_g"], by_key["breakfast"]["fat_g"])

    def test_logged_food_maps_to_its_slot_once(self) -> None:
        logged = [{"meal": "breakfast", "kcal": 500, "protein_g": 30, "carb_g": 80, "fat_g": 8},
                  {"meal": "", "kcal": 100, "protein_g": 0, "carb_g": 25, "fat_g": 0}]
        plan = self._plan({"discipline": "run", "duration_min": 45, "start_time": "06:30"},
                          logged=logged)
        post = next(s for s in plan["slots"] if s["key"] == "post")
        self.assertEqual(post["logged"]["carb_g"], 80)
        self.assertEqual(post["entries"], 1)
        self.assertEqual(plan["unassigned"]["entries"], 1)

    def test_notes_never_carry_portions(self) -> None:
        plan = self._plan({"discipline": "run", "duration_min": 150, "start_time": "12:00"})
        for slot in plan["slots"]:
            if slot["key"] != "during":
                self.assertNotRegex(slot["note"], r"\d")


class LibraryAndLogTests(_TempDb):
    def _log(self, **body) -> dict:
        return json.loads(main.nutrition_log(body).body)

    def test_structured_log_requires_every_macro(self) -> None:
        result = self._log(name="Rice", kcal=200, protein_g=4, carb_g=45)
        self.assertFalse(result["ok"])
        self.assertEqual(result["unknown_fields"], ["fat_g"])
        self.assertEqual(db.get_nutrition("2026-08-17"), [])

    def test_log_saves_food_and_one_tap_relog_multiplies(self) -> None:
        result = self._log(name="Greek yogurt", serving="170 g", kcal=100, protein_g=17,
                           carb_g=6, fat_g=0.7, sodium_mg=60, meal="snack", source="label")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["warning"])
        foods = db.list_foods()
        self.assertEqual(len(foods), 1)
        self.assertEqual((foods[0]["serving"], foods[0]["protein_g"], foods[0]["source"]),
                         ("170 g", 17, "label"))
        again = json.loads(main.nutrition_food_log(foods[0]["id"], {"servings": 2}).body)
        self.assertTrue(again["ok"])
        rows = db.get_nutrition("2026-08-17")
        self.assertEqual([r["protein_g"] for r in rows], [17, 34])
        self.assertEqual(rows[1]["description"], "Greek yogurt x2")
        self.assertEqual(db.get_food(foods[0]["id"])["uses"], 2)
        self.assertEqual(again["day"]["totals"]["sodium_mg"], 180)

    def test_same_name_updates_one_library_entry(self) -> None:
        self._log(name="Bagel", kcal=270, protein_g=10, carb_g=53, fat_g=1.5)
        self._log(name="bagel ", kcal=280, protein_g=11, carb_g=54, fat_g=2)
        foods = db.list_foods()
        self.assertEqual(len(foods), 1)
        self.assertEqual(foods[0]["kcal"], 280)

    def test_energy_mismatch_warns_but_logs(self) -> None:
        result = self._log(name="Typo bar", kcal=200, protein_g=10, carb_g=300, fat_g=5)
        self.assertTrue(result["ok"])
        self.assertIn("don't match", result["warning"])

    def test_bad_values_are_rejected(self) -> None:
        self.assertEqual(main.nutrition_log({"name": "X", "kcal": -1, "protein_g": 1,
                                             "carb_g": 1, "fat_g": 1}).status_code, 400)
        self.assertEqual(main.nutrition_log({"name": "X", "kcal": "lots", "protein_g": 1,
                                             "carb_g": 1, "fat_g": 1}).status_code, 400)
        self.assertEqual(main.nutrition_log({"name": "", "kcal": 1}).status_code, 400)

    def test_favorite_and_delete(self) -> None:
        self._log(name="Oats", kcal=150, protein_g=5, carb_g=27, fat_g=3)
        self._log(name="Banana", kcal=105, protein_g=1, carb_g=27, fat_g=0)
        oats = next(f for f in db.list_foods() if f["name"] == "Oats")
        main.nutrition_food_favorite(oats["id"], {"favorite": True})
        self.assertEqual(db.list_foods()[0]["name"], "Oats")
        main.nutrition_food_delete(oats["id"])
        self.assertEqual([f["name"] for f in db.list_foods()], ["Banana"])
        self.assertEqual(main.nutrition_food_log(oats["id"], {}).status_code, 404)

    def test_text_log_still_requires_explicit_macros(self) -> None:
        result = self._log(text="chicken and rice")
        self.assertTrue(result["requires_input"])
        self.assertEqual(db.list_foods(), [])


class WaterAndWeekTests(_TempDb):
    def test_water_adds_and_undoes(self) -> None:
        main.nutrition_water({"ml": 500})
        main.nutrition_water({"ml": 250})
        day = json.loads(main.nutrition_water_undo().body)["day"]
        self.assertEqual(day["hydration"]["logged_ml"], 500)
        self.assertEqual(main.nutrition_water({"ml": 0}).status_code, 400)
        self.assertEqual(main.nutrition_water({"ml": True}).status_code, 400)

    def test_hydration_target_adds_session_sweat(self) -> None:
        rest = nutrition.hydration({"is_rest": True}, "2026-08-17")
        run = nutrition.hydration({"discipline": "run", "duration_min": 90}, "2026-08-17")
        self.assertEqual(rest["target_ml"], 2600)        # 86 x 30 = 2,580 -> 2,600
        self.assertEqual(run["target_ml"] - rest["target_ml"], 1500)

    def test_week_uses_only_stored_targets(self) -> None:
        db.add_nutrition("2026-08-15", "Lunch", kcal=800, protein_g=50, carb_g=100, fat_g=20)
        nutrition._snapshot_targets("2026-08-17", _TARGETS)
        db.add_nutrition("2026-08-17", "Bowl", kcal=900, protein_g=150, carb_g=120, fat_g=25)
        week = nutrition.week_summary()
        self.assertEqual(len(week["days"]), 7)
        by_date = {d["date"]: d for d in week["days"]}
        self.assertIsNone(by_date["2026-08-15"]["target"])
        self.assertEqual(by_date["2026-08-17"]["target"]["protein_g"], 155)
        self.assertEqual(week["days_logged"], 2)
        self.assertEqual(week["protein_days_hit"], 1)


class LabelTests(unittest.TestCase):
    def _client(self, payload: dict):
        msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        created = {}

        def create(**kwargs):
            created.update(kwargs)
            return msg
        return SimpleNamespace(messages=SimpleNamespace(create=create)), created

    def test_label_values_prefill_without_logging(self) -> None:
        client, created = self._client({
            "is_nutrition_label": True, "product": "Protein bar", "serving": "1 bar (60 g)",
            "kcal": 210, "protein_g": 20, "carb_g": 22, "fat_g": 7, "sodium_mg": 180})
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client),
              patch.object(nutrition.db, "add_nutrition") as add):
            result = nutrition.read_label("aGk=", "image/jpeg")
        self.assertTrue(result["ok"])
        self.assertEqual(result["food"]["protein_g"], 20)
        self.assertEqual(result["unknown_fields"], [])
        add.assert_not_called()
        self.assertEqual(created["output_config"]["format"]["type"], "json_schema")

    def test_plate_photo_is_refused(self) -> None:
        client, _ = self._client({
            "is_nutrition_label": False, "product": None, "serving": None, "kcal": None,
            "protein_g": None, "carb_g": None, "fat_g": None, "sodium_mg": None})
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client)):
            result = nutrition.read_label("aGk=", "image/jpeg")
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_input"])

    def test_unreadable_field_stays_unknown(self) -> None:
        client, _ = self._client({
            "is_nutrition_label": True, "product": "Gel", "serving": "1 gel", "kcal": 90,
            "protein_g": 0, "carb_g": 23, "fat_g": None, "sodium_mg": 9999999})
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client)):
            result = nutrition.read_label("aGk=", "image/jpeg")
        self.assertEqual(result["unknown_fields"], ["fat_g"])
        self.assertIsNone(result["food"]["sodium_mg"])

    def test_label_endpoint_validates(self) -> None:
        self.assertEqual(main.nutrition_label({}).status_code, 400)
        self.assertEqual(main.nutrition_label({"image_b64": "x" * 7_000_001}).status_code, 413)
        self.assertFalse(nutrition.read_label("aGk=", "application/pdf")["ok"])


if __name__ == "__main__":
    unittest.main()
