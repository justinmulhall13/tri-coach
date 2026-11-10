"""Fuel: ballpark estimates for typed food, and barcode lookup.

The athlete asked to type "iced chai latte grande from Starbucks" and get a
ballpark. An estimate is logged, but always marked as one. A barcode is only
ever a prefill for review.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import config, db, main, nutrition

_CHAI = {"name": "Starbucks Iced Chai Tea Latte, grande", "serving": "16 fl oz", "kcal": 240,
         "protein_g": 7, "carb_g": 44, "fat_g": 4, "sodium_mg": 115, "basis": "published"}


def _client(payload: dict):
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))])
    return client


def _off(nutriments: dict, **product) -> dict:
    return {"status": 1, "product": {"product_name": "Hazelnut Spread", "brands": "Nutella,Ferrero",
                                     "serving_size": "15 g", "nutriments": nutriments, **product}}


class EstimateTests(unittest.TestCase):
    def _log(self, text: str, payload: dict):
        client = _client(payload)
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client),
              patch.object(nutrition, "get_day", return_value={}),
              patch.object(nutrition.db, "add_nutrition", side_effect=[11, 12, 13]) as add):
            return nutrition.log_food(text), add, client

    def test_typed_food_is_logged_as_an_estimate(self) -> None:
        result, add, client = self._log("Iced chai latte grande from starbucks",
                                        {"understood": True, "question": None, "items": [_CHAI]})
        self.assertTrue(result["ok"])
        self.assertTrue(result["estimated"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["input_provenance"]["macros"], "estimate")
        self.assertIn("Logged as an estimate", result["summary"])
        self.assertIn("240 kcal", result["summary"])
        self.assertNotIn("—", result["summary"])
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["source"], "estimate")
        self.assertEqual(kwargs["kcal"], 240)
        self.assertIn("Starbucks", add.call_args.args[1])
        sent = client.messages.create.call_args.kwargs
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertIn("Iced chai latte grande from starbucks", sent["messages"][0]["content"])

    def test_several_foods_become_several_entries(self) -> None:
        egg = {**_CHAI, "name": "Scrambled eggs", "serving": "2 eggs", "kcal": 180, "basis": "typical"}
        result, add, _ = self._log("eggs and a chai for breakfast",
                                   {"understood": True, "question": None, "items": [egg, _CHAI]})
        self.assertEqual(result["added"], 2)
        self.assertEqual(add.call_count, 2)
        self.assertEqual(add.call_args.kwargs["meal"], "breakfast")
        self.assertIn("420 kcal", result["summary"])

    def test_not_food_asks_instead_of_logging(self) -> None:
        result, add, _ = self._log("my bike is blue",
                                   {"understood": False, "question": "What did you eat?", "items": []})
        self.assertTrue(result["requires_input"])
        self.assertEqual(result["question"], "What did you eat?")
        add.assert_not_called()

    def test_out_of_range_items_are_dropped(self) -> None:
        result, add, _ = self._log("a lot", {"understood": True, "question": None,
                                             "items": [{**_CHAI, "kcal": 999999}]})
        self.assertTrue(result["requires_input"])
        add.assert_not_called()

    def test_model_failure_logs_nothing(self) -> None:
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("down")
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client),
              patch.object(nutrition.db, "add_nutrition") as add):
            result = nutrition.log_food("bagel")
        self.assertIn("Couldn't estimate", result["error"])
        add.assert_not_called()

    def test_explicit_macros_never_reach_the_model(self) -> None:
        with (patch.object(nutrition, "estimate_food") as est,
              patch.object(nutrition, "get_day", return_value={}),
              patch.object(nutrition.db, "add_nutrition", return_value=5) as add):
            result = nutrition.log_food("oats 400 kcal, 20 g protein, 60 g carbs, 9 g fat")
        est.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertIsNone(add.call_args.kwargs.get("source"))

    def test_the_source_column_is_stored_and_reported(self) -> None:
        old = db._DB_PATH, db._local
        with tempfile.TemporaryDirectory() as tmp:
            db._DB_PATH, db._local = Path(tmp) / "t.db", threading.local()
            try:
                db.add_nutrition("2026-11-10", "Chai", kcal=240, protein_g=7, carb_g=44,
                                 fat_g=4, source="estimate")
                db.add_nutrition("2026-11-10", "Oats", kcal=400, protein_g=20, carb_g=60, fat_g=9)
                rows = db.get_nutrition("2026-11-10")
                with (patch.object(nutrition, "_today_session", return_value={"discipline": "run", "duration_min": 60, "intensity": "Z2", "start_time": "07:00"}),
                      patch.object(nutrition, "weight_info", return_value={"kg": 80.0}),
                      patch.object(nutrition, "weight_kg", return_value=80.0),
                      patch.object(config, "local_today",
                                   return_value=__import__("datetime").date(2026, 11, 10)),
                      patch.object(nutrition.garmin_source, "get_recent_load",
                                   return_value={"activities": []})):
                    day = nutrition.get_day()
            finally:
                db._local.conn.close()
                db._DB_PATH, db._local = old
        self.assertEqual(day["estimated"], {"entries": 1, "kcal": 240})
        self.assertEqual([r["source"] for r in rows], ["estimate", None])


class BarcodeTests(unittest.TestCase):
    def test_check_digits(self) -> None:
        for good in ("3017620422003", "036000291452", "96385074", "00012345678905"):
            self.assertTrue(nutrition.barcode_ok(good), good)
        for bad in ("3017620422004", "12345", "30176204220031234", "abcdefgh", ""):
            self.assertFalse(nutrition.barcode_ok(bad), bad)

    def test_lookup_prefills_per_serving_and_logs_nothing(self) -> None:
        data = _off({"energy-kcal_serving": 80, "proteins_serving": 0.9, "carbohydrates_serving": 8.6,
                     "fat_serving": 4.6, "sodium_serving": 0.0064, "energy-kcal_100g": 539})
        with (patch.object(nutrition, "_off_fetch", return_value=data) as fetch,
              patch.object(nutrition.db, "add_nutrition") as add):
            result = nutrition.lookup_barcode("3017620422003")
        fetch.assert_called_once_with("3017620422003")
        add.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["food"]["name"], "Nutella Hazelnut Spread")
        self.assertEqual(result["food"]["serving"], "15 g")
        self.assertEqual(result["food"]["kcal"], 80)
        self.assertAlmostEqual(result["food"]["sodium_mg"], 6.4)
        self.assertEqual(result["unknown_fields"], [])

    def test_lookup_falls_back_to_per_100g(self) -> None:
        data = _off({"energy-kcal_100g": 539, "proteins_100g": 6.3, "carbohydrates_100g": 57.5})
        with patch.object(nutrition, "_off_fetch", return_value=data):
            result = nutrition.lookup_barcode("3017620422003")
        self.assertEqual(result["food"]["serving"], "100 g")
        self.assertEqual(result["food"]["kcal"], 539)
        self.assertEqual(result["unknown_fields"], ["fat_g"])

    def test_unknown_product_and_bad_code_ask_plainly(self) -> None:
        with patch.object(nutrition, "_off_fetch", return_value=None):
            missing = nutrition.lookup_barcode("3017620422003")
        self.assertTrue(missing["requires_input"])
        self.assertIn("3017620422003", missing["question"])
        with patch.object(nutrition, "_off_fetch") as fetch:
            bad = nutrition.lookup_barcode("3017620422004")
        fetch.assert_not_called()
        self.assertTrue(bad["requires_input"])

    def test_database_outage_is_an_error_not_a_guess(self) -> None:
        with patch.object(nutrition, "_off_fetch", side_effect=TimeoutError()):
            result = nutrition.lookup_barcode("3017620422003")
        self.assertIn("product database", result["error"])

    def test_a_typed_barcode_is_looked_up_not_estimated(self) -> None:
        with (patch.object(nutrition, "lookup_barcode", return_value={"ok": True}) as look,
              patch.object(nutrition, "estimate_food") as est):
            nutrition.log_food("3017 6204 22003")
        look.assert_called_once_with("3017620422003")
        est.assert_not_called()

    def test_a_barcode_photo_goes_to_the_lookup(self) -> None:
        client = _client({"is_nutrition_label": False, "product": None, "serving": None,
                          "kcal": None, "protein_g": None, "carb_g": None, "fat_g": None,
                          "sodium_mg": None, "barcode": "3 017620 422003"})
        with (patch.object(nutrition, "_ok_key", return_value="k"),
              patch("anthropic.Anthropic", return_value=client),
              patch.object(nutrition, "lookup_barcode", return_value={"ok": True}) as look):
            nutrition.read_label("aGk=", "image/jpeg")
        look.assert_called_once_with("3017620422003")
        self.assertIn("barcode", nutrition._LABEL_SCHEMA["required"])

    def test_the_request_names_no_person(self) -> None:
        captured = {}
        def fake(req, timeout=0):
            captured["ua"] = req.get_header("User-agent")
            captured["url"] = req.full_url
            raise TimeoutError()
        with patch("urllib.request.urlopen", side_effect=fake):
            with self.assertRaises(TimeoutError):
                nutrition._off_fetch("3017620422003")
        self.assertEqual(captured["ua"], "Tri-Coach/1.0")
        self.assertTrue(captured["url"].startswith("https://world.openfoodfacts.org/api/v2/product/3017620422003.json"))


class FuelUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (main.STATIC_DIR / "index.html").read_text()

    def test_there_is_a_say_it_box_and_estimates_are_labelled(self) -> None:
        self.assertIn('id="nu-say-text"', self.html)
        self.assertIn('$("nu-say").onsubmit=sayFood;', self.html)
        self.assertIn('e.source==="estimate"', self.html)
        self.assertIn("Scan barcode</button>", self.html)


if __name__ == "__main__":
    unittest.main()
