from __future__ import annotations

import unittest
from unittest.mock import patch

from app import (activity_detail, athlete_guide, coaching_contract, config, fueling_reference,
                 insights, plan, ring_detail, rings)


class EventProfileIsolationTests(unittest.TestCase):
    def _running_profile(self) -> dict:
        return {
            "id": "road-10k-2027",
            "mode": "RUNNING",
            "event": "Road 10K",
            "date": "2027-04-01",
            "disciplines_and_distances": {"run_km": 10},
            "goal": {},
        }

    def test_optional_triathlon_distances_are_safe_and_unknown(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            self.assertEqual(config.event_distance_km("run"), 10.0)
            self.assertIsNone(config.event_distance_km("swim"))
            self.assertIsNone(config.event_distance_km("bike"))
            self.assertFalse(config.supports_t100_features())

    def test_the_t100_model_is_gated_outside_its_installed_profile(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            readiness = rings.t100_readiness({}, {}, None, 20)
        self.assertFalse(readiness["available"])
        self.assertNotIn("components", readiness)

    def test_the_readiness_detail_serves_the_active_event_rather_than_erroring(self) -> None:
        # It used to return "T100 readiness is unavailable" for any other event,
        # so switching to a marathon left the ring on screen with a dead sheet.
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            with patch.object(ring_detail.garmin_source, "get_recent_load",
                              return_value={"by_sport": {"run": {"km": 30.0}},
                                            "activities": [{"sport": "run", "km": 12.0}]}), \
                 patch.object(ring_detail.garmin_source, "get_training_load", return_value={}), \
                 patch.object(ring_detail.garmin_source, "get_readiness", return_value={}):
                detail = ring_detail._event_ready()
        self.assertNotIn("error", detail)
        self.assertIn("readiness", detail["title"].lower())
        self.assertIsInstance(detail["score"], int)

    def test_t100_plan_is_not_reused_for_a_different_event_profile(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            result = plan.seed()
            reconciled = plan.reconcile_event_day()
        self.assertIn("will not be reused across profiles", result["error"])
        self.assertFalse(reconciled)

    def test_non_bike_event_gets_front_load_rule_not_bike_placement(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            context = fueling_reference.context()
            rules = context["fuel_audit_contract"]
        placement = next(rule for rule in rules if "Front-load" in rule)
        self.assertIn("no stated bike leg", placement)
        self.assertNotIn("put carbohydrate fuel on the bike", placement)
        self.assertFalse(any("T100" in source for source in context["sources"]))

    def test_activity_analysis_and_cache_are_event_profile_specific(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            prompt = activity_detail._analyze_prompt()
            cache_key = activity_detail._analysis_cache_key(42)
        self.assertNotIn("T100", prompt)
        self.assertIn("Do not import distances", prompt)
        self.assertIn("road_10k_2027", cache_key)

    def test_stale_t100_payload_cannot_create_cross_event_insight(self) -> None:
        stale = {
            "t100": {
                "lowest_volume_bucket": "swim",
                "components": {"swim": {"pct": 10}},
            }
        }
        with (
            patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True),
            patch.object(config, "race_phase", return_value={"phase": "build", "days_remaining": 30}),
            patch("app.db.get_wellness", return_value=[]),
        ):
            result = insights.get_insights(
                baseline_data={}, pmc_data={}, training_load_data={}, rings_data=stale,
            )
        joined = " ".join(signal["detail"] for signal in result["signals"])
        self.assertNotIn("T100", joined)
        self.assertNotIn("14-day target", joined)

    def test_vancouver_guide_is_unavailable_outside_its_profile(self) -> None:
        with patch.dict(coaching_contract.EVENT_PROFILE, self._running_profile(), clear=True):
            self.assertIsNone(athlete_guide.context_for("show me the bike course and aid stations"))


if __name__ == "__main__":
    unittest.main()
