# Claude Code handoff

This file is the durable continuation point for the current Tri Coach work. Read it before changing files.

## Non-negotiable user intent

- The active event profile is **IRONMAN 70.3 Victoria, 2027-05-30** (mode TRIATHLON, 1.9/90/21.1 km), activated 2026-10-20, two days after the Toronto marathon. Goal **5:00** (set 2026-10-20; split swim 0:37 / T 0:08 / bike 2:36 / run 1:39), with `pacing_targets` run 4:41/km, swim 1:57/100m, bike HR 142-152; the profile also carries `recovery_until: 2026-11-01`. The athlete chose 5:00 after the honest read (August form extrapolates to ~5:35-5:40, the bike is the biggest gap) — do not relitigate it. The T100 and marathon rows stay in the database, inert, scoped out by profile id. Do not activate another event while testing — pin a profile in the test instead (see below).
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

### Goal refinement keeps the profile id
`_server_profile_id` hashes the goal, so re-activating to set a finish time would
mint a new namespace and orphan the seeded plan, lifts and chat.
`db.refine_active_event_goal(goal, extras)` updates goal/extras in place for the
SAME race. A different race still goes through stage + activate.
`half_iron_plan` reads `pacing_targets` (`run_goal_min_per_km`,
`swim_goal_per_100m`, `bike_hr_bpm`) and writes explicit paces such as
"4:41/km" so `garmin_workout._stated_pace` targets them on the watch. With no
goal set, no pace appears.


## Session 2026-11-02 — Hevy cards showed "Exercise N"

`validate_routine` keeps only `exercise_template_id` for a resolved exercise
(the Hevy POST must not carry a title), and `_hevy_routine_view` named exercises
from `known_exercises()` — recent history only, in a cache warmed by lifting
turns. Never-logged movements rendered as `Exercise N`; a cold cache blanked them
all. `coach._card_names` now merges, lowest to highest priority: the model's
requested title, history, `hevy_connector.catalog_names()` (cached catalogue, no
request), and the resolver's `matched_title`. `tests/test_hevy_card_names.py`.
When the card was blank, Steve invented an explanation ("the old stored
session"); it cannot see the card, so treat such claims as unreliable.

## Session 2026-11-02 — truncated coach replies

Adaptive thinking spends from the same `max_tokens` as the visible reply, so with
the old 8000-token chat budget a long prompt (a full strength-block spec) used up
the budget thinking and came back empty, and the coach said "I ran long on that
one — ask me to continue". `chat_events` now starts at `_CHAT_MAX_TOKENS` (32000)
and on `stop_reason == "max_tokens"` retries with `_budgets()` doubling to the
128000 ceiling; `_stream_reply` (briefs, activity detail) does the same. The UI only
renders the final `done` reply, so discarded partial deltas never show. Prefill
is rejected on Sonnet 5, which is why this retries instead of continuing. The
"ask me to continue" fallback is gone; `tests/test_coach_truncation.py` pins it.

## Session 2026-11-02 — the athlete overrides the shoulder rules

The coach refused "explosive landmine press + incline DB bench" as two presses and
kept refusing after "just run the one I told you". The athlete's position: his
shoulder is his problem. The shoulder rules are now DEFAULTS for sessions the coach
designs or rebuilds unprompted; an explicit athlete request is built exactly as
asked, with no refusal, swap, warning or "your call" flag. Nothing in code ever
blocked the Hevy create (the rules only annotate the card), so this was a prompt
change in `_SYSTEM`; `tests/test_athlete_lift_override.py` pins it. Do not
reintroduce "never negotiable".

## Session 2026-11-02 — "Hevy API returned invalid JSON"

A confirmed routine create failed with `HevyAPIError: Hevy API returned invalid
JSON`. Re-running every read for that routine on Fly a few minutes later returned
clean JSON, so it was a one-off non-JSON reply from Hevy (gateway error page), not
a bad key or template. `HevyAPIConnector._request` now retries GETs up to 3 times
on invalid JSON, 429/5xx and connection errors (0.5s, 1.5s backoff); 4xx and all
writes stay single-attempt. `HevyAPIError.transient` marks what is retryable.
When the create response is `retry_safe` with no `partial_write`, the card
re-enables as "Try again" instead of locking on "Create status unknown".
`tests/test_hevy_read_retry.py` pins both.

## Session 2026-11-02 — Lifting tab follows routines created from Coach

Creating "Full Body A" in Hevy from a Coach card did not change the Lifting tab.
Now `create_confirmed_routine` returns a `summary` (template titles from the
verification GETs, working-set count, reps label, rest), and the
`/api/integrations/hevy/routines` route calls `lifting_split.apply_routine`: a
routine whose title contains a split day's name replaces that day's exercises
(kept exercises keep role/why; focus/why of the day stay), saved with
source "coach", response `split_updated: <slot>`. Any split change (PUT, DELETE,
coach sync) then runs `plan.reconcile_seeded_plan()` so seeded calendar lift days
match; edited/coach lift rows survive. Gotcha found live: the Lifting tab PUTs
the WHOLE split from its in-memory copy, so a page loaded before a server-side
change reverted it. `_saveSplit` now re-GETs the split before replacing its day.
Tests: `tests/test_split_follows_hevy_create.py`.

## Session 2026-11-02 — split days also follow routines edited IN Hevy

The athlete did Full Body A, changed exercises, and saved the workout back to the
"Full Body A" routine in Hevy; the split did not follow, since only Coach-created
routines were mirrored. `hevy_actions.sync_split_from_routines()` (throttled 10 min,
never raises) reads `GET /routines` and applies any routine named after a split day
whose `updated_at` is newer than the split's, saves with source "hevy", then
reconciles seeded lift days. Newer side wins, so a later tab edit is not undone.
Called from `GET /api/lifting/split` and the Coach context build.
`routine_summary` reps now span all working sets ("8-12" for a ramp).
Note: seeded calendar lifts cap sets at the phase dose (`min(e.sets, phase sets)`),
so a 4-set exercise shows 3 sets in base and 2 in a recovery week, by design.

## Session 2026-11-05 — monochrome redesign, nutrition tracker, auto-applied plan changes

The athlete asked for: a nutrition tab as good as the paid apps, a UI that
doesn't look AI-made ("i hate colours"), rings that don't scroll sideways on a
phone, shorter answers, and every plan-changing Coach answer to actually change
the plan.

- **Design contract (load-bearing).** Monochrome only: `--ink`/`--muted`/gray
  ramp `--g1..--g5`, solid fills via `--chip`/`--chip-ink`. `--good/--warn/--bad`
  and `--a1` all map to ink. No accent colours, emoji, glow, gradients,
  confetti, scroll-reveal or web fonts (system stack). Status is spelled out in
  words ("Low readiness", "Done"). `app/ring_detail.py` colours are CSS-var
  strings; the frontend keeps a server colour only if it starts with `var(`.
  Avoid em-dashes in UI copy; plain sentences.
- **Home**: header is wordmark + date + bell + refresh (no countdown/day chip;
  the race card owns days-to-go). Rings are a 2×2 grid at ≤820px. Phase strip is
  bare segments plus one "current phase" line.
- **Coach auto-apply**: `coach._finish_chat` applies adjustment/weekplan blocks
  and returns `applied_change {ok, kind, days, undo_ids}`; the chat renders
  `addAppliedChange` with one Undo → `POST /api/plan/undo {history_ids}`.
  `proposed_*` are null when applied; a failed apply falls back to the manual
  card. Tests: `tests/test_coach_auto_apply.py`.
- **Model**: `COACH_MODEL=claude-sonnet-5-5` (fly.toml + config default) with a
  one-shot retry on `COACH_FALLBACK_MODEL` (claude-sonnet-5) when
  `stop_reason == "refusal"`. `FAST_MODEL=claude-haiku-5-5`.
- **Nutrition backend** (`app/nutrition.py`, `app/db.py`): `foods` library
  (per-serving, upserted on log, favorites), `hydration_log`, `meal_plan()`
  (timed carb-coded slots around the session; largest-remainder split so every
  column sums to the daily target; notes contain no digits), `hydration()`,
  `week_summary()` against daily target snapshots in meta
  `nutrition_targets:{date}`, structured `log_entry()`, `log_saved_food()`,
  `read_label()` (vision + strict JSON schema; prefill only, never auto-log;
  non-label photos refused). Endpoints under `/api/nutrition/*` (foods, water,
  week, label). Tests: `tests/test_nutrition_tracker.py`.
- **Nutrition frontend**: summary (kcal left, goal segment, 4 bars), timeline,
  structured form (Auto meal = latest started slot), My foods one-tap servings,
  grouped log, water, session fuel (mix recipe collapsed), Chef Gordo, week bars,
  recipes + grocery in `details.more`.
- Removed dead code: `renderReadiness`, `renderBaselines`, `renderLoad`,
  `applyTheme`, `sessionVibe` (now `sessionLine`), `_DISC_ICO` (use `DISC_ICON`),
  scroll reveal + header compaction, confetti, `/api/baselines` fetch from Home,
  the free-text/photo food logger UI.
- Test venv: the project `.venv` is iCloud-evicted and hangs on import; a fresh
  venv from `requirements.txt` in a scratch dir works.
- Google Calendar event titles still carry discipline emoji
  (`calendar_source._DISC_EMOJI`); left alone so a deploy doesn't rewrite every
  synced event.

## Session 2026-11-05 — the watch runs exactly what the card says

The athlete asked that every workout type (bricks, swims, intervals, one-pace
runs) push to Garmin with no difference from the text. `garmin_workout.py` now
has one text-to-steps engine (`_clauses` → `_parse` → `_blocks`) with
per-sport target functions (`_run_block`, `_bike_block`, `_swim_steps`).

Rules that must hold:

- **Never invent a number.** An unstated recovery is a lap-button step (always
  for strides, hills "jog down", "full recovery"). An unquantified block is a
  lap step, or for a run/bike main the session's remaining minutes. A swim set
  with no stated rest gets no rest step. A placeholder swim ("as planned") is a
  lap step, never a default distance.
- **A written pace wins** over every other target (range as written, single pace
  ±8 s). Bricks: the run leg comes from `structure.run` (or the legacy
  "… then N min run" split), numbered globally across both segments.
- **Bike recovery** with no target is "easy spin"; a Z2 recovery gets Z2 HR. Work
  under 120 s with no explicit/race HR rides by feel. Race-effort clauses use the
  event bike HR. Outdoor watts never reach the watch (watt regex now requires a
  real unit, so "158 w/ 4 min" is no longer 158 W).
- **Garmin cannot nest repeats**: an outer N×(…) is unrolled.
- `half_iron_plan` text now states every recovery and rest it means
  (`OPENERS`, `STRIDES_WARMUP`, ", 15s rest", threshold pace range via
  `_thr_pace()`), and brick durations equal their timed totals.
- `marathon_plan.goal_pace_per_km()` returns None for TRIATHLON profiles without
  an explicit `run_goal_min_per_km`: a finish time is not a run pace.
- Coach prompt has a WATCH-READY STRUCTURE paragraph; `coach._keep_unchanged_blocks`
  restores digit-free placeholder blocks ("existing swim warmup") from the stored
  day so a move-only edit never blanks a session.
- Frontend: `workoutBlocks` draws `st.run`, and `splitSessions` keeps it.

Known gap: `suggest.py`'s moderate-readiness "adjusted" option trims
`duration_min` to 70% but keeps the full structure, so the watch gets the full
session. Tests: `tests/test_garmin_workout_types.py` (walks every generated 70.3
session and checks repeats, laps, timed totals and stated paces against the text).

## Session 2026-11-09 — Garmin's official Personal API, as a hybrid

The athlete asked to move the backend to Garmin's new personal-use API. It
exists (Garmin Connect Personal API, launched 2026-10-21: free, self-serve,
read-only, own data) but it is partial, so a full switch would lose features.
`app/garmin_personal.py` adds it as a key-gated hybrid instead.

- **Off until `GARMIN_PERSONAL_API_KEY` is set** (Fly secret; the athlete
  creates the key at connect.garmin.com > Settings > Personal API token and sets
  it himself, never in chat or source). No key: `get_client()` returns the
  plain `garminconnect` client exactly as before.
- **Key set:** `garmin_source.get_client()` returns `HybridClient`, which
  duck-types the `garminconnect` client. No call site changed. Covered methods
  try the official API and fall back to `garminconnect` with the same arguments
  on any error or wrong-shaped body; every other attribute passes through to a
  lazily logged-in `garminconnect` client (`_legacy_client`, 60 s failure
  memory).
- **Official:** `get_sleep_data`, `get_hrv_data`, `get_body_battery`,
  `get_rhr_day`, `get_all_day_stress`, `get_spo2_data`, `get_stats`,
  `get_body_composition`, `get_activities`, and
  `connectapi("/biometric-service/heartRateZones/")`. Resting HR, stress and
  stats share one daily-summary GET (60 s cache).
- **Stays on garminconnect (not published by Garmin):** Training Readiness,
  Training Status, VO2max, lactate threshold, FTP, race predictions, activity
  splits / details / HR-in-zones, respiration, and every write (upload,
  schedule, delete workout). `get_activity` also stays: the official metadata
  has no `associatedWorkoutId`, which `interval_analysis` needs.
- **Auth:** `POST .../personal-api-key/token` with `Authorization: ApiKey <key>`
  returns a bare JWT (about 24 h, expiry read from `exp`). Renewed inside the
  last hour; a still-valid token is used if renewal fails. A 401/403 on exchange
  means the key expired or was revoked: hold 10 min, fall back, say so in
  `/api/health`. 429 pauses every official call for `Retry-After`; a timeout or
  5xx pauses 60 s so one slow API cannot stall the readiness fan-out.
- **`/api/health` > `sources.garmin`** shows the path and, per read, the source
  and last error. Check it first when numbers look wrong. It never contains the
  key or the token; error messages are built without them.
- Response shapes come from Garmin's OpenAPI spec. Verified live on 2026-11-09
  once the athlete set the key: all ten official reads report source `official`
  with no error in `/api/health`. If a read ever shows `garminconnect` with an
  error there, fix its adapter; the app keeps working meanwhile.
- The key expires (30 days to 1 year, chosen at creation). When it does, the
  app silently runs on `garminconnect` alone until the secret is replaced.
- Tests: `tests/test_garmin_personal.py` (no network; scripted session).

## Session 2026-11-10 — native-feeling shell, fitness age, a stricter Steve

Three asks in one session: make the app feel less like a web page (liquid glass,
triathlon theming, more interaction), give a fitness age even though Garmin
gives none under 20, and update Steve with current pro practice while making
him strict but forgiving.

**Design rules now in force (the 2026-11-05 design contract still holds, with
one amendment: the athlete asked for liquid glass):**

- Still monochrome: no accent colours, emoji, web fonts, confetti or scroll
  reveal. Glass (blur, translucency, a faint specular edge) is allowed on chrome
  that floats over content only (header bar, phone tab capsule, sticky Coach
  bar, composer); data surfaces stay opaque. Tokens: `--glass`,
  `--glass-edge`, `--glass-rim`, `--glass-shadow`, `--lens`, `--blur`, `--scrim`,
  `--scrim-0`, with dark values under `prefers-color-scheme:dark`.
- `@media(prefers-reduced-transparency:reduce)` turns the glass solid and stops
  the blur. Keep it in step with any new `backdrop-filter` (tested).
- Sports are told apart by line texture, never colour: `.tx-swim` (wave),
  `.tx-bike` (dashes), `.tx-run` (solid), `.tx-dot` (anything else). Used on the
  Home bib course and the Training volume rows.
- `--ink` is a text colour. Solid fills use `--chip` / `--chip-ink` (a test
  rejects `background:var(--ink)`).
- Display text: server strings keep " — "; the page tidies them. `plain(v)`
  only tidies, `show(v)` escapes then tidies. Titles and notes go through
  `show()`; `tests/test_display_text_safety.py` pins the title sinks.

**Shell behaviour:**

- Large page titles collapse into the glass header (`body.scrolled`,
  `body.titled`, set by `syncScrolled`). `syncChromeHeight()` writes `--hdr-h`
  (zero on a phone Coach tab, where the header is hidden); `.coach-top` sticks
  at `top:var(--hdr-h)`.
- **`#chatlog` does not scroll; the window does.** `chatFollow(el,force)`
  scrolls the page and stops a long reply with its first line under the chrome;
  `syncChatPinned()` tracks whether the athlete is at the bottom. Never write to
  `chatlog.scrollTop` (tested). Scroll-edge scrims (`body::after`,
  `body.v-coach::after`) fade text under the floating composer and tab bar.
- The Coach tab does not read `/api/coach/history`. It restores from
  `localStorage["triCoachVisibleSessionV2"]` (5 min TTL, saved on `pagehide`
  and on `visibilitychange` hidden) and opens at the latest message.
- Reply text is held behind the thinking state until `done`; the status line
  comes from server `status` events.
- Phone: tab capsule with a sliding lens, pull to refresh (touch only), per-tab
  scroll restore, tap the active tab to go to the top, Month view with a day
  list under the grid (`.cal-daylist`, phone only).
- Home activities and the Lifting lists show five rows with a "Show all N"
  expander.

**Fitness age (`app/fitness_age.py`, `GET /api/fitness-age`):**

- Garmin answers `USER_UNDER_AGE` for this athlete. The athlete asked for a
  number anyway. `fitness_age` stays Garmin's field (null here); `estimate` is a
  separate object (`age`, `floor`, `ceiling`, `method: "vo2max_norms"`,
  `vo2max`, `typical_vo2max_at_age`, `delta_years`) and is only present when
  Garmin's number is missing. Never copy the estimate into `fitness_age`.
- Method: the age at which a typical person of the same sex has this VO2max,
  read off the Cooper Institute bands in Garmin's manuals. The table starts at
  20 (`ESTIMATE_FLOOR`), so this athlete reads 20 with `floor: true`.
- `renderFitnessAge` draws the Home card, `openFitnessDetail` the sheet. On
  desktop the card fills its height with the VO2max rows (`.fit-more`).
- 5 min cache in `garmin_source.get_fitness_age`. The nutrition no-estimate
  policy is unchanged; this is the one sanctioned estimate and it is labelled.

**Training volume card (Analytics):** computed in the page by `renderVolume(l)`
from `l.activities`. Six Monday weeks, one `<button class="vw-row">` each
(`pickVolWeek`), each activity rounded to whole minutes before summing.
`_raceSports` (from `/api/race` distances) always get a row, even at zero.
The calendar grid is still Sunday-based (`_startOfWeek`).

**Calendar:** week-grid blocks with room for it (a session of 60 minutes or
more, a Google event 50px or taller) get `.cg-ev.tall`, which lets the title
wrap to two lines; the all-day lane collapses (`.allday-lane.empty`) when no
day has an all-day event.

**Steve (`app/triathlon_knowledge.py`, wired in `coach._context_block`):**

- `pro_reference`: `context_for(text, mode=...)` returns at most three of 14
  topics (`intensity`, `swim`, `bike`, `run`, `brick`, `fuel`, `heat`,
  `supplements`, `sleep`, `alcohol`, `taper`, `race`, `recovery`, `strength`),
  each with `evidence`, `pros`, `research`, `for_him`, `do_not`. Reviewed
  2026-11 (`REVIEWED`); the README lists the sources. The athlete's own rules
  outrank it and none of its numbers are prescriptions.
- `night_out_check`: the verdict is computed, the model only words it.
  `night_dates()` reads which nights are being asked about; the verdict is one
  of `hard_no` (1 to 14 days out, `RACE_WINDOW_DAYS`), `not_this_night` (next
  day is a race, or a key session once the block is past base), `go_and_move`
  (key session in base), `go` (`keep_it_light` from day 15 to 28), `celebrate`
  (race night). `clear_nights` offers up to three alternatives. It only knows
  the calendar: run-down readiness or illness still means bed.
- The prompt section "STRICT WHERE IT COUNTS, FORGIVING WHERE IT DOES NOT" sits
  before the FINAL RULE. Profanity for a hard no is the athlete's own request.
  Never write "never negotiable" in `_SYSTEM` (a test pins its absence).
- Do not test Steve through the production chat endpoint; it writes to the real
  conversation. Use the app's client against a local context instead.

**README screenshots:** never run the backend locally to take them (it
reconciles Google Calendar). They come from a static fixture server and headless
Chrome over CDP. The fixture drops personal calendar events, body weight never
appears, and the Coach replies shown are real model answers to test questions,
not the athlete's conversation. Look at every image before committing it.

Tests: `tests/test_fitness_age.py`, `tests/test_triathlon_knowledge.py`,
`AppChromeTests` in `tests/test_header_contrast.py`, and the escaping pins in
`tests/test_display_text_safety.py`.


## Session 2026-11-10 (part 2) — flowing background, tinted panes, review fixes

**Flow background (`const Flow` in `static/index.html`, `<canvas id="flow">` first in body).**
One WebGL1 fragment shader, greyscale only, tones from `--flow-lo` / `--flow-hi` /
`--flow-curve`. Styles `water` (caustics), `wind` (stretched noise), `cloud` (fBm),
`plain`; `auto` follows today's session via `Flow.sport(flowSportFor(_woSessions))`.
API: `Flow.set`, `Flow.sport`, `Flow.motion`, `Flow.pulse`, `Flow.retone`,
`Flow.state`, `Flow._seek(sec)` (captures). ~0.42x resolution, 30 fps, pauses on
`visibilitychange` / `pagehide`, handles context lost/restored, no `Math.random`.

Design contract, all pinned by `tests/test_flow_background.py`:

- Never put `opacity`, `transform`, `animation`, `transition` or `filter` on `#flow`
  (iPhone Safari drops frames or goes black). Movement lives in the shader.
- Blur (`backdrop-filter`) is for floating chrome only. `.card` is
  `background:var(--surface)`, translucent and tinted, never blurred. `.sheet` resets
  the surfaces to the solid set.
- Never animate opacity on an ancestor of glass: `main.view-in:not(#main-coach)`.
- `:root.solid`, `prefers-contrast:more` and `prefers-reduced-transparency:reduce`
  must all carry the same opaque token set. Safari never matches the last one, hence
  the in-app Panels switch (`localStorage.glass`, applied in `<head>` before paint).
- The Settings sheet (`#flow-btn`, `#flow-overlay`: `#app-theme`, `#bg-style`,
  `#bg-motion`, `#bg-glass`) is the WCAG 2.2.2 stop control. Do not remove it.
- **Light is the default theme; dark is opt-in.** The athlete does not want the
  system setting to pick dark. Dark tokens live in `:root.dark{...}` (no
  `prefers-color-scheme` anywhere, a test pins that), chosen in Settings, stored
  in `localStorage.theme`, applied in `<head>` before paint along with
  `#theme-meta`. Because `:root.dark` outranks a bare `:root`, the Increase
  Contrast and Reduce Transparency blocks select `:root,:root.dark`. Theme-aware
  script reads the class, not `matchMedia`.
- `--muted` only passes 4.5:1 on a pane. Text that sits on the bare field uses
  `--ink2` (`#main-calendar{--muted:var(--ink2);}`, `.coach-empty`,
  `.coach-thinking-status`). Changing `--surface`, `--muted` or the flow tokens
  reruns those sums in the test.

Nothing was tested on a real iPhone. Expect a tuning pass on the device.

**Steve night-out parser (review fixes).** New non-verdict outcome: a named night that
cannot be placed returns `{nights: [], night_unclear: true, how_to_answer: {unclear}}`
and Steve asks which night. Rule: no date cue means tonight; a date cue that cannot be
resolved means unclear, never tonight. `night_is_unclear(text, *, today, race_date)`,
`coach._context_block(user_query, *, night_query=None)`, `coach._written_on`.
"drunk a week from saturday" is a night ("drunk a litre" is not); "getting hammered /
wasted" counts as drinking, "legs got hammered" does not. Known gaps: an undated event
("at the wedding") is judged as tonight; a bare "going out in ten days" with no drink
or party word gets no verdict.

**Fitness age.** A half-failed Garmin read is cached 60 s (`_FITNESS_AGE_PARTIAL_TTL_S`),
both answered 300 s, both failed not at all. `/api/fitness-age` adds
`reads: {fitness_age, vo2max}`; `estimate.source` is `estimated`, the VO2max rating
carries `rating_source: derived`.

A breaker pass and a wide review of this change were cut short to stay inside a usage
cap; the parser has only the unit tests and a hand spot check behind it.

## Session 2026-11-10 (part 3) — Hevy custom exercise create

Symptom: Send to Hevy on a split day said "Could not match" for exercises Hevy lacked,
and each retry added another copy of them to Hevy. Cause: the create landed but the
reply was unusable, `resolve` reported `failed`, and the catalogue cache was dropped
only on success, so the next send could not see the new exercise either.

- `hevy_connector._request_once(scalar_key=)`: for the exercise create only, a 2xx
  reply that is a bare id (text, JSON string or number) becomes `{"id": ...}`.
- `create_exercise_template` drops the catalogue cache in a `finally`.
- `hevy_exercises.resolve` reads the catalogue back after any create without an id
  and uses an exact-title match (`_read_back`). A real refusal stays `failed`.
- The push endpoint returns `unresolved_reason`; the sheet shows it.
- The exact reply shape Hevy sends was not observed (that needs a production write).
  Tests: `tests/test_hevy_create_readback.py`.

## Session 2026-11-10 (part 4) — Fuel: typed estimates and barcode

- The athlete asked for ballpark logging by typing a food ("iced chai latte grande from
  starbucks"). `nutrition.log_food` now: explicit macros -> self-reported (unchanged); a bare
  8-14 digit line -> `lookup_barcode`; anything else -> `estimate_food` (model, JSON schema)
  and logged with `nutrition_log.source = "estimate"`. No model key -> old "macros unknown"
  question. Estimates are always labelled (row label, summary line, `day.estimated`).
- Barcode: there is no client decoder (iOS Safari has no BarcodeDetector). The scan photo goes
  to `read_label`, whose schema now also returns `barcode`; `barcode_ok` checks the GTIN check
  digit and `lookup_barcode` reads Open Food Facts (User-Agent `Tri-Coach/1.0`, no personal
  data). Result only prefills the form. A printed nutrition panel still wins when present.
- Meal photos (`log_photo`) and the nutrition coach prompt still do not estimate.
- Tests: `tests/test_fuel_estimate_barcode.py`. Tests that call `log_food` without macros must
  patch `_ok_key` or the model, or they hit the real API when a local key exists.

## Session 2026-11-10 (part 5) — Garmin daily calorie burn

- `garmin_source.get_daily_energy(days=7)` reads `get_user_summary(date)` (legacy garminconnect call,
  forwarded by `HybridClient`) for today plus the last 7 days: `totalKilocalories`,
  `activeKilocalories`, `bmrKilocalories`. Finished days are averaged; today is a running total,
  flagged `partial` and kept out of the average; days with no total go in `missing_dates`.
- `coach._live_context` fetches it as `"energy"`; `_context_block` exposes it as `daily_energy` via
  `_energy_context`, which adds `goal`, `goal_adjustment_kcal` and a code-computed `target_kcal`
  (average + `nutrition._GOAL_KCAL[goal]`, rounded to 10). Read it with `live.get("energy")`:
  older tests build `live` without that key.
- `coach._SYSTEM` has a rule telling Steve to answer calorie questions from `daily_energy` and to
  give no number when it is null. The Fuel tab's own target is still the g/kg macro target.
- Tests: `tests/test_daily_energy.py`.

## Session 2026-11-10 (part 6) — run structure: no same-pace splits, no walk recoveries

- Steve wrote an easy run as warm-up 10 min + main 25 min + cool-down at one pace, with strides
  "w/ 60s walk". The athlete wants neither: same-pace blocks are one run, and he does not walk.
- `app/run_structure.py` `tidy(structure, discipline)` joins a same-pace warm-up/cool-down into the
  neighbouring main block (sets the other to "—") and turns walk recoveries into "easy jog" in
  `warmup`/`main` (cool-down text is left alone). Run sessions only.
- It runs at read time in `db._row_to_day` and `db._row_to_secondary`, so the card, the watch build
  and the coach context agree. Stored rows are not rewritten, so no production write was needed.
- `garmin_workout._parse`: strides with no stated recovery now say "easy jog back".
- `coach._SYSTEM` has a RUN STRUCTURE paragraph with both rules.
- Tests: `tests/test_run_structure.py`.
