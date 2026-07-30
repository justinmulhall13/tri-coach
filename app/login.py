"""One-time interactive Garmin login. Caches an OAuth token in ~/.garminconnect/.

Run from the coach/ dir:  ../.venv/bin/python -m app.login
Handles MFA via prompt. After this, the backend resumes from the token.
"""
from __future__ import annotations

import os
import sys

from garminconnect import Garmin

from . import config


def main() -> None:
    email = config._get("GARMIN_EMAIL")
    password = config._get("GARMIN_PASSWORD")
    if not email or not password:
        print("Set GARMIN_EMAIL and GARMIN_PASSWORD in coach/.env first.", file=sys.stderr)
        sys.exit(1)
    store = os.path.normpath(os.path.expanduser("~/.garminconnect"))
    print("Garmin login…", file=sys.stderr)
    g = Garmin(email=email, password=password, prompt_mfa=lambda: input("MFA code: "))
    g.login(store)
    print(f"OK. Token cached in {store}", file=sys.stderr)


if __name__ == "__main__":
    main()
