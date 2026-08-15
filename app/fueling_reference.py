"""Shared race-fueling arithmetic and reasoning contract for both AI agents."""
from __future__ import annotations

import re
from typing import Any

from . import coaching_contract


# Athlete-supplied conversions. These values are intentionally not re-derived
# from food tables or molecular weight because the athlete explicitly asked the
# agents to use this arithmetic consistently.
TABLE_SALT_SODIUM_FRACTION = 0.39
TABLE_SALT_G_PER_TSP = 6.0
TABLE_SALT_SODIUM_MG_PER_TSP = 2360
SUGAR_CARB_G_PER_TBSP = 12.5
MAPLE_CARB_G_PER_TBSP = 13.0
MAPLE_GLUCOSE_FRACTION = 0.5
MAPLE_FRUCTOSE_FRACTION = 0.5
GEL_CARB_G = 23.0
GEL_CAFFEINE_MG = 20.0
GLUCOSE_CEILING_G_PER_H = 60.0
FRUCTOSE_CEILING_G_PER_H = 30.0
SODIUM_MG_PER_L = (800, 900)
DRINK_CARB_MASS_FRACTION = (0.06, 0.08)

_FUEL_RE = re.compile(
    r"\b(fuel|fuelling|fueling|nutrition|carb|carbohydrate|sugar|syrup|salt|sodium|"
    r"electrolyte|hydrate|hydration|fluid|drink|mix|bottle|flask|gel|gu|maurten|"
    r"caffeine|caffeinated|aid station|bonk|cramp|gi|stomach)\b",
    re.I,
)
_SPORT_FOOD_RE = re.compile(r"\b(eat|eating|food|snack|chew|carry)\b", re.I)
_SPORT_CONTEXT_RE = re.compile(
    r"\b(bike|ride|run|swim|race|workout|training|session|course|aid|during|before|after|carry)\b",
    re.I,
)


def is_fueling_query(text: str) -> bool:
    value = text or ""
    return bool(
        _FUEL_RE.search(value)
        or (_SPORT_FOOD_RE.search(value) and _SPORT_CONTEXT_RE.search(value))
    )


def sodium_from_salt_mg(salt_mg: float) -> int:
    """Convert milligrams of table salt (NaCl), not label sodium, to sodium."""
    return round(float(salt_mg) * TABLE_SALT_SODIUM_FRACTION)


def sodium_from_salt_tsp(tsp: float) -> int:
    return round(float(tsp) * TABLE_SALT_SODIUM_MG_PER_TSP)


def carb_from_sugar_tbsp(tbsp: float) -> float:
    return round(float(tbsp) * SUGAR_CARB_G_PER_TBSP, 2)


def maple_from_tbsp(tbsp: float) -> dict[str, float]:
    total = float(tbsp) * MAPLE_CARB_G_PER_TBSP
    return {
        "total_carb_g": round(total, 2),
        "glucose_g": round(total * MAPLE_GLUCOSE_FRACTION, 2),
        "fructose_g": round(total * MAPLE_FRUCTOSE_FRACTION, 2),
    }


def gel_totals(count: float) -> dict[str, float]:
    return {
        "carb_g": round(float(count) * GEL_CARB_G, 2),
        "caffeine_mg": round(float(count) * GEL_CAFFEINE_MG, 2),
    }


def _active_event_has_bike_leg() -> bool:
    distances = coaching_contract.EVENT_PROFILE.get("disciplines_and_distances") or {}
    if not isinstance(distances, dict):
        return False
    raw = distances.get("bike_km")
    if isinstance(raw, bool):
        return False
    try:
        return float(raw) > 0
    except (TypeError, ValueError):
        return False


def _active_event_is_t100_vancouver() -> bool:
    return str(coaching_contract.EVENT_PROFILE.get("id") or "") == "t100-vancouver-2026"


def context() -> dict[str, Any]:
    """Facts and an audit contract injected only for fueling conversations."""
    if _active_event_has_bike_leg():
        placement_rule = ("For this event, put carbohydrate fuel on the bike rather than the run; "
                          "run aid is for the non-carbohydrate hydration/cooling plan. This universal "
                          "rule does not change with conversational corrections.")
    else:
        placement_rule = ("The active event profile has no stated bike leg. Front-load carbohydrate "
                          "early and expect the final third to be tolerance-limited; do not import "
                          "bike-versus-run placement from another event.")
    scope_rule = ("Resolve scope first: training session versus race, then swim/bike/run and expected "
                  "leg duration." if _active_event_is_t100_vancouver() else
                  "Resolve scope first: training session versus race, then the active event discipline "
                  "and expected duration. Do not import disciplines from another event profile.")
    sources = [
        "AIS Sports Nutrition sports-drink/electrolyte guidance",
        "World Athletics 2019 Nutrition for Athletics consensus",
        "Maurten official product fueling guides",
    ]
    if _active_event_is_t100_vancouver():
        sources.append("Maurten official T100 fueling guide")
    product_warning = (
        "The Athlete Guide does not state on-course cup/bottle volume or exact gel variant. "
        "Do not assign nutrition to a hand-up unless the serving/label is confirmed."
        if _active_event_is_t100_vancouver() else
        "Use only the active event profile's confirmed aid products and serving sizes; "
        "do not import hand-up details from another event."
    )
    return {
        "unit_conversions": {
            "provenance": "self-reported fixed conversions",
            "table_salt_to_sodium": "table salt x 0.39 = sodium by mass",
            "1000_mg_table_salt": f"about {sodium_from_salt_mg(1000)} mg sodium",
            "1100_mg_table_salt": f"about {sodium_from_salt_mg(1100)} mg sodium",
            "1_tsp_table_salt": (f"1 tsp x {TABLE_SALT_G_PER_TSP:g} g/tsp = "
                                     f"{TABLE_SALT_G_PER_TSP:g} g salt = about "
                                     f"{sodium_from_salt_tsp(1):,} mg sodium"),
            "half_tsp_table_salt": (f"0.5 tsp x {TABLE_SALT_SODIUM_MG_PER_TSP:,} mg/tsp = "
                                        f"{sodium_from_salt_tsp(0.5):,} mg sodium"),
            "granulated_sugar": (f"1 tbsp x {SUGAR_CARB_G_PER_TBSP:g} g carb/tbsp = "
                                     f"{carb_from_sugar_tbsp(1):g} g carbohydrate"),
            "maple_syrup": (f"1 tbsp x {MAPLE_CARB_G_PER_TBSP:g} g carb/tbsp = "
                                f"{MAPLE_CARB_G_PER_TBSP:g} g total = 6.5 g glucose + 6.5 g fructose"),
            "gel": (f"1 gel = {GEL_CARB_G:g} g carbohydrate + "
                       f"{GEL_CAFFEINE_MG:g} mg caffeine"),
            "carb_powder": "100% glucose; fructose factor = 0",
            "critical_ambiguity": ("'1000 mg salt' and '1000 mg sodium' are not interchangeable. "
                                   "If the wording or label is unclear, ask before prescribing."),
        },
        "hard_limits": {
            "glucose": f"maximum {GLUCOSE_CEILING_G_PER_H:g} g/h",
            "fructose": f"maximum {FRUCTOSE_CEILING_G_PER_H:g} g/h",
            "usable_total_equation": "min(glucose, 60) + min(fructose, 30)",
            "above_60_g_per_h": "roughly 2:1 glucose to fructose is mandatory",
            "duration_bands": "under 60 min: none; 60-150 min: 30-60 g/h; over 150 min: 70-90 g/h",
            "sodium": "800-900 mg per litre of fluid",
            "drink_concentration": "6-8% carbohydrate by mass of the finished drink",
            "magnesium": "do not stack magnesium",
        },
        "known_product_labels": {
            "Maurten Drink Mix 160": "40 g carbohydrate per prepared serving",
            "Maurten Gel 100": "25 g carbohydrate",
            "Maurten Gel 100 Caf 100": "25 g carbohydrate plus 100 mg caffeine",
            "warning": product_warning,
        },
        "fuel_audit_contract": [
            scope_rule,
            "Inventory every source separately; preserve the athlete's stated label values instead of substituting generic tablespoon estimates.",
            "Show leg totals and rates: carbohydrate g total and g/h; sodium mg total and mg/h; fluid mL total and mL/h; caffeine mg total and mg/kg.",
            "Show glucose g/h separately from usable total carbohydrate g/h, using min(glucose,60) + min(fructose,30).",
            "Label each input measured, self-reported, or assumed, and show every conversion factor.",
            "Distinguish table-salt mass from sodium mass before any electrolyte calculation.",
            "Use aid-station spacing and actual available products; never invent cup volume, bottle volume or product variant.",
            "Aid stations are opportunities, not automatic doses. Compute how many gels/sips the leg needs, then place only that many; the final schedule must exactly match the displayed totals and hourly rates.",
            "A correction from the athlete replaces the prior assumption. Recalculate from raw inputs rather than defending the previous answer.",
            "Use only the newest exact ingredient label or the fixed ingredient-specific tablespoon factors in this contract; never carry an older unnamed tablespoon equivalence into a new calculation.",
            "If one missing value can flip the verdict, ask one focused clarifying question and stop; do not manufacture precision.",
            "Do not diagnose cramping as sodium deficiency, and do not call a concentrate unsafe solely from flask concentration when it is chased with water.",
            placement_rule,
            "Count caffeine from every source and use only a previously tolerated race-day dose; product labels, not product type, determine caffeine content.",
            "Every race and long-session plan includes an abort protocol for GI symptoms.",
        ],
        "sources": sources,
    }
