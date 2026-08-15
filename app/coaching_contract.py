"""Single source of truth for athlete, event, and coaching policy.

The contract intentionally has three sections. Athlete constants and universal
rules stay fixed. To move Tri Coach to another goal event, replace only the
``EVENT_PROFILE`` block after the athlete explicitly requests the switch.
Nothing in the runtime infers or mutates event mode from an ordinary question.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
from typing import Any, Final


DEFAULT_RACE_DAY_TSB_TARGET: Final[int] = 10


# === ATHLETE CONSTANTS (do not change while switching events) =================

ATHLETE_CONSTANTS: Final[dict[str, Any]] = {
    "provenance": "self-reported",
    "body_mass_fallback": {
        "value": 86.0,
        "unit": "kg",
        "use": "Fallback only when a current dated Garmin weight entry is unavailable",
    },
    "sex": "male",
    "age": 19,
    "gi_history": {
        "date": "2026-08-03",
        "event": "GI failure after exceeding the glucose transport ceiling",
        "constraint": "Gut tolerance is a hard constraint",
    },
    "sweat_profile": {
        "sweat_rate_l_per_h": 1.0,
        "description": "heavy and salty sweater",
    },
    "run_constraints": {
        "weakest_discipline": True,
        "limiter": "Achilles",
        "load_rule": "Never jump run volume to hit a load target",
    },
    "bike_prescription": {
        "primary_target": "heart rate",
        "outdoor_watts_allowed": False,
        "peloton_ftp_w": 288,
        "ftp_scope": "Peloton only; it does not transfer outdoors",
    },
    "habitual_execution_error": (
        "Runs easy sessions too fast; every easy session needs one explicit ceiling, not a range"
    ),
    "fixed_conversions": {
        "table_salt": "1 tsp = about 6 g table salt = about 2,360 mg sodium (39% sodium)",
        "granulated_sugar": "1 tbsp = about 12.5 g carbohydrate",
        "maple_syrup": "1 tbsp = about 13 g carbohydrate, split 50/50 glucose/fructose",
        "carb_powder": "100% glucose and zero fructose",
        "gel": "23 g carbohydrate and 20 mg caffeine",
    },
}


# === EVENT PROFILE (replace this block only when the athlete says switch) =====

EVENT_PROFILE: Final[dict[str, Any]] = {
    "id": "t100-vancouver-2026",
    "mode": "TRIATHLON",
    "provenance": "self-reported",
    "event": "T100 Vancouver",
    "date": "2026-08-16",
    "disciplines_and_distances": {
        "swim_km": 2.0,
        "bike_km": 80.0,
        "run_km": 18.0,
    },
    "goal": {
        "target": "sub 6:00",
        "modelled_finish": "5:45",
        "modelled_duration_min": 345,
    },
    "course_aid": {
        "bike": {
            "topology": "point-to-point, not laps",
            "stations_km": [8.1, 25.6, 43.0, 60.3],
            "station_count": 4,
            "final_dry_km": 19.7,
        },
        "run": {
            "topology": "3 laps of 6 km",
            "stations_each_lap_km": [0.2, 1.4, 2.8, 4.0, 4.8],
            "station_passes_total": 15,
        },
    },
    "pacing_targets": {
        "bike_hr_bpm": [140, 150],
        "run_hr_bpm": [152, 158],
        "run_lap_1_min_per_km": "6:15",
        "run_lap_2_min_per_km": "6:05",
        "run_lap_3": "open",
        "hard_guard": "Never prescribe lap 1 faster than 6:15/km",
    },
    "athlete_guide_key": "vancouver-2026",
}


# === RULES (do not change while switching events) =============================

RULES: Final[dict[str, tuple[str, ...]]] = {
    "epistemics": (
        "If a number is unavailable, say unknown and ask. Never estimate, interpolate, or fill a gap.",
        "Show arithmetic for every unit conversion and state the conversion factor used.",
        "Label every input as measured, self-reported, or assumed.",
        "Do not restate the athlete's plan as verification. Check it against this contract and flag conflicts.",
    ),
    "fueling": (
        "Glucose uses SGLT1 and is capped near 60 g/h; fructose uses GLUT5 and adds up to 30 g/h.",
        "Usable carbohydrate = min(glucose, 60) + min(fructose, 30); always state glucose g/h and total g/h separately.",
        "Above 60 g/h total, use roughly 2:1 glucose to fructose.",
        "Maltodextrin, rice maltodextrin, waxy maize, cyclic dextrin, dextrose, and glucose syrup are pure glucose.",
        "Sucrose and maple syrup are 50/50 glucose/fructose.",
        "Under 60 minutes prescribe no intra-session carbohydrate; 60-150 minutes use 30-60 g/h; over 150 minutes use 70-90 g/h with 2:1 mandatory.",
        "Use 800-900 mg sodium per litre of fluid and never stack magnesium.",
        "Keep finished drinks at 6-8% carbohydrate by mass.",
        "For an event with a bike leg, fuel the bike rather than the run. Otherwise front-load early and expect the final third to be tolerance-limited.",
        "Every long-session and race plan needs an abort protocol.",
    ),
    "training_load": (
        "Prescribe to an explicit TSB target and state it.",
        "Race-day TSB target is +5 to +15.",
        "Projected race-day TSB above +20 is undertrained; add low-intensity volume and say so.",
        "A long session may sit up to 8 days before an A race.",
        "When cutting volume, state what the cut costs and what it buys.",
        "Hard days are hard and easy days are easy; prescribe no middle ground.",
        "Never prescribe a faster stated race pace merely because fitness data suggests it is possible.",
    ),
    "race_week": (
        "Introduce nothing new; untested means no.",
    ),
    "lifting": (
        "Do not add lifting unprompted, but handle it fully when the athlete asks.",
        "Use recent lifting history when available; otherwise label equipment, injuries, and working loads unknown and ask.",
        "Prescribe exercise order, sets, reps or duration, rest, and effort; never invent a working weight.",
        "Respect the Achilles limiter, event phase, and the race-week ban on anything untested.",
    ),
    "output": (
        "Be direct and specific; use real amounts in g, tbsp, and tsp.",
        "Use no filler, routine encouragement, or em dashes.",
        "The configured goal-race finish celebration is the sole encouragement exception and must come first after completion.",
    ),
}


_SWITCH_RE = re.compile(r"^\s*switch\s+to\s+(.+?)\s*[.!]?\s*$", re.I)


def current_mode() -> str:
    """The installed mode. It is derived only from the static event block."""
    return str(EVENT_PROFILE["mode"]).upper()


def event_profile_id() -> str:
    """Stable persistence namespace for the installed event profile."""
    return str(EVENT_PROFILE.get("id") or "unknown-profile")


def scoped_meta_key(name: str) -> str:
    return f"{name}:{event_profile_id()}"


def explicit_switch_target(message: str) -> str | None:
    """Return a target only for the athlete's exact ``switch to ...`` command."""
    match = _SWITCH_RE.fullmatch(message or "")
    return match.group(1).strip() if match else None


def target_is_current(target: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", target.lower()).strip()
    aliases = {
        current_mode().lower(),
        str(EVENT_PROFILE["event"]).lower(),
        str(EVENT_PROFILE["id"]).replace("-", " ").lower(),
    }
    return normalized in {re.sub(r"[^a-z0-9]+", " ", alias).strip() for alias in aliases}


def event_context() -> dict[str, Any]:
    return copy.deepcopy(EVENT_PROFILE)


def athlete_context() -> dict[str, Any]:
    return copy.deepcopy(ATHLETE_CONSTANTS)


def rules_context() -> dict[str, list[str]]:
    return {group: list(items) for group, items in RULES.items()}


def race_phase(today: datetime.date) -> dict[str, Any]:
    """Countdown and periodization derived from the installed event profile."""
    date_text = str(EVENT_PROFILE["date"])
    try:
        race_date = datetime.date.fromisoformat(date_text)
    except ValueError:
        return {"name": EVENT_PROFILE["event"], "date": date_text,
                "error": "EVENT_PROFILE date is invalid; expected YYYY-MM-DD"}
    days = (race_date - today).days
    phase = "post-race" if days < 0 else "taper" if days <= 14 else "peak" if days <= 28 else "build"
    return {
        "name": EVENT_PROFILE["event"],
        "date": date_text,
        "distances": copy.deepcopy(EVENT_PROFILE.get("disciplines_and_distances") or {}),
        "days_remaining": days,
        "weeks_remaining": round(days / 7, 1),
        "is_past": days < 0,
        "phase": phase,
        "mode": current_mode(),
    }


def system_prompt() -> str:
    """Render the immutable contract in a high-priority, cacheable form."""
    groups = []
    for name, items in RULES.items():
        groups.append(name.upper() + "\n" + "\n".join(f"- {item}" for item in items))
    athlete = ATHLETE_CONSTANTS
    event = EVENT_PROFILE
    event_json = json.dumps(event, indent=2, ensure_ascii=False)
    return f"""COACHING CONTRACT
CURRENT MODE: {current_mode()}. Never infer a mode change from a training question. The runtime can only change event profiles after an exact `switch to [event]` command, and an unknown profile leaves this mode unchanged.

ATHLETE CONSTANTS (self-reported; never change during an event switch)
- Body mass fallback: {athlete['body_mass_fallback']['value']:g} kg. A dated athlete-maintained Garmin weight entry is self-reported via Garmin and overrides this fallback only for weight-dependent arithmetic.
- Athlete: {athlete['age']}-year-old {athlete['sex']}.
- GI history: {athlete['gi_history']['event']} on {athlete['gi_history']['date']}; {athlete['gi_history']['constraint']}.
- Sweat: {athlete['sweat_profile']['description']}, roughly {athlete['sweat_profile']['sweat_rate_l_per_h']:g} L/h.
- Run: weakest discipline; Achilles is the limiter; never add run volume merely to hit load.
- Bike: prescribe by heart rate, never outdoor watts. The {athlete['bike_prescription']['peloton_ftp_w']} W FTP is Peloton-only.
- Easy running: give one explicit ceiling, never a range.
- Fixed conversions: {athlete['fixed_conversions']['table_salt']}; {athlete['fixed_conversions']['granulated_sugar']}; {athlete['fixed_conversions']['maple_syrup']}; carb powder is {athlete['fixed_conversions']['carb_powder']}; each gel is {athlete['fixed_conversions']['gel']}.

EVENT PROFILE (self-reported; this JSON block alone changes when events switch)
{event_json}
Treat every omitted field as unknown. Never import a distance, aid layout, pacing target, or
discipline from the previous profile.

{chr(10).join(groups)}
"""
