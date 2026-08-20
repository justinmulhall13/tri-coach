"""Configuration + derived race phase. All secrets come from .env."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from . import coaching_contract

BASE_DIR = Path(__file__).resolve().parent.parent  # the coach/ project dir
load_dotenv(BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# --- Race ---------------------------------------------------------------------
def _distance_value(raw: object) -> float:
    """Normalize an optional profile distance without inventing one."""
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def active_event_profile() -> dict:
    """Read the persisted profile; never snapshot it at module import."""
    return coaching_contract.event_context()


def race_name() -> str:
    return str(active_event_profile().get("event") or "Unknown event")


def race_date() -> str:
    return str(active_event_profile().get("date") or "")


def event_distance_km(discipline: str) -> float | None:
    """Return a stated distance for one active-profile leg, otherwise unknown."""
    distances = active_event_profile().get("disciplines_and_distances") or {}
    if not isinstance(distances, dict):
        return None
    value = _distance_value(distances.get(f"{(discipline or '').lower()}_km"))
    return value or None


def event_has_leg(discipline: str) -> bool:
    return event_distance_km(discipline) is not None


def supports_t100_features() -> bool:
    """True only for the one profile the bundled readiness model/plan fits.

    A TRIATHLON mode label alone is insufficient: another triathlon can have
    different distances and volume targets, so reusing the Vancouver model
    would silently carry event assumptions across a profile switch.
    """
    event = active_event_profile()
    return (
        coaching_contract.current_mode() == "TRIATHLON"
        and str(event.get("id") or "") == "t100-vancouver-2026"
        and all(event_has_leg(sport) for sport in ("swim", "bike", "run"))
    )

# --- Athlete profile (user-supplied, NOT ground truth) ------------------------
ATHLETE_PROFILE = {
    "age": coaching_contract.ATHLETE_CONSTANTS["age"],
    "sex": coaching_contract.ATHLETE_CONSTANTS["sex"],
    "body_mass_fallback_kg": coaching_contract.ATHLETE_CONSTANTS["body_mass_fallback"]["value"],
    "ftp_w": str(coaching_contract.ATHLETE_CONSTANTS["bike_prescription"]["peloton_ftp_w"]),
    "ftp_scope": coaching_contract.ATHLETE_CONSTANTS["bike_prescription"]["ftp_scope"],
    "swim_background": _get("ATHLETE_SWIM_BACKGROUND", ""),
    # Do not accept the legacy environment device string: older deployments
    # claimed an outdoor cycling power meter and contradicted the HR-only rule.
    "bike_target_device_constraint": "heart rate only outdoors; Peloton watts indoors only",
    "notes": _get("ATHLETE_NOTES", ""),
    "_provenance": "self-reported; a dated athlete-maintained Garmin weight entry overrides body mass for weight-dependent math",
}

# --- Nutrition (Chef Gordo) ---------------------------------------------------
# Bodyweight is used ONLY for fueling math (carbs/kg, protein/kg, portions) — the
# athlete's goal is being maximally fuelled, NOT weight loss. Targets are framed
# as loose ranges, not hard numbers.
ATHLETE_WEIGHT_KG = float(coaching_contract.ATHLETE_CONSTANTS["body_mass_fallback"]["value"])
NUTRITION_PREFS = _get("NUTRITION_PREFS", "No restrictions — suggest anything balanced; "
                       "athlete eats meat + rice/potato staples readily.")

# --- Anthropic ----------------------------------------------------------------
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
COACH_MODEL = _get("COACH_MODEL", "claude-opus-4-8")
# Medium keeps adaptive reasoning for real plan changes without paying the
# default high-effort latency on every ordinary coaching question.
COACH_EFFORT = _get("COACH_EFFORT", "medium").lower()
if COACH_EFFORT not in {"low", "medium", "high", "xhigh", "max"}:
    COACH_EFFORT = "medium"
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
    return coaching_contract.race_phase(today or local_today())
