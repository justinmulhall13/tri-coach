"""How hard to lift today, decided by what the running needs.

In a triathlon or marathon block the running is the point and the lifting
supports it. A heavy lower session the day before a threshold run does not make
the athlete stronger; it makes the run worse and the block shorter. So run load
sets a ceiling on lifting effort, and recovery can only lower that ceiling —
never raise it.

The output is a reps-in-reserve target rather than a weight, because RIR is
valid whether or not a current working weight is known. When the athlete has
been lifting in sessions Hevy never captured, RIR is the *only* honest
instruction available, and this module says so rather than inventing a load.
"""
from __future__ import annotations

import datetime
from typing import Any

# Ordered from hardest to easiest. Ceilings and downgrades move along this list.
LEVELS = ("heavy", "moderate", "light", "skip")

# Run intensities that make a session a key quality effort. A lift scheduled
# next to one of these has to give way.
_QUALITY_INTENSITIES = {
    "threshold", "race pace", "vo2", "vo2max", "interval", "intervals",
    "tempo", "speed", "hard",
}

# A run at or beyond this duration is a key session regardless of its label.
LONG_RUN_MIN = 90

# Garmin readiness at or below this is poor enough to pull the ceiling down.
POOR_READINESS = 35
FAIR_READINESS = 50

_CUES = {
    "heavy": "Go hard: work up to a heavy top set",
    "moderate": "Leave 1-2 reps in the tank",
    "light": "Leave 3-4 reps in the tank: this is a maintenance dose",
    "skip": "Skip the lift today: the run matters more",
}


def _index(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 1


def _lower(level: str, steps: int = 1) -> str:
    return LEVELS[min(_index(level) + max(0, steps), len(LEVELS) - 1)]


def _as_date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def is_key_run(day: Any) -> bool:
    """True when a planned day is a run hard or long enough to protect."""
    if not isinstance(day, dict):
        return False
    if str(day.get("discipline") or "").lower() not in {"run", "brick", "race"}:
        return False
    if str(day.get("discipline") or "").lower() == "race":
        return True
    intensity = str(day.get("intensity") or "").strip().lower()
    if intensity in _QUALITY_INTENSITIES:
        return True
    duration = day.get("duration_min")
    return (isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and float(duration) >= LONG_RUN_MIN)


def _readiness_score(readiness: Any) -> int | None:
    """Morning training readiness, which is the recovery signal that matters.

    Intraday readiness moves with whatever the athlete did an hour ago and is
    not a statement about capacity for the day.
    """
    if not isinstance(readiness, dict):
        return None
    block = readiness.get("training_readiness")
    score = block.get("score") if isinstance(block, dict) else None
    return int(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None


def decide(*, today: datetime.date, plan_days: Any, readiness: Any = None,
           strength: Any = None) -> dict[str, Any]:
    """Decide today's lifting effort.

    Run load sets the ceiling; recovery and unknown working weights can only
    lower it. Every step is reported in ``reasons`` so the athlete can see the
    reasoning rather than being handed a verdict.
    """
    reasons: list[str] = []
    days = [d for d in (plan_days or []) if isinstance(d, dict)]
    by_date = {}
    for day in days:
        date = _as_date(day.get("date"))
        if date is not None:
            by_date[date] = day

    tomorrow = today + datetime.timedelta(days=1)
    today_key = is_key_run(by_date.get(today))
    tomorrow_key = is_key_run(by_date.get(tomorrow))

    # 1. Run load sets the ceiling.
    if today_key and tomorrow_key:
        level = "skip"
        reasons.append("A key run today and another tomorrow leaves no room for a lift.")
    elif tomorrow_key:
        level = "light"
        reasons.append("A key run is scheduled tomorrow, so today's lift stays a maintenance dose.")
    elif today_key:
        level = "moderate"
        reasons.append("A key run today already takes the hard effort; lift submaximally after it.")
    else:
        level = "heavy"
        reasons.append("No key run today or tomorrow, so this is the slot to lift hard.")
    ceiling = level

    # 2. Recovery can only lower the ceiling.
    score = _readiness_score(readiness)
    if score is not None:
        if score <= POOR_READINESS:
            level = _lower(level, 2)
            reasons.append(f"Morning readiness {score} is poor, so the ceiling drops two steps.")
        elif score <= FAIR_READINESS:
            level = _lower(level)
            reasons.append(f"Morning readiness {score} is only fair, so the ceiling drops a step.")
        else:
            reasons.append(f"Morning readiness {score} does not restrict the session.")
    else:
        reasons.append("No morning readiness score available, so it neither raises nor lowers effort.")

    # 3. Unknown working weights cap the session regardless of how fresh he is.
    calibration = bool(isinstance(strength, dict) and strength.get("calibration_required"))
    if calibration and _index(level) < _index("moderate"):
        level = "moderate"
        reasons.append(
            "Recent lifting was not logged with weights, so today calibrates by feel "
            "rather than chasing a number that is not known."
        )

    return {
        "level": level,
        "ceiling_from_run_load": ceiling,
        "reps_in_reserve": _RIR[level],
        "cue": _cue_for(level, calibration=calibration),
        "reasons": reasons,
        "key_run_today": today_key,
        "key_run_tomorrow": tomorrow_key,
        "readiness_score": score,
    }


_RIR = {"heavy": "0-1", "moderate": "1-2", "light": "3-4", "skip": None}


def _cue_for(level: str, *, calibration: bool) -> str:
    if calibration and level == "moderate":
        return "Calibration day: work up until 2 reps are left in the tank, then log it"
    return _CUES.get(level, _CUES["moderate"])
