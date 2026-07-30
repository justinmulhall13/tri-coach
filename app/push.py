"""Web Push delivery for proactive nudges.

The Fly app scales to zero, so timed sends can't run from a sleeping server. An
EXTERNAL scheduler (GitHub Actions / cron-job.org) hits `POST /api/push/run` at
set local times — that wakes the machine, computes today's nudges via `nudges.py`,
and pushes the meaningful ones to every subscribed device (deduped per day). No
always-on machine required.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from typing import Any

from . import config, db, nudges

_PEM_PATH: str | None = None


def _pem_path() -> str | None:
    """Materialize the VAPID private key as a PEM file (pywebpush wants a path)."""
    global _PEM_PATH
    if _PEM_PATH and os.path.exists(_PEM_PATH):
        return _PEM_PATH
    pem = config.VAPID_PRIVATE_KEY
    if not pem:
        return None
    if "BEGIN" not in pem:                       # stored as base64 of the PEM
        try:
            pem = base64.b64decode(pem).decode()
        except Exception:
            return None
    f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    f.write(pem)
    f.close()
    _PEM_PATH = f.name
    return _PEM_PATH


def configured() -> bool:
    return bool(config.VAPID_PUBLIC_KEY and config.VAPID_PRIVATE_KEY)


def public_key() -> str:
    return config.VAPID_PUBLIC_KEY


def save_subscription(sub: dict[str, Any]) -> bool:
    endpoint = sub.get("endpoint")
    if not endpoint:
        return False
    db.add_push_subscription(endpoint, json.dumps(sub))
    return True


def _send_one(sub: dict[str, Any], payload: dict[str, Any]) -> int:
    """Returns HTTP status (0 on library error)."""
    from pywebpush import webpush, WebPushException
    try:
        webpush(subscription_info=sub, data=json.dumps(payload),
                vapid_private_key=_pem_path(),
                vapid_claims={"sub": config.VAPID_SUBJECT})
        return 201
    except WebPushException as e:
        return getattr(getattr(e, "response", None), "status_code", 0) or 0


def send(nudge_list: list[dict[str, Any]]) -> int:
    if not configured():
        return 0
    subs = db.get_push_subscriptions()
    sent = 0
    for row in subs:
        try:
            sub = json.loads(row["sub"])
        except (json.JSONDecodeError, TypeError):
            continue
        for n in nudge_list:
            code = _send_one(sub, {"title": n["title"], "body": n.get("detail", ""),
                                   "tag": n["id"], "icon": "/static/icon.png"})
            if code in (404, 410):               # subscription is dead — drop it
                db.delete_push_subscription(row["id"])
                break
            if code in (200, 201):
                sent += 1
    return sent


def run(force: bool = False) -> dict[str, Any]:
    """Compute today's nudges and push the meaningful, not-yet-sent ones."""
    data = nudges.get_nudges()
    day = data.get("date")
    to_send: list[dict[str, Any]] = []
    for n in data.get("nudges", []):
        key = f"push_sent|{day}|{n['id']}"
        if not force and db.get_meta(key):
            continue
        to_send.append(n)
        db.set_meta(key, "1")
    sent = send(to_send) if to_send else 0
    return {"date": day, "candidates": len(to_send), "sent": sent,
            "subscriptions": len(db.get_push_subscriptions()),
            "titles": [n["title"] for n in to_send]}
