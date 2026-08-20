"""Hard programming constraints for this athlete's sessions.

These are injury constraints, not preferences. A session that breaks them is not
merely suboptimal — it risks hurting a shoulder that is already bad. That is why
they live here, in code, checked on every generated or edited session, rather
than only in a prompt the model may drift away from.

The rules, as the athlete stated them:

* At most **one pressing movement** per session. This is the core rule.
* **Six exercises** per session.
* Alternate so no muscle group runs **back to back** — a group gets a rest
  before it is loaded again. Triceps counts as push-family work, so a push
  followed by triceps is the shape this rule catches.

Triceps isolation on a pressing day is explicitly **allowed**. An earlier
version banned it, reading a remark about triceps as a second constraint when
it was emphasis on why the one-press limit matters. Over-constraining is its own
failure: it rejects sessions the athlete deliberately wants.
* **No face pulls.** They aggravate the shoulder; rear delt flies instead.
* Watch back volume — one true back/thickness movement, not three.

``check`` reports violations without changing anything. ``arrange`` reorders a
set of exercises to satisfy the alternation rule where a legal order exists.
Neither invents or removes an exercise: dropping work silently is how the coach
previously produced three-exercise sessions without saying why.
"""
from __future__ import annotations

import re
from typing import Any

from . import strength_visual

# Patterns that load the shoulder through a press. These, and only these, spend
# the single pressing slot. A fly is deliberately excluded: it is chest work
# that loads the shoulder through a different path, so a session may pair one
# press with a fly, which is exactly how the athlete builds an upper day.
PRESS_PATTERNS = frozenset({"push_horizontal", "push_vertical"})

# True back/thickness work, the volume the athlete flagged as excessive at three.
BACK_PATTERNS = frozenset({"pull_horizontal", "pull_vertical"})

TARGET_EXERCISE_COUNT = 6
MAX_PRESSES = 1
MAX_BACK_MOVEMENTS = 2

# Movements barred outright regardless of programming.
_BANNED = (
    (re.compile(r"face\s*pull", re.I),
     "face pulls aggravate this athlete's shoulder; use a rear delt fly instead"),
)

# Coarse group used for the alternation rule. Two exercises in the same group
# must not sit next to each other.
_PATTERN_GROUP = {
    "push_horizontal": "push", "push_vertical": "push", "triceps": "push",
    "fly": "push",
    "pull_horizontal": "pull", "pull_vertical": "pull", "curl": "pull",
    "raise": "shoulder", "carry": "shoulder",
    "squat": "legs", "hinge": "legs", "lunge": "legs", "calf": "legs",
    "plyo": "legs", "olympic": "legs",
    "core": "core", "cardio": "conditioning", "other": "other",
}


def pattern_of(exercise: Any) -> str:
    """Movement pattern for one exercise, however it is shaped."""
    if not isinstance(exercise, dict):
        return "other"
    explicit = exercise.get("pattern")
    if isinstance(explicit, str) and explicit in strength_visual.PATTERNS:
        return explicit
    return strength_visual.classify(
        exercise.get("title") or exercise.get("exercise_name"),
        primary_muscle_group=exercise.get("primary_muscle_group"),
    )


def group_of(exercise: Any) -> str:
    return _PATTERN_GROUP.get(pattern_of(exercise), "other")


def _title(exercise: Any) -> str:
    if not isinstance(exercise, dict):
        return ""
    return str(exercise.get("title") or exercise.get("exercise_name") or "")


def check(exercises: Any) -> list[dict[str, Any]]:
    """Every rule this session breaks. Empty means the session is safe to do.

    Each violation names the rule and the exercises responsible, so the coach
    can explain the change rather than quietly rewriting the athlete's session.
    """
    items = [e for e in (exercises or []) if isinstance(e, dict)] \
        if isinstance(exercises, (list, tuple)) else []
    violations: list[dict[str, Any]] = []

    presses = [e for e in items if pattern_of(e) in PRESS_PATTERNS]
    back = [e for e in items if pattern_of(e) in BACK_PATTERNS]

    if len(presses) > MAX_PRESSES:
        violations.append({
            "rule": "one_press_per_session",
            "severity": "injury",
            "detail": (f"{len(presses)} pressing movements; this shoulder tolerates "
                       f"{MAX_PRESSES} per session"),
            "exercises": [_title(e) for e in presses],
        })
    for exercise in items:
        for expression, reason in _BANNED:
            if expression.search(_title(exercise)):
                violations.append({
                    "rule": "banned_movement", "severity": "injury",
                    "detail": reason, "exercises": [_title(exercise)],
                })
    if len(back) > MAX_BACK_MOVEMENTS:
        violations.append({
            "rule": "back_volume",
            "severity": "programming",
            "detail": f"{len(back)} true back movements; keep it to {MAX_BACK_MOVEMENTS}",
            "exercises": [_title(e) for e in back],
        })
    if items and len(items) != TARGET_EXERCISE_COUNT:
        violations.append({
            "rule": "exercise_count",
            "severity": "programming",
            "detail": f"{len(items)} exercises; sessions are built as {TARGET_EXERCISE_COUNT}",
            "exercises": [],
        })
    for first, second in zip(items, items[1:]):
        group = group_of(first)
        if group == group_of(second) and group not in {"other", "core"}:
            violations.append({
                "rule": "consecutive_same_group",
                "severity": "programming",
                "detail": f"{_title(first)} and {_title(second)} are both {group}; "
                          "alternate so a group gets a rest",
                "exercises": [_title(first), _title(second)],
            })
    return violations


def arrange(exercises: Any) -> list[dict[str, Any]]:
    """Reorder so no two adjacent exercises share a group, keeping every item.

    Greedy with lookahead: repeatedly take the exercise whose group differs from
    the previous one, preferring the group with the most remaining work so the
    crowded groups get spread out instead of piling up at the end. When no legal
    pick exists the original order is kept for the remainder — ``check`` will
    still report it rather than pretending the session is clean.
    """
    items = [e for e in (exercises or []) if isinstance(e, dict)] \
        if isinstance(exercises, (list, tuple)) else []
    remaining = list(items)
    ordered: list[dict[str, Any]] = []
    previous = None
    while remaining:
        counts: dict[str, int] = {}
        for item in remaining:
            counts[group_of(item)] = counts.get(group_of(item), 0) + 1
        candidates = [e for e in remaining if group_of(e) != previous]
        if not candidates:
            ordered.extend(remaining)
            break
        pick = max(candidates, key=lambda e: counts[group_of(e)])
        ordered.append(pick)
        remaining.remove(pick)
        previous = group_of(pick)
    return ordered


def summary(exercises: Any) -> dict[str, Any]:
    """Rule status for one session, for the coach and the lifting tab."""
    violations = check(exercises)
    items = [e for e in (exercises or []) if isinstance(e, dict)] \
        if isinstance(exercises, (list, tuple)) else []
    return {
        "ok": not violations,
        "violations": violations,
        "injury_violations": [v for v in violations if v["severity"] == "injury"],
        "press_count": sum(1 for e in items if pattern_of(e) in PRESS_PATTERNS),
        "back_count": sum(1 for e in items if pattern_of(e) in BACK_PATTERNS),
        "exercise_count": len(items),
        "groups": [group_of(e) for e in items],
        "rules": [
            f"At most {MAX_PRESSES} pressing movement per session.",
            f"{TARGET_EXERCISE_COUNT} exercises per session.",
            "No two adjacent exercises from the same group.",
            "No face pulls; rear delt flies instead.",
        ],
    }
