"""Chef Gordo — training-aware nutrition & fueling agent.

Goal: keep the athlete maximally FUELLED for training (not weight loss). Reads the
same training context Coach Steve uses — today's + upcoming sessions, the
constraints the athlete logs ("training at 6am"), readiness, and completed work —
plus what's been eaten today, then:

  • sets loose daily macro/calorie targets scaled to the day's training load
  • plans intra-workout fueling (carbs/hr, sodium/hr) for the day's session
  • suggests meals with example foods + portions, timed around training
  • answers "can I eat X?" with a portion the day can absorb
  • parses free-text food logs ("chicken and rice at 1pm") into macros

Targets are ranges, not hard numbers — the athlete tracks fully but eats to feel
energized, not to hit a target on the nose. Nothing fabricates Garmin data.
"""
from __future__ import annotations

import datetime
import json
import math
import re
from typing import Any

from . import (athlete_guide, coaching_contract, config, db, fueling_reference,
               garmin_source, suggest)

def weight_info() -> dict[str, Any]:
    """Use the latest dated self-reported Garmin entry, else the 86 kg fallback."""
    w = garmin_source.get_weight_kg()
    if w and w.get("kg"):
        return w
    return {"kg": config.ATHLETE_WEIGHT_KG, "lb": round(config.ATHLETE_WEIGHT_KG * 2.2046, 1),
            "as_of": None, "source": "self-reported", "provider": None,
            "fallback_reason": "Latest dated Garmin weight entry is unknown",
            "conversion": f"{config.ATHLETE_WEIGHT_KG:g} kg x 2.2046 lb/kg = "
                          f"{round(config.ATHLETE_WEIGHT_KG * 2.2046, 1):g} lb"}


def weight_kg() -> float:
    return float(weight_info()["kg"])


_SYSTEM = coaching_contract.system_prompt() + """

You are Chef Gordo, a high-performance sports chef and fueling coach for ONE \
endurance athlete (bodyweight is in the context block) training for the installed event. You work hand-in-hand \
with their training coach ("Coach Steve") — the context block carries Steve's training picture \
(today's and the week's sessions, when they train, readiness, completed work) plus what they've \
eaten today and their loose macro targets.

Your mission: keep them fuelled to train hard and feel energized. Fuel the work FIRST. \
The athlete sets a calorie goal (context `targets.goal`): on "maintain" or "surplus" never \
restrict for leanness. If they have chosen "deficit", respect it — but take the deficit from \
fat and non-training carbs, protect protein and the carbs around sessions, and say plainly \
when a deficit would compromise a key session.

Hard rules:
- Be specific and practical, but never estimate a food's portion, calories, or macros. Use a numeric \
food value only when the context includes the athlete's measured recipe or exact serving label. \
Otherwise name a simple food option, label its amount/macros unknown, and ask for the label or \
measured recipe when the number affects the decision. The athlete eats meat + rice/potato staples.
- TIME food around training. If they train early AM, pre-session should be small, carb-based and \
easily digestible (banana, toast+honey, small oats); the big carb refuel comes AFTER. If they \
train after work, keep lunch moderate and load carbs around the session. Two-a-days need fuel \
between sessions. Use the actual session times/notes in the context.
- Respect the day's load: long/hard days = more carbs; rest/easy days = fewer carbs, hold protein.
- Targets are LOOSE ranges — guide toward them, don't nag about exact numbers. If they're under \
on carbs before a big session, say so plainly.
- When they ask "can I eat X", answer YES/NO and what to pair it with. State how much only from an \
exact supplied label/recipe; otherwise say the fitting portion is unknown and ask.
INTRA-SESSION FUELLING — HARD CONSTRAINTS (this athlete had GI failure from
overfuelling glucose on 2026-08-03; treat gut tolerance as a limit, not a preference):
- Glucose absorbs via SGLT1 and caps near 60 g/h. Fructose uses GLUT5, worth ~30 g/h more.
Usable total = min(glucose,60) + min(fructose,30). Never prescribe more than 60 g/h glucose
regardless of the carb target. Above 60 g/h total the mix must be roughly 2:1 glucose:fructose.
- Maltodextrin, rice maltodextrin, waxy maize, cyclic dextrin, dextrose and glucose syrup are
ALL pure glucose — a product listing only these has ZERO fructose. Sucrose and maple syrup are
50/50. Honey composition is unknown without the exact product label. Their carb powder is 100%
glucose: assume zero fructose from it.
- Rate by duration: under 60 min none (water only); 60-150 min 30-60 g/h; over 150 min 70-90 g/h
with the 2:1 split mandatory. ALWAYS state the glucose rate separately from the total.
- The self-reported sweat rate is roughly 1 L/h. Use 800-900 mg sodium per litre, label both inputs
self-reported, and do not add magnesium. Do not diagnose cramps as sodium deficiency.
- TABLE SALT and SODIUM are different units. Use the supplied factors exactly: table salt x 0.39
= sodium by mass; 1 tsp table salt = about 6 g salt = about 2,360 mg sodium. If wording is
ambiguous, ask before calculating. Evaluate a concentrate with the water taken alongside it.
- The athlete's gels are 23 g carbohydrate and 20 mg caffeine each. Count every source in mg and
mg/kg using the latest dated self-reported Garmin weight entry, or the explicitly labelled 86 kg fallback.
- Never introduce a new product, mix, exercise, or rate on race day. For an event with a bike leg,
put carbohydrate fuel on the bike rather than the run.
- For every fueling audit, resolve training versus race and the exact leg/duration first. Inventory
each bottle, flask, gel and aid-station serving separately; preserve user-supplied label values;
then SHOW totals and rates (carb g/h, sodium mg/h, fluid mL/h, caffeine mg and mg/kg). If a missing
serving size or "salt versus sodium" ambiguity can flip the verdict, ask one focused question.
- Use the active profile's injected athlete-guide context, when present, for aid locations/products;
use `fueling_reference` for arithmetic. Never import Vancouver details into another profile or invent
the cup/bottle volume or exact product variant when the active guide does not state it.
- Aid stations are opportunities, not automatic doses. Calculate the units needed over the leg,
then place only that many; the station schedule must reconcile with the displayed totals/rates.
- The athlete's newest correction replaces the prior assumption. Recalculate from the original
quantities and scope; do not repeat or defend the discarded answer.
- OUTPUT FORMAT: never a wall of text. Lead with a one-line "TL;DR:" then 2–5 short \
"• Label: one line" bullets (labels like Now, Pre, Post, Portion, Macros, Timing, Verdict). \
Fueling audits may use up to 8 concise bullets so every equation remains checkable. \
Plain text only: no markdown bold/asterisks, no emoji, no filler, no em dashes."""

_SYSTEM = _SYSTEM.replace(" — ", ": ").replace("—", "-")


_TRANSIENT = ("RemoteProtocolError", "APIConnectionError", "ConnectionError",
              "ReadTimeout", "APITimeoutError", "InternalServerError", "OverloadedError")


def _sanitize_visible_reply(text: str) -> str:
    return (text or "").replace(" — ", ": ").replace("—", "-")


def _reply(messages: list[dict[str, Any]], max_tokens: int = 1200, system: str = _SYSTEM) -> Any:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=2)
    last: Exception | None = None
    for _ in range(3):
        try:
            with client.messages.stream(model=config.COACH_MODEL, max_tokens=max_tokens,
                                        system=system, messages=messages) as stream:
                return stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            last = e
            if type(e).__name__ in _TRANSIENT:
                continue
            raise
    raise last  # type: ignore[misc]


# --- Fueling math -------------------------------------------------------------
def _today_session() -> dict[str, Any]:
    """Today's session, including a derived bike duration for stored bricks."""
    def normalized(row: dict[str, Any]) -> dict[str, Any]:
        structure = row.get("structure") or {}
        out = {
            "discipline": row.get("discipline"), "duration_min": row.get("duration_min"),
            "intensity": row.get("intensity"), "title": row.get("title"),
            "is_rest": bool(row.get("is_rest") or (row.get("discipline") == "rest")),
            "structure": structure,
            # A stored plan is a coaching prescription, not an observation or
            # an athlete-entered measurement. An API caller can override this
            # only by supplying an explicit provenance label with its duration.
            "duration_source": row.get("duration_source") or "assumed coaching prescription",
        }
        if (row.get("discipline") or "").lower() == "brick":
            run_text = str(structure.get("run") or "")
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|minute)", run_text, re.I)
            total = row.get("duration_min")
            if match and isinstance(total, (int, float)):
                run_min = float(match.group(1))
                if 0 < run_min < float(total):
                    bike_min = round(float(total) - run_min, 2)
                    out["bike_duration_min"] = int(bike_min) if bike_min.is_integer() else bike_min
                    out["bike_duration_source"] = (
                        f"derived from {out['duration_source']}"
                    )
                    out["bike_duration_arithmetic"] = (
                        f"{float(total):g} min total - {run_min:g} min run = {bike_min:g} min bike"
                    )
        return out

    try:
        s = suggest.todays_suggestion()
        w = (s.get("suggestion") or {}) if isinstance(s, dict) else {}
        if w:
            return normalized(w)
    except Exception:
        pass
    d = db.get_plan_day(config.local_today().isoformat()) or {}
    return normalized(d)


def _is_hard(intensity: str, title: str = "") -> bool:
    return bool(re.search(r"threshold|vo2|race|tempo|interval|css|surge|brick", f"{intensity} {title}", re.I))


# Calorie goal offsets. Carbs/protein stay training-driven; the goal shifts total
# energy (fat carries most of the swing) so fuelling for the session is preserved.
_GOAL_KCAL = {"deficit": -550, "maintain": 0, "surplus": 350}


def get_goal() -> str:
    g = db.get_meta("nutrition_goal") or "maintain"
    return g if g in _GOAL_KCAL else "maintain"


def set_goal(goal: str) -> str:
    goal = (goal or "maintain").lower()
    if goal not in _GOAL_KCAL:
        goal = "maintain"
    db.set_meta("nutrition_goal", goal)
    return goal


def daily_targets(session: dict[str, Any], completed_min: float = 0.0) -> dict[str, Any]:
    """Loose daily macro/calorie targets scaled to the day's training demand,
    then shifted by the athlete's calorie goal (deficit / maintain / surplus)."""
    dur = (session.get("duration_min") or 0) + completed_min
    is_rest = bool(session.get("is_rest")) and completed_min < 20
    hard = _is_hard(session.get("intensity") or "", session.get("title") or "")
    long_day = dur >= 150 or (session.get("discipline") == "brick")

    if is_rest:
        cpk, basis = 4.0, "rest/recovery day — hold protein, ease carbs"
    elif long_day:
        cpk, basis = 8.5, "long/endurance day — load carbs hard"
    elif hard or dur >= 90:
        cpk, basis = 6.5, "quality session — solid carbs to hit intensity"
    else:
        cpk, basis = 5.5, "moderate day — steady carbs"

    w = weight_kg()
    carb = round(w * cpk)
    protein = round(w * 1.8)
    fat = round(w * 1.0)
    goal = get_goal()
    delta = _GOAL_KCAL[goal]
    if delta:
        # Absorb the shift in fat first (min 0.5 g/kg), then trim carbs if needed —
        # protein is protected so training quality and recovery aren't compromised.
        fat_floor = round(w * 0.5)
        fat = max(fat_floor, fat + round(delta / 9))
        spent = (fat - round(w * 1.0)) * 9
        rest = delta - spent
        if rest:
            carb = max(round(w * 3.0), carb + round(rest / 4))
    kcal = carb * 4 + protein * 4 + fat * 9

    def rng(x, pct=0.12):
        return [int(round(x * (1 - pct))), int(round(x * (1 + pct)))]

    return {
        "kcal": kcal, "carb_g": carb, "protein_g": protein, "fat_g": fat,
        "kcal_range": rng(kcal), "carb_range": rng(carb), "protein_range": rng(protein, 0.1),
        "carb_per_kg": cpk, "protein_per_kg": 1.8,
        "goal": goal,
        "basis": basis + ("" if goal == "maintain" else f" · {goal}"),
        "provenance": ("assumed coaching targets derived from self-reported body mass and "
                       "training classification; not measured intake requirements"),
    }


# ── Intestinal transport ceilings (the constraint that governs everything) ─────
# Glucose crosses via SGLT1 and saturates near 60 g/h. Fructose uses GLUT5, worth
# roughly another 30 g/h. Anything beyond those caps stays in the gut, pulls water
# in osmotically and causes urgency and cramping. This athlete has a documented GI
# failure from overfuelling glucose (2026-08-03), so these are hard limits.
GLUCOSE_CEILING_G_H = int(fueling_reference.GLUCOSE_CEILING_G_PER_H)
FRUCTOSE_CEILING_G_H = int(fueling_reference.FRUCTOSE_CEILING_G_PER_H)
MAX_CARB_G_H = GLUCOSE_CEILING_G_H + FRUCTOSE_CEILING_G_H     # 90
DRINK_CONC_MIN, DRINK_CONC_MAX = fueling_reference.DRINK_CARB_MASS_FRACTION
SWEAT_L_H = float(coaching_contract.ATHLETE_CONSTANTS["sweat_profile"]["sweat_rate_l_per_h"])
SODIUM_MG_PER_L = fueling_reference.SODIUM_MG_PER_L
TSP_SALT_MG_SODIUM = float(fueling_reference.TABLE_SALT_SODIUM_MG_PER_TSP)


def _salt_tsp(mg: float) -> str:
    """Table salt measure for a sodium dose, in eighths of a teaspoon."""
    eighths = max(1, round((mg / TSP_SALT_MG_SODIUM) * 8))
    whole, rem = divmod(eighths, 8)
    frac = {0: "", 1: "1/8", 2: "1/4", 3: "3/8", 4: "1/2", 5: "5/8", 6: "3/4", 7: "7/8"}[rem]
    if whole and frac:
        return f"{whole} {frac} tsp"
    return f"{whole} tsp" if whole else f"{frac} tsp"


def fueling_plan(session: dict[str, Any]) -> dict[str, Any]:
    """Intra-session fuelling built from the transport ceilings, with the
    arithmetic exposed so it can be checked."""
    raw_duration = session.get("duration_min")
    try:
        dur = float(raw_duration) if not isinstance(raw_duration, bool) else None
    except (TypeError, ValueError):
        dur = None
    if dur is not None and (not math.isfinite(dur) or dur <= 0):
        dur = None
    disc = (session.get("discipline") or "").lower()

    if session.get("is_rest") or disc == "rest":
        return {"needed": False, "note": "Rest day. No intra-session fuelling."}
    if disc in {"race", "triathlon"} and config.event_has_leg("bike"):
        return {
            "needed": True,
            "requires_input": True,
            "note": ("Race bike-leg duration is unknown. Ask Coach with the expected bike duration "
                     "and confirmed carried bottle volumes before calculating totals; do not apply "
                     "the whole-race duration to a bike-only carbohydrate plan."),
            "known_inputs": {
                "event_duration_min": ((coaching_contract.EVENT_PROFILE.get("goal") or {})
                                       .get("modelled_duration_min")),
                "event_duration_source": "self-reported",
                "bike_duration_min": None,
                "bike_duration_source": "unknown",
            },
        }
    if disc == "brick":
        if session.get("duration_scope") == "bike_leg":
            bike_dur = dur
            bike_source = session.get("duration_source") or "assumed coaching prescription"
            bike_arithmetic = None
        else:
            bike_dur = session.get("bike_duration_min")
            bike_source = session.get("bike_duration_source") or "unknown"
            bike_arithmetic = session.get("bike_duration_arithmetic")
            if not isinstance(bike_dur, (int, float)):
                run_text = str((session.get("structure") or {}).get("run") or "")
                match = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|minute)", run_text, re.I)
                if match and isinstance(dur, (int, float)):
                    run_min = float(match.group(1))
                    if 0 < run_min < float(dur):
                        bike_dur = round(float(dur) - run_min, 2)
                        total_source = (
                            session.get("duration_source") or "assumed coaching prescription"
                        )
                        bike_source = f"derived from {total_source}"
                        bike_arithmetic = (
                            f"{float(dur):g} min total - {run_min:g} min run = "
                            f"{float(bike_dur):g} min bike"
                        )
        if (not isinstance(bike_dur, (int, float))
                or not math.isfinite(float(bike_dur)) or bike_dur <= 0):
            return {
                "needed": True,
                "requires_input": True,
                "note": ("Brick bike-leg duration is unknown. Give the bike duration separately; "
                         "the total brick duration cannot be used because carbohydrate belongs on "
                         "the bike, not the run."),
                "known_inputs": {
                    "total_brick_duration_min": dur or None,
                    "total_brick_duration_source": (
                        session.get("duration_source") or "assumed coaching prescription"
                    ) if dur else "unknown",
                    "bike_duration_min": None,
                    "bike_duration_source": "unknown",
                },
            }
        dur = bike_dur
    else:
        bike_source = None
        bike_arithmetic = None

    if not isinstance(dur, (int, float)) or not math.isfinite(float(dur)) or dur <= 0:
        return {
            "needed": True,
            "requires_input": True,
            "note": "Session duration is unknown. Give the duration in minutes before calculating fuel.",
            "known_inputs": {"duration_min": None, "duration_source": "unknown"},
        }

    hours = dur / 60.0
    if dur < 60:
        duration_source = (
            bike_source if disc == "brick"
            else session.get("duration_source") or "assumed coaching prescription"
        )
        return {"needed": False, "duration_min": dur,
                "duration_source": duration_source,
                "note": "Under 60 min: water only, no carbohydrate needed. "
                        "Start topped up rather than fuelling during."}

    # Rate band by duration.
    if hours <= 2.5:
        lo, hi = 30, 60
        band = "60-150 min: 30-60 g/h. Composition matters little at this rate."
        ratio_required = False
    else:
        lo, hi = 70, 90
        band = "Over 150 min: 70-90 g/h, and the 2:1 glucose:fructose split is mandatory."
        ratio_required = True

    # A practical 75 g/h lands exactly at 2:1 using 25 g glucose powder plus
    # 50 g granulated sugar (25 g glucose + 25 g fructose). It stays inside the
    # requested 70-90 g/h band.
    target = 75 if ratio_required else min(hi, MAX_CARB_G_H)

    # Split. Above 60 g/h total, glucose alone cannot carry it — 2:1 keeps both
    # transporters under their ceiling (at 90 g/h that is exactly 60 + 30).
    if target > GLUCOSE_CEILING_G_H or ratio_required:
        glucose = round(target * 2 / 3)
        fructose = target - glucose
    else:
        glucose, fructose = target, 0
    glucose = min(glucose, GLUCOSE_CEILING_G_H)
    fructose = min(fructose, FRUCTOSE_CEILING_G_H)
    total = glucose + fructose

    # Mass and volume remain separate. The carbohydrate drink is deliberately
    # prescribed to a scale-verified finished mass of 1,000 g, which makes the
    # 60 or 75 g carbohydrate concentration exactly checkable by mass. Its volume
    # is unknown until measured. Plain water then brings total measured fluid to
    # the self-reported 1 L/h target; no 1 g/mL density assumption is allowed.
    fluid_ml = int(SWEAT_L_H * 1000)
    finished_drink_mass_g = 1000
    in_drink = total
    out_of_drink = 0
    conc_pct = round(in_drink / finished_drink_mass_g * 100, 1)

    raw_drink_volume = session.get("finished_drink_volume_ml")
    try:
        carb_drink_volume_ml = (round(float(raw_drink_volume))
                                if raw_drink_volume is not None else None)
    except (TypeError, ValueError):
        carb_drink_volume_ml = None
    volume_valid = bool(carb_drink_volume_ml and 0 < carb_drink_volume_ml <= fluid_ml)
    plain_water_ml = fluid_ml - carb_drink_volume_ml if volume_valid else None
    requires_volume = not volume_valid
    if carb_drink_volume_ml and carb_drink_volume_ml > fluid_ml:
        volume_note = (
            f"Measured carbohydrate-drink volume {carb_drink_volume_ml} ml exceeds the "
            f"{fluid_ml} ml/h fluid target. The exact fluid plan is unresolved; ask Coach "
            "before using it."
        )
    else:
        volume_note = (
            "Finished carbohydrate-drink volume is unknown. Weigh the finished drink to "
            f"exactly {finished_drink_mass_g} g, then measure its volume in ml and tell Coach. "
            f"The separate plain-water amount needed to reach {fluid_ml} ml/h cannot be "
            "calculated until then."
        )

    na_lo, na_hi = SODIUM_MG_PER_L
    na_lo, na_hi = int(na_lo * SWEAT_L_H), int(na_hi * SWEAT_L_H)
    # 3/8 tsp is a real kitchen measure inside the target band:
    # 0.375 tsp x 2,360 mg sodium/tsp = 885 mg sodium.
    salt_tsp = 3 / 8
    sodium_from_recipe = fueling_reference.sodium_from_salt_tsp(salt_tsp)

    # Sucrose is the practical fructose source (50/50 glucose/fructose): each gram
    # of sucrose supplies 0.5 g fructose, so cover the fructose need with 2x sucrose,
    # then make up the remaining glucose from the pure-glucose powder.
    sucrose_g = round(fructose * 2)
    glucose_from_sucrose = sucrose_g / 2
    powder_g = max(0, round(glucose - glucose_from_sucrose))
    drink_powder = powder_g
    drink_sucrose = sucrose_g

    is_swim = disc in ("swim", "pool_swim", "open_water_swim")
    sugar_tbsp = round(sucrose_g / fueling_reference.SUGAR_CARB_G_PER_TBSP, 2)

    plan = {
        "needed": True,
        "requires_input": requires_volume,
        "note": volume_note if requires_volume else None,
        "duration_min": dur, "hours": round(hours, 2), "discipline": disc,
        "duration_scope": "bike_leg" if disc == "brick" else "session",
        "duration_source": (
            bike_source if disc == "brick"
            else session.get("duration_source") or "assumed coaching prescription"
        ),
        "duration_arithmetic": bike_arithmetic,
        "band": band,
        "carb_g_per_hr": total,
        "glucose_g_per_hr": glucose,
        "fructose_g_per_hr": fructose,
        "glucose_ceiling": GLUCOSE_CEILING_G_H,
        "fructose_ceiling": FRUCTOSE_CEILING_G_H,
        "ratio": f"{round(glucose / fructose, 1)}:1" if fructose else "glucose only",
        "fluid_ml_per_hr": [fluid_ml, fluid_ml],
        "sodium_mg_per_hr": [na_lo, na_hi],
        "salt_per_hr": _salt_tsp(sodium_from_recipe),
        "recipe_sodium_mg_per_hr": sodium_from_recipe,
        "drink_carb_g": in_drink, "drink_conc_pct": conc_pct,
        "finished_drink_mass_g": finished_drink_mass_g,
        "finished_drink_mass_source": "assumed coaching prescription; requires scale verification",
        "finished_drink_volume_ml": carb_drink_volume_ml,
        "finished_drink_volume_source": (
            session.get("finished_drink_volume_source") or "self-reported"
            if carb_drink_volume_ml is not None else "unknown"
        ),
        "plain_water_ml_per_hr": plain_water_ml,
        "carb_outside_drink_g": out_of_drink,
        "total_carb_g": [round(total * hours), round(total * hours)],
        "total_fluid_ml": [int(fluid_ml * hours), int(fluid_ml * hours)],
        "total_sodium_mg": [int(na_lo * hours), int(na_hi * hours)],
        "recipe": (
            f"{'Poolside per hour' if is_swim else 'Per hour'}: {drink_powder} g glucose "
            f"powder + {drink_sucrose} g table sugar "
            f"({round(drink_sucrose / fueling_reference.SUGAR_CARB_G_PER_TBSP, 2):g} tbsp) + "
            f"{_salt_tsp(sodium_from_recipe)} table salt. Add water until the finished "
            f"carbohydrate drink weighs exactly {finished_drink_mass_g} g on a scale "
            f"({conc_pct}% carbohydrate by mass). "
            + (f"Its measured volume is {carb_drink_volume_ml} ml; take {plain_water_ml} ml "
               f"separate plain water so combined measured fluid is {fluid_ml} ml/h."
               if volume_valid else volume_note)
        ),
        "arithmetic": [
            f"Usable total: min({glucose}, 60) + min({fructose}, 30) = {total} g/h",
            f"Glucose: {glucose} g/h; ceiling factor = {GLUCOSE_CEILING_G_H} g/h",
            f"Fructose: {fructose} g/h; ceiling factor = {FRUCTOSE_CEILING_G_H} g/h",
            f"Sugar: {sucrose_g} g / {fueling_reference.SUGAR_CARB_G_PER_TBSP:g} g/tbsp = {sugar_tbsp:g} tbsp/h",
            f"Sodium: {salt_tsp:g} tsp x {int(TSP_SALT_MG_SODIUM):,} mg/tsp = {sodium_from_recipe} mg; {sodium_from_recipe} mg / 1 L combined fluid = {sodium_from_recipe} mg/L",
            f"Fluid target: {SWEAT_L_H:g} L/h x 1,000 ml/L = {fluid_ml} ml/h",
            f"Drink concentration: {in_drink} g / {finished_drink_mass_g} g x 100 = {conc_pct}% by mass",
            (f"Plain water: {fluid_ml} ml/h - {carb_drink_volume_ml} ml/h = {plain_water_ml} ml/h"
             if volume_valid else
             f"Plain water: {fluid_ml} ml/h - unknown carbohydrate-drink volume = unknown"),
        ],
        "caffeine": ("No caffeine amount prescribed without the athlete's requested dose and tolerance. "
                     f"Fixed gel factor: {fueling_reference.GEL_CAFFEINE_MG:g} mg per gel."),
        "abort": ("If GI distress hits: stop carbohydrate intake, reduce intensity, and use only "
                  "a previously tested hydration source as tolerated. Restart at half the prior "
                  "carbohydrate rate only with a previously tested source; otherwise remain stopped. "
                  "Do not introduce a different substrate during the session."),
        "input_provenance": [
            "Carb powder composition: self-reported as 100% glucose and zero fructose.",
            "Table sugar composition: self-reported as 50/50 glucose/fructose.",
            "Sweat rate: self-reported at roughly 1 L/h; it is not a measured sweat test.",
            "Sodium target: self-reported at 800-900 mg/L.",
            f"Finished carbohydrate-drink mass: prescribed at {finished_drink_mass_g} g and must be scale-verified.",
            (f"Finished carbohydrate-drink volume: {session.get('finished_drink_volume_source') or 'self-reported'} at {carb_drink_volume_ml} ml."
             if carb_drink_volume_ml is not None else
             "Finished carbohydrate-drink volume: unknown; measure before calculating plain water."),
            "No density is assumed and no mass-to-volume conversion is made.",
            "No magnesium added; stacking it is a live GI risk.",
        ],
        "verify_labels": [
            "Check the carb powder label for any added fructose or sucrose before assuming zero.",
            "Check gel labels for actual glucose:fructose split and sodium per serving.",
            "Nothing new on race day — only use mixes already tested in training.",
        ],
    }
    if config.event_has_leg("bike") and disc in ("bike", "brick", "race", "triathlon"):
        plan["placement"] = "Put carbohydrate fuel on the bike, not the run."
    elif disc in ("race", "triathlon"):
        plan["placement"] = (
            "The active event has no bike leg. Front-load carbohydrate early and expect "
            "the final third to be tolerance-limited."
        )
    elif disc == "run" and hours > 2.5:
        plan["placement"] = "Long run: carry the rate at the low end. The run is where GI failure shows up."
    return plan


def _logged_totals(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    def s(k):
        total = round(sum(float(r.get(k) or 0) for r in rows), 2)
        return int(total) if total.is_integer() else total
    return {"kcal": s("kcal"), "protein_g": s("protein_g"), "carb_g": s("carb_g"), "fat_g": s("fat_g")}


def get_day() -> dict[str, Any]:
    """Nutrition dashboard payload: targets, what's logged, remaining, fueling."""
    today = config.local_today().isoformat()
    session = _today_session()
    completed_min = 0.0
    try:
        load = garmin_source.get_recent_load(2)
        completed_min = sum((a.get("minutes") or 0) for a in (load.get("activities") or [])
                            if (a.get("date") or "") == today)
    except Exception:
        completed_min = 0.0

    targets = daily_targets(session, completed_min)
    logged = db.get_nutrition(today)
    totals = _logged_totals(logged)
    remaining = {k: max(0, targets[k] - totals[k]) for k in ("kcal", "carb_g", "protein_g", "fat_g")}

    return {
        "date": today,
        "weight": weight_info(),
        "session": session,
        "targets": targets,
        "logged": logged,
        "totals": totals,
        "remaining": remaining,
        "fueling": fueling_plan(session),
    }


def log_completed_fueling() -> dict[str, Any]:
    """Record the carbohydrate energy from today's prescribed intra-session fuel.

    This deliberately logs only what the athlete was told to take *during* the
    session. It does not guess at recovery food or exercise calories, and uses a
    stable description to make the confirmation button safe to tap twice.
    """
    today = config.local_today().isoformat()
    session = _today_session()
    plan = fueling_plan(session)
    # The volume measurement blocks only the fluid split. Carbohydrate rate and
    # duration are still exact and may be logged after the athlete confirms
    # taking them. Race plans with an unknown leg duration have no total and
    # remain blocked.
    if not plan.get("needed") or not plan.get("total_carb_g"):
        return {"ok": True, "added": False, "note": plan.get("note", "No fuel was prescribed."),
                "day": get_day()}

    carbs = int(round((plan.get("total_carb_g") or [0, 0])[1] or 0))
    title = (session.get("title") or session.get("discipline") or "workout").strip()
    description = f"Workout fuel · {title}"
    existing = db.get_nutrition_by_description(today, description)
    if existing:
        return {"ok": True, "added": False, "already_logged": True,
                "note": "Today's prescribed workout fuel is already in your intake.", "day": get_day()}

    db.add_nutrition(today, description, eaten_at="during workout", meal="during",
                     kcal=carbs * 4, carb_g=carbs, protein_g=0, fat_g=0)
    return {"ok": True, "added": True, "carb_g": carbs, "kcal": carbs * 4,
            "note": f"Added {carbs} g carbohydrate ({carbs * 4} kcal) from today's prescribed workout fuel.",
            "day": get_day()}


# --- Context for the AI -------------------------------------------------------
def _context_block(user_request: str = "") -> str:
    today = config.local_now()
    tdi = today.date().isoformat()
    week_end = (today.date() + datetime.timedelta(days=6)).isoformat()
    plan_week = db.get_plan(tdi, week_end)
    day = get_day()
    # what the athlete told Coach Steve today (often includes training time)
    constraints = [c["text"] for c in db.get_constraints(tdi)]
    try:
        rd = garmin_source.get_readiness()
        readiness = {"recovery_am": (rd.get("training_readiness") or {}).get("score"),
                     "current": (rd.get("current_readiness") or {}).get("score"),
                     "sleep_h": (rd.get("sleep") or {}).get("hours")}
    except Exception:
        readiness = {}
    payload = {
        "now_local": today.strftime("%A %H:%M %Z"),
        "race": config.race_phase(),
        "coaching_contract": {
            "current_mode": coaching_contract.current_mode(),
            "athlete_constants": coaching_contract.athlete_context(),
            "event_profile": coaching_contract.event_context(),
        },
        "athlete_weight_kg": day["weight"]["kg"],
        "athlete_weight": day["weight"],
        "input_provenance": {
            "Garmin weight": "self-reported via Garmin when a dated entry is present",
            "Garmin recovery": "measured when dated and present",
            "athlete constants and event profile": "self-reported",
            "finished drink mass and volume": "unknown until separately measured",
            "daily macro targets": "assumed coaching prescription, explicitly labelled in targets.provenance",
        },
        "prefs": config.NUTRITION_PREFS,
        "today_session": day["session"],
        "intra_workout_fueling": day["fueling"],
        "daily_targets_loose": day["targets"],
        "eaten_so_far_today": day["logged"],
        "totals_so_far": day["totals"],
        "remaining_to_target": day["remaining"],
        "notes_to_coach_today": constraints,
        "readiness": readiness,
        "plan_this_week": [{"date": d["date"], "discipline": d["discipline"], "title": d["title"],
                            "duration_min": d["duration_min"]} for d in plan_week],
    }
    guide = (athlete_guide.context_for(user_request)
             if coaching_contract.EVENT_PROFILE.get("athlete_guide_key") == "vancouver-2026"
             else None)
    if guide:
        payload["vancouver_athlete_guide"] = guide
    if fueling_reference.is_fueling_query(user_request):
        payload["fueling_reference"] = fueling_reference.context()
    return json.dumps(payload, separators=(",", ":"), default=str)


def _ok_key() -> str | None:
    key = config.ANTHROPIC_API_KEY
    if not key or key.strip().endswith("..."):
        return None
    return key


# --- AI actions ---------------------------------------------------------------
def suggest_meal(user_request: str = "") -> dict[str, Any]:
    """Proactive next-meal suggestion, or answer a request like 'can I eat a burger?'."""
    if not _ok_key():
        return {"error": "ANTHROPIC_API_KEY is not set"}
    ctx = _context_block(user_request)
    if user_request.strip():
        ask = (f"The athlete asks: \"{user_request.strip()}\"\n\nAnswer it using the context — if it's "
               "a 'can I eat X' question, give a verdict and what to pair it with. Give an amount only "
               "from an exact supplied label or recipe; otherwise ask for it. "
               "If they're asking what to eat, suggest the right next meal for where they are in the day "
               "and their training.")
    else:
        ask = ("Suggest what to eat NEXT given the time of day, what's left to hit targets, and today's "
               "training (before/after). Give 1–2 concrete meal options and timing. Do not estimate "
               "portions or macros; request an exact label or measured recipe where needed.")
    try:
        msg = _reply([{"role": "user", "content": f"<context>\n{ctx}\n</context>\n\n{ask}"}])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    reply = _sanitize_visible_reply("".join(b.text for b in msg.content if b.type == "text").strip())
    return {"reply": reply, "model": msg.model}


_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def log_food(text: str) -> dict[str, Any]:
    """Log one meal only when every macro was explicitly self-reported.

    Typical-food and photo estimates look precise but are not checkable inputs.
    Missing values therefore stay unknown and the athlete gets one concrete
    question instead of a fabricated entry that would corrupt the day's totals.
    """
    if not text.strip():
        return {"error": "nothing to log"}
    raw = text.strip()
    values = {
        "kcal": _explicit_nutrition_value(raw, ("kcal", "calorie", "calories"), unit=""),
        "protein_g": _explicit_nutrition_value(raw, ("protein", "p")),
        "carb_g": _explicit_nutrition_value(raw, ("carb", "carbs", "carbohydrate", "c")),
        "fat_g": _explicit_nutrition_value(raw, ("fat", "f")),
    }
    missing = [name for name, value in values.items() if value is None]
    if missing:
        labels = {"kcal": "calories", "protein_g": "protein grams",
                  "carb_g": "carbohydrate grams", "fat_g": "fat grams"}
        return {
            "ok": False,
            "requires_input": True,
            "added": 0,
            "unknown_fields": missing,
            "question": ("Macros are unknown and nothing was logged. Give the exact "
                         + ", ".join(labels[field] for field in missing)
                         + " from the serving label or your measured recipe."),
            "input_provenance": {"description": "self-reported", "macros": "unknown"},
        }

    lowered = raw.lower()
    meal = next((name for name in ("breakfast", "lunch", "dinner", "snack", "pre", "during", "post")
                 if re.search(rf"\b{re.escape(name)}\b", lowered)), "")
    time_match = re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:1[0-2]|[1-9])(?::[0-5]\d)?\s*(?:am|pm)\b",
                           lowered)
    eid = db.add_nutrition(
        config.local_today().isoformat(), raw[:200],
        eaten_at=(time_match.group(0) if time_match else ""), meal=meal,
        kcal=values["kcal"], protein_g=values["protein_g"],
        carb_g=values["carb_g"], fat_g=values["fat_g"],
    )
    return {
        "ok": True,
        "added": 1,
        "entry_id": eid,
        "input_provenance": {"description": "self-reported",
                             "macros": "self-reported exact values"},
        "day": get_day(),
    }


def log_photo(image_b64: str, media_type: str = "image/jpeg") -> dict[str, Any]:
    """Refuse uncheckable macro estimation from a meal photo."""
    if not image_b64:
        return {"error": "no image"}
    del media_type
    return {
        "ok": False,
        "requires_input": True,
        "added": 0,
        "unknown_fields": ["portion_mass", "kcal", "protein_g", "carb_g", "fat_g"],
        "question": ("A photo cannot provide checkable portions or macros, so nothing was logged. "
                     "Enter the measured portion and exact calories, protein, carbohydrate, and fat "
                     "from the label or recipe."),
        "input_provenance": {"photo": "measured image", "portion_and_macros": "unknown"},
    }


def _explicit_nutrition_value(text: str, labels: tuple[str, ...], *, unit: str = "g") -> float | int | None:
    """Extract only a number explicitly paired with its nutrition label."""
    label = "|".join(re.escape(item) for item in labels)
    number = r"(\d+(?:\.\d+)?)"
    unit_re = rf"\s*{re.escape(unit)}?" if unit else r"\s*"
    # Label-first is accepted only with explicit punctuation. This prevents a
    # label between two values from stealing the following macro's number.
    strict = re.search(rf"\b(?:{label})\b\s*[:=]\s*{number}{unit_re}\b", text, re.I)
    if strict:
        value = float(strict.group(1))
        return int(value) if value.is_integer() else value

    all_labels = (r"kcal|calorie|calories|protein|carb|carbs|carbohydrate|fat|p|c|f")
    for match in re.finditer(rf"\b{number}{unit_re}\s*(?:{label})\b", text, re.I):
        # `protein: 2.5g carbs` belongs to protein, not carbs. Reject the
        # overlapping number-before-label interpretation.
        if re.search(rf"\b(?:{all_labels})\b\s*[:=]\s*$", text[:match.start(1)], re.I):
            continue
        value = float(match.group(1))
        return int(value) if value.is_integer() else value
    return None


def _int(v: Any) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None
