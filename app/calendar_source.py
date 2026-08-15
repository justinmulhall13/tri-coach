"""Google Calendar layer (direct Calendar API, read + write).

Auth: OAuth Desktop client. Put the downloaded client JSON at
GOOGLE_CREDENTIALS_FILE; first run opens a browser once and caches a token
at GOOGLE_TOKEN_FILE. If credentials are missing we degrade gracefully and
report it — the rest of the dashboard still works.

Workout events we create are tagged with extendedProperties.private.triCoach so
we can find/update/delete only our own events and never touch personal ones.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from . import config

# read + write on the user's calendars (needed to create/move workout events).
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Marker keys on events we own.
TRICOACH_KEY = "triCoach"
TRICOACH_DATE_KEY = "triCoachDate"
TARGET_CALENDAR = "primary"

_DISC_EMOJI = {"swim": "🏊", "bike": "🚴", "run": "👟", "brick": "🚴👟",
               "strength": "💪", "recovery": "🧘", "rest": "😴", "race": "🏁"}


def _creds_status() -> dict[str, Any]:
    cred = Path(config.GOOGLE_CREDENTIALS_FILE)
    tok = Path(config.GOOGLE_TOKEN_FILE)
    return {
        "credentials_present": cred.exists(),
        "token_present": tok.exists(),
        "credentials_path": str(cred),
    }


def _get_service():
    """Build an authenticated Calendar service, or raise with a clear message."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    cred_file = Path(config.GOOGLE_CREDENTIALS_FILE)
    tok_file = Path(config.GOOGLE_TOKEN_FILE)
    if not cred_file.exists():
        raise FileNotFoundError(
            f"Google credentials not found at {cred_file}. See README → "
            "'Configure Google Calendar' to download credentials.json."
        )

    creds = None
    if tok_file.exists():
        creds = Credentials.from_authorized_user_file(str(tok_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
            creds = flow.run_local_server(port=0)
        tok_file.parent.mkdir(parents=True, exist_ok=True)
        tok_file.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_events(start: datetime.datetime | None = None, days: int = 7) -> dict[str, Any]:
    """Upcoming events across configured calendars. Never raises to the caller."""
    status = _creds_status()
    if not status["credentials_present"]:
        return {
            "available": False,
            "reason": "missing_credentials",
            "detail": f"No credentials.json at {status['credentials_path']} (see README).",
            "events": [],
        }
    try:
        service = _get_service()
    except Exception as e:
        return {"available": False, "reason": "auth_error", "detail": f"{type(e).__name__}: {e}", "events": []}

    start = start or datetime.datetime.now().astimezone()
    end = start + datetime.timedelta(days=days)
    events: list[dict[str, Any]] = []
    for cal_id in config.GOOGLE_CALENDAR_IDS:
        try:
            resp = service.events().list(
                calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=50,
            ).execute()
        except Exception as e:
            events.append({"_error": f"calendar {cal_id}: {e}"})
            continue
        for ev in resp.get("items", []):
            # Skip workout events we created — the plan already supplies those to
            # the app, so surfacing them here would double-count.
            props = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
            if props.get(TRICOACH_KEY):
                continue
            s = ev.get("start", {})
            e_ = ev.get("end", {})
            events.append({
                "calendar": cal_id,
                "summary": ev.get("summary", "(no title)"),
                "start": s.get("dateTime") or s.get("date"),
                "end": e_.get("dateTime") or e_.get("date"),
                "all_day": "date" in s,
                "location": ev.get("location"),
                "description": ev.get("description"),
            })
    events.sort(key=lambda x: x.get("start") or "")
    return {"available": True, "count": len(events), "window_days": days, "events": events}


def todays_availability(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rough free-time read for today, to feed the workout suggester."""
    today = datetime.date.today().isoformat()
    todays = [e for e in events if (e.get("start") or "").startswith(today)]
    busy_blocks = [{"summary": e["summary"], "start": e["start"], "end": e["end"]}
                   for e in todays if not e.get("all_day")]
    return {"date": today, "event_count": len(todays), "busy_blocks": busy_blocks}


# --- Write layer (workout events) --------------------------------------------
def _event_body(day: dict[str, Any]) -> dict[str, Any]:
    """Build a Google event body from a plan day."""
    disc = day.get("discipline") or "other"
    emoji = _DISC_EMOJI.get(disc, "•")
    title = day.get("title") or disc
    summary = f"{emoji} {title}".strip()

    st = day.get("structure") or {}
    lines = []
    for label, key in (("Warm-up", "warmup"), ("Main", "main"), ("Cool-down", "cooldown"),
                       ("Swim", "swim"), ("Strength", "strength"), ("2nd leg (run)", "run")):
        v = st.get(key)
        if v and v != "—":
            lines.append(f"{label}: {v}")
    if day.get("why"):
        lines.append(f"\nWhy: {day['why']}")
    description = "\n".join(lines)

    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "extendedProperties": {"private": {TRICOACH_KEY: "1", TRICOACH_DATE_KEY: day["date"]}},
        "transparency": "transparent",  # doesn't block your free/busy
        "reminders": {"useDefault": False},
    }

    date = day["date"]
    start_time = day.get("start_time")
    if day.get("is_rest") or not start_time:
        # All-day marker (rest days, or any session without a time yet).
        nxt = (datetime.date.fromisoformat(date) + datetime.timedelta(days=1)).isoformat()
        body["start"] = {"date": date}
        body["end"] = {"date": nxt}
    else:
        dur = int(day.get("duration_min") or 60)
        try:
            h, m = (int(x) for x in start_time.split(":"))
        except Exception:
            h, m = 6, 30
        start_dt = datetime.datetime.fromisoformat(date) + datetime.timedelta(hours=h, minutes=m)
        end_dt = start_dt + datetime.timedelta(minutes=dur)
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE}
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE}
    return body


def upsert_workout_event(day: dict[str, Any]) -> dict[str, Any]:
    """Create or patch the Google event for a plan day. Returns {event_id} or {error}."""
    try:
        service = _get_service()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    body = _event_body(day)
    eid = day.get("gcal_event_id")
    try:
        if eid:
            try:
                ev = service.events().patch(calendarId=TARGET_CALENDAR, eventId=eid, body=body).execute()
                # Patching a deleted event returns it with status 'cancelled' and does
                # NOT resurrect it — treat that as a miss and insert a fresh one.
                if ev.get("status") != "cancelled":
                    return {"event_id": ev.get("id")}
            except Exception:
                pass  # stale id (404/410) — fall through to insert
        ev = service.events().insert(calendarId=TARGET_CALENDAR, body=body).execute()
        return {"event_id": ev.get("id")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def create_personal_event(title: str, date: str, start: str | None = None,
                          duration_min: int | None = None,
                          all_day: bool = False) -> dict[str, Any]:
    """Create a normal personal event (e.g. from a coach chat commitment). Tagged
    triCoachPersonal so it's recognisable but never treated as a workout — it
    still shows in the read feed alongside the athlete's own events."""
    try:
        service = _get_service()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    body: dict[str, Any] = {
        "summary": title,
        "extendedProperties": {"private": {"triCoachPersonal": "1"}},
    }
    if all_day:
        nxt = (datetime.date.fromisoformat(date) + datetime.timedelta(days=1)).isoformat()
        body["start"] = {"date": date}
        body["end"] = {"date": nxt}
    else:
        if not start or duration_min is None:
            return {"error": "timed events require an explicit start and duration_min"}
        try:
            dur = int(duration_min)
            if dur <= 0:
                raise ValueError("duration must be positive")
            h, m = (int(x) for x in start.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("invalid time")
        except Exception:
            return {"error": "invalid explicit start or duration_min"}
        start_dt = datetime.datetime.fromisoformat(date) + datetime.timedelta(hours=h, minutes=m)
        end_dt = start_dt + datetime.timedelta(minutes=dur)
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE}
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE}
    try:
        ev = service.events().insert(calendarId=TARGET_CALENDAR, body=body).execute()
        return {"event_id": ev.get("id"), "title": title}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def delete_workout_event(event_id: str) -> dict[str, Any]:
    if not event_id:
        return {"ok": True}
    try:
        service = _get_service()
        service.events().delete(calendarId=TARGET_CALENDAR, eventId=event_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def list_workout_events(days: int = 60) -> dict[str, Any]:
    """All triCoach-owned events in the window, keyed by their triCoachDate.
    Used by reconcile to prune events whose day no longer syncs."""
    try:
        service = _get_service()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "by_date": {}, "all": []}
    # Scan from the start of today so today's (already-started) events are included.
    start = datetime.datetime.combine(datetime.date.today(), datetime.time()).astimezone()
    end = start + datetime.timedelta(days=days)
    by_date: dict[str, str] = {}
    all_ids: list[str] = []
    try:
        # A plain window list + inline property filter is used instead of the
        # privateExtendedProperty search, which lags for freshly-created events.
        resp = service.events().list(
            calendarId=TARGET_CALENDAR, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, maxResults=250,
        ).execute()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "by_date": {}, "all": []}
    for ev in resp.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        props = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
        if not props.get(TRICOACH_KEY):
            continue
        eid = ev.get("id")
        all_ids.append(eid)
        d = props.get(TRICOACH_DATE_KEY)
        if d:
            by_date[d] = eid
    return {"by_date": by_date, "all": all_ids}


def fetch_workout_events(days: int = 40) -> dict[str, Any]:
    """Detailed pull of the workout events we own — id, the plan date they were
    created for (triCoachDate), their CURRENT start (date/time), and Google's
    `updated` timestamp — paginated. Feeds the reverse-sync."""
    try:
        service = _get_service()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "events": []}
    start = datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=3),
                                      datetime.time()).astimezone()
    end = start + datetime.timedelta(days=days)
    events: list[dict[str, Any]] = []
    token = None
    try:
        while True:
            resp = service.events().list(
                calendarId=TARGET_CALENDAR, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, maxResults=250, pageToken=token,
            ).execute()
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                props = (ev.get("extendedProperties", {}) or {}).get("private", {}) or {}
                if not props.get(TRICOACH_KEY):
                    continue
                s = ev.get("start", {})
                events.append({
                    "id": ev.get("id"),
                    "plan_date": props.get(TRICOACH_DATE_KEY),
                    "start": s.get("dateTime") or s.get("date"),
                    "all_day": "date" in s,
                    "updated": ev.get("updated"),
                })
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "events": []}
    return {"events": events}
