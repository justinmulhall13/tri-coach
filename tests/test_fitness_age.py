"""Fitness age is Garmin's when it has one, a labelled estimate when it does not."""
from __future__ import annotations

import datetime
import json
import unittest
from unittest.mock import MagicMock, patch

from app import coaching_contract, config, fitness_age, garmin_source, main

_GARMIN = {
    "chronologicalAge": 34, "fitnessAge": 27.44, "achievableFitnessAge": 25.02,
    "previousFitnessAge": 27.9, "lastUpdated": "2026-11-08T06:10:00.0",
    "components": {
        "rhr": {"value": 47, "targetValue": 45, "potentialAge": 26.9, "priority": 2},
        "vigorousDaysAvg": {"value": 4.2, "targetValue": 5, "potentialAge": 26.1, "priority": 1},
        "bmi": {"value": 23.4, "stale": True, "priority": 3},
        "broken": {"value": None},
    },
}


class NormaliseTests(unittest.TestCase):
    def test_garmin_number_is_passed_through_with_its_parts(self) -> None:
        out = fitness_age.normalise(_GARMIN, age=19, sex="male", vo2=55.8)
        self.assertEqual(out["fitness_age"], 27.4)
        self.assertEqual(out["chronological_age"], 34)       # Garmin's, not the fallback
        self.assertEqual(out["achievable_age"], 25.0)
        self.assertEqual(out["delta_years"], 6.6)
        self.assertEqual(out["as_of"], "2026-11-08")
        self.assertIsNone(out["reason"])
        self.assertIsNone(out["estimate"])                   # Garmin's own number wins
        self.assertEqual([c["key"] for c in out["components"]], ["vigorousDaysAvg", "rhr", "bmi"])
        self.assertEqual(out["components"][0]["label"], "Vigorous days a week")
        self.assertTrue(out["components"][2]["stale"])

    def test_list_answer_uses_the_newest_record(self) -> None:
        old = {**_GARMIN, "fitnessAge": 30.0, "lastUpdated": "2026-10-01T00:00:00.0"}
        self.assertEqual(fitness_age.normalise([old, _GARMIN])["fitness_age"], 27.4)

    def test_under_20_gets_a_reason_and_a_labelled_estimate(self) -> None:
        out = fitness_age.normalise({"invalidReason": "USER_UNDER_AGE"}, age=19, sex="male",
                                    vo2=55.8, vo2_bike=58.0, vo2_as_of="2026-11-08")
        self.assertIsNone(out["fitness_age"])                # Garmin's field stays Garmin's
        est = out["estimate"]
        self.assertEqual((est["age"], est["floor"], est["ceiling"]), (20, True, False))
        self.assertEqual(est["method"], "vo2max_norms")
        self.assertEqual(est["source"], "estimated")         # the app's number, not Garmin's
        self.assertIsNone(est["provider"])
        self.assertEqual(est["vo2max"], 55.8)
        self.assertEqual(est["typical_vo2max_at_age"], 44.1)
        self.assertIsNone(est["delta_years"])                # 19 is below the scale
        self.assertIsNone(out["delta_years"])
        self.assertEqual(out["components"], [])
        self.assertEqual(out["reason"], "under_age")
        self.assertEqual(out["reason_text"], "Garmin gives a fitness age from 20.")
        self.assertEqual(out["chronological_age"], 19)
        vo2 = out["vo2max"]
        self.assertEqual((vo2["value"], vo2["rating"], vo2["rank"]), (55.8, "Superior", "top 5%"))
        self.assertEqual(vo2["group"], "men 20 to 29")
        self.assertEqual((vo2["bike"], vo2["as_of"]), (58.0, "2026-11-08"))
        # The value is Garmin's reading; the band it lands in is read off a table here.
        self.assertEqual((vo2["rating_source"], vo2["rating_method"]),
                         ("derived", "cooper_institute_bands"))

    def test_nothing_from_garmin_is_unavailable(self) -> None:
        out = fitness_age.normalise(None)
        self.assertEqual(out["reason"], "unavailable")
        self.assertIsNone(out["vo2max"])
        self.assertIsNone(out["estimate"])                   # no VO2max, no number


class EstimateTests(unittest.TestCase):
    def test_typical_vo2max_of_a_decade_reads_its_middle_age(self) -> None:
        # Middle of Fair for men 40 to 49 is (38.5 + 42.4) / 2.
        est = fitness_age.estimate_from_vo2(40.45, "male", chronological=30)
        self.assertEqual((est["age"], est["floor"], est["ceiling"]), (44, False, False))
        self.assertEqual(est["delta_years"], -14)
        self.assertEqual(fitness_age.estimate_from_vo2(42.25, "male")["age"], 34)
        self.assertEqual(fitness_age.estimate_from_vo2(41.35, "male")["age"], 40)   # halfway between

    def test_women_use_their_own_table(self) -> None:
        self.assertEqual(fitness_age.estimate_from_vo2(36.1, "female")["age"], 34)
        self.assertTrue(fitness_age.estimate_from_vo2(45.0, "female")["floor"])

    def test_estimate_only_gets_older_as_vo2max_falls(self) -> None:
        for sex in ("male", "female"):
            ages = [fitness_age.estimate_from_vo2(v / 2, sex)["age"] for v in range(130, 40, -1)]
            self.assertEqual(ages, sorted(ages))
            self.assertEqual((ages[0], ages[-1]), (20, 79))

    def test_ends_of_the_scale_are_flagged_not_extrapolated(self) -> None:
        high = fitness_age.estimate_from_vo2(70, "male", chronological=34)
        self.assertEqual((high["age"], high["floor"], high["delta_years"]), (20, True, 14))
        low = fitness_age.estimate_from_vo2(20, "male")
        self.assertEqual((low["age"], low["ceiling"]), (79, True))

    def test_no_measurement_or_no_table_means_no_estimate(self) -> None:
        self.assertIsNone(fitness_age.estimate_from_vo2(None, "male"))
        self.assertIsNone(fitness_age.estimate_from_vo2(0, "male"))
        self.assertIsNone(fitness_age.estimate_from_vo2(55.8, None))
        self.assertIsNone(fitness_age.estimate_from_vo2(True, "male"))


class StandingTests(unittest.TestCase):
    def test_band_edges_belong_to_the_higher_band(self) -> None:
        rate = lambda v, age=25, sex="male": fitness_age.vo2_standing(v, age, sex)["rating"]
        self.assertEqual(rate(41.6), "Poor")
        self.assertEqual(rate(41.7), "Fair")
        self.assertEqual(rate(45.4), "Good")
        self.assertEqual(rate(51.1), "Excellent")
        self.assertEqual(rate(55.4), "Superior")

    def test_age_and_sex_pick_the_row(self) -> None:
        self.assertEqual(fitness_age.vo2_standing(49.0, 45, "male")["rating"], "Excellent")
        self.assertEqual(fitness_age.vo2_standing(49.0, 45, "male")["group"], "men 40 to 49")
        self.assertEqual(fitness_age.vo2_standing(44.0, 31, "female")["rating"], "Excellent")
        self.assertEqual(fitness_age.vo2_standing(44.0, 85, "female")["group"], "women 70 to 79")

    def test_bands_are_listed_low_to_high_with_their_lower_edge(self) -> None:
        bands = fitness_age.vo2_standing(50, 19, "male")["bands"]
        self.assertEqual([b["name"] for b in bands],
                         ["Poor", "Fair", "Good", "Excellent", "Superior"])
        self.assertEqual([b["from"] for b in bands], [None, 41.7, 45.4, 51.1, 55.4])

    def test_unrateable_inputs_return_nothing(self) -> None:
        self.assertIsNone(fitness_age.vo2_standing(None, 25, "male"))
        self.assertIsNone(fitness_age.vo2_standing(50, None, "male"))
        self.assertIsNone(fitness_age.vo2_standing(50, 25, "unknown"))
        self.assertIsNone(fitness_age.vo2_standing(True, 25, "male"))


class SourceAndRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        garmin_source._FITNESS_AGE_CACHE.update({"val": None, "ts": 0.0})
        self.addCleanup(lambda: garmin_source._FITNESS_AGE_CACHE.update({"val": None, "ts": 0.0}))
        clock = patch.object(config, "local_today", return_value=datetime.date(2026, 11, 9))
        clock.start()
        self.addCleanup(clock.stop)

    def _client(self) -> MagicMock:
        client = MagicMock(name="garmin")
        client.get_fitnessage_data.return_value = {"invalidReason": "USER_UNDER_AGE"}
        client.get_max_metrics.return_value = [{
            "generic": {"vo2MaxPreciseValue": 55.8, "calendarDate": "2026-11-08"},
            "cycling": {"vo2MaxPreciseValue": 58.0}}]
        return client

    def test_route_serves_the_reason_and_the_measured_vo2max(self) -> None:
        client = self._client()
        with patch.object(garmin_source, "get_client", return_value=client):
            body = json.loads(main.fitness_age().body)
            again = json.loads(main.fitness_age().body)
        self.assertIsNone(body["fitness_age"])
        self.assertEqual(body["reason"], "under_age")
        self.assertEqual(body["vo2max"]["rating"], "Superior")
        self.assertEqual(body["chronological_age"], coaching_contract.ATHLETE_CONSTANTS["age"])
        self.assertEqual(again, body)
        client.get_fitnessage_data.assert_called_once_with("2026-11-09")   # second call cached

    def test_record_says_which_parts_are_garmins_and_which_are_worked_out(self) -> None:
        with patch.object(garmin_source, "get_client", return_value=self._client()):
            body = json.loads(main.fitness_age().body)
        # Garmin's own fields keep Garmin's label; the record no longer claims to be
        # measured from end to end while it carries the app's estimate.
        self.assertEqual(body["provider"], "Garmin")
        self.assertTrue(body["source"].startswith("measured"))
        self.assertNotEqual(body["source"], "measured")
        self.assertIn("estimate", body["source"])
        self.assertIsNone(body["fitness_age"])
        est = body["estimate"]
        self.assertEqual((est["source"], est["provider"]), ("estimated", None))
        self.assertEqual(body["vo2max"]["rating_source"], "derived")
        # Every key the page reads is still there.
        self.assertLessEqual({"age", "floor", "ceiling", "method", "vo2max",
                              "typical_vo2max_at_age", "delta_years"}, set(est))
        self.assertLessEqual({"value", "rating", "rank", "group", "bands", "bike", "as_of"},
                             set(body["vo2max"]))

    def _age_cache(self, seconds: float) -> None:
        garmin_source._FITNESS_AGE_CACHE["ts"] -= seconds

    def test_full_answer_is_cached_for_the_full_ttl(self) -> None:
        client = self._client()
        with patch.object(garmin_source, "get_client", return_value=client):
            first = garmin_source.get_fitness_age()
            self._age_cache(garmin_source._FITNESS_AGE_TTL_S - 5)
            garmin_source.get_fitness_age()
            self.assertEqual(client.get_fitnessage_data.call_count, 1)
            self._age_cache(10)
            garmin_source.get_fitness_age()
        self.assertEqual(client.get_fitnessage_data.call_count, 2)
        self.assertEqual(first["reads"], {"fitness_age": "answered", "vo2max": "answered"})

    def test_an_empty_answer_is_an_answer_and_is_cached(self) -> None:
        # Garmin answering "nothing for this athlete" is not Garmin being down.
        client = MagicMock(name="garmin")
        client.get_fitnessage_data.return_value = {}
        client.get_max_metrics.return_value = []
        with patch.object(garmin_source, "get_client", return_value=client):
            out = garmin_source.get_fitness_age()
            self._age_cache(garmin_source._FITNESS_AGE_TTL_S - 5)
            garmin_source.get_fitness_age()
        self.assertEqual(out["reads"], {"fitness_age": "answered", "vo2max": "answered"})
        self.assertIsNone(out["vo2max"])
        self.assertEqual(client.get_fitnessage_data.call_count, 1)

    def test_vo2max_read_down_is_not_held_for_the_full_ttl(self) -> None:
        client = self._client()
        client.get_max_metrics.side_effect = RuntimeError("login failed")
        with patch.object(garmin_source, "get_client", return_value=client):
            down = garmin_source.get_fitness_age()
            client.get_max_metrics.side_effect = None            # Garmin is back
            self._age_cache(garmin_source._LEGACY_RETRY_S + 1)
            up = garmin_source.get_fitness_age()
        self.assertLessEqual(garmin_source._FITNESS_AGE_PARTIAL_TTL_S,
                             garmin_source._LEGACY_RETRY_S)
        self.assertEqual(down["reads"], {"fitness_age": "answered", "vo2max": "failed"})
        self.assertEqual(down["reason"], "under_age")
        self.assertIsNone(down["vo2max"])
        self.assertIsNone(down["estimate"])
        self.assertEqual(up["reads"]["vo2max"], "answered")
        self.assertEqual(up["estimate"]["age"], 20)
        self.assertEqual(up["vo2max"]["value"], 55.8)

    def test_fitness_age_read_down_is_not_held_for_the_full_ttl(self) -> None:
        client = self._client()
        client.get_fitnessage_data.side_effect = RuntimeError("down")
        with patch.object(garmin_source, "get_client", return_value=client):
            down = garmin_source.get_fitness_age()
            client.get_fitnessage_data.side_effect = None        # Garmin is back
            client.get_fitnessage_data.return_value = _GARMIN
            self._age_cache(garmin_source._LEGACY_RETRY_S + 1)
            up = garmin_source.get_fitness_age()
        self.assertEqual(down["reads"], {"fitness_age": "failed", "vo2max": "answered"})
        self.assertIsNone(down["fitness_age"])
        self.assertEqual(down["estimate"]["age"], 20)            # the stand-in, briefly
        self.assertEqual(up["fitness_age"], 27.4)                # Garmin's number replaces it
        self.assertIsNone(up["estimate"])

    def test_one_failed_day_in_the_vo2max_scan_makes_the_read_partial(self) -> None:
        # A failed day could have held a newer reading, so the scan is not the full answer.
        client = self._client()
        good = client.get_max_metrics.return_value
        no_bike = [{"generic": good[0]["generic"]}]
        client.get_max_metrics.side_effect = [RuntimeError("429")] + [no_bike] * 20
        with patch.object(garmin_source, "get_client", return_value=client):
            out = garmin_source.get_fitness_age()
            self._age_cache(garmin_source._LEGACY_RETRY_S + 1)
            client.get_max_metrics.side_effect = None
            garmin_source.get_fitness_age()
        self.assertEqual(out["reads"]["vo2max"], "failed")
        self.assertEqual(out["vo2max"]["value"], 55.8)           # what did answer is still shown
        self.assertEqual(client.get_fitnessage_data.call_count, 2)

    def test_partial_answer_is_held_briefly_so_a_struggling_garmin_is_not_hammered(self) -> None:
        client = self._client()
        client.get_max_metrics.side_effect = RuntimeError("429")
        with patch.object(garmin_source, "get_client", return_value=client):
            garmin_source.get_fitness_age()
            garmin_source.get_fitness_age()
        self.assertEqual(client.get_fitnessage_data.call_count, 1)

    def test_a_dead_garmin_read_is_reported_and_not_cached(self) -> None:
        client = MagicMock(name="garmin")
        client.get_fitnessage_data.side_effect = RuntimeError("down")
        client.get_max_metrics.side_effect = RuntimeError("down")
        with patch.object(garmin_source, "get_client", return_value=client):
            out = garmin_source.get_fitness_age()
            garmin_source.get_fitness_age()
        self.assertEqual(out["reason"], "unavailable")
        self.assertIsNone(out["vo2max"])
        self.assertEqual(out["reads"], {"fitness_age": "failed", "vo2max": "failed"})
        self.assertEqual(client.get_fitnessage_data.call_count, 2)

    def test_markers_still_report_vo2max(self) -> None:
        client = self._client()
        client.get_cycling_ftp.return_value = {"functionalThresholdPower": 288}
        client.get_race_predictions.return_value = []
        with patch.object(garmin_source, "get_client", return_value=client):
            markers = garmin_source.get_fitness_markers()
        self.assertEqual((markers["vo2_max_running"], markers["vo2_max_cycling"]), (55.8, 58.0))
        self.assertEqual(markers["vo2_as_of"], "2026-11-08")
        self.assertEqual(markers["cycling_ftp_w"], 288)


class RaceGoalTests(unittest.TestCase):
    def test_public_goal_keeps_target_and_splits_only(self) -> None:
        goal = {"basis": "private reasoning", "modelled_duration_min": 300, "target": "5:00",
                "split": {"swim": "0:37", "bike": "2:36", "run": "1:39", "T1+T2": "0:08", "x": 3}}
        self.assertEqual(coaching_contract._public_goal(goal), {
            "target": "5:00",
            "split": {"swim": "0:37", "bike": "2:36", "run": "1:39", "T1+T2": "0:08"}})
        self.assertIsNone(coaching_contract._public_goal({"basis": "only"}))
        self.assertIsNone(coaching_contract._public_goal(None))


if __name__ == "__main__":
    unittest.main()
