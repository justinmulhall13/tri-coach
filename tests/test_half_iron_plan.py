"""The 70.3 builder and its three weekly full-body lifts.

Pins the block shape (phases counted back from race day, recovery weeks, three
swims), the post-marathon recovery window, and the lifting placement: full
body Monday/Wednesday/Friday, nothing in the first week after the marathon,
and maintenance-light once the race gets close.
"""
from __future__ import annotations

import datetime
import pathlib
import tempfile
import threading
import unittest

from app import coaching_contract, db, half_iron_plan as hip, lifting_rules, plan

RACE = datetime.date(2027, 5, 30)
START = datetime.date(2026, 10, 20)
RECOVERY_UNTIL = datetime.date(2026, 11, 1)


def _activate_703() -> None:
    db.stage_event_profile({
        "event_name": "IRONMAN 70.3 Victoria",
        "event_date": RACE.isoformat(),
        "distances": {"swim_km": 1.9, "bike_km": 90.0, "run_km": 21.1},
        "goal": {"target": "strong first 70.3"},
        "mode": "TRIATHLON",
        "provenance": {field: "confirmed_by_user"
                       for field in coaching_contract.PROFILE_FIELDS},
        "recovery_until": RECOVERY_UNTIL.isoformat(),
    })
    db.activate_pending_event_profile()


class HalfIronBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "703.db"
        db._local = threading.local()
        _activate_703()
        self.result = plan.seed(START)
        self.rows = db.get_plan(START.isoformat(), RACE.isoformat())
        self.by_date = {r["date"]: r for r in self.rows}
        self.lifts = {d["date"]: d for d in
                      db.get_plan_strength_range(START.isoformat(), RACE.isoformat())}

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    def _week(self, monday: datetime.date) -> list[dict]:
        return [self.by_date[(monday + datetime.timedelta(days=i)).isoformat()]
                for i in range(7)]

    # -- dispatch and race day ----------------------------------------------

    def test_a_non_t100_triathlon_gets_the_70_3_builder(self) -> None:
        self.assertNotIn("error", self.result)
        self.assertEqual(self.result["seeded_through"], RACE.isoformat())
        self.assertIs(plan._event_builder(), hip)

    def test_race_day_names_the_persisted_event(self) -> None:
        race = self.by_date[RACE.isoformat()]
        self.assertEqual(race["discipline"], "race")
        self.assertIn("IRONMAN 70.3 Victoria", race["title"])
        self.assertIn("1.9 km swim, 90 km bike, 21.1 km run", race["structure"]["main"])
        self.assertNotIn("T100", race["title"])

    # -- phases ---------------------------------------------------------------

    def test_phases_run_in_order(self) -> None:
        order = []
        for row in self.rows:
            if not order or order[-1] != row["phase"]:
                order.append(row["phase"])
        self.assertEqual(order, ["recovery", "base", "build", "specific",
                                 "peak", "taper", "race"])

    def test_the_taper_is_the_final_two_weeks(self) -> None:
        taper = [r["date"] for r in self.rows if r["phase"] == "taper"]
        self.assertEqual(taper[0], "2027-05-17")
        self.assertEqual(taper[-1], "2027-05-29")

    def test_phase_strip_reads_the_block_phase(self) -> None:
        phase = coaching_contract.race_phase(datetime.date(2027, 2, 10))
        self.assertEqual(phase["block_phase"], "build")
        self.assertEqual(phase["block_phases"][0], "recovery")

    # -- post-marathon recovery -------------------------------------------------

    def test_no_running_in_the_first_week_after_the_marathon(self) -> None:
        first_week = [r for r in self.rows if r["date"] <= "2026-10-25"]
        self.assertFalse(any(r["discipline"] in {"run", "brick"} for r in first_week))
        self.assertFalse(any(r["intensity"] in {"threshold", "tempo"} for r in first_week))

    def test_running_returns_easy_in_week_two(self) -> None:
        runs = [r for r in self.rows
                if "2026-10-26" <= r["date"] <= RECOVERY_UNTIL.isoformat()
                and r["discipline"] == "run"]
        self.assertTrue(runs)
        self.assertTrue(all(r["intensity"] == "easy" for r in runs))

    # -- weekly shape -------------------------------------------------------------

    def test_a_base_week_has_three_swims_a_long_ride_and_a_long_run(self) -> None:
        week = self._week(datetime.date(2026, 11, 2))
        self.assertEqual(sum(r["discipline"] == "swim" for r in week), 3)
        self.assertTrue(week[5]["title"].startswith("Long ride"))
        self.assertTrue(week[6]["title"].startswith("Long run"))

    def test_bricks_arrive_in_build(self) -> None:
        saturday = self.by_date["2027-02-06"]
        self.assertEqual(saturday["discipline"], "brick")
        self.assertTrue(saturday["structure"].get("run"))

    def test_every_recovery_week_has_a_full_rest_day(self) -> None:
        for monday in (datetime.date(2026, 12, 14), datetime.date(2027, 2, 8)):
            week = self._week(monday)
            self.assertTrue(any(r["is_rest"] for r in week), monday)

    def test_long_ride_peaks_in_the_specific_phase(self) -> None:
        def minutes(row):
            return row["duration_min"] if row["date"] and row["phase"] != "race" else 0
        saturdays = [r for r in self.rows
                     if datetime.date.fromisoformat(r["date"]).weekday() == 5]
        longest = max(saturdays, key=minutes)
        self.assertEqual(longest["phase"], "specific")

    def test_the_long_run_never_exceeds_the_race_run(self) -> None:
        for row in self.rows:
            if row["title"].startswith("Long run"):
                km = float(row["title"].split("—")[1].split()[0])
                self.assertLessEqual(km, 21.1, row["date"])

    # -- lifting ------------------------------------------------------------------

    def test_three_full_body_lifts_on_monday_wednesday_friday(self) -> None:
        week = [(datetime.date(2027, 1, 4) + datetime.timedelta(days=i)).isoformat()
                for i in range(7)]
        lifted = [d for d in week if d in self.lifts]
        self.assertEqual([datetime.date.fromisoformat(d).weekday() for d in lifted], [0, 2, 4])
        self.assertEqual([self.lifts[d]["title"] for d in lifted],
                         ["Full Body A", "Full Body B", "Full Body C"])
        self.assertTrue(all(self.lifts[d]["slot"] == "full" for d in lifted))

    def test_no_lifting_in_the_first_week_after_the_marathon(self) -> None:
        self.assertFalse([d for d in self.lifts if d <= "2026-10-25"])
        self.assertTrue([d for d in self.lifts if "2026-10-26" <= d <= "2026-11-01"])

    def test_lifting_is_heavy_in_base_and_light_in_the_taper(self) -> None:
        self.assertEqual(self.lifts["2027-01-04"]["effort_level"], "heavy")
        self.assertEqual(self.lifts["2027-04-26"]["effort_level"], "moderate")
        for date, lift in self.lifts.items():
            if date >= "2027-05-10":
                self.assertEqual(lift["effort_level"], "light", date)

    def test_race_week_keeps_only_the_monday_lift(self) -> None:
        race_week = [d for d in self.lifts if d >= "2027-05-24"]
        self.assertEqual(race_week, ["2027-05-24"])

    def test_every_seeded_lift_obeys_the_shoulder_rules(self) -> None:
        for date, lift in self.lifts.items():
            titles = [part.split(" — ")[0] for part in lift["structure"]["main"].split("; ")]
            violations = lifting_rules.check([{"title": t} for t in titles])
            # Recovery weeks drop the box jumps, so exercise count may be five.
            injury = [v for v in violations if v.get("rule") != "exercise_count"]
            self.assertEqual(injury, [], date)

    def test_reseeding_keeps_a_lift_the_athlete_edited(self) -> None:
        date = "2027-01-06"
        edited = {**self.lifts[date], "title": "My own session", "source": "edited"}
        db.upsert_plan_strength(edited)
        plan.reconcile_seeded_plan(START)
        self.assertEqual(db.get_plan_strength(date)["title"], "My own session")


if __name__ == "__main__":
    unittest.main()
