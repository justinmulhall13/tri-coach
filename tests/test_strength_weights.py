from __future__ import annotations

import unittest

from app import strength_weights as sw


# Exact values returned by the Hevy API for this athlete's logged sets. Each is
# a pound value that survived a lb -> kg -> float round trip.
REAL_KG_TO_LB = {
    2.267964547178199: 5.0,
    4.535929094356398: 10.0,
    5.669911367945497: 12.5,
    6.8038936415345965: 15.0,
    10.205840462301894: 22.5,
    12.473805009480093: 27.5,
    15.875751830247390: 35.0,
    22.679645471781990: 50.0,
    34.019468207672980: 75.0,
    61.235042773811365: 135.0,
    102.058404623018940: 225.0,
    142.881766472226530: 315.0,
    145.149731019404730: 320.0,
}


class UnitConversionTests(unittest.TestCase):
    def test_every_real_hevy_weight_snaps_to_the_logged_pound_value(self) -> None:
        for kg, expected_lb in REAL_KG_TO_LB.items():
            self.assertEqual(sw.history_weight_lb(kg), expected_lb, f"{kg} kg")

    def test_snapping_never_merges_two_genuinely_different_loads(self) -> None:
        self.assertNotEqual(sw.history_weight_lb(sw.lb_to_kg(135.0)),
                            sw.history_weight_lb(sw.lb_to_kg(136.0)))

    def test_a_true_kilogram_load_is_left_alone_rather_than_forced_to_pounds(self) -> None:
        # 100 kg is 220.462 lb, which is not a half-pound value and must not be
        # silently snapped to 220.5.
        self.assertAlmostEqual(sw.history_weight_lb(100.0), 220.46226, places=4)

    def test_non_finite_weight_is_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), None, "135", True):
            with self.assertRaises(sw.WeightDerivationError):
                sw.snap_lb(bad)


class IncrementInferenceTests(unittest.TestCase):
    def test_dumbbell_history_with_half_steps_infers_two_and_a_half_pounds(self) -> None:
        self.assertEqual(sw.infer_increment_lb([12.5, 22.5, 27.5, 35.0]), 2.5)

    def test_barbell_history_infers_five_pounds(self) -> None:
        self.assertEqual(sw.infer_increment_lb([135.0, 225.0, 315.0, 320.0]), 5.0)

    def test_empty_history_falls_back_to_a_safe_common_increment(self) -> None:
        self.assertEqual(sw.infer_increment_lb([]), 2.5)

    def test_rounding_ties_go_down_so_load_is_never_invented(self) -> None:
        self.assertEqual(sw.round_to_increment(137.5, 5.0), 135.0)
        self.assertEqual(sw.round_to_increment(138.0, 5.0), 140.0)

    def test_rounding_never_returns_zero_or_negative_load(self) -> None:
        self.assertEqual(sw.round_to_increment(0.4, 5.0), 5.0)


class DerivationTests(unittest.TestCase):
    ANCHOR_KG = 142.881766472226530  # 315 lb barbell squat, 2026-06-09

    def _derive(self, pct: float, increment: float = 5.0) -> dict:
        return sw.derive_working_weight(
            anchor_weight_kg=self.ANCHOR_KG, anchor_reps=5, anchor_date="2026-06-09",
            exercise_template_id="D04AC939", pct=pct, increment_lb=increment,
        )

    def test_a_deload_derives_a_real_loadable_pound_value(self) -> None:
        result = self._derive(0.85)
        self.assertEqual(result["weight_lb"], 270.0)  # 315 * 0.85 = 267.75 -> nearest 5
        self.assertEqual(result["weight_provenance"], "hevy_derived")
        self.assertEqual(result["derivation"]["anchor_weight_lb"], 315.0)

    def test_the_explanation_states_the_evidence_in_pounds(self) -> None:
        text = self._derive(0.85)["explanation"]
        self.assertIn("270 lb", text)
        self.assertIn("315 lb", text)
        self.assertIn("2026-06-09", text)

    def test_derived_kilograms_round_trip_back_to_the_prescribed_pounds(self) -> None:
        result = self._derive(0.9)
        self.assertEqual(sw.history_weight_lb(result["weight_kg"]), result["weight_lb"])

    def test_a_percentage_outside_the_safe_band_is_refused(self) -> None:
        for pct in (0.2, 0.59, 1.06, 1.5):
            with self.assertRaises(sw.WeightDerivationError):
                self._derive(pct)

    def test_a_non_finite_percentage_is_refused(self) -> None:
        with self.assertRaises(sw.WeightDerivationError):
            self._derive(float("nan"))


class VerificationTests(unittest.TestCase):
    ANCHOR_KG = 142.881766472226530

    def setUp(self) -> None:
        self.history = {("D04AC939", round(self.ANCHOR_KG, 4)): {"reps": 5}}
        self.derived = sw.derive_working_weight(
            anchor_weight_kg=self.ANCHOR_KG, anchor_reps=5, anchor_date="2026-06-09",
            exercise_template_id="D04AC939", pct=0.85, increment_lb=5.0,
        )

    def test_a_sound_derivation_verifies(self) -> None:
        self.assertIsNone(sw.verify_derivation(
            self.derived["derivation"], self.history,
            claimed_weight_kg=self.derived["weight_kg"],
        ))

    def test_an_anchor_absent_from_history_is_rejected(self) -> None:
        error = sw.verify_derivation(
            {**self.derived["derivation"], "anchor_weight_kg": sw.lb_to_kg(405.0)},
            self.history, claimed_weight_kg=self.derived["weight_kg"],
        )
        self.assertIsNotNone(error)
        self.assertIn("not found in fetched Hevy history", error)

    def test_a_weight_that_contradicts_its_own_derivation_is_rejected(self) -> None:
        error = sw.verify_derivation(
            self.derived["derivation"], self.history,
            claimed_weight_kg=sw.lb_to_kg(315.0),  # claims a deload, ships the full anchor
        )
        self.assertIsNotNone(error)
        self.assertIn("does not match its own derivation", error)

    def test_an_out_of_band_percentage_is_rejected_at_verification_too(self) -> None:
        error = sw.verify_derivation(
            {**self.derived["derivation"], "pct": 1.4}, self.history,
            claimed_weight_kg=sw.lb_to_kg(441.0),
        )
        self.assertIsNotNone(error)
        self.assertIn("safe band", error)

    def test_a_missing_derivation_record_is_rejected(self) -> None:
        for bad in (None, "hevy_history", 42, []):
            self.assertIsNotNone(sw.verify_derivation(
                bad, self.history, claimed_weight_kg=self.ANCHOR_KG))


if __name__ == "__main__":
    unittest.main()


class MachineStackTests(unittest.TestCase):
    """Selection stacks whose real loads fit no standard increment.

    This athlete's calf machine produced 192, 332 and 392 lb: not multiples of
    2.5 or 5. A computed step would prescribe a pin position that cannot exist.
    """

    OBSERVED = [192.0, 332.0, 392.0]
    ANCHOR_KG = 177.80842049877077  # 392 lb

    def _derive(self, pct: float) -> dict:
        return sw.derive_working_weight(
            anchor_weight_kg=self.ANCHOR_KG, anchor_reps=6, anchor_date="2026-06-16",
            exercise_template_id="47B9DF13", pct=pct,
            increment_lb=sw.infer_increment_lb(self.OBSERVED),
            observed_lb=self.OBSERVED,
        )

    def test_nearest_observed_picks_a_real_pin_position(self) -> None:
        self.assertEqual(sw.nearest_observed_lb(300.0, self.OBSERVED), 332.0)
        self.assertEqual(sw.nearest_observed_lb(200.0, self.OBSERVED), 192.0)

    def test_nearest_observed_breaks_ties_toward_the_lighter_load(self) -> None:
        self.assertEqual(sw.nearest_observed_lb(262.0, [192.0, 332.0]), 192.0)

    def test_no_standard_increment_explains_this_machine(self) -> None:
        for increment in (2.5, 5.0, 10.0):
            self.assertFalse(sw.increment_explains(self.OBSERVED, increment))

    def test_a_derived_machine_weight_is_one_the_stack_has_produced(self) -> None:
        result = self._derive(0.85)
        self.assertIn(result["weight_lb"], self.OBSERVED)
        self.assertEqual(result["derivation"]["rounding"], "nearest_observed")

    def test_the_explanation_says_the_weight_was_snapped_to_real_history(self) -> None:
        self.assertIn("actually produced", self._derive(0.85)["explanation"])

    def test_a_barbell_with_a_clean_increment_still_uses_increment_rounding(self) -> None:
        result = sw.derive_working_weight(
            anchor_weight_kg=142.881766472226530, anchor_reps=5, anchor_date="2026-06-09",
            exercise_template_id="D04AC939", pct=0.85, increment_lb=5.0,
            observed_lb=[135.0, 225.0, 315.0],
        )
        self.assertEqual(result["derivation"]["rounding"], "increment")
        self.assertEqual(result["weight_lb"], 270.0)

    def test_a_snapped_weight_verifies_against_the_same_history(self) -> None:
        result = self._derive(0.85)
        history = {("47B9DF13", round(sw.lb_to_kg(lb), 4)): {} for lb in self.OBSERVED}
        history[("47B9DF13", round(self.ANCHOR_KG, 4))] = {"reps": 6}
        self.assertIsNone(sw.verify_derivation(
            result["derivation"], history, claimed_weight_kg=result["weight_kg"]))

    def test_self_consistency_is_still_enforced_for_snapped_weights(self) -> None:
        result = self._derive(0.85)
        history = {("47B9DF13", round(sw.lb_to_kg(lb), 4)): {} for lb in self.OBSERVED}
        history[("47B9DF13", round(self.ANCHOR_KG, 4))] = {"reps": 6}
        # Claims an 85% snap but ships the full 392 lb anchor.
        error = sw.verify_derivation(
            result["derivation"], history, claimed_weight_kg=self.ANCHOR_KG)
        self.assertIsNotNone(error)
        self.assertIn("does not match its own derivation", error)
