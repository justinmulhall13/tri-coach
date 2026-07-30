"""Two-way sync between the training plan and Google Calendar.

- reconcile(): push upcoming plan days to Google as tagged workout events,
  filling in a default start-time where the user hasn't set one, and pruning
  Google events whose day no longer maps to a synced plan day. Idempotent —
  safe to call after any plan mutation.
- move_session(): the drag handler. Change a session's time, move it to another
  day, or (if the target day is occupied) swap the two days' sessions. Then
  reconcile so both Google events update.

Everything degrades gracefully when Google isn't connected: reconcile returns a
status dict rather than raising, so plan mutations never fail because of sync.
"""
from __future__ import annotations

import datetime
from typing import Any

from . import calendar_source, config, db, schedule_time


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# Fields that make up "the session" — what moves/swaps between days.
_SESSION_FIELDS = ("discipline", "title", "structure", "duration_min",
                   "intensity", "why", "is_rest")

_REST = {"discipline": "rest", "title": "Rest", "intensity": "rest",
         "duration_min": 0, "is_rest": 1,
         "structure": {"warmup": "", "main": "Full rest", "cooldown": ""},
         "why": "Rest day."}


def _upcoming(days: int) -> list[dict[str, Any]]:
    today = config.local_today().isoformat()
    end = (config.local_today() + datetime.timedelta(days=days)).isoformat()
    return [d for d in db.get_plan(today, end) if d["date"] >= today]


def reconcile(days: int = 21) -> dict[str, Any]:
    """Ensure every upcoming plan day has a matching, up-to-date Google event."""
    status = calendar_source._creds_status()
    if not status.get("token_present") or not status.get("credentials_present"):
        return {"connected": False, "synced": 0,
                "note": "Google Calendar not connected — skipped sync."}

    # Existing timed events (for smart default placement / collision-avoidance).
    read = calendar_source.get_events(days=days)
    existing = read.get("events", []) if read.get("available") else []

    upcoming = _upcoming(days)
    synced = 0
    errors: list[str] = []
    kept_by_date: dict[str, str] = {}   # date -> the ONE event id we keep

    for day in upcoming:
        # Fill a default start-time once; user drags win thereafter.
        if not day.get("start_time") and not day.get("is_rest"):
            st = schedule_time.default_start(day, existing)
            if st:
                day["start_time"] = st
                db.edit_plan_day(day["date"], {"start_time": st, "pos_updated_at": _utcnow()},
                                 source=day.get("source", "seed"), record_history=False)
        res = calendar_source.upsert_workout_event(day)
        if res.get("error"):
            errors.append(f"{day['date']}: {res['error']}")
            continue
        eid = res.get("event_id")
        if eid and eid != day.get("gcal_event_id"):
            db.edit_plan_day(day["date"], {"gcal_event_id": eid},
                             source=day.get("source", "seed"), record_history=False)
        if eid:
            kept_by_date[day["date"]] = eid
        synced += 1

    # Prune: delete every workout event that ISN'T the one we track for its day.
    # This removes both stale (out-of-window/moved) events AND same-day duplicates
    # accumulated from earlier syncs — guaranteeing exactly one event per day.
    pruned = 0
    today = config.local_today().isoformat()
    for ev in calendar_source.fetch_workout_events(days=max(days, 60)).get("events", []):
        d, eid = ev.get("plan_date"), ev.get("id")
        if not eid or not d or d < today:
            continue  # leave past-day (already-done) events untouched
        if kept_by_date.get(d) != eid:
            calendar_source.delete_workout_event(eid)
            pruned += 1

    return {"connected": True, "synced": synced, "pruned": pruned,
            "errors": errors or None,
            "synced_at": datetime.datetime.now().isoformat(timespec="seconds")}


def _session_fields(day: dict[str, Any]) -> dict[str, Any]:
    return {k: day.get(k) for k in _SESSION_FIELDS}


def push_days(dates: list[str]) -> dict[str, Any]:
    """Push ONLY the given days' workout events to Google (create/update, and
    delete the event when a day became rest). Much faster than a full reconcile —
    no 22-event scan/prune. Used after a single drag / calendar-assistant edit."""
    status = calendar_source._creds_status()
    if not status.get("token_present") or not status.get("credentials_present"):
        return {"connected": False, "pushed": 0}
    pushed = 0
    for date in dict.fromkeys(d for d in dates if d):  # dedupe, keep order
        day = db.get_plan_day(date)
        if not day:
            continue
        if day.get("is_rest"):
            # Day became rest → remove its timed workout event if we had one.
            if day.get("gcal_event_id"):
                calendar_source.delete_workout_event(day["gcal_event_id"])
                db.edit_plan_day(date, {"gcal_event_id": None},
                                 source=day.get("source", "seed"), record_history=False)
            continue
        if not day.get("start_time"):
            st = schedule_time.default_start(day, [])
            if st:
                day["start_time"] = st
                db.edit_plan_day(date, {"start_time": st, "pos_updated_at": _utcnow()},
                                 source=day.get("source", "seed"), record_history=False)
        res = calendar_source.upsert_workout_event(day)
        eid = res.get("event_id")
        if eid and eid != day.get("gcal_event_id"):
            db.edit_plan_day(date, {"gcal_event_id": eid},
                             source=day.get("source", "seed"), record_history=False)
        if eid:
            pushed += 1
    return {"connected": True, "pushed": pushed}


def _parse_dt(iso: str | None):
    """Parse an ISO8601 string (with offset or trailing Z) to an aware datetime."""
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _event_local_datetime(ev: dict[str, Any]) -> tuple[str | None, str | None]:
    """(date, HH:MM) of an event's start in the athlete's local timezone. Time is
    None for all-day events."""
    if ev.get("all_day"):
        return (ev.get("start") or "")[:10] or None, None
    dt = _parse_dt(ev.get("start"))
    if not dt:
        return None, None
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(config.TIMEZONE))
    except Exception:
        pass
    return dt.date().isoformat(), dt.strftime("%H:%M")


def pull_from_google(days: int = 40) -> dict[str, Any]:
    """Bring workout-event edits made in Google back into the plan. Most-recent
    wins: an event whose position changed and whose `updated` is newer than the
    day's `pos_updated_at` is adopted. Time changes and moves onto an empty/rest
    day are applied; a move onto an already-occupied day is left to the app side
    (reconcile will restore it), to avoid ambiguous swaps."""
    status = calendar_source._creds_status()
    if not status.get("token_present") or not status.get("credentials_present"):
        return {"connected": False, "pulled": 0, "note": "Google Calendar not connected."}

    res = calendar_source.fetch_workout_events(days=days)
    if res.get("error"):
        return {"connected": True, "pulled": 0, "error": res["error"]}

    changes: list[dict[str, Any]] = []
    skipped_occupied: list[str] = []

    for ev in res.get("events", []):
        plan_date = ev.get("plan_date")
        if not plan_date:
            continue
        day = db.get_plan_day(plan_date)
        if not day:
            continue
        # Only trust the single event we track for this day.
        if day.get("gcal_event_id") and ev.get("id") != day.get("gcal_event_id"):
            continue

        ev_date, ev_time = _event_local_datetime(ev)
        if not ev_date:
            continue
        plan_time = day.get("start_time")
        if ev_date == plan_date and ev_time == plan_time:
            continue  # nothing moved

        # Most-recent-wins: is Google's edit newer than the app's last position change?
        ev_upd = _parse_dt(ev.get("updated"))
        pos_ts = _parse_dt(day.get("pos_updated_at"))
        if ev_upd and pos_ts and ev_upd <= pos_ts:
            continue  # app changed it more recently → push will correct Google

        if ev_date == plan_date:
            move_session(plan_date, new_start=ev_time, pos_ts=ev.get("updated"), do_reconcile=False)
            changes.append({"date": plan_date, "kind": "time", "to": ev_time})
        else:
            dst = db.get_plan_day(ev_date)
            if dst and not dst.get("is_rest"):
                skipped_occupied.append(f"{plan_date}→{ev_date}")
                continue  # ambiguous swap — leave for the app / reconcile restore
            move_session(plan_date, new_date=ev_date, new_start=ev_time,
                         pos_ts=ev.get("updated"), do_reconcile=False)
            changes.append({"date": plan_date, "kind": "day", "to": ev_date, "at": ev_time})

    return {"connected": True, "pulled": len(changes), "changes": changes,
            "skipped_occupied": skipped_occupied or None}


def sync(days: int = 21) -> dict[str, Any]:
    """Full two-way sync: pull Google edits in, then push the plan out."""
    pull = pull_from_google(days=max(days, 40))
    push = reconcile(days=days)
    return {"connected": push.get("connected", pull.get("connected")),
            "pulled": pull.get("pulled", 0), "pull": pull,
            "synced": push.get("synced", 0), "pruned": push.get("pruned", 0),
            "errors": push.get("errors"), "synced_at": push.get("synced_at")}


def move_session(date: str, new_date: str | None = None,
                 new_start: str | None = None, new_duration: int | None = None,
                 pos_ts: str | None = None, do_reconcile: bool = True) -> dict[str, Any]:
    """Reschedule a session by time and/or day, and/or resize its duration.
    Cross-day onto an occupied day swaps the two days' sessions. `pos_ts` stamps
    when the position changed (defaults to now; the reverse-sync passes Google's
    event.updated so it wins consistently). `do_reconcile=False` skips the push."""
    src = db.get_plan_day(date)
    if not src:
        return {"error": f"no plan day for {date}"}
    pts = pos_ts or _utcnow()
    dur = None
    if new_duration is not None:
        try:
            dur = max(15, int(new_duration))
        except (TypeError, ValueError):
            dur = None

    changed: list[str] = []

    # Same-day (or no day change): just set the time / duration.
    if not new_date or new_date == date:
        fields = {}
        if new_start:
            fields["start_time"] = new_start
        if dur is not None:
            fields["duration_min"] = dur
        if fields:
            fields["pos_updated_at"] = pts
            db.edit_plan_day(date, fields, source="edited",
                             reason="Rescheduled from calendar drag")
        changed = [date]
    else:
        dst = db.get_plan_day(new_date)
        src_sess = _session_fields(src)
        if not dst or dst.get("is_rest"):
            # Move into an empty/rest day; source becomes rest.
            move_fields = dict(src_sess)
            move_fields["start_time"] = new_start or src.get("start_time")
            if dur is not None:
                move_fields["duration_min"] = dur
            move_fields["pos_updated_at"] = pts
            db.edit_plan_day(new_date, move_fields, source="edited",
                             reason=f"Moved from {date} (calendar drag)")
            rest = dict(_REST); rest["pos_updated_at"] = pts
            db.edit_plan_day(date, rest, source="edited",
                             reason=f"Session moved to {new_date}")
        else:
            # Swap the two days' sessions; each keeps its own start_time unless
            # a new one was dropped on the target.
            dst_sess = _session_fields(dst)
            dst_fields = dict(src_sess)
            dst_fields["start_time"] = new_start or dst.get("start_time")
            dst_fields["pos_updated_at"] = pts
            db.edit_plan_day(new_date, dst_fields, source="edited",
                             reason=f"Swapped with {date} (calendar drag)")
            src_fields = dict(dst_sess)
            src_fields["start_time"] = src.get("start_time")
            src_fields["pos_updated_at"] = pts
            db.edit_plan_day(date, src_fields, source="edited",
                             reason=f"Swapped with {new_date} (calendar drag)")
        changed = [date, new_date]

    sync = reconcile() if do_reconcile else None
    return {"ok": True, "changed": changed,
            "days": [db.get_plan_day(d) for d in changed], "sync": sync}
