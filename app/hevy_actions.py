"""Validation and explicit-confirmation boundary for Hevy routine writes."""
from __future__ import annotations

import math
from typing import Any

from . import hevy_connector
from . import hevy_exercises
from . import strength_weights as sw


_SET_TYPES = {"warmup", "normal", "failure", "dropset"}

# A working weight may reach Hevy by exactly two routes: it is a value the
# athlete has actually lifted, or it is a bounded derivation from one such value
# (a deload after a layoff, a small progression). Anything else is a number the
# model made up, and is refused.
_EXACT = "hevy_history"
_DERIVED = "hevy_derived"
_WEIGHT_PROVENANCE = {_EXACT, _DERIVED}

# Keys carried through validation for server-side verification and for showing
# the athlete why a weight was chosen. Hevy's API does not accept them, so they
# are stripped from the payload immediately before the create.
_INTERNAL_SET_KEYS = ("weight_provenance", "derivation")


def _number(value: Any, *, minimum: float = 0) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= minimum else None


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    number = _number(value, minimum=minimum)
    if number is None or not number.is_integer():
        return None
    return int(number)


def resolve_routine_exercises(raw: dict[str, Any], *, create_missing: bool = True,
                              connector: Any = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill in `exercise_template_id` for any exercise given only by name.

    Previously the model had to supply an exact template id, so an exercise it
    could not find an id for was dropped and the session silently shrank. Now a
    plain title is enough: the full Hevy catalogue is searched (not just the
    athlete's recent history, which is what made ordinary movements look
    missing), preferring variants the athlete actually uses, and anything
    genuinely absent is created.

    Returns the routine plus one report per exercise, so the caller can tell the
    athlete which movements were matched, created, or could not be resolved
    rather than quietly returning a shorter workout.
    """
    reports: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return raw, reports
    exercises = raw.get("exercises")
    if not isinstance(exercises, list):
        return raw, reports

    client = connector or hevy_connector.connector()
    # Only reach for Hevy when something actually needs resolving: a routine
    # that already carries template ids should cost zero API calls.
    needs_lookup = any(
        isinstance(e, dict)
        and not str(e.get("exercise_template_id") or "").strip()
        and str(e.get("title") or e.get("exercise_name") or "").strip()
        for e in exercises
    )
    history_ids: Any = {}
    catalog: list[dict[str, Any]] = []
    if needs_lookup:
        # History counts only bias which equipment variant wins, so a failure
        # there is not fatal.
        try:
            history_ids = hevy_connector.history_template_counts()
        except Exception:  # noqa: BLE001
            history_ids = {}
        # Rank against the whole catalogue. A substring search cannot find
        # "Rear Delt Reverse Fly" from "Rear Delt Fly", and handing the matcher
        # an empty pool is what made exercises silently vanish from a session.
        try:
            catalog = client.all_exercise_templates() or []
        except Exception:  # noqa: BLE001
            catalog = []

    resolved: list[Any] = []
    for exercise in exercises:
        if not isinstance(exercise, dict):
            resolved.append(exercise)
            continue
        template_id = str(exercise.get("exercise_template_id") or "").strip()
        title = str(exercise.get("title") or exercise.get("exercise_name") or "").strip()
        if template_id or not title:
            resolved.append(exercise)
            continue
        pool = catalog
        if not pool:
            try:
                pool = client.search_exercise_templates(title) or []
            except Exception:  # noqa: BLE001
                pool = []
        report = hevy_exercises.resolve(
            title, templates=pool, connector=client if create_missing else None,
            create=create_missing, history_ids=history_ids,
        )
        reports.append(report)
        if report.get("exercise_template_id"):
            exercise = {**exercise, "exercise_template_id": report["exercise_template_id"]}
        resolved.append(exercise)
    return {**raw, "exercises": resolved}, reports


def validate_routine(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return an API-safe routine or explicit validation errors.

    Working weights are accepted only when tied to an exact Hevy-history value.
    An unlabelled model-generated weight is rejected instead of being acted on.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["routine object is required"]
    title = str(raw.get("title") or "").strip()
    if not title:
        errors.append("routine title is required")
    exercises = raw.get("exercises")
    if not isinstance(exercises, list) or not exercises:
        errors.append("at least one exercise is required")
        return None, errors
    if len(exercises) > 20:
        errors.append("routine cannot contain more than 20 exercises")

    clean_exercises: list[dict[str, Any]] = []
    for index, exercise in enumerate(exercises[:20], start=1):
        if not isinstance(exercise, dict):
            errors.append(f"exercise {index} must be an object")
            continue
        template_id = str(exercise.get("exercise_template_id") or "").strip()
        if not template_id:
            errors.append(f"exercise {index} is missing exercise_template_id")
            continue
        sets = exercise.get("sets")
        if not isinstance(sets, list) or not sets:
            errors.append(f"exercise {index} needs at least one set")
            continue
        if len(sets) > 12:
            errors.append(f"exercise {index} cannot contain more than 12 sets")
        clean_sets: list[dict[str, Any]] = []
        for set_index, item in enumerate(sets[:12], start=1):
            if not isinstance(item, dict):
                errors.append(f"exercise {index} set {set_index} must be an object")
                continue
            set_type = str(item.get("type") or "normal").lower()
            if set_type not in _SET_TYPES:
                errors.append(f"exercise {index} set {set_index} has invalid type")
                continue
            clean: dict[str, Any] = {"type": set_type}
            reps = _integer(item.get("reps"), minimum=1)
            duration = _integer(item.get("duration_seconds"), minimum=1)
            distance = _integer(item.get("distance_meters"), minimum=1)
            custom_metric = _number(item.get("custom_metric"))
            rep_range = item.get("rep_range")
            if reps is not None:
                clean["reps"] = reps
            elif isinstance(rep_range, dict):
                start = _integer(rep_range.get("start"), minimum=1)
                end = _integer(rep_range.get("end"), minimum=1)
                if start is not None and end is not None and start <= end:
                    clean["rep_range"] = {"start": start, "end": end}
            if duration is not None:
                clean["duration_seconds"] = duration
            if distance is not None:
                clean["distance_meters"] = distance
            if custom_metric is not None:
                clean["custom_metric"] = custom_metric
            if not any(key in clean for key in (
                "reps", "rep_range", "duration_seconds", "distance_meters", "custom_metric",
            )):
                errors.append(
                    f"exercise {index} set {set_index} needs reps, a rep range, duration, "
                    "distance, or custom metric"
                )
                continue
            weight = item.get("weight_kg")
            if weight is not None:
                parsed_weight = _number(weight)
                if parsed_weight is None:
                    errors.append(f"exercise {index} set {set_index} has invalid weight")
                    continue
                provenance = item.get("weight_provenance")
                if provenance not in _WEIGHT_PROVENANCE:
                    errors.append(
                        f"exercise {index} set {set_index} weight must be an exact Hevy value "
                        f"({_EXACT}) or a bounded derivation from one ({_DERIVED})"
                    )
                    continue
                if provenance == _DERIVED:
                    derivation = item.get("derivation")
                    if not isinstance(derivation, dict):
                        errors.append(
                            f"exercise {index} set {set_index} is marked {_DERIVED} but carries "
                            "no derivation record"
                        )
                        continue
                    if str(derivation.get("exercise_template_id") or "") != template_id:
                        errors.append(
                            f"exercise {index} set {set_index} derives its weight from a "
                            "different exercise than the one it prescribes"
                        )
                        continue
                    clean["derivation"] = derivation
                clean["weight_kg"] = parsed_weight
                clean["weight_provenance"] = provenance
            clean_sets.append(clean)
        if not clean_sets:
            continue
        rest = (_integer(exercise.get("rest_seconds"))
                if exercise.get("rest_seconds") is not None else None)
        cleaned: dict[str, Any] = {
            "exercise_template_id": template_id,
            "sets": clean_sets,
        }
        if rest is not None:
            cleaned["rest_seconds"] = rest
        elif exercise.get("rest_seconds") is not None:
            errors.append(f"exercise {index} rest_seconds must be a non-negative integer")
        if exercise.get("notes"):
            cleaned["notes"] = str(exercise["notes"])
        if exercise.get("superset_id") is not None:
            superset = _integer(exercise.get("superset_id"))
            if superset is not None:
                cleaned["superset_id"] = superset
            else:
                errors.append(f"exercise {index} superset_id must be a non-negative integer")
        clean_exercises.append(cleaned)

    if errors or not clean_exercises:
        return None, errors or ["routine has no valid exercises"]
    folder_id = raw.get("folder_id")
    if folder_id is not None and (
        isinstance(folder_id, bool) or not isinstance(folder_id, (int, float))
        or not math.isfinite(float(folder_id))
    ):
        return None, [*errors, "folder_id must be a finite number or null"]
    routine = {
        "title": title,
        "folder_id": folder_id,
        "notes": str(raw.get("notes") or ""),
        "exercises": clean_exercises,
    }
    return routine, []


def _recent_hevy_weights(client: hevy_connector.HevyConnector, *,
                         limit: int = 10) -> dict[tuple[str, float], dict[str, Any]]:
    """Read exact exercise/weight pairs from recent completed Hevy sets.

    Keyed by ``(exercise_template_id, weight kg rounded to 4dp)`` so both an
    exact-match claim and a derivation anchor can be checked against evidence
    fetched here rather than against anything the caller supplied.
    """
    listing = client.get_workouts(page=1, page_size=max(1, min(10, limit)))
    summaries = listing.get("workouts") if isinstance(listing, dict) else None
    found: dict[tuple[str, float], dict[str, Any]] = {}
    for summary in summaries or []:
        if not isinstance(summary, dict):
            continue
        workout = summary
        if not isinstance(workout.get("exercises"), list) and workout.get("id"):
            payload = client.get_workout(str(workout["id"]))
            workout = (payload.get("workout") if isinstance(payload, dict)
                       and isinstance(payload.get("workout"), dict) else payload)
        if not isinstance(workout, dict):
            continue
        for exercise in workout.get("exercises") or []:
            if not isinstance(exercise, dict):
                continue
            template_id = str(exercise.get("exercise_template_id") or "")
            if not template_id:
                continue
            for item in exercise.get("sets") or []:
                if not isinstance(item, dict):
                    continue
                weight = _number(item.get("weight_kg"))
                if weight is not None:
                    found[(template_id, round(weight, 4))] = {
                        "reps": item.get("reps"),
                        "date": str(workout.get("start_time") or "")[:10],
                    }
    return found


def create_confirmed_routine(raw: dict[str, Any], *, operation_id: str,
                             confirmed: bool) -> dict[str, Any]:
    """Verify every template and perform one non-retrying Hevy create."""
    if not confirmed:
        return {"error": "explicit confirmation is required", "created": False}
    routine, errors = validate_routine(raw)
    if errors or routine is None:
        return {"error": "invalid routine", "details": errors, "created": False}
    state = hevy_connector.status()
    if not state.get("connected"):
        return {"error": state.get("reason") or "Hevy is not connected", "created": False}
    client = hevy_connector.connector()
    try:
        verified = []
        for exercise in routine["exercises"]:
            template_id = exercise["exercise_template_id"]
            payload = client.get_exercise_template(template_id)
            # Official GET /exercise_templates/{id} returns the template directly.
            template = (payload.get("exercise_template") if isinstance(payload, dict)
                        and isinstance(payload.get("exercise_template"), dict) else payload)
            if not isinstance(template, dict) or str(template.get("id") or "") != template_id:
                return {
                    "error": f"Hevy exercise template {template_id} was not found",
                    "created": False,
                }
            verified.append(template_id)
        weighted_sets = [
            (exercise["exercise_template_id"], item)
            for exercise in routine["exercises"]
            for item in exercise["sets"]
            if item.get("weight_kg") is not None
        ]
        if weighted_sets:
            history = _recent_hevy_weights(client)
            problems: list[dict[str, Any]] = []
            for template_id, item in weighted_sets:
                weight_kg = float(item["weight_kg"])
                if item.get("weight_provenance") == _DERIVED:
                    failure = sw.verify_derivation(
                        item.get("derivation"), history, claimed_weight_kg=weight_kg,
                    )
                    if failure:
                        problems.append({
                            "exercise_template_id": template_id,
                            "weight_lb": sw.history_weight_lb(weight_kg),
                            "reason": failure,
                        })
                elif (template_id, round(weight_kg, 4)) not in history:
                    problems.append({
                        "exercise_template_id": template_id,
                        "weight_lb": sw.history_weight_lb(weight_kg),
                        "reason": "weight was not found in the 10 most recent Hevy workouts",
                    })
            if problems:
                return {
                    "error": "a proposed working weight could not be traced to Hevy history",
                    "details": problems,
                    "created": False,
                    "retry_safe": True,
                    "instruction": (
                        "Remove the weight, use an exact recent Hevy value, or derive it "
                        "from an anchor that appears in recent history."
                    ),
                }
    except (hevy_connector.HevyAPIError, hevy_connector.HevyUnavailableError, ValueError) as exc:
        return {
            "error": str(exc),
            "created": False,
            "retry_safe": True,
            "instruction": "No Hevy write was attempted; restore the read connection and retry.",
        }
    payload = {
        **routine,
        "exercises": [
            {**exercise, "sets": [
                {k: v for k, v in item.items() if k not in _INTERNAL_SET_KEYS}
                for item in exercise["sets"]
            ]}
            for exercise in routine["exercises"]
        ],
    }
    try:
        result = client.create_routine(payload, idempotency_key=operation_id)
    except (hevy_connector.HevyAPIError, hevy_connector.HevyUnavailableError, ValueError) as exc:
        return {
            "error": str(exc),
            "created": False,
            "retry_safe": False,
            "instruction": "Check Hevy before retrying because the create may already have arrived.",
        }
    return {"ok": True, "created": True, "routine": result.get("routine") or result,
            "verified_template_ids": verified}
