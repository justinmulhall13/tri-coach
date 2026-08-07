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

Your mission: keep them MAXIMALLY FUELLED to train hard and feel energized. This is NOT about \
weight loss — never restrict for leanness. Fuel the work.

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


def daily_targets(session: dict[str, Any], completed_min: float = 0.0) -> dict[str, Any]:
    """Loose daily macro/calorie targets scaled to the day's training demand."""
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
    kcal = carb * 4 + protein * 4 + fat * 9

    def rng(x, pct=0.12):
        return [int(round(x * (1 - pct))), int(round(x * (1 + pct)))]

    return {
        "kcal": kcal, "carb_g": carb, "protein_g": protein, "fat_g": fat,
        "kcal_range": rng(kcal), "carb_range": rng(carb), "protein_range": rng(protein, 0.1),
        "carb_per_kg": cpk, "protein_per_kg": 1.8, "basis": basis,
    }


def fueling_plan(session: dict[str, Any]) -> dict[str, Any]:
    """Intra-workout carb + sodium plan for today's session."""
    dur = session.get("duration_min") or 0
    disc = session.get("discipline") or ""
    if session.get("is_rest") or disc in ("rest", "recovery") or dur < 45:
        return {"needed": False,
                "note": "Short/easy session — water is enough; no carb strategy required. "
                        "Just don't start depleted."}
    hours = dur / 60.0
    if dur <= 75:
        cph, sph = (30, 45), (300, 500)
    elif dur <= 150:
        cph, sph = (45, 60), (400, 600)
    else:
        cph, sph = (60, 90), (500, 800)
    carb_lo, carb_hi = round(cph[0] * hours), round(cph[1] * hours)
    na_lo, na_hi = round(sph[0] * hours), round(sph[1] * hours)
    # Fluids: ~500–750 ml/hr, leaning higher for the bike (easier to drink) and
    # in heat. Swims don't need bottle fluid.
    fluid_lo, fluid_hi = (500, 750) if disc != "swim" else (0, 0)
    fluid_total_lo, fluid_total_hi = round(fluid_lo * hours), round(fluid_hi * hours)
    # practical: a 500ml bottle of mix ~ 30-40g carb + ~400mg sodium
    gels = round(((cph[0] + cph[1]) / 2 * hours) / 22)   # ~22g carb/gel
    return {
        "needed": True, "duration_min": dur, "discipline": disc,
        "carb_per_hr": list(cph), "sodium_mg_per_hr": list(sph),
        "fluid_ml_per_hr": [fluid_lo, fluid_hi],
        "total_carb_g": [carb_lo, carb_hi], "total_sodium_mg": [na_lo, na_hi],
        "total_fluid_ml": [fluid_total_lo, fluid_total_hi],
        "hours": round(hours, 1),
        "practical": f"≈{gels} gels-worth of carb (or {round(carb_lo/30)}–{round(carb_hi/30)} "
                     f"bottles of carb+electrolyte mix). Sip early, don't wait until you're empty.",
    }


# --- Day dashboard ------------------------------------------------------------
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
