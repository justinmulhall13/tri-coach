"""Dormant, provider-neutral boundary for a future Hevy MCP bridge.

Codex can access a host-managed Hevy MCP connector, but the deployed Fly.io
process cannot call that connector directly. Tri Coach therefore exposes the
real read/write shape without pretending it is connected. A future runtime
adapter can implement ``HevyConnector`` without changing Coach policy.

All Hevy create operations are additive and non-idempotent. This module never
performs a write merely because a model generated a draft.
"""
from __future__ import annotations

import re
from typing import Any, Protocol


class HevyUnavailableError(RuntimeError):
    """Raised instead of returning a fake empty history or fake write result."""


class HevyConnector(Protocol):
    def status(self) -> dict[str, Any]: ...

    def get_workouts(self, *, page: int, page_size: int) -> dict[str, Any]: ...

    def get_workout(self, workout_id: str) -> dict[str, Any]: ...

    def search_exercise_templates(self, query: str) -> list[dict[str, Any]]: ...

    def create_routine(self, routine: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]: ...

    def create_workout(self, workout: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]: ...


_CAPABILITIES = {
    "get_workouts": False,
    "get_workout": False,
    "search_exercise_templates": False,
    "create_routine": False,
    "create_workout": False,
}


class UnavailableHevyConnector:
    reason = "Hevy runtime bridge is not configured"

    def status(self) -> dict[str, Any]:
        return {
            "provider": "hevy",
            "connected": False,
            "transport": "unconfigured",
            "capabilities": dict(_CAPABILITIES),
            "reason": self.reason,
        }

    def _unavailable(self, *_args: Any, **_kwargs: Any) -> Any:
        raise HevyUnavailableError(self.reason)

    get_workouts = _unavailable
    get_workout = _unavailable
    search_exercise_templates = _unavailable
    create_routine = _unavailable
    create_workout = _unavailable


_connector: HevyConnector = UnavailableHevyConnector()
_LIFTING_RE = re.compile(
    r"\b(lift|lifting|strength|weight[ -]?training|working weight|gym|reps?|squat|"
    r"deadlift|rdl|bench(?: press)?|overhead press|barbell(?: row)?|dumbbell(?: row)?|"
    r"kettlebell|pull[ -]?up|(?:leg|push|pull) day|(?:upper|lower|full)[ -]?body|"
    r"hevy|lifting routine|strength routine)\b",
    re.I,
)
_WEIGHTS_WORKOUT_RE = re.compile(
    r"\b(?:weights?\s+(?:workout|session|routine|program)|"
    r"(?:program|build|make|do|use)\s+(?:me\s+)?(?:a\s+)?weights?)\b",
    re.I,
)


def configure(connector: HevyConnector) -> None:
    """Install a real runtime adapter. Application startup owns this decision."""
    global _connector
    _connector = connector


def reset() -> None:
    """Restore the truthful disconnected adapter, primarily for tests."""
    global _connector
    _connector = UnavailableHevyConnector()


def status() -> dict[str, Any]:
    return _connector.status()


def is_lifting_query(text: str) -> bool:
    value = text or ""
    return bool(_LIFTING_RE.search(value) or _WEIGHTS_WORKOUT_RE.search(value))


def context_for(text: str) -> dict[str, Any] | None:
    """Return strength provenance and safety rules only for relevant chats."""
    if not is_lifting_query(text):
        return None
    state = status()
    return {
        "provenance": "self-reported via Hevy when connected",
        "connection": state,
        "recent_workouts": "unknown" if not state.get("connected") else "available through connector",
        "read_workflow": [
            "List only the newest summaries needed, then fetch exact sets by workout id.",
            "Keep Hevy exercise/set history separate from Garmin measured physiology and training load.",
        ],
        "write_workflow": [
            "Search exercise templates first; never invent an exercise_template_id.",
            "A future reusable prescription is create_routine, not a completed workout.",
            "create_workout is only for a completed lift with exact performed sets and UTC start/end times.",
            "Generated drafts are proposals only; require explicit confirmation before a write.",
            "Never automatically retry a timed-out create because it may already have succeeded.",
        ],
    }


def connector() -> HevyConnector:
    """Return the configured adapter to the future integration service layer."""
    return _connector
