"""The athlete's standing four-day lifting split.

Four sessions a week — Upper 1, Lower 1, Upper 2, Lower 2 — built to hold muscle
through a marathon block rather than to add it. Two constraints shape every
choice here:

**The running is the point.** Lifting supports it. That rules out the obvious
bodybuilding lower day: heavy bilateral back squats to depth leave legs that
cannot run well for two days. The leg days instead lean on the qualities that
transfer to running — unilateral strength, eccentric hamstring work, hip
extension, and calf/Achilles stiffness — and keep the highest-damage movements
low in volume.

**The shoulder is an injury constraint.** One press per session, six exercises,
no group twice in a row, no face pulls. Every day here is checked against
``lifting_rules`` rather than trusted to have been written correctly.

The two Upper days deliberately differ by plane: Upper 1 is horizontal, Upper 2
is vertical. Running the same session twice a week covers one angle twice and
leaves the other untrained.

Exercise names are ordinary titles, not Hevy ids. ``hevy_exercises`` resolves
them against the full catalogue and prefers the equipment variant the athlete
already uses, so this file stays readable and does not rot when ids change.
"""
from __future__ import annotations

from typing import Any

from . import lifting_rules

SESSIONS_PER_WEEK = 4

# Slot order is the order they should fall in a week. Upper/Lower alternate so
# neither half is worked on consecutive days.
SLOTS = ("upper_1", "lower_1", "upper_2", "lower_2")


def _ex(title: str, sets: int, reps: str, rest: int, role: str, why: str) -> dict[str, Any]:
    return {"title": title, "sets": sets, "reps": reps, "rest_seconds": rest,
            "role": role, "why": why}


DEFAULT_SPLIT: tuple[dict[str, Any], ...] = (
    {
        "slot": "upper_1",
        "name": "Upper 1",
        "focus": "Horizontal push and pull",
        "why": ("The horizontal half of the upper body: rowing and pressing in the "
                "same plane, with rear delt work to keep the shoulder balanced."),
        "exercises": (
            _ex("Dumbbell Row", 3, "8-10", 120, "primary pull",
                "Heaviest pull of the day while fresh; builds the mid-back that holds "
                "posture together late in a long run."),
            _ex("Incline Bench Press", 3, "6-8", 150, "the one press",
                "The single pressing movement. Incline keeps the shoulder in a "
                "friendlier position than a flat bar."),
            _ex("Rear Delt Reverse Fly", 3, "12-15", 75, "shoulder health",
                "Rear delt work in place of face pulls, which aggravate this shoulder."),
            _ex("Chest Fly", 3, "12-15", 75, "chest, non-press",
                "Adds chest volume without spending a second pressing slot."),
            _ex("Bicep Curl", 3, "10-12", 60, "arms",
                "Direct arm work the compound pulls do not finish."),
            _ex("Dead Bug", 3, "8-10 each side", 45, "core",
                "Anti-extension bracing that carries directly into running posture."),
        ),
    },
    {
        "slot": "lower_1",
        "name": "Lower 1",
        "focus": "Posterior chain, running-specific",
        "why": ("Hamstrings, glutes and calves — the tissues that fail first in "
                "marathon training. Unilateral and eccentric work, no heavy bilateral "
                "squatting, so this does not cost the next run."),
        "exercises": (
            _ex("Romanian Deadlift", 3, "6-8", 150, "primary hinge",
                "Hamstring strength through the hip, which protects the stride at the "
                "point where a tired runner starts to reach."),
            _ex("Bulgarian Split Squat", 3, "8-10 each leg", 120, "unilateral quad",
                "Running is a single-leg sport; this loads that pattern without the "
                "systemic cost of a heavy back squat."),
            _ex("Nordic Hamstring Curl", 3, "5-6", 120, "eccentric hamstring",
                "The single best-evidenced hamstring injury reducer. Keep the reps low "
                "and the lowering slow."),
            _ex("Calf Extension", 3, "12-15", 60, "calf and Achilles",
                "Stiffer calves and Achilles return more energy per stride and tolerate "
                "mileage better."),
            _ex("Hip Thrust", 3, "8-10", 120, "glute / hip extension",
                "Direct hip extension: the propulsive half of the stride."),
            _ex("Dead Bug", 3, "8-10 each side", 45, "core",
                "Keeps the pelvis quiet under fatigue."),
        ),
    },
    {
        "slot": "upper_2",
        "name": "Upper 2",
        "focus": "Vertical push and pull",
        "why": ("The vertical half: pulling overhead and pressing overhead, plus the "
                "arm work the horizontal day does not cover. Running Upper 1 twice "
                "would train one plane twice and leave this one untrained."),
        "exercises": (
            _ex("Lat Pulldown", 3, "8-10", 120, "primary vertical pull",
                "Lats through a full overhead range, which the rowing day never reaches."),
            _ex("Half Kneeling Landmine Press", 3, "8-10", 150, "the one press",
                "Vertical pressing on an arc the shoulder tolerates far better than a "
                "strict overhead barbell."),
            _ex("Lateral Raise", 3, "12-15", 60, "shoulder",
                "Side delt volume with a light load and no pressing cost."),
            _ex("Single Arm Cable Row", 3, "10-12", 90, "secondary pull",
                "One side at a time, which evens out the stronger arm."),
            _ex("Triceps Rope Pushdown", 3, "10-12", 60, "arms",
                "Triceps work is fine on a pressing day; only a second press is not."),
            _ex("Pallof Press", 3, "10-12 each side", 45, "core",
                "Anti-rotation, which is exactly what the torso resists while running."),
        ),
    },
    {
        "slot": "lower_2",
        "name": "Lower 2",
        "focus": "Power, quads and calves, running-specific",
        "why": ("The knee-dominant and elastic half. Jumping first while fresh for "
                "tendon stiffness, then quads and calves. Deliberately lighter in "
                "volume than a bodybuilding leg day so the week's running survives it."),
        "exercises": (
            _ex("Box Jump", 4, "3", 90, "power / elasticity",
                "Low reps, full recovery. Trains tendon stiffness, which is free "
                "running economy, and creates almost no muscle damage."),
            _ex("Single Leg Landmine Hinge", 3, "8-10 each leg", 120, "unilateral hinge",
                "Single-leg hip extension with a balance demand, close to the stride."),
            _ex("Front Squat", 3, "5-6", 150, "quad strength",
                "More upright than a back squat, so the load lands on the quads rather "
                "than the lower back that also has to absorb the long run."),
            _ex("Standing Calf Raise", 4, "10-12", 60, "calf",
                "Straight-leg calf work to pair with the bent-knee version on Lower 1; "
                "together they cover both heads."),
            _ex("Good Morning", 3, "8-10", 120, "hinge",
                "Hamstring and lower back under a light load, controlled."),
            _ex("Pallof Press", 3, "10-12 each side", 45, "core",
                "Anti-rotation to finish."),
        ),
    },
)

# What a week must cover for the split to count as complete maintenance.
REQUIRED_COVERAGE = ("push", "pull", "shoulder", "quad", "hinge", "calf", "core")


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
            "Four sessions a week: Upper 1, Lower 1, Upper 2, Lower 2.",
            "Upper days split by plane — horizontal, then vertical — so neither "
            "is trained twice while the other is skipped.",
            "Leg days are built for a runner: unilateral work, eccentric hamstrings, "
            "hip extension and calves, with the highest-damage lifts kept low in volume.",
            "One press per session, six exercises, no group twice in a row, no face pulls.",
        ],
    }
