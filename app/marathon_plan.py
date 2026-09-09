"""Compressed marathon build.

The constraint that shapes this file: an athlete arriving with a large cycling
base but a short longest run cannot get to the distance by scaling weekly
kilometres. Weekly volume can only move so fast without injury, and the marathon
asks for one specific adaptation — tolerating hours of eccentric loading — that
only the long run trains.

So the long run carries the specific progression and the bike carries aerobic
volume. That also matches how this athlete self-regulates: run when the legs are
good, ride when they are sore.

Peak long run lands 14 days out, then a two-week taper.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from . import coaching_contract, config, db, zones

# Sunday long-run progression, in km, counting back from race week. The final
# entry is the race itself.
LONG_RUN_KM: tuple[float, ...] = (14, 18, 23, 28, 16)

# The bike is the release valve, stated on every easy day so it is a rule the
# athlete can act on rather than advice buried in a note.
SORE_SWAP = ("If the legs are sore or heavy, ride this instead at the same effort "
             "and keep the running for the days that matter.")

# Scheduling metadata a content refresh must never clobber: a dragged session
# keeps its slot and its Google Calendar event.
# Phase is NOT here: it is derived from the date and race day, so a refresh
# must recompute it or a week keeps a label the builder no longer agrees with.
PRESERVED_FIELDS = ("date", "week_index", "start_time",
                    "gcal_event_id", "pos_updated_at")


# A stated finish time inside the athlete's own goal text, e.g. "sub-3:30".
_GOAL_TIME = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")


def goal_pace_per_km() -> str | None:
    """Target pace derived from the athlete's own stated goal, or None.

    Derived, never invented. An explicit ``run_goal_min_per_km`` wins; otherwise
    a finish time is read out of the goal text and divided by the race distance.
    With neither this returns None and the sessions talk about effort rather than
    a number the athlete never asked for.
    """
    event = coaching_contract.event_context() or {}
    explicit = str((event.get("pacing_targets") or {}).get("run_goal_min_per_km") or "").strip()
    if explicit:
        return explicit
    goal = event.get("goal")
    text = goal.get("target") if isinstance(goal, dict) else goal
    match = _GOAL_TIME.search(str(text or ""))
    if not match:
        return None
    total = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3) or 0)
    try:
        km = float((event.get("distances") or {}).get("run_km") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or km <= 0:
        return None
    per_km = round(total / km)
    return f"{per_km // 60}:{per_km % 60:02d}"


def _easy_ceiling() -> str:
    """Explicit pace ceiling for easy running, from the athlete's real zones."""
    try:
        r = zones.pace_range("easy")
        if r:
            return f"no faster than {zones.fmt_pace(r[0])}/km"
    except Exception:  # noqa: BLE001
        pass
    return "conversational, no faster than easy"


def _wo(discipline: str, title: str, intensity: str, dur: int, warmup: str,
        main: str, cooldown: str, why: str, is_rest: int = 0) -> dict[str, Any]:
    return {"discipline": discipline, "title": title, "intensity": intensity,
            "duration_min": dur, "is_rest": is_rest, "why": why,
            "structure": {"warmup": warmup, "main": main, "cooldown": cooldown}}


def _long_run(km: float, week: int, total_weeks: int) -> dict[str, Any]:
    mins = int(km * 6.2)  # easy long-run pace, generous
    fuel = ("Practise race fuelling: 60-90 g carb/h from the tested mix, "
            "800-900 mg sodium/h, nothing new.") if km >= 18 else \
           "Carry fluid; start fuelling habits early."
    # Labelled "long", not "easy": zones read it as Z2 exactly the same, but the
    # label is what tells the rest of the app this is the week's key session.
    # A fast finish on the longest runs is the only place goal pace is rehearsed
    # on legs that are already tired, which is the state it has to be held in.
    pace = goal_pace_per_km()
    if km >= 23 and pace:
        body = (f"{km - 5:g} km steady, {_easy_ceiling()}, then the last 5 km at "
                f"{pace}/km. {fuel}")
        why = (f"The session that decides your marathon. Week {week} of {total_weeks}: "
               "the fast finish rehearses goal pace on tired legs, which is the "
               "only state it ever has to be held in.")
    else:
        body = f"{km:g} km steady, {_easy_ceiling()}. {fuel}"
        why = (f"The session that decides your marathon. Week {week} of {total_weeks}: "
               "time on feet and eccentric tolerance, not pace.")
    return _wo("run", f"Long run — {km:g} km", "long", mins,
               "10 min easy build", body, "10 min walk + legs up", why)


def _quality(week: int) -> dict[str, Any]:
    blocks = {1: "5×3 min @ threshold w/ 2 min jog",
              2: "4×6 min @ threshold w/ 2 min jog",
              3: "3×10 min @ threshold w/ 3 min jog",
              4: "20 min continuous @ threshold",
              5: "3×3 min @ threshold w/ 2 min jog"}
    return _wo("run", "Run — threshold", "threshold", 55,
               "15 min easy + 4 strides",
               blocks.get(week, blocks[2]),
               "10 min easy",
               "Holds the top end while volume climbs. One hard run a week is "
               "enough when the long run is already this demanding.")


def _easy_run(km: float, why: str) -> dict[str, Any]:
    return _wo("run", f"Easy run — {km:g} km", "easy", int(km * 6.4),
               "—", f"{km:g} km easy, {_easy_ceiling()}. {SORE_SWAP}", "—", why)


def _bike(mins: int, why: str) -> dict[str, Any]:
    return _wo("bike", "Aerobic ride", "Z2", mins, "10 min spin up",
               f"{mins - 20} min steady Z2 by heart rate", "10 min spin",
               why)


def _build_week(week: int, total_weeks: int, long_km: float,
                taper: bool) -> dict[int, dict[str, Any]]:
    """weekday index (0=Mon) -> session for one week of the block."""
    mid = round(max(6.0, min(14.0, long_km * 0.45)))
    easy = 6.0 if not taper else 5.0
    return {
        0: _bike(45, "Day after the long run. Spin the damage out; do not run today."),
        1: _quality(week),
        2: _easy_run(easy, "Easy aerobic volume between the two hard days."),
        3: _wo("run", f"Mid-long run — {mid:g} km", "easy", int(mid * 6.3), "—",
               f"{mid:g} km steady, {_easy_ceiling()}. {SORE_SWAP}", "—",
               "The second-longest run of the week. This is what turns a single "
               "long run into real durability."),
        4: _bike(50, "Aerobic volume with no landing impact, so Sunday's long run "
                     "starts on fresh legs."),
        5: _easy_run(4.0, "Short shakeout. Keep it genuinely easy.") if not taper
           else _wo("rest", "Rest", "rest", 0, "", "Full rest", "",
                    "Taper. Rest is the session.", is_rest=1),
        6: _long_run(long_km, week, total_weeks),
    }


def race_day() -> dict[str, Any]:
    """Race day names the athlete's actual event, never a hardcoded one."""
    event = coaching_contract.event_context() or {}
    name = str(event.get("event") or event.get("event_name") or "Marathon").strip()
    try:
        km = float((event.get("distances") or {}).get("run_km") or 42.195)
    except (TypeError, ValueError):
        km = 42.195
    hold = round(km * 0.76)
    pace = goal_pace_per_km()
    target = f" at {pace}/km" if pace else ""
    return _wo("run", f"\U0001F3C1 RACE DAY — {name}",
               "race", 240, "20 min easy + strides",
               f"{round(km, 1):g} km{target}. Hold that for the first {hold:g} km and bank "
               "nothing; the race starts after that.",
               "walk, refuel, protein within the hour",
               "Everything in this block was for today.")


def _ladder(start: datetime.date, race: datetime.date
            ) -> tuple[list[datetime.date], list[float]]:
    """Sundays from `start` to race day, and the long run each one carries.

    The ladder is right-aligned to race day, never left-aligned to `start`, so
    reconciling mid-block yields the same remaining long runs the athlete was
    already shown. A block longer than the stock ladder grows at the FRONT with
    smaller runs; the peak must stay two Sundays out however early the build
    began.
    """
    sundays: list[datetime.date] = []
    d = start + datetime.timedelta(days=(6 - start.weekday()) % 7)
    while d <= race:
        sundays.append(d)
        d += datetime.timedelta(days=7)

    n_long = max(len(sundays) - 1, 0)              # excludes race Sunday
    rungs = list(LONG_RUN_KM)
    while len(rungs) < n_long:
        rungs.insert(0, max(10.0, rungs[0] - 4))
    return sundays, rungs[-n_long:] if n_long else []


def workout_for_date(day: datetime.date, race: datetime.date,
                     sundays: list[datetime.date], ladder: list[float],
                     ) -> tuple[dict[str, Any], str]:
    """One day's session and its phase. Shared by seed and reconcile."""
    if day == race:
        return race_day(), "race"
    sunday = day + datetime.timedelta(days=(6 - day.weekday()) % 7)
    idx = sundays.index(sunday) if sunday in sundays else max(len(sundays) - 1, 0)
    long_km = ladder[idx] if idx < len(ladder) else (ladder[-1] if ladder else 14)
    # The peak long run sits exactly 14 days out and is a BUILD session; the
    # taper starts the day after it, not the week that contains it.
    taper = (race - sunday).days < 14
    session = _build_week(idx + 1, len(sundays), long_km, taper)[day.weekday()]
    return session, ("taper" if (race - day).days < 14 else "build")


def seed(start: datetime.date | None = None, *,
         overwrite_edited: bool = False) -> dict[str, Any]:
    """Write the marathon block from `start` through race day."""
    start = start or config.local_today()
    race = datetime.date.fromisoformat(config.race_date())
    if race < start:
        return {"error": "race date is in the past"}

    sundays, ladder = _ladder(start, race)
    if not sundays:
        return {"error": "no full week remains before race day"}

    week0_monday = start - datetime.timedelta(days=start.weekday())
    created = 0
    d = start
    while d <= race:
        day, phase = workout_for_date(d, race, sundays, ladder)
        day.update({"date": d.isoformat(),
                    "week_index": (d - week0_monday).days // 7,
                    "phase": phase, "source": "seed"})
        db.upsert_plan_day(day, only_if_absent_or_seed=not overwrite_edited)
        created += 1
        d += datetime.timedelta(days=1)

    return {"seeded_through": race.isoformat(), "days_written": created,
            "long_runs": ladder, "summary": db.plan_summary()}


def reconcile(start: datetime.date | None = None) -> dict[str, Any]:
    """Refresh remaining GENERATED rows in place from the current builder.

    Only rows still marked ``seed`` are touched, and only their content: the
    date, week index, phase and calendar placement of each row survive, so a
    session the athlete dragged keeps its slot and a session Coach Steve
    adjusted is never overwritten.
    """
    start = start or config.local_today()
    try:
        race = datetime.date.fromisoformat(config.race_date())
    except ValueError:
        return {"reconciled": 0, "skipped_modified": 0, "skipped_profile": 0,
                "error": "event profile date is invalid; expected YYYY-MM-DD"}
    if start > race:
        return {"reconciled": 0, "skipped_modified": 0, "skipped_profile": 0,
                "start": start.isoformat(), "end": race.isoformat()}

    sundays, ladder = _ladder(start, race)
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
        refreshed, phase = workout_for_date(day_date, race, sundays, ladder)
        refreshed.update({field: existing.get(field) for field in PRESERVED_FIELDS})
        refreshed["phase"] = phase
        refreshed["source"] = "seed"
        db.upsert_plan_day(refreshed)
        reconciled += 1

    return {"reconciled": reconciled, "skipped_modified": skipped_modified,
            "skipped_profile": skipped_profile, "skipped_invalid": skipped_invalid,
            "start": start.isoformat(), "end": race.isoformat()}
