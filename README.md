# Tri Coach

A personal endurance-coaching app built around a single question I ask every
morning before training: *given how I actually slept and recovered, what should
I do today — and does it still fit the rest of my life?*

It pulls live Garmin recovery and training data, models readiness, runs a
periodized plan toward a race, and keeps two AI agents in the loop: a
conversational coach that can reprogram the plan, and a natural-language
calendar assistant that schedules workouts around a real calendar with two-way
Google sync. It runs as an installable PWA and is deployed on Fly.io.

Built for training toward **T100 Vancouver** (2.0 km swim / 80 km bike /
18 km run), but the engine is general to any endurance build.

> Status: actively used daily by one athlete (me). It's a real tool, not a demo —
> which is why the hosted instance is access-gated rather than a public link.

---

## Why I built it

I tried a lot of the obvious things first — Garmin Connect Premium, WHOOP, a
couple of paid triathlon coaching plans. They're all good at what they measure.
What none of them handled was *my actual life*.

Every one of them assumed my weeks were identical: same wake-up, same free
evenings, same willingness to be in the pool at 5am on a Tuesday. But an old
friend asks if I want to grab beers, I'm out until 2am, and the 5am session was
never happening. A rigid plan responds to that by turning the rest of the week
into a guilt trip and a pile of "missed" workouts.

I wanted the opposite: **a plan as fluid as the life it has to fit into.** One
that re-plans week to week around real work hours and real commitments, moves a
session instead of writing it off, tells me when I'm actually recovered enough to
go hard — and, crucially, *doesn't quietly decide for me*. An early version of
this app silently swapped a key VO2 session for a recovery spin because my HRV
looked low; I saw the easy session, went back to sleep, and later found the
numbers hadn't been that bad. That's why readiness in here is **advice, not a
veto** — the athlete makes the call.

So the design goals came out of frustration, not features:

- **The plan bends, the athlete doesn't get bullied.** Low recovery surfaces a
  warning and an easier option; it never rewrites your day behind your back.
- **Life is a first-class input.** Your real calendar drives scheduling —
  workouts land around a 9–4 workday and route around whatever else is on it.
- **Talk to it like a person.** "Move my bike to Thursday morning", "I'm at the
  driving range tonight", "only 45 min today, legs are cooked."
- **Every week is allowed to look different.** The plan is re-shaped around
  travel, illness, a big work week, or a late night — without losing the arc that
  gets you to the start line.

---

## The five tabs

### Home
![Home tab](docs/screenshots/home.png)

The A-race hero carries the goal, the countdown, and where you sit in the block.
Below it the readiness score reads out of Garmin — HRV against your own rolling
baseline, sleep, resting HR, and form (TSB) — with a plain-English verdict rather
than a number to interpret. Then today's sessions, the four WHOOP-style rings
(each clickable into a full breakdown), proactive signals, recent activities and
current load.

Readiness advises; it never overrides. If recovery is poor the app says so and
offers an easier alternative when you push the session to your watch, but it will
not silently swap your training out from under you. That line was drawn after an
early version quietly downgraded a key workout and cost a good training day.

### Coach
![Coach tab](docs/screenshots/coach.png)

A conversational coach (Claude) with the full picture: Garmin data, the plan, the
week's load, and your own stated constraints. It is direct by design and cannot
invent numbers — a missing metric is reported as missing.

It will reprogram any upcoming day, rebuild a whole week around a constraint,
log sessions you did off-plan, propose calendar events from a passing mention,
and decide on its own what is worth remembering (availability, niggles, travel)
versus what is just chatter. It also follows an explicit set of load rules: state
the TSB target, hold the race-day taper between +5 and +15, name the tradeoff
when cutting volume, give easy sessions a hard ceiling rather than a range, and
never raise run volume to hit a load target when the Achilles is the limiter.

### Calendar
![Calendar tab](docs/screenshots/calendar.png)

Training and life on one surface, synced two ways with Google Calendar. Agenda,
Week and Month views, with navigation back through previous weeks and months.
Workouts are written to Google as timed blocks, placed in the mornings around a
fixed 9–4 workday and routed around whatever is already booked.

Drag a session to move it, drag its edge to resize it, tap it to open the full
breakdown — every change pushes straight to Google. Edits you make *in* Google
flow back on next open, resolved most-recent-edit-wins. The command bar at the
top takes plain English: "move my bike to Thursday 6am", "make Saturday's ride
2h", "add dentist Fri 2pm".

*Personal event titles are replaced with placeholders in this screenshot.*

### Fuel
![Fuel tab](docs/screenshots/fuel.png)

Daily targets scale to the day's training and shift with a calorie goal
(deficit / maintain / surplus) that takes its cut from fat and non-training
carbohydrate while protecting protein. Meals log from free text or from a photo
of the plate.

The part that matters is intra-session fuelling, which is built against
intestinal transport limits rather than a flat carb number. Glucose is capped at
60 g/h (SGLT1) and fructose at 30 g/h (GLUT5); above 60 g/h total the mix is held
at 2:1, so 90 g/h lands exactly on both ceilings and nothing is prescribed that
cannot be absorbed. Drink concentration is held to 6–8% by mass and any carb
beyond what the bottle can carry is assigned to gels with plain water. Every plan
shows its arithmetic, states what it assumed about product composition, lists the
labels to verify, and carries an abort protocol for GI distress.

### Analytics
![Analytics tab](docs/screenshots/analytics.png)

Fitness, fatigue and form (CTL / ATL / TSB) over 90 days against the race-day
target band, weekly volume by sport, load focus and acute:chronic ratio, and your
real Garmin training zones — per sport, since cycling zones sit lower than
running. Those same zones are what every pushed workout targets, so what you see
here is what lands on the watch.

---

## Also

**Structured workouts to the watch.** Sessions push to Garmin as native
structured workouts with explicit bpm ranges and pace bands drawn from your own
zones, not bare zone numbers. Bike work is prescribed by heart rate — an
indoor FTP does not transfer to hilly outdoor riding — and watts appear only as a
secondary cue on explicitly indoor sessions. Easy runs carry a pace *ceiling*
rather than a range.

**Completion that respects intent.** A stretch class syncing from Garmin as
`strength_training` will not tick off a planned strength lift; mobility work is
classified separately.

**Per-activity deep dive.** Route, splits, HR/power/pace series, aerobic
decoupling (Pw:HR or Pa:HR, first half versus second) and a best-effort curve
from 5 s to 60 min, plus a cached AI read of the session.

**It reaches out.** Web-push notifications for the things worth a heads-up: a big
brick tomorrow ("carb up tonight"), a low-recovery morning, the nightly review.

---

## Architecture

A FastAPI backend serving a single-file vanilla-JS PWA. No frontend framework —
the whole UI is one hand-written `index.html` (SVG rings, the calendar grid,
pointer-based drag/resize) so the app stays dependency-light and instant to load.

```
app/
  main.py            FastAPI app, routes, auth gate, background sync
  config.py          env + race-phase math; timezone-correct "today"
  db.py              SQLite (schema, migrations, plan/completions/constraints)

  garmin_source.py   Garmin ingestion: readiness, HRV, sleep, activities,
                     training load / ACWR, VO2 & FTP, race predictions
  rings.py           the four dashboard rings
  ring_detail.py     per-ring drill-downs (contributors, stages, charts)
  activity_detail.py per-activity deep dive + cached AI analysis

  zones.py           the athlete's REAL Garmin HR zones (per sport), LT, FTP
  plan.py            periodized plan generator (build / peak / taper)
  plan_adapt.py      closed-loop adaptation (session feedback, illness/travel reflow)
  suggest.py         today's session = plan + readiness advisory
  schedule_time.py   default workout times around the 9-4 workday
  garmin_workout.py  push structured workouts to the watch

  coach.py           "Coach Steve" agent (chat, briefs, plan edits, memory)
  calendar_agent.py  natural-language calendar assistant
  nutrition.py       daily targets + transport-ceiling intra-session fuelling

  calendar_source.py Google Calendar API (read + write, tagged events)
  calendar_sync.py   two-way sync engine (reconcile, reverse-pull, move/resize)

  push.py            web-push notifications
  insights.py        proactive signals    baselines.py  personal baselines
static/index.html    the entire front end (PWA)
```

**Data flow.** Garmin → normalized activity/readiness models → rings + plan +
readiness advisory → dashboard and today's session. The plan is the source of
truth; the calendar layer projects it onto real time and keeps Google in sync in
both directions.

---

## Design decisions worth calling out

A few problems here were more interesting than they first look:

- **Timezone-correct "today."** The server runs UTC on Fly; the athlete lives in
  Pacific. "Today" is resolved in the athlete's local zone everywhere, so the day
  rolls over at local midnight — not when UTC ticks over in the evening. A
  late-night bug where the app showed *tomorrow's* plan traced straight to this.

- **Idempotent calendar reconcile.** The sync guarantees exactly one Google event
  per training day. Events are tagged with private metadata so the app only ever
  touches its own, and each reconcile prunes any duplicate or orphaned copy —
  which matters once more than one instance can write to the same calendar.

- **Most-recent-wins reverse sync.** A workout's time can be edited on either side
  (dragged in the app or in Google), so the app tracks *when the position last
  changed* — separately from internal bookkeeping writes — and compares that
  against Google's `updated` timestamp so neither side clobbers a newer edit.

- **Latency budgeting.** The calendar assistant runs on a small fast model for
  structured parsing, and edits push only the single day that changed instead of
  re-syncing the whole week — turning multi-second commands into sub-second ones.

- **Classification that respects intent.** A Peloton stretch syncs from Garmin as
  `strength_training`; counting it as a completed strength lift is wrong, so
  mobility/yoga/stretch work is reclassified to its own category and never
  completes a strength session.

---

## Tech stack

Python · FastAPI · SQLite · [garminconnect](https://github.com/cyberjunky/python-garminconnect)
· Anthropic Claude API · Google Calendar API · Web Push (VAPID) · vanilla
JS/HTML/CSS PWA · Docker · Fly.io.

---

## Running it locally

Requires Python 3.10+ and a Garmin account. The AI features need an Anthropic API
key; the calendar features need a Google Cloud OAuth client. Both degrade
gracefully — the dashboard works without them.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in your keys / race details
python -m app.main        # serves http://127.0.0.1:8770
```

`.env.example` documents every setting. Secrets (`.env`, the SQLite DB, and the
Google `credentials.json` / `token.json`) live under `data/` and are gitignored —
nothing sensitive is in the repository.

### Google Calendar (optional)

1. In Google Cloud Console, enable the Calendar API and create an **OAuth client
   ID → Desktop app**; download the JSON to `data/credentials.json`.
2. Add your own address as a Test user on the OAuth consent screen.
3. First run opens a browser once to authorize; the token caches to
   `data/token.json` and refreshes itself after that.

### Deployment

Containerized with the included `Dockerfile` and deployed on Fly.io
(`fly.toml`), with a persistent volume for the SQLite DB and cached credentials.
The machine scales to zero and wakes on request.

---

## A note on how this was built

This was built quickly and iteratively with heavy use of AI coding tools — which
is rather the point. Every feature, data model, and product decision here was
mine; the AI was the pair that let one person design, build, debug, and ship a
system this broad on nights and weekends. That collaboration is the interesting
part, not something to paper over.

## License

MIT — see [LICENSE](LICENSE).
