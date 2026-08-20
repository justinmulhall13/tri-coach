"""Turn a validated Hevy routine into something the athlete can read at a glance.

The chat card needs three things the raw API payload does not carry: a movement
pattern (so each exercise can be drawn), a weight rendered in the athlete's own
unit, and the evidence behind that weight.

This module produces a *view* that is never sent to Hevy. Keeping it separate
from the routine payload means the thing displayed and the thing written are
derived from the same source but cannot contaminate each other: no render-only
field can accidentally reach the API, and no API field is reshaped for display.
"""
from __future__ import annotations

import re
from typing import Any

from . import strength_weights as sw

# Movement patterns the UI can draw. Anything unrecognised falls back to
# "other", which draws a neutral figure rather than a wrong one.
PATTERNS = (
    "squat", "hinge", "lunge", "push_horizontal", "push_vertical", "fly",
    "pull_vertical", "pull_horizontal", "calf", "core", "curl",
    "triceps", "raise", "plyo", "olympic", "carry", "cardio", "other",
)

# Title rules run before muscle-group rules because a title states the movement
# while a muscle group only states what it loads: "Romanian Deadlift" and "Good
# Morning" are both hamstrings, and both are hinges, but "Nordic Curl" is a
# hamstring exercise that is not a hinge at all.
_TITLE_RULES: tuple[tuple[str, str], ...] = (
    (r"\bcalf|calve", "calf"),
    (r"box jumps?\b|\bjumps?\b|\bplyo|\bbounds?\b|\bhops?\b|med ball slam", "plyo"),
    (r"\bcleans?\b|\bsnatch(es)?\b|\bjerks?\b", "olympic"),
    (r"\bcarry|carries|farmer|suitcase", "carry"),
    (r"split squats?\b|\blunges?\b|\bstep[- ]?ups?\b|bulgarian", "lunge"),
    (r"front squats?\b|\bzercher|\bsquats?\b|\bleg press\b|\bhack\b", "squat"),
    (r"\bdeadlifts?\b|\brdls?\b|good morning|\bhinges?\b|hip thrusts?\b|"
     r"glute bridge|\bswings?\b", "hinge"),
    (r"pull[- ]?ups?\b|chin[- ]?ups?\b|pulldowns?\b|pull[- ]?downs?\b", "pull_vertical"),
    (r"\brows?\b|face pulls?\b|pull[- ]?aparts?\b|rear delt", "pull_horizontal"),
    (r"overhead press|shoulder press|military|landmine press|push press", "push_vertical"),
    (r"\bfl(y|ies|yes)\b|pec ?deck|pec ?fly|chest fly|rear delt fly|reverse fly", "fly"),
    (r"bench press|floor press|\bpush[- ]?ups?\b|\bdips?\b|jm press", "push_horizontal"),
    (r"lateral raises?\b|front raises?\b|\braises?\b", "raise"),
    (r"triceps?\b|pushdowns?\b|skull ?crusher|extension \(triceps\)", "triceps"),
    (r"\bcurls?\b", "curl"),
    (r"\bplanks?\b|dead ?bugs?\b|pallof|palof|\bcrunch(es)?\b|sit[- ]?ups?\b|"
     r"\babs?\b|hollow|bird ?dogs?\b", "core"),
    (r"\bruns?\b|\bbikes?\b|\brow erg|treadmill|\bcardio", "cardio"),
)

_MUSCLE_PATTERN = {
    "calves": "calf",
    "quadriceps": "squat",
    "hamstrings": "hinge",
    "glutes": "hinge",
    "lower_back": "hinge",
    "lats": "pull_vertical",
    "upper_back": "pull_horizontal",
    "traps": "pull_horizontal",
    "chest": "push_horizontal",
    "shoulders": "push_vertical",
    "biceps": "curl",
    "triceps": "triceps",
    "abdominals": "core",
    "cardio": "cardio",
    "full_body": "olympic",
}

_COMPILED = tuple((re.compile(pattern, re.I), name) for pattern, name in _TITLE_RULES)


def classify(title: str | None, *, primary_muscle_group: str | None = None) -> str:
    """Best movement pattern for an exercise, for illustration purposes only."""
    text = str(title or "")
    for expression, name in _COMPILED:
        if expression.search(text):
            return name
    return _MUSCLE_PATTERN.get(str(primary_muscle_group or "").lower(), "other")


def _set_prescription(item: dict[str, Any]) -> str:
    reps = item.get("reps")
    rep_range = item.get("rep_range")
    if isinstance(reps, int) and not isinstance(reps, bool):
        return f"{reps} rep{'' if reps == 1 else 's'}"
    if isinstance(rep_range, dict):
        start, end = rep_range.get("start"), rep_range.get("end")
        if isinstance(start, int) and isinstance(end, int):
            return f"{start}-{end} reps"
    for key, unit in (("duration_seconds", "sec"), ("distance_meters", "m")):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:g} {unit}"
    return "set"


def _weight_display(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Weight in pounds plus the evidence for it, or ``(None, None)``."""
    weight_kg = item.get("weight_kg")
    if not isinstance(weight_kg, (int, float)) or isinstance(weight_kg, bool):
        return None, None
    pounds = sw.history_weight_lb(float(weight_kg))
    provenance = item.get("weight_provenance")
    if provenance == "hevy_derived":
        derivation = item.get("derivation") or {}
        anchor_lb = derivation.get("anchor_weight_lb")
        if anchor_lb is None and isinstance(derivation.get("anchor_weight_kg"), (int, float)):
            anchor_lb = sw.history_weight_lb(float(derivation["anchor_weight_kg"]))
        pct = derivation.get("pct")
        parts = []
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            parts.append(f"{round(float(pct) * 100)}% of")
        if anchor_lb is not None:
            reps = derivation.get("anchor_reps")
            parts.append(f"your {anchor_lb:g} lb" + (f" x{reps}" if reps else ""))
        if derivation.get("anchor_date"):
            parts.append(f"on {derivation['anchor_date']}")
        return f"{pounds:g} lb", (" ".join(parts) if parts else "derived from Hevy history")
    if provenance == "hevy_history":
        return f"{pounds:g} lb", "a weight you have lifted before"
    return f"{pounds:g} lb", None


def build_view(routine: dict[str, Any], *,
               titles: dict[str, str] | None = None,
               muscle_groups: dict[str, str] | None = None,
               effort_cue: str | None = None) -> dict[str, Any]:
    """Render model for the chat card. Never sent to Hevy.

    ``titles`` and ``muscle_groups`` map exercise_template_id to the values
    already known from Hevy history, so the card can name an exercise without
    a further API call and without the model inventing a name.
    """
    titles = titles or {}
    muscle_groups = muscle_groups or {}
    exercises = []
    for index, exercise in enumerate(routine.get("exercises") or [], start=1):
        if not isinstance(exercise, dict):
            continue
        template_id = str(exercise.get("exercise_template_id") or "")
        title = (exercise.get("title") or titles.get(template_id)
                 or f"Exercise {index}")
        sets = [item for item in (exercise.get("sets") or []) if isinstance(item, dict)]
        rendered = []
        for item in sets:
            weight, note = _weight_display(item)
            rendered.append({
                "type": item.get("type") or "normal",
                "prescription": _set_prescription(item),
                "weight": weight,
                "weight_note": note,
            })
        working = [item for item in rendered if item["type"] != "warmup"]
        exercises.append({
            "template_id": template_id,
            "title": str(title),
            "pattern": classify(title, primary_muscle_group=muscle_groups.get(template_id)),
            "set_count": len(working) or len(rendered),
            "summary": _exercise_summary(rendered),
            "rest_seconds": exercise.get("rest_seconds"),
            "notes": exercise.get("notes"),
            "sets": rendered,
        })
    return {
        "title": str(routine.get("title") or "Strength session"),
        "notes": str(routine.get("notes") or ""),
        "effort_cue": effort_cue,
        "exercise_count": len(exercises),
        "exercises": exercises,
        "unit": "lb",
    }


def _exercise_summary(rendered: list[dict[str, Any]]) -> str:
    """One line such as "3 x 8-10 @ 270 lb" when the sets agree."""
    working = [item for item in rendered if item["type"] != "warmup"] or rendered
    if not working:
        return ""
    prescriptions = {item["prescription"] for item in working}
    weights = {item["weight"] for item in working if item["weight"]}
    if len(prescriptions) == 1:
        line = f"{len(working)} x {next(iter(prescriptions))}"
    else:
        line = " · ".join(item["prescription"] for item in working)
    if len(weights) == 1:
        line += f" @ {next(iter(weights))}"
    return line
