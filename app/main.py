"""FastAPI backend — Stage 2: data pull endpoints.

Endpoints live now:
  GET /api/health     — config + which data sources are reachable
  GET /api/race       — countdown + derived phase
  GET /api/readiness  — this-morning Garmin recovery panel
  GET /api/load       — recent activities + volume by sport
  GET /api/fitness    — VO2max (run+bike), FTP, race predictions
  GET /api/calendar   — upcoming events (or a clear 'missing credentials' note)
  GET /api/morning    — combined morning payload (cached)

Plan model, coach agent, and frontend come in later stages.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (activity_detail, baselines, calendar_agent, calendar_source,
               calendar_sync, coach, config, db, fitness_trend, garmin_source,
               garmin_workout, insights, nudges, nutrition, plan, plan_adapt,
               push, rings, ring_detail, suggest)

app = FastAPI(title="Tri Coach")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _bootstrap_garmin_token() -> None:
    """Seed the Garmin OAuth token from a base64 secret on first boot (cloud).

    Logging in fresh from a datacenter IP trips Garmin's bot checks, so instead
    we ship the already-authenticated token from the Mac as GARMIN_TOKEN_B64 and
    write it into the (persistent) token store if it's not there yet. Once valid,
    garminconnect refreshes it in place on the volume.
    """
    import base64
    import os
    blob = os.environ.get("GARMIN_TOKEN_B64")
    if not blob:
        return
    store = Path(os.path.expanduser(config.GARMIN_TOKENSTORE))
    target = store / "garmin_tokens.json"
    if target.exists():
        return
    try:
        store.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(blob))
        os.chmod(target, 0o600)
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap] could not seed Garmin token: {e}")


_bootstrap_garmin_token()


def _bootstrap_plan() -> None:
    """Seed the periodized plan on first boot if the DB is empty (fresh cloud
    volume). Never clobbers an existing plan."""
    try:
        if not db.get_plan():
            plan.seed()
            print("[bootstrap] seeded fresh plan")
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap] plan seed skipped: {e}")


_bootstrap_plan()


@app.middleware("http")
async def _auth_gate(request, call_next):
    """Protect /api/* with a shared secret when ACCESS_TOKEN is set (public host).
    The static shell (/, /static, manifest) stays open; it's useless without the
    token, which the page collects and sends on every API call."""
    import hmac
    path = request.url.path
    # The push cron endpoint is called by an external scheduler that won't have the
    # ACCESS_TOKEN — it carries its own PUSH_CRON_KEY, checked inside the handler.
    if path == "/api/push/run":
        return await call_next(request)
    token = config.ACCESS_TOKEN
    if token and path.startswith("/api/"):
        sent = (request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or request.headers.get("x-access-token", "").strip())
        if not (sent and hmac.compare_digest(sent, token)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)

_cache: dict[str, Any] = {"morning": None, "ts": 0.0}
_CACHE_TTL = 300
_lock = threading.Lock()


def _bg_sync() -> None:
    """Fire-and-forget calendar reconcile after a plan change. Never blocks the
    response or raises — Google being down must not break plan edits."""
    def run():
        try:
            calendar_sync.reconcile()
        except Exception as e:  # pragma: no cover
            print(f"[calendar_sync] reconcile failed: {type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


# Reconcile once on startup so Google reflects the current plan even if nothing
# is edited this session (no-op when Google isn't connected).
_bg_sync()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "race": config.race_phase(),
        "athlete_profile": config.ATHLETE_PROFILE,
        "sources": {
            "garmin": {"path": "direct garminconnect"},
            "calendar": {"enabled": True, "note": "Google Calendar (read-only) — needs credentials.json"},
            "anthropic_key_present": bool(config.ANTHROPIC_API_KEY)
                and not config.ANTHROPIC_API_KEY.strip().endswith("..."),
        },
    }


@app.get("/api/race")
def race() -> dict[str, Any]:
    return config.race_phase()


@app.get("/api/readiness")
def readiness() -> JSONResponse:
    try:
        rd = garmin_source.get_readiness()
        try:
            baselines.record_from_readiness(rd)  # snapshot for personal baselines
        except Exception:
            pass
        return JSONResponse(rd)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.get("/api/load")
def load(days: int = 14) -> JSONResponse:
    try:
        return JSONResponse(garmin_source.get_recent_load(days))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.post("/api/workout/push")
def workout_push(body: dict = Body(default={})) -> JSONResponse:
    """Push a session to Garmin as a scheduled structured workout (→ watch)."""
    import datetime
    date_str = body.get("date") or config.local_today().isoformat()
    sess = body.get("session")
    if not sess:  # no explicit session → resolve from today's plan/suggestion
        s = suggest.todays_suggestion() or {}
        sig = s.get("readiness_signal") or {}
        planned = s.get("suggestion")   # == the planned session
        adjusted = s.get("adjusted")    # readiness-eased alternative, if any
        # Readiness suggests easing off. NEVER swap silently — surface both options
        # and let the athlete decide (they re-push the chosen session explicitly).
        # confirm=True means they've already acknowledged.
        if planned and adjusted and sig.get("downregulate") and not body.get("confirm"):
            return JSONResponse({
                "needs_confirmation": True,
                "reason": sig.get("reason") or "recovery markers are low",
                "level": sig.get("level"),
                "hard": bool(sig.get("hard")),
                "planned": planned,
                "adjusted": adjusted,
            })
        sess = planned  # default to the plan; the dialog offers the easier option
    if not sess:
        return JSONResponse({"error": "no workout available to push"}, status_code=400)
    try:
        result = garmin_workout.push(sess, date_str)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.post("/api/workout/delete")
def workout_delete(body: dict = Body(...)) -> JSONResponse:
    wid = body.get("workout_id")
    if not wid:
        return JSONResponse({"error": "workout_id required"}, status_code=400)
    return JSONResponse(garmin_workout.delete(wid))


@app.get("/api/rings")
def get_rings() -> JSONResponse:
    try:
        return JSONResponse(rings.get_rings())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/ring/{name}")
def ring_breakdown(name: str) -> JSONResponse:
    """WHOOP-style click-through breakdown for one of the four rings."""
    try:
        result = ring_detail.get_detail(name)
        return JSONResponse(result, status_code=404 if result.get("error") else 200)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/activity/{activity_id}")
def get_activity_detail(activity_id: int) -> JSONResponse:
    try:
        return JSONResponse(activity_detail.get_detail(activity_id))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.post("/api/activity/{activity_id}/analyze")
def analyze_activity(activity_id: int) -> JSONResponse:
    result = activity_detail.analyze(activity_id)
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.get("/api/training_load")
def training_load() -> JSONResponse:
    try:
        return JSONResponse(garmin_source.get_training_load())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.get("/api/fitness")
def fitness() -> JSONResponse:
    try:
        return JSONResponse(garmin_source.get_fitness_markers())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.get("/api/pmc")
def pmc(days: int = 90) -> JSONResponse:
    """Fitness (CTL) / Fatigue (ATL) / Form (TSB) time series."""
    try:
        return JSONResponse(fitness_trend.get_pmc(days))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.get("/api/baselines")
def get_baselines() -> JSONResponse:
    try:
        return JSONResponse(baselines.get_baselines())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.post("/api/baselines/backfill")
def backfill_baselines(days: int = 30) -> JSONResponse:
    """One-shot historical seed of recovery markers (slow; run once)."""
    try:
        return JSONResponse(baselines.backfill(days))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"}, status_code=502)


@app.get("/api/insights")
def get_insights() -> JSONResponse:
    try:
        return JSONResponse(insights.get_insights())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/nudges")
def get_nudges() -> JSONResponse:
    try:
        return JSONResponse(nudges.get_nudges())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "nudges": []}, status_code=200)


# --- Web push (proactive nudge notifications) --------------------------------
@app.get("/api/push/vapid")
def push_vapid() -> JSONResponse:
    return JSONResponse({"key": push.public_key(), "configured": push.configured()})


@app.post("/api/push/subscribe")
def push_subscribe(body: dict = Body(...)) -> JSONResponse:
    sub = body.get("subscription") or body
    ok = push.save_subscription(sub)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


@app.api_route("/api/push/run", methods=["GET", "POST"])
def push_run(request: Request, force: bool = False) -> JSONResponse:
    """Called by the EXTERNAL scheduler (wakes the machine). Accepts GET or POST so
    any cron service works out of the box. Auth = PUSH_CRON_KEY."""
    import hmac
    key = config.PUSH_CRON_KEY
    sent = request.query_params.get("key", "") or request.headers.get("x-cron-key", "")
    if not (key and hmac.compare_digest(sent, key)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(push.run(force=force))


@app.post("/api/plan/feedback")
def plan_feedback(body: dict = Body(...)) -> JSONResponse:
    """Post-session RPE + completion → adapt the next few days."""
    import datetime
    date_str = body.get("date") or config.local_today().isoformat()
    rpe = body.get("rpe")
    try:
        rpe = int(rpe) if rpe is not None and rpe != "" else None
    except (TypeError, ValueError):
        rpe = None
    result = plan_adapt.apply_session_feedback(
        date_str, status=body.get("status", "done"), rpe=rpe, note=body.get("note", ""))
    db.set_meta("brief_sig", "")  # force the coach to re-brief against the change
    _bg_sync()
    return JSONResponse(result)


@app.post("/api/plan/reflow")
def plan_reflow(body: dict = Body(...)) -> JSONResponse:
    """Reshape the coming week around illness / travel / a missed session."""
    result = plan_adapt.reflow(kind=body.get("kind", "illness"),
                               days=body.get("days", 3), note=body.get("note", ""))
    db.set_meta("brief_sig", "")
    _bg_sync()
    return JSONResponse(result)


@app.get("/api/morning")
def morning(force: bool = False) -> JSONResponse:
    """Combined morning payload, cached so the page is fast and tolerant of
    delayed watch syncs. Each section degrades independently."""
    now = time.time()
    with _lock:
        if _cache["morning"] and not force and (now - _cache["ts"]) < _CACHE_TTL:
            return JSONResponse({"cached": True, "age_s": round(now - _cache["ts"]), **_cache["morning"]})

    def safe(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    payload = {
        "race": config.race_phase(),
        "readiness": safe(garmin_source.get_readiness),
        "fitness": safe(garmin_source.get_fitness_markers),
        "load": safe(lambda: garmin_source.get_recent_load(14)),
        "suggestion": safe(suggest.todays_suggestion),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _cache["morning"] = payload
        _cache["ts"] = time.time()
    return JSONResponse({"cached": False, "age_s": 0, **payload})


# --- Training plan ------------------------------------------------------------
@app.get("/api/plan")
def get_plan(start: str | None = None, end: str | None = None) -> JSONResponse:
    return JSONResponse({"summary": db.plan_summary(), "days": db.get_plan(start, end)})


# NOTE: must be declared BEFORE /api/plan/{date} or "history" is parsed as a date.
@app.get("/api/plan/history")
def plan_history(limit: int = 40) -> JSONResponse:
    """Recent plan changes (for the review + undo view)."""
    return JSONResponse({"history": db.get_plan_history(limit)})


@app.get("/api/plan/{date}")
def get_plan_day(date: str) -> JSONResponse:
    day = db.get_plan_day(date)
    if not day:
        return JSONResponse({"error": f"no plan day for {date}"}, status_code=404)
    return JSONResponse(day)


@app.put("/api/plan/{date}")
def edit_plan_day(date: str, fields: dict = Body(...)) -> JSONResponse:
    updated = db.edit_plan_day(date, fields)
    if not updated:
        return JSONResponse({"error": f"no plan day for {date}"}, status_code=404)
    _bg_sync()
    return JSONResponse(updated)


@app.post("/api/plan/seed")
def seed_plan(overwrite_edited: bool = False) -> JSONResponse:
    result = plan.seed(overwrite_edited=overwrite_edited)
    _bg_sync()
    return JSONResponse(result)


@app.post("/api/plan/revert/{history_id}")
def plan_revert(history_id: int) -> JSONResponse:
    reverted = db.revert_plan_history(history_id)
    if not reverted:
        return JSONResponse({"error": "history entry not found"}, status_code=404)
    db.set_meta("brief_sig", "")
    _bg_sync()
    return JSONResponse({"ok": True, "day": reverted})


# --- Completions / constraints ------------------------------------------------
@app.post("/api/completions")
def add_completion(body: dict = Body(...)) -> JSONResponse:
    db.add_completion(body["date"], body.get("status", "done"),
                      body.get("notes", ""), body.get("garmin_activity_id", ""))
    return JSONResponse({"ok": True})


@app.get("/api/completions")
def list_completions(start: str | None = None, end: str | None = None) -> JSONResponse:
    return JSONResponse({"completions": db.get_completions(start, end)})


@app.post("/api/constraints")
def add_constraint(body: dict = Body(...)) -> JSONResponse:
    db.add_constraint(body.get("date"), body["text"])
    return JSONResponse({"ok": True})


# --- Today's suggested workout -----------------------------------------------
@app.get("/api/suggestion")
def suggestion() -> JSONResponse:
    try:
        return JSONResponse(suggest.todays_suggestion())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


# --- Coach agent --------------------------------------------------------------
@app.get("/api/coach/history")
def coach_history(limit: int = 50, all: bool = False) -> JSONResponse:
    import datetime
    since = None if all else config.local_today().isoformat()
    return JSONResponse({"messages": db.get_chat(limit, since=since)})


def _training_signature() -> str:
    """A fingerprint of today's training state — changes when a new activity is
    completed (so the brief regenerates and re-evaluates) and when the day
    crosses into the evening window (so the nightly review fires)."""
    today = config.local_today().isoformat()
    try:
        acts = garmin_source.get_recent_load(2).get("activities", [])
        ids = sorted(str(a.get("activity_id")) for a in acts if (a.get("date") or "") == today)
    except Exception:
        ids = []
    part = "eve" if coach._is_evening(config.local_now()) else "day"
    return f"{today}|{part}|{','.join(ids)}"


@app.post("/api/coach/brief")
def coach_brief(force: bool = False) -> JSONResponse:
    # Regenerate when today's completed-training state changes (or when forced).
    sig = _training_signature()
    if not force and db.get_meta("brief_sig") == sig:
        return JSONResponse({"skipped": True, "reason": "already briefed for current training state"})
    result = coach.morning_brief()
    if not result.get("error"):
        db.set_meta("brief_sig", sig)
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.post("/api/coach/clear")
def coach_clear(body: dict = Body(default={})) -> JSONResponse:
    import datetime
    since = None if body.get("all") else config.local_today().isoformat()
    deleted = db.clear_chat(since=since)
    db.set_meta("brief_sig", "")  # force a fresh briefing on the next load
    return JSONResponse({"ok": True, "deleted": deleted})


@app.post("/api/coach/chat")
def coach_chat(body: dict = Body(...)) -> JSONResponse:
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    result = coach.chat(message, log_as_constraint=bool(body.get("log_as_constraint")))
    status = 502 if result.get("error") else 200
    return JSONResponse(result, status_code=status)


@app.post("/api/coach/accept")
def coach_accept(body: dict = Body(...)) -> JSONResponse:
    adj = body.get("adjustment")
    if not isinstance(adj, dict):
        return JSONResponse({"error": "adjustment object is required"}, status_code=400)
    updated = coach.accept_adjustment(adj)
    if not updated:
        return JSONResponse({"error": "no plan day for today to update"}, status_code=404)
    _bg_sync()
    return JSONResponse({"ok": True, "updated_plan_day": updated})


@app.post("/api/coach/accept_week")
def coach_accept_week(body: dict = Body(...)) -> JSONResponse:
    """Apply a whole-week rebuild proposed by the coach."""
    days = body.get("week") or body.get("days")
    if not isinstance(days, list) or not days:
        return JSONResponse({"error": "week array is required"}, status_code=400)
    result = coach.accept_weekplan(days)
    if result.get("ok"):
        _bg_sync()
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.post("/api/activity/manual")
def log_manual_activity(body: dict = Body(...)) -> JSONResponse:
    """Record coach-reported / manually-logged activities (before a Garmin sync)."""
    acts = body.get("activities")
    if isinstance(body.get("activity"), dict):
        acts = [body["activity"]]
    if not isinstance(acts, list) or not acts:
        return JSONResponse({"error": "activities array is required"}, status_code=400)
    result = coach.log_activities(acts)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/api/activity/manual/{entry_id}")
def delete_manual_activity(entry_id: int) -> JSONResponse:
    return JSONResponse({"ok": db.delete_manual_activity(entry_id)})


# --- Nutrition (Chef Gordo) ---------------------------------------------------
@app.get("/api/nutrition/day")
def nutrition_day() -> JSONResponse:
    try:
        return JSONResponse(nutrition.get_day())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.post("/api/nutrition/log")
def nutrition_log(body: dict = Body(...)) -> JSONResponse:
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    result = nutrition.log_food(text)
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.post("/api/nutrition/suggest")
def nutrition_suggest(body: dict = Body(default={})) -> JSONResponse:
    result = nutrition.suggest_meal((body.get("request") or "").strip())
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.delete("/api/nutrition/{entry_id}")
def nutrition_delete(entry_id: int) -> JSONResponse:
    ok = db.delete_nutrition(entry_id)
    return JSONResponse({"ok": ok, "day": nutrition.get_day()})


@app.get("/api/calendar")
def calendar(days: int = 7) -> JSONResponse:
    """Upcoming Google Calendar events, normalized for the Calendar tab. Degrades
    to a clear 'not connected' state when credentials/token are missing."""
    days = max(1, min(int(days or 7), 31))
    data = calendar_source.get_events(days=days)
    if not data.get("available"):
        return JSONResponse({
            "connected": False,
            "reason": data.get("reason"),
            "note": data.get("detail") or "Google Calendar isn't connected yet.",
            "events": [], "days": days,
        })
    events = []
    for e in data.get("events", []):
        if e.get("_error"):
            continue
        start = e.get("start") or ""
        events.append({
            "title": e.get("summary"),
            "date": start[:10],
            "start": e.get("start"),
            "all_day": bool(e.get("all_day")),
            "location": e.get("location"),
        })
    return JSONResponse({"connected": True, "days": days, "events": events})


@app.post("/api/calendar/sync")
def calendar_sync_now() -> JSONResponse:
    """Full two-way sync: pull edits made in Google back into the plan, then push
    the plan out (create/update/prune). Called on Calendar-tab open + Sync button."""
    return JSONResponse(calendar_sync.sync())


@app.post("/api/workout/move")
def workout_move(body: dict = Body(default={})) -> JSONResponse:
    """Reschedule/resize a session from a calendar drag: change time, day, and/or
    duration (a drop onto an occupied day swaps the two). Persists immediately and
    pushes to Google in the BACKGROUND so the UI updates without a multi-second wait."""
    date = body.get("date")
    if not date:
        return JSONResponse({"error": "date is required"}, status_code=400)
    result = calendar_sync.move_session(
        date, new_date=body.get("new_date"), new_start=body.get("new_start"),
        new_duration=body.get("new_duration"), do_reconcile=False)
    if not result.get("error"):
        # Push ONLY the changed day(s) in the background — not a full 22-event sync.
        changed = result.get("changed") or [date]
        threading.Thread(target=lambda: calendar_sync.push_days(changed), daemon=True).start()
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/calendar/command")
def calendar_command(body: dict = Body(default={})) -> JSONResponse:
    """Natural-language calendar assistant: 'move my bike to Thursday', 'add
    dentist Friday 2pm', 'make Saturday's ride 2 hours'. Applies + syncs."""
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    result = calendar_agent.command(text)
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.post("/api/calendar/event")
def calendar_event(body: dict = Body(default={})) -> JSONResponse:
    """Create a personal (non-workout) event on Google Calendar — used by the
    'Add' button on a coach-proposed event."""
    title = (body.get("title") or "").strip()
    date = body.get("date")
    if not title or not date:
        return JSONResponse({"error": "title and date are required"}, status_code=400)
    result = calendar_source.create_personal_event(
        title=title, date=date, start=body.get("start"),
        duration_min=body.get("duration_min"), all_day=bool(body.get("all_day")))
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.post("/api/fuel")
def fuel(body: dict = Body(default={})) -> JSONResponse:
    """Intra-workout fueling (carbs / fluids / electrolytes) for a given session.
    Surfaced inside the workout detail sheet."""
    sess = body.get("session")
    if not sess:  # fall back to today's resolved session
        sess = (suggest.todays_suggestion() or {}).get("suggestion") or {}
    return JSONResponse(nutrition.fueling_plan(sess))


# --- Frontend ----------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    # The single-page shell must NEVER be cached, or WKWebView (the Mac app) keeps
    # serving a stale index.html and misses new features (e.g. the Nutrition tab)
    # while the phone/PWA — which revalidates — shows the update.
    return FileResponse(STATIC_DIR / "index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from root so its scope can control the whole PWA (push + notifications).
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse({
        "name": "Tri Coach", "short_name": "Tri Coach", "start_url": "/",
        "display": "standalone", "background_color": "#080b10", "theme_color": "#080b10",
        "icons": [{"src": "/static/icon.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    print(f"\n  Tri-coach dashboard → http://{config.HOST}:{config.PORT}\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
