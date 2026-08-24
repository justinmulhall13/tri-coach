"""Natural-language calendar assistant.

The Calendar tab's command bar sends free text ("move my bike to Thursday
morning", "add dentist Friday 2pm", "make Saturday's ride 2 hours", "cancel the
driving range"). We give Claude the upcoming week (workouts + personal events)
and ask for a small JSON action list, then execute it deterministically against
the same primitives the drag UI uses — so everything auto-syncs to Google.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any

from . import calendar_source, calendar_sync, config, db

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM = """You are the calendar assistant inside an endurance athlete's training app. The athlete \
types what they want done to their calendar; you return a SMALL JSON object of concrete actions.

Output ONLY a JSON object, no prose around it:
{"reply": "<one short sentence confirming what you did>", "actions": [ ... ]}

Action types (use the exact date strings from CONTEXT; resolve "tomorrow/Friday/tonight" from `today`):
- {"type":"move","session_kind":"primary|strength|secondary","session_id":<int for secondary>,"date":"YYYY-MM-DD","new_date":"YYYY-MM-DD"(optional),"new_start":"HH:MM"(optional,24h)}
    Reschedule the matching PRIMARY, STRENGTH, or named additional workout currently on "date".
    Omit fields you aren't changing. Moving onto a day with the same session_kind swaps those two;
    a strength move never moves that date's primary run/ride.
- {"type":"add_event","title":"...","date":"YYYY-MM-DD","start":"HH:MM","duration_min":<int>,"all_day":false}
    Add a timed personal event only when start and duration were explicitly provided. For an explicitly
    all-day event use all_day:true and omit start/duration. If either timed value is unknown, return no
    action and ask one focused question; never estimate it.
- {"type":"delete_event","match":"<words from the event title>","date":"YYYY-MM-DD"(optional)}
    Remove a personal event the athlete added.
- {"type":"rest","date":"YYYY-MM-DD"}  Turn a day into a rest day (removes its workout).

Rules: workout duration and workout contents are set only by Coach Steve. If asked to make a workout
longer/shorter or change its prescribed work, return no actions and say to ask Coach Steve. Only include
actions the athlete clearly asked for; [] if it's just a question (answer in "reply"). \
Prefer morning workout times before their 9-4 workday unless told otherwise. Keep "reply" to one sentence."""


def _context() -> str:
    today = config.local_today()
    end = (today + datetime.timedelta(days=16)).isoformat()
    days = [d for d in db.get_plan(today.isoformat(), end) if d["date"] >= today.isoformat()]
    lines = [f"today = {today.isoformat()} ({today.strftime('%A')})", "", "WORKOUTS (upcoming):"]
    for d in days:
        wd = datetime.date.fromisoformat(d["date"]).strftime("%a")
        if d.get("is_rest"):
            lines.append(f"  {d['date']} {wd}: REST")
        else:
            lines.append(f"  {d['date']} {wd}: {d.get('discipline')} — {d.get('title')} "
                         f"@ {d.get('start_time') or '?'} for {d.get('duration_min')}min")
    strength = db.get_plan_strength_range(today.isoformat(), end)
    if strength:
        lines.append("")
        lines.append("STRENGTH SESSIONS (separate from primary workouts):")
        for d in strength:
            wd = datetime.date.fromisoformat(d["date"]).strftime("%a")
            lines.append(f"  {d['date']} {wd}: strength — {d.get('title')} "
                         f"@ {d.get('start_time') or '?'} for {d.get('duration_min')}min")
    secondary = db.get_plan_secondary_range(today.isoformat(), end)
    if secondary:
        lines.append("")
        lines.append("ADDITIONAL WORKOUTS (each is independently movable):")
        for session in secondary:
            wd = datetime.date.fromisoformat(session["date"]).strftime("%a")
            lines.append(
                f"  id={session['id']} {session['date']} {wd}: "
                f"{session.get('discipline')} — {session.get('title')} "
                f"@ {session.get('start_time') or '?'} for {session.get('duration_min')}min"
            )
    ev = calendar_source.get_events(days=16)
    if ev.get("available") and ev.get("events"):
        lines.append("")
        lines.append("PERSONAL EVENTS (upcoming):")
        for e in ev["events"]:
            when = "all-day" if e.get("all_day") else (e.get("start") or "")[11:16]
            lines.append(f"  {e.get('date')}: {e.get('summary')} ({when})")
    return "\n".join(lines)


def command(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"error": "empty command"}
    key = config.ANTHROPIC_API_KEY
    if not key or key.strip().endswith("...") or key.strip() in ("", "sk-ant-..."):
        return {"error": "ANTHROPIC_API_KEY is not set"}

    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=2)
    try:
        msg = client.messages.create(
            model=config.FAST_MODEL, max_tokens=900, system=_SYSTEM,
            messages=[{"role": "user", "content": f"<context>\n{_context()}\n</context>\n\n{text}"}],
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    m = _JSON_RE.search(raw)
    if not m:
        return {"reply": raw or "I couldn't parse that — try rephrasing.", "actions": [], "applied": 0}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"reply": "I couldn't parse that — try rephrasing.", "actions": [], "applied": 0}

    actions = data.get("actions") or []
    if not isinstance(actions, list) or any(not isinstance(a, dict) for a in actions):
        return {"reply": "I couldn't safely parse that calendar change.",
                "actions": [], "applied": 0}
    allowed = {"move", "add_event", "delete_event", "rest"}
    if any(a.get("type") not in allowed for a in actions):
        return {"reply": "I couldn't safely parse that calendar change.",
                "actions": [], "applied": 0}
    for action in actions:
        if action.get("type") in {"move", "rest"}:
            try:
                datetime.date.fromisoformat(str(action.get("date") or ""))
            except ValueError:
                return {"reply": "That workout date was invalid, so nothing changed.",
                        "actions": [], "applied": 0}
        if action.get("type") == "delete_event" and not str(action.get("match") or "").strip():
            return {"reply": "Tell me which personal event to delete by name.",
                    "actions": [], "applied": 0}
    if any(a.get("type") == "move" and a.get("new_duration") is not None for a in actions if isinstance(a, dict)):
        return {"reply": "Workout length stays fixed in Calendar — ask Coach Steve to change the session.",
                "actions": [], "applied": 0}
    if any(a.get("type") == "move"
           and str(a.get("session_kind") or "primary").lower()
           not in {"primary", "strength", "secondary"}
           for a in actions if isinstance(a, dict)):
        return {"reply": "I couldn't identify which workout type to move.",
                "actions": [], "applied": 0}
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "add_event":
            continue
        if action.get("all_day"):
            continue
        try:
            duration = int(action.get("duration_min"))
        except (TypeError, ValueError):
            duration = 0
        if not action.get("start") or duration <= 0:
            return {
                "reply": "That event's start time or duration is unknown. Tell me both before I add it.",
                "actions": [], "applied": 0,
            }
    done: list[str] = []
    applied = 0
    touched: list[str] = []   # workout dates that need pushing to Google
    for a in actions:
        try:
            done.append(_apply(a, touched))
            applied += 1
        except Exception as e:  # noqa: BLE001
            done.append(f"⚠ couldn't apply {a.get('type')}: {e}")

    # Push ONLY the workout days we changed (add/delete events already hit Google
    # directly) — no slow full reconcile.
    google_sync = calendar_sync.push_days(touched) if touched else None

    return {"reply": data.get("reply") or "Done.", "actions": actions,
            "applied": applied, "details": done, "google_sync": google_sync}


def _apply(a: dict[str, Any], touched: list[str]) -> str:
    t = a.get("type")
    if t == "move":
        kind = str(a.get("session_kind") or "primary").lower()
        if kind == "secondary":
            if a.get("session_id") is None:
                raise RuntimeError("secondary workout needs session_id")
            r = calendar_sync.move_secondary_session(
                a["session_id"], new_date=a.get("new_date"),
                new_start=a.get("new_start"), do_reconcile=False,
            )
        else:
            mover = (calendar_sync.move_strength_session
                     if kind == "strength" else calendar_sync.move_session)
            r = mover(a["date"], new_date=a.get("new_date"),
                      new_start=a.get("new_start"), do_reconcile=False)
        if r.get("error"):
            raise RuntimeError(r["error"])
        touched.extend(r.get("changed") or [a["date"]])
        return f"moved {kind} session {a['date']}"
    if t == "rest":
        from .calendar_sync import _REST, _utcnow
        rest = dict(_REST); rest["pos_updated_at"] = _utcnow()
        db.edit_plan_day(a["date"], rest, source="edited", reason="Set to rest via calendar assistant")
        touched.append(a["date"])
        return f"rest {a['date']}"
    if t == "add_event":
        r = calendar_source.create_personal_event(
            title=a.get("title") or "Event", date=a["date"], start=a.get("start"),
            duration_min=a.get("duration_min"), all_day=bool(a.get("all_day")))
        if r.get("error"):
            raise RuntimeError(r["error"])
        return f"added '{a.get('title')}'"
    if t == "delete_event":
        return _delete_event(a.get("match") or "", a.get("date"))
    raise RuntimeError(f"unknown action {t}")


def _delete_event(match: str, date: str | None) -> str:
    """Delete a personal (non-workout) event whose title contains `match`."""
    match = match.lower().strip()
    service = calendar_source._get_service()
    start = datetime.datetime.combine(config.local_today(), datetime.time()).astimezone()
    resp = service.events().list(calendarId=calendar_source.TARGET_CALENDAR,
                                 timeMin=start.isoformat(),
                                 timeMax=(start + datetime.timedelta(days=30)).isoformat(),
                                 singleEvents=True, maxResults=250).execute()
    for e in resp.get("items", []):
        if e.get("status") == "cancelled":
            continue
        props = (e.get("extendedProperties", {}) or {}).get("private", {}) or {}
        if props.get("triCoach"):
            continue  # never delete a workout event this way
        s = e.get("start", {})
        edate = (s.get("dateTime") or s.get("date") or "")[:10]
        if date and edate != date:
            continue
        if match and match not in (e.get("summary") or "").lower():
            continue
        service.events().delete(calendarId=calendar_source.TARGET_CALENDAR, eventId=e["id"]).execute()
        return f"deleted '{e.get('summary')}'"
    return f"no personal event matching '{match}'"
