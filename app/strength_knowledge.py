"""Physiotherapy and strength-coaching reference for Coach Steve.

The coach previously reasoned about pain and exercise selection from whatever a
general model happens to know. That produces plausible-sounding advice with no
consistent standard behind it — and on a shoulder that already hurts, plausible
is not good enough.

This module is a reference the coach reads, not a diagnostician. It carries
three kinds of thing:

* **Decision rules** a physio would actually apply — the pain-monitoring
  traffic light, tissue adaptation timelines, load-progression limits.
* **Substitutions**: for a movement that hurts, the alternative that keeps the
  training goal while changing the mechanics.
* **Referral triggers**: the signs that mean stop self-managing and see a
  clinician. These exist so the coach knows where its competence ends.

It never names a diagnosis. "Your shoulder hurts pressing overhead" is an
observation the coach can act on; "you have subacromial impingement" is a
clinical judgement it is not entitled to make, and acting on a wrong one is how
someone trains through a stress fracture.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- pain rules

PAIN_MONITORING = {
    "name": "Pain-monitoring traffic light",
    "rule": (
        "Pain during a lift is acceptable up to about 3/10 provided it settles "
        "to baseline within 24 hours and does not build session to session."
    ),
    "green": "0-3/10, settles within 24h, no morning stiffness beyond usual — continue as programmed.",
    "amber": (
        "4-5/10, or soreness still present the next morning — keep training but "
        "reduce load or range, and reassess the following session."
    ),
    "red": (
        "Above 5/10, pain that worsens across sets, night pain, or symptoms that "
        "are worse 24h later — stop that movement and substitute."
    ),
    "why": (
        "Complete rest deconditions the tissue that needs to tolerate load. "
        "Modifying the movement almost always beats stopping entirely."
    ),
}

REFERRAL_TRIGGERS = (
    "Pain that wakes the athlete at night or is present at rest.",
    "Pins and needles, numbness, or weakness that is neurological rather than fatigue.",
    "A sudden pop or tearing sensation, or immediate loss of strength.",
    "Localised bone pain that worsens with impact and persists at rest — possible "
    "bone stress injury, which running through can turn into a fracture.",
    "Pain unchanged or worse after two to three weeks of sensible modification.",
    "Any joint that is hot, visibly swollen, or locking.",
)

# ------------------------------------------------------- tissue and adaptation

ADAPTATION = (
    {"tissue": "Muscle", "timeline": "Strength gains within 2-4 weeks; visible size 8-12 weeks",
     "note": "Adapts fastest and forgives errors most readily."},
    {"tissue": "Tendon", "timeline": "8-12 weeks minimum, often longer",
     "note": ("Adapts far slower than the muscle pulling on it, which is why a "
              "rapid strength jump can produce tendon pain in a strong athlete. "
              "Tendons respond to heavy slow load and to time under tension, not volume.")},
    {"tissue": "Bone", "timeline": "3-4 months to remodel",
     "note": ("Responds to impact and to load variety. Bone stress builds silently; "
              "the first symptom is often localised pain that worsens with impact.")},
    {"tissue": "Cartilage / joint", "timeline": "Slow, and load-dependent",
     "note": "Tolerates repeated moderate load far better than sporadic heavy load."},
)

LOAD_PROGRESSION = (
    "Progress one variable at a time — load, or reps, or sets. Not two.",
    "Roughly 5-10% a week on a lift is sustainable; jumps beyond that outrun tendon adaptation.",
    "After more than two weeks off, restart at about 70-80% of previous working "
    "weight and rebuild over two to three sessions. Strength returns faster than "
    "tissue tolerance, so feeling strong is not evidence of being ready.",
    "Deload roughly every fourth to sixth week, or when performance stalls for two "
    "sessions at the same load.",
)

CONCURRENT_TRAINING = (
    "Strength MAINTENANCE in an endurance block needs intensity, not volume: two "
    "hard-enough sessions a week at 80%+ preserves strength on far less work than building it took.",
    "Interference is greatest when hard lifting and hard running sit within about "
    "6 hours of each other. Separating them by a day, or by morning and evening, largely removes it.",
    "Lower-body strength work does not blunt endurance adaptation; excessive lower-body "
    "VOLUME does, by leaving legs too sore to run well.",
    "Heavy strength work improves running economy. It is not optional padding in a "
    "marathon block — it is part of the training.",
)

# ------------------------------------------------------------- substitutions

# Keyed by the region the athlete reports. Each entry keeps the training goal
# while changing what the tissue is asked to do.
SUBSTITUTIONS = {
    "shoulder": {
        "usually_provocative": (
            "Barbell overhead press, especially behind the neck",
            "Wide-grip flat bench to full depth",
            "Upright rows",
            "Dips and deep chest stretches under load",
            "Face pulls for this athlete specifically",
        ),
        "usually_tolerated": (
            "Landmine and half-kneeling presses — an arc rather than straight overhead",
            "Neutral or semi-supinated grips",
            "Incline pressing over flat",
            "Floor press, which limits the bottom range that tends to pinch",
            "Rear delt and scapular work in a pain-free range",
        ),
        "swaps": (
            ("Overhead Press", "Half Kneeling Landmine Press"),
            ("Barbell Bench Press", "Incline Bench Press (Dumbbell)"),
            ("Upright Row", "Lateral Raise"),
            ("Face Pull", "Rear Delt Reverse Fly"),
            ("Dips", "Triceps Rope Pushdown"),
        ),
    },
    "knee": {
        "usually_provocative": ("Deep loaded squats", "Leg extension at end range",
                                "High-volume downhill running", "Deep lunges under load"),
        "usually_tolerated": ("Partial-range squats", "Spanish squat and wall sit isometrics",
                              "Hip-dominant work: RDL, hip thrust", "Step-ups to a lower box"),
        "swaps": (("Front Squat", "Leg Press (partial range)"),
                  ("Bulgarian Split Squat", "Step Up"),
                  ("Box Jump", "Trap Bar Jump (lower height)")),
    },
    "achilles": {
        "usually_provocative": ("Explosive plyometrics", "Fast hill running",
                                "Sudden mileage increases", "Deep loaded dorsiflexion"),
        "usually_tolerated": (
            "Heavy slow calf raises through a comfortable range",
            "Isometric calf holds, which reduce tendon pain for hours afterwards",
            "Seated calf work, which loads soleus with less tendon strain",
        ),
        "swaps": (("Box Jump", "Isometric Calf Hold"),
                  ("Standing Calf Raise", "Seated Calf Raise")),
    },
    "lower_back": {
        "usually_provocative": ("Loaded spinal flexion", "Heavy conventional deadlifts when fatigued",
                                "Good mornings at heavy load", "Back squats after a long run"),
        "usually_tolerated": ("Front-loaded squats", "Trap bar deadlift", "Hip thrust",
                              "Anti-extension and anti-rotation core work"),
        "swaps": (("Good Morning", "Hip Thrust"),
                  ("Romanian Deadlift", "Single Leg Landmine Hinge"),
                  ("Back Squat", "Front Squat")),
    },
    "hamstring": {
        "usually_provocative": ("Fast running at long muscle length", "Ballistic stretching",
                                "Heavy eccentrics while already sore"),
        "usually_tolerated": ("Nordic curls with reduced range", "Isometric hamstring bridges",
                              "Hip thrust", "Short-lever eccentrics"),
        "swaps": (("Nordic Hamstring Curl", "Hamstring Bridge Isometric"),
                  ("Romanian Deadlift", "Hip Thrust")),
    },
    "hip": {
        "usually_provocative": ("Deep flexion under load", "End-range hip flexor stretching",
                                "High-volume single-leg work when weak"),
        "usually_tolerated": ("Glute bridging", "Lateral band work", "Partial-range split squats"),
        "swaps": (("Bulgarian Split Squat", "Hip Thrust"),
                  ("Front Squat", "Leg Press (partial range)")),
    },
}

_REGION_PATTERNS = (
    ("shoulder", r"shoulder|delt|rotator|ac joint|impinge|press.*hurt"),
    ("knee", r"\bknee|patell|quad tendon|itb|it band"),
    ("achilles", r"achilles|calf pain|heel|tendon.*ankle"),
    ("lower_back", r"lower back|low back|lumbar|\bback pain|si joint"),
    ("hamstring", r"hamstring|hammy|posterior thigh"),
    ("hip", r"\bhip\b|glute pain|groin|adductor|hip flexor"),
)

# Stems, not whole words: a trailing \b would stop "tweak" matching "tweaked",
# "injur" matching "injured", or "tendin" matching "tendinopathy" — which is to
# say it would miss most of how anyone actually reports pain.
_PAIN_RE = re.compile(
    r"\b(?:pain|hurt|sore|ache|aching|aggravat|tweak|strain|"
    r"injur|niggle|flare|tight|stiff|swollen|impinge|tendin|inflam)\w*", re.I)

_PROGRAM_RE = re.compile(
    r"\b(?:program|split|deload|periodi|progress|volume|intensit|"
    r"maintain|maintenance|hypertroph|strength|taper|rehab|prehab|mobilit|"
    r"substitut|swap|replac|alternativ)\w*", re.I)


def mentions_pain(text: Any) -> bool:
    return bool(_PAIN_RE.search(str(text or "")))


def regions_mentioned(text: Any) -> list[str]:
    """Body regions the athlete named, most specific first."""
    value = str(text or "").lower()
    return [region for region, pattern in _REGION_PATTERNS if re.search(pattern, value)]


def is_relevant(text: Any) -> bool:
    """True when a turn should carry this reference."""
    value = str(text or "")
    return bool(mentions_pain(value) or _PROGRAM_RE.search(value) or regions_mentioned(value))


def context_for(text: Any, *, always_regions: Any = ("shoulder",)) -> dict[str, Any] | None:
    """The slice of the reference this turn actually needs.

    ``always_regions`` carries the athlete's standing problem area even when the
    turn does not mention it, so advice about a pressing day is informed by the
    shoulder without being asked.
    """
    if not is_relevant(text):
        return None
    regions = regions_mentioned(text)
    for region in (always_regions or ()):
        if region not in regions and region in SUBSTITUTIONS:
            regions.append(region)
    payload: dict[str, Any] = {
        "role": ("Reference from physiotherapy and strength-coaching practice. "
                 "It informs training decisions; it is not a diagnosis."),
        "load_progression": list(LOAD_PROGRESSION),
        "concurrent_training": list(CONCURRENT_TRAINING),
        "tissue_adaptation": [dict(item) for item in ADAPTATION],
        "regions": {region: {
            "usually_provocative": list(SUBSTITUTIONS[region]["usually_provocative"]),
            "usually_tolerated": list(SUBSTITUTIONS[region]["usually_tolerated"]),
            "swaps": [{"from": a, "to": b} for a, b in SUBSTITUTIONS[region]["swaps"]],
        } for region in regions if region in SUBSTITUTIONS},
        "rules_of_engagement": [
            "Never name a diagnosis. Describe what aggravates the movement and change the movement.",
            "Modify before resting: a substituted exercise keeps the tissue loaded, "
            "and complete rest deconditions what needs to tolerate load.",
            "One variable at a time when progressing back after pain.",
            "If a referral trigger is present, say so plainly and stop programming around it.",
        ],
    }
    if mentions_pain(text):
        payload["pain_monitoring"] = dict(PAIN_MONITORING)
        payload["referral_triggers"] = list(REFERRAL_TRIGGERS)
    return payload


def substitute(exercise_title: Any, region: Any) -> str | None:
    """A safer alternative for one movement, or ``None`` if nothing fits."""
    entry = SUBSTITUTIONS.get(str(region or "").lower())
    if not entry:
        return None
    wanted = re.sub(r"[^a-z0-9]+", " ", str(exercise_title or "").lower()).strip()
    if not wanted:
        return None
    for source, replacement in entry["swaps"]:
        key = re.sub(r"[^a-z0-9]+", " ", source.lower()).strip()
        if key and (key in wanted or wanted in key):
            return replacement
    return None
