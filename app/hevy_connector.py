"""Hevy integration boundary with a deployable REST adapter.

Codex can use a host-managed Hevy MCP server, but Fly.io cannot inherit that
host process or its credential.  The deployed app therefore uses Hevy's public
REST API when ``HEVY_API_KEY`` is configured.  The protocol deliberately
matches the MCP capabilities so a remote-MCP transport can be substituted
later without changing Coach policy.

Hevy create operations are additive and are not safely retryable.  This module
never writes merely because a model generated a routine, never retries a timed
out create, and requires a caller-supplied operation key to suppress duplicate
writes within the running process.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class HevyUnavailableError(RuntimeError):
    """Raised instead of returning a fake empty history or fake write result."""


class HevyAPIError(RuntimeError):
    """A checked Hevy API failure that never includes the API key."""


class HevyConnector(Protocol):
    def status(self) -> dict[str, Any]: ...

    def get_workouts(self, *, page: int, page_size: int) -> dict[str, Any]: ...

    def get_workout(self, workout_id: str) -> dict[str, Any]: ...

    def get_exercise_template(self, exercise_template_id: str) -> dict[str, Any]: ...

    def search_exercise_templates(self, query: str) -> list[dict[str, Any]]: ...

    def create_routine(self, routine: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]: ...

    def create_workout(self, workout: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]: ...

    def create_exercise_template(self, exercise: dict[str, Any], *,
                                 idempotency_key: str) -> dict[str, Any]: ...


_CAPABILITIES = {
    "get_workouts": False,
    "get_workout": False,
    "get_exercise_template": False,
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
    get_exercise_template = _unavailable
    search_exercise_templates = _unavailable
    create_routine = _unavailable
    create_workout = _unavailable
    create_exercise_template = _unavailable


class HevyAPIConnector:
    """Minimal adapter for the official ``https://api.hevyapp.com/v1`` API."""

    def __init__(self, api_key: str, *, base_url: str = "https://api.hevyapp.com/v1",
                 timeout_s: float = 12.0) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("HEVY_API_KEY is required")
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._verified_at = 0.0
        self._verified_user: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._write_results: dict[tuple[str, str], dict[str, Any]] = {}
        self._template_catalog: list[dict[str, Any]] | None = None
        self._template_catalog_at = 0.0
        self._catalog_lock = threading.Lock()
        self._lock = threading.Lock()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            data=payload,
            method=method.upper(),
            headers={
                "api-key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Tri-Coach/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(600).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            suffix = f": {detail}" if detail else ""
            raise HevyAPIError(f"Hevy API returned HTTP {exc.code}{suffix}") from exc
        except (URLError, TimeoutError) as exc:
            # A POST may have reached Hevy before a timeout. Never retry it here.
            raise HevyAPIError(f"Hevy API connection failed: {type(exc).__name__}") from exc
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HevyAPIError("Hevy API returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise HevyAPIError("Hevy API returned an unexpected payload")
        return data

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._verified_at >= 300:
            try:
                user = self._request("GET", "user/info")
                self._verified_user = user
                self._last_error = None
            except HevyAPIError as exc:
                self._verified_user = None
                self._last_error = str(exc)
            self._verified_at = now
        connected = self._verified_user is not None
        capabilities = {name: connected for name in _CAPABILITIES}
        # The transport method exists, but the app intentionally exposes no
        # completed-workout write until a dedicated workout-set validator and
        # confirmation boundary are implemented.
        capabilities["create_workout"] = False
        return {
            "provider": "hevy",
            "configured": True,
            "connected": connected,
            "transport": "official_rest_api",
            "capabilities": capabilities,
            "reason": None if connected else (self._last_error or "Hevy credential not verified"),
        }

    def get_workouts(self, *, page: int, page_size: int) -> dict[str, Any]:
        return self._request("GET", "workouts", params={
            "page": max(1, int(page)),
            "pageSize": max(1, min(10, int(page_size))),
        })

    def get_workout(self, workout_id: str) -> dict[str, Any]:
        if not workout_id:
            raise ValueError("workout_id is required")
        return self._request("GET", f"workouts/{quote(str(workout_id), safe='')}")

    def get_exercise_template(self, exercise_template_id: str) -> dict[str, Any]:
        if not exercise_template_id:
            raise ValueError("exercise_template_id is required")
        template_id = quote(str(exercise_template_id), safe="")
        return self._request("GET", f"exercise_templates/{template_id}")

    def search_exercise_templates(self, query: str) -> list[dict[str, Any]]:
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        now = time.monotonic()
        with self._catalog_lock:
            if self._template_catalog is None or now - self._template_catalog_at >= 3600:
                catalog: list[dict[str, Any]] = []
                page = 1
                while True:
                    result = self._request("GET", "exercise_templates", params={
                        "page": page, "pageSize": 10,
                    })
                    templates = result.get("exercise_templates") or []
                    catalog.extend(item for item in templates if isinstance(item, dict))
                    page_count = result.get("page_count")
                    if not isinstance(page_count, int) or page >= page_count:
                        break
                    page += 1
                self._template_catalog = catalog
                self._template_catalog_at = now
            catalog = list(self._template_catalog)
        return [template for template in catalog
                if needle in str(template.get("title") or "").casefold()]

    def _create_once(self, path: str, envelope: str, value: dict[str, Any],
                     idempotency_key: str) -> dict[str, Any]:
        key = (idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for a Hevy create")
        operation_key = (path, key)
        with self._lock:
            previous = self._write_results.get(operation_key)
            if previous is not None:
                return dict(previous)
            # Keep the lock through the single network call. A concurrent retry
            # cannot race this operation and create a duplicate.
            result = self._request("POST", path, body={envelope: value})
            self._write_results[operation_key] = dict(result)
            return result

    def create_routine(self, routine: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self._create_once("routines", "routine", routine, idempotency_key)

    def create_workout(self, workout: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self._create_once("workouts", "workout", workout, idempotency_key)

    def create_exercise_template(self, exercise: dict[str, Any], *,
                                 idempotency_key: str) -> dict[str, Any]:
        """Create a custom exercise so a missing movement is not a dead end.

        The cached template catalog is dropped afterwards; otherwise the very
        next lookup would still report the exercise as missing and try to
        create it again.
        """
        result = self._create_once("exercise_templates", "exercise", exercise,
                                   idempotency_key)
        with self._catalog_lock:
            self._template_catalog = None
            self._template_catalog_at = 0.0
        return result


def _connector_from_environment() -> HevyConnector:
    key = (os.environ.get("HEVY_API_KEY") or "").strip()
    return HevyAPIConnector(key) if key else UnavailableHevyConnector()


# Built on first use, never at import. `app.config` is what loads `.env`, so a
# connector constructed while this module is imported may read an environment
# that does not yet contain HEVY_API_KEY — which silently produced a permanently
# "not connected" Hevy whenever this module happened to be imported first.
_connector: HevyConnector | None = None
_explicitly_configured = False
_CONTEXT_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CONTEXT_LOCK = threading.Lock()
_WORKOUTS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_WORKOUTS_LOCK = threading.Lock()
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
    """Install a connector explicitly. Tests and the host bridge use this."""
    global _connector, _explicitly_configured
    _connector = connector
    _explicitly_configured = True


def reset() -> None:
    """Return to the truthful disconnected adapter."""
    global _connector, _explicitly_configured
    _connector = UnavailableHevyConnector()
    _explicitly_configured = True


def reload_from_environment() -> HevyConnector:
    """Rebuild from the current environment, discarding any cached connector."""
    global _connector, _explicitly_configured
    _connector = _connector_from_environment()
    _explicitly_configured = False
    return _connector


def status() -> dict[str, Any]:
    return connector().status()


def is_lifting_query(text: str) -> bool:
    value = text or ""
    return bool(_LIFTING_RE.search(value) or _WEIGHTS_WORKOUT_RE.search(value))


def context_for(text: str) -> dict[str, Any] | None:
    """Return strength provenance and safety rules only for relevant chats."""
    if not is_lifting_query(text):
        return None
    state = status()
    recent: Any = "unknown"
    if state.get("connected"):
        client = connector()
        now = time.monotonic()
        with _CONTEXT_LOCK:
            if now - float(_CONTEXT_CACHE.get("at") or 0) < 300 and _CONTEXT_CACHE.get("data") is not None:
                recent = _CONTEXT_CACHE["data"]
            else:
                try:
                    # The official list endpoint embeds each workout's exercises,
                    # so a full page costs one request and gives the coach enough
                    # history to anchor a working weight rather than guess one.
                    listing = client.get_workouts(page=1, page_size=10)
                    summaries = listing.get("workouts") or []
                    detailed = []
                    for summary in summaries[:10]:
                        if isinstance(summary, dict) and isinstance(summary.get("exercises"), list):
                            detailed.append(summary)
                            continue
                        workout_id = summary.get("id") if isinstance(summary, dict) else None
                        if not workout_id:
                            continue
                        payload = client.get_workout(str(workout_id))
                        # Official GET /workouts/{id} returns the Workout directly.
                        # Accept the historical wrapper only for transport adapters.
                        workout = (payload.get("workout") if isinstance(payload, dict)
                                   and isinstance(payload.get("workout"), dict) else payload)
                        if isinstance(workout, dict) and workout.get("id"):
                            detailed.append(workout)
                    recent = detailed
                    _CONTEXT_CACHE.update({"at": now, "data": recent})
                except (HevyAPIError, HevyUnavailableError) as exc:
                    recent = {"error": str(exc), "value": "unknown"}
    return {
        "provenance": "self-reported via Hevy when connected",
        "connection": state,
        "recent_workouts": recent,
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


def all_workouts(max_pages: int = 6, page_size: int = 10) -> list[dict[str, Any]]:
    """Every recent logged workout, paged, for the lifting statistics.

    Cached briefly: the stats tab re-renders often and this is several round
    trips. A read failure returns what was gathered rather than raising, so a
    partial history still produces a usable tab.
    """
    now = time.monotonic()
    with _WORKOUTS_LOCK:
        cached = _WORKOUTS_CACHE.get("data")
        if cached is not None and now - float(_WORKOUTS_CACHE.get("at") or 0) < 300:
            return list(cached)
    client = connector()
    collected: list[dict[str, Any]] = []
    try:
        for page in range(1, max(1, max_pages) + 1):
            result = client.get_workouts(page=page, page_size=page_size)
            batch = (result or {}).get("workouts") or []
            collected.extend(w for w in batch if isinstance(w, dict))
            page_count = (result or {}).get("page_count")
            if not batch or (isinstance(page_count, int) and page >= page_count):
                break
    except Exception:  # noqa: BLE001 - a partial history still renders
        pass
    if collected:
        with _WORKOUTS_LOCK:
            _WORKOUTS_CACHE.update({"at": time.monotonic(), "data": list(collected)})
    return collected


def known_exercises() -> tuple[dict[str, str], dict[str, str]]:
    """Titles and muscle groups for exercises seen in cached recent history.

    Used to name an exercise on a proposal card without another API call and
    without letting a model supply the name. Returns empty maps when nothing
    has been fetched yet, which renders a placeholder rather than a guess.
    """
    titles: dict[str, str] = {}
    muscles: dict[str, str] = {}
    with _CONTEXT_LOCK:
        cached = _CONTEXT_CACHE.get("data")
    if not isinstance(cached, list):
        return titles, muscles
    for workout in cached:
        if not isinstance(workout, dict):
            continue
        for exercise in workout.get("exercises") or []:
            if not isinstance(exercise, dict):
                continue
            template_id = str(exercise.get("exercise_template_id") or "")
            if not template_id:
                continue
            if exercise.get("title"):
                titles.setdefault(template_id, str(exercise["title"]))
            if exercise.get("primary_muscle_group"):
                muscles.setdefault(template_id, str(exercise["primary_muscle_group"]))
    return titles, muscles


def connector() -> HevyConnector:
    """The active Hevy connector, built from the environment on first use.

    An unconfigured connector is retried rather than cached: the key may have
    arrived after this module was imported, which is exactly what happens when
    `.env` is loaded by `app.config`.
    """
    global _connector
    if _explicitly_configured and _connector is not None:
        return _connector
    if _connector is None or isinstance(_connector, UnavailableHevyConnector):
        _connector = _connector_from_environment()
    return _connector
