"""Race preparedness for whatever event is actually active.

The bundled T100 score is tuned to one race: its 2 km / 80 km / 18 km demands
are baked into fixed 14-day volume targets, which is why it is gated to that
profile. Reusing those numbers for a marathon would silently claim a runner
needed 170 km of cycling a fortnight.

This module derives the targets from the active profile's own distances
instead, so a marathon gets a marathon ring and the label reads "Marathon
readiness". The scoring shape deliberately mirrors the T100 model — volume
coverage, load balance, recovery — so the two are read the same way.
"""
from __future__ import annotations

import re
from typing import Any

# Fortnightly training volume as a multiple of race distance. A marathoner
# covers far more than one race distance in two weeks; a long-course swim leg
# is a small fraction of the week's swimming. These are coaching rules of
# thumb, not physiology, and are stated here so they can be argued with.
VOLUME_MULTIPLE = {"swim": 4.0, "bike": 2.2, "run": 1.0}

# How much each discipline counts toward the score. Run is weighted hardest for
# this athlete because it is the weakest discipline and the injury-limited one.
DISCIPLINE_WEIGHT = {"swim": 0.18, "bike": 0.22, "run": 0.34}

LOAD_WEIGHT = 0.16
RECOVERY_WEIGHT = 0.10

_DISTANCE_KEYS = {
    "swim": ("swim_km", "swim"),
    "bike": ("bike_km", "bike", "cycle_km", "ride_km"),
    "run": ("run_km", "run"),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def race_distances(profile: Any) -> dict[str, float]:
    """Per-discipline race distance in km, from the active event profile."""
    distances = {}
    source = {}
    if isinstance(profile, dict):
        for key in ("distances", "disciplines_and_distances"):
            candidate = profile.get(key)
            if isinstance(candidate, dict) and candidate:
                source = candidate
                break
    for discipline, keys in _DISTANCE_KEYS.items():
        for key in keys:
            value = _number(source.get(key))
            if value is not None:
                distances[discipline] = value
                break
    return distances


def event_label(profile: Any) -> str:
    """What to call this ring, derived from the event rather than hardcoded.

    Prefers a recognisable race shape ("Marathon", "Half marathon", "70.3")
    over the full event name, because the ring has room for two words.
    """
    if not isinstance(profile, dict):
        return "Race"
    name = str(profile.get("event_name") or profile.get("event") or "").strip()
    mode = str(profile.get("mode") or "").strip()
    run_km = race_distances(profile).get("run")

    haystack = f"{name} {mode}".lower()
    for pattern, label in (
        (r"\b70\.?3\b|half[- ]?iron", "70.3"),
        (r"\bironman\b|\b140\.?6\b|full distance", "Ironman"),
        (r"\bt100\b", "T100"),
        (r"half[- ]?marathon", "Half marathon"),
        (r"\bmarathon\b", "Marathon"),
        (r"\bultra\b", "Ultra"),
        (r"\b10\s?k\b", "10K"),
        (r"\b5\s?k\b", "5K"),
    ):
        if re.search(pattern, haystack):
            return label
    if run_km is not None and not race_distances(profile).get("bike"):
        # A run-only event is named by its distance when nothing else says so.
        for distance, label in ((42.0, "Marathon"), (21.0, "Half marathon"),
                                (10.0, "10K"), (5.0, "5K")):
            if abs(run_km - distance) <= 1.0:
                return label
        return f"{run_km:g} km"
    if mode:
        return mode.title()
    return name.split(",")[0][:18] or "Race"


def readiness(*, load14: Any, training_load: Any, readiness_score: Any,
              days_left: Any, profile: Any) -> dict[str, Any]:
    """0-100 preparedness against the active event's own demands."""
    distances = race_distances(profile)
    if not distances:
        return {"available": False,
                "reason": "the active event profile has no usable race distances"}

    by_sport = (load14 or {}).get("by_sport") if isinstance(load14, dict) else None
    by_sport = by_sport if isinstance(by_sport, dict) else {}

    components: dict[str, Any] = {}
    coverage: dict[str, float] = {}
    for discipline, race_km in distances.items():
        target = race_km * VOLUME_MULTIPLE.get(discipline, 1.0)
        actual = (by_sport.get(discipline) or {}).get("km") or 0
        ratio = min(1.0, actual / target) if target > 0 else 0.0
        coverage[discipline] = ratio
        components[discipline] = {
            "pct": round(ratio * 100), "km_14d": round(float(actual), 1),
            "target": round(target, 1), "race_km": race_km,
        }

    acwr = (training_load or {}).get("load_ratio") if isinstance(training_load, dict) else None
    if isinstance(acwr, (int, float)) and not isinstance(acwr, bool):
        load_c = 1.0 if 0.8 <= acwr <= 1.3 else 0.7
    else:
        load_c = 0.5
    components["load_balance"] = {"pct": round(load_c * 100), "acwr": acwr}

    score_value = readiness_score if isinstance(readiness_score, (int, float)) \
        and not isinstance(readiness_score, bool) else 50
    recovery_c = min(1.0, max(0.0, score_value / 100))

    # Weights are renormalised over the disciplines this event actually has, so
    # a run-only race is not permanently capped by absent swim and bike volume.
    weights = {d: DISCIPLINE_WEIGHT.get(d, 0.2) for d in coverage}
    discipline_total = sum(weights.values()) or 1.0
    volume_share = 1.0 - LOAD_WEIGHT - RECOVERY_WEIGHT
    volume_score = sum(
        (weights[d] / discipline_total) * volume_share * coverage[d] for d in coverage
    )
    score = round(100 * (volume_score + LOAD_WEIGHT * load_c + RECOVERY_WEIGHT * recovery_c))

    lowest = min(coverage.items(), key=lambda item: item[1])[0] if coverage else None
    return {
        "available": True,
        "score": score,
        "label": ("Race ready" if score >= 85 else "On track" if score >= 65 else
                  "Behind" if score >= 40 else "Way behind"),
        "event_label": event_label(profile),
        "days_left": days_left if isinstance(days_left, int) else None,
        "components": components,
        "lowest_volume_bucket": lowest,
        "basis": "14-day volume against this event's own race distances",
    }
