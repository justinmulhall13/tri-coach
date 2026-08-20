"""Unit-correct working-weight math grounded in verified Hevy history.

Hevy stores every weight in kilograms as a float.  An athlete who logs in
pounds therefore reads back values like ``61.235042773811365`` for a 135 lb
squat, and ``2.267964547178199`` for a 5 lb plate.  Displaying or prescribing
those raw numbers is wrong twice over: the precision is fictional, and the unit
is not the one the athlete actually loads onto the bar.

This module does three things and nothing else:

1.  Converts Hevy kilograms to the athlete's logging unit and snaps the
    float round-trip noise back onto the value that was really entered.
2.  Infers the loadable increment for an exercise from its own observed
    history, so a proposal lands on a weight the equipment can actually make.
3.  Derives a working weight as an auditable function of one exact historical
    set, never as a free-floating model guess.

Point 3 is the safety property.  ``hevy_actions`` refuses any weight that is
not either an exact historical value or a bounded derivation from one, so a
prescription can always be traced back to a set the athlete really performed.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

LB_PER_KG = 2.2046226218487757

# Hevy's kg floats round-trip from the pound the athlete typed, so the error is
# a few parts in 10^7.  A tolerance of 0.01 lb is far larger than that noise and
# far smaller than the smallest real increment (1 lb), so it cannot merge two
# genuinely different loads.
_SNAP_TOLERANCE_LB = 0.01

# Increments real equipment can make, ascending. 1.25 lb covers micro
# plates; 2.5 lb covers dumbbells and most cable stacks; 5 lb covers barbells.
_CANDIDATE_INCREMENTS_LB = (1.25, 2.5, 5.0, 10.0)

# A derived weight must stay inside a defensible band around its anchor. Below
# 60% the prescription is no longer that exercise's working weight; above 105%
# it is an untested max attempt dressed up as a working set.
MIN_DERIVATION_PCT = 0.60
MAX_DERIVATION_PCT = 1.05


class WeightDerivationError(ValueError):
    """Raised when a proposed weight cannot be tied to verified history."""


def kg_to_lb(kg: float) -> float:
    return float(kg) * LB_PER_KG


def lb_to_kg(lb: float) -> float:
    return float(lb) / LB_PER_KG


def _is_finite_number(value: Any) -> bool:
    # bool is an int subclass, and a numeric string is not a number here: both
    # are rejected so a weight can never arrive from a loosely-typed payload.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def snap_lb(lb: float) -> float:
    """Snap float round-trip noise onto the value the athlete actually logged.

    Hevy returns 135 lb as 135.00001745…; a plain ``round(x, 2)`` would keep
    the lie at a smaller scale. Snapping to the nearest half pound recovers the
    entered value for every increment real equipment produces, including the
    12.5 and 22.5 lb dumbbells present in this athlete's history.
    """
    if not _is_finite_number(lb):
        raise WeightDerivationError("weight must be a finite number")
    halves = round(float(lb) * 2.0) / 2.0
    return halves if abs(float(lb) - halves) <= _SNAP_TOLERANCE_LB else float(lb)


def history_weight_lb(weight_kg: float) -> float:
    """Convert one stored Hevy set weight into clean pounds."""
    return snap_lb(kg_to_lb(weight_kg))


def infer_increment_lb(observed_lb: Iterable[float]) -> float:
    """Largest candidate increment that explains every observed weight.

    Inferring from the exercise's own history avoids guessing at equipment:
    a cable stack that only moves in 5 lb steps yields 5, while a dumbbell
    rack that produced 12.5 and 22.5 yields 2.5.
    """
    values = [abs(float(v)) for v in observed_lb if _is_finite_number(v) and float(v) > 0]
    if not values:
        return _CANDIDATE_INCREMENTS_LB[1]
    # Largest first: 1.25 divides every value 2.5 does, so searching upward
    # would always return the finest increment and lose the real constraint.
    for increment in reversed(_CANDIDATE_INCREMENTS_LB):
        if all(abs(v / increment - round(v / increment)) <= 0.02 for v in values):
            return increment
    return _CANDIDATE_INCREMENTS_LB[0]


def increment_explains(observed_lb: Iterable[float], increment: float) -> bool:
    """True when every observed weight is a whole multiple of ``increment``."""
    values = [abs(float(v)) for v in observed_lb if _is_finite_number(v) and float(v) > 0]
    if not values or not _is_finite_number(increment) or float(increment) <= 0:
        return False
    return all(abs(v / float(increment) - round(v / float(increment))) <= 0.02 for v in values)


def nearest_observed_lb(target_lb: float, observed_lb: Iterable[float]) -> float | None:
    """The closest weight the athlete has actually used for this exercise.

    Selection plate stacks do not move in round increments: this athlete's calf
    machine produced 192, 332 and 392 lb, which fit no standard step. Rather
    than invent a load the pin cannot make, prescribe one the machine has
    demonstrably produced, breaking ties toward the lighter weight.
    """
    values = sorted({abs(float(v)) for v in observed_lb
                     if _is_finite_number(v) and float(v) > 0})
    if not values or not _is_finite_number(target_lb):
        return None
    return min(values, key=lambda v: (abs(v - float(target_lb)), v))


def round_to_increment(lb: float, increment: float) -> float:
    """Round a target load onto a weight the equipment can actually make.

    Rounds to nearest, breaking exact ties downward: overshooting a prescribed
    intensity is the error with real consequences, so a value sitting exactly
    between two loadable weights resolves to the lighter one.
    """
    if not _is_finite_number(lb) or not _is_finite_number(increment) or float(increment) <= 0:
        raise WeightDerivationError("weight and increment must be positive finite numbers")
    steps = math.ceil(float(lb) / float(increment) - 0.5)
    return max(float(increment), steps * float(increment))


def epley_e1rm_lb(weight_lb: float, reps: int) -> float:
    """Estimated one-rep max. Only meaningful for roughly 1-12 honest reps."""
    if not _is_finite_number(weight_lb) or not isinstance(reps, int) or reps < 1:
        raise WeightDerivationError("e1RM needs a finite weight and reps >= 1")
    return float(weight_lb) * (1.0 + reps / 30.0)


def derive_working_weight(
    *,
    anchor_weight_kg: float,
    anchor_reps: int | None,
    anchor_date: str,
    exercise_template_id: str,
    pct: float,
    increment_lb: float,
    observed_lb: Iterable[float] | None = None,
) -> dict[str, Any]:
    """Derive one working weight from an exact historical set.

    Returns the prescription together with the evidence it rests on, so the
    athlete can see *why* a number was chosen and the write boundary can
    re-verify the anchor against Hevy before any routine is created.
    """
    if not _is_finite_number(pct):
        raise WeightDerivationError("pct must be a finite number")
    pct = float(pct)
    if not MIN_DERIVATION_PCT <= pct <= MAX_DERIVATION_PCT:
        raise WeightDerivationError(
            f"pct {pct:.2f} is outside the safe derivation band "
            f"{MIN_DERIVATION_PCT:.2f}-{MAX_DERIVATION_PCT:.2f}"
        )
    anchor_lb = history_weight_lb(anchor_weight_kg)
    if anchor_lb <= 0:
        raise WeightDerivationError("anchor weight must be greater than zero")
    observed = list(observed_lb) if observed_lb is not None else []
    raw_target = anchor_lb * pct
    if observed and not increment_explains(observed, increment_lb):
        # No standard increment explains this exercise's history, so a computed
        # step would land on a weight the equipment cannot produce.
        target_lb = nearest_observed_lb(raw_target, observed) or round_to_increment(
            raw_target, increment_lb)
        rounding = "nearest_observed"
    else:
        target_lb = round_to_increment(raw_target, increment_lb)
        rounding = "increment"
    return {
        "weight_lb": target_lb,
        "weight_kg": lb_to_kg(target_lb),
        "increment_lb": float(increment_lb),
        "weight_provenance": "hevy_derived",
        "derivation": {
            "exercise_template_id": str(exercise_template_id),
            "anchor_weight_kg": float(anchor_weight_kg),
            "anchor_weight_lb": anchor_lb,
            "anchor_reps": anchor_reps,
            "anchor_date": str(anchor_date),
            "pct": pct,
            "increment_lb": float(increment_lb),
            "rounding": rounding,
        },
        "explanation": (
            f"{target_lb:g} lb = {round(pct * 100)}% of your {anchor_lb:g} lb"
            + (f" x{anchor_reps}" if anchor_reps else "")
            + f" on {anchor_date}, "
            + ("snapped to the nearest weight this machine has actually produced."
               if rounding == "nearest_observed"
               else f"rounded to the nearest {increment_lb:g} lb.")
        ),
    }


def verify_derivation(derivation: Any, history: dict[tuple[str, float], dict[str, Any]],
                      *, claimed_weight_kg: float) -> str | None:
    """Re-check a derived weight against verified history.

    Returns an error string, or ``None`` when the derivation is sound. The
    caller supplies ``history`` keyed by ``(template_id, rounded kg)`` built
    from real fetched workouts, so a model cannot supply its own evidence.
    """
    if not isinstance(derivation, dict):
        return "derived weight is missing its derivation record"
    template_id = str(derivation.get("exercise_template_id") or "")
    anchor_kg = derivation.get("anchor_weight_kg")
    pct = derivation.get("pct")
    if not template_id:
        return "derivation is missing exercise_template_id"
    if not _is_finite_number(anchor_kg) or not _is_finite_number(pct):
        return "derivation needs a finite anchor weight and pct"
    if not MIN_DERIVATION_PCT <= float(pct) <= MAX_DERIVATION_PCT:
        return (
            f"derivation pct {float(pct):.2f} is outside the safe band "
            f"{MIN_DERIVATION_PCT:.2f}-{MAX_DERIVATION_PCT:.2f}"
        )
    anchor_key = (template_id, round(float(anchor_kg), 4))
    if anchor_key not in history:
        return (
            f"anchor {history_weight_lb(float(anchor_kg)):g} lb for exercise "
            f"{template_id} was not found in fetched Hevy history"
        )
    increment = derivation.get("increment_lb")
    increment_lb = float(increment) if _is_finite_number(increment) else 2.5
    raw_target = history_weight_lb(float(anchor_kg)) * float(pct)
    if derivation.get("rounding") == "nearest_observed":
        # Reconstruct the loads this exercise has actually produced from the
        # same fetched history, so the caller cannot supply its own evidence.
        observed = [history_weight_lb(kg) for (tid, kg) in history if tid == template_id]
        expected_lb = nearest_observed_lb(raw_target, observed)
        if expected_lb is None:
            return "no observed history for this exercise to snap a weight onto"
    else:
        expected_lb = round_to_increment(raw_target, increment_lb)
    claimed_lb = history_weight_lb(float(claimed_weight_kg))
    if abs(expected_lb - claimed_lb) > _SNAP_TOLERANCE_LB:
        return (
            f"claimed weight {claimed_lb:g} lb does not match its own derivation "
            f"({expected_lb:g} lb)"
        )
    return None
