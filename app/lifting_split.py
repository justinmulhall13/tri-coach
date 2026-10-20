"""The athlete's standing three-day full-body split.

Three sessions a week — Full Body A, B and C — sitting on the Monday, Wednesday
and Friday of the 70.3 plan. Full body rather than upper/lower because three
sessions is the budget: an upper/lower split at three a week trains one half
twice and the other once, while full body touches every group three times at a
volume the swim, bike and run can absorb.

**The triathlon is the point.** Lifting supports it. That rules out the
bodybuilding leg day: heavy bilateral squats to failure leave legs that cannot
ride or run well for two days. Leg work leans on what transfers — unilateral
strength, eccentric hamstrings, hip extension, calf/Achilles stiffness — and
the pulling work favours the lats and mid-back that carry the swim.

The three days differ on purpose, and their order follows the week:

* **A (Monday)** is the heaviest leg day. It follows the Sunday long run and
  precedes a bike-only Tuesday, so the soreness lands on a concentric day.
* **B (Wednesday)** is unilateral and moderate, because Thursday is the run
  quality session.
* **C (Friday)** is the lightest on the legs — low-damage plyometrics and
  single-leg work — because Saturday is the long ride. It also carries no
  press, which gives the shoulder a day off from pressing every week.

**The shoulder is an injury constraint.** One press per session, six exercises,
no group twice in a row, no face pulls. Every day here is checked against
``lifting_rules`` rather than trusted to have been written correctly.

Exercise names are ordinary titles, not Hevy ids. ``hevy_exercises`` resolves
them against the full catalogue and prefers the equipment variant the athlete
already uses, so this file stays readable and does not rot when ids change.
"""
from __future__ import annotations

from typing import Any

from . import lifting_rules

SESSIONS_PER_WEEK = 3

# Slot order is the order they fall in a week: Monday, Wednesday, Friday.
SLOTS = ("full_a", "full_b", "full_c")


def _ex(title: str, sets: int, reps: str, rest: int, role: str, why: str) -> dict[str, Any]:
    return {"title": title, "sets": sets, "reps": reps, "rest_seconds": rest,
            "role": role, "why": why}


DEFAULT_SPLIT: tuple[dict[str, Any], ...] = (
    {
        "slot": "full_a",
        "name": "Full Body A",
        "focus": "Strength: squat, vertical pull, hinge",
        "why": ("The heavy day. It follows the long run and comes before a "
                "bike-only Tuesday, so the leg soreness lands where it costs least."),
        "exercises": (
            _ex("Front Squat", 3, "5-6", 150, "primary quad",
                "Upright squat, so the load lands on the quads rather than the lower "
                "back that also has to hold an aero position."),
            _ex("Lat Pulldown", 3, "8-10", 120, "vertical pull",
                "Lats through a full overhead range: the catch and pull of every "
                "freestyle stroke."),
            _ex("Romanian Deadlift", 3, "6-8", 150, "primary hinge",
                "Hamstring and glute strength through the hip, which holds the run "
                "stride together late in a 70.3."),
            _ex("Incline Bench Press", 3, "6-8", 150, "the one press",
                "The single pressing movement. Incline keeps the shoulder in a "
                "friendlier position than a flat bar."),
            _ex("Standing Calf Raise", 3, "10-12", 60, "calf and Achilles",
                "Stiffer calves and Achilles return more energy per stride and "
                "tolerate run volume off the bike."),
            _ex("Dead Bug", 3, "8-10 each side", 45, "core",
                "Anti-extension bracing that carries into aero position and running "
                "posture."),
        ),
    },
    {
        "slot": "full_b",
        "name": "Full Body B",
        "focus": "Unilateral: split squat, row, hip extension",
        "why": ("Single-leg and moderate, because Thursday is the run quality "
                "session. Swimming and running are both one side at a time."),
        "exercises": (
            _ex("Bulgarian Split Squat", 3, "8-10 each leg", 120, "unilateral quad",
                "Running is a single-leg sport; this loads that pattern without the "
                "systemic cost of a heavy back squat."),
            _ex("Single Arm Cable Row", 3, "10-12", 90, "horizontal pull",
                "One side at a time, which evens out the stronger arm — the same "
                "asymmetry that shows up as a crossover in the swim."),
            _ex("Hip Thrust", 3, "8-10", 120, "glute / hip extension",
                "Direct hip extension: the propulsive half of the stride and of the "
                "pedal stroke."),
            _ex("Half Kneeling Landmine Press", 3, "8-10", 150, "the one press",
                "Pressing on an arc the shoulder tolerates far better than a strict "
                "overhead barbell."),
            _ex("Nordic Hamstring Curl", 3, "5-6", 120, "eccentric hamstring",
                "The best-evidenced hamstring injury reducer. Keep the reps low and "
                "the lowering slow."),
            _ex("Pallof Press", 3, "10-12 each side", 45, "core",
                "Anti-rotation, which is exactly what the torso resists while running."),
        ),
    },
    {
        "slot": "full_c",
        "name": "Full Body C",
        "focus": "Power and shoulder health, light on the legs",
        "why": ("The day before the long ride, so the legs get elastic work and "
                "single-leg balance, not damage. No press: the shoulder gets a "
                "pressing-free day every week, and rear delt work balances the swim."),
        "exercises": (
            _ex("Box Jump", 4, "3", 90, "power / elasticity",
                "Low reps, full recovery. Trains tendon stiffness, which is free "
                "running economy, and creates almost no muscle damage."),
            _ex("Dumbbell Row", 3, "8-10", 120, "horizontal pull",
                "Mid-back strength that holds posture in the aero bars and late in "
                "the run."),
            _ex("Single Leg Landmine Hinge", 3, "8-10 each leg", 120, "unilateral hinge",
                "Single-leg hip extension with a balance demand, close to the stride."),
            _ex("Rear Delt Reverse Fly", 3, "12-15", 75, "shoulder health",
                "Rear delt work in place of face pulls, which aggravate this shoulder. "
                "Swimming is all internal rotation; this pulls it back."),
            _ex("Calf Extension", 3, "12-15", 60, "calf",
                "Bent-knee calf work to pair with the straight-leg raise on A; "
                "together they cover both heads."),
            _ex("Side Plank", 3, "30-40 s each side", 45, "core",
                "Lateral hip and trunk stability, the thing that stops the hips "
                "dropping when the run gets long."),
        ),
    },
)

# What a week must cover for the split to count as complete maintenance.
REQUIRED_COVERAGE = ("push", "pull", "shoulder", "quad", "hinge", "calf", "core")


def _comparable(day: Any) -> list[tuple[str, Any, Any]]:
    """The parts of a day that decide whether it differs from the default."""
    exercises = (day or {}).get("exercises") or []
    return [(str((e or {}).get("title") or "").strip().casefold(),
             (e or {}).get("sets"), str((e or {}).get("reps") or "").strip())
            for e in exercises if isinstance(e, dict)]


def _is_customised(day: dict[str, Any]) -> bool:
    """True when this day no longer matches the shipped default.

    Drives whether the tab offers to push the day to Hevy: an untouched default
    is not something the athlete has decided to send anywhere.
    """
    default = next((d for d in DEFAULT_SPLIT
                    if str(d.get("slot")) == str(day.get("slot"))), None)
    if default is None:
        return True
    if str(day.get("name") or "") != str(default.get("name") or ""):
        return True
    return _comparable(day) != _comparable(default)


# Card label by slot family. A stored split from the old upper/lower layout
# keeps its labels; anything else is a full-body day.
_TAGS = {"upper": "Upper", "lower": "Lower", "full": "Full body"}


def _day_summary(day: dict[str, Any]) -> dict[str, Any]:
    exercises = [dict(e) for e in day.get("exercises") or []]
    for exercise in exercises:
        exercise["group"] = lifting_rules.group_of(exercise)
        exercise["pattern"] = lifting_rules.pattern_of(exercise)
    return {
        **{k: v for k, v in day.items() if k != "exercises"},
        "exercises": exercises,
        "exercise_count": len(exercises),
        "rule_status": lifting_rules.summary(exercises),
        "is_lower": str(day.get("slot", "")).startswith("lower"),
        "tag": _TAGS.get(str(day.get("slot", "")).split("_")[0], "Full body"),
        "is_customised": _is_customised(day),
    }


def weekly_coverage(days: Any) -> dict[str, Any]:
    """Which movement groups the week trains, and what it misses."""
    counts: dict[str, int] = {}
    for day in days or []:
        for exercise in (day or {}).get("exercises") or []:
            group = exercise.get("group") or lifting_rules.group_of(exercise)
            counts[group] = counts.get(group, 0) + 1
    missing = [group for group in REQUIRED_COVERAGE if not counts.get(group)]
    return {
        "sets_by_group": counts,
        "missing": missing,
        "complete": not missing,
        "note": ("Every major group is trained at least once a week."
                 if not missing else
                 "This week leaves " + ", ".join(missing) + " untrained."),
    }


def build(days: Any = None) -> dict[str, Any]:
    """The active split, validated, with its reasoning attached.

    ``days`` allows a stored or edited split to be summarised the same way as
    the default, so the tab renders one shape whichever it is showing.
    """
    source = days if isinstance(days, (list, tuple)) and days else DEFAULT_SPLIT
    summarised = [_day_summary(day) for day in source if isinstance(day, dict)]
    coverage = weekly_coverage(summarised)
    problems = [
        {"day": day["name"], **violation}
        for day in summarised
        for violation in day["rule_status"]["violations"]
    ]
    return {
        "sessions_per_week": SESSIONS_PER_WEEK,
        "days": summarised,
        "coverage": coverage,
        "violations": problems,
        "ok": not problems and coverage["complete"],
        "principles": [
            "Three full-body sessions a week: A Monday, B Wednesday, C Friday.",
            "Leg load falls through the week — heavy A after the long run, "
            "unilateral B before the run quality day, light C before the long ride.",
            "Built for a triathlete: lats and mid-back for the swim, unilateral legs, "
            "eccentric hamstrings, hip extension and calves; no bodybuilding leg day.",
            "Heavy in base (Nov-Jan), maintenance once race-specific work starts, "
            "light in the taper.",
            "One press per session, six exercises, no group twice in a row, no face pulls.",
        ],
    }
