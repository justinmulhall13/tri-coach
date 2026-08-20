from __future__ import annotations

import unittest

from app import strength_knowledge as sk


class RelevanceTests(unittest.TestCase):
    def test_pain_language_is_recognised(self) -> None:
        for text in ("my shoulder hurts when I press", "knee is sore after the run",
                     "achilles niggle", "lower back tight", "that tweaked my hamstring"):
            self.assertTrue(sk.mentions_pain(text), text)

    def test_ordinary_training_talk_is_not_treated_as_pain(self) -> None:
        for text in ("what is my long run", "how did my swim go", "build me a week"):
            self.assertFalse(sk.mentions_pain(text), text)

    def test_programming_questions_pull_the_reference_without_pain(self) -> None:
        self.assertTrue(sk.is_relevant("should I deload this week"))
        self.assertTrue(sk.is_relevant("how do I maintain strength in a marathon block"))

    def test_an_unrelated_question_carries_no_reference(self) -> None:
        self.assertIsNone(sk.context_for("what should I eat before the bike"))

    def test_regions_are_identified_from_natural_language(self) -> None:
        self.assertIn("knee", sk.regions_mentioned("my left knee aches going downstairs"))
        self.assertIn("achilles", sk.regions_mentioned("achilles is grumpy"))
        self.assertIn("lower_back", sk.regions_mentioned("low back is stiff"))


class ContextTests(unittest.TestCase):
    def test_pain_turns_carry_the_monitoring_rule_and_referral_triggers(self) -> None:
        context = sk.context_for("my knee hurts when I squat")
        self.assertIn("pain_monitoring", context)
        self.assertTrue(context["referral_triggers"])
        self.assertIn("knee", context["regions"])

    def test_a_programming_turn_carries_no_referral_noise(self) -> None:
        context = sk.context_for("how should I progress my squat")
        self.assertNotIn("referral_triggers", context)
        self.assertTrue(context["load_progression"])

    def test_the_standing_shoulder_problem_is_always_present(self) -> None:
        # Advice about a pressing day should know about the shoulder unprompted.
        context = sk.context_for("should I deload this week")
        self.assertIn("shoulder", context["regions"])

    def test_the_reference_refuses_to_diagnose(self) -> None:
        context = sk.context_for("my shoulder hurts")
        self.assertIn("not a diagnosis", context["role"])
        self.assertTrue(any("Never name a diagnosis" in r
                            for r in context["rules_of_engagement"]))

    def test_bone_stress_is_called_out_as_a_referral_trigger(self) -> None:
        triggers = " ".join(sk.context_for("my shin hurts")["referral_triggers"])
        self.assertIn("bone stress", triggers.lower())

    def test_concurrent_training_guidance_is_available_to_a_runner(self) -> None:
        context = sk.context_for("how do I maintain strength while marathon training")
        joined = " ".join(context["concurrent_training"]).lower()
        self.assertIn("maintenance", joined)
        self.assertIn("running economy", joined)


class SubstitutionTests(unittest.TestCase):
    def test_a_provocative_press_has_a_shoulder_friendly_swap(self) -> None:
        self.assertEqual(sk.substitute("Overhead Press", "shoulder"),
                         "Half Kneeling Landmine Press")

    def test_the_banned_face_pull_maps_to_the_athletes_replacement(self) -> None:
        self.assertEqual(sk.substitute("Face Pull", "shoulder"), "Rear Delt Reverse Fly")

    def test_a_knee_swap_keeps_the_training_goal(self) -> None:
        self.assertTrue(sk.substitute("Front Squat", "knee"))

    def test_an_unknown_movement_returns_nothing_rather_than_guessing(self) -> None:
        self.assertIsNone(sk.substitute("Kettlebell Windmill", "shoulder"))
        self.assertIsNone(sk.substitute("Squat", "elbow"))

    def test_matching_tolerates_equipment_qualifiers(self) -> None:
        self.assertTrue(sk.substitute("Overhead Press (Dumbbell)", "shoulder"))

    def test_hostile_input_does_not_raise(self) -> None:
        for bad in (None, 42, True, [], {}, object()):
            self.assertFalse(sk.mentions_pain(bad))
            self.assertEqual(sk.regions_mentioned(bad), [])
            self.assertIsNone(sk.substitute(bad, bad))


if __name__ == "__main__":
    unittest.main()
