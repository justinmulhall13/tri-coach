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
import json
from typing import Any

from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (activity_detail, baselines, calendar_agent, calendar_source,
               calendar_sync, coach, coaching_contract, config, db, fitness_trend, garmin_source,
               garmin_workout, hevy_actions, hevy_connector, insights, nudges, nutrition, plan, plan_adapt, zones,
               lifting_rules, lifting_split, lifting_stats,
               push, rings, ring_detail, strength_block, strength_effort, suggest)

app = FastAPI(title="Tri Coach")

# A strength block longer than a year is not a training block; capping it
# also keeps week arithmetic inside the date range.
MAX_BLOCK_WEEKS = 52
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
    """Seed the active profile or migrate its remaining generated rows."""
    try:
        if not db.get_plan():
            result = plan.seed()
            if result.get("error"):
                print(f"[bootstrap] active profile has no plan builder: {result['error']}")
            else:
                print(f"[bootstrap] seeded plan for {coaching_contract.event_profile_id()}")
        else:
            result = plan.reconcile_seeded_plan()
            if result.get("error"):
                print(f"[bootstrap] seeded-plan reconcile skipped: {result['error']}")
            elif result.get("reconciled"):
                print(f"[bootstrap] refreshed {result['reconciled']} generated plan rows")
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

_cache: dict[str, Any] = {"morning": None, "ts": 0.0, "date": None}
_CACHE_TTL = 300
_lock = threading.Lock()
_last_activity_revision: str | None = None


def _observe_activity_revision(payload: dict[str, Any] | None) -> bool:
    """Invalidate derived snapshots when Garmin's activity set changes."""
    global _last_activity_revision
    revision = payload.get("activity_revision") if isinstance(payload, dict) else None
    if not revision:
        return False
    with _lock:
        previous = _last_activity_revision
        _last_activity_revision = str(revision)
        changed = previous is not None and previous != revision
        if changed:
            _cache.update({"morning": None, "ts": 0.0, "date": None})
    if changed:
        coach.invalidate_context_cache()
    return changed


def _bg_sync() -> None:
    """Fire-and-forget calendar reconcile after a plan change. Never blocks the
    response or raises — Google being down must not break plan edits."""
    def run():
        try:
            calendar_sync.reconcile()
        except Exception as e:  # pragma: no cover
            print(f"[calendar_sync] reconcile failed: {type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


@app.on_event("startup")
def _startup_sync() -> None:
    """Reconcile once on startup so Google reflects the current plan even if
    nothing is edited this session (no-op when Google isn't connected).

    This deliberately runs on ASGI startup rather than at import. Importing a
    module must not perform I/O: the test suite imports `app.main` for route
    coverage, and doing this at import meant every test run fired a real
    calendar reconcile from a developer machine — which is precisely how the
    same plan gets written to Google from both here and Fly.
    """
    _bg_sync()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "race": config.race_phase(),
        "athlete_profile": config.ATHLETE_PROFILE,
        "sources": {
            "garmin": {"path": "direct garminconnect"},
            "calendar": {"enabled": True, "note": "Google Calendar (read-only) — needs credentials.json"},
            "hevy": hevy_connector.status(),
            "anthropic_key_present": bool(config.ANTHROPIC_API_KEY)
                and not config.ANTHROPIC_API_KEY.strip().endswith("..."),
        },
    }


@app.get("/api/race")
def race() -> dict[str, Any]:
    return config.race_phase()


@app.get("/api/integrations/hevy")
def hevy_status() -> dict[str, Any]:
    """Truthful runtime capability report; no Hevy read or write is attempted."""
    return hevy_connector.status()


@app.get("/api/integrations/hevy/workouts/recent")
def hevy_recent_workouts(limit: int = 5) -> JSONResponse:
    """Return a bounded list of recent Hevy workout summaries."""
    state = hevy_connector.status()
    if not state.get("connected"):
        return JSONResponse(
            {"error": state.get("reason") or "Hevy is not connected"}, status_code=503,
        )
    page_size = max(1, min(10, int(limit)))
    try:
        payload = hevy_connector.connector().get_workouts(page=1, page_size=page_size)
    except (hevy_connector.HevyAPIError, hevy_connector.HevyUnavailableError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(payload)


@app.get("/api/integrations/hevy/exercise-templates/search")
def hevy_exercise_template_search(q: str = "") -> JSONResponse:
    """Search real Hevy exercise templates before a routine is proposed."""
    query = (q or "").strip()
    if not query:
        return JSONResponse({"error": "q is required"}, status_code=400)
    state = hevy_connector.status()
    if not state.get("connected"):
        return JSONResponse(
            {"error": state.get("reason") or "Hevy is not connected"}, status_code=503,
        )
    try:
        templates = hevy_connector.connector().search_exercise_templates(query)
    except (hevy_connector.HevyAPIError, hevy_connector.HevyUnavailableError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"query": query, "exercise_templates": templates[:25]})


@app.post("/api/integrations/hevy/routines")
def hevy_create_routine(body: dict = Body(default={})) -> JSONResponse:
    """Create one proposed routine only after explicit athlete confirmation.

    The service call performs one non-retrying write. An ambiguous failure is
    returned to the UI with the instruction to inspect Hevy before doing
    anything else.
    """
    if body.get("confirmed") is not True:
        return JSONResponse(
            {"error": "explicit confirmation is required", "created": False}, status_code=400,
        )
    routine = body.get("routine")
    if not isinstance(routine, dict):
        return JSONResponse(
            {"error": "routine object is required", "created": False}, status_code=400,
        )
    operation_id = str(body.get("operation_id") or "").strip()
    if not operation_id:
        return JSONResponse(
            {"error": "operation_id is required", "created": False}, status_code=400,
        )
    if len(operation_id) > 200:
        return JSONResponse(
            {"error": "operation_id is too long", "created": False}, status_code=400,
        )
    result = hevy_actions.create_confirmed_routine(
        routine, operation_id=operation_id, confirmed=True,
    )
    if result.get("created"):
        return JSONResponse(result)
    status_code = 502 if result.get("retry_safe") is False else 400
    if not hevy_connector.status().get("connected"):
        status_code = 503
    return JSONResponse(result, status_code=status_code)


@app.get("/api/readiness")
def readiness() -> JSONResponse:
    try:
        rd = garmin_source.get_readiness()
        try:
            baselines.record_from_readiness(rd)  # snapshot for personal baselines
        except Exception:
            pass
        return JSONResponse(rd, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"},
                            status_code=502, headers={"Cache-Control": "no-store"})


@app.get("/api/load")
def load(days: int = 14) -> JSONResponse:
    try:
        payload = garmin_source.get_recent_load(days)
        # The dashboard's canonical 14-day pull establishes the activity
        # revision. A later sync (including a just-finished race) immediately
        # expires Coach/readiness caches instead of waiting out their TTLs.
        if days == 14:
            _observe_activity_revision(payload)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"},
                            status_code=502, headers={"Cache-Control": "no-store"})


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
        return JSONResponse(rings.get_rings(), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502,
                            headers={"Cache-Control": "no-store"})


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
        recent = garmin_source.get_recent_load(14)
        _observe_activity_revision(recent)
        _, payload = garmin_source.reconcile_freshness(
            None, garmin_source.get_training_load(), recent
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "source": "garmin"},
                            status_code=502, headers={"Cache-Control": "no-store"})


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
        return JSONResponse(insights.get_insights(), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502,
                            headers={"Cache-Control": "no-store"})


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
    coach.invalidate_context_cache()
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
    today = config.local_today().isoformat()
    with _lock:
        if (_cache["morning"] and _cache.get("date") == today and not force
                and (now - _cache["ts"]) < _CACHE_TTL):
            return JSONResponse(
                {"cached": True, "age_s": round(now - _cache["ts"]), **_cache["morning"]},
                headers={"Cache-Control": "no-store"},
            )

    def safe(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    readiness = safe(garmin_source.get_readiness)
    fitness = safe(garmin_source.get_fitness_markers)
    load = safe(lambda: garmin_source.get_recent_load(14))
    if isinstance(load, dict):
        _observe_activity_revision(load)
    if isinstance(readiness, dict) and isinstance(load, dict):
        readiness, _ = garmin_source.reconcile_freshness(readiness, None, load)
    payload = {
        "race": config.race_phase(),
        "readiness": readiness,
        "fitness": fitness,
        "load": load,
        "suggestion": safe(lambda: suggest.todays_suggestion(
            readiness if isinstance(readiness, dict) else {},
            load if isinstance(load, dict) else {},
        )),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    coach.prime_context_cache(readiness=readiness, fitness=fitness, load=load)
    with _lock:
        _cache["morning"] = payload
        _cache["ts"] = time.time()
        _cache["date"] = today
    return JSONResponse({"cached": False, "age_s": 0, **payload},
                        headers={"Cache-Control": "no-store"})


# --- Training plan ------------------------------------------------------------
@app.get("/api/plan")
def get_plan(start: str | None = None, end: str | None = None) -> JSONResponse:
    return JSONResponse({"summary": db.plan_summary(), "days": db.get_plan(start, end)})


# NOTE: must be declared BEFORE /api/plan/{date} or the literal segment is
# parsed as a date, exactly as with /api/plan/history below.
@app.get("/api/plan/strength")
def plan_strength_range(start: str, end: str) -> JSONResponse:
    """Lifts attached to calendar days, rendered beneath that day's session."""
    return JSONResponse({"days": db.get_plan_strength_range(start, end)})


@app.post("/api/plan/strength")
def plan_strength_attach(body: dict = Body(default={})) -> JSONResponse:
    """Attach one agreed lift to a date.

    The routine is validated against the same boundary a Hevy write uses, so a
    session cannot be parked on the calendar in a shape that could never be
    created later.
    """
    date = str(body.get("date") or "").strip()
    if not date:
        return JSONResponse({"error": "date is required"}, status_code=400)
    routine, errors = hevy_actions.validate_routine(body.get("routine") or {})
    if errors or routine is None:
        return JSONResponse({"error": "invalid routine", "details": errors},
                            status_code=400)
    try:
        stored = db.upsert_plan_strength({
            "date": date,
            "slot": body.get("slot") or "full",
            "title": body.get("title") or routine.get("title") or "Strength",
            "routine": routine,
            "effort_level": body.get("effort_level"),
            "effort_cue": body.get("effort_cue"),
            "hevy_routine_id": body.get("hevy_routine_id"),
            "source": body.get("source") or "coach",
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "day": stored})


@app.delete("/api/plan/strength/{date}")
def plan_strength_remove(date: str) -> JSONResponse:
    return JSONResponse({"ok": True, "removed": db.delete_plan_strength(date)})


@app.post("/api/plan/strength-block")
def plan_strength_block(body: dict = Body(default={})) -> JSONResponse:
    """Preview a repeating upper/lower block placed around the existing runs.

    Returns placements only. Nothing is written until each day is attached, so
    the athlete agrees the shape before it appears on the calendar.
    """
    import datetime as _dt
    try:
        start = (_dt.date.fromisoformat(str(body["start"])[:10]) if body.get("start")
                 else config.local_today())
        until = (_dt.date.fromisoformat(str(body["until"])[:10])
                 if body.get("until") else None)
        # Both are clamped: an unclamped `weeks` overflows the date arithmetic
        # in strength_block with an OverflowError rather than a 400.
        weeks = max(1, min(int(body.get("weeks") or 1), MAX_BLOCK_WEEKS))
        sessions = int(body.get("sessions_per_week") or 4)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": f"invalid block request: {exc}"}, status_code=400)
    readiness = None
    try:
        readiness = garmin_source.get_readiness()
    except Exception:  # noqa: BLE001 - an absent score must not fail the preview
        readiness = None
    try:
        return JSONResponse(strength_block.build(
            start=start, weeks=weeks, sessions_per_week=sessions,
            plan_days=db.get_plan(), readiness=readiness, until=until,
        ))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/lifting/stats")
def lifting_stats_view() -> JSONResponse:
    """Everything the lifting tab renders, from the logged Hevy history."""
    state = hevy_connector.status()
    if not state.get("connected"):
        return JSONResponse({
            "connected": False,
            "reason": state.get("reason") or "Hevy is not connected",
            "has_data": False,
        })
    workouts = hevy_connector.all_workouts()
    payload = lifting_stats.build(workouts, today=config.local_today())
    payload["connected"] = True
    payload["workouts_read"] = len(workouts)
    return JSONResponse(payload)


@app.get("/api/lifting/split")
def lifting_split_view() -> JSONResponse:
    """The active four-day split, validated, with its reasoning attached."""
    stored = db.get_lifting_split()
    payload = lifting_split.build(stored["days"] if stored else None)
    payload["source"] = stored["source"] if stored else "default"
    payload["updated_at"] = stored["updated_at"] if stored else None
    return JSONResponse(payload)


@app.put("/api/lifting/split")
def lifting_split_save(body: dict = Body(default={})) -> JSONResponse:
    """Replace the stored split.

    The result is returned validated, so an edit that breaks a rule is saved but
    reported rather than silently accepted or silently rejected.
    """
    days = body.get("days")
    if not isinstance(days, list) or not days:
        return JSONResponse({"error": "days array is required"}, status_code=400)
    try:
        stored = db.save_lifting_split(days, source=str(body.get("source") or "edited"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    payload = lifting_split.build(stored["days"])
    payload["source"] = stored["source"]
    payload["updated_at"] = stored["updated_at"]
    return JSONResponse(payload)


@app.delete("/api/lifting/split")
def lifting_split_reset() -> JSONResponse:
    return JSONResponse({"ok": True, "reset": db.reset_lifting_split()})


@app.get("/api/lifting/rules")
def lifting_rules_view() -> JSONResponse:
    """The athlete's standing programming constraints, for display."""
    return JSONResponse(lifting_rules.summary([]))


@app.post("/api/lifting/check")
def lifting_check(body: dict = Body(default={})) -> JSONResponse:
    """Check an edited session against the injury rules before it is saved."""
    exercises = body.get("exercises")
    if not isinstance(exercises, list):
        return JSONResponse({"error": "exercises array is required"}, status_code=400)
    normalised = [{"title": str(e.get("title") or e)} if isinstance(e, dict)
                  else {"title": str(e)} for e in exercises]
    summary = lifting_rules.summary(normalised)
    summary["suggested_order"] = [e["title"] for e in lifting_rules.arrange(normalised)]
    return JSONResponse(summary)


@app.get("/api/plan/strength-effort")
def plan_strength_effort() -> JSONResponse:
    """Today's lifting ceiling, decided from run load then recovery."""
    readiness = None
    try:
        readiness = garmin_source.get_readiness()
    except Exception:  # noqa: BLE001 - an absent score must not fail the call
        readiness = None
    return JSONResponse(strength_effort.decide(
        today=config.local_today(), plan_days=db.get_plan(), readiness=readiness,
    ))


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
    date = str(body.get("date") or "").strip()
    if not date:
        return JSONResponse({"error": "date is required"}, status_code=400)
    db.add_completion(date, body.get("status", "done"),
                      body.get("notes", ""), body.get("garmin_activity_id", ""))
    coach.invalidate_context_cache()
    return JSONResponse({"ok": True})


@app.get("/api/completions")
def list_completions(start: str | None = None, end: str | None = None) -> JSONResponse:
    return JSONResponse({"completions": db.get_completions(start, end)})


@app.post("/api/constraints")
def add_constraint(body: dict = Body(...)) -> JSONResponse:
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    db.add_constraint(body.get("date"), text)
    return JSONResponse({"ok": True})


# --- Today's suggested workout -----------------------------------------------
@app.get("/api/suggestion")
def suggestion() -> JSONResponse:
    try:
        return JSONResponse(suggest.todays_suggestion(), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502,
                            headers={"Cache-Control": "no-store"})


# --- Coach agent --------------------------------------------------------------
@app.get("/api/coach/history")
def coach_history(limit: int = 50, all: bool = False) -> JSONResponse:
    import datetime
    since = None if all else config.local_today().isoformat()
    return JSONResponse({"messages": db.get_chat(limit, since=since)})


def _training_signature() -> str:
    """Fingerprint only meaningful coaching events, not app opens or clock time.

    A new wellness date means the athlete slept; a changed activity set or a new
    completion means training happened or feedback was logged. Keeping the most
    recent event markers independent of "today" prevents a midnight rollover
    from manufacturing an update when nothing actually changed.
    """
    import json

    wellness = db.get_wellness(7)
    latest_sleep = wellness[-1]["date"] if wellness else None

    try:
        acts = garmin_source.get_recent_load(7).get("activities", [])
    except Exception:
        acts = []
    dated = [a for a in acts if a.get("date")]
    latest_activity_date = max((a["date"] for a in dated), default=None)
    latest_activities = sorted(
        str(a.get("activity_id") or
            f"{a.get('sport')}|{a.get('name')}|{a.get('minutes')}|{a.get('km')}")
        for a in dated if a.get("date") == latest_activity_date
    )

    completions = db.get_completions()
    latest_completion = max(completions, key=lambda c: c.get("id") or 0) if completions else None
    completion_marker = None if not latest_completion else {
        "id": latest_completion.get("id"),
        "date": latest_completion.get("date"),
        "status": latest_completion.get("status"),
        "rpe": latest_completion.get("rpe"),
    }
    return json.dumps({
        "event_profile_id": coaching_contract.event_profile_id(),
        "sleep_date": latest_sleep,
        "activity_date": latest_activity_date,
        "activity_ids": latest_activities,
        "completion": completion_marker,
    }, sort_keys=True, separators=(",", ":"))


def _coach_unread() -> dict[str, Any] | None:
    key = coaching_contract.scoped_meta_key("coach_unread")
    raw = db.get_meta(key)
    # One-time compatibility for data written before event scoping. That data
    # was created under the only profile installed at the time: Vancouver T100.
    if raw is None and coaching_contract.event_profile_id() == "t100-vancouver-2026":
        raw = db.get_meta("coach_unread")
    if not raw:
        return None
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict) or not item.get("id") or not item.get("reply"):
        return None
    item_profile = item.get("event_profile_id")
    if item_profile and item_profile != coaching_contract.event_profile_id():
        return None
    item["event_profile_id"] = coaching_contract.event_profile_id()
    if db.get_meta(key) is None:
        db.set_meta(key, json.dumps(item, separators=(",", ":")))
    return item


def _brief_response(payload: dict[str, Any]) -> JSONResponse:
    inbox = _coach_unread()
    return JSONResponse(
        {**payload, "unread": bool(inbox), "inbox": inbox},
        status_code=502 if payload.get("error") else 200,
    )


@app.post("/api/coach/brief")
def coach_brief(force: bool = False) -> JSONResponse:
    # First call establishes a quiet baseline. Thereafter Steve speaks only when
    # sleep/recovery or completed-training state actually changes (or when forced).
    sig = _training_signature()
    sig_key = coaching_contract.scoped_meta_key("coach_event_sig")
    previous = db.get_meta(sig_key)
    if previous is None and coaching_contract.event_profile_id() == "t100-vancouver-2026":
        legacy = db.get_meta("coach_event_sig")
        if legacy is not None:
            try:
                legacy_payload = json.loads(legacy)
                if not isinstance(legacy_payload, dict):
                    raise ValueError("legacy signature is not an object")
                legacy_payload["event_profile_id"] = coaching_contract.event_profile_id()
                previous = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
            except (json.JSONDecodeError, ValueError, TypeError):
                # An unreadable legacy marker must not manufacture a launch
                # update. Establish the scoped baseline quietly.
                previous = sig
            db.set_meta(sig_key, previous)
    if not force and previous is None:
        db.set_meta(sig_key, sig)
        return _brief_response({"skipped": True, "reason": "event baseline established"})
    if not force and previous == sig:
        return _brief_response({"skipped": True, "reason": "no new sleep or workout event"})
    coach.invalidate_context_cache()
    result = coach.morning_brief()
    if not result.get("error"):
        db.set_meta(sig_key, sig)
        import datetime
        import hashlib
        event_id = hashlib.sha256(
            f"{coaching_contract.event_profile_id()}|{sig}".encode()
        ).hexdigest()[:16]
        db.set_meta(coaching_contract.scoped_meta_key("coach_unread"), json.dumps({
            "id": event_id,
            "reply": result.get("reply"),
            "celebrate": bool(result.get("celebrate")),
            "event_profile_id": coaching_contract.event_profile_id(),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }, separators=(",", ":")))
    return _brief_response(result)


@app.get("/api/coach/inbox")
def coach_inbox() -> JSONResponse:
    inbox = _coach_unread()
    return JSONResponse({"unread": bool(inbox), "inbox": inbox})


@app.post("/api/coach/inbox/read")
def coach_inbox_read(body: dict = Body(default={})) -> JSONResponse:
    """Acknowledge only the message the client actually opened.

    Matching the id avoids a slower device clearing a newer update generated
    while its request was in flight.
    """
    inbox = _coach_unread()
    event_id = body.get("id")
    if inbox and not event_id:
        return JSONResponse({"ok": False, "reason": "message id is required", "unread": True},
                            status_code=400)
    if inbox and event_id and inbox.get("id") != event_id:
        return JSONResponse({"ok": False, "reason": "newer unread message exists", "unread": True})
    if inbox:
        db.set_meta(coaching_contract.scoped_meta_key("coach_unread"), "")
    return JSONResponse({"ok": True, "unread": False})


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


@app.post("/api/coach/chat/stream")
def coach_chat_stream(body: dict = Body(...)):
    """NDJSON Coach stream: status, text deltas, then the complete result."""
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    def lines():
        for event in coach.chat_events(
                message, log_as_constraint=bool(body.get("log_as_constraint"))):
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store, no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/coach/accept")
def coach_accept(body: dict = Body(...)) -> JSONResponse:
    adj = body.get("adjustment")
    if not isinstance(adj, dict):
        return JSONResponse({"error": "adjustment object is required"}, status_code=400)
    try:
        updated = coach.accept_adjustment(adj)
    except coach.AdjustmentValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not updated:
        return JSONResponse({"error": "no plan day for today to update"}, status_code=404)
    _bg_sync()
    return JSONResponse({"ok": True, "updated_plan_day": updated})


@app.post("/api/coach/accept_week")
def coach_accept_week(body: dict = Body(...)) -> JSONResponse:
    """Apply a whole-week draft; legacy callers may still submit the array."""
    plan_id = body.get("plan_id")
    days = body.get("week") or body.get("days")
    if plan_id is None and (not isinstance(days, list) or not days):
        return JSONResponse({"error": "plan_id or week array is required"}, status_code=400)
    try:
        result = coach.accept_weekplan(days or [], plan_id=plan_id)
    except coach.WeekPlanValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    if result.get("ok"):
        _bg_sync()
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.get("/api/coach/plan-drafts/latest")
def coach_plan_draft_latest() -> JSONResponse:
    """Recover the newest unactivated schedule in a fresh app session."""
    return JSONResponse({"draft": db.get_latest_plan_draft(status="draft")})


@app.get("/api/coach/plan-drafts/{plan_id}")
def coach_plan_draft_get(plan_id: int) -> JSONResponse:
    draft = db.get_plan_draft(plan_id)
    if not draft:
        return JSONResponse({"error": "plan draft not found"}, status_code=404)
    return JSONResponse({"draft": draft})


@app.patch("/api/coach/plan-drafts/{plan_id}")
def coach_plan_draft_edit(plan_id: int, body: dict = Body(...)) -> JSONResponse:
    days = body.get("week") or body.get("days")
    if not isinstance(days, list) or not days:
        return JSONResponse({"error": "week array is required"}, status_code=400)
    try:
        coach.validate_weekplan(days)
        draft = db.update_plan_draft(plan_id, days)
    except (coach.WeekPlanValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if not draft:
        return JSONResponse({"error": "plan draft not found"}, status_code=404)
    return JSONResponse({"ok": True, "draft": draft})


@app.post("/api/coach/plan-drafts/{plan_id}/activate")
def coach_plan_draft_activate(plan_id: int) -> JSONResponse:
    try:
        result = coach.activate_weekplan(plan_id)
    except coach.WeekPlanValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
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
    ok = db.delete_manual_activity(entry_id)
    if ok:
        coach.invalidate_context_cache()
    return JSONResponse({"ok": ok})


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


@app.post("/api/nutrition/complete-fueling")
def nutrition_complete_fueling() -> JSONResponse:
    """One-tap confirmation that today's planned intra-workout fuel was taken."""
    return JSONResponse(nutrition.log_completed_fueling())


@app.post("/api/nutrition/suggest")
def nutrition_suggest(body: dict = Body(default={})) -> JSONResponse:
    result = nutrition.suggest_meal((body.get("request") or "").strip())
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


@app.delete("/api/nutrition/{entry_id}")
def nutrition_delete(entry_id: int) -> JSONResponse:
    ok = db.delete_nutrition(entry_id)
    return JSONResponse({"ok": ok, "day": nutrition.get_day()})


@app.get("/api/calendar")
def calendar(days: int = 7, start: str | None = None) -> JSONResponse:
    """Google Calendar events for a window, normalized for the Calendar tab.
    `start` (YYYY-MM-DD) lets the UI page backwards/forwards through history;
    it defaults to today. Degrades to a clear 'not connected' state."""
    import datetime as _dt
    days = max(1, min(int(days or 7), 62))
    start_dt = None
    if start:
        try:
            start_dt = _dt.datetime.combine(_dt.date.fromisoformat(start), _dt.time()).astimezone()
        except ValueError:
            start_dt = None
    data = calendar_source.get_events(start=start_dt, days=days)
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
    """Reschedule a session from a calendar drag: change time and/or day only.
    Workout duration is a coaching prescription and can only be changed by Coach.
    A drop onto an occupied day swaps the two. Persists immediately and
    pushes to Google in the BACKGROUND so the UI updates without a multi-second wait."""
    date = body.get("date")
    if not date:
        return JSONResponse({"error": "date is required"}, status_code=400)
    if body.get("new_duration") is not None:
        return JSONResponse({"error": "Workout length is set by Coach Steve, not the calendar."}, status_code=400)
    result = calendar_sync.move_session(
        date, new_date=body.get("new_date"), new_start=body.get("new_start"), do_reconcile=False)
    if not result.get("error"):
        # Push ONLY the changed day(s) in the background — not a full 22-event sync.
        changed = result.get("changed") or [date]
        threading.Thread(target=lambda: calendar_sync.push_days(changed), daemon=True).start()
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.get("/api/zones")
def athlete_zones(force: bool = False) -> JSONResponse:
    """The athlete's real Garmin HR zones (per sport), lactate threshold and FTP —
    what every prescribed workout targets."""
    try:
        if force:
            zones.get(force=True)
        return JSONResponse(zones.summary())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.post("/api/nutrition/goal")
def nutrition_goal(body: dict = Body(default={})) -> JSONResponse:
    """Set the calorie goal (deficit / maintain / surplus) — shifts daily targets."""
    goal = nutrition.set_goal(body.get("goal") or "maintain")
    return JSONResponse({"ok": True, "goal": goal, "day": nutrition.get_day()})


@app.post("/api/nutrition/photo")
def nutrition_photo(body: dict = Body(...)) -> JSONResponse:
    """Reject uncheckable photo macro estimates and request exact inputs."""
    img = body.get("image_b64")
    if not img:
        return JSONResponse({"error": "image_b64 is required"}, status_code=400)
    result = nutrition.log_photo(img, body.get("media_type") or "image/jpeg")
    return JSONResponse(result, status_code=502 if result.get("error") else 200)


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
    if not body.get("all_day") and (not body.get("start") or body.get("duration_min") is None):
        return JSONResponse(
            {"error": "timed events require an explicit start and duration_min"}, status_code=400
        )
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
