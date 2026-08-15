"""Seed a periodized T100 plan from today → race day.

Philosophy: the plan is the backbone (readiness only modifies the daily
suggestion later). This is a peaking block, not base building. Weekly shape:

  Mon  Swim — CSS/threshold (primary swim)
  Tue  Bike — intervals (threshold → VO2 as we sharpen)
  Wed  Run  — tempo/threshold
  Thu  Swim — endurance / open-water skills
  Fri  Recovery spin (Z1) or rest
  Sat  Long bike (race-specific) + brick run
  Sun  Long run + easy aerobic swim (3rd swim)

Swim lands 3×/week deliberately — recent load showed only 1 swim in 14 days
against a 2 km race swim. Bike 3× and run 3× (including the brick).
Strength is prescribed only when the athlete explicitly asks for it.
Taper (final ~2 weeks): volume cut hard, short race-pace touches kept.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import coaching_contract, config, db

def _hr(intensity: str) -> str:
    """Bike target from the event profile or measured Garmin cycling zones."""
    if "race" in (intensity or "").lower():
        target = ((coaching_contract.EVENT_PROFILE.get("pacing_targets") or {}).get("bike_hr_bpm") or [])
        if len(target) == 2:
            return f"HR {target[0]}-{target[1]}"
        return "HR unknown (event race target required)"
    try:
        from . import zones as _z
        band = _z.hr_range(intensity, "bike")
        if band:
            return f"HR {band[0]}-{band[1]}"
    except Exception:  # noqa: BLE001
        pass
    return "HR unknown (Garmin cycling zones required)"


def _easy_run_ceiling() -> str:
    """Pace-first ceiling matching the target encoded by garmin_workout."""
    try:
        from . import zones as _z
        pace = _z.pace_range("easy")
        if pace:
            return f"pace ceiling {_z.fmt_pace(pace[1])}/km; do not go faster"
        band = _z.hr_range("easy", "run")
        if band:
            return f"HR ceiling {band[1]} bpm; do not exceed"
    except Exception:  # noqa: BLE001
        pass
    return "HR ceiling unknown; Garmin running zones required before execution"


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
        "tsb_target": coaching_contract.DEFAULT_RACE_DAY_TSB_TARGET,
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

    if weekday == 0:  # Mon: swim CSS
        if taper:
            return _wo("swim", "Swim — sharpen", "race pace",
                       40, "300 easy + 4×50 build",
                       "6×100 @ race pace, 20s rest; 200 steady", "100 easy",
                       "Keep the feel without fatigue — taper week.")
        return _wo("swim", "Swim: CSS intervals", "threshold",
                   60, "400 easy + 8×50 drill/build",
                   f"{'10' if peak else '8'}×100 @ CSS (best avg you can hold), 15s rest; 2×300 steady @ ~CSS+3s",
                   "200 easy backstroke",
                   "Primary swim of the week; rebuild threshold feel and frequency.")

    if weekday == 1:  # Tue — Bike intervals
        if taper:
            return _wo("bike", "Bike — openers", "threshold",
                       45, "15 min Z2 building", f"3×3 min @ {_hr('threshold')} w/ 3 min easy", "10 min spin",
                       "Short, sharp, race-pace touches; no depth.")
        if peak:
            return _wo("bike", "Bike — VO2max", "vo2",
                       80, "20 min progressive", f"5×4 min @ {_hr('vo2')} w/ 6 min easy", "10 min spin",
                       "Top-end cardiovascular work to lift the ceiling before taper.")
        return _wo("bike", "Bike — threshold", "threshold",
                   80, "15 min Z2 + 3×1 min openers", f"3×12 min @ {_hr('threshold')} w/ 5 min easy", "10 min spin",
                   "Build sustainable bike strength by heart rate for the 80 km.")

    if weekday == 2:  # Wed — Run tempo
        if taper:
            return _wo("run", "Run — strides", "race pace",
                       35, f"10 min easy; {_easy_run_ceiling()}",
                       f"6×20 s strides w/ 90 s easy; recoveries {_easy_run_ceiling()}; 10 min steady",
                       f"5 min easy; {_easy_run_ceiling()}",
                       "Leg speed, fully fresh.")
        return _wo("run", "Run — tempo", "threshold",
                   55, f"12 min easy + drills; {_easy_run_ceiling()}",
                   f"{'30' if peak else '25'} min @ tempo (~just under threshold, ref 10K pace 4:37/km)",
                   f"8 min easy; {_easy_run_ceiling()}",
                   "Run economy off a heavy bike week.")

    if weekday == 3:  # Thu: swim endurance
        if taper:
            return _wo("swim", "Swim — easy aerobic", "Z2",
                       30, "200 easy", "800 continuous smooth + 4×50 drill", "100 easy",
                       "Flush the legs, hold water feel.")
        return _wo("swim", "Swim: endurance / OW skills", "Z2-tempo",
                   65, "400 easy + 6×50 drill",
                   "1500 as 3×500 (sighting every 6 strokes on middle 500); 4×100 @ race pace",
                   "200 easy",
                   "Second swim; build aerobic base and open-water sighting skill.")

    if weekday == 4:  # Fri — Recovery / rest
        if taper or peak:
            return _wo("rest", "Rest", "rest", 0, "", "Full rest or 20 min easy walk", "",
                       "Absorb the work; prioritize sleep.", is_rest=1)
        return _wo("recovery", "Recovery spin", "Z1",
                   40, "—", f"40 min very easy spin @ {_hr('recovery')}, high cadence", "—",
                   "Active recovery before the weekend's key sessions.")

    if weekday == 5:  # Sat — Long bike + brick
        if taper and dtr <= 6:
            return _wo("brick", "Race-week brick (short)", "race pace",
                       50, "10 min easy", f"30 min @ {_hr('race')} (race effort)", "",
                       "Rehearse race intensity and the bike→run transition, short.",
                       run="10 min run off the bike @ race pace")
        return _wo("brick", "Long bike + brick run", "race-specific",
                   long_ride + 20, "20 min Z2 build",
                   f"{long_ride - 30} min @ {_hr('race')} (race simulation)",
                   "10 min easy spin",
                   "The cornerstone: 80 km race simulation + legs-off-bike feel.",
                   run="20 min run @ race pace, immediately off the bike (brick)")

    # weekday == 6  Sun — Long run + easy swim
    if taper:
        return _wo("run", "Long run (reduced)", "Z2",
                   long_run, f"10 min easy; {_easy_run_ceiling()}",
                   f"{long_run - 15} min steady Z2; {_easy_run_ceiling()}",
                   f"5 min easy; {_easy_run_ceiling()}",
                   "Maintain endurance, trim volume.")
    return _wo("run", "Long run + easy swim", "Z2",
               long_run, f"10 min easy; {_easy_run_ceiling()}",
               f"{long_run - 30} min easy; {_easy_run_ceiling()}; last 15 min @ tempo",
               f"10 min easy; {_easy_run_ceiling()}",
               "Aerobic durability for the 18 km; 3rd swim keeps frequency up.",
               swim="Optional easy 1000 m swim later — relaxed aerobic, keeps weekly swim frequency up.")


def _race_day_workout() -> dict[str, Any]:
    event = coaching_contract.EVENT_PROFILE
    if not config.supports_t100_features():
        raise ValueError("The bundled race-day workout is only valid for the installed T100 Vancouver profile")
    distances = event.get("disciplines_and_distances") or {}
    goal = event.get("goal") or {}
    return _wo(
        "race", f"🏁 RACE DAY: {event.get('event')}", "race",
        int(goal["modelled_duration_min"]),
        "Race-morning routine + swim warmup",
        (f"{distances['swim_km']:g} km swim, {distances['bike_km']:g} km bike, "
         f"{distances['run_km']:g} km run. Execute event-profile pacing and fueling."),
        "", "Race-day execution.",
    )


WEEKDAY_NAME = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _seed_workout_for_date(day_date: datetime.date, race_date: datetime.date) -> tuple[dict[str, Any], str]:
    """Build the current seed content for one date without persistence metadata."""
    days_to_race = (race_date - day_date).days
    phase = "post-race" if days_to_race < 0 else _phase_for(days_to_race)
    workout = _race_day_workout() if day_date == race_date else _workout(
        day_date.weekday(), phase, days_to_race,
    )
    return workout, phase


def seed(start: datetime.date | None = None, *, overwrite_edited: bool = False) -> dict[str, Any]:
    """Generate the plan from `start` (default today) to race day, inclusive.

    Reseeding preserves days the user edited or the coach adjusted unless
    `overwrite_edited=True`.
    """
    if not config.supports_t100_features():
        return {"error": (f"No plan builder is installed for event profile "
                          f"{coaching_contract.EVENT_PROFILE.get('id') or 'unknown'}; "
                          "the T100 Vancouver builder will not be reused across modes and "
                          "will not be reused across profiles")}
    start = start or config.local_today()
    race = datetime.date.fromisoformat(config.RACE_DATE)
    if race < start:
        return {"error": "RACE_DATE is in the past"}

    week0_monday = start - datetime.timedelta(days=start.weekday())
    d = start
    created = 0
    while d <= race:
        day, phase = _seed_workout_for_date(d, race)
        week_index = (d - week0_monday).days // 7
        day.update({"date": d.isoformat(), "week_index": week_index, "phase": phase, "source": "seed"})
        db.upsert_plan_day(day, only_if_absent_or_seed=not overwrite_edited)
        created += 1
        d += datetime.timedelta(days=1)

    return {"seeded_through": race.isoformat(), "days_written": created, "summary": db.plan_summary()}


_PRESERVED_SEED_FIELDS = (
    "date", "week_index", "phase", "start_time", "gcal_event_id", "pos_updated_at",
)


def reconcile_seeded_plan(start: datetime.date | None = None) -> dict[str, Any]:
    """Refresh remaining generated rows from the current profile's seed builder.

    This is a schema/content migration for generated plan rows. It replaces old
    seed workout content, including legacy watt prescriptions, while retaining
    scheduling metadata. User-edited and Coach-adjusted rows are never written.
    ``db.get_plan`` already scopes the query to the active event profile; the
    explicit profile check below keeps that boundary intact under mocked or
    future data sources too.
    """
    if not config.supports_t100_features():
        return {
            "reconciled": 0,
            "skipped_modified": 0,
            "skipped_profile": 0,
            "error": "No seeded-plan reconciler is installed for the active event profile",
        }

    start = start or config.local_today()
    try:
        race = datetime.date.fromisoformat(config.RACE_DATE)
    except ValueError:
        return {
            "reconciled": 0,
            "skipped_modified": 0,
            "skipped_profile": 0,
            "error": "EVENT_PROFILE date is invalid; expected YYYY-MM-DD",
        }
    if start > race:
        return {
            "reconciled": 0,
            "skipped_modified": 0,
            "skipped_profile": 0,
            "start": start.isoformat(),
            "end": race.isoformat(),
        }

    active_profile = coaching_contract.event_profile_id()
    rows = db.get_plan(start.isoformat(), race.isoformat())
    reconciled = 0
    skipped_modified = 0
    skipped_profile = 0
    skipped_invalid = 0
    for existing in rows:
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

        refreshed, _derived_phase = _seed_workout_for_date(day_date, race)
        refreshed.update({field: existing.get(field) for field in _PRESERVED_SEED_FIELDS})
        refreshed["source"] = "seed"
        db.upsert_plan_day(refreshed)
        reconciled += 1

    return {
        "reconciled": reconciled,
        "skipped_modified": skipped_modified,
        "skipped_profile": skipped_profile,
        "skipped_invalid": skipped_invalid,
        "start": start.isoformat(),
        "end": race.isoformat(),
    }


def reconcile_event_day() -> bool:
    """Refresh a generated race row without losing its Calendar placement."""
    if not config.supports_t100_features():
        return False
    existing = db.get_plan_day(config.RACE_DATE)
    if not existing or existing.get("source") != "seed":
        return False
    day = _race_day_workout()
    day.update({
        "date": existing["date"],
        "week_index": existing["week_index"],
        "phase": existing["phase"],
        "source": "seed",
        "start_time": existing.get("start_time"),
        "gcal_event_id": existing.get("gcal_event_id"),
        "pos_updated_at": existing.get("pos_updated_at"),
    })
    db.upsert_plan_day(day)
    return True


if __name__ == "__main__":
    import json
    print(json.dumps(seed(), indent=2))
