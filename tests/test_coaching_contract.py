from __future__ import annotations

import datetime
import json
import unittest
from unittest.mock import patch

from app import (coach, coaching_contract, fueling_reference, garmin_source,
                 garmin_workout, hevy_connector, nutrition, plan)


class CoachingContractTests(unittest.TestCase):
    def test_event_mode_is_static_and_only_exact_command_is_detected(self) -> None:
        self.assertEqual(coaching_contract.current_mode(), "TRIATHLON")
        self.assertIsNone(coaching_contract.explicit_switch_target("How should I train for a marathon?"))
        self.assertEqual(coaching_contract.explicit_switch_target("switch to marathon"), "marathon")

    def test_contract_contains_fixed_profile_and_event_guards(self) -> None:
        athlete = coaching_contract.athlete_context()
        event = coaching_contract.event_context()
        self.assertEqual(athlete["body_mass_fallback"]["value"], 86.0)
        self.assertEqual(athlete["age"], 19)
        self.assertEqual(event["course_aid"]["bike"]["topology"], "point-to-point, not laps")
        self.assertEqual(event["pacing_targets"]["run_lap_1_min_per_km"], "6:15")
        prompt = coaching_contract.system_prompt()
        self.assertIn("no filler, routine encouragement, or em dashes", prompt)
        self.assertIn("goal-race finish celebration", prompt)

    def test_unknown_switch_leaves_current_mode_installed(self) -> None:
        with (
            patch.object(coach.db, "add_chat") as add_chat,
            patch.object(coach.db, "stage_event_profile", return_value={"event_name": "marathon"}),
        ):
            result = coach._explicit_mode_switch("switch to marathon")
        self.assertIn("Mode remains TRIATHLON", result["reply"])
        self.assertEqual(coaching_contract.current_mode(), "TRIATHLON")
        self.assertEqual(add_chat.call_count, 2)

    def test_current_switch_is_a_deterministic_noop(self) -> None:
        with patch.object(coach.db, "add_chat"):
            result = coach._explicit_mode_switch("switch to triathlon")
        self.assertEqual(result["model"], "deterministic-policy")
        self.assertIn("already the active mode", result["reply"])

    def test_invalid_race_day_tsb_targets_fail_to_safe_plus_ten(self) -> None:
        for value in (float("nan"), float("inf"), -5, 4.9, 15.1, 25, "unknown"):
            self.assertEqual(coach._normalized_tsb_target(value), 10)
        self.assertEqual(coach._normalized_tsb_target(5), 5)
        self.assertEqual(coach._normalized_tsb_target(12.5), 12.5)

    def test_post_race_phase_explicitly_marks_event_past(self) -> None:
        phase = coaching_contract.race_phase(datetime.date(2026, 8, 17))
        self.assertTrue(phase["is_past"])
        self.assertEqual(phase["phase"], "post-race")

    def test_triathlon_plan_is_not_reused_for_another_mode(self) -> None:
        with patch.object(coaching_contract, "current_mode", return_value="RUNNING"):
            result = plan.seed()
        self.assertIn("will not be reused across modes", result["error"])

    def test_visible_output_removes_em_dashes(self) -> None:
        self.assertEqual(coach._sanitize_visible_reply("• Fuel — bike first"), "• Fuel: bike first")
        self.assertEqual(nutrition._sanitize_visible_reply("TL;DR: yes — measured"), "TL;DR: yes: measured")

    def test_coach_context_receives_dated_self_reported_weight_and_lifting_status(self) -> None:
        live = {
            "readiness": {}, "load": {"activities": [], "by_sport": {}},
            "training_load": {}, "fitness": {}, "pmc": {}, "zones": {},
            "weight": {"kg": 85.4, "as_of": "2026-08-15", "source": "self-reported", "provider": "Garmin"},
        }
        now = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.timezone.utc)
        # app.config loads .env, so a machine with a real HEVY_API_KEY would
        # otherwise build a live connector and call the Hevy API from this test.
        previous = hevy_connector.connector()
        hevy_connector.reset()
        self.addCleanup(hevy_connector.configure, previous)
        with (
            patch.object(coach, "_live_context", return_value=live),
            patch.object(coach.config, "local_now", return_value=now),
            patch.object(coach.rings, "t100_readiness", return_value={}),
            patch.object(coach.suggest, "todays_suggestion", return_value={}),
            patch("app.baselines.get_baselines", return_value={}),
            patch("app.insights.get_insights", return_value={}),
            patch.object(coach.db, "get_plan", return_value=[]),
            patch.object(coach.db, "get_constraints", return_value=[]),
            patch.object(coach.db, "get_constraint_history", return_value=[]),
            patch.object(coach.db, "get_plan_day", return_value=None),
            # A lifting turn pulls a longer activity window for strength history.
            # Without this the test would make a real Garmin call.
            patch.object(coach, "_strength_load", return_value={"activities": [
                {"date": "2026-07-31", "sport": "strength", "name": "Weight Training",
                 "minutes": 48.0, "hr_avg": 99},
            ]}),
        ):
            payload = json.loads(coach._context_block("Build me a lifting routine"))
        self.assertEqual(payload["athlete_weight"]["kg"], 85.4)
        self.assertEqual(payload["athlete_weight"]["source"], "self-reported")
        self.assertEqual(payload["coaching_contract"]["current_mode"], "TRIATHLON")
        strength = payload["strength_training_source"]
        # Garmin saw the session even though Hevy holds no weights for it, and a
        # disconnected Hevy must never read as "the athlete has not been lifting".
        self.assertEqual(strength["session_evidence"]["last_session_date"], "2026-07-31")
        self.assertFalse(strength["weight_evidence"]["connected"])
        self.assertEqual(strength["weight_evidence"]["anchors"], [])
        self.assertTrue(strength["calibration_required"])


class FuelingContractTests(unittest.TestCase):
    def test_sport_food_wording_routes_to_fueling_context(self) -> None:
        self.assertTrue(fueling_reference.is_fueling_query("What should I eat on the bike?"))
        self.assertTrue(fueling_reference.is_fueling_query("What food should I carry?"))
        self.assertFalse(fueling_reference.is_fueling_query("What should I eat for dinner?"))

    def test_all_fixed_conversions_use_athlete_factors(self) -> None:
        self.assertEqual(fueling_reference.sodium_from_salt_tsp(1), 2360)
        self.assertEqual(fueling_reference.sodium_from_salt_tsp(0.5), 1180)
        self.assertEqual(fueling_reference.carb_from_sugar_tbsp(2), 25)
        self.assertEqual(fueling_reference.maple_from_tbsp(1), {
            "total_carb_g": 13.0, "glucose_g": 6.5, "fructose_g": 6.5,
        })
        self.assertEqual(fueling_reference.gel_totals(2), {"carb_g": 46.0, "caffeine_mg": 40.0})

    def test_under_sixty_minutes_has_no_intra_session_carbohydrate(self) -> None:
        result = nutrition.fueling_plan({"discipline": "run", "duration_min": 59})
        self.assertFalse(result["needed"])
        self.assertEqual(result["duration_min"], 59)
        self.assertEqual(result["duration_source"], "assumed coaching prescription")

    def test_missing_duration_stays_unknown(self) -> None:
        result = nutrition.fueling_plan({"discipline": "bike"})
        self.assertTrue(result["requires_input"])
        self.assertIsNone(result["known_inputs"]["duration_min"])
        self.assertIn("duration is unknown", result["note"].lower())

    def test_long_plan_is_checkable_and_inside_every_hard_limit(self) -> None:
        result = nutrition.fueling_plan({"discipline": "brick", "duration_min": 180,
                                         "duration_scope": "bike_leg"})
        self.assertEqual(result["carb_g_per_hr"], 75)
        self.assertEqual(result["glucose_g_per_hr"], 50)
        self.assertEqual(result["fructose_g_per_hr"], 25)
        self.assertEqual(result["ratio"], "2.0:1")
        self.assertEqual(result["drink_conc_pct"], 7.5)
        self.assertEqual(result["recipe_sodium_mg_per_hr"], 885)
        self.assertEqual(result["placement"], "Put carbohydrate fuel on the bike, not the run.")
        self.assertTrue(result["requires_input"])
        self.assertIsNone(result["finished_drink_volume_ml"])
        self.assertIsNone(result["plain_water_ml_per_hr"])
        self.assertTrue(any("No density is assumed" in line
                            for line in result["input_provenance"]))
        self.assertIn("abort", result)
        self.assertTrue(any("min(50, 60) + min(25, 30)" in line for line in result["arithmetic"]))

    def test_measured_drink_volume_resolves_plain_water_without_density_assumption(self) -> None:
        result = nutrition.fueling_plan({
            "discipline": "brick",
            "duration_min": 180,
            "duration_scope": "bike_leg",
            "finished_drink_volume_ml": 940,
            "finished_drink_volume_source": "measured",
        })
        self.assertFalse(result["requires_input"])
        self.assertEqual(result["finished_drink_mass_g"], 1000)
        self.assertEqual(result["finished_drink_volume_ml"], 940)
        self.assertEqual(result["plain_water_ml_per_hr"], 60)
        self.assertIn("1,000 ml/h - 940 ml/h = 60 ml/h".replace(",", ""),
                      "".join(result["arithmetic"]).replace(",", ""))

    def test_no_bike_race_does_not_ask_for_bike_duration(self) -> None:
        running = {
            "id": "marathon-2027", "mode": "RUNNING", "event": "Marathon",
            "date": "2027-05-01", "disciplines_and_distances": {"run_km": 42.195},
        }
        with patch.object(coaching_contract, "EVENT_PROFILE", running):
            result = nutrition.fueling_plan({"discipline": "race", "duration_min": 180})
        self.assertNotIn("bike-leg duration", result.get("note") or "")
        self.assertIn("no bike leg", result["placement"].lower())

    def test_food_logger_never_estimates_missing_macros(self) -> None:
        with patch.object(nutrition.db, "add_nutrition") as add:
            result = nutrition.log_food("chicken and rice for dinner")
        self.assertTrue(result["requires_input"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["input_provenance"]["macros"], "unknown")
        add.assert_not_called()

    def test_food_logger_preserves_explicit_adjacent_macro_values(self) -> None:
        text = "dinner 500 kcal 30g protein 80g carbs 20g fat"
        with (
            patch.object(nutrition.db, "add_nutrition", return_value=7) as add,
            patch.object(nutrition, "get_day", return_value={}),
        ):
            result = nutrition.log_food(text)
        self.assertTrue(result["ok"])
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["kcal"], 500)
        self.assertEqual(kwargs["protein_g"], 30)
        self.assertEqual(kwargs["carb_g"], 80)
        self.assertEqual(kwargs["fat_g"], 20)

    def test_photo_food_log_requires_exact_portion_and_macros(self) -> None:
        result = nutrition.log_photo("ZmFrZQ==")
        self.assertTrue(result["requires_input"])
        self.assertEqual(result["added"], 0)

    def test_exact_carb_total_can_log_while_fluid_split_is_unknown(self) -> None:
        session = {"discipline": "brick", "duration_min": 180, "title": "Long brick",
                   "duration_scope": "bike_leg"}
        with (
            patch.object(nutrition, "_today_session", return_value=session),
            patch.object(nutrition.db, "get_nutrition_by_description", return_value=None),
            patch.object(nutrition.db, "add_nutrition") as add,
            patch.object(nutrition, "get_day", return_value={}),
        ):
            result = nutrition.log_completed_fueling()
        self.assertTrue(result["added"])
        self.assertEqual(result["carb_g"], 225)
        self.assertEqual(add.call_args.kwargs["carb_g"], 225)

    def test_brick_total_duration_never_fuels_the_run(self) -> None:
        unresolved = nutrition.fueling_plan({"discipline": "brick", "duration_min": 200})
        self.assertTrue(unresolved["requires_input"])
        self.assertIn("bike-leg duration is unknown", unresolved["note"])
        resolved = nutrition.fueling_plan({
            "discipline": "brick", "duration_min": 200, "bike_duration_min": 180,
            "bike_duration_source": "derived from assumed coaching prescription",
            "bike_duration_arithmetic": "200 min total - 20 min run = 180 min bike",
        })
        self.assertEqual(resolved["duration_min"], 180)
        self.assertEqual(resolved["total_carb_g"], [225, 225])
        self.assertIn("200 min total - 20 min run", resolved["duration_arithmetic"])
        derived = nutrition.fueling_plan({
            "discipline": "brick", "duration_min": 200,
            "structure": {"run": "20 min run off the bike"},
        })
        self.assertEqual(derived["duration_min"], 180)
        self.assertEqual(derived["duration_source"],
                         "derived from assumed coaching prescription")
        self.assertIn("200 min total - 20 min run", derived["duration_arithmetic"])

    def test_explicit_duration_provenance_is_preserved(self) -> None:
        result = nutrition.fueling_plan({
            "discipline": "brick", "duration_min": 180,
            "duration_scope": "bike_leg", "duration_source": "self-reported",
        })
        self.assertEqual(result["duration_source"], "self-reported")

    def test_food_logger_preserves_decimal_label_first_values_without_crossing(self) -> None:
        text = "protein: 2.5g carbs: 10.5g fat: 3.5g 100 kcal"
        with (
            patch.object(nutrition.db, "add_nutrition", return_value=8) as add,
            patch.object(nutrition, "get_day", return_value={}),
        ):
            result = nutrition.log_food(text)
        self.assertTrue(result["ok"])
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["kcal"], 100)
        self.assertEqual(kwargs["protein_g"], 2.5)
        self.assertEqual(kwargs["carb_g"], 10.5)
        self.assertEqual(kwargs["fat_g"], 3.5)
        self.assertEqual(nutrition._logged_totals([kwargs]), {
            "kcal": 100, "protein_g": 2.5, "carb_g": 10.5, "fat_g": 3.5,
        })

    def test_race_plan_does_not_apply_whole_event_duration_to_bike_fuel(self) -> None:
        result = nutrition.fueling_plan({"discipline": "race", "duration_min": 345})
        self.assertTrue(result["requires_input"])
        self.assertIsNone(result["known_inputs"]["bike_duration_min"])
        self.assertIn("unknown", result["known_inputs"]["bike_duration_source"])


class GarminWeightTests(unittest.TestCase):
    def tearDown(self) -> None:
        garmin_source._WEIGHT_CACHE.update({"val": None, "ts": 0.0})

    def test_latest_dated_garmin_weight_is_self_reported_and_converted(self) -> None:
        fake = type("Garmin", (), {"get_body_composition": lambda self, _start, _end: {
            "dateWeightList": [
                {"calendarDate": "2026-08-10", "weight": 87100},
                {"calendarDate": "2026-08-15", "weight": 86000},
            ]
        }})()
        garmin_source._WEIGHT_CACHE.update({"val": None, "ts": 0.0})
        with patch.object(garmin_source, "get_client", return_value=fake):
            result = garmin_source.get_weight_kg()
        self.assertEqual(result["kg"], 86.0)
        self.assertEqual(result["as_of"], "2026-08-15")
        self.assertEqual(result["source"], "self-reported")
        self.assertEqual(result["provider"], "Garmin")
        self.assertIn("86000 g / 1000", result["conversion"])
        self.assertIn("86 kg x 2.2046 lb/kg = 189.6 lb", result["conversion"])

    def test_missing_garmin_weight_uses_labelled_self_reported_fallback(self) -> None:
        with patch.object(nutrition.garmin_source, "get_weight_kg", return_value=None):
            result = nutrition.weight_info()
        self.assertEqual(result["kg"], 86.0)
        self.assertEqual(result["source"], "self-reported")
        self.assertIn("unknown", result["fallback_reason"])
        self.assertEqual(result["conversion"], "86 kg x 2.2046 lb/kg = 189.6 lb")

    def test_missing_garmin_weight_is_cached_for_five_minutes(self) -> None:
        fake = type("Garmin", (), {
            "calls": 0,
            "get_body_composition": lambda self, _start, _end: (
                setattr(self, "calls", self.calls + 1) or {"dateWeightList": []}
            ),
        })()
        with patch.object(garmin_source, "get_client", return_value=fake):
            self.assertIsNone(garmin_source.get_weight_kg())
            self.assertIsNone(garmin_source.get_weight_kg())
        self.assertEqual(fake.calls, 1)

    def test_undated_garmin_weight_is_not_presented_as_current(self) -> None:
        fake = type("Garmin", (), {"get_body_composition": lambda self, _start, _end: {
            "dateWeightList": [{"weight": 85000}],
        }})()
        with patch.object(garmin_source, "get_client", return_value=fake):
            self.assertIsNone(garmin_source.get_weight_kg())

    def test_weight_conversion_uses_the_same_displayed_kg_value(self) -> None:
        fake = type("Garmin", (), {"get_body_composition": lambda self, _start, _end: {
            "dateWeightList": [{"calendarDate": "2026-08-15", "weight": 85450}],
        }})()
        with patch.object(garmin_source, "get_client", return_value=fake):
            result = garmin_source.get_weight_kg()
        self.assertEqual(result["kg"], 85.45)
        self.assertEqual(result["lb"], 188.4)
        self.assertIn("85.45 kg x 2.2046 lb/kg = 188.4 lb", result["conversion"])


class PacingAndHevyTests(unittest.TestCase):
    def tearDown(self) -> None:
        hevy_connector.reset()

    def test_race_specific_watch_targets_use_event_hr_not_fitness(self) -> None:
        bike = garmin_workout._hr_for("race pace", "bike")
        run = garmin_workout._hr_for("race pace", "run")
        self.assertEqual((bike["targetValueOne"], bike["targetValueTwo"]), (140.0, 150.0))
        self.assertEqual((run["targetValueOne"], run["targetValueTwo"]), (152.0, 158.0))

    def test_outdoor_bike_plan_never_falls_back_to_watts(self) -> None:
        with patch("app.zones.hr_range", return_value=None):
            target = plan._hr("threshold")
        self.assertIn("HR unknown", target)
        self.assertNotIn(" W", target)

    def test_event_row_refresh_preserves_calendar_placement(self) -> None:
        existing = {
            "date": "2026-08-16", "week_index": 5, "phase": "taper", "source": "seed",
            "start_time": "06:30", "gcal_event_id": "event-1", "pos_updated_at": "stamp",
        }
        with (
            patch.object(plan.db, "get_plan_day", return_value=existing),
            patch.object(plan.db, "upsert_plan_day") as upsert,
        ):
            self.assertTrue(plan.reconcile_event_day())
        refreshed = upsert.call_args.args[0]
        self.assertEqual(refreshed["duration_min"], 345)
        self.assertEqual(refreshed["tsb_target"], 10)
        self.assertEqual(refreshed["start_time"], "06:30")
        self.assertEqual(refreshed["gcal_event_id"], "event-1")

    def test_disconnected_hevy_history_is_unknown_and_writes_fail_closed(self) -> None:
        # app.config loads .env, so a machine with a real HEVY_API_KEY would
        # otherwise build a live connector and call the Hevy API from this test.
        previous = hevy_connector.connector()
        hevy_connector.reset()
        self.addCleanup(hevy_connector.configure, previous)
        context = hevy_connector.context_for("Build me a lifting routine")
        self.assertFalse(context["connection"]["connected"])
        self.assertEqual(context["recent_workouts"], "unknown")
        self.assertIsNone(hevy_connector.context_for("How was my swim?"))
        with self.assertRaises(hevy_connector.HevyUnavailableError):
            hevy_connector.connector().create_routine({}, idempotency_key="test")

    def test_hevy_routing_ignores_body_weight_and_generic_plan_set_language(self) -> None:
        self.assertIsNone(hevy_connector.context_for("What is my Garmin weight?"))
        self.assertIsNone(hevy_connector.context_for("Set my plan for next week"))
        self.assertIsNotNone(hevy_connector.context_for("Build a strength routine with sets and reps"))
        for request in ("Build me a leg day", "Make me a push day",
                        "Program a dumbbell workout", "Train upper body",
                        "Program me a weights workout", "use weights"):
            self.assertIsNotNone(hevy_connector.context_for(request), request)


if __name__ == "__main__":
    unittest.main()
