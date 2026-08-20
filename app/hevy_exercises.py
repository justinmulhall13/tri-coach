"""Resolve an exercise name to a real Hevy template, creating one if needed.

Previously the coach could only use template IDs that already existed in the
athlete's history. When it wanted a Rear Delt Fly and found none, it silently
dropped the exercise, turning a six-exercise session into three without ever
saying which rule had bitten. That is the worst of both worlds: the athlete
loses the work *and* the explanation.

Hevy's API can create custom exercise templates, so a missing exercise is a
thing to create, not a reason to give up. The refusal that matters — never
inventing a *weight* — is untouched: this module invents a template definition,
never a load.

The POST schema does not match the GET schema, which is easy to get wrong:

===================  ====================
``GET`` returns      ``POST`` expects
===================  ====================
primary_muscle_group muscle_group
type                 exercise_type
equipment            equipment_category
===================  ====================
"""
from __future__ import annotations

import re
from typing import Any

from . import strength_visual

# Enum values the create endpoint accepts, taken from its own validation errors.
MUSCLE_GROUPS = frozenset({
    "abdominals", "shoulders", "biceps", "triceps", "forearms", "quadriceps",
    "hamstrings", "calves", "glutes", "abductors", "adductors", "lats",
    "upper_back", "traps", "lower_back", "chest", "cardio", "neck",
    "full_body", "other",
})
EXERCISE_TYPES = frozenset({
    "weight_reps", "reps_only", "bodyweight_reps", "bodyweight_assisted_reps",
    "duration", "weight_duration", "distance_duration", "short_distance_weight",
})
EQUIPMENT = frozenset({
    "none", "barbell", "dumbbell", "kettlebell", "machine", "plate",
    "resistance_band", "suspension", "other",
})

# Movement pattern -> the muscle the created template should be filed under.
_PATTERN_MUSCLE = {
    "squat": "quadriceps", "hinge": "hamstrings", "lunge": "quadriceps",
    "push_horizontal": "chest", "push_vertical": "shoulders", "fly": "chest",
    "pull_vertical": "lats", "pull_horizontal": "upper_back",
    "calf": "calves", "core": "abdominals", "curl": "biceps",
    "triceps": "triceps", "raise": "shoulders", "plyo": "quadriceps",
    "olympic": "full_body", "carry": "full_body", "cardio": "cardio",
    "other": "other",
}

# Title hints that override the pattern's default muscle.
_MUSCLE_HINTS = (
    (r"rear delt|reverse fly|rear[- ]?delt", "shoulders"),
    (r"\bglute|hip thrust|bridge", "glutes"),
    (r"\bcalf|calve", "calves"),
    (r"\btrap\b|shrug", "traps"),
    (r"forearm|wrist", "forearms"),
    (r"\bneck\b", "neck"),
    (r"lower back|back extension|hyperextension", "lower_back"),
    (r"abductor", "abductors"),
    (r"adductor", "adductors"),
)

_EQUIPMENT_HINTS = (
    (r"\bbarbell\b|\bbb\b|smith", "barbell"),
    (r"\bdumbbell\b|\bdb\b", "dumbbell"),
    (r"kettlebell|\bkb\b", "kettlebell"),
    (r"\bcable\b|\bmachine\b|pulldown|pushdown|pec ?deck|smith", "machine"),
    (r"resistance band|\bband\b", "resistance_band"),
    (r"suspension|\btrx\b", "suspension"),
    (r"\bplate\b", "plate"),
)

# Exercises measured by time rather than reps.
_DURATION_RE = re.compile(
    r"\bplank|hollow ?hold|dead ?hang|wall ?sit|\bhold\b|isometric", re.I)
# Bodyweight movements where a load is optional rather than expected.
_BODYWEIGHT_RE = re.compile(
    r"pull[- ]?up|chin[- ]?up|push[- ]?up|\bdips?\b|nordic|dead ?bug|bird ?dog|"
    r"air squat|sit[- ]?up|\bcrunch", re.I)

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
# Equipment qualifiers Hevy puts in parentheses; ignored when matching titles so
# "Rear Delt Fly" can match "Rear Delt Fly (Dumbbell)".
_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*")


def normalize(title: Any) -> str:
    """Comparable form of an exercise title, ignoring equipment qualifiers."""
    text = _QUALIFIER_RE.sub(" ", str(title or ""))
    return _NORMALIZE_RE.sub(" ", text.casefold()).strip()


def _tokens(title: Any) -> frozenset[str]:
    return frozenset(normalize(title).split())


def find_existing(templates: Any, title: str, *,
                  history_ids: Any = None,
                  prefer_equipment: str | None = None) -> dict[str, Any] | None:
    """Best existing template for a title, or ``None``.

    Hevy ships several equipment variants of the same movement, so "Chest Fly"
    alone matches Band, Dumbbell, Machine and Suspension. Picking the first is
    how a dumbbell presser ends up prescribed a barbell: variants the athlete
    has actually used win, which is what ``history_ids`` is for.

    Ranked, most specific first:

    1. the exact title;
    2. the same title ignoring the equipment qualifier — "Incline Bench Press"
       matches "Incline Bench Press (Dumbbell)";
    3. a candidate containing every word of the request — "Rear Delt Fly"
       matches "Rear Delt Reverse Fly (Dumbbell)".

    Within a tier: templates from the athlete's history first, then the closest
    match by word count, so "Chest Fly" never resolves to "Decline Chest Fly".
    A request whose words are not all present is never matched, which is why
    "Rear Delt Fly" still refuses to become "Lateral Raise".
    """
    if not isinstance(templates, (list, tuple)):
        return None
    wanted = normalize(title)
    if not wanted:
        return None
    # Accepts a set of ids or a mapping of id -> times logged. Counts let a
    # variant the athlete uses weekly beat one he tried once.
    if isinstance(history_ids, dict):
        counts = {str(k): int(v) for k, v in history_ids.items()}
    elif isinstance(history_ids, (set, frozenset, list, tuple)):
        counts = {str(i): 1 for i in history_ids}
    else:
        counts = {}
    known = set(counts)
    candidates = [t for t in templates if isinstance(t, dict) and t.get("id")]
    wanted_tokens = _tokens(title)

    def rank(template: dict[str, Any]) -> tuple:
        in_history = str(template.get("id")) in known
        equipment = str(template.get("equipment") or "")
        matches_equipment = bool(prefer_equipment) and equipment == prefer_equipment
        extra_words = len(_tokens(template.get("title")) - wanted_tokens)
        # Sorted ascending, so negate the preferences that should win.
        return (not in_history, -counts.get(str(template.get("id")), 0),
                not matches_equipment, extra_words,
                str(template.get("title") or ""))

    for tier in (
        lambda t: str(t.get("title") or "").casefold().strip() == str(title).casefold().strip(),
        lambda t: normalize(t.get("title")) == wanted,
        lambda t: wanted_tokens and wanted_tokens <= _tokens(t.get("title")),
    ):
        matched = [t for t in candidates if tier(t)]
        if matched:
            return sorted(matched, key=rank)[0]
    return None


def _hint(pairs: tuple[tuple[str, str], ...], text: str) -> str | None:
    for expression, value in pairs:
        if re.search(expression, text, re.I):
            return value
    return None


def creation_payload(title: str, *, pattern: str | None = None,
                     muscle_group: str | None = None,
                     equipment: str | None = None,
                     exercise_type: str | None = None) -> dict[str, Any]:
    """Build a valid ``POST /v1/exercise_templates`` body for a new exercise.

    Every field is constrained to the API's enums, so a bad guess becomes
    "other" rather than a rejected write the athlete has to decipher.
    """
    text = str(title or "").strip()
    if not text:
        raise ValueError("an exercise template needs a title")
    resolved_pattern = pattern or strength_visual.classify(text)

    muscle = muscle_group or _hint(_MUSCLE_HINTS, text) \
        or _PATTERN_MUSCLE.get(resolved_pattern, "other")
    if muscle not in MUSCLE_GROUPS:
        muscle = "other"

    gear = equipment or _hint(_EQUIPMENT_HINTS, text) or "none"
    if gear not in EQUIPMENT:
        gear = "other"

    if exercise_type:
        kind = exercise_type
    elif _DURATION_RE.search(text):
        kind = "duration"
    elif _BODYWEIGHT_RE.search(text):
        kind = "bodyweight_reps"
    else:
        kind = "weight_reps"
    if kind not in EXERCISE_TYPES:
        kind = "weight_reps"

    return {
        "title": text[:100],
        "muscle_group": muscle,
        "exercise_type": kind,
        "equipment_category": gear,
    }


def resolve(title: str, *, templates: Any, connector: Any = None,
            create: bool = True, idempotency_key: str = "",
            history_ids: Any = None, prefer_equipment: str | None = None) -> dict[str, Any]:
    """Find or create the template for one exercise title.

    Returns the outcome rather than raising, so a session can report exactly
    which exercises were matched, which were created, and which could not be —
    instead of quietly shrinking.
    """
    existing = find_existing(templates, title, history_ids=history_ids,
                             prefer_equipment=prefer_equipment)
    if existing is not None:
        return {"title": title, "exercise_template_id": str(existing["id"]),
                "resolution": "matched", "created": False,
                "matched_title": existing.get("title")}
    if not create or connector is None:
        return {"title": title, "exercise_template_id": None,
                "resolution": "missing", "created": False,
                "reason": "no matching Hevy template and creation was not requested"}
    payload = creation_payload(title)
    try:
        result = connector.create_exercise_template(
            payload, idempotency_key=idempotency_key or f"template:{normalize(title)}")
    except Exception as exc:  # noqa: BLE001 - reported, never silently swallowed
        return {"title": title, "exercise_template_id": None,
                "resolution": "failed", "created": False,
                "reason": f"{type(exc).__name__}: {exc}", "payload": payload}
    template_id = None
    if isinstance(result, dict):
        template_id = result.get("id") or (result.get("exercise_template") or {}).get("id")
    if not template_id:
        return {"title": title, "exercise_template_id": None,
                "resolution": "failed", "created": False,
                "reason": "Hevy accepted the create but returned no template id",
                "payload": payload}
    return {"title": title, "exercise_template_id": str(template_id),
            "resolution": "created", "created": True, "payload": payload}
