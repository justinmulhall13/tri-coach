from __future__ import annotations

import unittest

from app import hevy_exercises as hx


# Shapes returned by the real GET /v1/exercise_templates endpoint.
CATALOG = [
    {"id": "07B38369", "title": "Incline Bench Press (Dumbbell)",
     "primary_muscle_group": "chest", "equipment": "dumbbell"},
    {"id": "422B08F1", "title": "Lateral Raise (Cable)",
     "primary_muscle_group": "shoulders", "equipment": "machine"},
    {"id": "F1E57334", "title": "Dumbbell Row",
     "primary_muscle_group": "upper_back", "equipment": "dumbbell"},
]


class FakeConnector:
    def __init__(self, *, fail: bool = False, no_id: bool = False) -> None:
        self.created: list[dict] = []
        self._fail, self._no_id = fail, no_id

    def create_exercise_template(self, exercise, *, idempotency_key):
        if self._fail:
            raise RuntimeError("Hevy rejected it")
        self.created.append(exercise)
        return {} if self._no_id else {"id": "NEW12345", **exercise}


class MatchingTests(unittest.TestCase):
    def test_an_exact_title_matches(self) -> None:
        found = hx.find_existing(CATALOG, "Dumbbell Row")
        self.assertEqual(found["id"], "F1E57334")

    def test_an_equipment_qualifier_does_not_prevent_a_match(self) -> None:
        # Asking for "Incline Bench Press" must find the (Dumbbell) variant
        # rather than creating a near-duplicate template.
        found = hx.find_existing(CATALOG, "Incline Bench Press")
        self.assertEqual(found["id"], "07B38369")

    def test_a_different_movement_is_never_substituted(self) -> None:
        # The coach was right to refuse Lateral Raise for Rear Delt Fly: they
        # load different heads of the shoulder.
        self.assertIsNone(hx.find_existing(CATALOG, "Rear Delt Fly"))

    def test_matching_ignores_case_and_spacing(self) -> None:
        self.assertIsNotNone(hx.find_existing(CATALOG, "  dumbbell   row  "))

    def test_a_missing_or_malformed_catalog_is_handled(self) -> None:
        for bad in (None, "catalog", 42, [None, {}, {"title": "x"}]):
            self.assertIsNone(hx.find_existing(bad, "Dumbbell Row"))

    def test_an_empty_title_matches_nothing(self) -> None:
        self.assertIsNone(hx.find_existing(CATALOG, ""))


class VariantPreferenceTests(unittest.TestCase):
    """Hevy ships an equipment variant per movement. Picking the first is how a
    dumbbell presser gets prescribed a barbell, or a lifter gets band squats."""

    VARIANTS = [
        {"id": "BAND", "title": "Squat (Band)", "equipment": "resistance_band"},
        {"id": "BARB", "title": "Squat (Barbell)", "equipment": "barbell"},
        {"id": "SMITH", "title": "Squat (Smith Machine)", "equipment": "machine"},
        {"id": "DECLINE", "title": "Decline Chest Fly (Dumbbell)", "equipment": "dumbbell"},
        {"id": "CHESTDB", "title": "Chest Fly (Dumbbell)", "equipment": "dumbbell"},
        {"id": "CHESTBAND", "title": "Chest Fly (Band)", "equipment": "resistance_band"},
        {"id": "REARDELT", "title": "Rear Delt Reverse Fly (Dumbbell)", "equipment": "dumbbell"},
        {"id": "LATRAISE", "title": "Lateral Raise (Cable)", "equipment": "machine"},
    ]

    def test_a_variant_the_athlete_actually_uses_wins(self) -> None:
        found = hx.find_existing(self.VARIANTS, "Squat", history_ids={"BARB"})
        self.assertEqual(found["id"], "BARB")

    def test_without_history_it_still_returns_a_variant_rather_than_nothing(self) -> None:
        self.assertIsNotNone(hx.find_existing(self.VARIANTS, "Squat"))

    def test_a_closer_title_beats_a_longer_one(self) -> None:
        # "Chest Fly" must not resolve to "Decline Chest Fly".
        found = hx.find_existing(self.VARIANTS, "Chest Fly")
        self.assertIn(found["id"], {"CHESTDB", "CHESTBAND"})

    def test_a_word_subset_match_finds_a_renamed_movement(self) -> None:
        # "Rear Delt Fly" is Hevy's "Rear Delt Reverse Fly".
        found = hx.find_existing(self.VARIANTS, "Rear Delt Fly")
        self.assertEqual(found["id"], "REARDELT")

    def test_a_different_movement_is_still_never_substituted(self) -> None:
        # Lateral Raise shares no required word with Rear Delt Fly.
        found = hx.find_existing(self.VARIANTS, "Rear Delt Fly")
        self.assertNotEqual(found["id"], "LATRAISE")
        self.assertIsNone(hx.find_existing(
            [t for t in self.VARIANTS if t["id"] == "LATRAISE"], "Rear Delt Fly"))

    def test_an_explicit_equipment_preference_is_honoured(self) -> None:
        found = hx.find_existing(self.VARIANTS, "Squat", prefer_equipment="machine")
        self.assertEqual(found["id"], "SMITH")

    def test_history_outranks_an_equipment_preference(self) -> None:
        found = hx.find_existing(self.VARIANTS, "Squat", history_ids={"BARB"},
                                 prefer_equipment="resistance_band")
        self.assertEqual(found["id"], "BARB")


class CreationPayloadTests(unittest.TestCase):
    def test_every_field_is_a_valid_enum_value(self) -> None:
        for title in ("Rear Delt Fly", "Chest Fly (Cable)", "Plank", "Dead Bug",
                      "Nordic Hamstring Curl", "Zercher Squat", "Wrist Roller",
                      "Some Nonsense Movement"):
            payload = hx.creation_payload(title)
            self.assertIn(payload["muscle_group"], hx.MUSCLE_GROUPS, title)
            self.assertIn(payload["exercise_type"], hx.EXERCISE_TYPES, title)
            self.assertIn(payload["equipment_category"], hx.EQUIPMENT, title)

    def test_the_post_field_names_differ_from_the_get_field_names(self) -> None:
        # GET returns primary_muscle_group/type/equipment; POST rejects those.
        payload = hx.creation_payload("Rear Delt Fly")
        self.assertEqual(set(payload), {"title", "muscle_group",
                                        "exercise_type", "equipment_category"})

    def test_a_rear_delt_fly_is_filed_under_shoulders_not_chest(self) -> None:
        # The pattern says "fly" (chest); the title says rear delt, which wins.
        self.assertEqual(hx.creation_payload("Rear Delt Fly")["muscle_group"], "shoulders")

    def test_a_chest_fly_is_filed_under_chest(self) -> None:
        self.assertEqual(hx.creation_payload("Chest Fly (Cable)")["muscle_group"], "chest")

    def test_equipment_is_read_from_the_title(self) -> None:
        self.assertEqual(hx.creation_payload("Row (Barbell)")["equipment_category"], "barbell")
        self.assertEqual(hx.creation_payload("Curl (Dumbbell)")["equipment_category"], "dumbbell")
        self.assertEqual(hx.creation_payload("Lat Pulldown (Cable)")["equipment_category"], "machine")
        self.assertEqual(hx.creation_payload("Rear Delt Fly")["equipment_category"], "none")

    def test_a_held_position_is_measured_by_duration(self) -> None:
        self.assertEqual(hx.creation_payload("Plank")["exercise_type"], "duration")
        self.assertEqual(hx.creation_payload("Wall Sit")["exercise_type"], "duration")

    def test_a_bodyweight_movement_is_typed_as_such(self) -> None:
        for title in ("Pull Up", "Push-Up", "Dead Bug", "Nordic Hamstring Curl"):
            self.assertEqual(hx.creation_payload(title)["exercise_type"],
                             "bodyweight_reps", title)

    def test_a_loaded_movement_defaults_to_weight_and_reps(self) -> None:
        self.assertEqual(hx.creation_payload("Chest Fly (Cable)")["exercise_type"], "weight_reps")

    def test_an_absurd_title_is_truncated_rather_than_rejected(self) -> None:
        self.assertLessEqual(len(hx.creation_payload("x" * 500)["title"]), 100)

    def test_an_empty_title_is_refused(self) -> None:
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                hx.creation_payload(bad)


class ResolveTests(unittest.TestCase):
    def test_an_existing_exercise_is_matched_without_creating(self) -> None:
        connector = FakeConnector()
        result = hx.resolve("Dumbbell Row", templates=CATALOG, connector=connector)
        self.assertEqual(result["resolution"], "matched")
        self.assertEqual(result["exercise_template_id"], "F1E57334")
        self.assertEqual(connector.created, [])

    def test_a_missing_exercise_is_created_rather_than_dropped(self) -> None:
        connector = FakeConnector()
        result = hx.resolve("Rear Delt Fly", templates=CATALOG, connector=connector)
        self.assertEqual(result["resolution"], "created")
        self.assertEqual(result["exercise_template_id"], "NEW12345")
        self.assertEqual(connector.created[0]["title"], "Rear Delt Fly")

    def test_creation_can_be_withheld_and_is_reported_as_missing(self) -> None:
        result = hx.resolve("Rear Delt Fly", templates=CATALOG,
                            connector=FakeConnector(), create=False)
        self.assertEqual(result["resolution"], "missing")
        self.assertIsNone(result["exercise_template_id"])

    def test_a_failed_creation_is_reported_not_swallowed(self) -> None:
        result = hx.resolve("Rear Delt Fly", templates=CATALOG,
                            connector=FakeConnector(fail=True))
        self.assertEqual(result["resolution"], "failed")
        self.assertIn("Hevy rejected it", result["reason"])

    def test_a_create_that_returns_no_id_is_treated_as_a_failure(self) -> None:
        result = hx.resolve("Rear Delt Fly", templates=CATALOG,
                            connector=FakeConnector(no_id=True))
        self.assertEqual(result["resolution"], "failed")
        self.assertIn("no template id", result["reason"])

    def test_no_connector_means_no_creation_attempt(self) -> None:
        result = hx.resolve("Rear Delt Fly", templates=CATALOG, connector=None)
        self.assertEqual(result["resolution"], "missing")


if __name__ == "__main__":
    unittest.main()
