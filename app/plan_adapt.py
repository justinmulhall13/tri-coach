"""Closed-loop plan adaptation — the thing the commercial apps sell.

Two deterministic entry points that actually rewrite *upcoming* plan days (never
the past, never the periodization intent — just volume/intensity nudges):

  apply_session_feedback()  post-session RPE + completion → up/down-regulate the
                            next few days (too hard → back off, too easy → nudge up).
  reflow()                  "life happened" — illness / travel / a missed session
                            reshapes the coming week.

Edits are transparent (each returns exactly what changed and why), preserve any
day you or the coach hand-tuned (source stays 'edited'/'coach'), are marked
source='adapt' so a reseed won't clobber them, and drop a constraint note so
Coach Steve sees the same story you do.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from . import config, db

_HARD_NOTE = re.compile(r"\b(sick|ill|illness|injur|pain|sore|fever|cold|flu|exhaust|cooked|dead|tired|niggle)\b", re.I)
_QUALITY = re.compile(r"threshold|vo2|race|tempo|interval|css|sharpen", re.I)
_EDITABLE = {"seed", "adapt"}


def _round5(x: float) -> int:
    return max(20, int(round(x / 5.0)) * 5)


def _upcoming(limit_days: int = 21) -> list[dict[str, Any]]:
    today = config.local_today().isoformat()
    end = (config.local_today() + datetime.timedelta(days=limit_days)).isoformat()
    return [d for d in db.get_plan(today, end) if d["date"] > today]


def _is_quality(day: dict[str, Any]) -> bool:
    return bool(_QUALITY.search(f"{day.get('intensity','')} {day.get('title','')}"))


def _scale_day(day: dict[str, Any], factor: float, tag: str) -> dict[str, Any] | None:
    """Scale a day's duration by `factor`, annotate why. Returns a change record
    or None if the day isn't ours to touch / has no duration."""
    if day.get("source") not in _EDITABLE or day.get("is_rest") or not day.get("duration_min"):
        return None
    before = day["duration_min"]
    after = _round5(before * factor)
    if after == before:
        return None
    if after < before:
        cost = f"{before - after} min less {day.get('discipline') or 'training'} stimulus"
        benefit = f"{before - after} min less fatigue and more recovery margin"
    else:
        cost = f"{after - before} min more fatigue to absorb"
        benefit = f"{after - before} min more {day.get('discipline') or 'training'} stimulus"
    why = day.get("why") or ""
    why = (f"[Adapted: {tag}] " + re.sub(r"^\[Adapted[^\]]*\]\s*", "", why)
           + f" Cost: {cost}. Buy: {benefit}.")
    db.edit_plan_day(day["date"], {"duration_min": after, "why": why}, source="adapt")
    return {"date": day["date"], "title": day.get("title"), "field": "duration_min",
            "before": before, "after": after, "cost": cost, "benefit": benefit}


def _make_recovery(day: dict[str, Any], tag: str) -> dict[str, Any] | None:
    if day.get("source") not in _EDITABLE:
        return None
    before = f"{day.get('discipline')} · {day.get('duration_min')}m"
    cost = f"loses the planned {day.get('title') or day.get('discipline') or 'training'} stimulus"
    benefit = "buys recovery margin and protects the next executable quality session"
    db.edit_plan_day(day["date"], {
        "discipline": "recovery", "title": "Recovery (adapted)", "intensity": "Z1",
        "duration_min": min(30, day.get("duration_min") or 30), "is_rest": 0,
        "structure": {"warmup": "—", "main": "20–30 min very easy or full rest", "cooldown": "—"},
        "why": f"[Adapted: {tag}] Cost: {cost}. Buy: {benefit}.",
    }, source="adapt")
    return {"date": day["date"], "title": "→ Recovery", "field": "discipline",
            "before": before, "after": "recovery", "cost": cost, "benefit": benefit}


# --- Post-session feedback ----------------------------------------------------
def apply_session_feedback(date: str, status: str = "done", rpe: int | None = None,
                           note: str = "") -> dict[str, Any]:
    """Record a completed/partial/skipped session and adapt the next few days."""
    status = (status or "done").lower()
    note = (note or "").strip()
    db.add_completion(date, status, notes=note, rpe=rpe, feedback=note)
    db.add_constraint(date, f"Session feedback ({date}): {status}"
                      + (f", RPE {rpe}" if rpe is not None else "")
                      + (f" — {note}" if note else ""))

    hard = (status in ("skipped", "partial")
            or (isinstance(rpe, int) and rpe >= 9)
            or bool(_HARD_NOTE.search(note)))
    easy = (status == "done" and isinstance(rpe, int) and rpe <= 3 and not _HARD_NOTE.search(note))

    changes: list[dict[str, Any]] = []
    upcoming = _upcoming()

    if hard:
        very_hard = (isinstance(rpe, int) and rpe >= 9) or bool(_HARD_NOTE.search(note))
        # Back off the next 3 training days ~25%.
        touched = 0
        for day in upcoming:
            if touched >= 3:
                break
            ch = _scale_day(day, 0.75, "recovering")
            if ch:
                changes.append(ch); touched += 1
        # If it was really rough, turn the very next quality day into recovery.
        if very_hard:
            for day in upcoming:
                if _is_quality(day):
                    ch = _make_recovery(day, "recovering")
                    if ch:
                        changes.append(ch)
                    break
        direction = "downregulated"
        summary = ("Backed off the next few days. Cost: less planned stimulus. "
                   "Buy: lower fatigue and a safer return to quality.")
    elif easy:
        # Nudge the next quality day up ~12%.
        for day in upcoming:
            if _is_quality(day):
                ch = _scale_day(day, 1.12, "progressing")
                if ch:
                    changes.append(ch)
                break
        direction = "upregulated"
        summary = ("Nudged the next quality day up. Cost: more fatigue to absorb. "
                   "Buy: additional quality stimulus.")
    else:
        direction = "held"
        summary = "Logged. Session was on target — plan held as written."

    if hard and not changes:
        summary = "Logged. Nothing upcoming was ours to adjust (edited/coach days preserved)."

    return {"applied": bool(changes), "direction": direction, "summary": summary, "changes": changes}


# --- Life-happened reflow -----------------------------------------------------
def reflow(kind: str = "illness", days: int = 3, note: str = "") -> dict[str, Any]:
    """Reshape the coming week around illness / travel / a missed session."""
    kind = (kind or "illness").lower()
    days = max(1, min(int(days or 3), 7))
    upcoming = _upcoming()
    changes: list[dict[str, Any]] = []
    today = config.local_today().isoformat()

    if kind == "illness":
        # First `days` become recovery/rest; then ease back the following 3.
        for day in upcoming[:days]:
            ch = _make_recovery(day, "illness")
            if ch:
                changes.append(ch)
        for day in upcoming[days:days + 3]:
            ch = _scale_day(day, 0.6, "return-to-train")
            if ch:
                changes.append(ch)
        summary = (f"Illness: next {days} day(s) set to recovery, then a gradual ramp. "
                   "Cost: lost planned stimulus. Buy: recovery and reduced relapse risk.")

    elif kind == "travel":
        # Keep training but make it travel-friendly: shorter, easy.
        for day in upcoming[:days]:
            ch = _scale_day(day, 0.55, "travel")
            if ch:
                changes.append(ch)
        summary = (f"Travel: next {days} day(s) trimmed. Cost: less volume stimulus. "
                   "Buy: sessions remain executable with lower fatigue.")

    else:  # missed / catch-all
        # Don't cram — just lighten the next couple of quality days a touch.
        touched = 0
        for day in upcoming:
            if touched >= 2:
                break
            if _is_quality(day):
                ch = _scale_day(day, 0.85, "post-miss")
                if ch:
                    changes.append(ch); touched += 1
        summary = ("Missed session logged. Cost: slightly less quality stimulus. "
                   "Buy: avoids cramming fatigue into the remaining week.")

    db.add_constraint(today, f"Reflow ({kind}, {days}d)" + (f": {note}" if note else "") + f" — {summary}")
    return {"kind": kind, "applied": bool(changes), "summary": summary, "changes": changes}
