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

CREATE TABLE IF NOT EXISTS plan_strength (
    -- A lift attached to a calendar day. plan_days is one row per date and the
    -- whole app depends on that (reconciliation, calendar sync, activation), so
    -- a lift that shares a day with a run lives here and renders beneath it
    -- rather than competing for the same row.
    event_profile_id TEXT NOT NULL,
    date             TEXT NOT NULL,             -- ISO YYYY-MM-DD
    slot             TEXT NOT NULL,             -- upper | lower | full
    title            TEXT NOT NULL,
    routine_json     TEXT NOT NULL,             -- validated Hevy routine template
    effort_level     TEXT,                      -- heavy | moderate | light | skip
    effort_cue       TEXT,
    hevy_routine_id  TEXT,                      -- set once actually created in Hevy
    source           TEXT NOT NULL DEFAULT 'coach',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (event_profile_id, date)
);

CREATE INDEX IF NOT EXISTS idx_plan_strength_profile_date
    ON plan_strength (event_profile_id, date);

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
                    "updated_days": [], "count": 0, "skipped": []}
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
        days, _ = coach._weekplan_for_activation(stored_schedule)
        if not days:
            raise ValueError("plan draft has no actionable dated rows")

        now = datetime.datetime.now().isoformat(timespec="seconds")
        today = config.local_today()
        try:
            phase = str(coaching_contract.race_phase(today).get("phase") or "build")
        except Exception:  # noqa: BLE001
            phase = "build"
        for day in days:
            date = str(day["date"])
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
                       tsb_target=?,why=?,is_rest=?,source='coach',updated_at=?
                       WHERE event_profile_id=? AND date=?""",
                    (day.get("discipline") or "rest", day.get("title") or "Planned session",
                     structure_json, day.get("duration_min"), day.get("intensity"),
                     day.get("tsb_target"), day.get("why"), int(bool(day.get("is_rest"))),
                     now, profile_id, date),
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
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)""",
                    (profile_id, date, week_index, phase,
                     day.get("discipline") or "rest", day.get("title") or "Planned session",
                     structure_json, day.get("duration_min"), day.get("intensity"),
                     day.get("tsb_target"), day.get("why"), int(bool(day.get("is_rest"))),
                     "coach", now),
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
    return {"ok": True, "plan_id": value, "status": "active", "updated_days": updated,
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


# ---------------------------------------------------------------- strength days

def _row_to_strength(row: Any) -> dict[str, Any]:
    day = dict(row)
    try:
        day["routine"] = json.loads(day.pop("routine_json") or "null")
    except (TypeError, ValueError):
        day["routine"] = None
    return day


def upsert_plan_strength(day: dict[str, Any]) -> dict[str, Any]:
    """Attach (or replace) the lift on one calendar day.

    The routine is stored verbatim so the session shown later is the session
    that was agreed, not one regenerated from a prompt.
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    date = str(day.get("date") or "")
    if not date:
        raise ValueError("a strength day needs a date")
    slot = str(day.get("slot") or "full").lower()
    if slot not in {"upper", "lower", "full"}:
        raise ValueError(f"unsupported strength slot {slot!r}")
    routine_json = json.dumps(day.get("routine"), default=str)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO plan_strength (event_profile_id, date, slot, title,
                   routine_json, effort_level, effort_cue, hevy_routine_id,
                   source, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_profile_id, date) DO UPDATE SET
                   slot=excluded.slot, title=excluded.title,
                   routine_json=excluded.routine_json,
                   effort_level=excluded.effort_level,
                   effort_cue=excluded.effort_cue,
                   hevy_routine_id=COALESCE(excluded.hevy_routine_id,
                                            plan_strength.hevy_routine_id),
                   source=excluded.source, updated_at=excluded.updated_at""",
            (_active_profile_id(), date, slot, str(day.get("title") or "Strength"),
             routine_json, day.get("effort_level"), day.get("effort_cue"),
             day.get("hevy_routine_id"), str(day.get("source") or "coach"), now, now),
        )
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


def delete_plan_strength(date: str) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM plan_strength WHERE date=? AND event_profile_id=?",
            (date, _active_profile_id()),
        )
    return cursor.rowcount > 0
