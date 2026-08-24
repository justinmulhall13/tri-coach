"""SQLite persistence: training plan, completions, logged constraints, chat.

Plain stdlib sqlite3 (no ORM needed). The plan is the backbone; readiness
only modifies the daily suggestion, so the stored plan must persist verbatim
across restarts and survive reseeds of untouched days.
"""
from __future__ import annotations

import json
import hashlib
import re
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
CREATE TABLE IF NOT EXISTS event_profiles (
    id              TEXT PRIMARY KEY,
    event_name      TEXT NOT NULL,
    event_date      TEXT NOT NULL,
    distances_json  TEXT NOT NULL,
    goal_json       TEXT NOT NULL,
    mode            TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    extras_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_profile_state (
    singleton_id         INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    active_profile_id    TEXT NOT NULL,
    pending_profile_json TEXT,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft', -- draft | active | superseded
    schedule_json    TEXT NOT NULL,
    source_message   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    activated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_plan_drafts_profile_status
    ON plan_drafts (event_profile_id, status, id DESC);

CREATE TABLE IF NOT EXISTS lifting_split (
    -- The athlete's standing four-day split. One row per event profile, so a
    -- marathon block and a triathlon block can carry different splits without
    -- one overwriting the other.
    event_profile_id TEXT NOT NULL,
    days_json        TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'default',  -- default | coach | edited
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (event_profile_id)
);

CREATE TABLE IF NOT EXISTS plan_strength (
    -- A complete lift that happens to share a calendar date with plan_days.
    -- It remains an independent, clickable workout rather than being folded
    -- into the cardio structure or mislabelled as a brick.
    event_profile_id TEXT NOT NULL,
    date             TEXT NOT NULL,             -- ISO YYYY-MM-DD
    slot             TEXT NOT NULL,             -- upper | lower | full
    title            TEXT NOT NULL,
    routine_json     TEXT NOT NULL,             -- Hevy routine when one is available; null otherwise
    duration_min     INTEGER,                   -- this lift only, not the day's total
    intensity        TEXT,
    structure        TEXT NOT NULL DEFAULT '{}',-- independent warmup/main/cooldown
    why              TEXT,
    start_time       TEXT,                      -- optional HH:MM, independent of cardio
    effort_level     TEXT,                      -- heavy | moderate | light | skip
    effort_cue       TEXT,
    hevy_routine_id  TEXT,                      -- set once actually created in Hevy
    gcal_event_id    TEXT,                      -- independent Google event for this lift
    pos_updated_at   TEXT,                      -- most recent app/Google position change
    source           TEXT NOT NULL DEFAULT 'coach',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (event_profile_id, date)
);

CREATE INDEX IF NOT EXISTS idx_plan_strength_profile_date
    ON plan_strength (event_profile_id, date);

CREATE TABLE IF NOT EXISTS plan_secondary (
    -- Any additional same-day workout that is not the primary plan_days row or
    -- the dedicated plan_strength row.  A commute out and commute home are two
    -- records here/plan_days, never one summed duration and never a brick.
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
    date             TEXT NOT NULL,             -- ISO YYYY-MM-DD
    session_order    INTEGER NOT NULL DEFAULT 1,
    discipline       TEXT NOT NULL,
    title            TEXT NOT NULL,
    structure        TEXT NOT NULL DEFAULT '{}',
    duration_min     INTEGER NOT NULL,
    intensity        TEXT,
    why              TEXT,
    start_time       TEXT,
    gcal_event_id    TEXT,
    pos_updated_at   TEXT,
    source           TEXT NOT NULL DEFAULT 'coach',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (event_profile_id, date, session_order)
);

CREATE INDEX IF NOT EXISTS idx_plan_secondary_profile_date
    ON plan_secondary (event_profile_id, date, session_order);

CREATE TABLE IF NOT EXISTS completions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_profile_id TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS schedule_constraints (
    -- Athlete-level life/scheduling facts. These intentionally survive an
    -- event-mode switch: class hours, gym access and travel still matter when
    -- the active goal changes from a triathlon to a marathon.
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL,       -- busy|available|facility_hours|recovery_risk|travel|buffer
    label           TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    days_json       TEXT NOT NULL DEFAULT '[]',
    start_time      TEXT,
    end_time        TEXT,
    duration_min    INTEGER,
    note            TEXT,
    provenance      TEXT NOT NULL DEFAULT 'confirmed_by_user',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedule_constraints_window
    ON schedule_constraints (start_date, end_date, id);

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

CREATE TABLE IF NOT EXISTS saved_recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url  TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    image_url   TEXT NOT NULL,
    source_name TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grocery_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_text      TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'Other',
    checked        INTEGER NOT NULL DEFAULT 0,
    recipe_id      INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_grocery_recipe_item
    ON grocery_items (recipe_id, normalized_key);
"""

# Columns added after the initial release; applied idempotently by _migrate().
_MIGRATIONS = {
    "completions": [("rpe", "INTEGER"), ("feedback", "TEXT"),
                    ("event_profile_id", "TEXT")],
    # Calendar sync: local start time (HH:MM), the Google event id we own, and
    # when the app last changed this day's *position* (time/day) — used for
    # most-recent-wins against Google's event.updated. Stored as UTC ISO.
    "plan_days": [("start_time", "TEXT"), ("gcal_event_id", "TEXT"),
                  ("pos_updated_at", "TEXT"), ("tsb_target", "REAL"),
                  ("event_profile_id", "TEXT")],
    "plan_history": [("event_profile_id", "TEXT")],
    "constraints_log": [("event_profile_id", "TEXT")],
    "chat_history": [("event_profile_id", "TEXT")],
    # Strength used to be a decorative routine attached to a date. These fields
    # make it a complete, independently openable workout record.
    "plan_strength": [("duration_min", "INTEGER"), ("intensity", "TEXT"),
                      ("structure", "TEXT NOT NULL DEFAULT '{}'"), ("why", "TEXT"),
                      ("start_time", "TEXT"), ("gcal_event_id", "TEXT"),
                      ("pos_updated_at", "TEXT")],
}


def _active_profile_id(connection: sqlite3.Connection | None = None) -> str:
    override = coaching_contract.legacy_event_profile_override()
    if override is not None:
        return str(override.get("id") or "unknown-profile")
    c = connection or _conn()
    row = c.execute(
        "SELECT active_profile_id FROM event_profile_state WHERE singleton_id=1"
    ).fetchone()
    if not row:
        _ensure_event_profile_state(c)
        row = c.execute(
            "SELECT active_profile_id FROM event_profile_state WHERE singleton_id=1"
        ).fetchone()
    return str(row["active_profile_id"])


def _event_profile_params(record: dict[str, Any], now: str) -> dict[str, Any]:
    return {
        "id": record["id"],
        "event_name": record["event_name"],
        "event_date": record["event_date"],
        "distances_json": json.dumps(record["distances"], separators=(",", ":"), sort_keys=True),
        "goal_json": json.dumps(record["goal"], separators=(",", ":"), sort_keys=True),
        "mode": record["mode"],
        "provenance_json": json.dumps(record["provenance"], separators=(",", ":"), sort_keys=True),
        "extras_json": json.dumps(record.get("extras") or {}, separators=(",", ":"), sort_keys=True),
        "created_at": now,
        "updated_at": now,
    }


def _upsert_event_profile(c: sqlite3.Connection, record: dict[str, Any], now: str) -> None:
    c.execute(
        """INSERT INTO event_profiles
           (id,event_name,event_date,distances_json,goal_json,mode,provenance_json,
            extras_json,created_at,updated_at)
           VALUES (:id,:event_name,:event_date,:distances_json,:goal_json,:mode,:provenance_json,
                   :extras_json,:created_at,:updated_at)
           ON CONFLICT(id) DO UPDATE SET
             event_name=excluded.event_name,event_date=excluded.event_date,
             distances_json=excluded.distances_json,goal_json=excluded.goal_json,
             mode=excluded.mode,provenance_json=excluded.provenance_json,
             extras_json=excluded.extras_json,updated_at=excluded.updated_at""",
        _event_profile_params(record, now),
    )


def _ensure_event_profile_state(c: sqlite3.Connection) -> None:
    import datetime
    now = datetime.datetime.now().isoformat(timespec="seconds")
    default = coaching_contract.default_event_profile_record()
    # Bootstrap the bundled profile only once. Reopening the database must not
    # overwrite a subsequently confirmed record that deliberately uses the same
    # profile id with updated goal/profile facts.
    params = _event_profile_params(default, now)
    c.execute(
        """INSERT INTO event_profiles
           (id,event_name,event_date,distances_json,goal_json,mode,provenance_json,
            extras_json,created_at,updated_at)
           VALUES (:id,:event_name,:event_date,:distances_json,:goal_json,:mode,:provenance_json,
                   :extras_json,:created_at,:updated_at)
           ON CONFLICT(id) DO NOTHING""",
        params,
    )
    c.execute(
        """INSERT INTO event_profile_state
           (singleton_id,active_profile_id,pending_profile_json,updated_at)
           VALUES (1,?,?,?) ON CONFLICT(singleton_id) DO NOTHING""",
        (default["id"], None, now),
    )


def _decode_event_profile_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_name": row["event_name"],
        "event_date": row["event_date"],
        "distances": json.loads(row["distances_json"]),
        "goal": json.loads(row["goal_json"]),
        "mode": row["mode"],
        "provenance": json.loads(row["provenance_json"]),
        "extras": json.loads(row["extras_json"] or "{}"),
    }


def get_active_event_profile_record() -> dict[str, Any]:
    """Return the durable active profile, with an explicit test override only."""
    override = coaching_contract.legacy_event_profile_override()
    if override is not None:
        return override
    c = _conn()
    row = c.execute(
        """SELECT p.* FROM event_profile_state s
           JOIN event_profiles p ON p.id=s.active_profile_id
           WHERE s.singleton_id=1"""
    ).fetchone()
    if not row:
        raise RuntimeError("active event profile state is missing")
    return _decode_event_profile_row(row)


def get_pending_event_profile() -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT pending_profile_json FROM event_profile_state WHERE singleton_id=1"
    ).fetchone()
    if not row or not row["pending_profile_json"]:
        return None
    try:
        pending = json.loads(row["pending_profile_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    return pending if isinstance(pending, dict) else None


def stage_event_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Durably merge a partial profile without changing the active profile."""
    import datetime
    c = _conn()
    pending = get_pending_event_profile()
    normalized = coaching_contract.normalize_event_profile(profile, pending)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c.execute(
        """UPDATE event_profile_state
           SET pending_profile_json=?,updated_at=? WHERE singleton_id=1""",
        (json.dumps(normalized, separators=(",", ":"), sort_keys=True), now),
    )
    c.commit()
    return normalized


def clear_pending_event_profile() -> None:
    import datetime
    c = _conn()
    c.execute(
        """UPDATE event_profile_state SET pending_profile_json=NULL,updated_at=?
           WHERE singleton_id=1""",
        (datetime.datetime.now().isoformat(timespec="seconds"),),
    )
    c.commit()


def activate_pending_event_profile() -> dict[str, Any]:
    """Atomically save and activate a complete, explicitly confirmed profile."""
    import datetime
    c = _conn()
    pending = get_pending_event_profile()
    if pending is None:
        raise ValueError("no event profile switch is pending")
    record = coaching_contract.prepare_event_profile_for_activation(pending)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        c.execute("BEGIN IMMEDIATE")
        _upsert_event_profile(c, record, now)
        updated = c.execute(
            """UPDATE event_profile_state
               SET active_profile_id=?,pending_profile_json=NULL,updated_at=?
               WHERE singleton_id=1""",
            (record["id"], now),
        )
        if updated.rowcount != 1:
            raise RuntimeError("active event profile state was not updated")
        c.commit()
    except Exception:
        c.rollback()
        raise
    return record


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
    _ensure_event_profile_state(c)
    c.execute("UPDATE plan_days SET tsb_target=? WHERE tsb_target IS NULL",
              (coaching_contract.DEFAULT_RACE_DAY_TSB_TARGET,))
    profile_id = _active_profile_id(c)
    # Existing production data predates profile scoping and belongs to the
    # profile installed when this migration first runs. Once populated, these
    # values are never rewritten by a later event switch.
    for table in ("plan_days", "plan_history", "constraints_log", "chat_history", "completions"):
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


# --- Durable Coach week-plan drafts -----------------------------------------
def _row_to_plan_draft(row: sqlite3.Row) -> dict[str, Any]:
    draft = dict(row)
    try:
        schedule = json.loads(draft.pop("schedule_json"))
    except (json.JSONDecodeError, TypeError):
        schedule = []
    draft["plan_id"] = draft.pop("id")
    draft["schedule"] = schedule if isinstance(schedule, list) else []
    return draft


def create_plan_draft(schedule: list[dict[str, Any]], *, source_message: str = "") -> dict[str, Any]:
    """Atomically save the exact generated schedule before it can be displayed."""
    import datetime
    if not isinstance(schedule, list) or not schedule:
        raise ValueError("plan draft schedule must be a non-empty list")
    encoded = json.dumps(schedule, ensure_ascii=False, separators=(",", ":"))
    c = _conn()
    profile_id = _active_profile_id(c)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE plan_drafts SET status='superseded',updated_at=?
               WHERE event_profile_id=? AND status='draft'""",
            (now, profile_id),
        )
        cur = c.execute(
            """INSERT INTO plan_drafts
               (event_profile_id,status,schedule_json,source_message,created_at,updated_at)
               VALUES (?,'draft',?,?,?,?)""",
            (profile_id, encoded, source_message, now, now),
        )
        plan_id = int(cur.lastrowid)
        c.commit()
    except Exception:
        c.rollback()
        raise
    draft = get_plan_draft(plan_id)
    if draft is None:
        raise RuntimeError("saved plan draft could not be read back")
    return draft


def get_plan_draft(plan_id: int | str) -> dict[str, Any] | None:
    try:
        value = int(plan_id)
    except (TypeError, ValueError):
        return None
    c = _conn()
    row = c.execute(
        "SELECT * FROM plan_drafts WHERE id=? AND event_profile_id=?",
        (value, _active_profile_id(c)),
    ).fetchone()
    return _row_to_plan_draft(row) if row else None


def get_latest_plan_draft(*, status: str = "draft") -> dict[str, Any] | None:
    c = _conn()
    row = c.execute(
        """SELECT * FROM plan_drafts
           WHERE event_profile_id=? AND status=? ORDER BY id DESC LIMIT 1""",
        (_active_profile_id(c), status),
    ).fetchone()
    return _row_to_plan_draft(row) if row else None


def update_plan_draft(plan_id: int | str, schedule: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Replace an unactivated draft while retaining its stable plan_id."""
    import datetime
    if not isinstance(schedule, list) or not schedule:
        raise ValueError("plan draft schedule must be a non-empty list")
    try:
        value = int(plan_id)
    except (TypeError, ValueError):
        return None
    encoded = json.dumps(schedule, ensure_ascii=False, separators=(",", ":"))
    c = _conn()
    profile_id = _active_profile_id(c)
    existing = c.execute(
        "SELECT status FROM plan_drafts WHERE id=? AND event_profile_id=?",
        (value, profile_id),
    ).fetchone()
    if not existing:
        return None
    if existing["status"] != "draft":
        raise ValueError("only an unactivated plan draft can be edited")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE plan_drafts SET schedule_json=?,updated_at=?
               WHERE id=? AND event_profile_id=? AND status='draft'""",
            (encoded, datetime.datetime.now().isoformat(timespec="seconds"), value, profile_id),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    return get_plan_draft(value)


def activate_plan_draft(plan_id: int | str) -> dict[str, Any]:
    """Apply the exact stored draft and mark it active in one transaction.

    Missing plan dates are inserted so a newly activated arbitrary event can
    accept its first generated schedule even when no event-specific seed builder
    exists. Existing dates retain calendar positioning fields and get a history
    snapshot before the coach update. The schedule is decoded after the write
    lock is acquired, so a concurrent PATCH can never produce a stored/live
    mismatch.
    """
    import datetime
    try:
        value = int(plan_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid plan_id", "plan_id": plan_id}
    c = _conn()
    written_dates: list[str] = []
    try:
        c.execute("BEGIN IMMEDIATE")
        profile_id = _active_profile_id(c)
        draft = c.execute(
            "SELECT status,schedule_json FROM plan_drafts WHERE id=? AND event_profile_id=?",
            (value, profile_id),
        ).fetchone()
        if not draft:
            c.rollback()
            return {"ok": False, "error": "plan draft not found", "plan_id": value}
        if draft["status"] == "active":
            c.rollback()
            return {"ok": True, "plan_id": value, "status": "active", "already_active": True,
                    "updated_days": [], "updated_strength_days": [],
                    "updated_secondary_sessions": [], "count": 0, "skipped": []}
        if draft["status"] != "draft":
            c.rollback()
            return {"ok": False, "error": "plan draft is no longer active for review",
                    "plan_id": value, "status": draft["status"]}
        try:
            stored_schedule = json.loads(draft["schedule_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("stored plan draft is invalid JSON") from exc
        # Late import avoids the module cycle at import time. Validation and
        # normalization operate on the same locked schedule written below.
        from . import coach
        coach.validate_weekplan(stored_schedule, require_not_past=True)
        days, strength_sessions, secondary_sessions = coach._weekplan_for_activation(
            stored_schedule)
        if not days:
            raise ValueError("plan draft has no actionable dated rows")
        strength_by_date = {str(session["date"]): session for session in strength_sessions}
        secondary_by_date: dict[str, list[dict[str, Any]]] = {}
        for session in secondary_sessions:
            secondary_by_date.setdefault(str(session["date"]), []).append(session)

        now = datetime.datetime.now().isoformat(timespec="seconds")
        today = config.local_today()
        try:
            phase = str(coaching_contract.race_phase(today).get("phase") or "build")
        except Exception:  # noqa: BLE001
            phase = "build"
        for day in days:
            date = str(day["date"])
            replace_position = "start_time" in day or bool(day.get("is_rest"))
            planned_start = None if day.get("is_rest") else day.get("start_time")
            position_updated_at = (
                datetime.datetime.now(datetime.timezone.utc).isoformat()
                if replace_position else None
            )
            current = c.execute(
                "SELECT * FROM plan_days WHERE event_profile_id=? AND date=?",
                (profile_id, date),
            ).fetchone()
            structure_json = json.dumps(day.get("structure") or {}, ensure_ascii=False,
                                        separators=(",", ":"))
            if current:
                previous = _row_to_day(current)
                c.execute(
                    """INSERT INTO plan_history
                       (event_profile_id,date,prev,source,reason,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (profile_id, date, json.dumps(previous, default=str), "coach",
                     f"activated plan draft {value}", now),
                )
                c.execute(
                    """UPDATE plan_days SET
                       discipline=?,title=?,structure=?,duration_min=?,intensity=?,
                       tsb_target=?,why=?,is_rest=?,source='coach',updated_at=?,
                       start_time=CASE WHEN ? THEN ? ELSE start_time END,
                       pos_updated_at=CASE WHEN ? THEN ? ELSE pos_updated_at END
                       WHERE event_profile_id=? AND date=?""",
                    (day.get("discipline") or "rest", day.get("title") or "Planned session",
                     structure_json, day.get("duration_min"), day.get("intensity"),
                     day.get("tsb_target"), day.get("why"), int(bool(day.get("is_rest"))),
                     now, int(replace_position), planned_start,
                     int(replace_position), position_updated_at, profile_id, date),
                )
            else:
                try:
                    plan_date = datetime.date.fromisoformat(date)
                    week_index = max(0, (plan_date - today).days // 7)
                except ValueError:
                    raise ValueError(f"invalid draft date {date}")
                c.execute(
                    """INSERT INTO plan_days
                       (event_profile_id,date,week_index,phase,discipline,title,structure,
                        duration_min,intensity,tsb_target,why,is_rest,source,updated_at,
                        start_time,gcal_event_id,pos_updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (profile_id, date, week_index, phase,
                     day.get("discipline") or "rest", day.get("title") or "Planned session",
                     structure_json, day.get("duration_min"), day.get("intensity"),
                     day.get("tsb_target"), day.get("why"), int(bool(day.get("is_rest"))),
                     "coach", now, planned_start, None, position_updated_at),
                )
            strength = strength_by_date.get(date)
            if strength is not None:
                _write_plan_strength(c, profile_id, strength, now)
            else:
                # A changed draft row is the complete schedule for that date.
                # Do not leave an old lift invisibly attached after the coach
                # has rebuilt the day without one.
                c.execute(
                    "DELETE FROM plan_strength WHERE event_profile_id=? AND date=?",
                    (profile_id, date),
                )
            _replace_plan_secondary(
                c, profile_id, date, secondary_by_date.get(date, []), now,
            )
            written_dates.append(date)
        changed = c.execute(
            """UPDATE plan_drafts SET status='active',activated_at=?,updated_at=?
               WHERE id=? AND event_profile_id=? AND status='draft'""",
            (now, now, value, profile_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("plan draft activation state changed concurrently")
        c.commit()
    except Exception:
        c.rollback()
        raise

    placeholders = ",".join("?" for _ in written_dates)
    rows = c.execute(
        f"""SELECT * FROM plan_days WHERE event_profile_id=? AND date IN ({placeholders})
            ORDER BY date""",
        (profile_id, *written_dates),
    ).fetchall()
    updated = [_row_to_day(row) for row in rows]
    strength_rows = c.execute(
        f"""SELECT * FROM plan_strength WHERE event_profile_id=? AND date IN ({placeholders})
            ORDER BY date""",
        (profile_id, *written_dates),
    ).fetchall()
    secondary_rows = c.execute(
        f"""SELECT * FROM plan_secondary
            WHERE event_profile_id=? AND date IN ({placeholders})
            ORDER BY date,session_order,id""",
        (profile_id, *written_dates),
    ).fetchall()
    return {"ok": True, "plan_id": value, "status": "active", "updated_days": updated,
            "updated_strength_days": [_row_to_strength(row) for row in strength_rows],
            "updated_secondary_sessions": [
                _row_to_secondary(row) for row in secondary_rows
            ],
            "count": len(updated), "skipped": []}


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
    c.execute("INSERT INTO completions "
              "(event_profile_id,date,status,notes,garmin_activity_id,rpe,feedback,created_at) "
              "VALUES (?,?,?,?,?,?,?,?)",
              (_active_profile_id(), date, status, notes, garmin_activity_id, rpe, feedback,
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit()


def get_completions(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    c = _conn()
    profile_id = _active_profile_id()
    if start and end:
        rows = c.execute(
            "SELECT * FROM completions WHERE event_profile_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (profile_id, start, end),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM completions WHERE event_profile_id=? ORDER BY date",
            (profile_id,),
        ).fetchall()
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


# --- Structured schedule memory ---------------------------------------------
def _schedule_scope_key(item: dict[str, Any]) -> str:
    """Stable identity for one dated rule, excluding values the athlete may edit.

    A correction such as "classes are 9 to 1, not 8 to 1" updates the existing
    rule instead of leaving two contradictory busy blocks behind.
    """
    identity = {
        "kind": item.get("kind"),
        "label": str(item.get("label") or "").strip().casefold(),
        "start_date": item.get("start_date"),
        "end_date": item.get("end_date"),
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upsert_schedule_constraints(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist a validated set of athlete scheduling facts atomically."""
    import datetime
    if not isinstance(items, list) or not items:
        return []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    keys: list[str] = []
    try:
        c.execute("BEGIN IMMEDIATE")
        for item in items:
            key = _schedule_scope_key(item)
            keys.append(key)
            c.execute(
                """INSERT INTO schedule_constraints
                   (scope_key,kind,label,start_date,end_date,days_json,start_time,
                    end_time,duration_min,note,provenance,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope_key) DO UPDATE SET
                     days_json=excluded.days_json,start_time=excluded.start_time,end_time=excluded.end_time,
                     duration_min=excluded.duration_min,note=excluded.note,
                     provenance=excluded.provenance,updated_at=excluded.updated_at""",
                (key, item["kind"], item["label"], item["start_date"],
                 item["end_date"], json.dumps(item.get("days") or [], separators=(",", ":")),
                 item.get("start_time"), item.get("end_time"), item.get("duration_min"),
                 item.get("note"), item.get("provenance") or "confirmed_by_user",
                 now, now),
            )
        c.commit()
    except Exception:
        c.rollback()
        raise
    placeholders = ",".join("?" for _ in keys)
    rows = c.execute(
        f"SELECT * FROM schedule_constraints WHERE scope_key IN ({placeholders}) ORDER BY id",
        keys,
    ).fetchall()
    return [_row_to_schedule_constraint(row) for row in rows]


def _row_to_schedule_constraint(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    try:
        value["days"] = json.loads(value.pop("days_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        value["days"] = []
    value.pop("scope_key", None)
    return value


def get_schedule_constraints(start: str, end: str,
                             limit: int = 100) -> list[dict[str, Any]]:
    """Return rules whose date window overlaps the requested planning window."""
    rows = _conn().execute(
        """SELECT * FROM schedule_constraints
           WHERE end_date>=? AND start_date<=?
           ORDER BY start_date,id LIMIT ?""",
        (start, end, max(1, min(int(limit), 500))),
    ).fetchall()
    return [_row_to_schedule_constraint(row) for row in rows]


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


# --- Saved recipes and grocery list -----------------------------------------
def _recipe_row(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(row["recipe_json"])
    value.update({"id": row["id"], "saved_at": row["created_at"]})
    return value


def save_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    import datetime
    source_url = str(recipe.get("source_url") or "").strip()
    title = str(recipe.get("title") or "").strip()[:180]
    image_url = str(recipe.get("image_url") or "").strip()
    source_name = str(recipe.get("source_name") or "Recipe source").strip()[:120]
    ingredients = recipe.get("ingredients")
    if not source_url or not title or not image_url or not isinstance(ingredients, list) or not ingredients:
        raise ValueError("recipe needs source_url, title, image_url, and ingredients")
    clean = {
        **recipe,
        "source_url": source_url[:2000], "title": title,
        "image_url": image_url[:2000], "source_name": source_name,
        "ingredients": [str(item).strip()[:300] for item in ingredients
                        if str(item).strip()][:80],
    }
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    c.execute(
        """INSERT INTO saved_recipes
           (source_url,title,image_url,source_name,recipe_json,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source_url) DO UPDATE SET
             title=excluded.title,image_url=excluded.image_url,
             source_name=excluded.source_name,recipe_json=excluded.recipe_json,
             updated_at=excluded.updated_at""",
        (clean["source_url"], clean["title"], clean["image_url"], clean["source_name"],
         json.dumps(clean, ensure_ascii=False, separators=(",", ":")), now, now),
    )
    c.commit()
    row = c.execute("SELECT * FROM saved_recipes WHERE source_url=?", (source_url,)).fetchone()
    return _recipe_row(row)


def get_saved_recipes(limit: int = 50) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM saved_recipes ORDER BY updated_at DESC,id DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return [_recipe_row(row) for row in rows]


def get_saved_recipe(recipe_id: int) -> dict[str, Any] | None:
    row = _conn().execute("SELECT * FROM saved_recipes WHERE id=?", (recipe_id,)).fetchone()
    return _recipe_row(row) if row else None


def delete_saved_recipe(recipe_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM saved_recipes WHERE id=?", (recipe_id,))
    c.commit()
    return cur.rowcount > 0


def _grocery_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:240]


def _grocery_category(text: str) -> str:
    value = text.lower()
    groups = (
        ("Produce", r"\b(?:apple|banana|berry|berries|broccoli|carrot|celery|garlic|ginger|lemon|lime|onion|pepper|potato|spinach|tomato|zucchini|herb|cilantro|parsley)\b"),
        ("Protein", r"\b(?:beef|chicken|egg|fish|pork|salmon|shrimp|steak|tofu|turkey|tuna)\b"),
        ("Dairy", r"\b(?:butter|cheese|cream|milk|yogurt)\b"),
        ("Bakery & grains", r"\b(?:bread|bun|flour|oat|pasta|rice|tortilla|quinoa)\b"),
        ("Pantry", r"\b(?:oil|salt|sauce|spice|sugar|honey|syrup|stock|broth|vinegar)\b"),
    )
    return next((name for name, pattern in groups if re.search(pattern, value)), "Other")


def _grocery_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["checked"] = bool(value["checked"])
    return value


def add_recipe_to_grocery(recipe_id: int) -> list[dict[str, Any]]:
    import datetime
    recipe = get_saved_recipe(recipe_id)
    if not recipe:
        raise ValueError("saved recipe was not found")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    for raw in recipe.get("ingredients") or []:
        text = str(raw).strip()[:300]
        key = _grocery_key(text)
        if not text or not key:
            continue
        c.execute(
            """INSERT INTO grocery_items
               (item_text,normalized_key,category,checked,recipe_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(recipe_id,normalized_key) DO NOTHING""",
            (text, key, _grocery_category(text), 0, recipe_id, now, now),
        )
    c.commit()
    return get_grocery_items()


def add_grocery_item(item_text: str) -> dict[str, Any]:
    import datetime
    text = str(item_text or "").strip()[:300]
    key = _grocery_key(text)
    if not text or not key:
        raise ValueError("grocery item is required")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    cur = c.execute(
        """INSERT INTO grocery_items
           (item_text,normalized_key,category,checked,recipe_id,created_at,updated_at)
           VALUES (?,?,?,?,NULL,?,?)""",
        (text, key, _grocery_category(text), 0, now, now),
    )
    c.commit()
    row = c.execute("SELECT * FROM grocery_items WHERE id=?", (cur.lastrowid,)).fetchone()
    return _grocery_row(row)


def get_grocery_items() -> list[dict[str, Any]]:
    rows = _conn().execute(
        """SELECT g.*,r.title AS recipe_title FROM grocery_items g
           LEFT JOIN saved_recipes r ON r.id=g.recipe_id
           ORDER BY g.checked,g.category,g.id"""
    ).fetchall()
    return [_grocery_row(row) for row in rows]


def set_grocery_checked(item_id: int, checked: bool) -> dict[str, Any] | None:
    import datetime
    c = _conn()
    c.execute("UPDATE grocery_items SET checked=?,updated_at=? WHERE id=?",
              (int(bool(checked)), datetime.datetime.now().isoformat(timespec="seconds"), item_id))
    c.commit()
    row = c.execute(
        """SELECT g.*,r.title AS recipe_title FROM grocery_items g
           LEFT JOIN saved_recipes r ON r.id=g.recipe_id WHERE g.id=?""", (item_id,)).fetchone()
    return _grocery_row(row) if row else None


def delete_grocery_item(item_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM grocery_items WHERE id=?", (item_id,))
    c.commit()
    return cur.rowcount > 0


def clear_checked_grocery() -> int:
    c = _conn()
    cur = c.execute("DELETE FROM grocery_items WHERE checked=1")
    c.commit()
    return cur.rowcount


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


# ---------------------------------------------------------------- strength days

def _row_to_strength(row: Any) -> dict[str, Any]:
    day = dict(row)
    day["discipline"] = "strength"
    try:
        day["routine"] = json.loads(day.pop("routine_json") or "null")
    except (TypeError, ValueError):
        day["routine"] = None
    try:
        day["structure"] = json.loads(day.get("structure") or "{}")
    except (TypeError, ValueError):
        day["structure"] = {}
    return day


def _write_plan_strength(connection: sqlite3.Connection, profile_id: str,
                         day: dict[str, Any], now: str) -> None:
    """Write one strength workout through an existing transaction."""
    date = str(day.get("date") or "")
    if not date:
        raise ValueError("a strength day needs a date")
    slot = str(day.get("slot") or "full").lower()
    if slot not in {"upper", "lower", "full"}:
        raise ValueError(f"unsupported strength slot {slot!r}")
    routine_json = json.dumps(day.get("routine"), default=str)
    structure_json = json.dumps(day.get("structure") or {}, default=str)
    connection.execute(
        """INSERT INTO plan_strength (event_profile_id, date, slot, title,
               routine_json, duration_min, intensity, structure, why, start_time,
               effort_level, effort_cue, hevy_routine_id, gcal_event_id,
               pos_updated_at, source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(event_profile_id, date) DO UPDATE SET
               slot=excluded.slot, title=excluded.title,
               routine_json=excluded.routine_json,
               duration_min=excluded.duration_min,
               intensity=excluded.intensity,
               structure=excluded.structure,
               why=excluded.why,
               start_time=excluded.start_time,
               effort_level=excluded.effort_level,
               effort_cue=excluded.effort_cue,
               hevy_routine_id=COALESCE(excluded.hevy_routine_id,
                                        plan_strength.hevy_routine_id),
               gcal_event_id=COALESCE(excluded.gcal_event_id,
                                      plan_strength.gcal_event_id),
               pos_updated_at=COALESCE(excluded.pos_updated_at,
                                       plan_strength.pos_updated_at),
               source=excluded.source, updated_at=excluded.updated_at""",
        (profile_id, date, slot, str(day.get("title") or "Strength"),
         routine_json, day.get("duration_min"), day.get("intensity"), structure_json,
         day.get("why"), day.get("start_time"), day.get("effort_level"),
         day.get("effort_cue"), day.get("hevy_routine_id"),
         day.get("gcal_event_id"), day.get("pos_updated_at"),
         str(day.get("source") or "coach"), now, now),
    )


def edit_plan_day_with_sessions(date: str, fields: dict[str, Any], *,
                                strength: dict[str, Any] | None = None,
                                secondary_sessions: list[dict[str, Any]] | None = None,
                                replace_strength: bool = True,
                                replace_secondary: bool = True,
                                source: str = "coach") -> dict[str, Any] | None:
    """Atomically patch a primary and its complete same-day session set."""
    import datetime
    c = _conn()
    profile_id = _active_profile_id(c)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM plan_days WHERE event_profile_id=? AND date=?",
            (profile_id, date),
        ).fetchone()
        if not row:
            c.rollback()
            return None
        day = _row_to_day(row)
        c.execute(
            """INSERT INTO plan_history
               (event_profile_id,date,prev,source,reason,created_at)
               VALUES (?,?,?,?,?,?)""",
            (profile_id, date, json.dumps(day, default=str), source,
             "updated independent same-day workouts", now),
        )
        day.update({k: v for k, v in fields.items() if k in {
            "discipline", "title", "structure", "duration_min", "intensity",
            "tsb_target", "why", "is_rest", "phase", "start_time",
            "gcal_event_id", "pos_updated_at",
        }})
        c.execute(
            """UPDATE plan_days SET discipline=?,title=?,structure=?,duration_min=?,
               intensity=?,tsb_target=?,why=?,is_rest=?,phase=?,start_time=?,
               gcal_event_id=?,pos_updated_at=?,source=?,updated_at=?
               WHERE event_profile_id=? AND date=?""",
            (day.get("discipline"), day.get("title"),
             json.dumps(day.get("structure") or {}, ensure_ascii=False,
                        separators=(",", ":")),
             day.get("duration_min"), day.get("intensity"), day.get("tsb_target"),
             day.get("why"), int(bool(day.get("is_rest"))), day.get("phase"),
             day.get("start_time"), day.get("gcal_event_id"),
             day.get("pos_updated_at"), source, now, profile_id, date),
        )
        if replace_strength:
            if strength is not None:
                _write_plan_strength(c, profile_id, {**strength, "date": date}, now)
            else:
                c.execute(
                    "DELETE FROM plan_strength WHERE event_profile_id=? AND date=?",
                    (profile_id, date),
                )
        if replace_secondary:
            _replace_plan_secondary(
                c, profile_id, date, secondary_sessions or [], now,
            )
        c.commit()
    except Exception:
        c.rollback()
        raise
    return get_plan_day(date)


def edit_plan_day_with_strength(date: str, fields: dict[str, Any],
                                strength: dict[str, Any], *,
                                source: str = "coach") -> dict[str, Any] | None:
    """Backward-compatible wrapper for attaching a separate same-day lift."""
    return edit_plan_day_with_sessions(
        date, fields, strength=strength, replace_strength=True,
        replace_secondary=False, source=source,
    )


def upsert_plan_strength(day: dict[str, Any]) -> dict[str, Any]:
    """Attach (or replace) a complete, independent lift on one calendar day."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    date = str(day.get("date") or "")
    with _conn() as conn:
        _write_plan_strength(conn, _active_profile_id(conn), day, now)
    result = get_plan_strength(date)
    if result is None:  # pragma: no cover - only reachable if the row vanished
        raise RuntimeError("strength day was not stored")
    return result


def get_plan_strength(date: str) -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM plan_strength WHERE date=? AND event_profile_id=?",
        (date, _active_profile_id()),
    ).fetchone()
    return _row_to_strength(row) if row else None


def get_plan_strength_range(start: str, end: str) -> list[dict[str, Any]]:
    rows = _conn().execute(
        """SELECT * FROM plan_strength
           WHERE event_profile_id=? AND date BETWEEN ? AND ? ORDER BY date""",
        (_active_profile_id(), start, end),
    ).fetchall()
    return [_row_to_strength(r) for r in rows]


def set_plan_strength_calendar_fields(date: str, *,
                                      start_time: str | None = None,
                                      gcal_event_id: str | None = None,
                                      pos_updated_at: str | None = None,
                                      clear_event_id: bool = False) -> dict[str, Any] | None:
    """Update only calendar-owned fields without replacing the lift prescription."""
    import datetime
    fields: list[str] = []
    values: list[Any] = []
    if start_time is not None:
        fields.append("start_time=?")
        values.append(start_time)
    if gcal_event_id is not None or clear_event_id:
        fields.append("gcal_event_id=?")
        values.append(gcal_event_id)
    if pos_updated_at is not None:
        fields.append("pos_updated_at=?")
        values.append(pos_updated_at)
    if not fields:
        return get_plan_strength(date)
    fields.append("updated_at=?")
    values.append(datetime.datetime.now(datetime.timezone.utc).isoformat())
    values.extend((_active_profile_id(), date))
    with _conn() as conn:
        conn.execute(
            f"UPDATE plan_strength SET {', '.join(fields)} "
            "WHERE event_profile_id=? AND date=?",
            values,
        )
    return get_plan_strength(date)


def move_plan_strength(date: str, new_date: str | None = None,
                       new_start: str | None = None,
                       pos_updated_at: str | None = None) -> list[str]:
    """Move/swap a lift independently of the primary workout on either date."""
    import datetime
    c = _conn()
    profile_id = _active_profile_id(c)
    target = new_date or date
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pts = pos_updated_at or now
    try:
        c.execute("BEGIN IMMEDIATE")
        src_row = c.execute(
            "SELECT * FROM plan_strength WHERE event_profile_id=? AND date=?",
            (profile_id, date),
        ).fetchone()
        if not src_row:
            c.rollback()
            raise ValueError(f"no strength session for {date}")
        src = _row_to_strength(src_row)
        if target == date:
            if new_start is not None:
                c.execute(
                    """UPDATE plan_strength SET start_time=?,pos_updated_at=?,
                       source='edited',updated_at=? WHERE event_profile_id=? AND date=?""",
                    (new_start, pts, now, profile_id, date),
                )
            c.commit()
            return [date]

        dst_row = c.execute(
            "SELECT * FROM plan_strength WHERE event_profile_id=? AND date=?",
            (profile_id, target),
        ).fetchone()
        dst = _row_to_strength(dst_row) if dst_row else None
        if dst is None:
            moved = {**src, "date": target, "start_time": new_start or src.get("start_time"),
                     "pos_updated_at": pts, "source": "edited"}
            _write_plan_strength(c, profile_id, moved, now)
            c.execute(
                "DELETE FROM plan_strength WHERE event_profile_id=? AND date=?",
                (profile_id, date),
            )
        else:
            # Calendar identity stays with each date; the lift prescriptions swap.
            src_calendar = {
                "date": date, "start_time": src.get("start_time"),
                "gcal_event_id": src.get("gcal_event_id"),
            }
            dst_calendar = {
                "date": target, "start_time": new_start or dst.get("start_time"),
                "gcal_event_id": dst.get("gcal_event_id"),
            }
            _write_plan_strength(c, profile_id, {
                **src, **dst_calendar, "pos_updated_at": pts, "source": "edited",
            }, now)
            _write_plan_strength(c, profile_id, {
                **dst, **src_calendar, "pos_updated_at": pts, "source": "edited",
            }, now)
        c.commit()
    except Exception:
        c.rollback()
        raise
    return [date, target]


def delete_plan_strength(date: str) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM plan_strength WHERE date=? AND event_profile_id=?",
            (date, _active_profile_id()),
        )
    return cursor.rowcount > 0


# ----------------------------------------------------- additional plan sessions

def _row_to_secondary(row: Any) -> dict[str, Any]:
    session = dict(row)
    try:
        session["structure"] = json.loads(session.get("structure") or "{}")
    except (TypeError, ValueError):
        session["structure"] = {}
    return session


def _replace_plan_secondary(connection: sqlite3.Connection, profile_id: str,
                            date: str, sessions: list[dict[str, Any]], now: str) -> None:
    """Replace a date's non-strength secondary workouts inside a transaction.

    Slots are stable across edits, which preserves each Google event id. Extra
    old slots are removed because a coach adjustment/draft describes the full
    set of additional workouts for that date.
    """
    kept_orders: list[int] = []
    for order, session in enumerate(sessions, start=1):
        structure = json.dumps(session.get("structure") or {}, ensure_ascii=False,
                               separators=(",", ":"))
        connection.execute(
            """INSERT INTO plan_secondary
               (event_profile_id,date,session_order,discipline,title,structure,
                duration_min,intensity,why,start_time,gcal_event_id,pos_updated_at,
                source,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_profile_id,date,session_order) DO UPDATE SET
                 discipline=excluded.discipline,title=excluded.title,
                 structure=excluded.structure,duration_min=excluded.duration_min,
                 intensity=excluded.intensity,why=excluded.why,
                 start_time=excluded.start_time,
                 gcal_event_id=COALESCE(excluded.gcal_event_id,
                                        plan_secondary.gcal_event_id),
                 pos_updated_at=COALESCE(excluded.pos_updated_at,
                                         plan_secondary.pos_updated_at),
                 source=excluded.source,updated_at=excluded.updated_at""",
            (profile_id, date, order, session.get("discipline") or "other",
             session.get("title") or "Additional workout", structure,
             session.get("duration_min"), session.get("intensity"), session.get("why"),
             session.get("start_time"), session.get("gcal_event_id"),
             session.get("pos_updated_at"), session.get("source") or "coach", now, now),
        )
        kept_orders.append(order)
    if kept_orders:
        placeholders = ",".join("?" for _ in kept_orders)
        connection.execute(
            f"""DELETE FROM plan_secondary
                WHERE event_profile_id=? AND date=?
                  AND session_order NOT IN ({placeholders})""",
            (profile_id, date, *kept_orders),
        )
    else:
        connection.execute(
            "DELETE FROM plan_secondary WHERE event_profile_id=? AND date=?",
            (profile_id, date),
        )


def get_plan_secondary(date: str) -> list[dict[str, Any]]:
    rows = _conn().execute(
        """SELECT * FROM plan_secondary
           WHERE event_profile_id=? AND date=? ORDER BY session_order,id""",
        (_active_profile_id(), date),
    ).fetchall()
    return [_row_to_secondary(row) for row in rows]


def get_plan_secondary_range(start: str, end: str) -> list[dict[str, Any]]:
    rows = _conn().execute(
        """SELECT * FROM plan_secondary
           WHERE event_profile_id=? AND date BETWEEN ? AND ?
           ORDER BY date,session_order,id""",
        (_active_profile_id(), start, end),
    ).fetchall()
    return [_row_to_secondary(row) for row in rows]


def get_plan_secondary_by_id(session_id: int | str) -> dict[str, Any] | None:
    try:
        value = int(session_id)
    except (TypeError, ValueError):
        return None
    row = _conn().execute(
        "SELECT * FROM plan_secondary WHERE event_profile_id=? AND id=?",
        (_active_profile_id(), value),
    ).fetchone()
    return _row_to_secondary(row) if row else None


def set_plan_secondary_calendar_fields(session_id: int | str, *,
                                       start_time: str | None = None,
                                       gcal_event_id: str | None = None,
                                       pos_updated_at: str | None = None,
                                       clear_event_id: bool = False) -> dict[str, Any] | None:
    """Update calendar-owned fields without replacing the prescription."""
    import datetime
    session = get_plan_secondary_by_id(session_id)
    if not session:
        return None
    fields: list[str] = []
    values: list[Any] = []
    if start_time is not None:
        fields.append("start_time=?")
        values.append(start_time)
    if gcal_event_id is not None or clear_event_id:
        fields.append("gcal_event_id=?")
        values.append(gcal_event_id)
    if pos_updated_at is not None:
        fields.append("pos_updated_at=?")
        values.append(pos_updated_at)
    if not fields:
        return session
    fields.append("updated_at=?")
    values.append(datetime.datetime.now(datetime.timezone.utc).isoformat())
    values.extend((_active_profile_id(), int(session["id"])))
    with _conn() as connection:
        connection.execute(
            f"UPDATE plan_secondary SET {', '.join(fields)} "
            "WHERE event_profile_id=? AND id=?",
            values,
        )
    return get_plan_secondary_by_id(session["id"])


def move_plan_secondary(session_id: int | str, *, new_date: str | None = None,
                        new_start: str | None = None,
                        pos_updated_at: str | None = None) -> list[str]:
    """Move one additional workout without merging/swapping other sessions."""
    import datetime
    session = get_plan_secondary_by_id(session_id)
    if not session:
        raise ValueError(f"no additional workout with id {session_id}")
    source_date = str(session["date"])
    target_date = new_date or source_date
    if not get_plan_day(target_date):
        raise ValueError(f"no plan day for {target_date}; ask Coach to add that date first")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pts = pos_updated_at or now
    connection = _conn()
    profile_id = _active_profile_id(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = int(session.get("session_order") or 1)
        if target_date != source_date:
            row = connection.execute(
                """SELECT COALESCE(MAX(session_order),0)+1 AS next_order
                   FROM plan_secondary WHERE event_profile_id=? AND date=?""",
                (profile_id, target_date),
            ).fetchone()
            order = int(row["next_order"])
        connection.execute(
            """UPDATE plan_secondary SET date=?,session_order=?,
               start_time=COALESCE(?,start_time),pos_updated_at=?,source='edited',updated_at=?
               WHERE event_profile_id=? AND id=?""",
            (target_date, order, new_start, pts, now, profile_id, int(session["id"])),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return [source_date] if target_date == source_date else [source_date, target_date]


# ---------------------------------------------------------------- lifting split

def get_lifting_split() -> dict[str, Any] | None:
    """The stored split for the active event, or None to use the default."""
    row = _conn().execute(
        "SELECT * FROM lifting_split WHERE event_profile_id=?",
        (_active_profile_id(),),
    ).fetchone()
    if not row:
        return None
    record = dict(row)
    try:
        record["days"] = json.loads(record.pop("days_json") or "null")
    except (TypeError, ValueError):
        return None
    return record if isinstance(record.get("days"), list) else None


def save_lifting_split(days: list[dict[str, Any]], *, source: str = "edited") -> dict[str, Any]:
    """Persist an edited split. Stored verbatim so what is shown is what was agreed."""
    import datetime
    if not isinstance(days, list) or not days:
        raise ValueError("a split needs at least one day")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO lifting_split (event_profile_id, days_json, source, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(event_profile_id) DO UPDATE SET
                   days_json=excluded.days_json, source=excluded.source,
                   updated_at=excluded.updated_at""",
            (_active_profile_id(), json.dumps(days, default=str), str(source), now),
        )
    stored = get_lifting_split()
    if stored is None:  # pragma: no cover - only if the row vanished
        raise RuntimeError("split was not stored")
    return stored


def reset_lifting_split() -> bool:
    """Drop the stored split so the default applies again."""
    with _conn() as conn:
        cursor = conn.execute("DELETE FROM lifting_split WHERE event_profile_id=?",
                              (_active_profile_id(),))
    return cursor.rowcount > 0
