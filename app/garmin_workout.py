"""Build Garmin structured workouts from a session dict and push them to the watch.

Per-discipline, tuned to this athlete's kit (Fenix 8 + HRM 200, Peloton, no bike
power meter):

- Bike  → HR targets. Watts appear only when the session explicitly says Peloton
          or indoor; outdoor work never falls back to watts.
- Run   → pace targets for training, with the installed event's HR and pace guard
          taking precedence on race-specific steps.
- Swim  → distance steps (metres) you can lap, with rests, so the watch counts
          them off.
- else  → structured time steps + full detail in the description.

`upload_workout(json)` creates it; `schedule_workout(id, date)` pins it to a day
so it syncs to the watch. Anything unparseable still yields a valid workout.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from . import coaching_contract, config, garmin_source, interval_analysis, zones

FTP = int(config.ATHLETE_PROFILE.get("ftp_w") or 288)

_SPORT = {
    "bike": (2, "cycling"), "recovery": (2, "cycling"), "brick": (2, "cycling"),
    "run": (1, "running"), "swim": (4, "swimming"),
    "strength": (5, "strength_training"), "rest": (5, "strength_training"),
}
_STEP = {"warmup": (1, "warmup"), "cooldown": (2, "cooldown"), "interval": (3, "interval"),
         "recovery": (4, "recovery"), "rest": (5, "rest")}
_COND = {"time": (2, "time"), "distance": (3, "distance"), "lap": (1, "lap.button")}
_METER_UNIT = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
_FREE = {"strokeTypeId": 6, "strokeTypeKey": "free", "displayOrder": 6}

# intensity keyword → HR zone number (uses the watch's configured zones)
def _hr_zone(intensity: str, disc: str = "") -> int:
    s = (intensity or "").lower()
    if "z1" in s or "recovery" in s:
        return 1
    if "vo2" in s or "anaerobic" in s:
        return 5
    if "threshold" in s:
        return 4
    if "tempo" in s:
        return 3
    if "race" in s:
        return 4
    if "z2" in s or "endurance" in s or "easy" in s:
        return 2
    return 2


# intensity → Peloton watt range (fraction of FTP)
_PCT = {1: (0.50, 0.60), 2: (0.60, 0.75), 3: (0.76, 0.90), 4: (0.95, 1.03), 5: (1.06, 1.20)}


def _watts(intensity: str) -> tuple[int, int]:
    lo, hi = _PCT[_hr_zone(intensity)]
    return round(FTP * lo), round(FTP * hi)


def _sport(key: str) -> dict[str, Any]:
    sid, skey = _SPORT.get(key, (2, "cycling"))
    return {"sportTypeId": sid, "sportTypeKey": skey, "displayOrder": sid}


def _hr_target(zone: int) -> dict[str, Any]:
    return {"targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
            "zoneNumber": zone, "targetValueOne": None, "targetValueTwo": None}


def _hr_bpm_target(lo: int, hi: int) -> dict[str, Any]:
    """An explicit bpm range (from the athlete's OWN Garmin zones) rather than a
    bare zone number — the watch then shows real numbers to hold."""
    return {"targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
            "zoneNumber": None, "targetValueOne": float(lo), "targetValueTwo": float(hi)}


def _event_hr(discipline: str) -> tuple[int, int] | None:
    key = "bike_hr_bpm" if discipline in {"bike", "brick"} else "run_hr_bpm" if discipline == "run" else None
    event = coaching_contract.event_context()
    raw = ((event.get("pacing_targets") or {}).get(key) or []) if key else []
    if len(raw) != 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _hr_for(intensity: str, discipline: str, title: str = "") -> dict[str, Any]:
    """Best available HR target: real bpm band, else the zone number."""
    race_specific = "race" in (intensity or "").lower()
    disc = (discipline or "").lower()
    if race_specific:
        event_target = _event_hr(disc)
        return _hr_bpm_target(*event_target) if event_target else _no_target()
    try:
        r = zones.hr_range(intensity, discipline, title)
    except Exception:  # noqa: BLE001
        r = None
    if r:
        return _hr_bpm_target(r[0], r[1])
    return _hr_target(_hr_zone(intensity))


def _hr_note(intensity: str, discipline: str, title: str = "") -> str:
    """'HR 154-173' for step descriptions, or '' when zones are unavailable."""
    race_specific = "race" in (intensity or "").lower()
    disc = (discipline or "").lower()
    if race_specific:
        r = _event_hr(disc)
        if not r:
            return "HR unknown: event target required"
        return f"HR {r[0]}-{r[1]}"
    try:
        r = zones.hr_range(intensity, discipline, title)
    except Exception:  # noqa: BLE001
        r = None
    return f"HR {r[0]}-{r[1]}" if r else ""


# Indoor sessions are the only place watts are meaningful for this athlete (the
# FTP is trainer-specific and doesn't transfer to the road).
_INDOOR_RE = re.compile(r"\b(peloton|trainer|indoor|zwift|erg|smart bike|turbo)\b", re.I)
# "W" is case-sensitive and never followed by "/": "HR 139-158 w/ 4 min easy"
# is a recovery, and reading it as watts used to delete every outdoor recovery.
_WATT_UNIT = r"(?:W(?![/\w])|[Ww]atts?\b)"
_WATT_RANGE_RE = re.compile(r"\d{2,4}(?:\s*[–\-—]\s*\d{2,4})?\s*" + _WATT_UNIT)
_WATT_CUE_RE = re.compile(
    r"(?:(?:@|at)\s*)?\d{2,4}(?:\s*[–\-—]\s*\d{2,4})?\s*" + _WATT_UNIT)


def _is_indoor(*texts: str | None) -> bool:
    return any(_INDOOR_RE.search(t or "") for t in texts)


def _strip_watt_cue(text: str | None) -> str | None:
    """Remove an outdoor watt cue without inventing a warmup/cooldown HR."""
    if not text:
        return text
    cleaned = _WATT_CUE_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;")


def _pace_target(mps_slow: float, mps_fast: float) -> dict[str, Any]:
    return {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
            "zoneNumber": None, "targetValueOne": round(mps_slow, 3), "targetValueTwo": round(mps_fast, 3)}


# How far below an easy ceiling the slow bound sits. Garmin needs a real slow
# bound: a 0 m/s bound is an infinite pace and the watch renders it as garbage.
# 2:30/km of headroom keeps the too-slow alert out of the way on easy days.
_CEILING_SLACK_SEC = 150.0


def _pace_ceiling(max_mps: float) -> dict[str, Any]:
    """Encode a maximum running speed with a generous, real slow bound."""
    slow_mps = 1000.0 / (1000.0 / max_mps + _CEILING_SLACK_SEC)
    return {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
            "zoneNumber": None, "targetValueOne": round(slow_mps, 3), "targetValueTwo": round(max_mps, 3)}


def _pace_label(sec_per_km: float) -> str:
    """'5:26/km (8:45/mi)' — the watch may be set to either unit."""
    return f"{zones.fmt_pace(sec_per_km)}/km ({zones.fmt_pace(sec_per_km * 1.609344)}/mi)"


def _easy_ceiling_sec(title: str = "") -> float | None:
    """The one easy-run ceiling used everywhere: the slow end of the easy band.

    It is the number the plan text prints ("no faster than X/km"), so the watch
    and the app can never disagree. The fast end of the band sits near
    marathon pace and is not an easy ceiling."""
    try:
        # Never pass the title: "Run — threshold" would turn the easy lookup
        # into a threshold one and cap the warmup and jogs at threshold pace.
        r = zones.pace_range("easy")
    except Exception:  # noqa: BLE001
        r = None
    return r[0] if r else None


def _hr_bpm_ceiling(max_bpm: int) -> dict[str, Any]:
    """One-sided heart-rate ceiling used only when pace data is unavailable."""
    return {
        "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                       "displayOrder": 4},
        "zoneNumber": None,
        "targetValueOne": 0.0,
        "targetValueTwo": float(max_bpm),
    }


def _no_target() -> dict[str, Any]:
    return {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
            "zoneNumber": None, "targetValueOne": None, "targetValueTwo": None}


def _easy_run_target_and_note(title: str = "") -> tuple[dict[str, Any], str]:
    """Return the same one-sided easy ceiling for encoding and description."""
    ceiling = _easy_ceiling_sec(title)
    if ceiling:
        return _pace_ceiling(1000.0 / ceiling), f"CEILING {_pace_label(ceiling)}; do not go faster"
    try:
        hr = zones.hr_range("easy", "run", title)
    except Exception:  # noqa: BLE001
        hr = None
    if hr:
        return _hr_bpm_ceiling(hr[1]), f"HR CEILING {hr[1]} bpm; do not exceed"
    return _no_target(), "Easy ceiling unknown; Garmin running zones required"


def _exec(order: int, kind: str, end: str, value: float, target: dict[str, Any],
          desc: str | None = None, swim: bool = False) -> dict[str, Any]:
    sid, skey = _STEP[kind]
    cid, ckey = _COND[end]
    step = {
        "type": "ExecutableStepDTO", "stepOrder": order,
        "stepType": {"stepTypeId": sid, "stepTypeKey": skey, "displayOrder": sid},
        "description": desc,
        "endCondition": {"conditionTypeId": cid, "conditionTypeKey": ckey, "displayOrder": cid, "displayable": True},
        "endConditionValue": None if end == "lap" else float(value),
        "targetValueUnit": None,
        **target,
    }
    if end == "distance":
        step["preferredEndConditionUnit"] = _METER_UNIT
    if swim:
        step["strokeType"] = _FREE
    return step


def _repeat(order: int, iterations: int, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "RepeatGroupDTO", "stepOrder": order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": iterations, "smartRepeat": False, "workoutSteps": steps}


# --- pace helpers -------------------------------------------------------------
def _threshold_pace_sec() -> float | None:
    """Threshold pace (sec/km) from the 10K race prediction, else None."""
    try:
        rp = garmin_source.get_client().get_race_predictions()
        item = rp[-1] if isinstance(rp, list) and rp else (rp if isinstance(rp, dict) else {})
        t10k = item.get("time10K")
        if isinstance(t10k, (int, float)) and t10k > 0:
            return t10k / 10.0
    except Exception:
        pass
    return None


def _pace_for(intensity: str, thr: float) -> dict[str, Any]:
    """Pace target (m/s range) for a run intensity anchored on threshold pace."""
    z = _hr_zone(intensity)
    # seconds/km offsets from threshold
    band = {1: (75, 95), 2: (55, 75), 3: (12, 28), 4: (-6, 8), 5: (-28, -10)}[z]
    slow = thr + band[1]   # slower bound (bigger sec/km)
    fast = thr + band[0]   # faster bound (smaller sec/km)
    return _pace_target(1000.0 / slow, 1000.0 / fast)


def _fmt_pace(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


# --- workout text -> watch steps ---------------------------------------------
# The watch must run the session the app shows, step for step. Every block
# (warmup, main, cooldown, a brick's run leg) is split into clauses the way the
# plan and the coach write them, and each clause becomes steps with the same
# amounts, recoveries and targets. A number the text does not state is never
# invented: a recovery with no stated length ends on the lap button, and a
# block with no amount at all becomes a lap-button step carrying its own text.

_UNIT = r"(km|mi|miles?|m|mins?|minutes?|s|secs?|seconds?|h|hrs?|hours?)"
# "45-60 min" runs the lower bound; the text keeps the range.
_QTY_RE = re.compile(
    r"(?<![\d:./\-–])(\d+(?:\.\d+)?)(?:\s*[–\-]\s*\d+(?:\.\d+)?)?\s*" + _UNIT + r"\b", re.I)
_REPEAT_RE = re.compile(r"(?<![\d:./])(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*" + _UNIT + r"\b", re.I)
# A comma starts a new step only when a new amount follows it.
_NEW_AMOUNT = (r"(?=\d+(?:\.\d+)?\s*(?:[x×]\s*\d|km\b|mi\b|miles?\b|m\b|mins?\b|minutes?\b"
               r"|s\b|secs?\b|seconds?\b|h\b|hrs?\b|hours?\b))")
_SEP_RE = re.compile(r"\s*;\s*|\s*\+\s*|(?:,\s*|\s+|^)then\b\s*|,\s*" + _NEW_AMOUNT, re.I)
_REC_WORD_RE = re.compile(
    r"\b(?:jog|easy|recover\w*|rest|walk|float|spin|between|z1|z2|endurance|steady|off)\b", re.I)
_LAP_REC_RE = re.compile(
    r"\bfull\s+(?:recovery|rest)\b|\b(?:jog|walk)\s+(?:back|down)\b|\buntil\s+recovered\b"
    r"|\bwhen\s+ready\b", re.I)
_PACE_RE = re.compile(r"(\d{1,2}):([0-5]\d)\s*/\s*(km|mi)\b", re.I)
_PACE_RANGE_RE = re.compile(
    r"(\d{1,2}):([0-5]\d)\s*[–\-—]\s*(\d{1,2}):([0-5]\d)\s*/\s*(km|mi)\b", re.I)
_GOAL_PACE_RE = re.compile(r"\b(goal|race|marathon)[\s-]*pace\b|\bMP\b", re.I)
_PACE_BAND_SEC = 8.0   # ± around a stated "at X/km" pace
_EMPTY = {"", "—", "-", "–", "n/a", "none"}
_NESTED_RE = re.compile(r"\s*(\d+)\s*[x×]\s*\((.+)\)(.*)$")
_KIND = {"warmup": "warmup", "cooldown": "cooldown"}


def _amount(value: str, unit: str) -> tuple[str, float]:
    """('time', seconds) or ('distance', metres) for one written amount."""
    u, v = unit.lower(), float(value)
    if u.startswith("min"):
        return "time", v * 60
    if u.startswith("mi"):
        return "distance", v * 1609.344
    if u.startswith("h"):
        return "time", v * 3600
    if u.startswith("s"):
        return "time", v
    if u == "km":
        return "distance", v * 1000
    return "distance", v


def _clean(text: str | None) -> str:
    t = (text or "").strip()
    return "" if t.lower() in _EMPTY else t


def _clauses(text: str | None) -> list[tuple[str, str]]:
    """Split a block into (clause, join) pairs in the order they are run.

    join is 'start', 'then' (a sequence written with "then"), 'seq' (";", "+"
    or a comma before a new amount), 'sentence' (a later sentence that is a
    step, e.g. "Last 4 km @ 4:41/km") or 'note' (a later sentence that is
    advice, e.g. "Stop if anything hurts")."""
    text = _clean(text)
    if not text:
        return []
    text = _normalise_times(text)
    out: list[tuple[str, str]] = []
    sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if s.strip()]
    for i, sentence in enumerate(sentences):
        sentence = sentence.strip().rstrip(".!? ").strip()
        header = re.match(r"([^:()]*[A-Za-z][^:()]*):\s+", sentence)
        if header and (_QTY_RE.match(sentence[header.end():])
                       or _REPEAT_RE.match(sentence[header.end():])
                       or _NESTED_RE.match(sentence[header.end():])):
            # "Main set: 3×10 min ..." / "60 min progression: 20 min easy, ..."
            sentence = sentence[header.end():]
        if i and not re.match(r"(?:then|last|final|finish|followed by|\d)", sentence, re.I):
            out.append((sentence, "note"))
            continue
        join = "start" if i == 0 else "sentence"
        pos = 0
        for sep in _SEP_RE.finditer(sentence):
            if sep.end() == sep.start() or _depth(sentence, sep.start()):
                continue
            piece = sentence[pos:sep.start()].strip(" ,")
            if piece:
                out.append((piece, join))
            join = "then" if "then" in sep.group(0).lower() else "seq"
            pos = sep.end()
        piece = sentence[pos:].strip(" ,")
        if piece:
            out.append((piece, join))
    # "3×8 min @ threshold, 2 min jog" is one repeat, not two steps.
    merged: list[tuple[str, str]] = []
    for clause, join in out:
        prev = merged[-1][0] if merged else ""
        if (merged and join == "seq" and _REPEAT_RE.search(prev)
                and not _recovery(prev[_REPEAT_RE.search(prev).end():])
                and not _REPEAT_RE.search(clause) and _QTY_RE.match(clause)
                and _REC_WORD_RE.search(clause)):
            merged[-1] = (f"{prev}, {clause}", merged[-1][1])
            continue
        merged.append((clause, join))
    return merged


def _normalise_times(text: str) -> str:
    """'4 min 30 s' -> '270 s', '1 h 30 min' -> '90 min', '2:00 jog' -> '120 s jog'."""
    text = re.sub(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?!\s*/)(?=\s*(?:min\w*\s*)?"
                  r"(?:jog|easy|rest|recover|walk|float|spin|between)\b)",
                  lambda m: f"{int(m[1]) * 60 + int(m[2])} s", text, flags=re.I)
    text = re.sub(r"(\d+)\s*s\s*min\w*\b", r"\1 s", text)
    text = re.sub(r"(\d+)\s*min(?:utes?|s)?\s*(\d+)\s*s(?:ecs?|econds?)?\b",
                  lambda m: f"{int(m[1]) * 60 + int(m[2])} s", text, flags=re.I)
    return re.sub(r"(\d+)\s*h(?:rs?|ours?)?\s*(\d+)\s*min(?:utes?|s)?\b",
                  lambda m: f"{int(m[1]) * 60 + int(m[2])} min", text, flags=re.I)


def _depth(text: str, pos: int) -> int:
    """Bracket depth at a position: separators inside "(...)" are not steps."""
    return max(0, text[:pos].count("(") - text[:pos].count(")"))


def _recovery(tail: str) -> tuple[str, float | None, str, int] | None:
    """(end, amount, text, start) of the recovery written after a repeat."""
    for m in _QTY_RE.finditer(tail):
        around = tail[max(0, m.start() - 8):m.end() + 24]
        if "w/" in around.lower() or re.search(r"\bwith\b", around, re.I) or _REC_WORD_RE.search(around):
            end, amount = _amount(m.group(1), m.group(2))
            start = max(0, m.start() - 3) if tail[max(0, m.start() - 3):m.start()].strip() == "w/" else m.start()
            return end, amount, tail[m.start():].strip(" ,"), start
    lap = _LAP_REC_RE.search(tail)
    if lap:
        return "lap", None, tail[lap.start():].strip(" ,"), lap.start()
    return None


def _parse(clause: str) -> dict[str, Any]:
    """One clause as a single step, a repeat, work embedded in a longer block,
    or a note with no amount."""
    nested = _NESTED_RE.match(clause)
    if nested:
        return {"type": "nested", "n": int(nested.group(1)), "inner": nested.group(2),
                "rec": _recovery(nested.group(3)), "text": clause}
    rep = _REPEAT_RE.search(clause)
    emb = re.search(r"\bwith\b\s+(?=\d)", clause, re.I)
    if emb and (not rep or rep.start() >= emb.end()):
        base_text = clause[:emb.start()].strip(" ,")
        base_qty = _QTY_RE.search(base_text)
        inner = _parse(clause[emb.end():])
        if base_qty and inner["type"] in ("single", "repeat"):
            end, amount = _amount(base_qty.group(1), base_qty.group(2))
            return {"type": "embedded", "text": clause, "inner": inner,
                    "base": {"type": "single", "end": end, "amount": amount, "text": base_text}}
    if rep:
        end, amount = _amount(rep.group(2), rep.group(3))
        tail = clause[rep.end():]
        rec = _recovery(tail)
        work = clause[:rep.end()] + (tail[:rec[3]] if rec else tail)
        if not rec and re.search(r"\bstrides?\b|\bpick-?ups?\b", clause, re.I):
            # Strides are never run back to back; with no stated recovery the
            # watch waits for the lap button instead of inventing one.
            rec = ("lap", None, "easy jog back", len(tail))
        return {"type": "repeat", "n": int(rep.group(1)), "end": end, "amount": amount,
                "work": work.strip(" ,"), "rec": rec, "text": clause}
    qty = _QTY_RE.search(clause)
    if qty:
        end, amount = _amount(qty.group(1), qty.group(2))
        return {"type": "single", "end": end, "amount": amount, "text": clause}
    return {"type": "note", "text": clause}


def _span(p: dict[str, Any]) -> tuple[str, float] | None:
    """(end, total) a parsed single/repeat covers, or None if it ends on lap."""
    if p["type"] == "single":
        return p["end"], p["amount"]
    if p["type"] == "repeat":
        rec = p["rec"]
        if rec and (rec[0] == "lap" or rec[0] != p["end"]):
            return None
        return p["end"], p["n"] * (p["amount"] + (rec[1] if rec else 0.0))
    return None


def _split_base(base: float, block: float, text: str, end: str) -> tuple[float, float]:
    """Lead and tail around work placed inside a longer block."""
    rest = base - block
    low = text.lower()
    if re.search(r"second half|back half|later in", low):
        lead = base / 2 if base / 2 + block <= base else rest
    elif re.search(r"\b(?:final|at the end|to finish)\b", low):
        lead = rest
    elif re.search(r"\b(?:first|at the start|early)\b", low):
        lead = 0.0
    else:
        lead = rest / 2
    if end == "time" and rest % 60 == 0:
        lead = (lead // 60) * 60
    else:
        lead = float(round(lead))
    return lead, rest - lead


def _with_note(step: dict[str, Any], note: str) -> None:
    note = note.strip(" .,;")
    if not note:
        return
    target = step
    while target.get("type") == "RepeatGroupDTO":
        target = target["workoutSteps"][0]   # a cue describes the work, not the rest
    joined = f"{target.get('description') or ''} · {note}".strip(" ·")
    target["description"] = joined[:160]


def _lap_step(kind: str, target: dict[str, Any], desc: str, swim: bool = False) -> dict[str, Any]:
    return _exec(0, kind, "lap", 0, target, desc=desc[:160], swim=swim)


def _timed_seconds(steps: list[dict[str, Any]]) -> float:
    total = 0.0
    for s in steps:
        if s.get("type") == "RepeatGroupDTO":
            total += s["numberOfIterations"] * _timed_seconds(s["workoutSteps"])
        elif s["endCondition"]["conditionTypeKey"] == "time":
            total += s["endConditionValue"] or 0.0
    return total


def _has_amount(steps: list[dict[str, Any]]) -> bool:
    for s in steps:
        if s.get("type") == "RepeatGroupDTO" or s["endCondition"]["conditionTypeKey"] != "lap.button":
            return True
    return False


def _renumber(steps: list[dict[str, Any]], order: int = 1) -> int:
    """Garmin numbers every node in order, a repeat before its children."""
    for s in steps:
        s["stepOrder"] = order
        order += 1
        if s.get("type") == "RepeatGroupDTO":
            order = _renumber(s["workoutSteps"], order)
    return order


def _blocks(text: str | None, block: str, single, repeat, recovery) -> list[dict[str, Any]]:
    """Turn one block of text into steps with sport-specific builders.

    single(text, block, end, amount) -> step; repeat(work_text, block, end,
    amount) -> step; recovery(rec, block) -> step."""
    steps: list[dict[str, Any]] = []
    if block == "inner":
        # Inside "4×(400 m @ 3:45/km, 200 m jog)" the jog is the recovery.
        outer_single = single

        def single(text, blk, end, amount):  # noqa: F811
            if (end != "lap" and _REC_WORD_RE.search(text) and not _stated_pace(text)
                    and not re.search(r"tempo|threshold|race|vo2|@", text, re.I)):
                return recovery((end, amount, text, 0), "main")
            return outer_single(text, "main", end, amount)

    def build(p: dict[str, Any], blk: str) -> list[dict[str, Any]]:
        if p["type"] == "nested":
            inner = _blocks(p["inner"], "inner", single, repeat, recovery)
            rec = recovery(p["rec"], blk) if p["rec"] else None
            if not any(s.get("type") == "RepeatGroupDTO" for s in inner):
                return [_repeat(0, p["n"], inner + ([rec] if rec else []))]
            # Garmin does not nest repeats: unroll the outer one.
            out: list[dict[str, Any]] = []
            for i in range(p["n"]):
                out.extend(copy.deepcopy(inner))
                if rec and i < p["n"] - 1:
                    out.append(copy.deepcopy(rec))
            return out
        if p["type"] == "single":
            return [single(p["text"], blk, p["end"], p["amount"])]
        work = repeat(p["work"], blk, p["end"], p["amount"])
        group = [work] + ([recovery(p["rec"], blk)] if p["rec"] else [])
        return [_repeat(0, p["n"], group)]

    for clause, join in _clauses(text):
        if join == "note":
            if steps:
                _with_note(steps[-1], clause)
            continue
        p = _parse(clause)
        if p["type"] == "note":
            if steps:
                _with_note(steps[-1], clause)
            else:
                steps.append(single(clause, block, "lap", None))
            continue
        if (p["type"] == "single" and join == "sentence"
                and re.match(r"(?:the\s+)?(?:last|final)\b", clause, re.I)):
            # "16 km steady. Last 4 km @ 4:41/km." carves the finish out of the
            # 16 km instead of adding 4 km to it.
            prev = steps[-1] if steps else None
            if (prev and prev.get("type") == "ExecutableStepDTO"
                    and prev["endCondition"]["conditionTypeKey"] == p["end"]
                    and (prev["endConditionValue"] or 0) > p["amount"]):
                prev["endConditionValue"] = float(prev["endConditionValue"] - p["amount"])
        if p["type"] == "embedded":
            base, inner = p["base"], p["inner"]
            span = _span(inner)
            stride = bool(re.search(r"\bstrides?\b", inner["text"], re.I))
            if not span or stride or span[0] != base["end"] or span[1] > base["amount"]:
                # Strides after an easy run, or work that does not fit inside
                # the stated block, run after it.
                steps.append(single(base["text"], block, base["end"], base["amount"]))
                steps.extend(build(inner, block))
                continue
            lead, tail = _split_base(base["amount"], span[1], p["text"], base["end"])
            if lead:
                steps.append(single(base["text"], block, base["end"], lead))
            steps.extend(build(inner, "main" if block == "main" else block))
            if tail:
                steps.append(single(base["text"], block, base["end"], tail))
            continue
        steps.extend(build(p, block))
    return steps


# --- running ------------------------------------------------------------------
def _stated_pace(clause: str) -> tuple[dict[str, Any], str] | None:
    """The pace written in a clause as a watch target + note, or None.

    "at 4:59/km" -> 4:59 ±8 s; "4:09-4:27/km" -> that band; "no faster than
    5:26/km" -> a ceiling; a bare "goal pace" resolves to the athlete's goal
    pace from the active event."""
    rng = _PACE_RANGE_RE.search(clause)
    if rng:
        a = int(rng.group(1)) * 60 + int(rng.group(2))
        b = int(rng.group(3)) * 60 + int(rng.group(4))
        if rng.group(5).lower() == "mi":
            a, b = a / 1.609344, b / 1.609344
        slow, fast = max(a, b), min(a, b)
        return (_pace_target(1000.0 / slow, 1000.0 / fast),
                f"{zones.fmt_pace(fast)}-{zones.fmt_pace(slow)}/km")
    m = _PACE_RE.search(clause)
    sec = None
    if m:
        sec = int(m.group(1)) * 60 + int(m.group(2))
        if m.group(3).lower() == "mi":
            sec /= 1.609344
    elif _GOAL_PACE_RE.search(clause):
        try:
            from . import marathon_plan
            g = marathon_plan.goal_pace_per_km()
        except Exception:  # noqa: BLE001
            g = None
        if g and re.fullmatch(r"\d{1,2}:[0-5]\d", g):
            sec = int(g[:-3]) * 60 + int(g[-2:])
    if not sec:
        return None
    if re.search(r"no faster than|ceiling|at most|or slower", clause, re.I):
        return _pace_ceiling(1000.0 / sec), f"CEILING {_pace_label(sec)}; do not go faster"
    return (_pace_target(1000.0 / (sec + _PACE_BAND_SEC), 1000.0 / (sec - _PACE_BAND_SEC)),
            f"{_pace_label(sec)} (±{int(_PACE_BAND_SEC)}s)")


def _is_easy(inten: str, title: str) -> bool:
    return inten in ("easy", "recovery") or zones.zone_for(inten, title) <= 2


def _run_zone_target(inten: str, title: str, thr: float | None, brick: bool) -> dict[str, Any]:
    """Watch target for a run intensity word: event HR for race work, an
    explicit pace ceiling for easy running, a pace band otherwise."""
    if "race" in (inten or "").lower():
        tg = _hr_for(inten, "run", title)
        if brick and tg["targetType"]["workoutTargetTypeKey"] == "no.target" and thr:
            return _pace_for("race", thr)
        return tg
    easy = _is_easy(inten, title)
    try:
        r = zones.pace_range(inten, title)
    except Exception:  # noqa: BLE001
        r = None
    if r:
        if easy:
            return _easy_run_target_and_note(title)[0]
        return _pace_target(1000.0 / r[0], 1000.0 / r[1])
    if thr:
        pace = _pace_for(inten, thr)
        return _pace_ceiling(pace["targetValueTwo"]) if easy else pace
    if easy:
        return _easy_run_target_and_note(title)[0]
    return _hr_for(inten, "run", title)


def _run_zone_note(inten: str, title: str, thr: float | None, brick: bool) -> str:
    if "race" in (inten or "").lower():
        if brick and not _event_hr("run") and thr:
            return "race effort · threshold-derived pace"
        event = coaching_contract.event_context()
        pace = (event.get("pacing_targets") or {}).get("run_lap_1_min_per_km")
        suffix = f"; lap 1 ceiling {pace}/km" if pace else "; lap 1 pace unknown"
        return f"{_hr_note(inten, 'run', title)}{suffix}"
    easy = _is_easy(inten, title)
    try:
        r = zones.pace_range(inten, title)
    except Exception:  # noqa: BLE001
        r = None
    if easy:
        note = _easy_run_target_and_note(title)[1]
        if "unknown" in note and thr:
            pace = _pace_for("easy", thr)
            return f"CEILING {_fmt_pace(1000.0 / pace['targetValueTwo'])}/km; do not go faster"
        return note
    hr = _hr_note(inten, "run", title)
    if not r:
        return hr
    return f"{zones.fmt_pace(r[1])}-{zones.fmt_pace(r[0])}/km" + (f" · {hr}" if hr else "")


_RUN_WORDS = (
    (r"\bvo2|\b[35]k\s*pace|\binterval pace", "vo2"),
    (r"\bthreshold|\bcruise", "threshold"),
    (r"\btempo", "tempo"),
    (r"\brace\b|\brace[- ](?:pace|effort)|\b70\.3 (?:run )?effort|\bgoal\b", "race pace"),
    (r"\beasy|\bjog|\brecovery|\bsteady|\bshakeout|\bconversational|\brelaxed|\baerobic"
     r"|\blong[- ]run|\bz[12]\b", "easy"),
)


def _run_word(text: str) -> str | None:
    """The first intensity word written in a run clause."""
    best = None
    for pattern, word in _RUN_WORDS:
        m = re.search(pattern, text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), word)
    return best[1] if best else None


def _run_target(text: str, block: str, ctx: dict[str, Any]) -> tuple[dict[str, Any], str]:
    stated = _stated_pace(text)
    if stated:
        return stated
    hr = interval_analysis.explicit_hr_range(text)
    if hr:
        return _hr_bpm_target(*hr), f"HR {hr[0]}-{hr[1]}"
    if re.search(r"\bwalk", text, re.I):
        return _no_target(), "walk"
    if re.search(r"\bstrides?\b|\bpick-?ups?\b", text, re.I):
        return _no_target(), "stride: fast but relaxed"
    if re.search(r"\buphill\b|\bhills?\b", text, re.I):
        return _no_target(), "uphill: hard, by feel"
    word = _run_word(text)
    if word is None:
        word = "easy" if block in ("warmup", "cooldown") else (ctx["intensity"] or "easy")
    args = (ctx["title"], ctx["thr"], ctx["brick"])
    return _run_zone_target(word, *args), _run_zone_note(word, *args)


def _run_block(text: str | None, block: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    def single(clause, blk, end, amount):
        tg, note = _run_target(clause, blk, ctx)
        if end == "lap":
            return _lap_step(_KIND.get(blk, "interval"), tg, f"{clause} · {note}")
        return _exec(0, _KIND.get(blk, "interval"), end, amount, tg, desc=note)

    def repeat(work, blk, end, amount):
        tg, note = _run_target(work, "main", ctx)
        return _exec(0, "interval", end, amount, tg, desc=note)

    def recovery(rec, blk):
        end, amount, text, _ = rec
        low = text.lower()
        rest = bool(re.search(r"\b(?:rest|walk|stand)\b", low)) and not re.search(r"\bjog\b", low)
        kind = "rest" if rest else "recovery"
        if end == "lap":
            return _lap_step(kind, _no_target(), f"{text}: press lap when ready")
        hr = interval_analysis.explicit_hr_range(text)
        if hr:
            tg, note = _hr_bpm_target(*hr), f"HR {hr[0]}-{hr[1]}"
        elif rest:
            tg, note = _no_target(), text
        else:
            args = (ctx["title"], ctx["thr"], ctx["brick"])
            tg, note = _run_zone_target("easy", *args), _run_zone_note("easy", *args)
        return _exec(0, kind, end, amount, tg, desc=note)

    return _blocks(text, block, single, repeat, recovery)


# --- cycling ------------------------------------------------------------------
_BIKE_WORDS = (
    (r"\bvo2|\banaerobic|\bsprint", "vo2"),
    (r"\bthreshold|\bftp\b", "threshold"),
    (r"\btempo|\bsweet ?spot", "tempo"),
    (r"\bz1\b|\brecovery\b|\bvery easy|\beasy spin", "recovery"),
    (r"\bz2\b|\beasy|\bendurance|\baerobic|\bsteady", "easy"),
)
_BIKE_DRILL_RE = re.compile(
    r"spin-?ups?|openers?|high[- ]cadence|pick-?ups?|accelerations?", re.I)
_RACE_WORK_RE = re.compile(r"\brace\b|\brace[- ](?:effort|pace|sim)|\b70\.3 effort", re.I)


def _bike_word(text: str) -> str | None:
    best = None
    for pattern, word in _BIKE_WORDS:
        m = re.search(pattern, text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), word)
    return best[1] if best else None


def _bike_target(text: str, ctx: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Bike work is prescribed by heart rate (the trainer FTP does not transfer
    outdoors). The event's race HR wins on race-effort work, an explicit bpm
    range next, then the intensity word in the clause, then the session's."""
    title, indoor = ctx["title"], ctx["indoor"]
    word = None
    if _RACE_WORK_RE.search(text):
        ev = _event_hr("bike")
        if ev:
            tg, note = _hr_bpm_target(*ev), f"HR {ev[0]}-{ev[1]}"
            return tg, _bike_watts(note, text, "race", ctx)
    hr = interval_analysis.explicit_hr_range(text)
    if hr:
        return _hr_bpm_target(*hr), _bike_watts(f"HR {hr[0]}-{hr[1]}", text, None, ctx)
    word = _bike_word(text)
    if word is None and _BIKE_DRILL_RE.search(text) and not _RACE_WORK_RE.search(text):
        label = _BIKE_DRILL_RE.search(text).group(0).lower()
        return _no_target(), f"{label}: by feel"
    word = word or ctx["intensity"]
    note = _hr_note(word, "bike", title) or ("steady effort" if not indoor else "")
    return _hr_for(word, "bike", title), _bike_watts(note, text, word, ctx)


def _bike_watts(note: str, text: str, word: str | None, ctx: dict[str, Any]) -> str:
    """Indoor rides add the Peloton watts as a note; outdoor never shows watts."""
    if not ctx["indoor"]:
        return note
    stated = _WATT_RANGE_RE.search(text)
    if stated:
        return f"{note} · Peloton {stated.group(0)}".strip(" ·")
    w = zones.watt_range(word or ctx["intensity"], ctx["title"])
    return f"{note} · indoor {w[0]}-{w[1]} W".strip(" ·") if w else note


def _bike_block(text: str | None, block: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    def single(clause, blk, end, amount):
        if blk in ("warmup", "cooldown"):
            # Warmups and cooldowns carry no encoded target.
            if end == "lap":
                return _lap_step(_KIND[blk], _no_target(), clause)
            return _exec(0, _KIND[blk], end, amount, _no_target(), desc=clause[:160])
        tg, note = _bike_target(clause, ctx)
        if end == "lap":
            return _lap_step("interval", tg, f"{clause} · {note}")
        return _exec(0, "interval", end, amount, tg, desc=note)

    def repeat(work, blk, end, amount):
        tg, note = _bike_target(work, ctx)
        if (end == "time" and amount < 120 and not interval_analysis.explicit_hr_range(work)
                and not _RACE_WORK_RE.search(work)):
            # Heart rate cannot respond inside a sprint: an HR target would only
            # alarm. The effort is ridden by feel, as written.
            drill = _BIKE_DRILL_RE.search(work)
            label = drill.group(0).lower() if drill else f"{_bike_word(work) or 'hard'} effort"
            tg, note = _no_target(), f"{label}, by feel"
        return _exec(0, "interval", end, amount, tg, desc=note)

    def recovery(rec, blk):
        end, amount, text, _ = rec
        hr = interval_analysis.explicit_hr_range(text)
        if hr:
            tg, note = _hr_bpm_target(*hr), f"HR {hr[0]}-{hr[1]}"
        elif re.search(r"\bz2\b|endurance|steady|aerobic", text, re.I):
            tg, note = _hr_for("easy", "bike", ctx["title"]), _hr_note("easy", "bike", ctx["title"])
        else:
            tg, note = _no_target(), "easy spin"
        if end == "lap":
            return _lap_step("recovery", tg, f"{text}: press lap when ready")
        return _exec(0, "recovery", end, amount, tg, desc=note or "easy spin")

    return _blocks(text, block, single, repeat, recovery)


# --- swimming -----------------------------------------------------------------
def _duration_seconds(text: str | None) -> int | None:
    """Seconds from a time phrase: '1:30', '90 s', '20 sec', '2 min'. None if none."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*:\s*(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+)\s*(?:min|minute)s?\b", text, re.I)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:s|sec|second)s?\b", text, re.I)
    if m:
        return int(m.group(1))
    return None


def _swim_distance(chunk: str) -> int | None:
    """Metres in a swim chunk, ignoring numbers that are actually times.
    So '30s rest' / '60 sec' → None (never a distance), '200 easy' → 200."""
    km = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*km\b", chunk, re.I)
    if km:
        return int(round(float(km.group(1)) * 1000))
    cleaned = re.sub(r"\d+\s*:\s*\d{2}", " ", chunk)                       # mm:ss
    cleaned = re.sub(r"/\s*\d+\s*m\b", " ", cleaned)                        # per 100m
    cleaned = re.sub(r"\d+\s*(?:s|sec|second|min|minute)s?\b", " ", cleaned, flags=re.I)  # durations
    m = re.search(r"(\d{2,4})", cleaned)
    if m and int(m.group(1)) >= 25:
        return int(m.group(1))
    return None


_REST_RE = re.compile(r"\b(rest|recover|ri|between)\b", re.I)
_SWIM_REST_RE = re.compile(
    r"(\d+(?::\d{2})?)\s*(s|secs?|seconds?|mins?|minutes?)?\s*(?:rest|ri|recovery|pause|between)\b"
    r"|\b(?:rest|ri)\s*(\d+(?::\d{2})?)\s*(s|secs?|seconds?|mins?|minutes?)?", re.I)

# Dryland / non-swim work that should NEVER become a swim step — it stays in the
# workout notes so the athlete sees it, but the watch swim workout ignores it.
_DRYLAND_RE = re.compile(
    r"\b(mobility|core|stretch\w*|foam|roll|dry ?land|yoga|band|activation|"
    r"strength|lift|plank|glute|hip|abs|pilates)\b", re.I)

# Garmin's own ids (GET /workout-service/workout/types).
_STROKES = ((re.compile(r"\bback(?:stroke)?\b", re.I), 2, "backstroke"),
            (re.compile(r"\bbreast(?:stroke)?\b", re.I), 3, "breaststroke"),
            (re.compile(r"\b(?:fly|butterfly)\b", re.I), 5, "fly"),
            (re.compile(r"\bIM\b|\bmedley\b"), 7, "individual_medley"),
            (re.compile(r"\bchoice\b|\bany stroke\b", re.I), 1, "any_stroke"))
_EQUIPMENT = ((re.compile(r"\bpull(?:[- ]?buoy)?\b", re.I), 4, "pull_buoy"),
              (re.compile(r"\bkick(?: ?board)?\b", re.I), 2, "kickboard"),
              (re.compile(r"\bpaddles?\b", re.I), 3, "paddles"),
              (re.compile(r"\bfins?\b", re.I), 1, "fins"),
              (re.compile(r"\bsnorkel\b", re.I), 5, "snorkel"))


def _is_dryland(text: str | None) -> bool:
    return bool(_DRYLAND_RE.search(text or ""))


def _swim_rest(chunk: str) -> int | None:
    m = _SWIM_REST_RE.search(chunk)
    if not m:
        return None
    value = m.group(1) or m.group(3)
    unit = (m.group(2) or m.group(4) or "s").lower()
    if ":" in value:
        a, b = value.split(":")
        return int(a) * 60 + int(b)
    return int(value) * (60 if unit.startswith("m") else 1)


def _swim_step(kind: str, end: str, value: float | None, chunk: str) -> dict[str, Any]:
    step = _exec(0, kind, end, value or 0, _no_target(), desc=chunk[:80], swim=True)
    for pattern, sid, key in _STROKES:
        if pattern.search(chunk):
            step["strokeType"] = {"strokeTypeId": sid, "strokeTypeKey": key, "displayOrder": sid}
            break
    for pattern, eid, key in _EQUIPMENT:
        if pattern.search(chunk):
            step["equipmentType"] = {"equipmentTypeId": eid, "equipmentTypeKey": key,
                                     "displayOrder": eid}
            break
    return step


def _swim_chunks(text: str) -> list[tuple[str, bool]]:
    """(chunk, is_note): split at ; + , and newlines outside brackets; a later
    sentence that does not start with an amount is a note."""
    out: list[tuple[str, bool]] = []
    sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip()) if s.strip()]
    for i, sentence in enumerate(sentences):
        sentence = sentence.strip().rstrip(".!? ")
        if i and not re.match(r"\d", sentence):
            out.append((sentence, True))
            continue
        depth, cur = 0, ""
        for ch in sentence:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            if depth == 0 and ch in ";+,\n":
                out.append((cur.strip(), False))
                cur = ""
            else:
                cur += ch
        out.append((cur.strip(), False))
    return [(c, n) for c, n in out if c]


def _swim_steps(text: str, order: int = 1, kind: str = "interval") -> tuple[list[dict[str, Any]], int]:
    """Parse a swim set into lappable distance steps (metres) with its rests.

      - 'N×M'          → repeat of an M-metre swim, plus the rest if one is written
      - a rest phrase  → the rest of the set before it, or a standalone rest step
      - a bare 'M ...' → an M-metre swim step
      - 'N min ...'    → a timed swim step (dryland text is skipped)
    Stroke (backstroke, breaststroke, ...) and equipment (pull buoy, kickboard,
    paddles, fins) written in a chunk are set on its step."""
    steps: list[dict[str, Any]] = []
    last_repeat: dict[str, Any] | None = None
    for chunk, is_note in _swim_chunks(text or ""):
        if is_note:
            if steps:
                _with_note(steps[-1], chunk)
            continue
        nested = _NESTED_RE.match(chunk)
        if nested:
            # "3×(4×100 @ CSS, 10s rest)": Garmin does not nest repeats, so the
            # rounds are unrolled; a following "1 min between rounds" goes
            # between them.
            inner, _ = _swim_steps(nested.group(2), kind=kind)
            if inner:
                rounds = {"type": "rounds", "n": int(nested.group(1)), "inner": inner}
                if not any(x.get("type") == "RepeatGroupDTO" for x in inner):
                    last_repeat = _repeat(0, rounds["n"], inner)
                    steps.append(last_repeat)
                else:
                    steps.append(rounds)
                    last_repeat = None
            continue
        rep = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*(m|metres?|meters?|s|secs?|seconds?|mins?|minutes?)?\b",
                        chunk, re.I)
        rest = _swim_rest(chunk)
        if rep:
            reps, value, unit = int(rep.group(1)), int(rep.group(2)), (rep.group(3) or "").lower()
            timed = unit.startswith(("s", "min"))
            if timed:
                work = _swim_step(kind, "time", value * (60 if unit.startswith("min") else 1), chunk)
            elif value >= 25:
                work = _swim_step(kind, "distance", value, chunk)
            else:
                last_repeat = None
                continue
            group = [work]
            if rest:
                group.append(_exec(0, "rest", "time", rest, _no_target(), desc=f"{rest}s rest"))
            last_repeat = _repeat(0, reps, group)
            steps.append(last_repeat)
            continue
        if rest is not None or (_REST_RE.search(chunk) and _duration_seconds(chunk)):
            secs = rest or _duration_seconds(chunk)
            if steps and steps[-1].get("type") == "rounds":
                steps[-1]["rest"] = secs
                continue
            if last_repeat is not None and len(last_repeat["workoutSteps"]) == 1:
                last_repeat["workoutSteps"].append(
                    _exec(0, "rest", "time", secs, _no_target(), desc=f"{secs}s rest"))
            else:
                steps.append(_exec(0, "rest", "time", secs, _no_target(), desc=chunk[:80]))
            last_repeat = None
            continue
        dist = _swim_distance(chunk)
        if dist:
            steps.append(_swim_step(kind, "distance", dist, chunk))
            last_repeat = None
            continue
        secs = _duration_seconds(chunk)
        if secs and secs >= 60 and not _is_dryland(chunk):
            steps.append(_swim_step(kind, "time", secs, chunk))
            last_repeat = None
            continue
        if steps and not _is_dryland(chunk) and steps[-1].get("type") != "rounds":
            _with_note(steps[-1], chunk)
    out: list[dict[str, Any]] = []
    for step in steps:
        if step.get("type") != "rounds":
            out.append(step)
            continue
        for i in range(step["n"]):
            out.extend(copy.deepcopy(step["inner"]))
            if step.get("rest") and i < step["n"] - 1:
                out.append(_exec(0, "rest", "time", step["rest"], _no_target(),
                                 desc=f"{step['rest']}s between rounds"))
    return out, order + len(out)


def _swim_block(text: str | None, kind: str) -> list[dict[str, Any]]:
    text = _clean(text)
    if not text:
        return []
    steps, _ = _swim_steps(text, kind=kind)
    if not steps and not _is_dryland(text):
        # No amount anywhere: a lap-button step, never a made-up distance.
        steps = [_lap_step(kind, _no_target(), text, swim=True)]
    return steps


# --- bricks -------------------------------------------------------------------
# Garmin's top-level multi_sport workout type (id discovered empirically; id 9 is
# "hiit", 10 is "multi_sport"). A brick becomes ONE multisport activity on the
# watch with two sport segments (bike → run) and an automatic transition.
_MULTISPORT = {"sportTypeId": 10, "sportTypeKey": "multi_sport", "displayOrder": 10}


def _main_or_remainder(steps: list[dict[str, Any]], around: list[dict[str, Any]],
                       duration_min: Any, text: str | None, make) -> list[dict[str, Any]]:
    """A main block with no amount runs for the session's remaining minutes
    (the app shows the total), else until the lap button. Never a default."""
    if _has_amount(steps):
        return steps
    try:
        left = float(duration_min or 0) * 60 - _timed_seconds(around)
    except (TypeError, ValueError):
        left = 0.0
    desc = _clean(text) or "main set"
    if left >= 60:
        return [make("time", left, desc)]
    return steps or [make("lap", None, desc)]


def _build_brick(sess: dict[str, Any], warm, main, cool, run_txt, intensity: str,
                 name: str, desc: str, thr: float | None, indoor: bool = False) -> dict[str, Any]:
    """A true brick: cycling segment (warmup + the full ride + cooldown) then a
    running segment, as one multisport workout so the watch records ONE activity
    and only transitions once the whole planned ride is done."""
    # Prefer the dedicated `structure.run`; fall back to the legacy
    # "…then … run" phrasing for plan days seeded before the split.
    if not _clean(run_txt):
        parts = re.split(r";?\s*then\b", main or "", maxsplit=1)
        if len(parts) > 1 and re.search(r"\brun\b", parts[1], re.I):
            main, run_txt = parts[0], parts[1]
        else:
            run_txt = ""
    bike_ctx = {"intensity": intensity, "title": name, "indoor": indoor}
    warm_steps = _bike_block(warm, "warmup", bike_ctx)
    cool_steps = _bike_block(cool, "cooldown", bike_ctx)
    body = _bike_block(main, "main", bike_ctx)

    def bike_fill(end, value, text):
        tg, note = _bike_target(text, bike_ctx)
        if end == "lap":
            return _lap_step("interval", tg, f"{text} · {note}")
        return _exec(0, "interval", end, value, tg, desc=note)

    if not _has_amount(body):
        body = body or [bike_fill("lap", None, _clean(main) or "ride")]
    bike_steps = warm_steps + body + cool_steps

    run_ctx = {"intensity": intensity, "title": name, "thr": thr, "brick": True}
    run_steps = _run_block(run_txt, "leg", run_ctx)
    if not _has_amount(run_steps):
        tg, note = _run_target(run_txt or "", "leg", run_ctx)
        run_steps = [_lap_step("interval", tg, f"{_clean(run_txt) or 'run off the bike'} · {note}")]
    for step in run_steps:
        if step.get("type") == "ExecutableStepDTO" and step["description"]:
            step["description"] = f"run off the bike · {step['description']}"[:160]
            break
    order = _renumber(bike_steps)
    _renumber(run_steps, order)
    return {
        "workoutName": name, "description": desc[:1024], "sportType": _MULTISPORT,
        "estimatedDurationInSecs": int((sess.get("duration_min") or 60) * 60),
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": _sport("bike"), "workoutSteps": bike_steps},
            {"segmentOrder": 2, "sportType": _sport("run"), "workoutSteps": run_steps},
        ],
    }


# --- top-level ----------------------------------------------------------------
def build_workout(sess: dict[str, Any], date_str: str, thr_pace: float | None = None) -> dict[str, Any] | None:
    disc = (sess.get("discipline") or "").lower()
    if disc in {"rest", "race"} or sess.get("is_rest"):
        return None
    st = sess.get("structure") or {}
    warm, main, cool = st.get("warmup"), st.get("main"), st.get("cooldown")
    intensity = sess.get("intensity") or ""
    name = (sess.get("title") or "Workout").strip()[:40]
    run_leg = (st.get("run") if disc == "brick" else None)
    bike_session = disc in {"bike", "recovery", "brick"}
    session_indoor = _is_indoor(name, warm, main, cool) if bike_session else False
    if bike_session and not session_indoor:
        race_hr = _event_hr("bike") if "race" in f"{intensity} {main or ''}".lower() else None
        # Only the work block can supply the work target. A warmup HR cue must
        # never leak into a legacy watt-based main set.
        explicit_hr = interval_analysis.explicit_hr_range(main)
        fallback_note = _hr_note(intensity, "bike", name)
        hr = race_hr or explicit_hr
        replacement = (f"HR {hr[0]}-{hr[1]}" if hr else fallback_note or "HR target unknown")
        # Warmups and cooldowns carry no encoded target, so do not manufacture
        # the main-set HR in their descriptions when removing stale watts.
        warm = _strip_watt_cue(warm)
        main = _WATT_RANGE_RE.sub(replacement, main) if main else main
        cool = _strip_watt_cue(cool)
    desc = f"Training · {intensity} · {sess.get('duration_min','')} min\n" + "\n".join(
        p for p in [f"Warmup: {warm}" if _clean(warm) else None,
                    f"{'Bike' if disc == 'brick' else 'Main'}: {main}" if _clean(main) else None,
                    f"Cooldown: {cool}" if _clean(cool) else None,
                    f"Run off bike: {run_leg}" if _clean(run_leg) else None] if p)
    if disc == "brick":
        return _build_brick(
            sess, warm, main, cool, st.get("run"), intensity, name, desc, thr_pace, session_indoor,
        )
    swim = disc == "swim"
    duration = sess.get("duration_min")

    if swim:
        warm_steps = _swim_block(warm, "warmup")
        body = _swim_block(main, "interval")
        cool_steps = _swim_block(cool, "cooldown")
        if not body and not _is_dryland(main):
            body = [_lap_step("interval", _no_target(), _clean(main) or name, swim=True)]
    elif bike_session:
        ctx = {"intensity": intensity, "title": name, "indoor": session_indoor}
        warm_steps = _bike_block(warm, "warmup", ctx)
        cool_steps = _bike_block(cool, "cooldown", ctx)

        def bike_fill(end, value, text):
            tg, note = _bike_target(text, ctx)
            if end == "lap":
                return _lap_step("interval", tg, f"{text} · {note}")
            return _exec(0, "interval", end, value, tg, desc=note)

        body = _main_or_remainder(_bike_block(main, "main", ctx), warm_steps + cool_steps,
                                  duration, main, bike_fill)
    elif disc == "run":
        ctx = {"intensity": intensity, "title": name, "thr": thr_pace, "brick": False}
        warm_steps = _run_block(warm, "warmup", ctx)
        cool_steps = _run_block(cool, "cooldown", ctx)

        def run_fill(end, value, text):
            tg, note = _run_target(text, "main", ctx)
            if end == "lap":
                return _lap_step("interval", tg, f"{text} · {note}")
            return _exec(0, "interval", end, value, tg, desc=note)

        body = _main_or_remainder(_run_block(main, "main", ctx), warm_steps + cool_steps,
                                  duration, main, run_fill)
    else:  # strength / other
        warm_steps, cool_steps = [], []
        body = [_exec(0, "interval", "time", (duration or 40) * 60, _no_target(),
                      desc=(main or name)[:120])]
    steps = warm_steps + body + cool_steps
    _renumber(steps)

    sport = _sport(disc)
    segment: dict[str, Any] = {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
    if swim:
        segment["poolLength"] = 25.0
        segment["poolLengthUnit"] = _METER_UNIT
    return {
        "workoutName": name, "description": desc[:1024], "sportType": sport,
        "estimatedDurationInSecs": int((duration or 60) * 60),
        "workoutSegments": [segment],
    }


def push(sess: dict[str, Any], date_str: str) -> dict[str, Any]:
    discipline = (sess.get("discipline") or "").lower()
    if discipline == "race":
        return {
            "error": (
                "race-day multisport cannot be pushed as one cycling workout; "
                "use the watch's triathlon activity profile"
            )
        }
    thr = _threshold_pace_sec() if discipline in ("run", "brick") else None
    wo = build_workout(sess, date_str, thr_pace=thr)
    if wo is None:
        return {"error": "rest day — nothing to push"}
    c = garmin_source.get_client()
    created = c.upload_workout(wo)
    wid = created.get("workoutId") if isinstance(created, dict) else None
    if not wid:
        return {"error": "upload failed", "raw": created}
    scheduled = None
    try:
        scheduled = c.schedule_workout(wid, date_str)
    except Exception as e:
        scheduled = {"error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "workout_id": wid, "name": wo["workoutName"], "date": date_str,
            "scheduled": bool(scheduled and not scheduled.get("error"))}


def delete(workout_id: int | str) -> dict[str, Any]:
    try:
        garmin_source.get_client().delete_workout(workout_id)
        return {"ok": True, "deleted": workout_id}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
