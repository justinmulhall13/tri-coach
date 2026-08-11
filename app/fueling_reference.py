"""Shared race-fueling arithmetic and reasoning contract for both AI agents."""
from __future__ import annotations

import re
from typing import Any


# NaCl molecular-weight fraction: 22.989769 / 58.44277.
TABLE_SALT_SODIUM_FRACTION = 0.3934
TABLE_SALT_SODIUM_MG_PER_TSP = 2267

_FUEL_RE = re.compile(
    r"\b(fuel|fuelling|fueling|nutrition|carb|carbohydrate|sugar|syrup|salt|sodium|"
    r"electrolyte|hydrate|hydration|fluid|drink|mix|bottle|flask|gel|gu|maurten|"
    r"caffeine|caffeinated|aid station|bonk|cramp|gi|stomach)\b",
    re.I,
)


def is_fueling_query(text: str) -> bool:
    return bool(_FUEL_RE.search(text or ""))


def sodium_from_salt_mg(salt_mg: float) -> int:
    """Convert milligrams of table salt (NaCl), not label sodium, to sodium."""
    return round(float(salt_mg) * TABLE_SALT_SODIUM_FRACTION)


def sodium_from_salt_tsp(tsp: float) -> int:
    return round(float(tsp) * TABLE_SALT_SODIUM_MG_PER_TSP)


def context() -> dict[str, Any]:
    """Facts and an audit contract injected only for fueling conversations."""
    return {
        "unit_conversions": {
            "table_salt_to_sodium": "table salt is about 39.34% sodium by mass",
            "1000_mg_table_salt": f"about {sodium_from_salt_mg(1000)} mg sodium",
            "1100_mg_table_salt": f"about {sodium_from_salt_mg(1100)} mg sodium",
            "1_tsp_table_salt": f"about {sodium_from_salt_tsp(1)} mg sodium",
            "half_tsp_table_salt": f"about {sodium_from_salt_tsp(0.5)} mg sodium",
            "critical_ambiguity": ("'1000 mg salt' and '1000 mg sodium' are not interchangeable. "
                                   "If the wording or label is unclear, ask before prescribing."),
        },
        "evidence_based_ranges": {
            "carbohydrate": ("For endurance exercise 1-2.5 h, use 30-60 g/h as a starting range; "
                             "for 2.5-3+ h, up to 90 g/h can be used when practiced with multiple-transportable carbohydrates."),
            "fluid": ("Do not force a generic 1 L/h. Base intake on practiced thirst, weather, aid logistics, "
                      "pre/post body-mass change and measured sweat rate; avoid drinking beyond sweat losses."),
            "sodium": ("There is no universal performance dose. Base sodium on fluid plan, known sweat sodium/salty-sweater evidence, "
                       "weather and tolerance; cramps alone do not prove sodium deficiency. Without those data, report the calculated rate "
                       "and uncertainty but do not label it low, adequate or high."),
            "drink_concentration": "A conventional sports-drink starting range is 4-8 g carbohydrate per 100 mL, but total effective fluid matters.",
            "concentrate_rule": ("A small concentrated flask sipped with separate water is a delivery system, not a standalone drink. "
                                 "Judge each dose with the water consumed around it and the total hourly carbohydrate/fluid/sodium."),
        },
        "known_product_labels": {
            "Maurten Drink Mix 160": "40 g carbohydrate per prepared serving",
            "Maurten Gel 100": "25 g carbohydrate",
            "Maurten Gel 100 Caf 100": "25 g carbohydrate plus 100 mg caffeine",
            "warning": ("The Athlete Guide does not state on-course cup/bottle volume or exact gel variant. "
                        "Do not assign nutrition to a hand-up unless the serving/label is confirmed."),
        },
        "fuel_audit_contract": [
            "Resolve scope first: training session versus race, then swim/bike/run and expected leg duration.",
            "Inventory every source separately; preserve the athlete's stated label values instead of substituting generic tablespoon estimates.",
            "Show leg totals and rates: carbohydrate g total and g/h; sodium mg total and mg/h; fluid mL total and mL/h; caffeine mg total and mg/kg.",
            "Distinguish table-salt mass from sodium mass before any electrolyte calculation.",
            "Use aid-station spacing and actual available products; never invent cup volume, bottle volume or product variant.",
            "Aid stations are opportunities, not automatic doses. Compute how many gels/sips the leg needs, then place only that many; the final schedule must exactly match the displayed totals and hourly rates.",
            "A correction from the athlete replaces the prior assumption. Recalculate from raw inputs rather than defending the previous answer.",
            "Use explicit equivalences from earlier turns (for example, the athlete's six tablespoons = 80 g carbohydrate implies three = 40 g) before reaching for generic food-table estimates.",
            "If one missing value can flip the verdict, ask one focused clarifying question and stop; do not manufacture precision.",
            "Do not diagnose cramping as sodium deficiency, and do not call a concentrate unsafe solely from flask concentration when it is chased with water.",
            "Front-load carbohydrate on the bike when practical, but an 18 km run after 80 km cycling still needs a deliberate, practiced run plan.",
            "Count caffeine from every source and use only a previously tolerated race-day dose; product labels, not product type, determine caffeine content.",
        ],
        "sources": [
            "AIS Sports Nutrition sports-drink/electrolyte guidance",
            "World Athletics 2019 Nutrition for Athletics consensus",
            "Maurten official T100 and product fueling guides",
        ],
    }
