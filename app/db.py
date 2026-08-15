"""SQLite persistence: training plan, completions, logged constraints, chat.

Plain stdlib sqlite3 (no ORM needed). The plan is the backbone; readiness
only modifies the daily suggestion, so the stored plan must persist verbatim
across restarts and survive reseeds of untouched days.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import coaching_contract, config

_DB_PATH = (config.BASE_DIR / config.DB_PATH).resolve() if not Path(config.DB_PATH).is_absolute() else Path(config.DB_PATH)
_local = threading.local()


_PLAN_DAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_days (
    event_profile_id TEXT NOT NULL,       -- isolates plans when EVENT_PROFILE changes
    date        TEXT NOT NULL,             -- ISO YYYY-MM-DD
    week_index  INTEGER NOT NULL,         -- 0 = current week, counting forward
    phase       TEXT NOT NULL,            -- build | peak | taper | post-race
    discipline  TEXT NOT NULL,            -- swim|bike|run|brick|strength|rest|recovery
    title       TEXT NOT NULL,
    structure   TEXT NOT NULL,            -- JSON: {warmup, main, cooldown}
    duration_min INTEGER,
    intensity   TEXT,                     -- e.g. "threshold", "Z2", "race pace"
    tsb_target  REAL,                     -- explicit projected race-day form target
    why         TEXT,
    is_rest     INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'seed',  -- seed | edited | coach
    updated_at  TEXT NOT NULL,
    start_time  TEXT,
    gcal_event_id TEXT,
    pos_updated_at TEXT,
    PRIMARY KEY (event_profile_id, date)
);
"""


SCHEMA = _PLAN_DAYS_SCHEMA + """
CREATE TABLE IF NOT EXISTS completions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    status      TEXT NOT NULL,            -- done | partial | skipped
    notes       TEXT,
    garmin_activity_id TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS constraints_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
    date        TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
    role        TEXT NOT NULL,            -- user | assistant
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS wellness_daily (
    date            TEXT PRIMARY KEY,     -- ISO YYYY-MM-DD
    hrv_ms          REAL,
    rhr_bpm         REAL,
    sleep_h         REAL,
    sleep_score     REAL,
    readiness_score REAL,
    body_battery    REAL,
    stress          REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT UNIQUE NOT NULL,     -- dedup key
    sub         TEXT NOT NULL,            -- full subscription JSON
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
    date        TEXT NOT NULL,            -- plan day that changed
    prev        TEXT NOT NULL,            -- JSON snapshot of the day BEFORE the change
    source      TEXT,                     -- what made the change (coach|edited|adapt|...)
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,            -- ISO YYYY-MM-DD (local)
    sport       TEXT NOT NULL,            -- swim|bike|run|strength|brick|other
    name        TEXT,
    km          REAL,
    minutes     REAL,
    hr_avg      INTEGER,
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nutrition_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,            -- ISO YYYY-MM-DD (local)
    eaten_at    TEXT,                     -- free-text/parsed time e.g. "07:30" or "post-ride"
    meal        TEXT,                     -- breakfast|lunch|dinner|snack|pre|during|post
    description TEXT NOT NULL,            -- "chicken breast + 1 cup rice"
    kcal        INTEGER,
    protein_g   INTEGER,
    carb_g      INTEGER,
    fat_g       INTEGER,
    created_at  TEXT NOT NULL
);
"""

# Columns added after the initial release; applied idempotently by _migrate().
_MIGRATIONS = {
    "completions": [("rpe", "INTEGER"), ("feedback", "TEXT")],
    # Calendar sync: local start time (HH:MM), the Google event id we own, and
    # when the app last changed this day's *position* (time/day) — used for
    # most-recent-wins against Google's event.updated. Stored as UTC ISO.
    "plan_days": [("start_time", "TEXT"), ("gcal_event_id", "TEXT"),
                  ("pos_updated_at", "TEXT"), ("tsb_target", "REAL"),
                  ("event_profile_id", "TEXT")],
    "plan_history": [("event_profile_id", "TEXT")],
    "constraints_log": [("event_profile_id", "TEXT")],
    "chat_history": [("event_profile_id", "TEXT")],
}


def _active_profile_id() -> str:
    return coaching_contract.event_profile_id()


_PLAN_DAY_COLUMNS = (
    "event_profile_id", "date", "week_index", "phase", "discipline", "title",
    "structure", "duration_min", "intensity", "tsb_target", "why", "is_rest",
    "source", "updated_at", "start_time", "gcal_event_id", "pos_updated_at",
)


def _plan_days_has_profile_date_key(c: sqlite3.Connection) -> bool:
    info = c.execute("PRAGMA table_info(plan_days)").fetchall()
    primary_key = [row["name"] for row in sorted(
        (row for row in info if row["pk"]), key=lambda row: row["pk"]
    )]
    return primary_key == ["event_profile_id", "date"]


def _migrate_plan_days_profile_key(c: sqlite3.Connection) -> None:
    """Replace the legacy date-only primary key without losing plan rows."""
    if _plan_days_has_profile_date_key(c):
        return

    legacy = "plan_days_legacy_date_key"
    before = c.execute("SELECT COUNT(*) AS n FROM plan_days").fetchone()["n"]
    c.execute("SAVEPOINT migrate_plan_days_profile_key")
    try:
        c.execute(f"ALTER TABLE plan_days RENAME TO {legacy}")
        c.execute(_PLAN_DAYS_SCHEMA)
        columns = ", ".join(_PLAN_DAY_COLUMNS)
        c.execute(f"INSERT INTO plan_days ({columns}) SELECT {columns} FROM {legacy}")
        after = c.execute("SELECT COUNT(*) AS n FROM plan_days").fetchone()["n"]
        if after != before:
            raise RuntimeError("plan_days profile-key migration did not preserve every row")
        c.execute(f"DROP TABLE {legacy}")
        c.execute("RELEASE SAVEPOINT migrate_plan_days_profile_key")
    except Exception:
        c.execute("ROLLBACK TO SAVEPOINT migrate_plan_days_profile_key")
        c.execute("RELEASE SAVEPOINT migrate_plan_days_profile_key")
        raise


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    c.execute("UPDATE plan_days SET tsb_target=? WHERE tsb_target IS NULL",
              (coaching_contract.DEFAULT_RACE_DAY_TSB_TARGET,))
    profile_id = _active_profile_id()
    # Existing production data predates profile scoping and belongs to the
    # profile installed when this migration first runs. Once populated, these
    # values are never rewritten by a later event switch.
    for table in ("plan_days", "plan_history", "constraints_log", "chat_history"):
        c.execute(
            f"UPDATE {table} SET event_profile_id=? "
            "WHERE event_profile_id IS NULL OR event_profile_id=''",
            (profile_id,),
        )
    _migrate_plan_days_profile_key(c)
    c.commit()


def get_meta(key: str) -> str | None:
    r = _conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_meta(key: str, value: str) -> None:
    c = _conn()
    c.execute("INSERT INTO meta (key,value) VALUES (?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    c.commit()


def _conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(_DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _migrate(c)
        _local.conn = c
    return _local.conn


def _row_to_day(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["structure"] = json.loads(d["structure"]) if d.get("structure") else {}
    d["is_rest"] = bool(d["is_rest"])
    return d


# --- Plan ---------------------------------------------------------------------
def upsert_plan_day(day: dict[str, Any], *, only_if_absent_or_seed: bool = False) -> None:
    """Insert/replace a plan day.

    `only_if_absent_or_seed=True` (used by reseed) skips days the user has
    edited or the coach has adjusted, so manual changes are never clobbered.
    """
    import datetime
    c = _conn()
    profile_id = _active_profile_id()
    if only_if_absent_or_seed:
        existing = c.execute(
            "SELECT source FROM plan_days WHERE event_profile_id=? AND date=?",
            (profile_id, day["date"]),
        ).fetchone()
        if existing and existing["source"] != "seed":
            return
    c.execute(
        """INSERT INTO plan_days (date, event_profile_id, week_index, phase, discipline, title, structure,
                duration_min, intensity, tsb_target, why, is_rest, source, updated_at,
                start_time, gcal_event_id, pos_updated_at)
           VALUES (:date,:event_profile_id,:week_index,:phase,:discipline,:title,:structure,
                :duration_min,:intensity,:tsb_target,:why,:is_rest,:source,:updated_at,
                :start_time,:gcal_event_id,:pos_updated_at)
           ON CONFLICT(event_profile_id,date) DO UPDATE SET
                week_index=excluded.week_index, phase=excluded.phase,
                discipline=excluded.discipline, title=excluded.title,
                structure=excluded.structure, duration_min=excluded.duration_min,
                intensity=excluded.intensity, tsb_target=excluded.tsb_target, why=excluded.why,
                is_rest=excluded.is_rest, source=excluded.source,
                updated_at=excluded.updated_at,
                start_time=excluded.start_time, gcal_event_id=excluded.gcal_event_id,
                pos_updated_at=excluded.pos_updated_at""",
        {
            **day,
            "event_profile_id": profile_id,
            "structure": json.dumps(day.get("structure", {})),
            "is_rest": int(day.get("is_rest", 0)),
            "source": day.get("source", "seed"),
            "tsb_target": day.get("tsb_target"),
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "start_time": day.get("start_time"),
            "gcal_event_id": day.get("gcal_event_id"),
            "pos_updated_at": day.get("pos_updated_at"),
        },
    )
    c.commit()


def get_plan(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    c = _conn()
    q = "SELECT * FROM plan_days WHERE event_profile_id=?"
    args: list[Any] = [_active_profile_id()]
    if start and end:
        q += " AND date BETWEEN ? AND ?"; args.extend([start, end])
    q += " ORDER BY date"
    return [_row_to_day(r) for r in c.execute(q, args).fetchall()]


def get_plan_day(date: str) -> dict[str, Any] | None:
    r = _conn().execute(
        "SELECT * FROM plan_days WHERE date=? AND event_profile_id=?",
        (date, _active_profile_id()),
    ).fetchone()
    return _row_to_day(r) if r else None


def edit_plan_day(date: str, fields: dict[str, Any], source: str = "edited",
                  reason: str = "", record_history: bool = True) -> dict[str, Any] | None:
    """Patch a single day; marks it `edited` so reseed won't overwrite it. Snapshots
    the PRE-edit state to plan_history so any change can be reviewed and undone."""
    import datetime
    day = get_plan_day(date)
    if not day:
        return None
    if record_history:
        _record_plan_history(date, day, source, reason)
    day.update({k: v for k, v in fields.items() if k in
                {"discipline", "title", "structure", "duration_min", "intensity", "tsb_target", "why", "is_rest", "phase",
                 "start_time", "gcal_event_id", "pos_updated_at"}})
    day["source"] = source
    day["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    upsert_plan_day(day)
    return get_plan_day(date)


def _record_plan_history(date: str, prev_day: dict[str, Any], source: str, reason: str) -> None:
    import datetime
    c = _conn()
    c.execute("INSERT INTO plan_history (event_profile_id,date,prev,source,reason,created_at) VALUES (?,?,?,?,?,?)",
              (_active_profile_id(), date, json.dumps(prev_day, default=str), source, reason,
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_plan_history(limit: int = 40) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM plan_history WHERE event_profile_id=? ORDER BY id DESC LIMIT ?",
        (_active_profile_id(), limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["prev"] = json.loads(d["prev"])
        except (json.JSONDecodeError, TypeError):
            d["prev"] = {}
        out.append(d)
    return out


def revert_plan_history(history_id: int) -> dict[str, Any] | None:
    """Restore a plan day to its snapshot at that history entry."""
    r = _conn().execute(
        "SELECT * FROM plan_history WHERE id=? AND event_profile_id=?",
        (history_id, _active_profile_id()),
    ).fetchone()
    if not r:
        return None
    try:
        prev = json.loads(r["prev"])
    except (json.JSONDecodeError, TypeError):
        return None
    fields = {k: prev.get(k) for k in
              ("discipline", "title", "structure", "duration_min", "intensity", "tsb_target", "why", "is_rest", "phase")}
    # record the revert itself so it too can be undone, restore original source tag
    return edit_plan_day(r["date"], fields, source=prev.get("source", "edited"),
                         reason="reverted to earlier version")


def plan_summary() -> dict[str, Any]:
    c = _conn()
    profile_id = _active_profile_id()
    rows = c.execute(
        "SELECT phase, COUNT(*) n FROM plan_days WHERE event_profile_id=? GROUP BY phase",
        (profile_id,),
    ).fetchall()
    span = c.execute(
        "SELECT MIN(date) a, MAX(date) b, COUNT(*) n FROM plan_days WHERE event_profile_id=?",
        (profile_id,),
    ).fetchone()
    return {
        "days": span["n"], "start": span["a"], "end": span["b"],
        "by_phase": {r["phase"]: r["n"] for r in rows},
    }


# --- Wellness history (personal baselines) -----------------------------------
def upsert_wellness(date: str, vals: dict[str, Any]) -> None:
    """Store one day's recovery snapshot (idempotent). Only non-null fields are
    written, so a later fuller pull doesn't wipe an earlier partial one."""
    import datetime
    cols = ("hrv_ms", "rhr_bpm", "sleep_h", "sleep_score", "readiness_score", "body_battery", "stress")
    present = {k: vals[k] for k in cols if vals.get(k) is not None}
    if not present:
        return
    c = _conn()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c.execute(f"INSERT INTO wellness_daily (date, updated_at, {','.join(present)}) "
              f"VALUES (?,?,{','.join('?' for _ in present)}) "
              f"ON CONFLICT(date) DO UPDATE SET updated_at=excluded.updated_at, "
              + ", ".join(f"{k}=COALESCE(excluded.{k}, {k})" for k in present),
              (date, now, *present.values()))
    c.commit()


def get_wellness(days: int = 60) -> list[dict[str, Any]]:
    import datetime
    cutoff = (config.local_today() - datetime.timedelta(days=days)).isoformat()
    rows = _conn().execute("SELECT * FROM wellness_daily WHERE date >= ? ORDER BY date", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# --- Completions / constraints / chat ----------------------------------------
def add_completion(date: str, status: str, notes: str = "", garmin_activity_id: str = "",
                   rpe: int | None = None, feedback: str = "") -> None:
    import datetime
    c = _conn()
    c.execute("INSERT INTO completions (date,status,notes,garmin_activity_id,rpe,feedback,created_at) "
              "VALUES (?,?,?,?,?,?,?)",
              (date, status, notes, garmin_activity_id, rpe, feedback,
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_completions(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    c = _conn()
    if start and end:
        rows = c.execute("SELECT * FROM completions WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)).fetchall()
    else:
        rows = c.execute("SELECT * FROM completions ORDER BY date").fetchall()
    return [dict(r) for r in rows]


def add_constraint(date: str, text: str) -> None:
    import datetime
    c = _conn()
    c.execute("INSERT INTO constraints_log (event_profile_id,date,text,created_at) VALUES (?,?,?,?)",
              (_active_profile_id(), date, text,
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_constraints(date: str) -> list[dict[str, Any]]:
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM constraints_log WHERE date=? AND event_profile_id=? ORDER BY created_at",
        (date, _active_profile_id())).fetchall()]


def get_constraint_history(limit: int = 200) -> list[dict[str, Any]]:
    """Dated durable coaching memories, oldest-to-newest within the limit.

    The chat UI can start visually fresh without making Steve forget prior
    availability, injuries, equipment constraints, or preferences.
    """
    rows = _conn().execute(
        "SELECT * FROM constraints_log WHERE event_profile_id=? ORDER BY id DESC LIMIT ?",
        (_active_profile_id(), limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- Web push subscriptions ---------------------------------------------------
def add_push_subscription(endpoint: str, sub_json: str) -> None:
    import datetime
    c = _conn()
    c.execute("INSERT INTO push_subscriptions (endpoint, sub, created_at) VALUES (?,?,?) "
              "ON CONFLICT(endpoint) DO UPDATE SET sub=excluded.sub",
              (endpoint, sub_json, datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_push_subscriptions() -> list[dict[str, Any]]:
    return [dict(r) for r in _conn().execute("SELECT * FROM push_subscriptions").fetchall()]


def delete_push_subscription(entry_id: int) -> None:
    c = _conn()
    c.execute("DELETE FROM push_subscriptions WHERE id=?", (entry_id,))
    c.commit()


# --- Manual activities (coach-logged, before/without a Garmin sync) ----------
def add_manual_activity(date: str, sport: str, *, name: str = "", km: float | None = None,
                        minutes: float | None = None, hr_avg: int | None = None,
                        notes: str = "") -> int:
    import datetime
    c = _conn()
    cur = c.execute(
        "INSERT INTO manual_activity (date,sport,name,km,minutes,hr_avg,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (date, sport, name, km, minutes, hr_avg, notes,
         datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()
    return cur.lastrowid


def get_manual_activities(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    c = _conn()
    if start and end:
        rows = c.execute("SELECT * FROM manual_activity WHERE date BETWEEN ? AND ? ORDER BY date DESC", (start, end)).fetchall()
    else:
        rows = c.execute("SELECT * FROM manual_activity ORDER BY date DESC").fetchall()
    return [dict(r) for r in rows]


def delete_manual_activity(entry_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM manual_activity WHERE id=?", (entry_id,))
    c.commit()
    return cur.rowcount > 0


# --- Nutrition log (Chef Gordo) ----------------------------------------------
def add_nutrition(date: str, description: str, *, eaten_at: str = "", meal: str = "",
                  kcal: float | int | None = None, protein_g: float | int | None = None,
                  carb_g: float | int | None = None, fat_g: float | int | None = None) -> int:
    import datetime
    c = _conn()
    cur = c.execute(
        "INSERT INTO nutrition_log (date,eaten_at,meal,description,kcal,protein_g,carb_g,fat_g,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (date, eaten_at, meal, description, kcal, protein_g, carb_g, fat_g,
         datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()
    return cur.lastrowid


def get_nutrition(date: str) -> list[dict[str, Any]]:
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM nutrition_log WHERE date=? ORDER BY id", (date,)).fetchall()]


def get_nutrition_by_description(date: str, description: str) -> dict[str, Any] | None:
    """Find a generated nutrition entry so one-tap actions remain idempotent."""
    row = _conn().execute(
        "SELECT * FROM nutrition_log WHERE date=? AND description=? ORDER BY id DESC LIMIT 1",
        (date, description),
    ).fetchone()
    return dict(row) if row else None


def delete_nutrition(entry_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM nutrition_log WHERE id=?", (entry_id,))
    c.commit()
    return cur.rowcount > 0


def add_chat(role: str, content: str) -> None:
    import datetime
    c = _conn()
    c.execute("INSERT INTO chat_history (event_profile_id,role,content,created_at) VALUES (?,?,?,?)",
              (_active_profile_id(), role, content,
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_chat(limit: int = 50, since: str | None = None) -> list[dict[str, Any]]:
    """Recent chat. `since` is an ISO date/datetime prefix (e.g. '2026-06-30')
    to scope to today — created_at is stored as 'YYYY-MM-DDTHH:MM:SS' so a
    lexicographic >= comparison works."""
    c = _conn()
    if since:
        rows = c.execute(
            "SELECT * FROM chat_history WHERE event_profile_id=? AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?",
            (_active_profile_id(), since, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM chat_history WHERE event_profile_id=? ORDER BY id DESC LIMIT ?",
            (_active_profile_id(), limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_chat(since: str | None = None) -> int:
    """Delete chat history. `since` limits the delete to today (or any date
    prefix); None wipes everything. Returns rows deleted."""
    c = _conn()
    profile_id = _active_profile_id()
    if since:
        cur = c.execute(
            "DELETE FROM chat_history WHERE event_profile_id=? AND created_at >= ?",
            (profile_id, since),
        )
    else:
        cur = c.execute("DELETE FROM chat_history WHERE event_profile_id=?", (profile_id,))
    c.commit()
    return cur.rowcount
