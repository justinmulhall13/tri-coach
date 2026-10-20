"""Seven-month 70.3 build, with three full-body lifts a week.

The shape is set by where the athlete starts: two days out of a marathon, with
a large cycling base, a strong run, and a non-competitive swim background. A
70.3 rewards the opposite of a marathon block — the bike is the longest leg and
the run happens on legs the bike has already spent — so the block goes:

  recovery  the first two weeks after the marathon: no running in week one
  base      Nov-Jan: aerobic volume, swim frequency, the heavy lifting phase
  build     Feb-Mar: threshold on the bike and run, bricks begin
  specific  Apr-mid May: long rides with 70.3-effort blocks, longer bricks
  peak      one race-rehearsal week
  taper     the final two weeks

Every phase boundary is counted back from race day in whole weeks, never
forward from the generation date, so reconciling mid-block yields the same
remaining sessions the athlete was already shown. Every fourth week of base and
build is a recovery week.

The week is fixed so it can be learned:

  Mon  technique swim            + Full Body A (heaviest legs, after the long run)
  Tue  bike quality (Peloton)
  Wed  CSS swim                  + Full Body B (unilateral, before run quality)
  Thu  run quality
  Fri  endurance swim            + Full Body C (light legs, before the long ride)
  Sat  long ride (brick from build onward)
  Sun  long run

Three swims a week because the swim is the weakest leg. Lifts sit on swim days
because swimming barely touches the legs.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import coaching_contract, config, db, lifting_split, zones

# The named phases, in order, for the phase strip.
PHASES = ("recovery", "base", "build", "specific", "peak", "taper", "race")

# Weeks-to-race (0 = race week) at which each phase begins.
TAPER_WEEKS = 1      # n <= 1
PEAK_WEEK = 2        # n == 2
SPECIFIC_FROM = 8    # 3 <= n <= 8
BUILD_FROM = 16      # 9 <= n <= 16, base beyond that

# A recovery week every fourth week of base and build. Specific phase has one
# at n == 7 too; the peak and taper are their own unloading.
def _is_deload(n: int) -> bool:
    return n >= 7 and (n - 3) % 4 == 0


# Scheduling metadata a content refresh must never clobber.
PRESERVED_FIELDS = ("date", "week_index", "start_time",
                    "gcal_event_id", "pos_updated_at")

LIFT_WEEKDAYS = {0: "full_a", 2: "full_b", 4: "full_c"}

SORE_SWAP = ("If the legs are sore or heavy, ride this instead at the same effort "
             "and keep the running for the days that matter.")


# ------------------------------------------------------------------ helpers

def _ramp(n: int, n_far: int, n_near: int, far: float, near: float) -> float:
    """Linear progression from `far` (n_far weeks out) to `near` (n_near weeks out)."""
    if n_far == n_near:
        return near
    t = (n_far - n) / (n_far - n_near)
    t = max(0.0, min(1.0, t))
    return far + (near - far) * t


def _r5(x: float) -> int:
    return int(5 * round(x / 5))


def _hr(intensity: str) -> str:
    try:
        band = zones.hr_range(intensity, "bike")
        if band:
            return f"HR {band[0]}-{band[1]}"
    except Exception:  # noqa: BLE001
        pass
    return f"{intensity} by HR (Garmin cycling zones required)"


def _easy() -> str:
    try:
        r = zones.pace_range("easy")
        if r:
            return f"no faster than {zones.fmt_pace(r[0])}/km"
    except Exception:  # noqa: BLE001
        pass
    return "conversational, no faster than easy"


def _wo(discipline: str, title: str, intensity: str, dur: int, warmup: str,
        main: str, cooldown: str, why: str, *, is_rest: int = 0,
        run: str | None = None) -> dict[str, Any]:
    structure: dict[str, Any] = {"warmup": warmup, "main": main, "cooldown": cooldown}
    if run:
        # Brick run leg kept separate so the watch builds a bike→run multisport.
        structure["run"] = run
    return {"discipline": discipline, "title": title, "intensity": intensity,
            "duration_min": dur, "is_rest": is_rest, "why": why,
            "structure": structure}


def _event() -> dict[str, Any]:
    return coaching_contract.event_context() or {}


def _race_name() -> str:
    event = _event()
    return str(event.get("event") or event.get("event_name") or "70.3").strip()


def _distances() -> tuple[float, float, float]:
    d = _event().get("disciplines_and_distances") or {}
    try:
        return (float(d.get("swim_km") or 1.9), float(d.get("bike_km") or 90),
                float(d.get("run_km") or 21.1))
    except (TypeError, ValueError):
        return 1.9, 90.0, 21.1


def recovery_until() -> datetime.date | None:
    """Last day of the post-marathon recovery window, from the active profile."""
    try:
        return datetime.date.fromisoformat(str(_event().get("recovery_until") or ""))
    except ValueError:
        return None


def weeks_out(day: datetime.date, race: datetime.date) -> int:
    """Whole weeks between this day's week and race week (0 = race week)."""
    sunday = day + datetime.timedelta(days=(6 - day.weekday()) % 7)
    race_sunday = race + datetime.timedelta(days=(6 - race.weekday()) % 7)
    return max(0, (race_sunday - sunday).days // 7)


def phase_for(day: datetime.date, race: datetime.date) -> str:
    if day == race:
        return "race"
    if day > race:
        return "post-race"
    rec = recovery_until()
    if rec and day <= rec:
        return "recovery"
    n = weeks_out(day, race)
    if n <= TAPER_WEEKS:
        return "taper"
    if n == PEAK_WEEK:
        return "peak"
    if n <= SPECIFIC_FROM:
        return "specific"
    if n <= BUILD_FROM:
        return "build"
    return "base"


# ------------------------------------------------------------------ recovery

def _recovery_session(day: datetime.date, rec_end: datetime.date) -> dict[str, Any]:
    """Two weeks after the marathon: blood flow, no damage, running returns in week two."""
    second_week = (rec_end - day).days < 7
    wd = day.weekday()
    if wd in (0, 4):
        return _wo("swim", "Swim — easy return", "Z1-Z2", 35, "200 easy",
                   "8×100 easy, 20s rest, focus on long smooth strokes", "100 easy",
                   "Marathon recovery. Swimming moves blood through sore legs without "
                   "loading them, and restarts the leg of the 70.3 that needs the most work.")
    if wd == 2:
        return _wo("swim", "Swim — technique", "Z2", 40, "300 easy",
                   "6×50 drill (catch-up / fingertip drag), 15s rest; 4×100 smooth, 20s rest",
                   "100 easy", "Technique while the legs recover.")
    if wd == 1:
        return _wo("bike", "Easy spin", "Z1", 40, "—",
                   f"40 min very easy spin @ {_hr('recovery')}, high cadence", "—",
                   "Flush the legs. No intervals for two weeks after a marathon.")
    if wd == 3:
        if second_week:
            return _wo("run", "Easy run — 5 km", "easy", 32, "—",
                       f"5 km easy, {_easy()}. Stop if anything hurts.", "—",
                       "First run back. Easy and short; the marathon damage outlasts "
                       "the soreness.")
        return _wo("bike", "Easy spin", "Z1", 45, "—",
                   f"45 min easy spin @ {_hr('recovery')}", "—",
                   "No running in the first week after a marathon.")
    if wd == 5:
        mins = 75 if second_week else 60
        return _wo("bike", f"Easy ride — {mins} min", "Z2", mins, "10 min spin up",
                   f"{mins - 20} min steady Z2 @ {_hr('easy')}", "10 min spin",
                   "Aerobic time back on the bike, the longest leg of a 70.3.")
    if second_week:
        return _wo("run", "Easy run — 8 km", "easy", 50, "—",
                   f"8 km easy, {_easy()}. {SORE_SWAP}", "—",
                   "Running returns gently. Base phase starts next week.")
    return _wo("rest", "Rest", "rest", 0, "", "Full rest or an easy walk", "",
               "Rest is the session. You ran a marathon a week ago.", is_rest=1)


# ------------------------------------------------------------------ main block

def _swim(wd: int, n: int, phase: str, deload: bool) -> dict[str, Any]:
    swim_km = _distances()[0]
    race_m = int(round(swim_km * 1000, -2))
    if wd == 0:   # technique
        reps = 6 if deload else int(_ramp(n, 30, 3, 6, 10))
        return _wo("swim", "Swim — technique", "Z2", 45,
                   "300 easy + 6×50 drill (catch-up, fingertip drag)",
                   f"{reps}×100 smooth @ easy aerobic, 20s rest, count strokes per length",
                   "100 easy backstroke",
                   "Technique is where a non-competitive swimmer finds the most free "
                   "time. Smooth, not hard.")
    if wd == 2:   # CSS
        if phase == "base":
            reps = 6 if deload else int(_ramp(n, 30, 17, 6, 12))
            main = f"{reps}×100 @ CSS (best average you can hold), 15s rest"
        elif phase in ("build", "specific"):
            reps = 3 if deload else int(_ramp(n, 16, 3, 4, 6))
            main = f"{reps}×200 @ CSS, 20s rest; 4×50 fast, 20s rest"
        else:
            main = "6×100 @ race pace, 20s rest"
        return _wo("swim", "Swim — CSS intervals", "threshold", 55,
                   "400 easy + 6×50 build", main, "200 easy",
                   "The quality swim. CSS work moves threshold swim pace, which is the "
                   "pace a 70.3 swim is held just under.")
    # Friday endurance / open water
    if phase == "base":
        cont = 600 if deload else int(round(_ramp(n, 30, 17, 800, 1500), -2))
        main = f"{cont} continuous steady, breathing every 3; 4×100 pull buoy, 15s rest"
    else:
        cont = int(round(race_m * (0.6 if deload else _ramp(n, 16, 3, 0.8, 1.1)), -2))
        main = (f"{cont} continuous @ race effort, sighting every 6 strokes; "
                "4×50 fast, 20s rest")
        if phase == "specific":
            main += ". Open water if the lakes are swimmable — wetsuit on."
    return _wo("swim", "Swim — endurance", "Z2-tempo", 50,
               "300 easy + 4×50 drill", main, "200 easy",
               f"Continuous swimming toward the {race_m} m race swim. Sighting is "
               "practised in the pool so it costs nothing in the lake.")


def _bike_quality(n: int, phase: str, deload: bool) -> dict[str, Any]:
    if deload:
        return _wo("bike", "Bike — easy with spin-ups", "Z2", 50, "10 min spin up",
                   f"30 min Z2 @ {_hr('easy')} with 6×30 s high-cadence spin-ups",
                   "10 min spin", "Recovery week: keep the legs turning, no depth.")
    if phase == "base":
        reps = int(_ramp(n, 30, 17, 3, 4))
        mins = int(_ramp(n, 30, 17, 10, 12))
        dur = 30 + reps * (mins + 4)
        why = ("Tempo builds the aerobic engine a 70.3 bike leg is ridden on. "
               "Peloton is fine; judge by heart rate.")
        if n > 22:
            return _wo("bike", "Bike — tempo", "tempo", dur,
                       "15 min Z2 + 3×1 min openers",
                       f"{reps}×{mins} min @ {_hr('tempo')} w/ 4 min easy",
                       "10 min spin", why)
        # From mid-base a short easy run off the bike makes three runs a week
        # without adding a separate session.
        return _wo("brick", "Bike — tempo + run off", "tempo", dur + 15,
                   "15 min Z2 + 3×1 min openers",
                   f"{reps}×{mins} min @ {_hr('tempo')} w/ 4 min easy",
                   "10 min spin",
                   why + " The short run off it teaches the legs the change early.",
                   run=f"15 min easy run off the bike, {_easy()}")
    if phase == "build":
        mins = int(_ramp(n, 16, 9, 10, 15))
        return _wo("bike", "Bike — threshold", "threshold", 30 + 3 * (mins + 5),
                   "15 min Z2 + 3×1 min openers",
                   f"3×{mins} min @ {_hr('threshold')} w/ 5 min easy",
                   "10 min spin", "Threshold raises the ceiling the race effort sits under.")
    if phase == "specific":
        mins = int(_ramp(n, 8, 3, 20, 30))
        return _wo("brick", "Bike — race effort + run off", "race-specific",
                   25 + 2 * mins + 5 + 15,
                   "15 min Z2 build",
                   f"2×{mins} min @ 70.3 effort ({_hr('tempo')}) w/ 5 min easy",
                   "5 min spin",
                   "Midweek brick: race effort, then the first kilometres off the bike, "
                   "which is where a 70.3 run is won or lost.",
                   run="15 min run off the bike @ steady, settle into form")
    if phase == "peak":
        return _wo("bike", "Bike — race-effort openers", "threshold", 60,
                   "15 min Z2", f"3×8 min @ {_hr('threshold')} w/ 4 min easy",
                   "10 min spin", "Sharpen without adding depth.")
    return _wo("bike", "Bike — openers", "threshold", 45, "15 min Z2 building",
               f"3×3 min @ {_hr('threshold')} w/ 3 min easy", "10 min spin",
               "Short, sharp touches to keep the legs awake for race day.")


def _run_quality(n: int, phase: str, deload: bool) -> dict[str, Any]:
    if deload:
        return _wo("run", "Easy run + strides", "easy", 40, "—",
                   f"35 min easy, {_easy()}; 6×20 s strides", "—",
                   "Recovery week: keep the rhythm, drop the load.")
    if phase == "base":
        if n > 22:
            return _wo("run", "Easy run + strides", "easy", 45, "—",
                       f"40 min easy, {_easy()}; 6×20 s strides w/ full recovery", "—",
                       "Rebuild run frequency after the marathon. Strides keep the "
                       "legs fast without fatigue.")
        reps = int(_ramp(n, 22, 17, 6, 8))
        return _wo("run", "Run — hill repeats", "threshold", 50,
                   "15 min easy + drills",
                   f"{reps}×60 s uphill @ hard, jog down",
                   "10 min easy",
                   "Hills are strength work for runners: power with almost no "
                   "landing impact.")
    if phase == "build":
        blocks = {16: "5×4 min", 15: "4×6 min", 14: "3×8 min", 13: "4×8 min",
                  12: "3×10 min", 11: "4×6 min", 10: "3×12 min", 9: "2×15 min"}
        return _wo("run", "Run — threshold", "threshold", 55,
                   "15 min easy + 4 strides",
                   f"{blocks.get(n, '3×10 min')} @ threshold w/ 2 min jog",
                   "10 min easy",
                   "Threshold is what lets 70.3 race effort feel steady instead of hard.")
    if phase == "specific":
        mins = int(_ramp(n, 8, 3, 20, 35))
        return _wo("run", "Run — race effort", "tempo", 30 + mins,
                   "15 min easy + 4 strides",
                   f"{mins} min @ 70.3 run effort (steady tempo, just under threshold)",
                   "10 min easy",
                   "Rehearses the effort you will hold for 21 km off the bike.")
    if phase == "peak":
        return _wo("run", "Run — race effort", "tempo", 50, "15 min easy",
                   "3×8 min @ 70.3 run effort w/ 2 min jog", "10 min easy",
                   "Last real run session. Crisp, not deep.")
    return _wo("run", "Run — strides", "race pace", 35,
               f"10 min easy; {_easy()}",
               "6×20 s strides w/ 90 s easy; 10 min steady",
               f"5 min easy; {_easy()}", "Leg speed, fully fresh.")


def _long_ride(n: int, phase: str, deload: bool) -> dict[str, Any]:
    _, bike_km, _ = _distances()
    if phase == "base":
        mins = _r5(_ramp(n, 30, 17, 90, 165) * (0.65 if deload else 1))
        return _wo("bike", f"Long ride — {mins} min", "Z2", mins, "15 min spin up",
                   f"{mins - 25} min steady Z2 @ {_hr('easy')}, fuel 60 g carb/h",
                   "10 min spin",
                   "Aerobic durability is the base of a 70.3. Indoor is fine through "
                   "winter; practise fuelling every time.")
    if phase in ("build", "specific"):
        if phase == "build":
            mins = _ramp(n, 16, 9, 165, 210)
            block = int(_ramp(n, 16, 9, 20, 30))
            body = f"2×{block} min @ 70.3 effort ({_hr('tempo')}) inside the ride"
            run_min = int(_ramp(n, 16, 9, 10, 20))
        else:
            mins = _ramp(n, 8, 3, 210, 240)
            block = int(_ramp(n, 8, 3, 40, 75))
            body = (f"{block} min continuous @ 70.3 effort ({_hr('tempo')}) in the "
                    "second half, on aero bars")
            run_min = int(_ramp(n, 8, 3, 20, 30))
        mins = _r5(mins * (0.65 if deload else 1))
        if deload:
            return _wo("bike", f"Long ride — {mins} min", "Z2", mins, "15 min spin up",
                       f"{mins - 25} min steady Z2 @ {_hr('easy')}", "10 min spin",
                       "Recovery week: shorter, no race-effort blocks.")
        return _wo("brick", f"Long ride + brick — {mins} min", "race-specific",
                   mins + run_min, "20 min Z2 build",
                   f"{mins - 30} min Z2 @ {_hr('easy')} with {body}. "
                   "Fuel 80-90 g carb/h and 600-800 mg sodium/h.",
                   "10 min easy spin",
                   f"The cornerstone of the week: race-day fuelling and the bike legs "
                   f"for {bike_km:g} km, then running on them.",
                   run=f"{run_min} min run off the bike @ 70.3 run effort")
    if phase == "peak":
        return _wo("brick", "Race rehearsal", "race-specific", 180,
                   "20 min Z2 build",
                   f"130 min with 90 min @ 70.3 effort ({_hr('tempo')}); race kit, "
                   "race breakfast, race fuelling", "10 min spin",
                   "Dress rehearsal: everything you will do on race day, at slightly "
                   "under race duration.",
                   run="30 min run off the bike @ 70.3 run effort")
    if n == 1:
        return _wo("brick", "Taper brick", "race pace", 110, "15 min Z2",
                   f"75 min Z2 with 3×10 min @ 70.3 effort ({_hr('tempo')})",
                   "10 min spin", "Keep the race feel while volume drops.",
                   run="15 min run off the bike @ 70.3 run effort")
    return _wo("brick", "Race-week shakeout", "race pace", 45, "10 min easy",
               f"20 min easy with 3×1 min @ 70.3 effort ({_hr('tempo')})", "",
               "Check the bike, wake the legs, then rest.",
               run="10 min run off the bike, relaxed")


def _long_run(n: int, phase: str, deload: bool) -> dict[str, Any]:
    _, _, run_km = _distances()
    if phase == "base":
        km = _ramp(n, 30, 17, 10, 16)
    elif phase == "build":
        km = _ramp(n, 16, 9, 16, 18)
    elif phase == "specific":
        km = _ramp(n, 8, 3, 18, min(20.0, run_km))
    elif phase == "peak":
        km = 14
    else:
        km = 12 if n == 1 else 0
    km = round(km * (0.75 if deload else 1))
    finish = ""
    if phase in ("build", "specific") and not deload and km >= 16:
        finish = " Last 4 km @ 70.3 run effort."
    return _wo("run", f"Long run — {km:g} km", "long", int(km * 6.1),
               "10 min easy build",
               f"{km:g} km steady, {_easy()}.{finish}",
               "10 min walk + legs up",
               f"Run durability for the {run_km:g} km off the bike. Kept a notch "
               "below marathon long runs: a 70.3 run is shorter, and the bike "
               "already pre-fatigues the legs.")


def race_day() -> dict[str, Any]:
    swim, bike, run = _distances()
    return _wo("race", f"\U0001F3C1 RACE DAY — {_race_name()}", "race", 330,
               "Race-morning routine + 10 min swim warmup",
               f"{swim:g} km swim, {bike:g} km bike, {run:g} km run. Bike at 70.3 effort "
               f"({_hr('tempo')}) and no harder; the run starts at the first aid "
               "station, not the dismount line.",
               "", "Everything in this block was for today.")


def _race_week(wd: int) -> dict[str, Any]:
    if wd == 0:
        return _wo("swim", "Swim — race-week sharpen", "race pace", 35,
                   "300 easy + 4×50 build", "6×100 @ race pace, 20s rest",
                   "100 easy", "Keep the feel without fatigue.")
    if wd == 1:
        return _bike_quality(0, "taper", False)
    if wd == 2:
        return _wo("swim", "Swim — easy", "Z2", 30, "200 easy",
                   "800 continuous smooth + 4×50 drill", "100 easy",
                   "Hold water feel.")
    if wd == 3:
        return _run_quality(0, "taper", False)
    if wd == 4:
        return _wo("rest", "Rest + travel", "rest", 0, "",
                   "Rest. Pack, check the bike, sleep.", "",
                   "Two days out: the work is done.", is_rest=1)
    return _long_ride(0, "taper", False)


def workout_for_date(day: datetime.date, race: datetime.date) -> tuple[dict[str, Any], str]:
    """One day's session and its phase. Shared by seed and reconcile."""
    phase = phase_for(day, race)
    if phase == "race":
        return race_day(), phase
    if phase == "recovery":
        return _recovery_session(day, recovery_until()), phase
    n = weeks_out(day, race)
    if n == 0:
        return _race_week(day.weekday()), phase
    deload = _is_deload(n) and phase in ("base", "build", "specific")
    wd = day.weekday()
    if deload and wd == 4:
        # One full day off in every recovery week; the lift stays, light.
        session = _wo("rest", "Rest", "rest", 0, "", "Full rest or an easy walk", "",
                      "Rest is when the last three weeks turn into fitness.", is_rest=1)
    elif wd in (0, 2, 4):
        session = _swim(wd, n, phase, deload)
    elif wd == 1:
        session = _bike_quality(n, phase, deload)
    elif wd == 3:
        session = _run_quality(n, phase, deload)
    elif wd == 5:
        session = _long_ride(n, phase, deload)
    else:
        session = _long_run(n, phase, deload)
    if deload:
        session["why"] = "Recovery week. " + session["why"]
    return session, phase


# ------------------------------------------------------------------ lifting

# Phase -> (effort level, set count, cue). Heavy when strength can actually be
# built (the off-season), maintenance once the race-specific work starts.
_LIFT_DOSE = {
    "recovery": ("light", 2, "Re-entry: two easy sets, 4 reps in reserve, no jumps."),
    "base": ("heavy", 3, "Strength phase: work up to heavy sets, 2 reps in reserve."),
    "build": ("moderate", 3, "Hold strength: same loads, 2-3 reps in reserve."),
    "specific": ("moderate", 2, "Maintenance: two working sets, 3 reps in reserve, "
                                "never to failure."),
    "peak": ("light", 2, "Maintenance only. Leave the gym fresher than you arrived."),
    "taper": ("light", 2, "Taper: light, upper-body emphasis, nothing new."),
}


def _split_days() -> dict[str, dict[str, Any]]:
    stored = None
    try:
        stored = db.get_lifting_split()
    except Exception:  # noqa: BLE001
        stored = None
    days = (stored or {}).get("days") or lifting_split.DEFAULT_SPLIT
    return {str(d.get("slot")): d for d in days if isinstance(d, dict)}


def lift_for_date(day: datetime.date, race: datetime.date) -> dict[str, Any] | None:
    """The full-body session attached to this date, or None."""
    slot = LIFT_WEEKDAYS.get(day.weekday())
    if not slot or day >= race:
        return None
    phase = phase_for(day, race)
    n = weeks_out(day, race)
    rec_end = recovery_until()
    # Nothing in the first week after the marathon: the legs are still damaged.
    if phase == "recovery" and rec_end and (rec_end - day).days >= 7:
        return None
    # Race week keeps only Monday; the last Wednesday/Friday lifts cost more than
    # they could return.
    if n == 0 and slot != "full_a":
        return None
    split = _split_days()
    template = split.get(slot) or (list(split.values()) or [None])[day.weekday() // 2 % 3]
    if not template:
        return None
    level, sets, cue = _LIFT_DOSE.get(phase, _LIFT_DOSE["build"])
    if _is_deload(n) and phase in ("base", "build", "specific"):
        level, sets, cue = "light", 2, "Recovery week: two sets, same loads, stop well short of failure."
    exercises = list(template.get("exercises") or [])
    if phase == "recovery":
        exercises = [e for e in exercises if "jump" not in str(e.get("title", "")).lower()]
    lines = [f"{e.get('title')} — {min(int(e.get('sets') or sets), sets)}×{e.get('reps')}"
             for e in exercises]
    return {
        "date": day.isoformat(),
        "slot": "full",
        "title": str(template.get("name") or "Full Body"),
        "routine": None,
        "duration_min": 35 + 5 * sets,
        "intensity": level,
        "structure": {"warmup": "5 min easy cardio + 2 ramp-up sets on the first lift",
                      "main": "; ".join(lines),
                      "cooldown": "5 min mobility"},
        "why": f"{template.get('focus') or ''}. {template.get('why') or ''}".strip(". ") + ".",
        "effort_level": level,
        "effort_cue": cue,
        "source": "seed",
    }


def _write_lift(day: datetime.date, race: datetime.date, *, overwrite: bool) -> bool:
    lift = lift_for_date(day, race)
    existing = db.get_plan_strength(day.isoformat())
    if existing and existing.get("source") != "seed" and not overwrite:
        return False
    if lift is None:
        if existing and existing.get("source") == "seed":
            db.delete_plan_strength(day.isoformat())
        return False
    if existing:
        for field in ("start_time", "gcal_event_id", "pos_updated_at", "hevy_routine_id"):
            if existing.get(field):
                lift[field] = existing[field]
    db.upsert_plan_strength(lift)
    return True


# ------------------------------------------------------------------ seed/reconcile

def seed(start: datetime.date | None = None, *,
         overwrite_edited: bool = False) -> dict[str, Any]:
    """Write the block, and its lifts, from `start` through race day."""
    start = start or config.local_today()
    race = datetime.date.fromisoformat(config.race_date())
    if race < start:
        return {"error": "race date is in the past"}

    week0_monday = start - datetime.timedelta(days=start.weekday())
    created = lifts = 0
    d = start
    while d <= race:
        day, phase = workout_for_date(d, race)
        day.update({"date": d.isoformat(),
                    "week_index": (d - week0_monday).days // 7,
                    "phase": phase, "source": "seed"})
        db.upsert_plan_day(day, only_if_absent_or_seed=not overwrite_edited)
        lifts += _write_lift(d, race, overwrite=overwrite_edited)
        created += 1
        d += datetime.timedelta(days=1)

    return {"seeded_through": race.isoformat(), "days_written": created,
            "lifts_written": lifts, "summary": db.plan_summary()}


def reconcile(start: datetime.date | None = None) -> dict[str, Any]:
    """Refresh remaining GENERATED rows in place; edited and coach rows survive."""
    start = start or config.local_today()
    try:
        race = datetime.date.fromisoformat(config.race_date())
    except ValueError:
        return {"reconciled": 0, "skipped_modified": 0, "skipped_profile": 0,
                "error": "event profile date is invalid; expected YYYY-MM-DD"}
    if start > race:
        return {"reconciled": 0, "skipped_modified": 0, "skipped_profile": 0,
                "start": start.isoformat(), "end": race.isoformat()}

    active_profile = coaching_contract.event_profile_id()
    reconciled = skipped_modified = skipped_profile = skipped_invalid = 0
    for existing in db.get_plan(start.isoformat(), race.isoformat()):
        row_profile = existing.get("event_profile_id")
        if row_profile not in (None, active_profile):
            skipped_profile += 1
            continue
        if existing.get("source") != "seed":
            skipped_modified += 1
            continue
        try:
            day_date = datetime.date.fromisoformat(str(existing.get("date") or ""))
        except ValueError:
            skipped_invalid += 1
            continue
        refreshed, phase = workout_for_date(day_date, race)
        refreshed.update({field: existing.get(field) for field in PRESERVED_FIELDS})
        refreshed["phase"] = phase
        refreshed["source"] = "seed"
        db.upsert_plan_day(refreshed)
        _write_lift(day_date, race, overwrite=False)
        reconciled += 1

    return {"reconciled": reconciled, "skipped_modified": skipped_modified,
            "skipped_profile": skipped_profile, "skipped_invalid": skipped_invalid,
            "start": start.isoformat(), "end": race.isoformat()}
