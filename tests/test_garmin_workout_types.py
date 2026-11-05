"""Every workout type pushes to the watch exactly as the app writes it.

The athlete asked that bricks, swims, interval runs, single-pace runs and every
other session reach Garmin with no difference from the text. These tests pin
the parser on each format the plan and Coach write, and walk the whole 70.3
block so a generator change that the watch cannot follow fails here.
"""
from __future__ import annotations

import datetime
import time
import unittest
from unittest.mock import patch

from app import coaching_contract, garmin_workout as gw, half_iron_plan as hip, marathon_plan, zones

RUN_HR = {1: (101, 120), 2: (121, 140), 3: (141, 160), 4: (161, 180), 5: (181, 201)}
BIKE_HR = {1: (100, 118), 2: (119, 138), 3: (139, 158), 4: (159, 178), 5: (179, 199)}
# LT 3.944 m/s -> easy band slow end 5:17/km, threshold 4:09-4:27/km.
ZONES = {"zones": {"DEFAULT": RUN_HR, "CYCLING": BIKE_HR}, "lt_speed_mps": 3.944,
         "ftp_w": 288, "lt_hr": 170, "errors": []}
EVENT = {
    "id": "victoria-test", "event": "IRONMAN 70.3 Victoria", "mode": "TRIATHLON",
    "disciplines_and_distances": {"swim_km": 1.9, "bike_km": 90, "run_km": 21.1},
    "distances": {"swim_km": 1.9, "bike_km": 90, "run_km": 21.1},
    "goal": {"target": "5:00", "modelled_duration_min": 300},
    "pacing_targets": {"bike_hr_bpm": [142, 152], "run_goal_min_per_km": "4:41",
                       "swim_goal_per_100m": "1:57"},
    "recovery_until": "2026-11-01",
}
RACE = datetime.date(2027, 5, 30)


def pace(sec: float) -> float:
    return round(1000.0 / sec, 3)


def band(sec: float) -> tuple[float, float]:
    """The ±8 s band encoded for a stated 'X/km' pace."""
    return pace(sec + 8), pace(sec - 8)


def target(step: dict) -> tuple:
    return (step["targetType"]["workoutTargetTypeKey"], step["targetValueOne"], step["targetValueTwo"])


def end(step: dict) -> tuple:
    return (step["endCondition"]["conditionTypeKey"], step["endConditionValue"])


def timed(steps: list[dict]) -> float:
    return gw._timed_seconds(steps)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(zones._CACHE)
        zones._CACHE.update(data=ZONES, ts=time.time() + 1e9)
        self.addCleanup(lambda: (zones._CACHE.clear(), zones._CACHE.update(self._saved)))
        ctx = patch.object(coaching_contract, "event_context", return_value=EVENT)
        ctx.start()
        self.addCleanup(ctx.stop)

    def build(self, disc: str, structure: dict, intensity: str = "", duration: int = 60,
              title: str = "Session") -> dict:
        return gw.build_workout({"discipline": disc, "title": title, "intensity": intensity,
                                 "duration_min": duration, "structure": structure},
                                "2026-11-10", thr_pace=253.5)

    def steps(self, *args, **kwargs) -> list[dict]:
        return self.build(*args, **kwargs)["workoutSegments"][0]["workoutSteps"]


class RunTests(_Fixture):
    def test_easy_run_then_strides_keeps_the_run(self) -> None:
        # This pushed as six strides and nothing else.
        run, strides = self.steps("run", {"main": "40 min easy, no faster than 5:17/km; "
                                                  "6×20 s strides w/ full recovery"}, "easy")
        self.assertEqual(end(run), ("time", 2400.0))
        self.assertEqual(target(run)[2], pace(317))
        self.assertEqual(strides["numberOfIterations"], 6)
        stride, back = strides["workoutSteps"]
        self.assertEqual(end(stride), ("time", 20.0))
        self.assertEqual(target(stride)[0], "no.target")
        self.assertEqual(end(back), ("lap.button", None))

    def test_strides_written_in_the_cooldown_are_pushed(self) -> None:
        steps = self.steps("run", {"warmup": "10 min easy", "main": "25 min easy no faster than 5:17/km",
                                   "cooldown": "4x20s strides, full recovery"}, "easy")
        self.assertEqual([s.get("numberOfIterations") for s in steps], [None, None, 4])

    def test_last_km_sentence_is_carved_out_of_the_long_run(self) -> None:
        warm, steady, finish, cool = self.steps("run", {
            "warmup": "10 min easy build",
            "main": "16 km steady, no faster than 5:17/km. Last 4 km @ 70.3 run effort, 4:41/km.",
            "cooldown": "10 min walk + legs up"}, "long")
        self.assertEqual((end(steady), end(finish)), (("distance", 12000.0), ("distance", 4000.0)))
        self.assertEqual(target(finish)[1:], band(281))
        self.assertEqual(target(cool)[0], "no.target")

    def test_time_block_at_a_written_pace_targets_that_pace(self) -> None:
        # Was the tempo zone (4:27-4:48) although the text says 4:41/km.
        warm, work, cool = self.steps("run", {
            "warmup": "15 min easy", "cooldown": "10 min easy",
            "main": "20 min @ 70.3 run effort, 4:41/km (steady tempo, just under threshold)"}, "tempo")
        self.assertEqual(end(work), ("time", 1200.0))
        self.assertEqual(target(work)[1:], band(281))

    def test_written_pace_range_is_the_band(self) -> None:
        (rep,) = self.steps("run", {"main": "5×4 min @ threshold (4:09-4:27/km) w/ 2 min jog"},
                            "threshold")
        work, jog = rep["workoutSteps"]
        self.assertEqual(target(work)[1:], (pace(267), pace(249)))
        self.assertEqual(end(jog), ("time", 120.0))
        self.assertEqual(target(jog)[2], pace(317))

    def test_interval_formats(self) -> None:
        cases = {
            "6×800 m @ 3:50/km w/ 400 m jog": (6, ("distance", 800.0), ("distance", 400.0)),
            "5×1 km at 4:05/km, 90 s jog between": (5, ("distance", 1000.0), ("time", 90.0)),
            "8×400 m @ 3:40/km w/ 200 m walk": (8, ("distance", 400.0), ("distance", 200.0)),
            "4×5 min @ 4:15-4:25/km w/ 2:00 jog": (4, ("time", 300.0), ("time", 120.0)),
            "3×2 km @ threshold, 2 min jog": (3, ("distance", 2000.0), ("time", 120.0)),
        }
        for text, (n, work_end, rec_end) in cases.items():
            with self.subTest(text):
                (rep,) = self.steps("run", {"main": text}, "threshold")
                work, rec = rep["workoutSteps"]
                self.assertEqual((rep["numberOfIterations"], end(work), end(rec)), (n, work_end, rec_end))
                self.assertEqual(target(work)[0], "pace.zone")

    def test_walk_recovery_is_a_rest_with_no_pace(self) -> None:
        (rep,) = self.steps("run", {"main": "8×400 m @ 3:40/km w/ 200 m walk"}, "vo2")
        rest = rep["workoutSteps"][1]
        self.assertEqual(rest["stepType"]["stepTypeKey"], "rest")
        self.assertEqual(target(rest)[0], "no.target")

    def test_hill_repeats_by_feel_with_jog_down_on_lap(self) -> None:
        warm, rep, cool = self.steps("run", {"warmup": "15 min easy + drills",
                                             "main": "6×60 s uphill @ hard, jog down",
                                             "cooldown": "10 min easy"}, "threshold")
        work, down = rep["workoutSteps"]
        self.assertEqual(target(work)[0], "no.target")
        self.assertEqual(end(down), ("lap.button", None))
        self.assertIn("drills", warm["description"])

    def test_sequence_with_commas_is_three_steps(self) -> None:
        steps = self.steps("run", {"main": "10 min easy, 20 min tempo, 10 min easy"}, "tempo")
        self.assertEqual([end(s) for s in steps], [("time", 600.0), ("time", 1200.0), ("time", 600.0)])
        self.assertEqual(target(steps[1])[1:], (pace(253.55 / 0.88), pace(253.55 / 0.95)))

    def test_header_and_progression(self) -> None:
        steps = self.steps("run", {"main": "60 min progression: 20 min easy, 20 min steady, "
                                           "20 min @ 4:41/km"}, "tempo")
        self.assertEqual(timed(steps), 3600.0)
        self.assertEqual(target(steps[2])[1:], band(281))

    def test_rounds_are_unrolled_because_garmin_cannot_nest(self) -> None:
        steps = self.steps("run", {"main": "2×(3×1 km @ 4:10/km w/ 1 min jog) w/ 3 min jog"}, "threshold")
        self.assertEqual([s["type"] for s in steps],
                         ["RepeatGroupDTO", "ExecutableStepDTO", "RepeatGroupDTO"])
        self.assertEqual(end(steps[1]), ("time", 180.0))
        (rep,) = self.steps("run", {"main": "4×(400 m @ 3:45/km, 200 m jog)"}, "vo2")
        work, jog = rep["workoutSteps"]
        self.assertEqual((rep["numberOfIterations"], end(work), end(jog)),
                         (4, ("distance", 400.0), ("distance", 200.0)))
        self.assertEqual(jog["stepType"]["stepTypeKey"], "recovery")

    def test_no_amount_runs_the_remaining_minutes_never_a_default(self) -> None:
        warm, main = self.steps("run", {"warmup": "10 min easy", "main": "Easy run by feel"},
                                "easy", duration=45)
        self.assertEqual(end(main), ("time", 2100.0))

    def test_minute_range_runs_the_lower_bound(self) -> None:
        (step,) = self.steps("run", {"main": "45-60 min easy"}, "easy")
        self.assertEqual(end(step), ("time", 2700.0))

    def test_steps_are_numbered_in_garmin_order(self) -> None:
        steps = self.steps("run", {"warmup": "15 min easy + 4×20 s strides w/ full recovery",
                                   "main": "3×8 min @ 4:41/km w/ 2 min jog", "cooldown": "10 min easy"})
        orders = []

        def walk(nodes):
            for n in nodes:
                orders.append(n["stepOrder"])
                if n["type"] == "RepeatGroupDTO":
                    walk(n["workoutSteps"])
        walk(steps)
        self.assertEqual(orders, list(range(1, len(orders) + 1)))


class BikeTests(_Fixture):
    def test_outdoor_recovery_is_never_read_as_watts(self) -> None:
        # "158 w/ 4 min" was read as 158 W: the recovery became a default 3 min.
        warm, openers, rep, cool = self.steps("bike", {
            "warmup": "15 min Z2 + 3×1 min openers w/ 1 min easy",
            "main": "3×10 min @ HR 139-158 w/ 4 min easy", "cooldown": "10 min spin"}, "tempo")
        work, easy = rep["workoutSteps"]
        self.assertEqual((end(work), end(easy)), (("time", 600.0), ("time", 240.0)))
        self.assertEqual(target(work)[1:], (139.0, 158.0))
        self.assertEqual(work["description"], "HR 139-158")
        self.assertEqual(openers["numberOfIterations"], 3)
        self.assertEqual([end(s) for s in openers["workoutSteps"]], [("time", 60.0), ("time", 60.0)])

    def test_race_blocks_inside_a_long_ride_keep_the_whole_ride(self) -> None:
        steps = self.steps("bike", {"main": "135 min Z2 @ HR 119-138 with 2×20 min @ 70.3 effort "
                                            "(HR 142-152) w/ 10 min Z2 inside the ride. Fuel 80-90 g carb/h."},
                           "race-specific")
        lead, rep, tail = steps
        self.assertEqual(timed(steps), 135 * 60.0)
        self.assertEqual(target(lead)[1:], (119.0, 138.0))
        work, z2 = rep["workoutSteps"]
        self.assertEqual(target(work)[1:], (142.0, 152.0))
        self.assertEqual((end(z2), target(z2)[1:]), (("time", 600.0), (119.0, 138.0)))

    def test_continuous_block_in_the_second_half(self) -> None:
        lead, block, tail = self.steps("bike", {
            "main": "180 min Z2 @ HR 119-138 with 40 min continuous @ 70.3 effort (HR 142-152) "
                    "in the second half, on aero bars"}, "race-specific")
        self.assertEqual([end(s)[1] / 60 for s in (lead, block, tail)], [90, 40, 50])
        self.assertEqual(target(block)[1:], (142.0, 152.0))

    def test_spin_ups_inside_a_z2_block(self) -> None:
        lead, rep, tail = self.steps("bike", {"main": "30 min Z2 @ HR 119-138 with 6×30 s "
                                                      "high-cadence spin-ups w/ 90 s easy"}, "Z2")
        self.assertEqual(timed([lead, rep, tail]), 1800.0)
        self.assertEqual(target(rep["workoutSteps"][0])[0], "no.target")

    def test_sprints_ride_by_feel(self) -> None:
        (rep,) = self.steps("bike", {"main": "8×30 s sprints w/ 4 min 30 s easy"}, "vo2")
        work, easy = rep["workoutSteps"]
        self.assertEqual(target(work)[0], "no.target")
        self.assertEqual(end(easy), ("time", 270.0))

    def test_hours_distance_and_sweet_spot(self) -> None:
        self.assertEqual(end(self.steps("bike", {"main": "2 h Z2 @ HR 119-138"}, "Z2")[0]), ("time", 7200.0))
        self.assertEqual(end(self.steps("bike", {"main": "1 h 30 min steady Z2"}, "Z2")[0]), ("time", 5400.0))
        self.assertEqual(end(self.steps("bike", {"main": "40 km steady"}, "Z2")[0]), ("distance", 40000.0))
        (rep,) = self.steps("bike", {"main": "3 × 15 min sweet spot, 5 min easy"}, "tempo")
        self.assertEqual([end(s) for s in rep["workoutSteps"]], [("time", 900.0), ("time", 300.0)])

    def test_indoor_watts_stay_a_note_and_hr_is_the_target(self) -> None:
        (rep,) = self.steps("bike", {"main": "4×8 min @ 250-270 W w/ 4 min easy"}, "threshold",
                            title="Peloton threshold")
        work = rep["workoutSteps"][0]
        self.assertEqual(target(work)[0], "heart.rate.zone")
        self.assertIn("Peloton 250-270 W", work["description"])


class SwimTests(_Fixture):
    def test_sets_rests_strokes_and_equipment(self) -> None:
        warm, drill, main, cool = self.steps("swim", {
            "warmup": "300 easy + 6×50 drill (catch-up, fingertip drag), 15s rest",
            "main": "6×100 smooth @ easy aerobic, 20s rest, count strokes per length",
            "cooldown": "100 easy backstroke"}, "Z2")
        self.assertEqual(end(warm), ("distance", 300.0))
        self.assertEqual([end(s) for s in drill["workoutSteps"]], [("distance", 50.0), ("time", 15.0)])
        self.assertIn("count strokes", main["workoutSteps"][0]["description"])
        self.assertEqual(cool["strokeType"]["strokeTypeKey"], "backstroke")
        steps = self.steps("swim", {"main": "4×100 pull buoy, 15s rest"})
        self.assertEqual(steps[0]["workoutSteps"][0]["equipmentType"]["equipmentTypeKey"], "pull_buoy")

    def test_no_rest_is_invented(self) -> None:
        (rep,) = self.steps("swim", {"main": "4×50 drill"})
        self.assertEqual(len(rep["workoutSteps"]), 1)

    def test_rounds_km_and_time_swims(self) -> None:
        steps = self.steps("swim", {"main": "3×(4×100 @ CSS, 10s rest), 1 min between rounds"})
        self.assertEqual([s["type"] for s in steps], ["RepeatGroupDTO", "ExecutableStepDTO"] * 2
                         + ["RepeatGroupDTO"])
        self.assertEqual(end(steps[1]), ("time", 60.0))
        self.assertEqual(end(self.steps("swim", {"main": "Swim 2 km steady"})[0]), ("distance", 2000.0))
        self.assertEqual(end(self.steps("swim", {"main": "30 min continuous"})[0]), ("time", 1800.0))

    def test_placeholder_text_is_a_lap_step_not_a_made_up_distance(self) -> None:
        warm, main, cool = self.steps("swim", {"warmup": "existing swim warmup",
                                               "main": "endurance set as planned",
                                               "cooldown": "200m easy"})
        self.assertEqual((end(warm), end(main)), (("lap.button", None), ("lap.button", None)))
        self.assertEqual(end(cool), ("distance", 200.0))


class BrickTests(_Fixture):
    def bricks(self, structure: dict, intensity: str = "race-specific") -> tuple[list, list]:
        wo = self.build("brick", structure, intensity)
        self.assertEqual(wo["sportType"]["sportTypeKey"], "multi_sport")
        bike, run = wo["workoutSegments"]
        return bike["workoutSteps"], run["workoutSteps"]

    def test_run_leg_at_a_written_pace_is_that_pace(self) -> None:
        # Was a threshold-derived 4:08-4:22 for "4:41/km".
        _, (run,) = self.bricks({"main": "60 min Z2", "run": "10 min run off the bike @ 70.3 run effort, 4:41/km"})
        self.assertEqual(target(run)[1:], band(281))
        self.assertTrue(run["description"].startswith("run off the bike"))

    def test_structured_run_leg(self) -> None:
        _, (rep,) = self.bricks({"main": "75 min Z2 @ HR 119-138 with 3×10 min @ 70.3 effort "
                                         "(HR 142-152) w/ 5 min easy",
                                 "run": "2×10 min @ 4:41/km w/ 2 min jog"})
        work, jog = rep["workoutSteps"]
        self.assertEqual((rep["numberOfIterations"], end(work), end(jog)), (2, ("time", 600.0), ("time", 120.0)))

    def test_distance_run_leg_and_relaxed_leg(self) -> None:
        _, (run,) = self.bricks({"main": "90 min Z2 @ HR 119-138", "run": "3 km @ 4:41/km"})
        self.assertEqual(end(run), ("distance", 3000.0))
        _, (easy,) = self.bricks({"main": "20 min easy", "run": "10 min run off the bike, relaxed, "
                                                               "no faster than 5:17/km"}, "race pace")
        self.assertEqual(target(easy)[2], pace(317))

    def test_legacy_then_run_phrasing_splits_the_legs(self) -> None:
        bike, (run,) = self.bricks({"main": "90 min Z2 then 20 min run @ race pace"}, "race pace")
        self.assertEqual(timed(bike), 5400.0)
        self.assertEqual(end(run), ("time", 1200.0))
        self.assertEqual(target(run)[1:], band(281))

    def test_numbering_continues_across_both_legs(self) -> None:
        bike, run = self.bricks({"warmup": "15 min Z2", "main": "2×20 min @ 70.3 effort (HR 142-152) "
                                                                "w/ 5 min easy", "cooldown": "5 min spin",
                                 "run": "15 min run off the bike, steady, no faster than 5:17/km"})
        self.assertEqual(run[0]["stepOrder"], 6)


class WholeBlockTests(_Fixture):
    """Every session the 70.3 plan generates, pushed and read back."""

    def sessions(self):
        day = datetime.date(2026, 10, 20)
        while day < RACE:
            session, phase = hip.workout_for_date(day, RACE)
            yield day, phase, session
            day += datetime.timedelta(days=1)

    def test_every_generated_session_matches_its_text(self) -> None:
        import re
        seen = 0
        for day, phase, session in self.sessions():
            wo = gw.build_workout(session, day.isoformat(), thr_pace=253.5)
            if wo is None:
                self.assertTrue(session["is_rest"] or session["discipline"] in ("rest", "race"))
                continue
            seen += 1
            st = session["structure"]
            text = " ".join(str(st.get(k) or "") for k in ("warmup", "main", "cooldown", "run"))
            steps = [s for seg in wo["workoutSegments"] for s in seg["workoutSteps"]]

            def walk(nodes):
                for n in nodes:
                    yield n
                    if n["type"] == "RepeatGroupDTO":
                        yield from walk(n["workoutSteps"])
            flat = list(walk(steps))
            with self.subTest(day=day.isoformat(), title=session["title"]):
                # Every repeat the text writes is on the watch, with its count.
                written = sorted(int(n) for n in re.findall(r"(\d+)\s*×", text))
                encoded = sorted(n["numberOfIterations"] for n in flat if n["type"] == "RepeatGroupDTO")
                self.assertEqual(encoded, written)
                # Nothing ends on the lap button except a written "full recovery" / "jog down".
                laps = [n for n in flat if n["type"] == "ExecutableStepDTO"
                        and n["endCondition"]["conditionTypeKey"] == "lap.button"]
                if laps:
                    self.assertRegex(text, r"full recovery|jog down")
                # No outdoor watts anywhere.
                self.assertNotRegex(str(wo), r"\d\s*W\b")
                # Sessions written only in minutes add up to their duration.
                if not re.search(r"\d\s*(?:km|m)\b|\d{3,4}\s+(?:easy|continuous)|×\d{2,3}\b|full recovery|jog down",
                                 text) and session["discipline"] != "swim":
                    self.assertEqual(timed(steps), session["duration_min"] * 60.0)
                # A stated pace is encoded as that pace.
                if "4:41/km" in text and session["discipline"] in ("run", "brick"):
                    paces = {target(n)[1:] for n in flat if n["type"] == "ExecutableStepDTO"}
                    self.assertIn(band(281), paces)
        self.assertGreater(seen, 150)


class GoalPaceTests(unittest.TestCase):
    def test_triathlon_finish_time_is_never_divided_by_the_run(self) -> None:
        # "sub 6:00" over 18 km gave the T100 profile a 20:00/km race pace.
        event = {"mode": "TRIATHLON", "goal": {"target": "sub 6:00"}, "distances": {"run_km": 18}}
        with patch.object(coaching_contract, "event_context", return_value=event):
            self.assertIsNone(marathon_plan.goal_pace_per_km())
        event["pacing_targets"] = {"run_goal_min_per_km": "4:41"}
        with patch.object(coaching_contract, "event_context", return_value=event):
            self.assertEqual(marathon_plan.goal_pace_per_km(), "4:41")


class FrontendTests(unittest.TestCase):
    """The plan card shows the same blocks the watch is sent."""

    def test_brick_run_leg_is_drawn_and_kept_when_sessions_split(self) -> None:
        import pathlib
        page = (pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
        self.assertIn("const raw=[st.warmup,st.main,st.cooldown,st.run]", page)
        self.assertIn('...(primary==="brick"?{run:st.run}:{})', page)


if __name__ == "__main__":
    unittest.main()
