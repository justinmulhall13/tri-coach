"""Swim drills as a structured set, never a bare "6×50 drill".

The athlete's format: each drill is one 100, 50 of the drill then 50 of
swimming that keeps the skill, one named drill per 100.

`drill_set` writes the set for the plan builders. `tidy` rewrites a bare
"N×50 drill" wherever a stored or coach-written swim still carries one. It runs
where plan rows are read, so the session card, the watch workout and the coach
context all see the same text. Stored rows are not rewritten.

Wording constraints (the text is parsed by `garmin_workout._swim_steps` and by
`workoutBlocks` in the page):
- each 100 leads with "100m", so both read it as a 100 metre step;
- the cue sits inside brackets, where the parser does not split on commas;
- a cue never uses a digit, or a word the parser reads as a stroke, a piece of
  equipment, a rest or dryland work (back, kick, pull, fins, rest, hip, ...).
"""
from __future__ import annotations

import re
from typing import Any

# (name, the 50 of drill, the 50 of swim)
DRILLS: tuple[tuple[str, str, str], ...] = (
    ("6-second switch",
     "hold your side for a count of six then switch",
     "same long rotation on every stroke"),
    ("fingertip drag",
     "drag your fingertips along the surface with a high elbow",
     "same high relaxed elbow"),
    ("catch-up",
     "lead hand waits out front until the other taps it",
     "same patient lead hand"),
    ("3-point touch",
     "tap thigh, shoulder and head on the way over",
     "same high elbow path"),
)

DEFAULT_REST_S = 20


def drill_100(index: int, rest_s: int | None = DEFAULT_REST_S) -> str:
    name, drill, swim = DRILLS[index % len(DRILLS)]
    text = f"100m {name} (50 drill: {drill} / 50 swim: {swim})"
    return f"{text}, {rest_s}s rest" if rest_s else text


def drill_set(count: int = len(DRILLS), *, start: int = 0,
              rest_s: int | None = DEFAULT_REST_S) -> str:
    """`count` 100s, one drill each, starting at drill `start` in the rotation."""
    return "; ".join(drill_100(start + i, rest_s) for i in range(max(1, count)))


# "6×50 drill (catch-up, fingertip drag), 15s rest" and its shorter forms.
# "8×50 drill/build" is a mixed set and is left as written.
_BARE = re.compile(
    r"(?P<lead>\s*\+\s*)?"
    r"(?P<reps>\d+)\s*[×x]\s*(?P<dist>\d+)\s*m?\s*drills?\b(?!\s*/)"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s*,\s*(?P<rest>\d+)\s*s(?:ec(?:ond)?s?)?\s*rest\b)?",
    re.I)


def _structured(m: re.Match[str]) -> str:
    metres = int(m.group("reps")) * int(m.group("dist"))
    count = max(1, min(len(DRILLS), round(metres / 100)))
    rest = int(m.group("rest")) if m.group("rest") else None
    return ("; " if m.group("lead") else "") + drill_set(count, rest_s=rest)


def structure_drills(text: str) -> str:
    return _BARE.sub(_structured, text)


def tidy(structure: Any, discipline: Any) -> Any:
    if not isinstance(structure, dict) or str(discipline or "").lower() != "swim":
        return structure
    out = dict(structure)
    for key in ("warmup", "main", "cooldown"):
        if isinstance(out.get(key), str):
            out[key] = structure_drills(out[key])
    return out
