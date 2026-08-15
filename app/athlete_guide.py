"""Compact, query-routed facts from the 2026 Vancouver Athlete Guide.

The source PDF is 66 image-heavy pages and is not present in the deployed image.
Sending its entire extracted text on every Coach turn would also undo the chat
latency work.  This module keeps the race-critical facts as structured,
page-linked sections and returns only the sections relevant to the current
conversation.

Source reviewed: ``26_Vancouver_Athlete-Guide-.pdf`` (66 pages, 2026 edition).
Page numbers below are the printed/PDF page numbers.
"""
from __future__ import annotations

import re
from typing import Any

from . import coaching_contract


_SECTIONS: dict[str, dict[str, Any]] = {
    "race_day": {
        "pages": [5, 13],
        "keywords": "race day start sunday schedule morning expo award bike checkout",
        "facts": [
            "100 km race is Sunday August 16, 2026; transition opens 05:00, closes 06:15, and the race starts 06:30 at Locarno Beach.",
            "Expo is 07:00-14:00; 100 km bike checkout is 12:00-14:30; all bikes and white bags must be removed by 14:30.",
            "100 km awards are 14:30-15:00; Sunday shuttle service ends 15:30.",
        ],
    },
    "locations_transport": {
        "pages": [5, 15],
        "keywords": "where location venue shuttle transport parking uber lyft dropoff start finish transition",
        "facts": [
            "Race pack/registration, swim start, and finish are at Locarno Beach; T1/T2 is at Jericho Sailing Centre.",
            "Free shuttles run from UBC North Parkade; Sunday 04:30-06:15 every 15 minutes, 06:15-11:15 every 30 minutes, none 11:15-12:00, then every 15 minutes to 15:30.",
            "Sunday 06:20-08:40 venue drop-off is Jericho Hill Centre due to the live course; allow 15 extra walking minutes.",
            "Rideshare drop-off is NW Marine Drive and West 2nd Ave; allow 15 extra walking minutes.",
        ],
    },
    "registration_checkin": {
        "pages": [12, 16, 17, 20, 21],
        "keywords": "registration race pack packet qr id check in check-in bike helmet rack sticker tattoo wristband timing chip",
        "facts": [
            "100 km race-pack collection is Friday 12:00-18:00 or Saturday 09:00-15:00 at Jericho Sailing Centre; none on race day. Bring race QR code and photo ID.",
            "Mandatory 100 km bike/helmet check-in is Saturday 12:00-16:30; there is no bike check-in race morning.",
            "The timing chip is issued after racking the bike; wear it on the left ankle with the transponder facing out and return it at the finish ($75 replacement charge).",
            "Race-morning transition access is allowed for bike checks, tyre inflation, and adding nutrition; do not leave food overnight.",
        ],
    },
    "transition_bags": {
        "pages": [17, 23, 24],
        "keywords": "transition t1 t2 bag black red white shoes helmet nutrition change tent after race bag drop pump",
        "facts": [
            "100 km uses a black Swim-to-Bike bag on the bottom rack and red Bike-to-Run bag on the top rack; both stay in the change tent.",
            "The black bag may hold cycling shoes, helmet, sunglasses and nutrition; the red bag may hold bib/race belt, shoes, cap, sunglasses and run nutrition.",
            "The white after-race bag is dropped/retrieved at the Registration Tent; only the designated white bag is accepted.",
            "Food cannot be left overnight, but nutrition can be added to transition bags on race morning.",
        ],
    },
    "course": {
        "pages": [27, 29, 30],
        "keywords": "course route map lap swim bike run hill gravel culvert diversion turnaround distance",
        "facts": [
            "Swim: two clockwise 1 km laps at Locarno Beach with an Aussie exit between laps.",
            "Bike: four laps of the NW Marine/SW Marine out-and-back for 80 km; the lap decision point is Spanish Banks West.",
            "A culvert-construction diversion near Spanish Banks Hill uses covered gravel/seawall ramps and is a no-passing zone.",
            "Run: three laps for 18 km; turn right at the Jericho Sailing Centre roundabout for laps two/three, then left to the finish after lap three.",
        ],
    },
    "cutoffs": {
        "pages": [5, 35],
        "keywords": "cutoff cut-off deadline last lap finish time broom wagon",
        "facts": [
            "100 km cutoffs: first swim lap 07:40, swim finish 08:15, start final bike lap 11:05, bike finish 12:05, start final run lap 14:00, run finish 15:00.",
        ],
    },
    "swim_start": {
        "pages": [36, 43],
        "keywords": "swim wave cap color rolling start wetsuit temperature beach projected time",
        "facts": [
            "The 100 km uses a rolling wave beach start; arrive at swim start at least 20 minutes before the assigned start.",
            "Projected 2 km waves: under 35 min orange; 35-39 green; 40-42 pink; 43-45 purple; 46-59 red; 60+ blue.",
            "For 100 km, wetsuits are not permitted above 24.6 C and are mandatory below 15.9 C.",
        ],
    },
    "bike_aid": {
        "pages": [39],
        "keywords": "fuel fueling fuelling nutrition carb carbohydrate sodium salt hydration hydrate drink bottle gel aid bike maurten water",
        "facts": [
            "100 km bike has one aid station per lap, passed at 8.1, 25.6, 43.0 and 60.3 km.",
            "Bike aid offers water in white bike bottles, Maurten Drink Mix in black bike bottles, and Maurten Energy Gels.",
            "There is no aid station in transition; the guide recommends starting the bike with two full bottles.",
            "Bike hand-up order is Maurten drink mix, Maurten gel, then water; unwanted items may only be discarded inside the litter zone.",
        ],
    },
    "run_aid": {
        "pages": [40],
        "keywords": "fuel fueling fuelling nutrition carb carbohydrate sodium salt hydration hydrate drink cup gel aid run maurten cola food ice water flask",
        "facts": [
            "The 100 km run has five aid stations per lap at 0.2, 1.4, 2.8, 4.0 and 4.8 km; over three laps that is 15 passes.",
            "Run aid offers water, Maurten Drink Mix, cola, Maurten Energy Gels, food and ice.",
            "Run-station order is drink mix, gel, water, food, cola, ice; discard only inside the litter zone.",
            "The guide does not state cup volume, bottle volume, gel variant, or nutrition per serving; do not invent those values.",
        ],
    },
    "rules": {
        "pages": [42, 43],
        "keywords": "rule drafting draft pass penalty card headphones outside assistance bib torso helmet",
        "facts": [
            "The race is non-drafting: keep a 12 m/six-bike-length gap and complete a pass within 25 seconds; keep right and pass left.",
            "100 km drafting penalties are two minutes for first and second offenses, then disqualification.",
            "No headphones; bib must be visible on the run; no outside assistance; helmet stays fastened until the bike is re-racked.",
        ],
    },
    "finish_recovery": {
        "pages": [44, 46, 47, 50],
        "keywords": "finish recovery medal award qualify qualification qatar canada tracking app heat medical cramp dizzy",
        "facts": [
            "Return the timing chip at the finish; hydration, post-race snacks, food trucks and first-come recovery services are available.",
            "Top 10 men/women in each 100 km age group qualify for the 2026 Qatar T100 Worlds; two age-group spots qualify for Canada's 2027 Long Distance Worlds, with pre-race expression of interest required.",
            "The T100 app provides athlete tracking.",
            "For suspected heat exhaustion: stop, find shade, alert a marshal/medical team, cool down, hydrate, rest and wait for medical staff.",
        ],
    },
    "contact": {
        "pages": [52, 66],
        "keywords": "contact email question whatsapp information desk faq update",
        "facts": [
            "Last-minute updates are posted through the Vancouver T100 community WhatsApp channel and event social accounts.",
            "The information desk operates during expo hours; unresolved questions can go to vancouver@t100triathlon.com.",
        ],
    },
}

_RACE_TRIGGER = re.compile(
    r"\b(t100|vancouver|race|aid|transition|check[ -]?in|cut[ -]?off|wave|wetsuit|"
    r"course|shuttle|race pack|bib|athlete guide)\b",
    re.I,
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def context_for(query: str, *, max_sections: int = 4) -> dict[str, Any] | None:
    """Return only Athlete Guide sections relevant to ``query``.

    An explicit request for the whole guide gets the high-signal race-day set;
    normal turns are keyword-routed. Page references let the model distinguish
    guide facts from coaching inference.
    """
    if coaching_contract.EVENT_PROFILE.get("athlete_guide_key") != "vancouver-2026":
        return None
    q = (query or "").lower()
    if not _RACE_TRIGGER.search(q):
        return None
    if (re.search(r"\b(after|post)[ -]?(?:the )?race\b", q)
            and re.search(r"\b(plan|training|rebuild|goal|season|offseason|off-season)\b", q)):
        return None
    words = set(_WORD_RE.findall(q)) - {"race", "vancouver", "t100", "athlete", "guide"}
    named_legs = {leg for leg in ("swim", "bike", "run") if leg in words}
    scored: list[tuple[int, str]] = []
    for name, section in _SECTIONS.items():
        keys = set(_WORD_RE.findall(section["keywords"]))
        score = len(words & keys)
        if name in ("bike_aid", "run_aid") and re.search(
                r"\b(fuel|fuelling|fueling|nutrition|carb|salt|sodium|gel|flask|drink|aid)\b", q):
            leg_is_named = ((name == "bike_aid" and "bike" in words)
                            or (name == "run_aid" and "run" in words))
            no_leg_named = not ({"bike", "run"} & words)
            score += 4 if leg_is_named or no_leg_named else 1
        if name == "bike_aid" and named_legs == {"run"}:
            score = 0
        if name == "run_aid" and named_legs == {"bike"}:
            score = 0
        if name == "transition_bags" and "transition" in words:
            score += 2
        if score:
            scored.append((score, name))

    if re.search(r"\b(entire|whole|full) (?:athlete )?guide\b|\bguide context\b", q):
        preferred = ["race_day", "registration_checkin", "transition_bags", "course",
                     "cutoffs", "bike_aid", "run_aid", "rules"]
    else:
        preferred = [name for _, name in sorted(scored, key=lambda x: (-x[0], x[1]))]
    if not preferred and not re.search(r"\b(t100|vancouver|athlete guide)\b", q):
        return None
    selected = preferred[:max_sections] or ["race_day", "course"]
    result = {
        "source": "2026 Vancouver Athlete Guide (66 pages; supplied by athlete)",
        "authority": ("Separate guide facts from coaching inference. The newer self-reported "
                      "EVENT PROFILE governs the coaching plan where it conflicts; name any material "
                      "conflict. Race-day signage and race-director updates supersede the PDF."),
        "sections": {
            name: {"pages": _SECTIONS[name]["pages"], "facts": _SECTIONS[name]["facts"]}
            for name in selected
        },
    }
    if {"course", "bike_aid"} & set(selected):
        bike = (((coaching_contract.EVENT_PROFILE.get("course_aid") or {}).get("bike")) or {})
        if bike:
            result["known_conflicts"] = [{
                "topic": "bike course topology",
                "guide_fact": "The supplied guide digest describes four bike laps and one aid pass per lap.",
                "newer_event_profile": (f"{bike.get('topology', 'unknown')}; "
                                        f"stations at {bike.get('stations_km', 'unknown')} km; "
                                        f"final {bike.get('final_dry_km', 'unknown')} km dry."),
                "handling": "Use the newer event profile for the plan and explicitly flag the discrepancy.",
            }]
    return result
