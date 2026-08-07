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
import re
from typing import Any

from . import config, db, garmin_source, suggest

def weight_info() -> dict[str, Any]:
    """Live weight from Garmin (auto-updates on each weigh-in), else the .env default."""
    w = garmin_source.get_weight_kg()
    if w and w.get("kg"):
        return w
    return {"kg": config.ATHLETE_WEIGHT_KG, "lb": round(config.ATHLETE_WEIGHT_KG * 2.2046, 1),
            "as_of": None, "source": "default"}


def weight_kg() -> float:
    return float(weight_info()["kg"])


_SYSTEM = """You are Chef Gordo, a high-performance sports chef and fueling coach for ONE \
endurance triathlete (bodyweight is in the context block) training for a T100 triathlon. You work hand-in-hand \
with their training coach ("Coach Steve") — the context block carries Steve's training picture \
(today's and the week's sessions, when they train, readiness, completed work) plus what they've \
eaten today and their loose macro targets.

Your mission: keep them fuelled to train hard and feel energized. Fuel the work FIRST. \
The athlete sets a calorie goal (context `targets.goal`): on "maintain" or "surplus" never \
restrict for leanness. If they have chosen "deficit", respect it — but take the deficit from \
fat and non-training carbs, protect protein and the carbs around sessions, and say plainly \
when a deficit would compromise a key session.

Hard rules:
- Be specific and practical. Give real foods with PORTIONS and rough macros (e.g. "220 g cooked \
rice + 200 g ground beef ≈ 70 g carb / 45 g protein"). The athlete eats meat + rice/potato \
staples readily and has no restrictions — suggest freely but keep it simple to cook.
- TIME food around training. If they train early AM, pre-session should be small, carb-based and \
easily digestible (banana, toast+honey, small oats); the big carb refuel comes AFTER. If they \
train after work, keep lunch moderate and load carbs around the session. Two-a-days need fuel \
between sessions. Use the actual session times/notes in the context.
- Respect the day's load: long/hard days = more carbs; rest/easy days = fewer carbs, hold protein.
- Targets are LOOSE ranges — guide toward them, don't nag about exact numbers. If they're under \
on carbs before a big session, say so plainly.
- When they ask "can I eat X", answer YES/NO + how much fits the day, and what to pair it with.
INTRA-SESSION FUELLING — HARD CONSTRAINTS (this athlete had GI failure from
overfuelling glucose on 2026-08-03; treat gut tolerance as a limit, not a preference):
- Glucose absorbs via SGLT1 and caps near 60 g/h. Fructose uses GLUT5, worth ~30 g/h more.
Usable total = min(glucose,60) + min(fructose,30). Never prescribe more than 60 g/h glucose
regardless of the carb target. Above 60 g/h total the mix must be roughly 2:1 glucose:fructose.
- Maltodextrin, rice maltodextrin, waxy maize, cyclic dextrin, dextrose and glucose syrup are
ALL pure glucose — a product listing only these has ZERO fructose. Sucrose and maple syrup are
50/50; honey is ~40% fructose. Their carb powder is 100% glucose: assume zero fructose from it.
- Rate by duration: under 60 min none (water only); 60-150 min 30-60 g/h; over 150 min 70-90 g/h
with the 2:1 split mandatory. ALWAYS state the glucose rate separately from the total.
- Fluid/sodium: heavy salty sweater at ~1 L/h; 800-900 mg sodium per litre from TABLE SALT
(3/8 tsp = 850 mg), not tablets. Never add magnesium — the carb powder contains it and magnesium
at dose is a laxative. Keep drink concentration 6-8% carb by mass; carb beyond that goes in gels
with plain water.
- Caffeine 3-6 mg/kg from coffee, not gels (gels are ~20 mg). ~5 h half-life. NEVER before swimming.
- Never introduce a new product, mix or rate on race day. Give an ABORT PROTOCOL on every long
session. Fuel the bike, not the run. SHOW THE ARITHMETIC (g/h, glucose/h, fructose/h, sodium/h,
fluid/h) and state which product labels to verify.
- OUTPUT FORMAT — never a wall of text. Lead with a one-line "TL;DR:" then 2–5 short \
"• Label — one line" bullets (labels like Now, Pre, Post, Portion, Macros, Timing, Verdict). \
Plain text only: no markdown bold/asterisks, no emoji, no filler."""


_TRANSIENT = ("RemoteProtocolError", "APIConnectionError", "ConnectionError",
              "ReadTimeout", "APITimeoutError", "InternalServerError", "OverloadedError")


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
    """Today's session as {discipline, duration_min, intensity, title, is_rest}."""
    try:
        s = suggest.todays_suggestion()
        w = (s.get("suggestion") or {}) if isinstance(s, dict) else {}
        if w:
            return {"discipline": w.get("discipline"), "duration_min": w.get("duration_min"),
                    "intensity": w.get("intensity"), "title": w.get("title"),
                    "is_rest": bool(w.get("is_rest") or (w.get("discipline") == "rest"))}
    except Exception:
        pass
    d = db.get_plan_day(config.local_today().isoformat()) or {}
    return {"discipline": d.get("discipline"), "duration_min": d.get("duration_min"),
            "intensity": d.get("intensity"), "title": d.get("title"), "is_rest": bool(d.get("is_rest"))}


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
    }


# ── Intestinal transport ceilings (the constraint that governs everything) ─────
# Glucose crosses via SGLT1 and saturates near 60 g/h. Fructose uses GLUT5, worth
# roughly another 30 g/h. Anything beyond those caps stays in the gut, pulls water
# in osmotically and causes urgency and cramping. This athlete has a documented GI
# failure from overfuelling glucose (2026-08-03), so these are hard limits.
GLUCOSE_CEILING_G_H = 60
FRUCTOSE_CEILING_G_H = 30
MAX_CARB_G_H = GLUCOSE_CEILING_G_H + FRUCTOSE_CEILING_G_H     # 90
DRINK_CONC_MIN, DRINK_CONC_MAX = 0.06, 0.08   # 6-8% carb by mass
SWEAT_L_H = 1.0                                # heavy, salty sweater
SODIUM_MG_PER_L = (800, 900)
TSP_SALT_MG_SODIUM = 2267.0    # 1 tsp table salt ~ 2267 mg sodium (3/8 tsp ~ 850)


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
    dur = session.get("duration_min") or 0
    disc = (session.get("discipline") or "").lower()
    hours = dur / 60.0

    if session.get("is_rest") or disc == "rest":
        return {"needed": False, "note": "Rest day. No intra-session fuelling."}
    if dur < 60:
        return {"needed": False,
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

    target = min(hi, MAX_CARB_G_H)

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

    # Drink concentration. Carb in the bottle is limited to 6-8% by mass; the rest
    # has to come from gels/solids taken with plain water.
    fluid_ml = int(SWEAT_L_H * 1000)
    max_in_drink = int(fluid_ml * DRINK_CONC_MAX)      # 80 g in 1 L at 8%
    in_drink = min(total, max_in_drink)
    out_of_drink = total - in_drink
    conc_pct = round(in_drink / fluid_ml * 100, 1)

    na_lo, na_hi = SODIUM_MG_PER_L
    na_lo, na_hi = int(na_lo * SWEAT_L_H), int(na_hi * SWEAT_L_H)
    na_mid = (na_lo + na_hi) // 2

    # Sucrose is the practical fructose source (50/50 glucose/fructose): each gram
    # of sucrose supplies 0.5 g fructose, so cover the fructose need with 2x sucrose,
    # then make up the remaining glucose from the pure-glucose powder.
    sucrose_g = round(fructose * 2)
    glucose_from_sucrose = sucrose_g / 2
    powder_g = max(0, round(glucose - glucose_from_sucrose))
    # Only the in-bottle share goes in the drink; the rest is gels with plain water.
    share = (in_drink / total) if total else 0
    drink_powder = round(powder_g * share)
    drink_sucrose = round(sucrose_g * share)

    caff_lo, caff_hi = round(3 * weight_kg()), round(6 * weight_kg())
    is_swim = disc in ("swim", "pool_swim", "open_water_swim")

    plan = {
        "needed": True,
        "duration_min": dur, "hours": round(hours, 2), "discipline": disc,
        "band": band,
        "carb_g_per_hr": total,
        "glucose_g_per_hr": glucose,
        "fructose_g_per_hr": fructose,
        "glucose_ceiling": GLUCOSE_CEILING_G_H,
        "fructose_ceiling": FRUCTOSE_CEILING_G_H,
        "ratio": f"{round(glucose / fructose, 1)}:1" if fructose else "glucose only",
        "fluid_ml_per_hr": [fluid_ml, fluid_ml],
        "sodium_mg_per_hr": [na_lo, na_hi],
        "salt_per_hr": _salt_tsp(na_mid),
        "drink_carb_g": in_drink, "drink_conc_pct": conc_pct,
        "carb_outside_drink_g": out_of_drink,
        "total_carb_g": [round(total * hours * 0.9), round(total * hours)],
        "total_fluid_ml": [int(fluid_ml * hours), int(fluid_ml * hours)],
        "total_sodium_mg": [int(na_lo * hours), int(na_hi * hours)],
        "recipe": (
            (f"Poolside per hour: {powder_g} g glucose powder + {sucrose_g} g table sugar "
             f"({_salt_tsp(na_mid)} salt) in ~600 ml, sipped between sets. "
             "Swimming does not allow a bottle on the move, and sweat loss is lower in water.")
            if is_swim else
            (f"Bottle per hour: {drink_powder} g glucose powder + {drink_sucrose} g table sugar "
             f"+ {_salt_tsp(na_mid)} salt in {fluid_ml} ml water ({conc_pct}%)"
             + (f". Remaining {out_of_drink} g as gels, each with plain water."
                if out_of_drink else "."))),
        "arithmetic": [
            f"Total carb: {total} g/h",
            f"Glucose: {glucose} g/h (ceiling {GLUCOSE_CEILING_G_H})",
            f"Fructose: {fructose} g/h (ceiling {FRUCTOSE_CEILING_G_H})",
            f"Sodium: {na_lo}-{na_hi} mg/h = {_salt_tsp(na_mid)} table salt",
            f"Fluid: {fluid_ml} ml/h",
            f"Drink concentration: {in_drink} g in {fluid_ml} ml = {conc_pct}% "
            f"(limit {int(DRINK_CONC_MAX*100)}%)",
        ],
        "caffeine": ("No caffeine before swimming."
                     if is_swim else
                     f"{caff_lo}-{caff_hi} mg (3-6 mg/kg) from coffee, not gels "
                     f"(gels are ~20 mg). ~5 h half-life, so time it off the session start."),
        "abort": ("If GI distress hits: stop carb intake immediately, keep sipping plain water "
                  "with salt only, drop intensity until symptoms settle, then restart at half "
                  "the carb rate with sugar-based fuel rather than powder. Do not push through it."),
        "assumptions": [
            "Carb powder is 100% glucose (maltodextrin/dextrose/cyclic dextrin are all glucose) — zero fructose assumed.",
            "Table sugar is 50/50 glucose/fructose; it is the fructose source here.",
            "Sweat rate assumed 1 L/h and salty — the default for this athlete.",
            "No magnesium added: the carb powder already contains it and magnesium at dose is a laxative.",
        ],
        "verify_labels": [
            "Check the carb powder label for any added fructose or sucrose before assuming zero.",
            "Check gel labels for actual glucose:fructose split and sodium per serving.",
            "Nothing new on race day — only use mixes already tested in training.",
        ],
    }
    if disc in ("bike", "brick"):
        plan["placement"] = "Fuel the bike, not the run. Carbs absorb far better on the bike and GI problems surface on the run."
    elif disc == "run" and hours > 2.5:
        plan["placement"] = "Long run: carry the rate at the low end. The run is where GI failure shows up."
    return plan


def _logged_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    def s(k):
        return int(sum(r.get(k) or 0 for r in rows))
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


# --- Context for the AI -------------------------------------------------------
def _context_block() -> str:
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
        "athlete_weight_kg": day["weight"]["kg"],
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
    return json.dumps(payload, indent=2, default=str)


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
    ctx = _context_block()
    if user_request.strip():
        ask = (f"The athlete asks: \"{user_request.strip()}\"\n\nAnswer it using the context — if it's "
               "a 'can I eat X' question, give a verdict + how much fits today and what to pair it with. "
               "If they're asking what to eat, suggest the right next meal for where they are in the day "
               "and their training.")
    else:
        ask = ("Suggest what to eat NEXT given the time of day, what's left to hit targets, and today's "
               "training (before/after). Give 1–2 concrete meal options with portions and rough macros, "
               "and say when to eat relative to the session.")
    try:
        msg = _reply([{"role": "user", "content": f"<context>\n{ctx}\n</context>\n\n{ask}"}])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    reply = "".join(b.text for b in msg.content if b.type == "text").strip()
    return {"reply": reply, "model": msg.model}


_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def log_food(text: str) -> dict[str, Any]:
    """Parse a free-text food log into structured entries, store them, return the day."""
    if not text.strip():
        return {"error": "nothing to log"}
    if not _ok_key():
        return {"error": "ANTHROPIC_API_KEY is not set"}
    parse_system = ("You convert a free-text food log into JSON. Output ONLY a JSON array, no prose. "
                    "Each item: {\"description\": str, \"eaten_at\": str (e.g. \"07:30\" or \"post-ride\" "
                    "or \"\"), \"meal\": one of breakfast|lunch|dinner|snack|pre|during|post, "
                    "\"kcal\": int, \"protein_g\": int, \"carb_g\": int, \"fat_g\": int}. "
                    "Estimate macros from typical portions when amounts are vague. If multiple foods, "
                    f"split into separate items. Be realistic for a ~{weight_kg():.0f} kg athlete.")
    try:
        msg = _reply([{"role": "user", "content": f"Log this: {text.strip()}"}],
                     max_tokens=900, system=parse_system)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    raw = "".join(b.text for b in msg.content if b.type == "text")
    m = _JSON_RE.search(raw)
    if not m:
        return {"error": "could not parse the food", "raw": raw[:300]}
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"error": "could not parse the food", "raw": raw[:300]}

    today = config.local_today().isoformat()
    stored = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("description"):
            continue
        eid = db.add_nutrition(
            today, str(it["description"])[:200], eaten_at=str(it.get("eaten_at") or "")[:40],
            meal=str(it.get("meal") or "")[:20],
            kcal=_int(it.get("kcal")), protein_g=_int(it.get("protein_g")),
            carb_g=_int(it.get("carb_g")), fat_g=_int(it.get("fat_g")))
        stored.append(eid)
    if not stored:
        return {"error": "nothing recognized to log", "raw": raw[:300]}
    return {"ok": True, "added": len(stored), "day": get_day()}


def log_photo(image_b64: str, media_type: str = "image/jpeg") -> dict[str, Any]:
    """Log a meal from a photo — the model reads the plate and estimates macros."""
    if not image_b64:
        return {"error": "no image"}
    if not _ok_key():
        return {"error": "ANTHROPIC_API_KEY is not set"}
    system = ("You read a photo of a meal and convert it to JSON. Output ONLY a JSON array, no prose. "
              "Each item: {\"description\": str, \"eaten_at\": \"\", \"meal\": "
              "breakfast|lunch|dinner|snack|pre|during|post, \"kcal\": int, \"protein_g\": int, "
              "\"carb_g\": int, \"fat_g\": int}. Identify each distinct food you can see and estimate "
              "its portion from visual cues (plate size, utensils). Be realistic for a "
              f"~{weight_kg():.0f} kg endurance athlete. If you cannot identify any food, return [].")
    try:
        msg = _reply([{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": "Log this meal."},
        ]}], max_tokens=900, system=system)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    raw = "".join(b.text for b in msg.content if b.type == "text")
    m = _JSON_RE.search(raw)
    if not m:
        return {"error": "couldn't read that photo", "raw": raw[:300]}
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"error": "couldn't read that photo", "raw": raw[:300]}

    today = config.local_today().isoformat()
    stored = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("description"):
            continue
        stored.append(db.add_nutrition(
            today, str(it["description"])[:200], eaten_at=str(it.get("eaten_at") or "")[:40],
            meal=str(it.get("meal") or "")[:20],
            kcal=_int(it.get("kcal")), protein_g=_int(it.get("protein_g")),
            carb_g=_int(it.get("carb_g")), fat_g=_int(it.get("fat_g"))))
    if not stored:
        return {"error": "no food recognized in that photo"}
    return {"ok": True, "added": len(stored), "day": get_day()}


def _int(v: Any) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None
