"""The workout sheet's view of a session: the steps the watch runs, laid out to read.

The card used to re-parse the session text in the browser, so the list on screen
and the workout on the watch could disagree, and a set such as "3x10 min @ HR
139-158 w/ 4 min easy" was printed as one unbroken line. This module asks
`garmin_workout.build_workout` for the real step tree and reshapes it for
display: phases (warm up, main set, cool down, run off the bike), repeat groups
with their work and recovery rows, one target per row, and an intensity level
per step for the profile chart.

Nothing here invents a prescription. Amounts and targets are read off the watch
steps. Two things are display-only and never shown as numbers: `level` (how
tall a bar is drawn) and `w` (how wide), which for a distance step with no
stated time is a share of the session's remaining minutes.
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import coaching_contract, garmin_workout as gw, zones

ZONE_NAME = {1: "Recovery", 2: "Endurance", 3: "Tempo", 4: "Threshold", 5: "VO2"}
RUN_ZONE_NAME = {1: "Recovery", 2: "Easy", 3: "Tempo", 4: "Threshold", 5: "VO2"}
PHASE_LABEL = {"warmup": "Warm up", "main": "Main set", "cooldown": "Cool down",
               "run": "Run off the bike"}
LEG_LABEL = {"cycling": "Bike", "running": "Run", "swimming": "Swim"}
_STRUCTURED = {"swim", "bike", "run", "brick", "recovery"}

# Drawing only: how wide a step with no stated time is drawn.
_OPEN_STEP_S = 60.0
_FALLBACK_S_PER_M = {"running": 0.33, "cycling": 0.12}
_SWIM_REST_M_PER_S = 0.4
_SWIM_M_PER_S = 1 / 1.2
_OPEN_SWIM_M = 50.0

_THR_CACHE: dict[str, Any] = {"value": None, "ts": 0.0}
_THR_TTL = 600


def _threshold_pace() -> float | None:
    """The same threshold pace a push uses, held ten minutes so opening a
    sheet does not hit Garmin every time."""
    now = time.time()
    if _THR_CACHE["ts"] and now - _THR_CACHE["ts"] < _THR_TTL:
        return _THR_CACHE["value"]
    value = gw._threshold_pace_sec()
    _THR_CACHE.update(value=value, ts=now)
    return value


# --- small formatters ---------------------------------------------------------
def _time_label(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s % 60 == 0:
        m = s // 60
        if m >= 120:
            return f"{m // 60} h {m % 60:02d}" if m % 60 else f"{m // 60} h"
        return f"{m} min"
    if s < 120:
        return f"{s} s"   # "90 s", the way the plan writes it
    return f"{s // 60}:{s % 60:02d}"   # never rounded: rows must add up to the heading


def _distance_label(metres: float, swim: bool) -> str:
    m = float(metres)
    if swim:
        return f"{int(round(m)):,} m"
    miles = m / 1609.344
    # Written in miles (to a tenth): shown the way the plan wrote it.
    if round(miles, 1) > 0 and abs(miles * 10 - round(miles * 10)) < 1e-6 and abs(m - round(m)) > 1e-3:
        return f"{round(miles, 1):g} mi"
    if m < 1000:
        return f"{int(round(m)):,} m"
    return f"{round(m / 1000, 2):g} km"


def _duration_label(minutes: Any) -> str | None:
    try:
        m = int(round(float(minutes)))
    except (TypeError, ValueError, OverflowError):
        return None
    if m <= 0:
        return None
    if m < 120:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d}" if m % 60 else f"{m // 60} h"


def _pace(sec_per_km: float) -> str:
    s = int(round(sec_per_km))
    return f"{s // 60}:{s % 60:02d}"


def _sentence(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip(" ,.;:·")
    return t[:1].upper() + t[1:] if t else ""


# --- targets ------------------------------------------------------------------
def _zone_table(sport: str) -> dict[int, tuple[int, int]]:
    try:
        table = zones.get().get("zones") or {}
    except Exception:  # noqa: BLE001
        return {}
    key = "CYCLING" if sport == "cycling" else "DEFAULT"
    return table.get(key) or table.get("DEFAULT") or {}


def _hr_zone(lo: float, hi: float, sport: str) -> int | None:
    table = _zone_table(sport)
    mid = (lo + hi) / 2
    best, gap = None, None
    for z, band in table.items():
        try:
            if band[0] <= mid <= band[1]:
                return int(z)
            d = min(abs(mid - band[0]), abs(mid - band[1]))
        except (TypeError, IndexError):
            continue
        if gap is None or d < gap:
            best, gap = int(z), d
    # Garmin's bands leave a one-beat gap between zones; a midpoint that lands
    # in one belongs to the nearer band.
    return best if gap is not None and gap <= 2 else None


def _pace_zone(sec_per_km: float) -> int | None:
    try:
        mps = zones.get().get("lt_speed_mps")
    except Exception:  # noqa: BLE001
        mps = None
    if not mps:
        return None
    frac = (1000.0 / sec_per_km) / mps   # share of threshold speed
    best, gap = None, 9.0
    for z, (lo, hi) in zones._PACE_BANDS.items():
        if lo <= frac <= hi:
            return z
        d = min(abs(frac - lo), abs(frac - hi))
        if d < gap:
            best, gap = z, d
    return best


def _goal_run_pace() -> int | None:
    try:
        raw = (coaching_contract.event_context().get("pacing_targets") or {}).get("run_goal_min_per_km")
    except Exception:  # noqa: BLE001
        return None
    m = re.match(r"^\s*(\d{1,2}):([0-5]\d)\s*$", str(raw or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _target(step: dict[str, Any], sport: str) -> dict[str, Any] | None:
    """What to hold on this step, read off the watch target."""
    key = ((step.get("targetType") or {}).get("workoutTargetTypeKey") or "")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    desc = step.get("description") or ""
    if key == "heart.rate.zone":
        if step.get("zoneNumber"):
            z = int(step["zoneNumber"])
            return {"kind": "hr", "text": f"HR zone {z}", "zone": z}
        if one is None or two is None:
            return None
        lo, hi = int(round(one)), int(round(two))
        if lo <= 0:
            return {"kind": "ceiling", "text": f"HR {hi}", "note": "or lower", "zone": 2}
        race = gw._event_hr("bike" if sport == "cycling" else "run")
        return {"kind": "hr", "text": f"HR {lo}-{hi}", "zone": _hr_zone(lo, hi, sport),
                "race": bool(race and tuple(race) == (lo, hi))}
    if key == "pace.zone" and one and two:
        slow, fast = 1000.0 / one, 1000.0 / two   # sec/km
        # Read the kind of target off the encoded band, never off words in the
        # description: a cue appended to it ("HR ceiling 165", "cadence 180 ±5")
        # must not relabel a stated pace. A ceiling is always fast + 150 s, a
        # single stated pace always ±8 s with the builder's own "(±8s)" note.
        width = slow - fast
        if abs(width - gw._CEILING_SLACK_SEC) < 1.0:
            return {"kind": "ceiling", "text": f"{_pace(fast)}/km", "note": "or slower", "zone": 2}
        band = f"(±{int(gw._PACE_BAND_SEC)}s)"
        if (abs(width - 2 * gw._PACE_BAND_SEC) < 1.0
                and any(p.strip().endswith(band) for p in desc.split(" · "))):
            mid = (slow + fast) / 2
            goal = _goal_run_pace()
            return {"kind": "pace", "text": f"{_pace(mid)}/km", "zone": _pace_zone(mid),
                    "race": bool(goal and abs(goal - mid) < 1.5)}
        return {"kind": "pace", "text": f"{_pace(fast)}-{_pace(slow)}/km",
                "zone": _pace_zone((slow + fast) / 2)}
    return None


# --- names and cues -----------------------------------------------------------
# The builder's own target notes, which the target slot already shows. Case
# matters: the athlete's "HR ceiling 165" is a cue and stays.
_NOTE_RE = re.compile(r"^(?:HR \d+\s*-\s*\d+$|HR CEILING \d|HR (?:target )?unknown|CEILING \d"
                      r"|Easy ceiling unknown|\d{1,2}:\d{2}\s*(?:-\s*\d{1,2}:\d{2})?\s*/)")
_LEAD_AMOUNT_RE = re.compile(
    r"^\s*(?:\d+\s*[x×]\s*)?\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:km|miles?|mi|min(?:ute)?s?|yards?|yds?|m|s(?:ec(?:ond)?s?)?|h(?:(?:ou)?rs?)?)?\b\s*", re.I)
_DRILL_RE = re.compile(r"^\s*\d+\s*m?\s+(.+?)\s*\(([^()]*\bdrill\b[^()]*)\)\s*$", re.I)
_SWIM_PACE_RE = re.compile(r"(\d{1,2}:[0-5]\d)\s*/\s*100\s*m\b", re.I)
_SWIM_REST_TAIL_RE = re.compile(
    r"(?:,|\s+w/|\s+with|\s+@)?\s*\d+\s*(?:s|secs?|seconds?|mins?|minutes?)\s*(?:rest|ri|recovery)\b.*$", re.I)
_FUEL_CUE_RE = re.compile(r"\d\s*(?:g|mg|ml|l)\b[^.;]*?(?:/\s*h\b|per\s+hour|an\s+hour)", re.I)
_REPEAT_MARK_RE = re.compile(r"\d\s*[x×]\s*\d|\b(?:repeats?|reps?)\s*[x×]\s*\d|[x×]\s*\d+\b", re.I)
_RANGE_LEAD_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(km|mins?|minutes?|m)\b", re.I)
_EASY_SWIM_RE = re.compile(r"\b(easy|choice|loosen|relaxed)\b", re.I)
_HARD_SWIM_RE = re.compile(r"\b(css|threshold|race(?:[- ](?:pace|effort))?|sprint|max)\b", re.I)
_HARD_RE = re.compile(r"\b(stride|sprint|hard|uphill|max|all[- ]out|vo2)", re.I)
_SWIM_LEVELS = (
    (re.compile(r"\b(css|threshold|race(?:[- ](?:pace|effort))?|fast|hard|sprint|max)\b", re.I), 4.0),
    (re.compile(r"\b(build|tempo|strong|descend\w*|moderate)\b", re.I), 3.0),
    (re.compile(r"\b(steady|smooth|aerobic|pull|continuous|paddles?|z2)\b", re.I), 2.0),
    (re.compile(r"\b(drill|technique|kick|scull\w*)\b", re.I), 1.5),
    (re.compile(r"\b(easy|choice|loosen|relaxed)\b", re.I), 1.0),
)


def _strip_amount(text: str) -> str:
    return _LEAD_AMOUNT_RE.sub("", text or "", count=1).strip(" ,.;:")


def _swim_name(text: str) -> tuple[str, list[str], list[str]]:
    """(name, cues, detail) for one swim chunk."""
    head, *notes = [p.strip() for p in (text or "").split(" · ") if p.strip()] or [""]
    drill = _DRILL_RE.match(head)
    if drill:
        detail = [_sentence(part) for part in drill.group(2).split(" / ") if part.strip()]
        return _sentence(drill.group(1)), [_sentence(n) for n in notes], detail
    cues = [_sentence(n) for n in notes]
    paren = re.search(r"\(([^()]*)\)", head)   # before the rest is cut: it may follow it
    if paren:
        cues.insert(0, _sentence(paren.group(1)))
        head = (head[:paren.start()] + head[paren.end():]).strip()
    body = _SWIM_REST_TAIL_RE.sub("", head)
    body = re.sub(r"\s*@\s*$", "", _strip_amount(body))
    body = re.sub(r"^@\s*", "", body)
    body = re.sub(r"\s*@\s*", ", ", body)
    return _sentence(body) or "Swim", [c for c in cues if c], []


def _land_name(step: dict[str, Any], kind: str, sport: str, phase: str,
               target: dict[str, Any] | None) -> tuple[str, list[str]]:
    """(name, cues) for a bike or run step."""
    parts = [p.strip() for p in (step.get("description") or "").split(" · ") if p.strip()]
    lead = parts[0] if parts else None   # only the clause itself opens with its amount
    parts = [p for p in parts if not _NOTE_RE.match(p)]
    parts = [p for p in parts if p.lower() != "run off the bike"]   # the leg heading says it
    # The Fuel section is the one place a rate is printed.
    parts = [p for p in parts if not _FUEL_CUE_RE.search(p)]
    name = ""
    cues: list[str] = []
    for part in parts:
        text = _strip_amount(part) if part is lead else part
        if not text:
            continue
        if not name and kind in {"interval", "recovery"} and not target:
            if ":" in text and not re.search(r"\d:\d", text):
                head, tail = text.split(":", 1)
                name, text = _sentence(head), tail
            elif re.search(r",\s*by feel\s*$", text, re.I):
                name, text = _sentence(re.sub(r",\s*by feel\s*$", "", text, flags=re.I)), "by feel"
            else:
                name, text = _sentence(text), ""
        if text.strip():
            cues.append(_sentence(text))
    if kind in {"warmup", "cooldown"}:
        # The phase heading already says warm up or cool down, so the row says
        # how: the written cue, or plain "Easy" when there is a target to hold.
        name = "Easy" if target or not cues else cues.pop(0)
    elif kind == "rest":
        name = "Rest"
    elif kind == "recovery":
        if target and target["kind"] == "hr" and (target.get("zone") or 0) >= 2:
            # "10 min Z2" between efforts is the same effort as the Z2 around it.
            name = (RUN_ZONE_NAME if sport == "running" else ZONE_NAME).get(target["zone"], "Steady")
        elif target or not name:
            name = "Easy spin" if sport == "cycling" else "Easy jog"
    elif target:
        names = RUN_ZONE_NAME if sport == "running" else ZONE_NAME
        if target.get("race"):
            name = "Race pace" if target["kind"] == "pace" else "Race effort"
        elif target["kind"] == "ceiling":
            name = "Easy"
        else:
            name = names.get(target.get("zone") or 0, "Steady")
    elif not name:
        name = "Steady"
    name = name.replace("High-cadence", "High cadence")
    return name, cues


def _level(kind: str, sport: str, text: str, target: dict[str, Any] | None,
           session_zone: int) -> float:
    """How tall the step is drawn. Rest is flat, hard work is full height."""
    if kind == "rest":
        return 0.0
    if sport == "swimming":
        text = text.split(" · ")[0]   # a cue ("nothing fast") is not the effort
        easy = bool(_EASY_SWIM_RE.search(text)) and not _HARD_SWIM_RE.search(text)
        for pattern, level in _SWIM_LEVELS:
            if easy and level >= 4:
                continue   # "easy, nothing fast": the hard word is negated
            if pattern.search(text):
                return 1.0 if kind == "cooldown" and level < 3 else level
        return {"warmup": 1.5, "cooldown": 1.0}.get(kind, float(session_zone))
    if target and target.get("zone"):
        return float(target["zone"]) + (0.4 if target.get("race") else 0.0)
    if kind in {"recovery", "cooldown"}:
        return 1.0
    if kind == "warmup":
        return 1.5
    if _HARD_RE.search(text):
        return 5.0
    if re.search(r"\bopeners?\b", text, re.I):
        return 4.0
    if re.search(r"cadence|spin-?ups?", text, re.I):
        return 3.0
    return float(session_zone)


# --- steps --------------------------------------------------------------------
def _swim_full_text(sess: dict[str, Any]) -> list[str]:
    st = sess.get("structure") or {}
    out: list[str] = []
    for key in ("warmup", "main", "cooldown"):
        try:
            out.extend(chunk for chunk, _note in gw._swim_chunks(str(st.get(key) or "")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _untruncate(desc: str, chunks: list[str]) -> str:
    head = desc.split(" · ")[0]
    if len(head) < 80:
        return desc
    for chunk in chunks:
        if chunk.startswith(head) and len(chunk) > len(head):
            return chunk + desc[len(head):]
    return desc


def _step(step: dict[str, Any], sport: str, phase: str, ctx: dict[str, Any]) -> dict[str, Any]:
    kind = (step.get("stepType") or {}).get("stepTypeKey") or "interval"
    end = (step.get("endCondition") or {}).get("conditionTypeKey") or "lap.button"
    value = step.get("endConditionValue")
    swim = sport == "swimming"
    desc = step.get("description") or ""
    if swim:
        desc = _untruncate(desc, ctx["swim_chunks"])
    target = None if swim else _target(step, sport)
    if swim:
        if kind == "rest":
            name, cues, detail = "Rest", [], []
        else:
            name, cues, detail = _swim_name(desc)
            # A pace written into the set ("@ race effort (1:57/100m)") is the
            # thing to hold, so it takes the target slot rather than a cue line.
            written = _SWIM_PACE_RE.search(desc)
            if written:
                target = {"kind": "pace", "text": f"{written.group(1)}/100 m"}
                cues = [c for c in cues if not _SWIM_PACE_RE.fullmatch(c.strip())]
    else:
        name, cues = _land_name(step, kind, sport, phase, target)
        detail = []
    if end == "time" and value:
        amount, unit = _time_label(value), "time"
    elif end == "distance" and value:
        amount, unit = _distance_label(value, swim), "distance"
    else:
        amount, unit, value = "Open", "open", None
    role = {"interval": "work", "warmup": "warmup", "cooldown": "cooldown",
            "recovery": "recovery", "rest": "rest"}.get(kind, "work")
    out: dict[str, Any] = {
        "type": "step", "role": role, "amount": amount, "unit": unit, "value": value,
        "name": name, "level": _level(kind, sport, desc, target, ctx["session_zone"]),
    }
    if target:
        out["target"] = target["text"]
        if target.get("note"):
            out["target_note"] = target["note"]
        if target.get("zone"):
            out["zone"] = target["zone"]
    cues = [c for c in cues if c and c.lower() != name.lower()]
    if unit == "open" and not any("lap" in c.lower() for c in cues):
        cues.append("Press lap when ready")
    if cues:
        out["cue"] = ". ".join(dict.fromkeys(cues))
    if detail:
        out["detail"] = detail
    return out


def _fold_rests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A swim rest reads as part of the swim before it ("100 m, 20 s rest"), the
    way a set is written on a pool deck. The rest stays in the list for the
    chart; `fold` tells the sheet not to give it a row of its own."""
    prev = None
    for item in items:
        if (item["type"] == "step" and item["role"] == "rest" and item["unit"] == "time"
                and prev and prev["type"] == "step" and prev["role"] != "rest" and "rest" not in prev):
            prev["rest"] = f"{item['amount']} rest"
            item["fold"] = True
        prev = item
    return items


def _item(node: dict[str, Any], sport: str, phase: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if node.get("type") == "RepeatGroupDTO":
        children = [_item(child, sport, phase, ctx) for child in node.get("workoutSteps") or []]
        if sport == "swimming":
            _fold_rests(children)
        return {"type": "repeat", "count": int(node.get("numberOfIterations") or 1), "items": children}
    return _step(node, sport, phase, ctx)


def _walk(items: list[dict[str, Any]], times: int = 1):
    """Every executable step with how many times it runs."""
    for item in items:
        if item["type"] == "repeat":
            yield from _walk(item["items"], times * item["count"])
        else:
            yield item, times


def _weights(legs: list[dict[str, Any]], duration_min: Any) -> None:
    """Set `w`, the drawn width of each step, in one unit per leg sport."""
    try:
        session_s = float(duration_min or 0) * 60
    except (TypeError, ValueError):
        session_s = 0.0
    land = [(leg, list(_walk([i for ph in leg["phases"] for i in ph["items"]])))
            for leg in legs if leg["sport"] != "swimming"]
    timed = sum(s["value"] * n for _leg, steps in land for s, n in steps if s["unit"] == "time")
    metres = sum(s["value"] * n for _leg, steps in land for s, n in steps if s["unit"] == "distance")
    left = session_s - timed
    for leg, steps in land:
        for s, _n in steps:
            if s["unit"] == "time":
                s["w"] = float(s["value"])
            elif s["unit"] == "distance":
                if metres and left >= 60:
                    s["w"] = s["value"] / metres * left
                else:
                    s["w"] = s["value"] * _FALLBACK_S_PER_M.get(leg["sport"], 0.33)
            else:
                s["w"] = _OPEN_STEP_S
    for leg in legs:
        if leg["sport"] != "swimming":
            continue
        for s, _n in _walk([i for ph in leg["phases"] for i in ph["items"]]):
            if s["unit"] == "distance":
                s["w"] = float(s["value"])
            elif s["unit"] == "time":
                s["w"] = s["value"] * (_SWIM_REST_M_PER_S if s["role"] == "rest" else _SWIM_M_PER_S)
            else:
                s["w"] = _OPEN_SWIM_M


def _phase_total(items: list[dict[str, Any]], swim: bool, *, always: bool = False) -> str | None:
    visible = [i for i in items if not i.get("fold")]
    if not always and len(visible) == 1 and visible[0]["type"] == "step":
        return None   # the one row under the heading already says it
    steps = [(s, n) for s, n in _walk(items)]
    if swim:
        swims = [(s, n) for s, n in steps if s["role"] != "rest"]
        if swims and all(s["unit"] == "distance" for s, _n in swims):
            return _distance_label(sum(s["value"] * n for s, n in swims), True)
        return None
    if steps and all(s["unit"] == "time" for s, _n in steps):
        return _time_label(sum(s["value"] * n for s, n in steps))
    if len(steps) > 1 and all(s["unit"] == "distance" for s, _n in steps):
        # "16 km steady. Last 4 km at race pace" is rows of 12 km and 4 km;
        # the heading keeps the 16 the plan wrote.
        return _distance_label(sum(s["value"] * n for s, n in steps), False)
    return None


def _repeat_amount(item: dict[str, Any]) -> str:
    working = [s for s in item["items"] if s["type"] == "step" and s["role"] not in {"recovery", "rest"}]
    work = working[0] if working else None
    if not work or len(working) > 1:
        return f"{item['count']} rounds"   # "3×(200 pull, 100 kick)" is not 3 × 200 m
    return f"{item['count']} × {work['amount']}"


def _focus(legs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The piece of the session the summary line names: the hardest part of the
    main set that is a real share of it, so strides do not outrank the run."""
    main = [item for leg in legs[:1] for ph in leg["phases"] if ph["key"] == "main" for item in ph["items"]]
    if not main:
        main = [item for leg in legs[:1] for ph in leg["phases"] for item in ph["items"]]
    total = sum(s.get("w", 0) * n for s, n in _walk(main)) or 1.0
    best = None
    for item in main:
        if item["type"] == "repeat":
            work = [s for s in item["items"] if s["type"] == "step" and s["role"] == "work"]
            if not work:
                continue
            lead = max(work, key=lambda s: s["level"])
            weight = sum(s.get("w", 0) for s in work) * item["count"]
            cand = {"amount": _repeat_amount(item), "step": lead, "weight": weight}
        else:
            if item["role"] in {"rest", "recovery"}:
                continue
            cand = {"amount": item["amount"], "step": item, "weight": item.get("w", 0)}
        cand["share"] = cand["weight"] / total
        cand["plain"] = item["type"] == "step"
        key = (cand["share"] >= 0.2, cand["step"]["level"], cand["weight"])
        if best is None or key > best[0]:
            best = (key, cand)
    if not best:
        return None
    pick = best[1]
    if not pick["plain"]:
        # Rounds the watch had to unroll ("3×(4×100)") are one set to the athlete.
        same = sum(1 for i in main if i["type"] == "repeat" and _repeat_amount(i) == pick["amount"])
        if same > 1:
            pick["amount"] = f"{same} × {pick['amount']}"
    plain = [i for i in main if i["type"] == "step" and i["role"] == "work"]
    if pick["plain"] and len(plain) > 1 and len({(i["name"], i.get("target")) for i in plain}) == 1:
        # One steady effort with short efforts dropped into it ("30 min Z2 with
        # 6x30 s spin-ups") is named by the whole block, not by one half of it.
        whole = _phase_total(main, False, always=True)
        if whole:
            pick["amount"] = whole
    return pick


def _as_written(legs: list[dict[str, Any]], st: dict[str, Any], session_zone: int) -> None:
    """A main set with no amount in it ("fartlek by feel around the loop") runs
    on the watch as one step for the remaining minutes. The watch names that
    step from a keyword; the sheet shows the athlete the sentence itself."""
    text = str(st.get("main") or "").strip()
    if not text or gw._QTY_RE.search(text) or gw._REPEAT_RE.search(text):
        return
    for leg in legs[:1]:
        if leg["sport"] == "swimming":
            return
        for ph in leg["phases"]:
            if ph["key"] != "main" or len(ph["items"]) != 1 or ph["items"][0]["type"] != "step":
                continue
            step = ph["items"][0]
            first, _, more = text.partition(". ")
            step["name"] = _sentence(first)
            step.pop("cue", None)
            if more.strip():
                step["cue"] = _sentence(more)
            if re.search(r"\bby feel\b|\bno targets?\b", text, re.I):
                # The text asks for no number; the session label must not add one.
                for key in ("target", "target_note", "zone"):
                    step.pop(key, None)
            if not step.get("target"):
                step["level"] = float(session_zone)


def _written(st: dict[str, Any], brick: bool) -> list[dict[str, str]]:
    """The session text, block by block, for the sheet's "As written" fold."""
    out = []
    for key, label in (("warmup", "Warm up"), ("main", "Bike" if brick else "Main set"),
                       ("cooldown", "Cool down"), ("run", "Run off the bike")):
        text = str(st.get(key) or "").strip()
        if text and text not in gw._EMPTY:
            out.append({"label": label, "text": text})
    return out


def _keep_ranges(legs: list[dict[str, Any]], st: dict[str, Any]) -> bool:
    """"45-60 min easy" runs the lower bound on the watch. The row keeps the
    range the plan wrote. True when any row was relabelled."""
    found = False
    for leg in legs:
        for ph in leg["phases"]:
            m = _RANGE_LEAD_RE.match(str(st.get(ph["key"]) or ""))
            first = ph["items"][0] if ph["items"] else None
            if not m or not first or first["type"] != "step" or first["value"] is None:
                continue
            unit = m.group(3).lower()
            low = float(m.group(1)) * (60 if unit.startswith("min") else 1000 if unit == "km" else 1)
            if abs(first["value"] - low) < 0.5:
                first["amount"] = f"{m.group(1)}-{m.group(2)} {'min' if unit.startswith('min') else unit}"
                ph["ranged"] = found = True   # no total can be stated for this phase
    return found


def _plain_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _left_out(legs: list[dict[str, Any]], st: dict[str, Any]) -> list[str]:
    """Asides written into a swim set that no row carries.

    The watch has nowhere to put "(sight every 6 strokes on rep 2)" when it
    follows a rest, so the parser lets it go. The athlete still wrote it, or
    the coach did, so the sheet lists it under the steps."""
    shown = ""
    for leg in legs:
        for step, _n in _walk([i for ph in leg["phases"] for i in ph["items"]]):
            shown += _plain_key(" ".join([step.get("name", ""), step.get("cue", ""), step.get("target", ""),
                                          " ".join(step.get("detail") or [])]))
    out: list[str] = []
    for key in ("warmup", "main", "cooldown"):
        text = str(st.get(key) or "")
        asides = [m.group(1) for m in re.finditer(r"\(([^()]{4,160})\)", text)
                  if not re.search(r"\d\s*[x×]\s*$", text[:m.start()])]   # not a round's body
        asides += re.findall(r"\s[—–]\s+([^;.()]{4,160})", text)
        for aside in asides:
            k = _plain_key(aside)
            if k and k not in shown and _sentence(aside) not in out:
                out.append(_sentence(aside))
    return out[:6]


def _text_rows(structure: dict[str, Any]) -> list[dict[str, Any]]:
    """Strength and other sessions: rows as written, nothing parsed."""
    blocks = structure.get("blocks")
    if isinstance(blocks, list) and blocks:
        rows = []
        for b in blocks:
            if isinstance(b, dict):
                rows.append({"amount": str(b.get("label") or ""), "name": str(b.get("text") or ""),
                             "cue": str(b.get("target") or "")})
            elif b:
                rows.append({"amount": "", "name": str(b), "cue": ""})
        return rows
    rows = []
    for key, label in (("warmup", "Warm up"), ("main", ""), ("cooldown", "Cool down")):
        text = str(structure.get(key) or "").strip()
        if not text or text in gw._EMPTY:
            continue
        for part in re.split(r"\s*;\s*|\n+", text):
            if part.strip():
                rows.append({"amount": label, "name": part.strip(), "cue": ""})
    return rows


def build(sess: dict[str, Any], *, thr_pace: float | None = None,
          fetch_threshold: bool = False) -> dict[str, Any]:
    """The sheet's view model for one session.

    `kind` is one of: `steps` (a parsed swim, bike, run or brick), `list`
    (strength and anything else, rows as written), `text` (race day), `rest`.
    """
    sess = sess or {}
    disc = str(sess.get("discipline") or "").lower()
    st = sess.get("structure") if isinstance(sess.get("structure"), dict) else {}
    duration = _duration_label(sess.get("duration_min"))
    base: dict[str, Any] = {"discipline": disc, "duration": duration}

    if sess.get("is_rest") or disc == "rest":
        text = str(st.get("main") or "").strip()
        return {**base, "kind": "rest", "text": text if text not in gw._EMPTY else "Rest day."}
    if disc == "race":
        blocks = []
        for key, label in (("warmup", "Before the start"), ("main", "The race"), ("cooldown", "After")):
            text = str(st.get(key) or "").strip()
            if text and text not in gw._EMPTY:
                lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if s.strip()]
                blocks.append({"label": label, "lines": lines})
        return {**base, "kind": "text", "blocks": blocks}
    if disc not in _STRUCTURED or (isinstance(st.get("blocks"), list) and st.get("blocks")):
        return {**base, "kind": "list", "rows": _text_rows(st)}

    if thr_pace is None and fetch_threshold and disc in {"run", "brick"}:
        thr_pace = _threshold_pace()
    phases: list[list[tuple[str, int]]] = []
    try:
        workout = gw.build_workout(sess, "", thr_pace=thr_pace, phases=phases)
    except Exception:  # noqa: BLE001
        workout = None
    segments = (workout or {}).get("workoutSegments") or []
    if not segments or len(phases) != len(segments):
        return {**base, "kind": "list", "rows": _text_rows(st)}

    ctx = {"session_zone": zones.zone_for(str(sess.get("intensity") or ""), str(sess.get("title") or "")),
           "swim_chunks": _swim_full_text(sess) if disc == "swim" else []}
    legs: list[dict[str, Any]] = []
    for segment, counts in zip(segments, phases):
        sport = ((segment.get("sportType") or {}).get("sportTypeKey") or "cycling")
        nodes = list(segment.get("workoutSteps") or [])
        leg: dict[str, Any] = {"sport": sport, "label": LEG_LABEL.get(sport, ""), "phases": []}
        pos = 0
        for key, count in counts:
            chunk, pos = nodes[pos:pos + count], pos + count
            if not chunk:
                continue
            items = [_item(node, sport, key, ctx) for node in chunk]
            if sport == "swimming":
                _fold_rests(items)
            leg["phases"].append({"key": key, "label": PHASE_LABEL.get(key, ""), "items": items})
        if leg["phases"]:
            legs.append(leg)
    if not legs:
        return {**base, "kind": "list", "rows": _text_rows(st)}
    main_text = str(st.get("main") or "")
    main_items = [i for ph in legs[0]["phases"] if ph["key"] == "main" for i in ph["items"]]
    if ((_REPEAT_MARK_RE.search(main_text) and not any(i["type"] == "repeat" for i in main_items))
            or (disc == "swim" and re.search(r"\d\s*(?:yds?|yards?)\b", " ".join(
                str(st.get(k) or "") for k in ("warmup", "main", "cooldown")), re.I))):
        # The text asks for repeats the parser did not find ("1 mile repeats x4"),
        # or is in yards. Drawing the mis-read as the session would mislead, so
        # the sheet shows the text.
        return {**base, "kind": "list", "rows": _text_rows(st)}
    if disc == "brick" and len(legs) > 1 and not gw._clean(str(st.get("run") or "")):
        # No run was written: the watch waits on the lap button there. The
        # sheet must not print a pace the plan never gave.
        for step, _n in _walk([i for ph in legs[1]["phases"] for i in ph["items"]]):
            if step["unit"] == "open":
                for key in ("target", "target_note", "zone", "cue"):
                    step.pop(key, None)
                step["name"], step["level"] = "Run", 2.0

    _as_written(legs, st, ctx["session_zone"])
    ranged = _keep_ranges(legs, st)
    _weights(legs, sess.get("duration_min"))
    for leg in legs:
        for ph in leg["phases"]:
            total = None if ph.pop("ranged", False) else _phase_total(ph["items"], leg["sport"] == "swimming")
            if total:
                ph["total"] = total
            for item in ph["items"]:
                if item["type"] == "repeat":
                    item["amount"] = _repeat_amount(item)

    every = [(s, n, leg["sport"]) for leg in legs
             for s, n in _walk([i for ph in leg["phases"] for i in ph["items"]])]
    figures: list[dict[str, str]] = []
    if duration:
        figures.append({"label": "Duration", "value": duration})
    swim_m = sum(s["value"] * n for s, n, sport in every
                 if sport == "swimming" and s["unit"] == "distance" and s["role"] != "rest")
    if swim_m:
        figures.append({"label": "Distance", "value": _distance_label(swim_m, True)})
    focus = _focus(legs)
    main_total = next((ph.get("total") for ph in legs[0]["phases"] if ph["key"] == "main"), None)
    if (main_total and legs[0]["sport"] != "swimming" and main_total.endswith(" km")
            and (not focus or focus["amount"] != main_total)):
        figures.append({"label": "Distance", "value": main_total})
    if focus:
        step = focus["step"]
        short = step["name"] if len(step["name"]) <= 28 else ""
        # A one-block session's main set is its duration; saying it twice is
        # noise. A step that ends on the lap button has no amount to headline.
        if (focus["amount"] != duration or step.get("target")) and step["unit"] != "open":
            figures.append({"label": "Main set", "value": focus["amount"],
                            "sub": "" if step.get("target") else short})
        if step.get("target"):
            # A ceiling reads the same here as in its row: "5:17/km, or slower".
            figures.append({"label": "Target", "value": step["target"],
                            "sub": step.get("target_note") or short})

    # The axis is only labelled when every drawn width is a stated amount.
    axis = None
    if len(legs) == 1 and legs[0]["sport"] == "swimming":
        if swim_m and all(s["unit"] == "distance" for s, _n, _sp in every if s["role"] != "rest"):
            axis = {"unit": "distance", "total": _distance_label(swim_m, True)}
    elif every and not ranged and all(s["unit"] == "time" for s, _n, _sp in every):
        timed = sum(s["value"] * n for s, n, _sp in every)
        try:
            stated = float(sess.get("duration_min") or 0) * 60
        except (TypeError, ValueError):
            stated = 0.0
        # Within two minutes the plan's own figure is the one shown above.
        axis = {"unit": "time",
                "total": duration if duration and abs(stated - timed) <= 120 else _time_label(timed)}
    notes = _left_out(legs, st) if disc == "swim" else []
    drawable = sum(1 for s, _n, _sp in every if s["unit"] != "open") >= 1 and len(every) >= 2
    return {**base, "kind": "steps", "legs": legs, "figures": figures[:4],
            "axis": axis, "chart": drawable, "notes": notes,
            "written": _written(st, disc == "brick")}
