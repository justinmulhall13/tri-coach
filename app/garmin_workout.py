"""Build Garmin structured workouts from a session dict and push them to the watch.

Per-discipline, tuned to this athlete's kit (Fenix 8 + HRM 200, Peloton, no bike
power meter):

- Bike  → HR-zone targets (measurable on the road bike or the Peloton via the
          HRM). Each work step names the Peloton watt equivalent AND the HR, so
          it's clear which metric to focus on per device.
- Run   → pace-range targets derived from the athlete's race-predictor threshold
          pace, on lappable interval/tempo steps.
- Swim  → distance steps (metres) you can lap, with rests, so the watch counts
          them off.
- else  → structured time steps + full detail in the description.

`upload_workout(json)` creates it; `schedule_workout(id, date)` pins it to a day
so it syncs to the watch. Anything unparseable still yields a valid workout.
"""
from __future__ import annotations

import re
from typing import Any

from . import config, garmin_source, zones

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


def _hr_for(intensity: str, discipline: str, title: str = "") -> dict[str, Any]:
    """Best available HR target: real bpm band, else the zone number."""
    try:
        r = zones.hr_range(intensity, discipline, title)
    except Exception:  # noqa: BLE001
        r = None
    if r:
        return _hr_bpm_target(r[0], r[1])
    return _hr_target(_hr_zone(intensity))


def _hr_note(intensity: str, discipline: str, title: str = "") -> str:
    """'HR 154-173' for step descriptions, or '' when zones are unavailable."""
    try:
        r = zones.hr_range(intensity, discipline, title)
    except Exception:  # noqa: BLE001
        r = None
    return f"HR {r[0]}-{r[1]}" if r else ""


# Indoor sessions are the only place watts are meaningful for this athlete (the
# FTP is trainer-specific and doesn't transfer to the road).
_INDOOR_RE = re.compile(r"\b(peloton|trainer|indoor|zwift|erg|smart bike|turbo)\b", re.I)


def _is_indoor(*texts: str | None) -> bool:
    return any(_INDOOR_RE.search(t or "") for t in texts)


def _pace_target(mps_slow: float, mps_fast: float) -> dict[str, Any]:
    return {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
            "zoneNumber": None, "targetValueOne": round(mps_slow, 3), "targetValueTwo": round(mps_fast, 3)}


def _no_target() -> dict[str, Any]:
    return {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
            "zoneNumber": None, "targetValueOne": None, "targetValueTwo": None}


def _exec(order: int, kind: str, end: str, value: float, target: dict[str, Any],
          desc: str | None = None, swim: bool = False) -> dict[str, Any]:
    sid, skey = _STEP[kind]
    cid, ckey = _COND[end]
    step = {
        "type": "ExecutableStepDTO", "stepOrder": order,
        "stepType": {"stepTypeId": sid, "stepTypeKey": skey, "displayOrder": sid},
        "description": desc,
        "endCondition": {"conditionTypeId": cid, "conditionTypeKey": ckey, "displayOrder": cid, "displayable": True},
        "endConditionValue": float(value),
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


def _mins(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*min", text)
    return int(m.group(1)) if m else None


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


# --- per-discipline builders --------------------------------------------------
def _bike_steps(main: str, intensity: str, order: int, title: str = "") -> tuple[list[dict[str, Any]], int]:
    """Bike is prescribed by HEART RATE (the athlete's FTP is trainer-specific and
    doesn't transfer to hilly outdoor riding). Watts appear only as a secondary
    note when the session is explicitly indoor."""
    steps: list[dict[str, Any]] = []
    o = order
    indoor = _is_indoor(main, title)

    def note(inten: str) -> str:
        hr = _hr_note(inten, "bike", title)
        if indoor:
            w = zones.watt_range(inten, title)
            if w:
                return f"{hr} · indoor {w[0]}-{w[1]} W" if hr else f"indoor {w[0]}-{w[1]} W"
        return hr or "steady effort"

    m = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*min", main)
    if m:
        reps, dur = int(m.group(1)), int(m.group(2))
        rec = re.search(r"w/\s*(\d+)\s*min", main)
        rec_min = int(rec.group(1)) if rec else 3
        work = _exec(o + 1, "interval", "time", dur * 60, _hr_for(intensity, "bike", title),
                     desc=note(intensity))
        recov = _exec(o + 2, "recovery", "time", rec_min * 60, _hr_for("recovery", "bike"),
                      desc=f"easy spin · {_hr_note('recovery', 'bike')}".strip(" ·"))
        steps.append(_repeat(o, reps, [work, recov]))
        return steps, o + 3
    dur = _mins(main) or 45
    steps.append(_exec(o, "interval", "time", dur * 60, _hr_for(intensity, "bike", title),
                       desc=note(intensity)))
    return steps, o + 1


def _run_steps(main: str, intensity: str, thr: float | None, order: int,
               title: str = "") -> tuple[list[dict[str, Any]], int]:
    """Run steps target a real pace band derived from the athlete's Garmin
    lactate-threshold pace. Easy/recovery work carries an explicit pace CEILING
    (not a range) — running easy days too fast is this athlete's main execution
    risk, so the slow end is stated as a hard limit."""
    steps: list[dict[str, Any]] = []
    o = order

    def target(inten: str):
        r = None
        try:
            r = zones.pace_range(inten, title)
        except Exception:  # noqa: BLE001
            r = None
        if r:
            return _pace_target(1000.0 / r[0], 1000.0 / r[1])
        return _pace_for(inten, thr) if thr else _hr_for(inten, "run", title)

    def note(inten: str) -> str:
        try:
            r = zones.pace_range(inten, title)
        except Exception:  # noqa: BLE001
            r = None
        hr = _hr_note(inten, "run", title)
        if not r:
            return hr
        slow, fast = zones.fmt_pace(r[0]), zones.fmt_pace(r[1])
        if inten in ("easy", "recovery") or zones.zone_for(inten, title) <= 2:
            # A ceiling, not a range — do not run faster than this.
            return f"CEILING {fast}/km — do not go faster" + (f" · {hr}" if hr else "")
        return f"{fast}-{slow}/km" + (f" · {hr}" if hr else "")

    m = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*(s|sec|min)", main)
    if m:
        reps = int(m.group(1)); val = int(m.group(2)); unit = m.group(3)
        secs = val if unit.startswith("s") else val * 60
        rec = re.search(r"(\d+)\s*(s|sec|min)\s*(easy|jog|recover)", main)
        rsecs = 90
        if rec:
            rsecs = int(rec.group(1)) * (1 if rec.group(2).startswith("s") else 60)
        work = _exec(o + 1, "interval", "time", secs, target(intensity), desc=note(intensity))
        recov = _exec(o + 2, "recovery", "time", rsecs, target("easy"), desc=note("easy"))
        steps.append(_repeat(o, reps, [work, recov]))
        return steps, o + 3
    dur = _mins(main) or 40
    steps.append(_exec(o, "interval", "time", dur * 60, target(intensity), desc=note(intensity)))
    return steps, o + 1


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
    cleaned = re.sub(r"\d+\s*:\s*\d{2}", " ", chunk)                       # mm:ss
    cleaned = re.sub(r"\d+\s*(?:s|sec|second|min|minute)s?\b", " ", cleaned, flags=re.I)  # durations
    m = re.search(r"(\d{2,4})", cleaned)
    if m and int(m.group(1)) >= 25:
        return int(m.group(1))
    return None


_REST_RE = re.compile(r"\b(rest|recover|ri)\b", re.I)


# Dryland / non-swim work that should NEVER become a swim step — it stays in the
# workout notes so the athlete sees it, but the watch swim workout ignores it.
_DRYLAND_RE = re.compile(
    r"\b(mobility|core|stretch\w*|foam|roll|dry ?land|yoga|band|activation|"
    r"strength|lift|plank|glute|hip|abs|pilates)\b", re.I)


def _is_dryland(text: str | None) -> bool:
    return bool(_DRYLAND_RE.search(text or ""))


def _swim_fallback(order: int, kind: str, text: str | None, default_m: int) -> list[dict[str, Any]]:
    """0 or 1 swim steps when parsing found nothing structured. Dryland work (mobility/
    core/stretch) → NO step (kept in notes). Else a TIME step if minutes are stated,
    otherwise a default distance step."""
    if _is_dryland(text):
        return []
    secs = _duration_seconds(text or "")
    if secs and secs >= 60:
        return [_exec(order, kind, "time", secs, _no_target(), desc=(text or "")[:80], swim=True)]
    return [_exec(order, kind, "distance", default_m, _no_target(), desc=(text or "")[:80], swim=True)]


def _swim_steps(text: str, order: int, kind: str = "interval") -> tuple[list[dict[str, Any]], int]:
    """Parse a swim set into lappable distance steps (metres) with proper rests.

    Robust to how the coach phrases things:
      - 'N×M'          → repeat of M-metre swim + a rest step
      - a rest phrase  → a rest (time) step, never a bogus distance step; if it
                         trails a rep set (often comma-split off), it sets that
                         set's rest instead of adding a stray step
      - a bare 'M ...' → an M-metre swim step
    """
    steps: list[dict[str, Any]] = []
    o = order
    last_repeat: dict[str, Any] | None = None
    for chunk in re.split(r"[;+,\n]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        rep = re.search(r"(\d+)\s*[x×]\s*(\d+)", chunk)
        if rep:
            reps, dist = int(rep.group(1)), int(rep.group(2))
            if dist < 25:
                last_repeat = None
                continue
            rest_s = _duration_seconds(chunk) or 15
            work = _exec(o + 1, kind, "distance", dist, _no_target(), desc=chunk[:80], swim=True)
            rst = _exec(o + 2, "rest", "time", rest_s, _no_target(), desc=f"{rest_s}s rest")
            grp = _repeat(o, reps, [work, rst])
            steps.append(grp); o += 3
            last_repeat = grp
            continue
        # A rest/recovery instruction — a rest step, NOT a swim distance step.
        if _REST_RE.search(chunk):
            rsec = _duration_seconds(chunk)
            if rsec:
                if last_repeat is not None:              # belongs to the preceding set
                    rst = last_repeat["workoutSteps"][1]
                    rst["endConditionValue"] = float(rsec)
                    rst["description"] = f"{rsec}s rest"
                else:                                    # standalone rest between sets
                    steps.append(_exec(o, "rest", "time", rsec, _no_target(), desc=chunk[:80])); o += 1
            continue
        # A distance swim step ("200 easy", "1500 as 3×500").
        dist = _swim_distance(chunk)
        if dist:
            steps.append(_exec(o, kind, "distance", dist, _no_target(), desc=chunk[:80], swim=True)); o += 1
            last_repeat = None
            continue
        # A TIME-based swim ("60 min continuous", "30 min steady") → a duration step.
        # Dryland work ("20 min mobility/core") is skipped — it stays in the notes.
        secs = _duration_seconds(chunk)
        if secs and secs >= 60 and not _is_dryland(chunk):
            steps.append(_exec(o, kind, "time", secs, _no_target(), desc=chunk[:80], swim=True)); o += 1
            last_repeat = None
    return steps, o


# Garmin's top-level multi_sport workout type (id discovered empirically; id 9 is
# "hiit", 10 is "multi_sport"). A brick becomes ONE multisport activity on the
# watch with two sport segments (bike → run) and an automatic transition.
_MULTISPORT = {"sportTypeId": 10, "sportTypeKey": "multi_sport", "displayOrder": 10}


def _zone_from_watts(lo: int, hi: int) -> int:
    frac = ((lo + hi) / 2) / FTP
    if frac < 0.60:
        return 1
    if frac < 0.76:
        return 2
    if frac < 0.90:
        return 3
    if frac < 1.05:
        return 4
    return 5


def _brick_bike_steps(main: str, intensity: str, order: int) -> tuple[list[dict[str, Any]], int]:
    """Bike leg of a brick, honoring the FULL stated ride duration.

    A brick ride is written as a total block with surges inside, e.g.
    "120 min @ 207–236 W (race-sim, with 4×8 min @ 253–274 W surges)". The old
    code only built the 4×8 repeat (~44 min) and silently dropped the 120 min —
    so the watch ended the ride early and forced the transition. Here the total
    duration is preserved: steady base → surges → steady base, summing to total.
    """
    o = order
    steps: list[dict[str, Any]] = []
    total = _mins(main)                       # leading total ride minutes
    watts = re.findall(r"(\d{2,4})\s*[–\-—]\s*(\d{2,4})\s*W", main or "")
    base = (int(watts[0][0]), int(watts[0][1])) if watts else None
    surge = (int(watts[1][0]), int(watts[1][1])) if len(watts) > 1 else None
    base_z = _zone_from_watts(*base) if base else _hr_zone(intensity)

    def base_desc():
        return (f"Peloton {base[0]}-{base[1]} W · " if base else "") + f"HR zone {base_z} (race-sim base)"

    iv = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*min", main or "")
    if total and iv:
        reps, dur = int(iv.group(1)), int(iv.group(2))
        surge_z = _zone_from_watts(*surge) if surge else min(5, base_z + 1)
        rec = re.search(r"w/\s*(\d+)\s*min", main or "")
        rec_min = int(rec.group(1)) if rec else 3
        surge_block = reps * (dur + rec_min)
        remaining = max(0, total - surge_block)
        lead, tail = remaining // 2, remaining - remaining // 2
        surge_desc = (f"Peloton {surge[0]}-{surge[1]} W · " if surge else "") + f"HR zone {surge_z} ({dur}-min surge)"
        if lead:
            steps.append(_exec(o, "interval", "time", lead * 60, _hr_target(base_z), desc=base_desc())); o += 1
        work = _exec(o + 1, "interval", "time", dur * 60, _hr_target(surge_z), desc=surge_desc)
        recov = _exec(o + 2, "recovery", "time", rec_min * 60, _hr_target(base_z), desc="back to race-sim base")
        steps.append(_repeat(o, reps, [work, recov])); o += 3
        if tail:
            steps.append(_exec(o, "interval", "time", tail * 60, _hr_target(base_z), desc=base_desc())); o += 1
        return steps, o

    if total:
        steps.append(_exec(o, "interval", "time", total * 60, _hr_target(base_z), desc=base_desc())); o += 1
        return steps, o

    return _bike_steps(main or "", intensity, o)   # fallback (no leading total)


def _build_brick(sess: dict[str, Any], warm, main, cool, run_txt, intensity: str,
                 name: str, desc: str, thr: float | None) -> dict[str, Any]:
    """A true brick: cycling segment (warmup + full ride + easy-spin cooldown)
    then a running segment (run off the bike), as one multisport workout so the
    watch records ONE activity with a bike leg + run leg and only transitions
    once the FULL planned ride is done."""
    o = 1
    bike_steps: list[dict[str, Any]] = []
    if warm and _mins(warm):
        bike_steps.append(_exec(o, "warmup", "time", _mins(warm) * 60, _no_target(), desc=warm[:120])); o += 1
    body, o = _brick_bike_steps(main or "", intensity, o)
    bike_steps.extend(body)
    if cool and _mins(cool):
        bike_steps.append(_exec(o, "cooldown", "time", _mins(cool) * 60, _no_target(), desc=cool[:120])); o += 1

    # Run leg off the bike. Prefer the dedicated `structure.run`; fall back to the
    # legacy "…then … run" phrasing for plan days seeded before the split.
    if not run_txt:
        parts = re.split(r";?\s*then\b", main or "", maxsplit=1)
        run_txt = parts[1] if len(parts) > 1 else ""
    rmin = _mins(run_txt) or 20
    tg = _pace_for("race", thr) if thr else _hr_target(4)
    if thr and tg["targetType"]["workoutTargetTypeKey"] == "pace.zone":
        band = {1: (75, 95), 2: (55, 75), 3: (12, 28), 4: (-6, 8), 5: (-28, -10)}[4]
        run_desc = f"brick run off the bike · target ~{_fmt_pace(thr+band[1])}–{_fmt_pace(thr+band[0])} /km"
    else:
        run_desc = "brick run off the bike @ race effort"
    run_steps = [_exec(o, "interval", "time", rmin * 60, tg, desc=run_desc)]

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
    if disc == "rest" or sess.get("is_rest"):
        return None
    st = sess.get("structure") or {}
    warm, main, cool = st.get("warmup"), st.get("main"), st.get("cooldown")
    intensity = sess.get("intensity") or ""
    name = (sess.get("title") or "Workout").strip()[:40]
    run_leg = (st.get("run") if disc == "brick" else None)
    desc = f"Tri Coach · {intensity} · {sess.get('duration_min','')} min\n" + "\n".join(
        p for p in [f"Warmup: {warm}" if warm else None,
                    f"{'Bike' if disc == 'brick' else 'Main'}: {main}" if main else None,
                    f"Cooldown: {cool}" if cool else None,
                    f"Run off bike: {run_leg}" if run_leg else None] if p)
    if disc == "brick":
        return _build_brick(sess, warm, main, cool, st.get("run"), intensity, name, desc, thr_pace)
    swim = disc == "swim"
    steps: list[dict[str, Any]] = []
    o = 1

    # Warmup
    if warm:
        if swim:
            body, o = _swim_steps(warm, o, kind="warmup")
            if not body:
                body = _swim_fallback(o, "warmup", warm, 300); o += len(body)
            steps.extend(body)
        elif _mins(warm):
            steps.append(_exec(o, "warmup", "time", _mins(warm) * 60, _no_target(), desc=warm[:120])); o += 1

    # Main
    if disc in ("bike", "recovery", "brick"):
        body, o = _bike_steps(main or "", intensity, o, name); steps.extend(body)
        if disc == "brick":
            mrun = re.search(r"(\d+)\s*min[^.]*run", main or "")
            rmin = int(mrun.group(1)) if mrun else 20
            tg = _pace_for("race", thr_pace) if thr_pace else _hr_target(3)
            steps.append(_exec(o, "interval", "time", rmin * 60, tg, desc="brick run off the bike @ race effort")); o += 1
    elif disc == "run":
        body, o = _run_steps(main or "", intensity, thr_pace, o, name); steps.extend(body)
    elif swim:
        body, o = _swim_steps(main or "", o)
        if not body:
            body = _swim_fallback(o, "interval", main or name, 1000); o += len(body)
        steps.extend(body)
    else:  # strength / other
        steps.append(_exec(o, "interval", "time", (sess.get("duration_min") or 40) * 60, _no_target(),
                           desc=(main or name)[:120])); o += 1

    # Cooldown
    if cool:
        if swim:
            body, o = _swim_steps(cool, o, kind="cooldown")
            if not body:
                body = _swim_fallback(o, "cooldown", cool, 200); o += len(body)
            steps.extend(body)
        elif _mins(cool):
            steps.append(_exec(o, "cooldown", "time", _mins(cool) * 60, _no_target(), desc=cool[:120])); o += 1

    sport = _sport(disc)
    segment: dict[str, Any] = {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
    if swim:
        segment["poolLength"] = 25.0
        segment["poolLengthUnit"] = _METER_UNIT
    return {
        "workoutName": name, "description": desc[:1024], "sportType": sport,
        "estimatedDurationInSecs": int((sess.get("duration_min") or 60) * 60),
        "workoutSegments": [segment],
    }


def push(sess: dict[str, Any], date_str: str) -> dict[str, Any]:
    thr = _threshold_pace_sec() if (sess.get("discipline") in ("run", "brick")) else None
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
