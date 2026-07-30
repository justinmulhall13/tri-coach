"""Stage-2 console proof: pull real Garmin + Calendar data and print it.

Run from coach/:  ../.venv/bin/python pull.py
This is the checkpoint script — confirms both connections before we build
the plan, coach agent, and frontend.
"""
from __future__ import annotations

import json

from app import calendar_source, config, garmin_source


def section(title: str) -> None:
    print("\n" + "=" * 64 + f"\n{title}\n" + "=" * 64)


def main() -> None:
    section("RACE / PHASE")
    print(json.dumps(config.race_phase(), indent=2))

    section("GARMIN — READINESS (this morning)")
    try:
        print(json.dumps(garmin_source.get_readiness(), indent=2, default=str))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

    section("GARMIN — FITNESS MARKERS (incl. cycling VO2max)")
    try:
        print(json.dumps(garmin_source.get_fitness_markers(), indent=2, default=str))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

    section("GARMIN — RECENT LOAD (14d by sport)")
    try:
        load = garmin_source.get_recent_load(14)
        print(json.dumps({"count": load["count"], "by_sport": load["by_sport"]}, indent=2, default=str))
        print("\nMost recent activities:")
        for a in load["activities"][:8]:
            print(f"  {a['date']}  {a['sport']:8} {a['name']!r:35} {a['km']}km {a['minutes']}min hr={a['hr_avg']}")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

    section("GOOGLE CALENDAR — next 7 days")
    cal = calendar_source.get_events(days=7)
    if not cal.get("available"):
        print(f"NOT AVAILABLE — {cal.get('reason')}: {cal.get('detail')}")
    else:
        for e in cal["events"]:
            print(f"  {e['start']}  {e['summary']}")


if __name__ == "__main__":
    main()
