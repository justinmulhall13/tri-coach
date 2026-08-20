# Claude Code handoff

This file is the durable continuation point for the current Tri Coach work. Read it before changing files.

## Non-negotiable user intent

- Keep the real active event profile on `t100-vancouver-2026`. Do not activate a marathon or another event while testing.
- Event switches must be persisted, provenance-tagged, and committed before any success message.
- Generated schedules must be durable drafts with a `plan_id` before they are shown.
- Preserve every existing app feature. Do not deploy unless the full suite, Python compilation, JavaScript syntax check, and database integrity check pass.
- Hevy writes require an explicit UI confirmation, verified exercise-template IDs, and no guessed working weights.

## Current uncommitted work

- Durable `event_profiles` / `event_profile_state`, pending switch staging, transactional activation, dynamic per-turn profile context, and cross-profile isolation.
- Durable `plan_drafts`, latest/by-ID/edit/activate APIs, transactional plan activation, recovery in a fresh Coach session, and fixed-date language guards.
- Garmin readiness/ACWR source-date and activity-revision freshness checks.
- Bounded Anthropic web search with visible citations for current/event lookup turns.
- Hevy official REST connector, read endpoints, proposed-routine card, and explicit create endpoint. The host MCP can read Hevy, but Fly still needs `HEVY_API_KEY`; never place the key in chat or source control.

## Work still in progress at handoff creation

- Decide/implement exact-switch web discovery without weakening the explicit confirmation
  transaction.
- Update README, rerun all checks, commit, push GitHub, deploy Fly, and smoke-test production.

## Verification commands

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall app tests
node --check /tmp/tri-coach-index.js
git diff --check
sqlite3 data/coach.db 'PRAGMA integrity_check;'
```

Extract the final inline script from `static/index.html` to `/tmp/tri-coach-index.js` before `node --check` (see existing test/tool history or use a small read-only extraction command).

Before deployment, verify without mutating state:

```sql
SELECT active_profile_id, pending_profile_json FROM event_profile_state WHERE singleton_id=1;
```

Expected active ID: `t100-vancouver-2026`; expected pending value: `NULL`.


## Session 2026-08-20 (Claude Code) — findings and changes

### Corrected assumption
The athlete HAS been lifting; Hevy simply stopped capturing it. Garmin shows six
strength sessions after the last Hevy log (Jun 22 → Jul 31). Never infer "no
lifting" from Hevy alone.

### Source split (load-bearing)
- Garmin = session occurrence/duration/HR. Never exercises or weights. Fresh.
- Hevy = exercises/sets/exact weights. Only sessions logged there. Stale since 2026-06-16.

### Units
The athlete logs in POUNDS. Hevy stores kg floats; all 89 distinct observed
values map to clean lb (61.235042773811365 kg == 135 lb). Never display raw kg.

### New modules
- `app/strength_weights.py` — lb/kg snapping, per-exercise increment inference,
  anchor-derived working weights with auditable provenance (`tests/test_strength_weights.py`).
- `app/strength_context.py` — merges Garmin session evidence with Hevy weight
  evidence, keeps both dated, sets `calibration_required` (`tests/test_strength_context.py`).

### Test hygiene
- `tests/test_coach_plan_drafts.py` pinned `config.local_today` to a fixed anchor;
  it had rotted once the real clock passed its hardcoded dates.
- `app.config` loads `.env`, so tests asserting a disconnected Hevy must call
  `hevy_connector.reset()` and restore via `addCleanup`, or a machine with a real
  `HEVY_API_KEY` makes live API calls from the suite.
- `coach._strength_load()` performs a Garmin fetch on lifting turns; patch it in
  any test that calls `_context_block` with a lifting query.
- A `tests/__init__.py` does NOT run under `unittest discover -s tests` (modules
  load top-level, not as a package). Do not rely on it for global setup.

### Strength programming (added 2026-08-20)

Delivered and tested (247 tests green):

| Module | Responsibility |
| --- | --- |
| `app/strength_weights.py` | lb/kg snapping, per-exercise increment inference, anchor-derived weights |
| `app/strength_context.py` | merges Garmin session evidence with Hevy weight evidence |
| `app/strength_effort.py` | today's lifting ceiling: run load first, recovery may only lower it |
| `app/strength_visual.py` | movement-pattern classifier + chat card view model |
| `db.plan_strength` | sidecar table attaching one lift to a calendar date |

Design decisions that must not be casually reversed:

- **A working weight is never invented.** It is either an exact Hevy value
  (`weight_provenance: hevy_history`) or a bounded derivation from one
  (`hevy_derived` + a `derivation` record, pct within 0.60-1.05). `hevy_actions`
  re-verifies the anchor against freshly fetched history at write time; the model
  cannot supply its own evidence.
- **Self-consistency is enforced separately from anchor existence.** A weight being
  historical does NOT excuse a routine that claims 85% and ships 100% — the card would
  show one number and Hevy receive another. Both checks must stay.
- **`plan_days` stays one row per date.** Reconciliation, calendar sync, gcal ids and
  plan activation all rely on that PK. A lift sharing a day with a run lives in
  `plan_strength` and renders beneath it.
- **The view is not the payload.** `strength_visual.build_view` output is display-only;
  `_INTERNAL_SET_KEYS` are stripped from the routine immediately before the Hevy POST.

Known refinement, not a safety hole: `derivation.increment_lb` is supplied by the model
and only advisory. A wrong increment yields a self-consistent weight that may sit between
pin positions (e.g. 152.5 lb on a 10 lb stack). The weight is still a bounded fraction of a
real lifted load. The clean fix is to make the server recompute the increment and the weight
from anchor + pct, treating the model's number as advisory throughout.

Delivered since: the plan view now draws an attached lift beneath its day
(`decoratePlanStrength` / `renderDayLift` in `static/index.html`), and
`app/strength_block.py` lays out a repeating upper/lower block via
`POST /api/plan/strength-block`.

Block placement invariant worth preserving: slots **strictly alternate along the
calendar**. Four sessions in seven days forces one adjacent pair, and alternating is
the only thing stopping that pair being two leg days. An earlier version ranked days
by freedom and handed the freest ones to `lower`, which put back-to-back lower days on
Mon/Tue of a real plan. Of the two possible alternations, the one keeping lower days
off the morning before a key run is chosen.

Remaining for this feature:

- A chat affordance for accepting a whole block at once (the API returns placements;
  each day is still attached individually).


## Debugging pass 2026-08-20

### Fixed: stored XSS in three render sinks
`addWeekPlan`, `renderPlan` and `addAdjustment` interpolated model-generated
`title`, `intensity` and `structure.main` straight into `innerHTML`. The API token
lives in `localStorage.tc_token`, so this was token exfiltration, and a weekplan
draft persists and re-renders in a fresh session. Three layers now:

1. Every model-derived field goes through `esc()` at the sink.
2. `esc()` also escapes quotes. `textContent` does not, and
   `class="sport-${esc(...)}"` puts an escaped value inside an attribute.
3. `_check_display_fields` rejects angle brackets and control characters in
   weekplan and adjustment free text, so a payload never reaches storage.

Quotes are deliberately NOT rejected server-side — `5' rest` and `3" spacing` are
legitimate training text. Escaping at the sink is what makes them inert.

Adjustments previously had no field validation at all: `_extract_adjustment` returned
any dict and `coach_accept` only checked it was one. They now go through the same
text guard and a discipline whitelist, raising `AdjustmentValidationError` (400).

### Fixed: unbounded profile namespace
A 5000-character event name produced a 5024-character `event_profile_id`, which is
stored on every plan, draft, chat and strength row. The stem is now capped at
`_MAX_PROFILE_STEM`; identity still comes from the digest. The T100 default returns
early and is unaffected.

### Fixed: crashes on hostile input
- `strength_effort.decide` / `strength_block.build`: `plan_days or []` let a truthy
  non-sequence through to iteration (`TypeError`). Both now type-check.
- `POST /api/plan/strength-block`: `weeks` was unclamped and overflowed date
  arithmetic. Capped at `main.MAX_BLOCK_WEEKS`.
- `POST /api/completions` and `POST /api/constraints` raised `KeyError` (500) on a
  missing field; both now return 400.

### Verified correct, no change needed
- Draft activation under concurrency: an 8-way simultaneous activation applies
  exactly once, and creating a draft supersedes the previous one so a stale
  activation is refused rather than interleaved (`test_plan_draft_concurrency.py`).
- Event-profile namespace collisions: `_server_profile_id` hashes name, date,
  distances, goal and mode, so same-name/date events with different demands do not
  share state (`test_event_profile_namespace.py`).
- Garmin-offline handling: every affected route returns a 502 naming the source;
  `/api/plan/strength-effort` degrades to 200 with a null readiness score.


## Session 2026-08-20 (part 2) — lifting rules, tab, and Hevy resolution

### The athlete's shoulder is a hard constraint, enforced in code
`app/lifting_rules.py` is the authority, not the prompt: at most one pressing
movement per session, six exercises, no two adjacent exercises from the same
group, no face pulls (rear delt fly instead), and back volume capped. `check`
reports violations, `arrange` reorders without ever adding or dropping an
exercise.

**Triceps isolation on a pressing day is ALLOWED.** An earlier version banned it,
reading the athlete's remark "only one pressing exercise per, especially if I'm
doing triceps as well" as a second constraint when it was emphasis on the first.
He corrected it explicitly. Because triceps is push-family work, the alternation
rule already keeps it off the press's shoulder; that is what "no push then
triceps back" meant. Do not reintroduce the ban.

A **chest fly is not a press**. It is grouped as push for alternation but does
not spend the single pressing slot, so one press plus a fly is legal — which is
how the athlete actually builds an upper day. `strength_visual` gained a `fly`
pattern for this; without it the rule engine rejected valid sessions.

### The coach was never actually blocked by missing exercises
It reported Chest Fly, Plank and Dead Bug as having "no template ID on file".
All three exist in Hevy's 476-template catalogue. It was searching only the
athlete's recent *workout history* (45 titles). `hevy_exercises.find_existing`
now searches the catalogue, ranked:

1. exact title, 2. title ignoring the equipment qualifier, 3. candidate
containing every requested word ("Rear Delt Fly" → "Rear Delt Reverse Fly").

Within a tier, **variants the athlete has actually used win**. Without that bias
"Squat" resolved to *Squat (Band)* and "Incline Bench Press" to *(Barbell)* when
his history is dumbbell. A request whose words are not all present is never
matched, so Lateral Raise still cannot stand in for Rear Delt Fly.

Genuinely absent exercises are created via `POST /v1/exercise_templates`. That
schema does NOT match the GET schema:

| GET returns | POST expects |
| --- | --- |
| `primary_muscle_group` | `muscle_group` |
| `type` | `exercise_type` |
| `equipment` | `equipment_category` |

`resolve_routine_exercises` fills ids from plain titles, so the model no longer
needs to quote one, and reports per-exercise outcomes instead of silently
returning a shorter session.

### Readiness follows the active event
`app/event_readiness.py` derives 14-day volume targets from the active profile's
own race distances and labels the ring from the event ("Marathon Ready",
"70.3 Ready"). The tuned T100 model is kept where it applies because its targets
are specific to that race; every other event is scored against its own numbers.
Weights are renormalised over the disciplines the event actually has, so a
run-only race is not capped by absent swim and bike volume.

### Lifting tab
`app/lifting_stats.py` + `GET /api/lifting/stats`. Records, biggest gains,
push/pull balance (a health metric for this shoulder, not a curiosity),
consistency, stale lifts, weekly tonnage, and a retrospective rule check on
logged sessions — which correctly flags historical press+triceps days and face
pulls.

**Progress is measured between sessions, never within one.** Comparing the first
logged set to the best reported a "+443% gain" on bench press where both ends
were the same December afternoon: the first set was the empty bar. `biggest_gains`
requires three separate sessions and a `first_date != pr_date`.

### Fixed: importing the app reconciled the real calendar
`main.py` called `_bg_sync()` at import, so every test run — and any script
importing `app.main` — fired a real Google Calendar reconcile from the dev
machine. With Fly reconciling the same plan, that is exactly how duplicate
events appear. It is now an ASGI startup handler, covered by
`tests/test_startup_side_effects.py`.


### Correction: the lifting tab is a READ surface
The athlete edits workouts in Hevy once they exist — "I will likely fuck around
with it and only deal with that workout in the app itself". The tab therefore
invests in reflecting Hevy: recent sessions now carry the actual sets, reps and
weights performed, with PR markers, and the history leads the tab while the
rules card sits last. Creating or restructuring a session stays in the coach
chat; editing and executing it stays in Hevy.

A PR badge requires beating a *previous* best (`sessions_logged >= 2`).
Badging an exercise's only-ever session is technically correct and useless: it
puts "PR" on every new movement and teaches the athlete to ignore the badge.
