from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from app import strength_block as sb
from app import strength_effort as se


# Effort now also has a race-proximity ceiling. These tests are about placement,
# so the event is held far away rather than moving with the real active profile.
_FAR_RACE = None


def setUpModule():
    global _FAR_RACE
    _FAR_RACE = patch.object(se.coaching_contract, "race_phase",
                             return_value={"days_remaining": 120, "is_past": False})
    _FAR_RACE.start()


def tearDownModule():
    if _FAR_RACE is not None:
        _FAR_RACE.stop()


MONDAY = datetime.date(2026, 8, 24)


def _day(date: datetime.date, discipline: str, **kw) -> dict:
    return {"date": date.isoformat(), "discipline": discipline, **kw}


def _week(overrides: dict | None = None) -> list[dict]:
    """A plain fortnight of easy training, with named days overridden."""
    overrides = overrides or {}
    days = []
    for offset in range(14):
        date = MONDAY + datetime.timedelta(days=offset)
        days.append(overrides.get(date, _day(date, "swim", duration_min=40)))
    return days


class PlacementTests(unittest.TestCase):
    def test_a_clear_week_gets_the_requested_number_of_sessions(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                         plan_days=_week())
        self.assertEqual(len(block["placements"]), 4)

    def test_sessions_are_spread_rather_than_stacked(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                         plan_days=_week())
        dates = [datetime.date.fromisoformat(p["date"]) for p in block["placements"]]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        self.assertTrue(all(gap >= 1 for gap in gaps), gaps)
        self.assertGreaterEqual(max(dates) - min(dates), datetime.timedelta(days=4))

    def test_upper_and_lower_alternate_across_the_week(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                         plan_days=_week())
        counts = block["counts"]
        self.assertEqual(counts.get("lower"), 2)
        self.assertEqual(counts.get("upper"), 2)

    def test_no_lift_lands_between_two_consecutive_key_runs(self) -> None:
        wednesday = MONDAY + datetime.timedelta(days=2)
        thursday = MONDAY + datetime.timedelta(days=3)
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week({
            wednesday: _day(wednesday, "run", intensity="threshold", duration_min=60),
            thursday: _day(thursday, "run", intensity="Z2", duration_min=120),
        }))
        placed = {p["date"] for p in block["placements"]}
        self.assertNotIn(wednesday.isoformat(), placed)

    def test_a_lower_day_never_sits_the_day_before_a_key_run(self) -> None:
        # Key runs on Tue and Fri: Mon and Thu are the days before them.
        tuesday = MONDAY + datetime.timedelta(days=1)
        friday = MONDAY + datetime.timedelta(days=4)
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week({
            tuesday: _day(tuesday, "run", intensity="threshold", duration_min=60),
            friday: _day(friday, "run", intensity="race pace", duration_min=75),
        }))
        thursday = (MONDAY + datetime.timedelta(days=3)).isoformat()
        for placement in block["placements"]:
            if placement["date"] in (MONDAY.isoformat(), thursday):
                self.assertEqual(placement["slot"], "upper", placement)

    def test_rest_days_are_preferred_over_ordinary_training_days(self) -> None:
        sunday = MONDAY + datetime.timedelta(days=6)
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=2, plan_days=_week({
            sunday: _day(sunday, "rest", duration_min=0),
        }))
        self.assertIn(sunday.isoformat(), {p["date"] for p in block["placements"]})

    def test_a_fully_blocked_week_places_nothing_rather_than_forcing_a_lift(self) -> None:
        blocked = {}
        for offset in range(14):
            date = MONDAY + datetime.timedelta(days=offset)
            blocked[date] = _day(date, "run", intensity="threshold", duration_min=90)
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                         plan_days=_week(blocked))
        self.assertEqual(block["placements"], [])


class MultiWeekTests(unittest.TestCase):
    def test_a_block_repeats_across_every_requested_week(self) -> None:
        block = sb.build(start=MONDAY, weeks=2, sessions_per_week=4, plan_days=_week())
        self.assertEqual(len(block["placements"]), 8)
        self.assertEqual({p["week_index"] for p in block["placements"]}, {0, 1})

    def test_days_before_the_start_are_never_scheduled(self) -> None:
        wednesday = MONDAY + datetime.timedelta(days=2)
        block = sb.build(start=wednesday, weeks=1, sessions_per_week=4, plan_days=_week())
        self.assertTrue(all(p["date"] >= wednesday.isoformat() for p in block["placements"]))

    def test_an_end_date_truncates_the_block(self) -> None:
        end = MONDAY + datetime.timedelta(days=3)
        block = sb.build(start=MONDAY, weeks=2, sessions_per_week=4,
                         plan_days=_week(), until=end)
        self.assertTrue(all(p["date"] <= end.isoformat() for p in block["placements"]))

    def test_zero_weeks_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sb.build(start=MONDAY, weeks=0, sessions_per_week=4, plan_days=_week())

    def test_an_absurd_session_count_is_clamped(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=99, plan_days=_week())
        self.assertEqual(block["sessions_per_week"], sb.MAX_SESSIONS_PER_WEEK)


class EffortTests(unittest.TestCase):
    def test_each_placed_day_carries_its_own_effort_call(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week())
        for placement in block["placements"]:
            self.assertIn(placement["effort_level"], se.LEVELS)
            self.assertTrue(placement["effort_cue"])

    def test_unknown_working_weights_cap_every_day_at_moderate(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week(),
                         strength={"calibration_required": True})
        self.assertTrue(all(p["effort_level"] != "heavy" for p in block["placements"]))

    def test_poor_readiness_softens_the_block(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week(),
                         readiness={"training_readiness": {"score": 20}})
        self.assertTrue(all(p["effort_level"] != "heavy" for p in block["placements"]))

    def test_malformed_plan_days_do_not_raise(self) -> None:
        for bad in (None, [], ["x", None, 42]):
            block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=bad)
            self.assertEqual(len(block["placements"]), 4)




class RunOnlyBlockTests(unittest.TestCase):
    """A marathon week is running every day, so nothing scores as "free" the way
    a swim/bike week does. Spacing alone then parks a leg day on the long run."""

    def _marathon_week(self) -> list[dict]:
        """Mon ride, Tue threshold, Wed easy, Thu mid-long, Fri ride, Sat shakeout,
        Sun long run — the shape the marathon builder actually writes."""
        shape = [("bike", {"intensity": "Z2", "duration_min": 45}),
                 ("run", {"intensity": "threshold", "duration_min": 55}),
                 ("run", {"intensity": "easy", "duration_min": 38}),
                 ("run", {"intensity": "easy", "duration_min": 63}),
                 ("bike", {"intensity": "Z2", "duration_min": 50}),
                 ("run", {"intensity": "easy", "duration_min": 25}),
                 ("run", {"intensity": "long", "duration_min": 86})]
        days = []
        for offset in range(14):
            discipline, fields = shape[offset % 7]
            days.append(_day(MONDAY + datetime.timedelta(days=offset), discipline, **fields))
        return days

    def test_no_lift_lands_on_the_long_run_while_an_easy_day_is_free(self) -> None:
        week = self._marathon_week()
        block = sb.build(start=MONDAY, weeks=2, sessions_per_week=2, plan_days=week)
        longs = {d["date"] for d in week if d.get("intensity") == "long"}
        placed = {p["date"] for p in block["placements"]}
        self.assertTrue(placed)
        self.assertFalse(placed & longs, sorted(placed & longs))

    def test_no_lift_lands_on_the_threshold_run_either(self) -> None:
        week = self._marathon_week()
        block = sb.build(start=MONDAY, weeks=2, sessions_per_week=2, plan_days=week)
        quality = {d["date"] for d in week if d.get("intensity") == "threshold"}
        self.assertFalse({p["date"] for p in block["placements"]} & quality)

    def test_a_freer_day_beats_a_better_spaced_one(self) -> None:
        # Hard running every day except Wednesday and Thursday, so Wednesday is
        # the only day with neither a key run on it nor one the next morning.
        # Spacing alone would take Tuesday: it comes first among the usable days.
        days = []
        for offset in range(14):
            date = MONDAY + datetime.timedelta(days=offset)
            easy = offset % 7 in (2, 3)
            days.append(_day(date, "run", intensity="easy" if easy else "threshold",
                             duration_min=40 if easy else 60))
        wednesday = (MONDAY + datetime.timedelta(days=2)).isoformat()
        tuesday = (MONDAY + datetime.timedelta(days=1)).isoformat()
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=1, plan_days=days)
        self.assertEqual([p["date"] for p in block["placements"]], [wednesday])
        self.assertNotEqual(block["placements"][0]["date"], tuesday)


class RaceDayTests(unittest.TestCase):
    def test_nothing_is_ever_placed_on_race_day_or_the_morning_before(self) -> None:
        race = MONDAY + datetime.timedelta(days=6)
        for marker in ({"discipline": "race"},
                       {"discipline": "run", "intensity": "race", "duration_min": 240},
                       {"discipline": "run", "phase": "race", "duration_min": 240}):
            with self.subTest(marker=marker):
                days = _week({race: {"date": race.isoformat(), **marker}})
                block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                                 plan_days=days)
                placed = {p["date"] for p in block["placements"]}
                self.assertNotIn(race.isoformat(), placed)
                self.assertNotIn((race - datetime.timedelta(days=1)).isoformat(), placed)


class AlternationTests(unittest.TestCase):
    """Four sessions in seven days forces one adjacent pair; it must not be two
    leg days."""

    def test_consecutive_sessions_never_share_a_slot(self) -> None:
        block = sb.build(start=MONDAY, weeks=2, sessions_per_week=4, plan_days=_week())
        placements = block["placements"]
        for earlier, later in zip(placements, placements[1:]):
            gap = (datetime.date.fromisoformat(later["date"])
                   - datetime.date.fromisoformat(earlier["date"])).days
            if gap == 1:
                self.assertNotEqual(earlier["slot"], later["slot"],
                                    f"{earlier} then {later}")

    def test_slots_alternate_even_when_every_day_is_equally_free(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week())
        slots = [p["slot"] for p in block["placements"]]
        self.assertEqual(slots, ["lower", "upper", "lower", "upper"])

    def test_parity_flips_to_keep_lower_away_from_a_key_run(self) -> None:
        tuesday = MONDAY + datetime.timedelta(days=1)
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=_week({
            tuesday: _day(tuesday, "run", intensity="threshold", duration_min=75),
        }))
        for placement in block["placements"]:
            if placement["date"] == MONDAY.isoformat():
                self.assertEqual(placement["slot"], "upper")


class HostileInputTests(unittest.TestCase):
    def test_a_truthy_non_sequence_plan_does_not_crash_placement(self) -> None:
        for bad in (True, 1, -1, 3.7, float("inf"), "a string", object()):
            block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4, plan_days=bad)
            self.assertEqual(len(block["placements"]), 4, repr(bad))

    def test_days_that_are_not_objects_are_skipped_not_indexed(self) -> None:
        block = sb.build(start=MONDAY, weeks=1, sessions_per_week=4,
                         plan_days=[None, 42, "x", {"date": "nonsense"}, {}])
        self.assertEqual(len(block["placements"]), 4)


if __name__ == "__main__":
    unittest.main()
