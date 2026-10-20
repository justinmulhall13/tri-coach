# Claude Code handoff

This file is the durable continuation point for the current Tri Coach work. Read it before changing files.

## Non-negotiable user intent

- The active event profile is **IRONMAN 70.3 Victoria, 2027-05-30** (mode TRIATHLON, 1.9/90/21.1 km), activated 2026-10-20, two days after the Toronto marathon. No finish-time goal yet; the profile carries `recovery_until: 2026-11-01`. The T100 and marathon rows stay in the database, inert, scoped out by profile id. Do not activate another event while testing — pin a profile in the test instead (see below).
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

Expected active ID: the `ironman-70-3-victoria-2027-05-30-…` profile; expected pending
value: `NULL`.


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


## Session 2026-08-20 (part 3) — the split, and physio knowledge for Coach Steve

### The four-day split is the first thing on the Lifting tab
`app/lifting_split.py` holds Upper 1 / Lower 1 / Upper 2 / Lower 2, rendered four
across on desktop and two-by-two under 820px. Each day is clickable; the sheet
shows every exercise with its reason, and "ask coach about this day" routes into
the **main** coach chat rather than running a second conversation, because a reply
that changes the day should land where every other change lands.

Design points worth keeping:

- The two Upper days differ **by plane** — horizontal then vertical. Running the
  same upper session twice trains one plane twice and leaves the other untrained.
- Leg days are built for a runner: unilateral loading, eccentric hamstrings, hip
  extension and calves, with the highest-damage lifts kept low in volume. A
  bodybuilding leg day costs two days of running.
- Every exercise carries a `role` and a `why`. The tab shows them; a split the
  athlete cannot interrogate is one he will not follow.
- `db.lifting_split` is per event profile, so a marathon block and a tri block do
  not overwrite each other. An edit that breaks a rule is **saved and reported**,
  never silently rejected or silently accepted.

### Group subdivision (this made leg days possible at all)
"legs" as a single alternation group flagged every lower session, because a lower
day is legs by definition. Groups are now `quad` / `hinge` / `calf`, and
`_TITLE_GROUP` overrides the pattern where the movement name misleads:

- a Nordic **curl** is a hamstring exercise, not biceps work;
- a rear delt fly is shoulder isolation, not back thickness — grouping it with
  rows both broke the alternation shape and inflated the back-volume count the
  athlete asked to keep down.

`back_count` checks group as well as pattern for the same reason.

### Coach Steve now carries a physio/strength reference
`app/strength_knowledge.py`: the pain-monitoring traffic light, tissue adaptation
timelines, load-progression limits, concurrent-training rules, per-region
substitutions, and referral triggers. It is carried whenever the athlete mentions
pain **or** programming — a sore knee usually arrives mid-conversation about
running — and always includes the shoulder as the standing problem area.

**It never names a diagnosis.** "Your shoulder hurts pressing overhead" is an
observation the coach can act on; "you have impingement" is a clinical judgement
it is not entitled to make, and acting on a wrong one is how someone trains
through a stress fracture. Referral triggers exist so the coach knows where its
competence ends.

Regex note, twice-learned: match **stems**, not whole words. `\btweak\b` misses
"tweaked", `\binjur\b` misses "injured", `\bsubstitut\b` misses "substitution" —
which is most of how anyone actually writes.


## Session 2026-08-20 (part 4) — why exercises were still vanishing

### Root cause: the matcher was handed an empty pool
`resolve_routine_exercises` called `search_exercise_templates(title)`, which does a
naive substring match on the WHOLE query. `"rear delt fly"` is not a substring of
`"Rear Delt Reverse Fly (Dumbbell)"`, so the search returned **zero** candidates and
the ranked matcher never ran. The earlier fix was verified by passing the full
catalogue by hand, which is why it looked correct.

`all_exercise_templates()` now exposes the cached catalogue and resolution ranks
against all 476. A six-exercise day resolves as six.

### Variant selection needed frequency, not membership
`history_template_ids()` came from the chat context cache, which is only warm after
a lifting conversation — empty otherwise, so "Squat" resolved to *Squat (Band)*.
It now reads real workouts. And membership alone still tied: the athlete has logged
barbell, cable AND dumbbell curls, so the tie-break fell to the alphabet.
`history_template_counts()` ranks by how often each variant is actually used.

### Only fetch when something needs resolving
The catalogue and history were fetched unconditionally, so a routine that already
carried template ids cost 11 API calls — and pushed one test from 1.4s to 21.5s by
reaching the live API. Guarded by `needs_lookup`.

### The split is edited in place
`POST /api/lifting/split/{slot}/push` creates one day as a Hevy routine, confirmation
required, refusing the whole day (422) if any exercise cannot be matched rather than
shipping it with movements missing. The day sheet edits title/sets/reps inline, saves
the whole split back, and reports rule violations after saving. "Ask coach" remains,
but it is no longer the only way to change one exercise.

### Near-miss worth remembering
A block rewrite deleted `_liftFig` and `_liftNum` while leaving eight call sites — a
`ReferenceError` that would have broken the whole Lifting tab at runtime. `node --check`
passes fine on that, since the syntax is valid. `test_lifting_split_routes` now asserts
every helper the tab calls is actually defined.


### Send to Hevy only appears for a day that changed
`lifting_split.build()` marks each day `is_customised` by comparing its exercise
titles, sets and reps against `DEFAULT_SPLIT`. The button is shown only when that
flag is set AND the editor is clean.

The clean-editor half is the important one: `POST /api/lifting/split/{slot}/push`
reads the **stored** split, so offering the button mid-edit would ship the previous
version of the day to Hevy while the athlete watched their unsaved changes on
screen. Editing hides the button and says "save to send this day to Hevy".


### Split grid clipped on a phone: two CSS bugs
At 375px the page overflowed by 62px and the grid by 91px. Grid items default to
`min-width:auto`, so `.splitday` could not shrink below its longest exercise name
("Half Kneeling Landmine Press", 162px) and the columns were 204px each in a
317px grid. `min-width:0` on `.splitday` and `.sd-e` restores shrinking.

That alone left a third of the names ellipsised, so the phone breakpoint wraps
them instead — but the first attempt did nothing, because **a media query carries
no specificity**. `.splitday .sd-e span{white-space:nowrap}` was declared later
and won. The breakpoint now sits after the base `.splitday` rules.

Verified by measurement rather than eye: at 375px there are 2 columns, no page or
grid overflow, and zero truncated names; at 1280px, 4 columns and the same.


## Session 2026-08-21 — the readiness score was lying

### "88% Marathon Ready" off 21 km a week
`VOLUME_MULTIPLE["run"] = 1.0` made the 14-day target one race distance: 42 km per
fortnight, i.e. 21 km/week, for a marathon. On the athlete's real numbers that
scored **93, "Race ready"**. Targets now come from `weekly_run_target_km`, whose
multiple FALLS with distance (5K needs ~6x race distance weekly, a marathon ~1.55x),
giving 65 km/week for a marathon.

### Volume was never the whole story
Weekly kilometres accumulated in 5 km pieces do not make a marathon survivable, so
the score now carries a **long run** component — and for a run-only event the long
run also CAPS the score. An 18 km longest run against the 32 km a marathon asks for
holds the score to 55 however high the volume, and says so:

    Held to 55 by the long run: 17.5 km against the 32 km this distance asks for.

A triathlon is deliberately NOT capped this way: swim and bike carry real
information there, so the run leg alone must not govern.

Absent evidence is not evidence of readiness either. With no run history at all a
run-only event is held to `UNVERIFIED_CEILING` (70) rather than reading race ready.

On the athlete's real data: **46, "Behind"**, long run 17.5 / 31.7 km.

### The ring detail was still T100-only
`ring_detail._t100` returned "T100 readiness is unavailable for the active event
profile" for any other event, so after switching to the marathon the ring rendered
but its sheet was dead. `_event_ready` now serves whatever event is active, with a
long-run contributor and the limiter sentence as the insight. `"t100"` stays as an
alias so an older client still resolves.


## Session 2026-09-08 — the marathon block

### The event switched, and that broke twenty tests without breaking any code
T100 Vancouver is past. The athlete's event is now the TCS Toronto Waterfront
Marathon on 2026-10-18, goal sub-3:30, 40 days out at the switch.

Twenty tests went red the moment the profile changed, all of them asserting
T100-only data (`KeyError: 'course_aid'`, `'placement'`, `'error'`). None of them
were testing a switch — they read whichever profile the shared database happened
to hold. Five modules now pin the default profile at module scope:

```python
def setUpModule():
    _PROFILE_PIN = _patch.object(_db, "get_active_event_profile_record",
                                 return_value=_cc.default_event_profile_record())
    _PROFILE_PIN.start()
```

Insert that block **before the first `class`**, never "after the last import
line": several of these files open with a multi-line parenthesized import, and an
insert after its first line lands inside the parentheses and is a `SyntaxError`.

**A patched reader is not enough when a test writes plan rows.** `plan_days` are
namespaced by the profile id the *database* holds, so patching
`get_active_event_profile_record` alone writes marathon days stamped
`t100-vancouver-2026`, and every reconcile then skips them as another profile's
rows. `tests/test_marathon_plan.py` really stages and activates instead.

### `app/marathon_plan.py` — the compressed build
The athlete arrived with a 117 km/week bike base and an 8.5 km longest run. You
cannot get to a marathon from there by scaling weekly kilometres; weekly volume
only moves so fast, and the one adaptation the race demands — tolerating hours of
eccentric loading — is trained by the long run alone. Cycling is concentric and
does not buy it. So the long run carries the specific progression
(`LONG_RUN_KM = (14, 18, 23, 28, 16)`) and the bike carries aerobic volume. That
also matches how the athlete self-regulates: run when the legs are good, ride
when they are sore, which is now rule 9 in the coach prompt and `SORE_SWAP` on
every easy and mid-long day. It is deliberately absent from the long run and the
threshold run: those are the two sessions the block is built on.

Invariants worth keeping:

- **The ladder is right-aligned to race day, never left-aligned to the start.**
  That is what makes `reconcile` safe: re-running it mid-block yields the same
  remaining long runs the athlete was already shown. A block longer than the
  stock ladder grows at the front, so the peak stays two Sundays out.
- **The peak long run is a BUILD session.** The taper starts the day after it
  (`< 14`, not `<= 14`). An earlier version marked the peak week as taper and
  handed the athlete a rest day two days before their biggest run.
- **`phase` is not preserved by reconcile.** It is derived from the date and race
  day, so a refresh recomputes it; only true placement metadata (`start_time`,
  `gcal_event_id`, `pos_updated_at`) survives.
- **Nothing about the race is hardcoded.** `race_day()` reads the event name and
  distance from the active profile — an earlier version wrote "TCS Toronto
  Waterfront Marathon" into every MARATHON profile's race day, including a test
  fixture called "Berlin".
- **Goal pace is derived, never invented.** `goal_pace_per_km()` reads an explicit
  `run_goal_min_per_km`, else parses a finish time out of the athlete's own goal
  text. No stated time means no pace number anywhere in the plan.

### The long run is labelled `"long"`, not `"easy"`
`zones` already maps both to Z2, so nothing about the prescription changed — but
`strength_effort.is_key_run` keyed off duration alone (`LONG_RUN_MIN = 90`), and
an early-block 14 km long run is 86 minutes. It was scoring as an ordinary easy
day, and the lifting placer was putting a leg session on it.

### Two real placement bugs in `strength_block`
1. `_spread` optimises spacing and nothing else. In a triathlon week, where rest
   and swim days score high, that was invisible. A marathon week is running every
   day, so the freest days are only a 3, and spacing alone parked a lower day on
   the long run **every week**. `_freest_pool` now descends the freedom tiers
   first and lets spacing decide only among days that cost the same.
2. `is_key_run` rates race day a 2, which was enough to place a lift **on race
   day** in a week where nothing scored higher. `_day_freedom` now returns 0 for
   a race and for the morning before one.

### Lifting tapers, and the code says so rather than the prompt
`strength_effort.TAPER_CAPS` caps the session at moderate inside 21 days of the
race, light inside 14, and skip inside 2. Strength added that late does not
arrive in time to help; the fatigue it costs arrives immediately. It is a
ceiling and only ever lowers a call — poor readiness already saying "light" is
not raised back to a "moderate" cap.

Because effort now reads the active event, `test_strength_effort` and
`test_strength_block` hold the race 120 days away at module scope. Without that
they would start failing on their own as the real race approaches.

### The Achilles is not a standing injury
Removed from `coach.py` rule 8, `coaching_contract` (`run_constraints`, the prose
line and the rules tuple) and `insights.py`. It was a T100-specific worry about
running on gravel and the athlete said so plainly. The generic physio material in
`strength_knowledge.py` and `lifting_split.py` stays: it is reference used only
when he reports actual pain on the day.

### Today's session is an `edited` row on purpose
2026-09-08 records the 8.2 km he actually ran. Written with `source="edited"` so
the bootstrap reconcile skips it rather than prescribing a threshold run over a
session already done.


## Session 2026-09-10 — school replaces the workday, dark-mode chips

### School is 08:00-12:40 every day
The athlete said "every day", and it is taken literally, weekends included. The
old fixed 9-4 weekday workday is gone from `schedule_time.py`, the calendar
assistant prompt and the week view (the `workband` is now a labelled
`schoolband` drawn on all seven days). Placement: finish 15 minutes before
school if the session fits after 05:30, otherwise start at 13:00. School is also
added to the busy ranges, so the 15-minute nudge can never walk a session into
it. `SCHOOL_START_MIN` / `SCHOOL_END_MIN` in `index.html` must match
`schedule_time.SCHOOL_START` / `SCHOOL_END`; `tests/test_school_schedule.py`
checks that. `ATHLETE_CONSTANTS["daily_schedule"]` tells Coach Steve.

Days already synced carry a stored `start_time`, so a scheduler change alone
does not move them. Future rows whose stored time overlapped school were cleared
and re-placed.

### `--ink` is a text colour, never a background
The product-reset block used `background:var(--ink);color:#fff` for the coach
avatar, the thinking orb, the module icons and the user bubble. `--ink` turns
near-white in dark mode, so all of them became white glyphs on a white disc —
only the user bubble had a dark-mode patch. Solid chips now use `--chip` /
`--chip-ink`, defined in both palettes, and a test rejects
`background:var(--ink)` anywhere in the page.

### Do not run the backend locally to look at the UI
Uvicorn's startup handler reconciles Google Calendar, and Fly reconciles the
same plan: running both is how duplicate events appear. For a visual check,
render the page's CSS into a static harness and screenshot it with headless
Chrome (`--headless=new --screenshot`).


## Session 2026-10-20 — the 70.3 block and three full-body lifts

### `app/half_iron_plan.py`
Any TRIATHLON profile other than the bundled T100 one now dispatches here
(`plan._event_builder`). Phases are counted back from race week in whole weeks
(`weeks_out`): taper n<=1, peak n=2, specific 3-8, build 9-16, base beyond, and
`recovery` for every date up to the profile's `recovery_until`. Deload when
`n >= 7 and (n - 3) % 4 == 0`; the deload Friday is a full rest day.

`coaching_contract.race_phase` adds `block_phase` / `block_phases` for this
profile **alongside** `phase` — several readers compare `phase` to
build/peak/taper, so it was not repurposed. The phase strip prefers
`block_phase` when present.

### Lifts are seeded, not proposed
The builder writes a `plan_strength` row (slot `full`, `source="seed"`, no Hevy
routine) on Mon/Wed/Fri from the stored or default split. Reseed/reconcile only
touches lifts still marked `seed`, and keeps their calendar placement and any
Hevy id. No lift in the first post-marathon week; race week keeps Monday only.
Box jumps are dropped in recovery weeks, so that day is five exercises — the
`exercise_count` rule is a programming note, not an injury rule.

### The split is three full-body days
`lifting_split.DEFAULT_SPLIT` is Full Body A/B/C (`full_a`/`full_b`/`full_c`).
Leg load falls through the week; C has no press. Days carry a `tag` for the
card label, and the grid's column count follows the number of days.
