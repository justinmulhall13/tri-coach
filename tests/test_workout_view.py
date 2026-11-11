"""The workout sheet shows the steps the watch runs, laid out to read.

The athlete could not follow a session printed as one line of text ("3x10 min @
HR 139-158 w/ 4 min easy"). `workout_view.build` reshapes the watch workout into
phases, repeat groups and one target per row. These tests pin that shape on
every kind of session the 70.3 block writes, and that the sheet never shows a
step the watch would not run.
"""
from __future__ import annotations

import datetime
import pathlib
import time
import unittest
from unittest.mock import patch

from app import coaching_contract, garmin_workout as gw, half_iron_plan as hip, nutrition, workout_view as wv, zones

RUN_HR = {1: (101, 120), 2: (121, 140), 3: (141, 160), 4: (161, 180), 5: (181, 201)}
BIKE_HR = {1: (100, 118), 2: (119, 138), 3: (139, 158), 4: (159, 178), 5: (179, 199)}
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
THR = 253.5
INDEX = pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html"


def sess(disc, title, intensity, dur, warm, main, cool, run=None):
    st = {"warmup": warm, "main": main, "cooldown": cool}
    if run:
        st["run"] = run
    return {"discipline": disc, "title": title, "intensity": intensity,
            "duration_min": dur, "structure": st, "is_rest": 0}


def phases(view, leg=0):
    return {p["key"]: p for p in view["legs"][leg]["phases"]}


def flat(items, times=1):
    for item in items:
        if item["type"] == "repeat":
            yield from flat(item["items"], times * item["count"])
        else:
            yield item, times


def watch_steps(nodes, times=1):
    for node in nodes:
        if node.get("type") == "RepeatGroupDTO":
            yield from watch_steps(node["workoutSteps"], times * node["numberOfIterations"])
        else:
            yield node, times


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        saved = dict(zones._CACHE)
        zones._CACHE.update(data=ZONES, ts=time.time() + 1e9)
        self.addCleanup(lambda: (zones._CACHE.clear(), zones._CACHE.update(saved)))
        ctx = patch.object(coaching_contract, "event_context", return_value=EVENT)
        ctx.start()
        self.addCleanup(ctx.stop)


class BikeIntervals(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.view = wv.build(sess("bike", "Bike — tempo", "tempo", 73,
                                  "15 min Z2 + 3×1 min openers w/ 1 min easy",
                                  "3×10 min @ HR 139-158 w/ 4 min easy", "10 min spin"))

    def test_phases_in_order_with_totals(self) -> None:
        self.assertEqual(self.view["kind"], "steps")
        self.assertEqual([p["key"] for p in self.view["legs"][0]["phases"]],
                         ["warmup", "main", "cooldown"])
        ph = phases(self.view)
        self.assertEqual((ph["warmup"]["total"], ph["main"]["total"]), ("21 min", "42 min"))
        # One row under a heading already says how long it is.
        self.assertNotIn("total", ph["cooldown"])

    def test_the_set_is_a_repeat_with_work_and_recovery(self) -> None:
        rep = phases(self.view)["main"]["items"][0]
        self.assertEqual((rep["type"], rep["count"], rep["amount"]), ("repeat", 3, "3 × 10 min"))
        work, rec = rep["items"]
        self.assertEqual((work["amount"], work["name"], work["target"]), ("10 min", "Tempo", "HR 139-158"))
        self.assertEqual((rec["amount"], rec["name"]), ("4 min", "Easy spin"))
        self.assertNotIn("target", rec)   # the text gives the recovery no number

    def test_openers_belong_to_the_warm_up(self) -> None:
        items = phases(self.view)["warmup"]["items"]
        self.assertEqual(items[0]["name"], "Z2")
        self.assertEqual((items[1]["type"], items[1]["count"]), ("repeat", 3))
        self.assertEqual(items[1]["items"][0]["name"], "Openers")

    def test_figures_name_duration_set_and_target(self) -> None:
        self.assertEqual([(f["label"], f["value"]) for f in self.view["figures"]],
                         [("Duration", "73 min"), ("Main set", "3 × 10 min"), ("Target", "HR 139-158")])
        self.assertEqual(self.view["figures"][2]["sub"], "Tempo")
        self.assertEqual(self.view["axis"], {"unit": "time", "total": "73 min"})

    def test_harder_steps_are_drawn_taller(self) -> None:
        rep = phases(self.view)["main"]["items"][0]
        self.assertGreater(rep["items"][0]["level"], rep["items"][1]["level"])
        self.assertEqual(rep["items"][0]["w"], 600.0)


class Runs(_Fixture):
    def test_easy_run_and_strides_is_one_block_then_strides(self) -> None:
        v = wv.build(sess("run", "Easy run + strides", "easy", 45, "—",
                          "40 min easy, no faster than 5:17/km; 6×20 s strides w/ full recovery", "—"),
                     thr_pace=THR)
        self.assertEqual([p["key"] for p in v["legs"][0]["phases"]], ["main"])
        easy, strides = v["legs"][0]["phases"][0]["items"]
        self.assertEqual((easy["amount"], easy["target"], easy["target_note"]),
                         ("40 min", "5:17/km", "or slower"))
        self.assertEqual((strides["count"], strides["items"][0]["name"]), (6, "Stride"))
        rec = strides["items"][1]
        self.assertEqual((rec["unit"], rec["amount"]), ("open", "Open"))   # never an invented time
        # Strides do not outrank the run in the summary line.
        self.assertEqual([(f["label"], f["value"]) for f in v["figures"]],
                         [("Duration", "45 min"), ("Main set", "40 min"), ("Target", "5:17/km")])
        self.assertEqual(v["figures"][2]["sub"], "or slower")   # the same words as the row
        self.assertIsNone(v["axis"])   # an open step means no labelled axis

    def test_threshold_run_shows_the_written_pace_band(self) -> None:
        v = wv.build(sess("run", "Run — threshold", "threshold", 57,
                          "15 min easy + 4×20 s strides w/ full recovery",
                          "5×4 min @ threshold (4:09-4:27/km) w/ 2 min jog", "10 min easy"), thr_pace=THR)
        rep = phases(v)["main"]["items"][0]
        self.assertEqual((rep["count"], rep["items"][0]["name"], rep["items"][0]["target"]),
                         (5, "Threshold", "4:09-4:27/km"))
        self.assertEqual(rep["items"][1]["name"], "Easy jog")

    def test_a_distance_run_keeps_its_distance(self) -> None:
        v = wv.build(sess("run", "Long run — 10 km", "long", 61, "10 min easy build",
                          "10 km steady, no faster than 5:17/km.", "10 min walk + legs up"), thr_pace=THR)
        main = phases(v)["main"]["items"][0]
        self.assertEqual((main["amount"], main["unit"]), ("10 km", "distance"))
        # Drawn across the minutes the warm up and cool down leave; never shown as a number.
        self.assertAlmostEqual(main["w"], 61 * 60 - 1200)
        self.assertNotIn("total", phases(v)["main"])

    def test_a_finish_carved_out_of_a_long_run_keeps_the_whole_distance(self) -> None:
        v = wv.build(sess("run", "Long run — 16 km", "long", 97, "10 min easy build",
                          "16 km steady, no faster than 5:17/km. Last 4 km @ 4:41/km.",
                          "10 min walk + legs up"), thr_pace=THR)
        main = phases(v)["main"]
        self.assertEqual([i["amount"] for i in main["items"]], ["12 km", "4 km"])
        self.assertEqual(main["total"], "16 km")
        self.assertIn(("Distance", "16 km"), [(f["label"], f["value"]) for f in v["figures"]])

    def test_text_with_no_amount_is_shown_as_written(self) -> None:
        v = wv.build(sess("run", "Fartlek", "tempo", 40, "easy jog to the park",
                          "fartlek by feel around the loop, surge the hills", "jog home"), thr_pace=THR)
        main = phases(v)["main"]["items"][0]
        self.assertEqual(main["name"], "Fartlek by feel around the loop, surge the hills")
        self.assertEqual(main["amount"], "40 min")
        self.assertNotIn("cue", main)


class WhatThePlanWrote(_Fixture):
    """Review findings: the sheet must not restate the plan as something else."""

    def test_a_cue_that_says_ceiling_does_not_relabel_a_stated_pace(self) -> None:
        v = wv.build(sess("run", "Run — race effort", "tempo", 45, "15 min easy",
                          "20 min at 4:41/km. HR ceiling 165.", "10 min easy"), thr_pace=THR)
        main = phases(v)["main"]["items"][0]
        self.assertEqual((main["name"], main["target"]), ("Race pace", "4:41/km"))
        self.assertNotIn("target_note", main)
        self.assertGreater(main["level"], phases(v)["warmup"]["items"][0]["level"])

    def test_a_lap_button_warm_up_keeps_its_ceiling_and_a_plus_minus_cue_keeps_the_range(self) -> None:
        v = wv.build(sess("run", "Track", "threshold", 60, "easy jog to the track",
                          "5×4 min @ threshold (4:09-4:27/km) w/ 2 min jog. Cadence 180 ±5.", "10 min easy"),
                     thr_pace=THR)
        warm = phases(v)["warmup"]["items"][0]
        self.assertEqual((warm["amount"], warm["target"], warm["target_note"]), ("Open", "5:17/km", "or slower"))
        work = phases(v)["main"]["items"][0]["items"][0]
        self.assertEqual(work["target"], "4:09-4:27/km")

    def test_the_athletes_own_hr_note_stays_as_a_cue(self) -> None:
        v = wv.build(sess("run", "Run", "tempo", 45, "15 min easy", "20 min at 4:41/km. HR ceiling 165.",
                          "10 min easy"), thr_pace=THR)
        self.assertEqual(phases(v)["main"]["items"][0]["cue"], "HR ceiling 165")

    def test_a_written_range_stays_a_range(self) -> None:
        v = wv.build(sess("run", "Easy run", "easy", 60, "—", "45-60 min easy", "—"), thr_pace=THR)
        row = v["legs"][0]["phases"][0]["items"][0]
        self.assertEqual((row["amount"], row["value"]), ("45-60 min", 2700.0))
        self.assertIsNone(v["axis"])

    def test_a_trailing_count_is_read_as_the_repeat_it_is(self) -> None:
        v = wv.build(sess("run", "Mile repeats", "threshold", 60, "15 min easy",
                          "1 mile repeats x4 w/ 90 s jog", "10 min easy"), thr_pace=THR)
        rep = phases(v)["main"]["items"][0]
        self.assertEqual((rep["count"], rep["amount"]), (4, "4 × 1 mi"))
        self.assertEqual([(i["amount"], i["name"]) for i in rep["items"]],
                         [("1 mi", "Threshold"), ("90 s", "Easy jog")])

    def test_repeats_the_parser_cannot_read_are_shown_as_text(self) -> None:
        v = wv.build(sess("run", "Race pace", "tempo", 60, "15 min easy",
                          "5 km @ 4:41/km x2", "10 min easy"), thr_pace=THR)
        self.assertEqual(v["kind"], "list")
        self.assertIn("5 km @ 4:41/km x2", [r["name"] for r in v["rows"]])

    def test_recovery_between_sets_is_drawn_between_them(self) -> None:
        v = wv.build(sess("run", "400s", "vo2", 60, "15 min easy",
                          "2×(4×400 m @ 3:45/km w/ 200 m jog), 3 min jog between sets", "10 min easy"),
                     thr_pace=THR)
        items = phases(v)["main"]["items"]
        self.assertEqual([i["type"] for i in items], ["repeat", "step", "repeat"])
        self.assertEqual((items[1]["amount"], items[1]["role"]), ("3 min", "recovery"))
        self.assertIn(("Main set", "2 × 4 × 400 m"), [(f["label"], f["value"]) for f in v["figures"]])

    def test_miles_and_odd_seconds_are_not_mangled(self) -> None:
        self.assertEqual(wv._distance_label(1609.344, False), "1 mi")
        self.assertEqual(wv._distance_label(21082.4064, False), "13.1 mi")
        self.assertEqual(wv._distance_label(10000, False), "10 km")
        self.assertEqual((wv._time_label(90), wv._time_label(1650), wv._time_label(1425)),
                         ("90 s", "27:30", "23:45"))

    def test_fuel_is_not_printed_on_a_step_row(self) -> None:
        w, _ = hip.workout_for_date(datetime.date(2027, 2, 6), RACE)
        v = wv.build(w, thr_pace=THR)
        cues = " ".join(s.get("cue", "") for leg in v["legs"] for p in leg["phases"] for s, _n in flat(p["items"]))
        self.assertNotIn("carb", cues.lower())
        self.assertIn("Fuel 80-90 g carb/h", v["written"][1]["text"])   # still there as written

    def test_by_feel_gets_no_number(self) -> None:
        v = wv.build(sess("bike", "Ride", "Z2", 60, "—", "ride by feel, no targets today", "—"))
        row = v["legs"][0]["phases"][0]["items"][0]
        self.assertEqual(row["name"], "Ride by feel, no targets today")
        self.assertNotIn("target", row)
        self.assertEqual([f["label"] for f in v["figures"]], ["Duration"])

    def test_a_note_number_is_kept(self) -> None:
        v = wv.build(sess("run", "Run", "threshold", 55, "15 min easy + 4 strides",
                          "4×6 min @ threshold w/ 2 min jog", "10 min easy"), thr_pace=THR)
        self.assertEqual(phases(v)["warmup"]["items"][0]["cue"], "4 strides")

    def test_swim_joiner_easy_notes_and_rounds(self) -> None:
        v = wv.build(sess("swim", "Swim", "threshold", 50, "400 easy, nothing fast",
                          "8x100 m @ CSS w/ 15s rest", "200 easy, no hard efforts"))
        work = phases(v)["main"]["items"][0]["items"][0]
        self.assertEqual((work["name"], work["rest"]), ("CSS", "15 s rest"))
        self.assertEqual(phases(v)["warmup"]["items"][0]["level"], 1.0)
        self.assertEqual(phases(v)["cooldown"]["items"][0]["level"], 1.0)
        rounds = wv.build(sess("swim", "Swim", "threshold", 50, "300 easy",
                               "3x(4x100 @ CSS, 10s rest), 1 min between rounds", "200 easy"))
        self.assertEqual(rounds["notes"], [])   # a round's body is not an aside
        self.assertIn(("Main set", "3 × 4 × 100 m"), [(f["label"], f["value"]) for f in rounds["figures"]])

    def test_a_brick_with_no_run_written_prints_no_pace(self) -> None:
        v = wv.build(sess("brick", "Brick", "race-specific", 70, "10 min easy", "50 min @ HR 142-152",
                          "10 min spin"), thr_pace=THR)
        run = v["legs"][1]["phases"][0]["items"][0]
        self.assertEqual((run["amount"], run["name"]), ("Open", "Run"))
        self.assertNotIn("target", run)


class Swims(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        w, _ = hip.workout_for_date(datetime.date(2026, 11, 2), RACE)
        self.assertEqual(w["discipline"], "swim")
        self.view = wv.build(w)

    def test_distance_axis_and_figure(self) -> None:
        self.assertEqual(self.view["axis"], {"unit": "distance", "total": "1,400 m"})
        self.assertIn(("Distance", "1,400 m"), [(f["label"], f["value"]) for f in self.view["figures"]])

    def test_a_drill_names_itself_and_keeps_both_halves(self) -> None:
        drill = phases(self.view)["warmup"]["items"][1]
        self.assertEqual((drill["amount"], drill["name"]), ("100 m", "6-second switch"))
        self.assertEqual(len(drill["detail"]), 2)
        self.assertTrue(drill["detail"][0].startswith("50 drill:"))
        # The watch description is cut at 80 characters; the sheet is not.
        self.assertTrue(drill["detail"][1].endswith("every stroke"))

    def test_a_rest_reads_as_part_of_the_swim_before_it(self) -> None:
        items = phases(self.view)["warmup"]["items"]
        self.assertEqual(items[1]["rest"], "20 s rest")
        self.assertTrue(items[2]["fold"])
        rep = phases(self.view)["main"]["items"][0]
        self.assertEqual((rep["amount"], rep["items"][0]["rest"]), ("6 × 100 m", "20 s rest"))
        self.assertEqual(rep["items"][0]["cue"], "Count strokes per length")


class CoachWrittenSwim(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.view = wv.build(sess(
            "swim", "Swim — endurance", "Z2-tempo", 70,
            "400 easy + 6×50 drill @ 15 sec rest (stroke control focus)",
            "3×500 @ Z2 effort, 45 sec rest between each (sight to far-end wall marker every 6 strokes "
            "on rep 2); 90 sec free rest at wall; 4×100 hard-controlled effort, 30 sec rest between each "
            "— record every split", "200 easy"))

    def test_an_aside_after_the_rest_stays_with_its_set(self) -> None:
        drill = phases(self.view)["warmup"]["items"][1]["items"][0]
        self.assertEqual((drill["name"], drill["cue"], drill["rest"]),
                         ("Drill", "Stroke control focus", "15 s rest"))

    def test_asides_no_step_carries_are_listed_not_lost(self) -> None:
        self.assertEqual(self.view["notes"], ["Sight to far-end wall marker every 6 strokes on rep 2",
                                              "Record every split"])

    def test_a_written_swim_pace_is_the_target(self) -> None:
        v = wv.build(sess("swim", "Swim — endurance", "Z2-tempo", 50, "300 easy",
                          "1900 continuous @ race effort (1:57/100m), sighting every 6 strokes", "200 easy"))
        main = phases(v)["main"]["items"][0]
        self.assertEqual((main["amount"], main["name"], main["target"], main["cue"]),
                         ("1,900 m", "Continuous, race effort", "1:57/100 m", "Sighting every 6 strokes"))
        self.assertEqual(main["level"], 4.0)
        self.assertEqual(v["notes"], [])


class Bricks(_Fixture):
    def test_two_legs_and_the_run_keeps_its_pace(self) -> None:
        v = wv.build(sess("brick", "Long ride + brick", "race-specific", 175, "20 min Z2 build",
                          "135 min Z2 @ HR 119-138 with 2×20 min @ 70.3 effort (HR 142-152) w/ 10 min Z2 "
                          "inside the ride. Fuel 80-90 g carb/h and 800-900 mg sodium/h.",
                          "10 min easy spin", run="10 min run off the bike @ 70.3 run effort, 4:41/km"),
                     thr_pace=THR)
        self.assertEqual([leg["label"] for leg in v["legs"]], ["Bike", "Run"])
        rep = next(i for i in phases(v)["main"]["items"] if i["type"] == "repeat")
        self.assertEqual((rep["items"][0]["name"], rep["items"][0]["target"]), ("Race effort", "HR 142-152"))
        run = v["legs"][1]["phases"][0]
        self.assertEqual(run["label"], "Run off the bike")
        self.assertEqual((run["items"][0]["name"], run["items"][0]["target"]), ("Race pace", "4:41/km"))
        self.assertEqual(v["figures"][0], {"label": "Duration", "value": "2 h 55"})


class OtherKinds(_Fixture):
    def test_rest_race_and_strength(self) -> None:
        rest = wv.build({"discipline": "rest", "is_rest": 1, "structure": {"main": "Full rest"}})
        self.assertEqual((rest["kind"], rest["text"]), ("rest", "Full rest"))
        race = wv.build(hip.race_day())
        self.assertEqual(race["kind"], "text")
        self.assertEqual(race["blocks"][0]["label"], "Before the start")
        lift = wv.build({"discipline": "strength", "duration_min": 45, "structure": {"blocks": [
            {"label": "3 sets", "text": "Romanian Deadlift (Barbell)", "target": "controlled"}]}})
        self.assertEqual(lift["kind"], "list")
        self.assertEqual(lift["rows"][0], {"amount": "3 sets", "name": "Romanian Deadlift (Barbell)",
                                           "cue": "controlled"})

    def test_garbage_does_not_raise(self) -> None:
        for bad in (None, {}, {"discipline": "bike"}, {"discipline": "swim", "structure": "x"},
                    {"discipline": "bike", "duration_min": float("inf"), "structure": {"main": "30 min Z2"}},
                    {"discipline": "run", "structure": {"main": None}, "duration_min": "abc"}):
            self.assertIn(wv.build(bad)["kind"], {"steps", "list", "rest", "text"})


class SheetMatchesWatch(_Fixture):
    """Walk the whole 70.3 block: every step on the sheet is a step on the watch."""

    def test_every_generated_session(self) -> None:
        day, seen = datetime.date(2026, 11, 2), 0
        while day <= RACE:
            w, _phase = hip.workout_for_date(day, RACE)
            day += datetime.timedelta(days=1)
            if w.get("is_rest") or w["discipline"] in {"rest", "race"}:
                continue
            view = wv.build(w, thr_pace=THR)
            self.assertEqual(view["kind"], "steps", w["title"])
            workout = gw.build_workout(w, "", thr_pace=THR)
            # Asking for the phases never changes what the watch is sent.
            self.assertEqual(workout, gw.build_workout(w, "", thr_pace=THR, phases=[]), w["title"])
            for leg, segment in zip(view["legs"], workout["workoutSegments"], strict=True):
                shown = list(flat([i for p in leg["phases"] for i in p["items"]]))
                real = list(watch_steps(segment["workoutSteps"]))
                self.assertEqual(len(shown), len(real), w["title"])
                for (step, n), (node, m) in zip(shown, real):
                    self.assertEqual(n, m, w["title"])
                    end = node["endCondition"]["conditionTypeKey"]
                    self.assertEqual(step["unit"], {"time": "time", "distance": "distance"}.get(end, "open"))
                    self.assertEqual(step["value"], node["endConditionValue"])
                    one, two = node.get("targetValueOne"), node.get("targetValueTwo")
                    if node["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone" and one and two:
                        self.assertEqual(step["target"], f"HR {int(one)}-{int(two)}", w["title"])
                    self.assertTrue(step["name"], w["title"])
                    self.assertGreater(step["w"], 0, w["title"])
            self.assertLessEqual(len(view["figures"]), 4)
            seen += 1
        self.assertGreater(seen, 150)

    def test_asking_for_phases_does_not_change_the_workout(self) -> None:
        w, _ = hip.workout_for_date(datetime.date(2027, 2, 6), RACE)
        self.assertEqual(gw.build_workout(w, "", thr_pace=THR),
                         gw.build_workout(w, "", thr_pace=THR, phases=[]))


class SheetPage(unittest.TestCase):
    """The sheet's page code: what it calls exists, and the gut material is gone."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_helpers_the_sheet_calls_are_defined(self) -> None:
        for name in ("woPayload", "woDur", "woClock", "woLv", "woMeta", "woTextView", "woStepRow",
                     "woItems", "woIndex", "woChart", "woSelect", "renderWoView", "loadWoView",
                     "renderFuel", "openWorkoutDetail"):
            self.assertRegex(self.html, rf"function {name}\(", name)
        for element in ("wo-figs", "wo-chart", "wo-steps", "wo-why", "wo-fuel", "wo-sched", "wo-time", "wo-date"):
            self.assertIn(f'id="{element}"', self.html)
        self.assertNotIn("workoutBlocks(", self.html)

    def test_model_written_text_is_escaped_at_every_sink(self) -> None:
        for sink in ('<div class="ws-name">${show(s.name)}</div>', '${esc(s.target)}', '${show(s.cue)}',
                     '${show(f.placement)}', '<div class="ws-line">${show(n)}</div>', '${show(w.text)}',
                     '${show(r.name)}', "'flag':''}\">${show(n)}</div>"):
            self.assertIn(sink, self.html, sink)
        self.assertNotIn("• ${n}", self.html)
        self.assertNotIn('". "+c.note', self.html)

    def test_send_to_watch_sends_the_sheet_it_is_on(self) -> None:
        # Another day's sheet goes to that day as itself; an empty body would
        # push today's session. The sheet acts on its own snapshot, because the
        # dashboard refreshes _woDay and _woSessions behind it.
        self.assertIn("date:dated?day:undefined", self.html)
        self.assertIn("session:dated?woPayload(x):{", self.html)
        self.assertIn('(!x||(x.kind==="main"&&!dated)) ? "{}"', self.html)
        push = self.html[self.html.index("async function pushToWatch()"):self.html.index("// ---- Activity deep dive")]
        self.assertIn("_woOpen", push)
        self.assertNotIn("_woSessions[", push)
        self.assertNotIn("_woDay", push)

    def test_the_log_form_only_shows_on_todays_sheet(self) -> None:
        self.assertIn('$("fb-wrap").style.display=(!s._planDate||s._planDate===localToday())?"":"none";', self.html)

    def test_no_gut_material_in_the_sheet(self) -> None:
        for gone in ("GI distress", "f.abort", "glucose_ceiling", "fructose_ceiling", "f.ratio",
                     "Show the arithmetic", "fuel-hero", "Priority ·"):
            self.assertNotIn(gone, self.html, gone)
        # The sheet's own fuel block prints no raw recipe or Coach-facing note.
        fuel = self.html[self.html.index("async function renderFuel(x){"):self.html.index("function completedBanner(")]
        for gone in ("f.recipe", "f.arithmetic", "f.caffeine", "f.assumptions", "show(f.note||"):
            self.assertNotIn(gone, fuel, gone)

    def test_chart_is_grey_ramp_only(self) -> None:
        start = self.html.index("/* Workout sheet.")
        block = self.html[start:self.html.index(".wo-sched{", start)]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(")
        self.assertNotIn("background:var(--ink)", block)


class FuelMix(_Fixture):
    def test_plan_carries_the_bottle_as_plain_amounts(self) -> None:
        plan = nutrition.fueling_plan({"discipline": "bike", "duration_min": 95})
        self.assertEqual(plan["mix"]["glucose_powder_g"], 60)
        self.assertEqual(plan["mix"]["table_sugar_g"], 0)
        self.assertTrue(plan["mix"]["salt"].endswith("tsp"))

    def test_a_rate_the_session_writes_is_the_one_the_panel_shows(self) -> None:
        # The athlete's call on 2026-11-10: the plan text wins over the duration band.
        w, _ = hip.workout_for_date(datetime.date(2027, 2, 6), RACE)   # long brick, "Fuel 80-90 g carb/h"
        plan = nutrition.fueling_plan({"discipline": "brick", "duration_min": 165, "duration_scope": "bike_leg",
                                       "structure": w["structure"]})
        self.assertEqual(plan["carb_range_g_per_hr"], [80, 90])
        self.assertEqual((plan["carb_g_per_hr"], plan["carb_rate_source"]), (85, "session text"))
        self.assertEqual(plan["glucose_g_per_hr"] + plan["fructose_g_per_hr"], 85)
        self.assertLessEqual(plan["glucose_g_per_hr"], 60)
        self.assertLessEqual(plan["fructose_g_per_hr"], 30)
        self.assertEqual(plan["total_carb_g"][0], round(85 * 165 / 60))
        # A long base ride that writes 60 g/h gets 60, not the over-150-min default.
        ride = nutrition.fueling_plan({"discipline": "bike", "duration_min": 165, "structure": {
            "main": "140 min steady Z2 @ HR 119-138, fuel 60 g carb/h"}})
        self.assertEqual((ride["carb_g_per_hr"], ride["fructose_g_per_hr"]), (60, 0))
        # No rate written: the duration band still decides. A rate past the ceilings is held at 90.
        self.assertEqual(nutrition.fueling_plan({"discipline": "bike", "duration_min": 180})["carb_g_per_hr"], 75)
        self.assertEqual(nutrition.fueling_plan({"discipline": "bike", "duration_min": 180})["carb_rate_source"],
                         "duration band")
        capped = nutrition.fueling_plan({"discipline": "bike", "duration_min": 180,
                                         "structure": {"main": "3 h Z2, fuel 100-120 g carbs per hour"}})
        self.assertEqual(capped["carb_g_per_hr"], 90)
        self.assertIsNone(capped["carb_range_g_per_hr"])

    def test_the_sodium_a_long_brick_writes_is_the_panels(self) -> None:
        # The athlete's own figure (fueling_reference, 800-900 mg/L at 1 L/h),
        # confirmed 2026-11-10. The plan text used to say 600-800.
        import re
        w, _ = hip.workout_for_date(datetime.date(2027, 2, 6), RACE)
        written = re.search(r"(\d+)-(\d+) mg sodium/h", w["structure"]["main"])
        plan = nutrition.fueling_plan({"discipline": "brick", "duration_min": 165, "duration_scope": "bike_leg",
                                       "structure": w["structure"]})
        self.assertEqual([int(written.group(1)), int(written.group(2))], plan["sodium_mg_per_hr"])

    def test_every_written_rate_in_the_block_matches_the_panel(self) -> None:
        day = datetime.date(2026, 11, 2)
        checked = 0
        while day <= RACE:
            w, _ = hip.workout_for_date(day, RACE)
            day += datetime.timedelta(days=1)
            stated = nutrition.stated_carb_rate(w)
            if not stated or (w.get("duration_min") or 0) < 60:
                continue
            plan = nutrition.fueling_plan({"discipline": w["discipline"], "duration_min": w["duration_min"],
                                           "structure": w["structure"]})
            if plan.get("carb_g_per_hr"):
                self.assertTrue(stated[0] <= plan["carb_g_per_hr"] <= stated[1], w["title"])
                checked += 1
        self.assertGreater(checked, 20)

    def test_what_the_sheet_prints_carries_no_gut_wording(self) -> None:
        for disc in ("bike", "run", "swim", "brick"):
            for minutes in (30, 75, 150, 200, 270):
                plan = nutrition.fueling_plan({"discipline": disc, "duration_min": minutes,
                                               "duration_scope": "bike_leg" if disc == "brick" else None})
                shown = " ".join(str(plan.get(k) or "") for k in ("placement",))
                if plan.get("needed") is False:
                    shown += " " + str(plan.get("note") or "")
                self.assertNotRegex(shown, r"(?i)\bGI\b|gut|distress|ceiling|fructose", (disc, minutes))


class StepsRoute(_Fixture):
    """POST /api/workout/steps: read-only, and never a Garmin write."""

    def setUp(self) -> None:
        super().setUp()
        from app import garmin_source, main
        self.main = main
        wv._THR_CACHE.update(value=None, ts=0.0)
        self.addCleanup(lambda: wv._THR_CACHE.update(value=None, ts=0.0))
        self.thr = patch.object(gw, "_threshold_pace_sec", return_value=THR)
        self.thr_mock = self.thr.start()
        self.addCleanup(self.thr.stop)
        client = patch.object(garmin_source, "get_client", side_effect=AssertionError("no Garmin call"))
        client.start()
        self.addCleanup(client.stop)

    def _call(self, body):
        import json
        response = self.main.workout_steps(body)
        return response.status_code, json.loads(response.body)

    def test_a_session_is_required(self) -> None:
        self.assertEqual(self._call({})[0], 400)
        self.assertEqual(self._call({"session": []})[0], 400)

    def test_a_bike_session_comes_back_as_steps(self) -> None:
        code, view = self._call({"session": sess("bike", "Bike — tempo", "tempo", 73, "15 min Z2",
                                                 "3×10 min @ HR 139-158 w/ 4 min easy", "10 min spin")})
        self.assertEqual((code, view["kind"]), (200, "steps"))
        self.thr_mock.assert_not_called()   # a ride needs no run pace

    def test_threshold_pace_is_read_once_for_runs(self) -> None:
        run = {"session": sess("run", "Easy run", "easy", 40, "—", "40 min easy", "—")}
        self.assertEqual(self._call(run)[0], 200)
        self.assertEqual(self._call(run)[0], 200)
        self.assertEqual(self.thr_mock.call_count, 1)

    def test_a_dated_push_sends_that_session_to_that_day(self) -> None:
        from app import suggest
        session = sess("bike", "Long ride", "Z2", 95, "15 min spin up", "70 min steady Z2", "10 min spin")
        with patch.object(gw, "push", return_value={"ok": True, "name": "Long ride"}) as push, \
                patch.object(suggest, "todays_suggestion", side_effect=AssertionError("today not consulted")):
            response = self.main.workout_push({"date": "2026-11-14", "session": session})
        self.assertEqual(response.status_code, 200)
        push.assert_called_once_with(session, "2026-11-14")


if __name__ == "__main__":
    unittest.main()
