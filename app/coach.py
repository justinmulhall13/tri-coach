"""Conversational coach agent (Anthropic Messages API).

Every user turn is sent with full injected context: athlete profile, race
phase, today's readiness, recent load, today's calendar, the current plan,
today's suggested workout, and any logged constraints. The model never sees
fabricated data — missing Garmin metrics are passed through as "missing".

Persona: direct, evidence-based, no glazing. Names tradeoffs honestly, flags
overreaching / illness / unsafe ramp, and says when data is missing rather
than guessing.

If the coach proposes a concrete change to today's session, it appends a
fenced ```adjustment JSON block; the backend extracts it so the UI can offer
"accept", which persists it as today's plan (source="coach").
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any

from . import config, db, garmin_source, suggest, zones

_SYSTEM = """You are Coach Steve, a triathlon coach for a single athlete preparing \
for a T100 triathlon (2.0 km swim / 80 km bike / 18 km run). You are direct, \
evidence-based, and do not flatter. The athlete has explicitly asked for honesty \
over motivation.

Hard rules:
- Ground every claim about the athlete's body in the Garmin data provided in the \
context block. Never invent a number. If a metric is listed as missing/null, say \
it is missing — do not estimate it.
- `todays_readiness.training_readiness` is the MORNING post-sleep recovery — the stable \
"how recovered did the athlete wake up" value, and the one you judge the day's readiness by. \
`todays_readiness.current_readiness` is a LIVE value that drops after training \
(input_context AFTER_POST_EXERCISE_RESET). NEVER treat the post-exercise number as the \
morning readiness: if the athlete woke at 90/WELL_RECOVERED, they woke dialed, even if the \
current value reads low after a hard session. A multisport/brick session appears as its individual \
legs in `recent_activities` (each row tagged `leg_label` bike/run/transition with its own HR & \
distance) — judge each leg on its own numbers, not a single blended figure.
- The stored training plan is the backbone. Readiness and constraints MODIFY today's \
session; they do not replace the periodization.
- COACH THE PHASE. `race.phase` + `race.days_remaining` tell you where the athlete is: \
BUILD (>28d out — accumulate volume + race-specific work), PEAK (15–28d — sharpen, top-end \
quality, highest load), TAPER (≤14d — CUT volume hard while keeping short race-pace touches; \
freshness beats fitness now), RACE week — rest, trust the work. When you adjust or rebuild, \
keep it consistent with the phase; as the race nears, bias toward the taper and protect it — \
do NOT add volume in the taper even if a load bucket looks short. Name the phase when relevant.
- If recovery is poor, say rest or downregulate and explain the cost of not doing so. \
If the athlete is behind for the distance (e.g. swim volume too low), say so plainly.
- Use `training_load_and_focus` (ACWR + Garmin load-focus distribution vs targets) and \
`recent_activities` to steer intensity. If a load bucket is short of target (e.g. \
aerobic-high shortage) or the ramp is unsafe (ACWR), say so and bias today's work \
accordingly. Reference specific recent sessions by date when relevant.
- Use `fitness_fatigue_form_pmc` (CTL/ATL/TSB) to judge freshness and taper timing, \
`personal_baselines` to read today's HRV/resting-HR/sleep as deviations from the athlete's \
own norm (not absolutes), and `proactive_signals` as pre-computed trend flags — reinforce or \
contextualize them, don't contradict them without cause.
- Flag overreaching, illness signals, or an unsafe load ramp when the data supports it.
- Prior conversation turns are private server-side memory. Use them for continuity even though
the athlete starts each visible app session with a clean chat screen. Dated entries in
`durable_coaching_memory` remain relevant until superseded, but treat explicitly temporary or
old constraints according to their date instead of assuming they last forever.

LOAD AND INTENSITY RULES (the athlete set these — follow them, don't soften them):
1. Prescribe load to an explicit TSB/form target and STATE that number in every plan. \
Do not default to conservative.
2. Race-day taper target is TSB +5 to +15. If your plan projects above +20 you have \
UNDERTRAINED them — add low-intensity volume and say so explicitly.
3. Long sessions may sit up to 8 days before an A race. Do not delete them for false safety.
4. When you cut volume, state what the cut COSTS and what it BUYS. Give the tradeoff, \
never bare reassurance.
5. Every easy session needs an explicit pace or HR CEILING, not a range — phrase it as \
"no faster than X". Running easy days too fast is this athlete's single biggest execution risk.
6. Hard days hard, easy days easy. Never prescribe a middle-ground session.
7. Prescribe BIKE work by heart rate, never watts. The 288 W FTP is Peloton-only and does \
not transfer outdoors; terrain is hilly, so judge rides on lap-average HR, not instantaneous. \
Watts may be mentioned ONLY for explicitly indoor/Peloton sessions, as a secondary cue.
8. Run volume is the exception to all of the above: the run base is thin and the Achilles is \
the limiter. NEVER jump run volume to hit a load target — add that volume on the bike instead.

Use `athlete_zones` for real numbers: it carries the athlete's actual Garmin HR zones \
(per sport — cycling zones sit lower than running), lactate-threshold HR and pace, and FTP. \
Quote real bpm and min/km from it; never invent zone boundaries or paces.
- OUTPUT FORMAT — NEVER reply in paragraphs. Every reply is a scannable TL;DR:
    • Line 1 is exactly "TL;DR: <the single bottom-line call, one sentence>".
    • Then 2–5 bullet lines, each "• <Label> — <one tight line>". Choose labels that fit the \
question (e.g. Readiness, Today, Do, Watch, Week, Swim, Fuel, Why).
    • Use a number only when it changes the athlete's decision or gives them an actionable target. \
Never dump a dashboard of metrics. When you use one, say why it matters in the same line \
(for example: "HR cap 143 — keeps this genuinely easy").
    • Compress hard but keep every crucial verdict and instruction. One line per bullet, no \
sub-bullets, no restating. Sound like a candid training partner: direct, grounded, and useful — \
not a clinical report or motivational poster.
    • Each bullet is ONE short clause, ~14 words max. If a bullet needs two sentences, split it \
into two bullets. Short labels (one word if possible).
    • Plain text only: no markdown bold/asterisks/headers (they render literally), no filler, \
no hype, no emoji. This whole format rule applies to the morning brief and nightly review too.

You CAN reprogram ANY day in the plan — today OR any upcoming day through race day \
(tomorrow's swim, Monday's session, etc.). If the athlete says a planned session is \
unrealistic or wrong (e.g. "5 km in 85 min isn't feasible at my pace"), reassess it against \
their real data and propose a realistic replacement for THAT day.

When you and the athlete settle on a concrete change to a specific day, append this block at \
the very end of your reply (and only then). Set "date" to that day's date (YYYY-MM-DD) — use \
`today` and the dates in `plan_next_7_days` to resolve "tomorrow"/"Monday"/etc. Only omit \
"date" if the change is for today.

```adjustment
{"date": "YYYY-MM-DD", "title": "...", "discipline": "swim|bike|run|brick|strength|recovery|rest", \
"duration_min": <int>, "intensity": "...", \
"structure": {"warmup": "...", "main": "...", "cooldown": "..."}, \
"why": "one sentence on the tradeoff"}
```
Only include the block when proposing an actual change; omit it for general questions.

WHOLE-WEEK REBUILD: when the athlete gives you their week's OUTLOOK — availability, \
travel, work hours, an event/race, "only mornings Tue–Thu", "rest day Friday", etc. — \
rebuild the affected days as a coherent week. Preserve the periodization INTENT (protect \
the key swim frequency and the weekend long sessions, respect the taper if close to race, \
don't cram) while fitting their real constraints. Output ONE fenced block below (instead of \
a single ```adjustment) with an array of day objects — include EVERY day you are changing \
(each needs a "date"); leave untouched days out. Your bullet reply should summarize the shape \
of the week; the block is the machine-readable version.

```weekplan
[{"date": "YYYY-MM-DD", "title": "...", "discipline": "...", "duration_min": <int>, \
"intensity": "...", "structure": {"warmup": "...", "main": "...", "cooldown": "..."}, \
"is_rest": 0, "why": "..."}, ...]
```

LOGGING AN ACTIVITY: when the athlete tells you they DID a session that isn't in the Garmin \
data yet ("I biked 30 km this morning", "just ran 5k easy", "did my lift"), record it by \
appending the block below so it's saved even before the watch syncs. Estimate missing numbers \
conservatively from what they said. Use ONE object per activity done. `sport` is \
swim|bike|run|strength|brick|other. Don't log intentions ("I'm going to…") — only completed work.

```logactivity
[{"date": "YYYY-MM-DD", "sport": "bike", "name": "Morning ride", "minutes": <int>, \
"km": <number or null>, "hr_avg": <int or null>, "notes": "how it felt"}]
```

CALENDAR COMMITMENT: when the athlete mentions a NON-training commitment or appointment with a \
time — "driving range tonight", "dinner at 7", "flying out Friday morning", "dentist Tuesday 2pm" \
— append the block below so it can be added to their calendar. Resolve the date from `today` and \
the weekday (e.g. "tonight" = today, "tomorrow" = today+1). Use a 24h "start" (HH:MM) and estimate \
a sensible "duration_min"; set "all_day": true and omit "start" for all-day things (travel days, \
"off Friday"). Still factor the commitment into your training advice in the reply. This is a \
PROPOSAL — the athlete taps to confirm; don't claim it's added.

```calendar_event
{"title": "Driving range", "date": "YYYY-MM-DD", "start": "18:00", "duration_min": 90, "all_day": false}
```

REMEMBER (decide yourself what matters): if the athlete shares a DURABLE, training-relevant fact — \
availability ("only mornings this week"), fatigue/niggles ("left achilles a bit sore"), travel, \
work constraints, equipment ("no pool access till Friday"), preferences — append the block below so \
it persists across coaching sessions. Keep it a short third-person fact. Do NOT remember one-off chatter, \
questions, greetings, or anything not useful for training decisions. Omit the block when nothing is \
worth keeping (most messages).

```remember
{"note": "Only has mornings free Tue–Thu this week"}
```"""


_TRANSIENT = ("RemoteProtocolError", "APIConnectionError", "ConnectionError",
              "ReadTimeout", "APITimeoutError", "InternalServerError", "OverloadedError")


def _stream_reply(max_tokens: int, messages: list[dict[str, Any]]) -> Any:
    """Stream a completion with a retry on transient network/streaming errors."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=2)
    last: Exception | None = None
    for _ in range(3):
        try:
            with client.messages.stream(
                model=config.COACH_MODEL, max_tokens=max_tokens, system=_SYSTEM,
                thinking={"type": "adaptive"}, messages=messages,
            ) as stream:
                return stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            last = e
            if type(e).__name__ in _TRANSIENT:
                continue
            raise
    raise last  # type: ignore[misc]


def _context_block() -> str:
    """Assemble the injected context. Best-effort per section; flags failures."""
    def safe(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    from . import baselines, fitness_trend, insights
    phase = config.race_phase()
    readiness = safe(garmin_source.get_readiness)
    load = safe(lambda: garmin_source.get_recent_load(14))
    training_load = safe(garmin_source.get_training_load)
    fitness = safe(garmin_source.get_fitness_markers)
    pmc = safe(lambda: fitness_trend.get_pmc(90))
    baseline = safe(baselines.get_baselines)
    signals = safe(insights.get_insights)
    sugg = safe(suggest.todays_suggestion)
    now = config.local_now()
    today = now.date().isoformat()
    week_end = (now.date() + datetime.timedelta(days=7)).isoformat()
    plan_week = db.get_plan(today, week_end)
    constraints = db.get_constraints(today)
    memory = db.get_constraint_history(200)
    all_acts = (load.get("activities") or []) if isinstance(load, dict) else []
    todays_done = [a for a in all_acts if (a.get("date") or "") == today]
    todays_plan = db.get_plan_day(today)

    payload = {
        "today": today,
        "local_time": now.strftime("%A %H:%M %Z"),
        "athlete_profile": config.ATHLETE_PROFILE,
        "athlete_zones": safe(zones.summary),
        "race": phase,
        "todays_readiness": readiness,
        "fitness_markers": fitness,
        "training_load_and_focus": training_load,
        "fitness_fatigue_form_pmc": {"current": pmc.get("current"),
                                     "interpretation": pmc.get("interpretation")}
                                    if isinstance(pmc, dict) else pmc,
        "personal_baselines": baseline.get("markers") if isinstance(baseline, dict) else baseline,
        "proactive_signals": signals.get("signals") if isinstance(signals, dict) else signals,
        "recent_load_14d_by_sport": load.get("by_sport") if isinstance(load, dict) else load,
        "recent_activities": all_acts[:8],
        "todays_planned_workout": {
            "discipline": todays_plan.get("discipline"), "title": todays_plan.get("title"),
            "duration_min": todays_plan.get("duration_min"), "intensity": todays_plan.get("intensity"),
            "structure": todays_plan.get("structure"),
        } if todays_plan else None,
        "todays_completed_activities": todays_done,
        "todays_suggested_workout": sugg,
        # Week shape only — omit each day's full warmup/main/cooldown (big, and the
        # coach regenerates structures when it rebuilds anyway). Today's full
        # structure is already provided above in `todays_planned_workout`.
        "plan_next_7_days": [
            {"date": d["date"], "discipline": d["discipline"], "title": d["title"],
             "duration_min": d["duration_min"], "intensity": d.get("intensity"),
             "phase": d["phase"]}
            for d in plan_week
        ],
        "logged_constraints_today": [c["text"] for c in constraints],
        "durable_coaching_memory": [
            {"date": c["date"], "fact": c["text"]} for c in memory
        ],
    }
    return json.dumps(payload, indent=2, default=str)


_ADJ_RE = re.compile(r"```adjustment\s*(\{.*?\})\s*```", re.DOTALL)
_WEEK_RE = re.compile(r"```weekplan\s*(\[.*?\])\s*```", re.DOTALL)
_ACT_RE = re.compile(r"```logactivity\s*(\[.*?\]|\{.*?\})\s*```", re.DOTALL)
_EVENT_RE = re.compile(r"```calendar_event\s*(\{.*?\})\s*```", re.DOTALL)
_REMEMBER_RE = re.compile(r"```remember\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_remember(text: str) -> str | None:
    """Parse a ```remember block — a durable, training-relevant fact the coach
    decided is worth keeping (replaces the old manual 'log' checkbox)."""
    m = _REMEMBER_RE.search(text)
    if not m:
        return None
    try:
        note = (json.loads(m.group(1)) or {}).get("note")
    except json.JSONDecodeError:
        return None
    note = (note or "").strip()
    return note or None


def _extract_event(text: str) -> dict[str, Any] | None:
    """Parse a ```calendar_event block (a personal commitment to add to the
    calendar). Proposed only — the UI confirms before it's created."""
    m = _EVENT_RE.search(text)
    if not m:
        return None
    try:
        ev = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict) or not ev.get("title") or not ev.get("date"):
        return None
    return ev


def _extract_activities(text: str) -> list[dict[str, Any]] | None:
    """Parse a ```logactivity block (activities the athlete reports having done)."""
    m = _ACT_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else [data]
    out = [a for a in items if isinstance(a, dict) and a.get("sport")]
    return out or None


def _extract_adjustment(text: str) -> dict[str, Any] | None:
    m = _ADJ_RE.search(text)
    if not m:
        return None
    try:
        adj = json.loads(m.group(1))
        return adj if isinstance(adj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_weekplan(text: str) -> list[dict[str, Any]] | None:
    """Parse a ```weekplan array of day objects (whole-week rebuild)."""
    m = _WEEK_RE.search(text)
    if not m:
        return None
    try:
        days = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(days, list):
        return None
    out = [d for d in days if isinstance(d, dict) and d.get("date")]
    return out or None


def chat(user_message: str, log_as_constraint: bool = False) -> dict[str, Any]:
    """Send one coach turn with full context. Returns reply + any proposed adjustment.
    The coach now decides itself what's worth remembering (via a ```remember block),
    so no manual 'log' toggle is needed; `log_as_constraint` is kept for compatibility."""
    key = config.ANTHROPIC_API_KEY
    if not key or key.strip() in ("", "sk-ant-...") or key.strip().endswith("..."):
        return {
            "error": "ANTHROPIC_API_KEY is not set (still the .env placeholder)",
            "hint": "Put your real key in coach/.env as ANTHROPIC_API_KEY=sk-ant-..., then retry.",
        }

    import anthropic

    today = config.local_today().isoformat()

    # The visible UI starts fresh each time the app launches, but Steve's context
    # does not. Keep a generous private window of prior turns across days and trim
    # only if it would crowd out current training data/model reasoning.
    history = db.get_chat(limit=160)
    kept, chars = [], 0
    for turn in reversed(history):
        size = len(turn.get("content") or "")
        if kept and chars + size > 80_000:
            break
        kept.append(turn)
        chars += size
    history = list(reversed(kept))
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    # The morning brief is a standalone assistant message; the API requires the
    # first message to be 'user', so drop any leading assistant turns.
    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    context = _context_block()
    messages.append({
        "role": "user",
        "content": f"<context>\n{context}\n</context>\n\n{user_message}",
    })

    try:
        msg = _stream_reply(8000, messages)   # room for thinking + a full-week rebuild block
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "source": "anthropic"}

    reply = "".join(b.text for b in msg.content if b.type == "text").strip()
    adjustment = _extract_adjustment(reply)
    week = _extract_weekplan(reply)
    activities = _extract_activities(reply)
    event = _extract_event(reply)
    remember = _extract_remember(reply)
    # The coach decides what's worth keeping — log durable training constraints itself.
    if remember:
        db.add_constraint(today, remember)
    # Strip the machine blocks from the human-facing reply.
    reply_clean = _REMEMBER_RE.sub("", _EVENT_RE.sub("", _ACT_RE.sub("", _WEEK_RE.sub("", _ADJ_RE.sub("", reply))))).strip()

    # Never surface a blank "(no reply)". If the model returned only a machine block
    # (or ran out of tokens mid-thought), give a sensible confirmation — the proposal
    # card renders below it anyway.
    if not reply_clean:
        if activities:
            n = len(activities)
            reply_clean = f"Logged {n} session{'s' if n != 1 else ''}. ✓"
        elif week:
            reply_clean = "Reshaped your week — review and accept it below."
        elif event:
            reply_clean = "Want me to add that to your calendar? Confirm below."
        elif adjustment:
            reply_clean = "Here's the change — accept it below to lock it in."
        elif msg.stop_reason == "max_tokens":
            reply_clean = "I ran long on that one — ask me to continue and I'll finish the thought."
        else:
            reply_clean = "Got it."

    # Persist the visible exchange (cleaned text, not raw blocks; not the context blob).
    db.add_chat("user", user_message)
    db.add_chat("assistant", reply_clean)

    return {
        "reply": reply_clean,
        "proposed_adjustment": adjustment,
        "proposed_week": week,
        "proposed_activities": activities,
        "proposed_event": event,
        "model": msg.model,
        "stop_reason": msg.stop_reason,
    }


_BRIEF_INSTRUCTION = """I just opened my dashboard. Look at `todays_completed_activities` \
and `todays_planned_workout`, then give me a tight, unprompted status brief — no preamble.

- If I have ALREADY trained today (todays_completed_activities is non-empty): EVALUATE it. \
Did I complete the planned session, a modified version, or something different? Judge the \
actual numbers (distance, duration, HR, power/pace) against the target. Tell me how it went, \
whether it hit the intent, and what it means for the rest of the week. If a second session \
is still planned today and I haven't done it, say what's left.
- If I have NOT trained yet today: give the morning briefing — readiness read with the key \
signal, today's planned session and its main target, and the single most important thing to nail, \
drawing on my load focus and where I'm behind for the race.

Use the TL;DR + bullets format from your rules (TL;DR line, then 2–5 labeled bullets). \
No paragraphs, no adjustment block."""


_NIGHTLY_INSTRUCTION = """It's evening — give me my nightly review of TODAY, no preamble.

Use `local_time`, `todays_completed_activities`, `todays_planned_workout`, and today's \
readiness/load context.
- Recap what I actually trained today vs what was planned. Judge the real numbers \
(distance, duration, HR, power/pace) against the target — did I hit the intent, over/under-do it?
- If I did NOT train today and a session was planned, say so plainly and whether it's worth \
salvaging tomorrow or writing off.
- Give one honest takeaway on where today leaves me for the week and the race, and one \
concrete thing to prioritize tomorrow (including sleep/recovery if the markers warrant it).

Use the TL;DR + bullets format from your rules (TL;DR line, then 2–5 labeled bullets). \
No paragraphs, no adjustment block."""


def _is_evening(now: datetime.datetime) -> bool:
    """After 20:30 local (through end of day) → nightly review instead of a brief."""
    return (now.hour, now.minute) >= (20, 30)


def morning_brief() -> dict[str, Any]:
    """Generate (and persist) an unprompted briefing as an assistant message.

    Morning/daytime → a look-ahead brief (or post-workout evaluation); after
    20:30 local → a nightly review of the day. Called when today's chat is empty
    or today's training state changes.
    """
    key = config.ANTHROPIC_API_KEY
    if not key or key.strip().endswith("..."):
        return {"error": "ANTHROPIC_API_KEY is not set (still the .env placeholder)"}

    instruction = _NIGHTLY_INSTRUCTION if _is_evening(config.local_now()) else _BRIEF_INSTRUCTION
    context = _context_block()
    messages = [{"role": "user",
                 "content": f"<context>\n{context}\n</context>\n\n{instruction}"}]
    try:
        msg = _stream_reply(5000, messages)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "source": "anthropic"}

    reply = "".join(b.text for b in msg.content if b.type == "text").strip()
    reply = _ADJ_RE.sub("", reply).strip()
    if not reply:
        return {"error": f"empty reply (stop_reason={msg.stop_reason})", "source": "anthropic"}
    db.add_chat("assistant", reply)  # standalone greeting; chat() trims leading assistant turns
    return {"reply": reply, "model": msg.model}


def accept_adjustment(adjustment: dict[str, Any]) -> dict[str, Any] | None:
    """Persist an accepted coach adjustment to its target day (source='coach').

    Honors an optional 'date' (YYYY-MM-DD) so the coach can reprogram tomorrow's
    swim or any upcoming day — not just today. Past dates or bad values fall back
    to today."""
    today = config.local_today()
    raw = (adjustment.get("date") or "").strip()
    try:
        d = datetime.date.fromisoformat(raw)
        date = d.isoformat() if d >= today else today.isoformat()
    except (ValueError, TypeError):
        date = today.isoformat()
    fields = {k: adjustment[k] for k in
              ("title", "discipline", "duration_min", "intensity", "structure", "why")
              if k in adjustment}
    return db.edit_plan_day(date, fields, source="coach")


def accept_weekplan(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist a whole-week rebuild — one edit_plan_day per day (source='coach').
    Only today-or-future days are written; each becomes a coach day a reseed keeps."""
    today = config.local_today()
    updated: list[dict[str, Any]] = []
    skipped: list[str] = []
    for d in days or []:
        raw = (d.get("date") or "").strip()
        try:
            dt = datetime.date.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if dt < today:
            skipped.append(raw)
            continue
        fields = {k: d[k] for k in
                  ("title", "discipline", "duration_min", "intensity", "structure", "why", "is_rest")
                  if k in d}
        row = db.edit_plan_day(dt.isoformat(), fields, source="coach")
        if row:
            updated.append(row)
    db.set_meta("brief_sig", "")   # force a re-brief against the reshaped week
    return {"ok": bool(updated), "updated_days": updated, "count": len(updated), "skipped": skipped}


_SPORTS = {"swim", "bike", "run", "strength", "brick", "other"}


def log_activities(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """Record coach-reported activities the athlete did (before/without a Garmin sync)."""
    today = config.local_today().isoformat()
    added = []
    for a in activities or []:
        sport = (a.get("sport") or "other").lower()
        if sport not in _SPORTS:
            sport = "other"
        raw = (a.get("date") or "").strip()
        try:
            date = datetime.date.fromisoformat(raw).isoformat()
        except (ValueError, TypeError):
            date = today

        def num(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        def integer(v):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return None

        eid = db.add_manual_activity(
            date, sport, name=str(a.get("name") or f"{sport} session")[:120],
            km=num(a.get("km")), minutes=num(a.get("minutes")),
            hr_avg=integer(a.get("hr_avg")), notes=str(a.get("notes") or "")[:300])
        added.append(eid)
    db.set_meta("brief_sig", "")   # so the coach re-evaluates with the new activity
    return {"ok": bool(added), "count": len(added)}
