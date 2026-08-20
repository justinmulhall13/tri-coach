"""Single source of truth for athlete, event, and coaching policy.

The contract intentionally has three sections. Athlete constants and universal
rules stay fixed. To move Tri Coach to another goal event, replace only the
``EVENT_PROFILE`` block after the athlete explicitly requests the switch.
Nothing in the runtime infers or mutates event mode from an ordinary question.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
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

DEFAULT_EVENT_PROFILE: Final[dict[str, Any]] = {
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

# Backward-compatible test/development override. Runtime code reads the active
# persisted profile through ``event_context``; mutating/replacing this mapping is
# deliberately treated as an explicit in-process override by the persistence
# layer, not as a durable event switch.
EVENT_PROFILE: dict[str, Any] = copy.deepcopy(DEFAULT_EVENT_PROFILE)

PROFILE_FIELDS: Final[tuple[str, ...]] = (
    "event_name", "event_date", "distances", "goal", "mode",
)
PROFILE_PROVENANCE: Final[frozenset[str]] = frozenset({
    "confirmed_by_user", "pasted_from_guide", "unknown",
})


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
    """Read the active persisted mode on every call."""
    return str(event_context().get("mode") or "UNKNOWN").upper()


def event_profile_id() -> str:
    """Stable persistence namespace for the active persisted event profile."""
    return str(event_context().get("id") or "unknown-profile")


def scoped_meta_key(name: str) -> str:
    return f"{name}:{event_profile_id()}"


def explicit_switch_target(message: str) -> str | None:
    """Return a target only for the athlete's exact ``switch to ...`` command."""
    match = _SWITCH_RE.fullmatch(message or "")
    return match.group(1).strip() if match else None


def target_is_current(target: str) -> bool:
    event = event_context()
    normalized = re.sub(r"[^a-z0-9]+", " ", target.lower()).strip()
    aliases = {
        str(event.get("mode") or "").lower(),
        str(event.get("event") or event.get("event_name") or "").lower(),
        str(event.get("id") or "").replace("-", " ").lower(),
    }
    return normalized in {re.sub(r"[^a-z0-9]+", " ", alias).strip() for alias in aliases}


def event_context() -> dict[str, Any]:
    override = legacy_event_profile_override()
    if override is not None:
        return runtime_event_profile(override)
    # Late import avoids a config -> coaching_contract -> db -> config cycle.
    from . import db
    return runtime_event_profile(db.get_active_event_profile_record())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _server_profile_id(record: dict[str, Any]) -> str:
    """Create a server-owned namespace from every identity-defining field."""
    identity = {
        "event_name": record.get("event_name"),
        "event_date": record.get("event_date"),
        "distances": record.get("distances"),
        "goal": record.get("goal"),
        "mode": record.get("mode"),
    }
    default = _legacy_to_record(DEFAULT_EVENT_PROFILE)
    default_identity = {key: default.get(key) for key in identity}
    if identity == default_identity:
        return str(DEFAULT_EVENT_PROFILE["id"])
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    stem = _slug(f"{record.get('event_name')}-{record.get('event_date')}") or "event"
    return f"{stem}-{digest}"


def _legacy_to_record(profile: dict[str, Any]) -> dict[str, Any]:
    provenance = profile.get("field_provenance")
    if not isinstance(provenance, dict):
        provenance = profile.get("provenance") if isinstance(profile.get("provenance"), dict) else {}
    fields = {
        "event_name": profile.get("event_name", profile.get("event")),
        "event_date": profile.get("event_date", profile.get("date")),
        "distances": copy.deepcopy(profile.get("distances", profile.get("disciplines_and_distances")) or {}),
        "goal": copy.deepcopy(profile.get("goal")),
        "mode": str(profile.get("mode") or "").upper() or None,
    }
    extras = copy.deepcopy(profile.get("extras") or {})
    aliases = {
        "id", "event_name", "event", "event_date", "date", "distances",
        "disciplines_and_distances", "goal", "mode", "provenance",
        "field_provenance", "extras",
    }
    extras.update({k: copy.deepcopy(v) for k, v in profile.items() if k not in aliases})
    normalized_provenance = {
        field: (provenance.get(field) if provenance.get(field) in PROFILE_PROVENANCE else "unknown")
        for field in PROFILE_FIELDS
    }
    return {
        "id": str(profile.get("id") or "") or None,
        **fields,
        "provenance": normalized_provenance,
        "extras": extras,
    }


def normalize_event_profile(profile: dict[str, Any],
                            base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a partial staged profile without inventing missing values."""
    if not isinstance(profile, dict):
        raise ValueError("event profile must be an object")
    base_record = _legacy_to_record(base or {})
    incoming = _legacy_to_record(profile)
    merged: dict[str, Any] = {"id": incoming.get("id") or base_record.get("id")}
    for field in PROFILE_FIELDS:
        value = incoming.get(field)
        if field in {"distances", "goal"}:
            supplied = any(key in profile for key in (
                field,
                "disciplines_and_distances" if field == "distances" else "goal",
            ))
        else:
            aliases = {
                "event_name": ("event_name", "event"),
                "event_date": ("event_date", "date"),
                "mode": ("mode",),
            }[field]
            supplied = any(key in profile for key in aliases)
        merged[field] = copy.deepcopy(value if supplied else base_record.get(field))

    incoming_provenance = incoming["provenance"]
    base_provenance = base_record["provenance"]
    raw_provenance = (profile.get("field_provenance")
                      if isinstance(profile.get("field_provenance"), dict)
                      else profile.get("provenance")
                      if isinstance(profile.get("provenance"), dict) else {})
    merged["provenance"] = {
        field: (incoming_provenance[field] if field in raw_provenance
                else base_provenance.get(field, "unknown"))
        for field in PROFILE_FIELDS
    }
    merged["extras"] = {**base_record.get("extras", {}), **incoming.get("extras", {})}
    return merged


def prepare_event_profile_for_activation(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate all required fields after the athlete explicitly confirms."""
    record = normalize_event_profile(profile)
    missing = []
    if not str(record.get("event_name") or "").strip():
        missing.append("event_name")
    date_text = str(record.get("event_date") or "")
    try:
        datetime.date.fromisoformat(date_text)
    except ValueError:
        missing.append("event_date")
    distances = record.get("distances")
    if not isinstance(distances, dict) or not distances:
        missing.append("distances")
    else:
        try:
            valid_distances = all(not isinstance(v, bool) and float(v) > 0 for v in distances.values())
        except (TypeError, ValueError):
            valid_distances = False
        if not valid_distances:
            missing.append("distances")
    goal = record.get("goal")
    if goal in (None, "", {}):
        missing.append("goal")
    if not str(record.get("mode") or "").strip():
        missing.append("mode")
    if missing:
        raise ValueError("event profile is incomplete: " + ", ".join(dict.fromkeys(missing)))

    record["mode"] = str(record["mode"]).upper()
    # Never accept a caller/model-supplied namespace. This prevents a new
    # profile from claiming the reserved T100 feature gate or inheriting chat,
    # plans, and drafts from a materially different event.
    record["id"] = _server_profile_id(record)
    # An exact confirmation promotes only unknown provenance. Guide-sourced
    # facts retain their source even after the athlete approves the profile.
    record["provenance"] = {
        field: ("confirmed_by_user" if record["provenance"].get(field) == "unknown"
                else record["provenance"].get(field))
        for field in PROFILE_FIELDS
    }
    return record


def runtime_event_profile(record: dict[str, Any]) -> dict[str, Any]:
    """Expose persisted canonical fields plus legacy runtime aliases."""
    normalized = normalize_event_profile(record)
    runtime = copy.deepcopy(normalized.get("extras") or {})
    runtime.update({
        "id": normalized.get("id"),
        "mode": normalized.get("mode"),
        "event": normalized.get("event_name"),
        "date": normalized.get("event_date"),
        "disciplines_and_distances": copy.deepcopy(normalized.get("distances") or {}),
        "goal": copy.deepcopy(normalized.get("goal")),
        "event_name": normalized.get("event_name"),
        "event_date": normalized.get("event_date"),
        "distances": copy.deepcopy(normalized.get("distances") or {}),
        "provenance": copy.deepcopy(normalized.get("provenance") or {}),
        "field_provenance": copy.deepcopy(normalized.get("provenance") or {}),
    })
    return runtime


def default_event_profile_record() -> dict[str, Any]:
    record = _legacy_to_record(DEFAULT_EVENT_PROFILE)
    record["provenance"] = {field: "confirmed_by_user" for field in PROFILE_FIELDS}
    return prepare_event_profile_for_activation(record)


def legacy_event_profile_override() -> dict[str, Any] | None:
    """Honor explicit in-process replacements used by isolated tests/tools."""
    if EVENT_PROFILE == DEFAULT_EVENT_PROFILE:
        return None
    return _legacy_to_record(EVENT_PROFILE)


def athlete_context() -> dict[str, Any]:
    return copy.deepcopy(ATHLETE_CONSTANTS)


def rules_context() -> dict[str, list[str]]:
    return {group: list(items) for group, items in RULES.items()}


def race_phase(today: datetime.date) -> dict[str, Any]:
    """Countdown and periodization derived from the persisted active profile."""
    event = event_context()
    date_text = str(event.get("date") or "")
    try:
        race_date = datetime.date.fromisoformat(date_text)
    except ValueError:
        return {"name": event.get("event"), "date": date_text,
                "error": "EVENT_PROFILE date is invalid; expected YYYY-MM-DD"}
    days = (race_date - today).days
    phase = "post-race" if days < 0 else "taper" if days <= 14 else "peak" if days <= 28 else "build"
    return {
        "name": event.get("event"),
        "date": date_text,
        "distances": copy.deepcopy(event.get("disciplines_and_distances") or {}),
        "days_remaining": days,
        "weeks_remaining": round(days / 7, 1),
        "is_past": days < 0,
        "phase": phase,
        "mode": str(event.get("mode") or "UNKNOWN").upper(),
    }


def system_prompt() -> str:
    """Render the immutable contract in a high-priority, cacheable form."""
    groups = []
    for name, items in RULES.items():
        groups.append(name.upper() + "\n" + "\n".join(f"- {item}" for item in items))
    athlete = ATHLETE_CONSTANTS
    event = event_context()
    event_json = json.dumps(event, indent=2, ensure_ascii=False)
    return f"""COACHING CONTRACT
CURRENT MODE: {str(event.get('mode') or 'UNKNOWN').upper()}. Never infer a mode change from a training question. The runtime can only change event profiles after an exact `switch to [event]` command, and an unknown profile leaves this mode unchanged.

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
