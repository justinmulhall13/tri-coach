"""The compressed marathon builder.

The block is short, so the shape of it is the whole plan: the long run has to
climb, peak two Sundays out and then get out of the way. These tests pin that
shape, and pin the things a later edit is most likely to break by accident —
the race day naming the wrong race, a reconcile quietly rewriting long runs the
athlete has already seen, or the taper swallowing the peak week.
"""
from __future__ import annotations

import datetime
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import coaching_contract, db, marathon_plan, plan, strength_effort

RACE = datetime.date(2026, 10, 18)
START = datetime.date(2026, 9, 9)


def _activate_marathon(name: str = "TCS Toronto Waterfront Marathon",
                       run_km: float = 42.195, goal: str = "sub-3:30") -> None:
    """Really activate the profile rather than patching the accessor.

    Plan rows are namespaced by the profile id the *database* holds, so a
    patched reader alone writes marathon days stamped with the default event
    and every reconcile then skips them as another profile's rows.
    """
    db.stage_event_profile({
        "event_name": name,
        "event_date": RACE.isoformat(),
        "distances": {"run_km": run_km},
        "goal": {"target": goal},
        "mode": "MARATHON",
        "provenance": {field: "confirmed_by_user"
                       for field in coaching_contract.PROFILE_FIELDS},
    })
    db.activate_pending_event_profile()


class MarathonBlockTests(unittest.TestCase):
    """Every test writes plan rows, so each gets its own database."""

    def setUp(self) -> None:
        self._old_path, self._old_local = db._DB_PATH, db._local
        self._tmp = tempfile.TemporaryDirectory()
        db._DB_PATH = pathlib.Path(self._tmp.name) / "marathon.db"
        db._local = threading.local()
        _activate_marathon()

    def tearDown(self) -> None:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._DB_PATH, db._local = self._old_path, self._old_local
        self._tmp.cleanup()

    @staticmethod
    def _long_runs(rows: list[dict]) -> list[tuple[str, float]]:
        return [(row["date"], float(row["title"].split("—")[1].strip().split()[0]))
                for row in rows if row["title"].startswith("Long run")]

    # -- shape -------------------------------------------------------------

    def test_the_peak_long_run_lands_two_sundays_out(self) -> None:
        marathon_plan.seed(START)
        rows = db.get_plan(START.isoformat(), RACE.isoformat())
        longs = self._long_runs(rows)
        peak_date, peak_km = max(longs, key=lambda pair: pair[1])
        self.assertEqual(peak_km, max(marathon_plan.LONG_RUN_KM))
        self.assertEqual((RACE - datetime.date.fromisoformat(peak_date)).days, 14)

    def test_long_runs_climb_to_the_peak_and_then_fall(self) -> None:
        marathon_plan.seed(START)
        distances = [km for _, km in self._long_runs(
            db.get_plan(START.isoformat(), RACE.isoformat()))]
        peak = distances.index(max(distances))
        self.assertEqual(distances[:peak + 1], sorted(distances[:peak + 1]))
        self.assertLess(distances[-1], distances[peak])

    def test_the_peak_week_is_build_and_the_taper_starts_after_it(self) -> None:
        marathon_plan.seed(START)
        rows = {row["date"]: row for row in
                db.get_plan(START.isoformat(), RACE.isoformat())}
        peak_day = (RACE - datetime.timedelta(days=14)).isoformat()
        after = (RACE - datetime.timedelta(days=13)).isoformat()
        self.assertEqual(rows[peak_day]["phase"], "build")
        self.assertEqual(rows[after]["phase"], "taper")
        self.assertEqual(rows[RACE.isoformat()]["phase"], "race")

    def test_the_day_after_every_long_run_is_a_ride_not_a_run(self) -> None:
        marathon_plan.seed(START)
        rows = {row["date"]: row for row in
                db.get_plan(START.isoformat(), RACE.isoformat())}
        for date, _km in self._long_runs(list(rows.values())):
            monday = (datetime.date.fromisoformat(date)
                      + datetime.timedelta(days=1)).isoformat()
            if monday in rows:
                self.assertEqual(rows[monday]["discipline"], "bike", monday)

    def test_exactly_one_hard_run_a_week(self) -> None:
        marathon_plan.seed(START)
        rows = db.get_plan(START.isoformat(), RACE.isoformat())
        by_week: dict[int, int] = {}
        for row in rows:
            if row.get("intensity") == "threshold":
                by_week[row["week_index"]] = by_week.get(row["week_index"], 0) + 1
        self.assertTrue(by_week)
        self.assertEqual(set(by_week.values()), {1}, by_week)

    # -- the sore-legs swap ------------------------------------------------

    def test_the_bike_swap_is_offered_on_easy_days_but_never_on_the_key_ones(self) -> None:
        marathon_plan.seed(START)
        for row in db.get_plan(START.isoformat(), RACE.isoformat()):
            main = str((row.get("structure") or {}).get("main") or "")
            if row["title"].startswith(("Long run", "Run — threshold", "🏁")):
                self.assertNotIn(marathon_plan.SORE_SWAP, main, row["date"])
            elif row["title"].startswith(("Easy run", "Mid-long")):
                self.assertIn(marathon_plan.SORE_SWAP, main, row["date"])

    def test_distances_are_whole_kilometres(self) -> None:
        marathon_plan.seed(START)
        for row in db.get_plan(START.isoformat(), RACE.isoformat()):
            for token in row["title"].replace("—", " ").split():
                if token.replace(".", "", 1).isdigit():
                    self.assertEqual(float(token), int(float(token)), row["title"])

    # -- the race itself ---------------------------------------------------

    def test_race_day_names_the_athletes_event_not_a_hardcoded_one(self) -> None:
        _activate_marathon("Berlin Marathon")
        marathon_plan.seed(START)
        race_row = db.get_plan_day(RACE.isoformat())
        self.assertIn("Berlin Marathon", race_row["title"])
        self.assertNotIn("Toronto", race_row["title"])

    def test_race_day_distance_comes_from_the_profile(self) -> None:
        _activate_marathon("Half", run_km=21.1)
        marathon_plan.seed(START)
        race_row = db.get_plan_day(RACE.isoformat())
        self.assertIn("21.1 km", race_row["structure"]["main"])

    def test_a_past_race_date_is_refused_rather_than_seeded_backwards(self) -> None:
        result = marathon_plan.seed(RACE + datetime.timedelta(days=1))
        self.assertIn("error", result)

    # -- the long run is the key session -----------------------------------

    def test_an_early_short_long_run_still_counts_as_a_key_session(self) -> None:
        marathon_plan.seed(START)
        rows = db.get_plan(START.isoformat(), RACE.isoformat())
        first_long = next(row for row in rows if row["title"].startswith("Long run"))
        self.assertLess(first_long["duration_min"], strength_effort.LONG_RUN_MIN)
        self.assertTrue(strength_effort.is_key_run(first_long),
                        "the first long run is under 90 minutes and must still be key")

    # -- reconcile ---------------------------------------------------------

    def test_reconciling_mid_block_keeps_the_remaining_long_runs_identical(self) -> None:
        marathon_plan.seed(START)
        before = dict(self._long_runs(db.get_plan(START.isoformat(), RACE.isoformat())))
        later = START + datetime.timedelta(days=17)
        marathon_plan.reconcile(later)
        after = dict(self._long_runs(db.get_plan(later.isoformat(), RACE.isoformat())))
        self.assertTrue(after)
        for date, km in after.items():
            self.assertEqual(km, before[date], date)

    def test_reconcile_refreshes_content_but_never_an_edited_day(self) -> None:
        marathon_plan.seed(START)
        edited = (START + datetime.timedelta(days=3)).isoformat()
        db.edit_plan_day(edited, {"title": "Ran with a friend"}, source="edited")
        result = marathon_plan.reconcile(START)
        self.assertEqual(db.get_plan_day(edited)["title"], "Ran with a friend")
        self.assertEqual(result["skipped_modified"], 1)
        self.assertGreater(result["reconciled"], 0)

    def test_reconcile_keeps_a_dragged_sessions_calendar_placement(self) -> None:
        marathon_plan.seed(START)
        moved = (START + datetime.timedelta(days=2)).isoformat()
        day = db.get_plan_day(moved)
        day.update({"start_time": "06:15", "gcal_event_id": "evt-123"})
        db.upsert_plan_day(day)
        marathon_plan.reconcile(START)
        refreshed = db.get_plan_day(moved)
        self.assertEqual(refreshed["start_time"], "06:15")
        self.assertEqual(refreshed["gcal_event_id"], "evt-123")

    def test_reconcile_refreshes_a_stale_phase_label(self) -> None:
        marathon_plan.seed(START)
        peak_day = (RACE - datetime.timedelta(days=14)).isoformat()
        day = db.get_plan_day(peak_day)
        day["phase"] = "taper"
        db.upsert_plan_day(day)
        marathon_plan.reconcile(START)
        self.assertEqual(db.get_plan_day(peak_day)["phase"], "build")

    # -- goal pace ---------------------------------------------------------

    def test_goal_pace_is_derived_from_the_athletes_own_stated_time(self) -> None:
        # sub-3:30 over 42.195 km is 4:59/km.
        self.assertEqual(marathon_plan.goal_pace_per_km(), "4:59")

    def test_race_day_states_the_goal_pace(self) -> None:
        marathon_plan.seed(START)
        main = db.get_plan_day(RACE.isoformat())["structure"]["main"]
        self.assertIn("4:59/km", main)
        self.assertIn("42.2 km", main)

    def test_the_longest_runs_rehearse_goal_pace_on_tired_legs(self) -> None:
        marathon_plan.seed(START)
        for row in db.get_plan(START.isoformat(), RACE.isoformat()):
            if not row["title"].startswith("Long run"):
                continue
            km = float(row["title"].split("—")[1].strip().split()[0])
            main = row["structure"]["main"]
            if km >= 23:
                self.assertIn("last 5 km at 4:59/km", main, row["date"])
            else:
                self.assertNotIn("4:59/km", main, row["date"])

    def test_no_stated_time_means_no_invented_pace(self) -> None:
        _activate_marathon(goal="just finish it")
        self.assertIsNone(marathon_plan.goal_pace_per_km())
        marathon_plan.seed(START)
        main = db.get_plan_day(RACE.isoformat())["structure"]["main"]
        self.assertNotIn("/km", main)

    def test_an_explicit_pacing_target_wins_over_the_goal_text(self) -> None:
        with patch.object(marathon_plan.coaching_contract, "event_context",
                          return_value={"pacing_targets": {"run_goal_min_per_km": "4:45"},
                                        "goal": {"target": "sub-3:30"},
                                        "distances": {"run_km": 42.195}}):
            self.assertEqual(marathon_plan.goal_pace_per_km(), "4:45")

    def test_a_nonsense_goal_never_raises_or_invents_a_number(self) -> None:
        for goal in (None, {}, {"target": None}, {"target": "99:99"},
                     {"target": "0:00"}, "a plain string", 42):
            with patch.object(marathon_plan.coaching_contract, "event_context",
                              return_value={"goal": goal,
                                            "distances": {"run_km": 42.195}}):
                pace = marathon_plan.goal_pace_per_km()
            self.assertIn(pace, (None, "1:25"), repr(goal))

    # -- dispatch ----------------------------------------------------------

    def test_plan_seed_routes_marathon_mode_to_this_builder(self) -> None:
        with patch.object(marathon_plan, "seed",
                          return_value={"seeded_through": "sentinel"}) as seeded:
            self.assertEqual(plan.seed(START)["seeded_through"], "sentinel")
        seeded.assert_called_once()

    def test_a_longer_block_still_peaks_two_sundays_out(self) -> None:
        early = RACE - datetime.timedelta(days=120)
        marathon_plan.seed(early)
        longs = self._long_runs(db.get_plan(early.isoformat(), RACE.isoformat()))
        peak_date, peak_km = max(longs, key=lambda pair: pair[1])
        self.assertEqual(peak_km, max(marathon_plan.LONG_RUN_KM))
        self.assertEqual((RACE - datetime.date.fromisoformat(peak_date)).days, 14)
        self.assertGreater(len(longs), len(marathon_plan.LONG_RUN_KM))


if __name__ == "__main__":
    unittest.main()
