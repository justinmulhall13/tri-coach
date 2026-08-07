"""Seed a periodized T100 plan from today → race day.

Philosophy: the plan is the backbone (readiness only modifies the daily
suggestion later). This is a peaking block, not base building. Weekly shape:

  Mon  Swim — CSS/threshold (primary swim)   + short strength
  Tue  Bike — intervals (threshold → VO2 as we sharpen)
  Wed  Run  — tempo/threshold
  Thu  Swim — endurance / open-water skills  + strength
  Fri  Recovery spin (Z1) or rest
  Sat  Long bike (race-specific) + brick run
  Sun  Long run + easy aerobic swim (3rd swim)

Swim lands 3×/week deliberately — recent load showed only 1 swim in 14 days
against a 2 km race swim. Bike 3×, run 3× (incl. brick), strength ~2×.
Taper (final ~2 weeks): volume cut hard, short race-pace touches kept.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import config, db

FTP = int(config.ATHLETE_PROFILE.get("ftp_w") or 288)


def _w(lo: float, hi: float) -> str:
    """Bike intensity as a HEART-RATE range, not watts.

    The athlete's FTP is Peloton-specific and does not transfer to hilly outdoor
    riding, so rides are judged on lap-average HR. We map the old %FTP band onto
    the equivalent cycling HR zone from their real Garmin zones, and fall back to
    a %FTP watt range only if those zones can't be read.
    """
    mid = (lo + hi) / 2
    zone = 1 if mid < 0.60 else 2 if mid < 0.76 else 3 if mid < 0.94 else 4 if mid < 1.05 else 5
    try:
        from . import zones as _z
        band = (_z.get().get("zones", {}).get("CYCLING")
                or _z.get().get("zones", {}).get("DEFAULT") or {}).get(zone)
        if band:
            return f"HR {band[0]}–{band[1]}"
    except Exception:  # noqa: BLE001
        pass
    return f"{round(FTP * lo)}–{round(FTP * hi)} W"


def _phase_for(days_to_race: int) -> str:
    if days_to_race <= 14:
        return "taper"
    if days_to_race <= 28:
        return "peak"
    return "build"


def _wo(discipline, title, intensity, dur, warmup, main, cooldown, why, is_rest=0,
        strength=None, run=None, swim=None) -> dict[str, Any]:
    structure = {"warmup": warmup, "main": main, "cooldown": cooldown}
    if strength:
        # A distinct second session (e.g. a maintenance lift) — kept separate from
        # the primary workout's warmup/main/cooldown so the UI renders it as its
        # own description rather than burying it in the main set.
        structure["strength"] = strength
    if swim:
        # A distinct second swim session on a primarily run/bike day — kept as its
        # own segment so the day shows a separate swim icon and description.
        structure["swim"] = swim
    if run:
        # Brick: the run leg off the bike, kept as its own segment so the UI
        # describes bike first / run second and the watch builds a true
        # bike→run multisport workout (not a run step buried in the bike main).
        structure["run"] = run
    return {
        "discipline": discipline, "title": title, "intensity": intensity,
        "duration_min": dur, "is_rest": is_rest, "why": why,
        "structure": structure,
    }


def _workout(weekday: int, phase: str, dtr: int) -> dict[str, Any]:
    """weekday: 0=Mon … 6=Sun. dtr = days to race (for progression)."""
    taper = phase == "taper"
    peak = phase == "peak"

    # Progression knobs
    long_ride = 150 if phase == "build" else 210 if peak else 75   # minutes
    long_run = 75 if phase == "build" else 95 if peak else 45
    if dtr <= 6:  # race week
        long_ride, long_run = 50, 30

    if weekday == 0:  # Mon — Swim CSS + strength
        if taper:
            return _wo("swim", "Swim — sharpen", "race pace",
                       40, "300 easy + 4×50 build",
                       "6×100 @ race pace, 20s rest; 200 steady", "100 easy",
                       "Keep the feel without fatigue — taper week.")
        return _wo("swim", "Swim — CSS intervals + lift", "threshold",
                   85, "400 easy + 8×50 drill/build",
                   f"{'10' if peak else '8'}×100 @ CSS (best avg you can hold), 15s rest; 2×300 steady @ ~CSS+3s",
                   "200 easy backstroke",
                   "Primary swim of the week (rebuild threshold feel + frequency) plus the first of two "
                   "weekly full-body maintenance lifts — hold strength and durability without cooking the legs.",
                   strength="Full-body maintenance lift #1 (~25 min), after the swim or later the same day: "
                   "squat, hinge (RDL/deadlift), press, row/pull, core — 2–3 sets each, controlled tempo, "
                   "leave ~2 reps in the tank.")

    if weekday == 1:  # Tue — Bike intervals
        if taper:
            return _wo("bike", "Bike — openers", "threshold",
                       45, "15 min Z2 building", f"3×3 min @ {_w(0.98,1.03)} w/ 3 min easy", "10 min spin",
                       "Short, sharp, race-pace touches; no depth.")
        if peak:
            return _wo("bike", "Bike — VO2max", "vo2",
                       80, "20 min progressive", f"5×4 min @ {_w(1.06,1.15)} w/ 6 min easy", "10 min spin",
                       "Top-end power to lift ceiling before taper.")
        return _wo("bike", "Bike — threshold", "threshold",
                   80, "15 min Z2 + 3×1 min openers", f"3×12 min @ {_w(0.95,1.02)} w/ 5 min easy", "10 min spin",
                   "Build sustainable power around FTP for the 80 km.")

    if weekday == 2:  # Wed — Run tempo
        if taper:
            return _wo("run", "Run — strides", "race pace",
                       35, "10 min easy", "6×20 s strides w/ 90 s easy; 10 min steady", "5 min easy",
                       "Leg speed, fully fresh.")
        return _wo("run", "Run — tempo", "threshold",
                   55, "12 min easy + drills",
                   f"{'30' if peak else '25'} min @ tempo (~just under threshold, ref 10K pace 4:37/km)", "8 min easy",
                   "Run economy off a heavy bike week.")

    if weekday == 3:  # Thu — Swim endurance + strength
        if taper:
            return _wo("swim", "Swim — easy aerobic", "Z2",
                       30, "200 easy", "800 continuous smooth + 4×50 drill", "100 easy",
                       "Flush the legs, hold water feel.")
        return _wo("swim", "Swim — endurance / OW skills + lift", "Z2-tempo",
                   90, "400 easy + 6×50 drill",
                   "1500 as 3×500 (sighting every 6 strokes on middle 500); 4×100 @ race pace",
                   "200 easy",
                   "2nd swim (aerobic base + open-water sighting) plus the second of two weekly full-body "
                   "maintenance lifts — this is the one that guarantees you hit two lifts a week.",
                   strength="Full-body maintenance lift #2 (~25 min), after the swim or later the same day: "
                   "squat, hinge (RDL/deadlift), press, row/pull, core — 2–3 sets each, controlled, "
                   "leave ~2 reps in the tank.")

    if weekday == 4:  # Fri — Recovery / rest
        if taper or peak:
            return _wo("rest", "Rest", "rest", 0, "", "Full rest or 20 min easy walk", "",
                       "Absorb the work; prioritize sleep.", is_rest=1)
        return _wo("recovery", "Recovery spin", "Z1",
                   40, "—", f"40 min very easy spin @ {_w(0.5,0.6)}, high cadence", "—",
                   "Active recovery before the weekend's key sessions.")

    if weekday == 5:  # Sat — Long bike + brick
        if taper and dtr <= 6:
            return _wo("brick", "Race-week brick (short)", "race pace",
                       50, "10 min easy", f"30 min @ {_w(0.8,0.88)} (race effort)", "",
                       "Rehearse race intensity and the bike→run transition, short.",
                       run="10 min run off the bike @ race pace")
        return _wo("brick", "Long bike + brick run", "race-specific",
                   long_ride + 20, "20 min Z2 build",
                   f"{long_ride - 30} min @ {_w(0.72,0.82)} (race-sim, with 4×8 min @ {_w(0.88,0.95)} surges)",
                   "10 min easy spin",
                   "The cornerstone: 80 km race simulation + legs-off-bike feel.",
                   run="20 min run @ race pace, immediately off the bike (brick)")

    # weekday == 6  Sun — Long run + easy swim
    if taper:
        return _wo("run", "Long run (reduced)", "Z2",
                   long_run, "10 min easy", f"{long_run - 15} min steady Z2", "5 min easy",
                   "Maintain endurance, trim volume.")
    return _wo("run", "Long run + easy swim", "Z2",
               long_run, "10 min easy",
               f"{long_run - 15} min progressive (last 15 min @ tempo)", "10 min easy",
               "Aerobic durability for the 18 km; 3rd swim keeps frequency up.",
               swim="Optional easy 1000 m swim later — relaxed aerobic, keeps weekly swim frequency up.")


WEEKDAY_NAME = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def seed(start: datetime.date | None = None, *, overwrite_edited: bool = False) -> dict[str, Any]:
    """Generate the plan from `start` (default today) to race day, inclusive.

    Reseeding preserves days the user edited or the coach adjusted unless
    `overwrite_edited=True`.
    """
    start = start or config.local_today()
    race = datetime.date.fromisoformat(config.RACE_DATE)
    if race < start:
        return {"error": "RACE_DATE is in the past"}

    week0_monday = start - datetime.timedelta(days=start.weekday())
    d = start
    created = 0
    while d <= race:
        dtr = (race - d).days
        phase = "post-race" if dtr < 0 else _phase_for(dtr)
        week_index = (d - week0_monday).days // 7
        if d == race:
            day = _wo("race", "🏁 RACE DAY — T100", "race",
                      0, "Race-morning routine + swim warmup",
                      "2.0 km swim → 80 km bike → 18 km run. Execute pacing & nutrition plan.", "",
                      "Race day. Trust the work.")
        else:
            day = _workout(d.weekday(), phase, dtr)
        day.update({"date": d.isoformat(), "week_index": week_index, "phase": phase, "source": "seed"})
        db.upsert_plan_day(day, only_if_absent_or_seed=not overwrite_edited)
        created += 1
        d += datetime.timedelta(days=1)

    return {"seeded_through": race.isoformat(), "days_written": created, "summary": db.plan_summary()}


if __name__ == "__main__":
    import json
    print(json.dumps(seed(), indent=2))
