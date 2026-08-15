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
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import (athlete_guide, coaching_contract, config, db,
               fueling_reference, garmin_source, hevy_connector,
               interval_analysis, rings, suggest, zones)

_SYSTEM = coaching_contract.system_prompt() + """

You are Coach Steve, an endurance coach for a single athlete. The installed event and mode \
are defined only by the COACHING CONTRACT above. You are direct, \
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
quality, highest load), TAPER (≤14d: cut volume while keeping short race-pace touches), \
RACE week: introduce nothing new. When you adjust or rebuild, keep it consistent with the phase. \
The explicit TSB rules in the coaching contract govern taper volume, including the requirement \
to add low-intensity volume if projected race-day TSB is above +20. Name the phase when relevant.
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
- The athlete's newest correction overrides your earlier interpretation. If they say an activity
was double-synced, a quantity meant table salt rather than sodium, or "the run" meant the race
run rather than tomorrow's run, STOP carrying the old assumption. State the corrected scope once
and recompute from the raw quantities. Never defend or repeat a number that depended on the
discarded assumption.
- Garmin can receive the same workout from two recording systems. Entries marked
`deduplicated_sync_count` are one workout, not multiple sessions; never restore the ignored copy
into totals. If the athlete flags an unmarked duplicate, accept the correction and reason from one
session unless independent timing data proves they are separate.
- A structured interval workout MUST be graded from `todays_interval_execution`, not from the
whole-activity average HR. That average deliberately blends warm-up, recoveries and cool-down.
Read every ACTIVE interval: its prescribed duration, duration-weighted average/max HR, and minutes
below/in/above the prescribed band. Allow for normal HR rise during the opening portion of a work
bout. Never claim the work "didn't land" merely because the session average is below the interval
target. The associated Garmin workout is the authoritative prescription actually performed. If
`structured_target_mismatch` exists, identify it as an app/workout-construction error, never an
athlete execution failure.
- Completing the configured goal race is different from completing an ordinary workout. When
`goal_race_completion.celebration_pending` is true, CELEBRATE
first and enthusiastically. Name the race and use the recorded swim/bike/run facts so it feels
earned and specific. Do not lead with critique, load balance, what was missed, or the next training
block. A short evidence-based reflection and immediate recovery guidance can follow the celebration.

FUELING AND ATHLETE-GUIDE RULES:
1. `vancouver_athlete_guide` contains page-linked facts from the supplied race guide. Separate
guide facts from coaching inference. The newer self-reported EVENT PROFILE wins where they conflict,
but name the conflict when it changes logistics. Never invent a serving size or product variant.
2. On every fueling question, resolve the scope FIRST: training versus race, then swim/bike/run,
plus expected duration. Read the last turns so a short correction such as "only the run" retains
its conversational referent. If one unknown can flip the answer, ask one focused question instead
of guessing.
3. Follow `fueling_reference.fuel_audit_contract`. Inventory every bottle/flask/gel separately,
then show totals AND hourly rates for carbohydrate, sodium, fluid and caffeine. Preserve label
values supplied by the athlete; do not replace them with generic tablespoon estimates.
4. TABLE SALT and SODIUM are different units. Use the athlete's fixed factors exactly: table salt
x 0.39 = sodium by mass; 1 tsp table salt = about 6 g salt = about 2,360 mg sodium, so 1/2 tsp
x 2,360 mg/tsp = about 1,180 mg sodium. If the unit is ambiguous, clarify it.
5. A concentrate flask deliberately chased with water is not judged by the flask concentration
alone. Evaluate the dose plus the water around it and total hourly delivery. Do not call it
dangerously hypertonic without that combined-fluid calculation and evidence.
6. Do not diagnose cramps as sodium deficiency. Use the supplied 800-900 mg sodium/L and roughly
1 L/h self-reported sweat rate, label them self-reported, and do not stack magnesium.
7. When the active EVENT PROFILE contains a bike leg, put carbohydrate fuel on the bike rather
than the run. When it has no bike leg, front-load early and expect the final third to be
tolerance-limited. Never import placement from a previous profile.
8. The athlete's gels are 23 g carbohydrate and 20 mg caffeine each. Count all sources in mg and
mg/kg using the dated self-reported Garmin weight entry, or label the 86 kg fallback self-reported.
9. An aid station is an opportunity, not an automatic dose. Calculate the total number of
flask doses/gels needed across the leg, then place exactly those doses on the course. The final
station-by-station call must reconcile with the displayed total and hourly math; never say both
"gel at every non-flask station" and "three gels total."

LOAD AND INTENSITY RULES (the athlete set these; follow them without softening):
1. Prescribe load to an explicit TSB/form target and STATE that number in every plan. \
Do not default to conservative.
2. Race-day taper target is TSB +5 to +15. If your plan projects above +20 you have \
UNDERTRAINED them; add low-intensity volume and say so explicitly.
3. Long sessions may sit up to 8 days before an A race. Do not delete them for false safety.
4. When you cut volume, state what the cut COSTS and what it BUYS. Give the tradeoff, \
never bare reassurance.
5. Every easy session needs an explicit pace or HR CEILING, not a range; phrase it as \
"no faster than X". Running easy days too fast is this athlete's single biggest execution risk.
6. Hard days hard, easy days easy. Never prescribe a middle-ground session.
7. Prescribe BIKE work by heart rate, never watts. The 288 W FTP is Peloton-only and does \
not transfer outdoors; terrain is hilly, so judge rides on lap-average HR, not instantaneous. \
Watts may be mentioned ONLY for explicitly indoor/Peloton sessions, as a secondary cue.
8. Run volume is the exception to all of the above: the run base is thin and the Achilles is \
the limiter. NEVER jump run volume to hit a load target; add that volume on the bike instead.

LIFTING AND HEVY:
- A lifting question does not change event mode. Do not insert lifting unprompted, but fully handle it
when asked. Give exercise order, sets, reps or duration, rest, and effort; never invent working weight.
- Read `strength_training_source`. If Hevy is disconnected, its recent history is unknown, not empty.
Ask for unknown equipment, injuries, or working sets that materially change the prescription.
- A future Hevy routine is a prescription. A Hevy workout is completion evidence. Never claim either
was created from chat text; writes require resolved exercise-template IDs and explicit confirmation.

Use `athlete_zones` for real numbers: it carries the athlete's actual Garmin HR zones \
(per sport — cycling zones sit lower than running), lactate-threshold HR and pace, and FTP. \
Quote real bpm and min/km from it; never invent zone boundaries or paces.
- OUTPUT FORMAT: NEVER reply in paragraphs. Every reply is a scannable TL;DR:
    • Line 1 is exactly "TL;DR: <the single bottom-line call, one sentence>".
    • Then 2–5 bullet lines, each "• <Label>: <one tight line>". Choose labels that fit the \
    question (e.g. Readiness, Today, Do, Watch, Week, Swim, Fuel, Why).
    • Use a number only when it changes the athlete's decision or gives them an actionable target. \
Never dump a dashboard of metrics. When you use one, say why it matters in the same line \
(for example: "HR cap 143: keeps this genuinely easy").
    • Compress hard but keep every crucial verdict and instruction. One line per bullet, no \
sub-bullets, no restating. Sound like a candid training partner: direct, grounded, and useful, \
not a clinical report or motivational poster.
    • Each bullet is ONE short clause, ~14 words max. If a bullet needs two sentences, split it \
    into two bullets. Short labels (one word if possible). Fueling audits may use up to 8 concise \
    bullets with equations. A requested lifting prescription may use enough concise bullets to \
    give every exercise's order, sets, reps or duration, rest, and effort without omitting fields.
    • Plain text only: no markdown bold/asterisks/headers (they render literally), no filler, \
no hype, no emoji, and no em dashes. This applies to morning briefs and nightly reviews too.

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
"duration_min": <int>, "intensity": "...", "tsb_target": <explicit race-day TSB number>, \
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
"intensity": "...", "tsb_target": <explicit race-day TSB number>, \
"structure": {"warmup": "...", "main": "...", "cooldown": "..."}, \
"is_rest": 0, "why": "..."}, ...]
```

LOGGING AN ACTIVITY: when the athlete tells you they DID a session that isn't in the Garmin \
data yet ("I biked 30 km this morning", "just ran 5k easy", "did my lift"), record it by \
appending the block below so it is saved before the watch syncs. Keep missing numbers null; never \
estimate them. Ask if a missing value is required for the decision. Use ONE object per activity. `sport` is \
swim|bike|run|strength|brick|other. Don't log intentions ("I'm going to…") — only completed work.

```logactivity
[{"date": "YYYY-MM-DD", "sport": "bike", "name": "Morning ride", "minutes": <int>, \
"km": <number or null>, "hr_avg": <int or null>, "notes": "how it felt"}]
```

CALENDAR COMMITMENT: when the athlete mentions a NON-training commitment or appointment with a \
time — "driving range tonight", "dinner at 7", "flying out Friday morning", "dentist Tuesday 2pm" \
— append the block below so it can be added to their calendar. Resolve the date from `today` and \
the weekday (e.g. "tonight" = today, "tomorrow" = today+1). Use a self-reported 24h "start" \
(HH:MM). For a timed event, duration_min must also be explicitly self-reported; if it is unknown, \
say unknown and ask instead of emitting a block. Set "all_day": true only when the athlete explicitly \
said all day and omit "start" then. Still factor the commitment into training advice. This is a \
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

# Do not prime the model with the punctuation the athlete banned from output.
_SYSTEM = _SYSTEM.replace(" — ", ": ").replace("—", "-")


_TRANSIENT = ("RemoteProtocolError", "APIConnectionError", "ConnectionError",
              "ReadTimeout", "APITimeoutError", "InternalServerError", "OverloadedError")

_CLIENT: Any = None
_CLIENT_LOCK = threading.Lock()
_CONTEXT_CACHE: dict[str, Any] = {"at": 0.0, "date": None, "data": None}
_CONTEXT_LOCK = threading.Lock()
_CONTEXT_TTL_S = 90


def _anthropic_client() -> Any:
    """Reuse HTTP/TLS connections instead of creating a client per message."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                import anthropic
                _CLIENT = anthropic.Anthropic(
                    api_key=config.ANTHROPIC_API_KEY,
                    max_retries=2,
                )
    return _CLIENT


def _message_kwargs(max_tokens: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Shared low-latency request shape for chat and event briefs."""
    return {
        "model": config.COACH_MODEL,
        "max_tokens": max_tokens,
        "system": [{
            "type": "text",
            "text": _SYSTEM,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": config.COACH_EFFORT},
        # Automatic caching keeps the stable system/history prefix hot while
        # the final live-context/user turn changes on each message.
        "cache_control": {"type": "ephemeral"},
        "messages": messages,
    }


def _stream_reply(max_tokens: int, messages: list[dict[str, Any]]) -> Any:
    """Stream a completion with a retry on transient network/streaming errors."""
    client = _anthropic_client()
    last: Exception | None = None
    for _ in range(3):
        try:
            with client.messages.stream(**_message_kwargs(max_tokens, messages)) as stream:
                return stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            last = e
            if type(e).__name__ in _TRANSIENT:
                continue
            raise
    raise last  # type: ignore[misc]


def invalidate_context_cache() -> None:
    """Force the next Coach request to pull a fresh Garmin snapshot."""
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE.update({"at": 0.0, "date": None, "data": None})


def prime_context_cache(**sections: Any) -> None:
    """Seed Coach with Garmin data the dashboard already fetched."""
    usable = {name: value for name, value in sections.items() if value is not None}
    if not usable:
        return
    today = config.local_today().isoformat()
    with _CONTEXT_LOCK:
        existing = (_CONTEXT_CACHE.get("data") or {}) if _CONTEXT_CACHE.get("date") == today else {}
        _CONTEXT_CACHE.update({
            "at": time.monotonic(),
            "date": today,
            "data": {**existing, **usable},
        })


def _live_context(safe) -> dict[str, Any]:
    """Fetch independent Garmin sections in parallel and briefly reuse them.

    A normal conversation sends several follow-ups against the same workout and
    recovery state. Re-querying Garmin for every follow-up was both slower and
    less reliable, while a short cache keeps the context effectively live.
    """
    today = config.local_today().isoformat()
    now = time.monotonic()
    with _CONTEXT_LOCK:
        cache_is_fresh = (_CONTEXT_CACHE.get("date") == today
                          and now - float(_CONTEXT_CACHE.get("at") or 0) < _CONTEXT_TTL_S)
        cached = dict(_CONTEXT_CACHE.get("data") or {}) if cache_is_fresh else {}

        from . import fitness_trend
        calls = {
            "readiness": garmin_source.get_readiness,
            "load": lambda: garmin_source.get_recent_load(14),
            "training_load": garmin_source.get_training_load,
            "fitness": garmin_source.get_fitness_markers,
            "weight": garmin_source.get_weight_kg,
            "pmc": lambda: fitness_trend.get_pmc(90),
            "zones": zones.summary,
        }
        missing = {name: fn for name, fn in calls.items() if name not in cached}
        if not missing:
            return cached
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            futures = {name: pool.submit(safe, fn) for name, fn in missing.items()}
            data = {**cached, **{name: future.result() for name, future in futures.items()}}
        _CONTEXT_CACHE.update({"at": time.monotonic(), "date": today, "data": data})
        return data


def _goal_race_completion(activities: list[dict[str, Any]], race: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize the configured A-race from its Garmin swim/bike/run recordings.

    Native Garmin multisport children share a parent id. A fallback handles three
    separate recordings, but requires substantial distance in every discipline so
    a race-morning shakeout cannot trigger the finish celebration.
    """
    race_date = race.get("date")
    rows = [a for a in activities if a.get("date") == race_date]
    if not rows:
        return None

    distances = race.get("distances") or {}
    try:
        minimums = {
            # Allow normal GPS/course variance, but never celebrate a DNF or a
            # short race-morning multisport as the configured goal finish.
            sport: float(distances[f"{sport}_km"]) * 0.9
            for sport in ("swim", "bike", "run")
        }
    except (KeyError, TypeError, ValueError):
        # A finish cannot be identified safely without every configured leg.
        # Never import T100 defaults into another triathlon profile.
        minimums = {}

    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        parent = row.get("multisport_parent")
        if parent is not None:
            grouped.setdefault(parent, []).append(row)

    candidate: list[dict[str, Any]] | None = None
    detected_from = None
    for group in grouped.values():
        by_sport = {
            sport: [a for a in group if a.get("sport") == sport]
            for sport in ("swim", "bike", "run")
        }
        if minimums and all(
            sum(float(a.get("km") or 0) for a in by_sport[sport]) >= minimums[sport]
            for sport in by_sport
        ):
            candidate = group
            detected_from = "Garmin multisport recording"
            break

    if candidate is None:
        # The fallback is for truly separate leg recordings only. Native
        # multisport children were already evaluated as one parent above and
        # must not be counted again alongside a duplicate recorder. Also use
        # one plausible recording per sport rather than summing independent
        # rows, which could turn duplicated partial legs into a fake finish.
        standalone = [a for a in rows if a.get("multisport_parent") is None]
        by_sport = {sport: [a for a in standalone if a.get("sport") == sport]
                    for sport in ("swim", "bike", "run")}
        if minimums and all(
            max((float(a.get("km") or 0) for a in by_sport[sport]), default=0) >= minimums[sport]
            for sport in by_sport
        ):
            candidate = [
                max(by_sport[sport], key=lambda a: float(a.get("km") or 0))
                for sport in ("swim", "bike", "run")
            ]
            detected_from = "separate Garmin race-day recordings"

    if not candidate:
        return None

    ordered = sorted(candidate, key=lambda a: (a.get("leg") or 99, a.get("start_local") or ""))
    race_legs = [a for a in ordered if a.get("sport") in {"swim", "bike", "run"}]
    return {
        "completed": True,
        "race_name": race.get("name"),
        "race_date": race_date,
        "detected_from": detected_from,
        "multisport_parent": next((a.get("multisport_parent") for a in ordered
                                    if a.get("multisport_parent") is not None), None),
        "total_elapsed_min": round(sum(float(a.get("minutes") or 0) for a in ordered), 1),
        "legs": [
            {
                "sport": a.get("sport"),
                "distance_km": a.get("km"),
                "duration_min": a.get("minutes"),
                "avg_hr": a.get("hr_avg"),
                "max_hr": a.get("hr_max"),
                "pace_min_km": a.get("pace_min_km"),
                "avg_power_w": a.get("avg_power_w"),
            }
            for a in race_legs
        ],
    }


def _context_block(user_query: str = "") -> str:
    """Assemble the injected context. Best-effort per section; flags failures."""
    def safe(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    from . import baselines, insights
    phase = config.race_phase()
    live = _live_context(safe)
    readiness = live["readiness"]
    load = live["load"]
    training_load = live["training_load"]
    fitness = live["fitness"]
    weight = live["weight"]
    pmc = live["pmc"]
    baseline = safe(baselines.get_baselines)
    ready_score = (((readiness.get("training_readiness") or {}).get("score"))
                   if isinstance(readiness, dict) else None)
    ring_context = ({"t100": rings.t100_readiness(
        load if isinstance(load, dict) else {},
        training_load if isinstance(training_load, dict) else {},
        ready_score,
        phase.get("days_remaining", 0),
    )} if config.supports_t100_features() else {})
    signals = safe(lambda: insights.get_insights(
        baseline_data=baseline if isinstance(baseline, dict) else {},
        pmc_data=pmc if isinstance(pmc, dict) else {},
        training_load_data=training_load if isinstance(training_load, dict) else {},
        rings_data=ring_context,
    ))
    sugg = safe(lambda: suggest.todays_suggestion(
        readiness if isinstance(readiness, dict) else {},
        load if isinstance(load, dict) else {},
    ))
    now = config.local_now()
    today = now.date().isoformat()
    week_end = (now.date() + datetime.timedelta(days=7)).isoformat()
    plan_week = db.get_plan(today, week_end)
    constraints = db.get_constraints(today)
    memory = db.get_constraint_history(200)
    all_acts = (load.get("activities") or []) if isinstance(load, dict) else []
    todays_done = [a for a in all_acts if (a.get("date") or "") == today]
    todays_plan = db.get_plan_day(today)
    todays_intervals = []
    for activity in todays_done:
        candidate_ids = [activity.get("activity_id"), *(activity.get("deduplicated_activity_ids") or [])]
        execution = None
        # A duplicate pair may contain one native Garmin recording (with workout
        # steps) and one trainer sync (without them), so try every collapsed id.
        for candidate in dict.fromkeys(i for i in candidate_ids if i):
            execution = safe(lambda candidate=candidate: interval_analysis.get(int(candidate)))
            if execution and not execution.get("error"):
                break
        if execution and not execution.get("error"):
            todays_intervals.append(execution)
    race_completion = (_goal_race_completion(all_acts, phase)
                       if coaching_contract.current_mode() == "TRIATHLON" else None)
    if race_completion:
        celebrated_key = coaching_contract.scoped_meta_key(
            f"race_finish_celebrated_{race_completion['race_date']}"
        )
        race_completion["celebration_pending"] = not bool(db.get_meta(celebrated_key))

    if not isinstance(weight, dict) or not weight.get("kg"):
        fallback_kg = float(coaching_contract.ATHLETE_CONSTANTS["body_mass_fallback"]["value"])
        weight = {
            "kg": fallback_kg,
            "lb": round(fallback_kg * 2.2046, 1),
            "as_of": None,
            "source": "self-reported",
            "provider": None,
            "fallback_reason": "Latest dated Garmin weight entry is unknown",
            "conversion": (
                f"{fallback_kg:g} kg x 2.2046 lb/kg = "
                f"{round(fallback_kg * 2.2046, 1):g} lb"
            ),
        }

    payload = {
        "today": today,
        "local_time": now.strftime("%A %H:%M %Z"),
        "coaching_contract": {
            "current_mode": coaching_contract.current_mode(),
            "athlete_constants": coaching_contract.athlete_context(),
            "event_profile": coaching_contract.event_context(),
        },
        "input_provenance": {
            "measured": ["Garmin readiness, sleep, HRV, resting HR, activities, and zones"],
            "self_reported": ["athlete constants", "event profile", "logged constraints", "manual activities", "dated athlete-maintained Garmin weight entry"],
            "assumed": ["only values explicitly labelled assumed inside a section"],
        },
        "athlete_profile": config.ATHLETE_PROFILE,
        "athlete_weight": weight,
        "athlete_zones": live["zones"],
        "race": phase,
        "goal_race_completion": race_completion,
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
            "tsb_target": todays_plan.get("tsb_target"),
            "structure": todays_plan.get("structure"),
        } if todays_plan else None,
        "todays_completed_activities": todays_done,
        "todays_interval_execution": todays_intervals,
        "todays_suggested_workout": sugg,
        # Week shape only — omit each day's full warmup/main/cooldown (big, and the
        # coach regenerates structures when it rebuilds anyway). Today's full
        # structure is already provided above in `todays_planned_workout`.
        "plan_next_7_days": [
            {"date": d["date"], "discipline": d["discipline"], "title": d["title"],
             "duration_min": d["duration_min"], "intensity": d.get("intensity"),
             "tsb_target": d.get("tsb_target"),
             "phase": d["phase"]}
            for d in plan_week
        ],
        "logged_constraints_today": [c["text"] for c in constraints],
        "durable_coaching_memory": [
            {"date": c["date"], "fact": c["text"]} for c in memory
        ],
    }
    guide = (athlete_guide.context_for(user_query)
             if coaching_contract.EVENT_PROFILE.get("athlete_guide_key") == "vancouver-2026"
             else None)
    if guide:
        payload["vancouver_athlete_guide"] = guide
    if fueling_reference.is_fueling_query(user_query):
        payload["fueling_reference"] = fueling_reference.context()
    strength = hevy_connector.context_for(user_query)
    if strength:
        payload["strength_training_source"] = strength
    # Compact JSON preserves every field while reducing prompt bytes/tokens and
    # therefore input-processing time.
    return json.dumps(payload, separators=(",", ":"), default=str)


_ADJ_RE = re.compile(r"```adjustment\s*(\{.*?\})\s*```", re.DOTALL)
_WEEK_RE = re.compile(r"```weekplan\s*(\[.*?\])\s*```", re.DOTALL)
_ACT_RE = re.compile(r"```logactivity\s*(\[.*?\]|\{.*?\})\s*```", re.DOTALL)
_EVENT_RE = re.compile(r"```calendar_event\s*(\{.*?\})\s*```", re.DOTALL)
_REMEMBER_RE = re.compile(r"```remember\s*(\{.*?\})\s*```", re.DOTALL)


def _sanitize_visible_reply(text: str) -> str:
    """Enforce the athlete's no-em-dash output rule at the final boundary."""
    return (text or "").replace(" — ", ": ").replace("—", "-")


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
    if not ev.get("all_day"):
        try:
            duration = int(ev.get("duration_min"))
        except (TypeError, ValueError):
            return None
        if not ev.get("start") or duration <= 0:
            return None
        ev["duration_min"] = duration
    elif ev.get("start"):
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


def _key_error() -> dict[str, Any] | None:
    key = config.ANTHROPIC_API_KEY
    if not key or key.strip() in ("", "sk-ant-...") or key.strip().endswith("..."):
        return {
            "error": "ANTHROPIC_API_KEY is not set (still the .env placeholder)",
            "hint": "Put your real key in coach/.env as ANTHROPIC_API_KEY=sk-ant-..., then retry.",
        }
    return None


def _explicit_mode_switch(user_message: str) -> dict[str, Any] | None:
    """Handle exact mode commands without letting the model infer a profile.

    Only one static EVENT_PROFILE is installed. A request for any other target
    therefore leaves TRIATHLON active and asks for the replacement block; no
    ordinary coaching question can reach this branch.
    """
    target = coaching_contract.explicit_switch_target(user_message)
    if target is None:
        return None
    mode = coaching_contract.current_mode()
    if coaching_contract.target_is_current(target):
        reply = (f"TL;DR: {mode} is already the active mode.\n"
                 f"• Event: {coaching_contract.EVENT_PROFILE['event']} remains installed.\n"
                 "• Guard: Ordinary training questions cannot change event mode.")
    else:
        reply = (f"TL;DR: Mode remains {mode}; the {target} EVENT PROFILE is unknown.\n"
                 "• Needed: Send event, date, distances, goal, course aid, and pacing targets.\n"
                 "• Guard: I will not infer or carry assumptions into another event.")
    db.add_chat("user", user_message)
    db.add_chat("assistant", reply)
    return {
        "reply": reply,
        "proposed_adjustment": None,
        "proposed_week": None,
        "proposed_activities": None,
        "proposed_event": None,
        "model": "deterministic-policy",
        "stop_reason": "end_turn",
    }


def _prepare_chat(user_message: str) -> tuple[str, list[dict[str, Any]]]:
    """Build the private conversation window and append fresh training context."""
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
    # Route specialist context from the current message plus the two most recent
    # user turns. This keeps terse corrections ("no, only the race run") attached
    # to the fueling/guide discussion without bloating unrelated conversations.
    recent_user = [h["content"] for h in history if h["role"] == "user"][-2:]
    routing_query = "\n".join([*recent_user, user_message])
    context = _context_block(routing_query)
    messages.append({
        "role": "user",
        "content": f"<context>\n{context}\n</context>\n\n{user_message}",
    })
    return today, messages


def _finish_chat(user_message: str, today: str, msg: Any) -> dict[str, Any]:
    """Extract structured proposals, persist the clean exchange, and return it."""
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
    reply_clean = _sanitize_visible_reply(reply_clean)

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

    reply_clean = _sanitize_visible_reply(reply_clean)

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


def chat_events(user_message: str,
                log_as_constraint: bool = False) -> Iterator[dict[str, Any]]:
    """Yield status/text/final events so the UI can render the answer immediately."""
    del log_as_constraint  # compatibility with the pre-memory API
    switch_result = _explicit_mode_switch(user_message)
    if switch_result is not None:
        yield {"type": "done", "result": switch_result}
        return
    key_error = _key_error()
    if key_error:
        yield {"type": "error", **key_error}
        return

    started_at = time.monotonic()
    if fueling_reference.is_fueling_query(user_message):
        status = "Checking race logistics and fueling math"
    elif athlete_guide.context_for(user_message):
        status = "Checking the Vancouver athlete guide"
    else:
        status = "Checking your latest training data"
    yield {"type": "status", "message": status}
    try:
        today, messages = _prepare_chat(user_message)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return
    context_ms = round((time.monotonic() - started_at) * 1000)
    yield {"type": "status", "message": "Reviewing your plan and recent context"}

    try:
        client = _anthropic_client()
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "error": f"{type(e).__name__}: {e}", "source": "anthropic"}
        return
    first_token_ms: int | None = None
    last: Exception | None = None
    for attempt in range(3):
        sent_text = False
        try:
            with client.messages.stream(**_message_kwargs(8000, messages)) as stream:
                for text in stream.text_stream:
                    if not text:
                        continue
                    sent_text = True
                    if first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started_at) * 1000)
                    yield {"type": "delta", "text": text.replace("—", "-")}
                msg = stream.get_final_message()
            result = _finish_chat(user_message, today, msg)
            result["timing_ms"] = {
                "context": context_ms,
                "first_text": first_token_ms,
                "total": round((time.monotonic() - started_at) * 1000),
            }
            yield {"type": "done", "result": result}
            return
        except Exception as e:  # noqa: BLE001
            last = e
            if type(e).__name__ in _TRANSIENT and not sent_text and attempt < 2:
                yield {"type": "status", "message": "Connection hiccup — retrying"}
                continue
            break
    yield {"type": "error", "error": f"{type(last).__name__}: {last}", "source": "anthropic"}


def chat(user_message: str, log_as_constraint: bool = False) -> dict[str, Any]:
    """Compatibility JSON response for clients that cannot consume NDJSON."""
    for event in chat_events(user_message, log_as_constraint=log_as_constraint):
        if event["type"] == "done":
            return event["result"]
        if event["type"] == "error":
            return {k: v for k, v in event.items() if k != "type"}
    return {"error": "Coach stream ended before a reply", "source": "anthropic"}


_BRIEF_INSTRUCTION = """I just opened my dashboard. Look at `todays_completed_activities` \
and `todays_planned_workout`, then give me a tight, unprompted status brief — no preamble.

- If I have ALREADY trained today (todays_completed_activities is non-empty): EVALUATE it. \
Did I complete the planned session, a modified version, or something different? Judge the \
actual numbers (distance, duration, HR, power/pace) against the target. Tell me how it went, \
whether it hit the intent, and what it means for the rest of the week. If a second session \
is still planned today and I haven't done it, say what's left. For structured intervals, use \
`todays_interval_execution` bout by bout and NEVER use whole-session average HR as the grade.
- If I have NOT trained yet today: give the morning briefing — readiness read with the key \
signal, today's planned session and its main target, and the single most important thing to nail, \
drawing on my load focus and where I'm behind for the race.

Use the TL;DR + bullets format from your rules (TL;DR line, then 2–5 labeled bullets). \
No paragraphs, no adjustment block."""


_RACE_FINISH_INSTRUCTION = """The configured goal race in `goal_race_completion` has just \
finished syncing. This is the athlete's A-race finish, not another training-session review.

Celebrate it properly. Open with one short, genuinely excited headline that names the race and \
says they did it. Then use 3–5 concise bullets:
- recognize the full swim-bike-run achievement using specific recorded leg/duration facts;
- call out one or two moments in the data worth being proud of, without inventing a story;
- acknowledge the entire build and the fact that they reached the finish;
- finish with a simple tonight-only recovery instruction.

Do NOT lead with criticism, load metrics, targets they missed, or a new plan. Do not propose an \
adjustment. There will be time for the honest race debrief later. This message is the celebration."""


_NIGHTLY_INSTRUCTION = """It's evening — give me my nightly review of TODAY, no preamble.

Use `local_time`, `todays_completed_activities`, `todays_planned_workout`, and today's \
readiness/load context.
- Recap what I actually trained today vs what was planned. Judge the real numbers \
(distance, duration, HR, power/pace) against the target — did I hit the intent, over/under-do it? \
For structured intervals, use `todays_interval_execution` bout by bout and NEVER use \
whole-session average HR as the grade.
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

    context = _context_block()
    try:
        context_data = json.loads(context)
    except json.JSONDecodeError:
        context_data = {}
    race_completion = context_data.get("goal_race_completion") or {}
    race_date = race_completion.get("race_date")
    celebration_key = (coaching_contract.scoped_meta_key(f"race_finish_celebrated_{race_date}")
                       if race_date else None)
    celebrate = bool(race_completion.get("completed")
                     and race_completion.get("celebration_pending") and celebration_key)
    if celebrate:
        instruction = _RACE_FINISH_INSTRUCTION
    else:
        instruction = _NIGHTLY_INSTRUCTION if _is_evening(config.local_now()) else _BRIEF_INSTRUCTION
    messages = [{"role": "user",
                 "content": f"<context>\n{context}\n</context>\n\n{instruction}"}]
    try:
        msg = _stream_reply(5000, messages)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "source": "anthropic"}

    reply = "".join(b.text for b in msg.content if b.type == "text").strip()
    reply = _sanitize_visible_reply(_ADJ_RE.sub("", reply).strip())
    if not reply:
        return {"error": f"empty reply (stop_reason={msg.stop_reason})", "source": "anthropic"}
    db.add_chat("assistant", reply)  # standalone greeting; chat() trims leading assistant turns
    if celebrate and celebration_key:
        db.set_meta(celebration_key, config.local_now().isoformat())
    return {"reply": reply, "model": msg.model, "celebrate": celebrate}


def _normalized_tsb_target(value: Any) -> float | int:
    import math
    try:
        number = float(value)
    except (TypeError, ValueError):
        return coaching_contract.DEFAULT_RACE_DAY_TSB_TARGET
    if not math.isfinite(number) or not 5 <= number <= 15:
        return coaching_contract.DEFAULT_RACE_DAY_TSB_TARGET
    return int(number) if number.is_integer() else round(number, 1)


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
              ("title", "discipline", "duration_min", "intensity", "tsb_target", "structure", "why")
              if k in adjustment}
    fields["tsb_target"] = _normalized_tsb_target(fields.get("tsb_target"))
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
                  ("title", "discipline", "duration_min", "intensity", "tsb_target", "structure", "why", "is_rest")
                  if k in d}
        fields["tsb_target"] = _normalized_tsb_target(fields.get("tsb_target"))
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
    invalidate_context_cache()
    return {"ok": bool(added), "count": len(added)}
