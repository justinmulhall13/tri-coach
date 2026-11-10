"""Fitness age as Garmin reports it, and where VO2max sits on Garmin's own scale.

Garmin only gives a fitness age from age 20. Below that its API answers with an
``invalidReason`` and no number. The athlete asked for a number anyway, so when
Garmin has none this module works one out and says so: ``estimate`` is a
separate field, never written into ``fitness_age``, and Garmin's own figure
replaces it the day there is one.

The estimate is the comparison Garmin's first fitness age made: the age at which
a typical person of the same sex has this VO2max. "Typical" is read off the
Cooper Institute bands Garmin prints in its watch manuals (the middle of the
Fair band, which spans the 40th to 60th percentile), placed at the middle of
each decade and joined with straight lines. The table starts at 20, so 20 is
the youngest the estimate can read; a VO2max above the typical 20 year old's
lands on that floor and is flagged, not extrapolated into a nonsense age.
"""
from __future__ import annotations

from typing import Any

BAND_NAMES = ("Poor", "Fair", "Good", "Excellent", "Superior")
# Share of the age group at or above the bottom of each band.
BAND_RANK = ("bottom 40%", "top 60%", "top 40%", "top 20%", "top 5%")

# Lower edge of Fair, Good, Excellent and Superior (ml/kg/min) by the last age of
# each decade. Source: VO2 max standard ratings in Garmin's owner manuals,
# reprinted there from The Cooper Institute.
VO2_BANDS: dict[str, tuple[tuple[int, tuple[float, float, float, float]], ...]] = {
    "male": (
        (29, (41.7, 45.4, 51.1, 55.4)),
        (39, (40.5, 44.0, 48.3, 54.0)),
        (49, (38.5, 42.4, 46.4, 52.5)),
        (59, (35.6, 39.2, 43.4, 48.9)),
        (69, (32.3, 35.5, 39.5, 45.7)),
        (79, (29.4, 32.3, 36.7, 42.1)),
    ),
    "female": (
        (29, (36.1, 39.5, 43.9, 49.6)),
        (39, (34.4, 37.8, 42.4, 47.4)),
        (49, (33.0, 36.3, 39.7, 45.3)),
        (59, (30.1, 33.0, 36.7, 41.1)),
        (69, (27.5, 30.0, 33.0, 37.8)),
        (79, (25.9, 28.1, 30.9, 36.7)),
    ),
}

_REASONS = {
    "USER_UNDER_AGE": ("under_age", "Garmin gives a fitness age from 20."),
    "USER_OVER_AGE": ("over_age", "Garmin does not give a fitness age at this age."),
    "USER_EMPTY_AGE": ("no_birth_date", "Garmin needs a birth date to work out a fitness age."),
    "USER_PREGNANT": ("paused", "Garmin pauses fitness age during pregnancy."),
}

_COMPONENT_LABELS = {
    "vigorousDaysAvg": "Vigorous days a week",
    "vigorousMinutesAvg": "Vigorous minutes a week",
    "rhr": "Resting heart rate",
    "bmi": "BMI",
    "bodyFat": "Body fat",
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _age(value: Any) -> float | None:
    number = _num(value)
    return round(number, 1) if number is not None and number > 0 else None


def vo2_standing(vo2: Any, age: Any, sex: Any) -> dict[str, Any] | None:
    """Band, rank and band edges for a measured VO2max, or None if it cannot be rated."""
    value = _num(vo2)
    table = VO2_BANDS.get(str(sex or "").lower())
    years = _num(age)
    if value is None or value <= 0 or table is None or years is None:
        return None
    start = 20
    edges = table[-1][1]
    end = table[-1][0]
    for last, row in table:
        if years <= last:
            edges, end = row, last
            start = max(20, last - 9)
            break
    else:
        start = table[-1][0] - 9
    index = sum(1 for edge in edges if value >= edge)
    people = "men" if str(sex).lower() == "male" else "women"
    return {
        "value": round(value, 1),
        "rating": BAND_NAMES[index],
        "rank": BAND_RANK[index],
        "group": f"{people} {start} to {end}",
        "bands": [{"name": name, "from": (None if i == 0 else edges[i - 1])}
                  for i, name in enumerate(BAND_NAMES)],
    }


ESTIMATE_FLOOR = 20


def _typical_curve(table: Any) -> list[tuple[float, float]]:
    """(age, typical VO2max) at the middle of each decade, youngest first."""
    return [(last - 4.5, round((edges[0] + edges[1]) / 2, 2)) for last, edges in table]


def _typical_at(curve: list[tuple[float, float]], age: float) -> float:
    """Typical VO2max at an age, on the straight lines through the decade points."""
    pairs = list(zip(curve, curve[1:]))
    for (a0, v0), (a1, v1) in pairs:
        if age <= a1:
            break
    return v0 + (v1 - v0) * (age - a0) / (a1 - a0)


def estimate_from_vo2(vo2: Any, sex: Any, *, chronological: Any = None) -> dict[str, Any] | None:
    """A fitness age worked out from VO2max alone, or None if it cannot be.

    The age whose typical VO2max equals the measured one, in whole years,
    held between the first and last age of the table.
    """
    value = _num(vo2)
    table = VO2_BANDS.get(str(sex or "").lower())
    if value is None or value <= 0 or table is None:
        return None
    curve = _typical_curve(table)
    ceiling_age = table[-1][0]
    top = _typical_at(curve, ESTIMATE_FLOOR)
    bottom = _typical_at(curve, ceiling_age)
    if value >= top:
        age, floor, ceiling = ESTIMATE_FLOOR, True, False
    elif value <= bottom:
        age, floor, ceiling = ceiling_age, False, True
    else:
        low, high = float(ESTIMATE_FLOOR), float(ceiling_age)
        for _ in range(40):                      # typical VO2max only falls with age
            mid = (low + high) / 2
            if _typical_at(curve, mid) > value:
                low = mid
            else:
                high = mid
        age, floor, ceiling = int(round((low + high) / 2)), False, False
    years = _num(chronological)
    return {
        "age": age,
        "floor": floor,
        "ceiling": ceiling,
        "method": "vo2max_norms",
        "vo2max": round(value, 1),
        "typical_vo2max_at_age": round(_typical_at(curve, age), 1),
        # Under 20 the comparison with his own age says nothing: the scale starts above him.
        "delta_years": (int(round(years - age))
                        if years is not None and years >= ESTIMATE_FLOOR else None),
    }


def _latest(raw: Any) -> dict[str, Any]:
    """Garmin documents a list and answers with an object; accept either."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        rows = [row for row in raw if isinstance(row, dict)]
        if rows:
            return max(rows, key=lambda row: str(row.get("lastUpdated") or ""))
    return {}


def _components(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    out = []
    for key, item in raw.items():
        if not isinstance(item, dict) or _num(item.get("value")) is None:
            continue
        out.append({
            "key": str(key),
            "label": _COMPONENT_LABELS.get(str(key), str(key)),
            "value": round(float(item["value"]), 1),
            "target": (round(float(item["targetValue"]), 1)
                       if _num(item.get("targetValue")) is not None else None),
            "potential_age": _age(item.get("potentialAge")),
            "stale": bool(item.get("stale")),
            "priority": item.get("priority") if isinstance(item.get("priority"), int) else 99,
        })
    out.sort(key=lambda row: (row["priority"], row["label"]))
    for row in out:
        row.pop("priority")
    return out


def normalise(raw: Any, *, age: Any = None, sex: Any = None, vo2: Any = None,
              vo2_bike: Any = None, vo2_as_of: Any = None) -> dict[str, Any]:
    """One card-ready record from Garmin's fitness-age answer and the VO2max read."""
    record = _latest(raw)
    fitness = _age(record.get("fitnessAge"))
    chronological = record.get("chronologicalAge")
    if isinstance(chronological, bool) or not isinstance(chronological, (int, float)):
        chronological = age if isinstance(age, (int, float)) and not isinstance(age, bool) else None
    reason = reason_text = None
    if fitness is None:
        reason, reason_text = _REASONS.get(
            str(record.get("invalidReason") or ""),
            ("unavailable", "Garmin did not return a fitness age."))
    standing = vo2_standing(vo2, chronological, sex)
    if standing is not None:
        standing["bike"] = round(float(vo2_bike), 1) if _num(vo2_bike) else None
        standing["as_of"] = str(vo2_as_of) if vo2_as_of else None
    updated = str(record.get("lastUpdated") or "")[:10] or None
    return {
        "fitness_age": fitness,
        # Only when Garmin has no number of its own. Labelled, never merged.
        "estimate": (estimate_from_vo2(vo2, sex, chronological=chronological)
                     if fitness is None else None),
        "chronological_age": int(chronological) if chronological is not None else None,
        "achievable_age": _age(record.get("achievableFitnessAge")) if fitness is not None else None,
        "previous_age": _age(record.get("previousFitnessAge")) if fitness is not None else None,
        "delta_years": (round(float(chronological) - fitness, 1)
                        if fitness is not None and chronological is not None else None),
        "components": _components(record.get("components")) if fitness is not None else [],
        "as_of": updated if fitness is not None else None,
        "reason": reason,
        "reason_text": reason_text,
        "vo2max": standing,
    }
