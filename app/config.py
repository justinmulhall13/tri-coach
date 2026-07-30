"""Configuration + derived race phase. All secrets come from .env."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # the coach/ project dir
load_dotenv(BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# --- Race ---------------------------------------------------------------------
RACE_NAME = _get("RACE_NAME", "T100 Vancouver")
RACE_DATE = _get("RACE_DATE", "2026-08-16")  # ISO YYYY-MM-DD
SWIM_KM = float(_get("SWIM_KM", "2.0"))
BIKE_KM = float(_get("BIKE_KM", "80"))
RUN_KM = float(_get("RUN_KM", "18"))

# --- Athlete profile (user-supplied, NOT ground truth) ------------------------
ATHLETE_PROFILE = {
    "ftp_w": _get("ATHLETE_FTP_W", "288"),
    "swim_background": _get("ATHLETE_SWIM_BACKGROUND", ""),
    "device": _get("ATHLETE_DEVICE", ""),
    "notes": _get("ATHLETE_NOTES", ""),
    "_disclaimer": "User-supplied profile. Verify before relying; not measured ground truth.",
}

# --- Nutrition (Chef Gordo) ---------------------------------------------------
# Bodyweight is used ONLY for fueling math (carbs/kg, protein/kg, portions) — the
# athlete's goal is being maximally fuelled, NOT weight loss. Targets are framed
# as loose ranges, not hard numbers.
ATHLETE_WEIGHT_KG = float(_get("ATHLETE_WEIGHT_KG", "88.7"))   # 195.6 lb
NUTRITION_PREFS = _get("NUTRITION_PREFS", "No restrictions — suggest anything balanced; "
                       "athlete eats meat + rice/potato staples readily.")

# --- Anthropic ----------------------------------------------------------------
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
COACH_MODEL = _get("COACH_MODEL", "claude-opus-4-8")
# Fast, cheap model for structured/low-reasoning tasks (calendar command parsing).
FAST_MODEL = _get("FAST_MODEL", "claude-haiku-4-5-20251001")

# --- Google Calendar ----------------------------------------------------------
GOOGLE_CREDENTIALS_FILE = _get("GOOGLE_CREDENTIALS_FILE", "./data/credentials.json")
GOOGLE_TOKEN_FILE = _get("GOOGLE_TOKEN_FILE", "./data/token.json")
GOOGLE_CALENDAR_IDS = [c.strip() for c in _get("GOOGLE_CALENDAR_IDS", "primary").split(",") if c.strip()]

# --- App ----------------------------------------------------------------------
HOST = _get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(_get("DASHBOARD_PORT", "8770"))
DB_PATH = _get("DB_PATH", "./data/coach.db")
TIMEZONE = _get("TIMEZONE", "America/Vancouver")  # drives local_today()/local_now()

# Shared-secret gate for /api/* when the app is exposed to the public internet
# (cloud hosting). Empty = no auth, which keeps local/Mac use frictionless.
ACCESS_TOKEN = _get("ACCESS_TOKEN", "")

# --- Web push (proactive nudge notifications) ---------------------------------
VAPID_PUBLIC_KEY = _get("VAPID_PUBLIC_KEY")   # base64url app-server key (browser)
VAPID_PRIVATE_KEY = _get("VAPID_PRIVATE_KEY")  # PEM (or base64 of PEM) — server only
VAPID_SUBJECT = _get("VAPID_SUBJECT", "mailto:justinmulhall13@gmail.com")
PUSH_CRON_KEY = _get("PUSH_CRON_KEY")          # shared key the external scheduler uses

# Where the garminconnect OAuth token lives. Default matches the library's own
# default (~/.garminconnect). On a server we point this at the persistent volume.
GARMIN_TOKENSTORE = _get("GARMIN_TOKENSTORE", "~/.garminconnect")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


# --- Local time -------------------------------------------------------------
# The server clock is UTC (Fly), but "today" and time-of-day logic must follow
# the athlete's local calendar. Without this, late-evening Pacific time rolls
# over to tomorrow's date (UTC midnight) — the app "thought it was tomorrow".
def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def local_now() -> datetime.datetime:
    """Timezone-aware current time in the athlete's local zone."""
    return datetime.datetime.now(local_tz())


def local_today() -> datetime.date:
    """The athlete's local calendar date. Use this everywhere instead of
    datetime.date.today() so the day only rolls over at local midnight."""
    return local_now().date()


def race_phase(today: datetime.date | None = None) -> dict:
    """Countdown + periodization phase derived from weeks remaining.

    Peaking block logic (NOT base building):
      - taper:  <= 14 days out
      - peak:   15–28 days out
      - build:  > 28 days out
    """
    today = today or local_today()
    try:
        race = datetime.date.fromisoformat(RACE_DATE)
    except ValueError:
        return {"name": RACE_NAME, "date": RACE_DATE, "error": "RACE_DATE invalid (need YYYY-MM-DD)"}
    days = (race - today).days
    if days < 0:
        phase = "post-race"
    elif days <= 14:
        phase = "taper"
    elif days <= 28:
        phase = "peak"
    else:
        phase = "build"
    return {
        "name": RACE_NAME,
        "date": RACE_DATE,
        "distances": {"swim_km": SWIM_KM, "bike_km": BIKE_KM, "run_km": RUN_KM},
        "days_remaining": days,
        "weeks_remaining": round(days / 7, 1),
        "phase": phase,
    }
