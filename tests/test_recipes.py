from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import db, recipes


RECIPE_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "Chicken rice bowl",
  "image": ["https://cdn.example.com/chicken-rice.jpg"],
  "description": "A useful post-run dinner.",
  "totalTime": "PT35M",
  "recipeYield": "4 servings",
  "recipeIngredient": ["2 chicken breasts", "2 cups rice", "1 red pepper"],
  "recipeInstructions": [{"@type":"HowToStep","text":"Cook it."}],
  "nutrition": {
    "calories": "620 calories", "proteinContent": "44 g",
    "carbohydrateContent": "78 g", "fatContent": "14 g"
  }
}
</script>
</head></html>
"""


class RecipeParsingTests(unittest.TestCase):
    def test_recipe_facts_and_photo_come_from_json_ld(self) -> None:
        result = recipes.parse_recipe_page(
            RECIPE_HTML, "https://recipes.example.com/chicken-rice")
        self.assertEqual(result["title"], "Chicken rice bowl")
        self.assertEqual(result["image_url"], "https://cdn.example.com/chicken-rice.jpg")
        self.assertEqual(result["total_minutes"], 35)
        self.assertEqual(result["nutrition"]["protein_g"], 44)
        self.assertEqual(len(result["ingredients"]), 3)
        self.assertEqual(result["provenance"], "publisher_recipe_json_ld")

    def test_page_without_recipe_schema_is_not_invented(self) -> None:
        self.assertIsNone(recipes.parse_recipe_page(
            "<html><title>Roundup</title></html>", "https://example.com/roundup"))

    def test_private_and_local_urls_are_rejected(self) -> None:
        for url in ("http://127.0.0.1/recipe", "http://localhost/recipe",
                    "file:///etc/passwd", "http://10.0.0.5/recipe"):
            with self.assertRaises(ValueError, msg=url):
                recipes._public_url(url)

    def test_invalid_result_limit_is_rejected_cleanly(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            recipes.discover("dinner", limit="lots")

    def test_verified_discovery_is_cached_without_refetching_publishers(self) -> None:
        recipes._cache.clear()
        source = {"title": "Chicken", "url": "https://recipes.example.com/chicken-rice"}
        with patch.object(recipes, "_search_sources", return_value=[source]) as search, \
                patch.object(recipes, "_fetch_html", return_value=RECIPE_HTML) as fetch:
            first = recipes.discover("chicken dinner", day={"remaining": {}}, limit=1)
            second = recipes.discover("chicken dinner", day={"remaining": {}}, limit=1)
        self.assertEqual(len(first["recipes"]), 1)
        self.assertTrue(second["cached"])
        search.assert_called_once()
        fetch.assert_called_once()
        recipes._cache.clear()


class RecipePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db._DB_PATH
        self._old_local = db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "recipes.db"
        db._local = threading.local()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH = self._old_path
        db._local = self._old_local
        self._tmp.cleanup()

    def _recipe(self):
        return recipes.parse_recipe_page(
            RECIPE_HTML, "https://recipes.example.com/chicken-rice")

    def test_saved_recipe_survives_a_new_connection(self) -> None:
        saved = db.save_recipe(self._recipe())
        self.assertGreater(saved["id"], 0)
        db._local.conn.close()
        db._local = threading.local()
        loaded = db.get_saved_recipes()
        self.assertEqual(loaded[0]["source_url"], saved["source_url"])
        self.assertEqual(loaded[0]["ingredients"], saved["ingredients"])

    def test_recipe_builds_an_idempotent_checkable_grocery_list(self) -> None:
        saved = db.save_recipe(self._recipe())
        first = db.add_recipe_to_grocery(saved["id"])
        second = db.add_recipe_to_grocery(saved["id"])
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        checked = db.set_grocery_checked(first[0]["id"], True)
        self.assertTrue(checked["checked"])
        self.assertEqual(db.clear_checked_grocery(), 1)
        self.assertEqual(len(db.get_grocery_items()), 2)


if __name__ == "__main__":
    unittest.main()
