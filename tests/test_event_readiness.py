from __future__ import annotations

import unittest

from app import event_readiness as er


MARATHON = {"event_name": "TCS Toronto Waterfront Marathon", "mode": "MARATHON",
            "distances": {"run_km": 42.2}}
T100 = {"event_name": "T100 Vancouver", "mode": "TRIATHLON",
        "distances": {"swim_km": 2.0, "bike_km": 80.0, "run_km": 18.0}}


def _load(**km) -> dict:
    return {"by_sport": {sport: {"km": value} for sport, value in km.items()}}


class LabelTests(unittest.TestCase):
    def test_a_marathon_profile_is_labelled_marathon(self) -> None:
        self.assertEqual(er.event_label(MARATHON), "Marathon")

    def test_a_half_marathon_is_not_swallowed_by_the_marathon_rule(self) -> None:
        self.assertEqual(er.event_label(
            {"event_name": "Some Half Marathon", "distances": {"run_km": 21.1}}),
            "Half marathon")

    def test_the_t100_profile_keeps_its_own_name(self) -> None:
        self.assertEqual(er.event_label(T100), "T100")

    def test_common_race_shapes_are_recognised(self) -> None:
        cases = {
            "Oceanside 70.3": "70.3",
            "IRONMAN Canada": "Ironman",
            "Vancouver 10k": "10K",
            "Parkrun 5K": "5K",
            "Western States Ultra": "Ultra",
        }
        for name, expected in cases.items():
            self.assertEqual(er.event_label({"event_name": name}), expected, name)

    def test_a_run_only_event_is_named_from_its_distance(self) -> None:
        self.assertEqual(er.event_label({"event_name": "Club Champs",
                                         "distances": {"run_km": 42.2}}), "Marathon")

    def test_an_unknown_event_still_gets_a_usable_label(self) -> None:
        self.assertTrue(er.event_label({"event_name": "Some Odd Race"}))
        self.assertTrue(er.event_label(None))

    def test_a_label_stays_short_enough_for_the_ring(self) -> None:
        long_name = {"event_name": "The Extremely Long Named Charity Race Of 2026"}
        self.assertLessEqual(len(er.event_label(long_name)), 18)


class DistanceTests(unittest.TestCase):
    def test_distances_are_read_from_either_profile_shape(self) -> None:
        legacy = {"disciplines_and_distances": {"run_km": 42.2}}
        self.assertEqual(er.race_distances(legacy), {"run": 42.2})

    def test_zero_and_negative_distances_are_ignored(self) -> None:
        self.assertEqual(er.race_distances({"distances": {"run_km": 0, "bike_km": -5}}), {})

    def test_a_profile_without_distances_yields_nothing(self) -> None:
        for bad in (None, {}, {"distances": "far"}, 42):
            self.assertEqual(er.race_distances(bad), {})


class ScoringTests(unittest.TestCase):
    def _score(self, profile, load, **kw):
        return er.readiness(load14=load, training_load=kw.get("tl", {"load_ratio": 1.0}),
                            readiness_score=kw.get("score", 70), days_left=30,
                            profile=profile)

    def test_a_marathon_is_scored_against_run_volume_alone(self) -> None:
        result = self._score(MARATHON, _load(run=42.2))
        self.assertTrue(result["available"])
        self.assertEqual(result["event_label"], "Marathon")
        self.assertIn("run", result["components"])
        # Absent swim and bike must not drag a run-only event down.
        self.assertNotIn("swim", result["components"])

    def test_a_run_only_event_can_still_reach_race_ready(self) -> None:
        # 42.2 km of running per fortnight meets the derived target exactly.
        result = self._score(MARATHON, _load(run=60), score=95, tl={"load_ratio": 1.0})
        self.assertGreaterEqual(result["score"], 85)
        self.assertEqual(result["label"], "Race ready")

    def test_the_target_comes_from_the_event_not_a_fixed_number(self) -> None:
        marathon = self._score(MARATHON, _load(run=20))["components"]["run"]["target"]
        half = self._score({"distances": {"run_km": 21.1}}, _load(run=20))["components"]["run"]["target"]
        self.assertGreater(marathon, half)

    def test_a_triathlon_scores_every_discipline(self) -> None:
        result = self._score(T100, _load(swim=8, bike=170, run=18))
        self.assertEqual(set(result["components"]) - {"load_balance"},
                         {"swim", "bike", "run"})

    def test_the_weakest_discipline_is_reported(self) -> None:
        result = self._score(T100, _load(swim=8, bike=170, run=2))
        self.assertEqual(result["lowest_volume_bucket"], "run")

    def test_a_poor_load_ratio_lowers_the_score(self) -> None:
        good = self._score(MARATHON, _load(run=60), tl={"load_ratio": 1.0})["score"]
        bad = self._score(MARATHON, _load(run=60), tl={"load_ratio": 1.9})["score"]
        self.assertLess(bad, good)

    def test_a_missing_load_ratio_is_treated_as_unknown_not_perfect(self) -> None:
        known = self._score(MARATHON, _load(run=60), tl={"load_ratio": 1.0})["score"]
        unknown = self._score(MARATHON, _load(run=60), tl={})["score"]
        self.assertLess(unknown, known)

    def test_an_event_without_distances_reports_unavailable(self) -> None:
        result = self._score({"event_name": "Mystery"}, _load(run=40))
        self.assertFalse(result["available"])
        self.assertIn("no usable race distances", result["reason"])

    def test_hostile_input_does_not_raise(self) -> None:
        for bad in (None, 42, "load", {}, {"by_sport": "nope"}):
            result = er.readiness(load14=bad, training_load=bad, readiness_score=bad,
                                  days_left=bad, profile=MARATHON)
            self.assertIn("available", result)

    def test_the_score_stays_within_bounds(self) -> None:
        for load in (_load(run=0), _load(run=10000)):
            for score in (0, 100, None):
                result = self._score(MARATHON, load, score=score)
                self.assertGreaterEqual(result["score"], 0)
                self.assertLessEqual(result["score"], 100)


if __name__ == "__main__":
    unittest.main()
