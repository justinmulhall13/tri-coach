from __future__ import annotations

import ast
import datetime
import pathlib
import unittest
from unittest.mock import patch

from app import coaching_contract, plan


def _stored_day(date: str, source: str, *, profile: str | None = None) -> dict:
    return {
        "date": date,
        "event_profile_id": profile or coaching_contract.event_profile_id(),
        "week_index": 9,
        "phase": "preserved-phase",
        "discipline": "bike",
        "title": "Legacy watts",
        "structure": {"main": "3 x 3 min at 280 W"},
        "duration_min": 45,
        "intensity": "threshold",
        "tsb_target": 10,
        "why": "legacy",
        "is_rest": False,
        "source": source,
        "start_time": "06:30",
        "gcal_event_id": f"calendar-{date}",
        "pos_updated_at": "2026-08-10T12:00:00",
    }


class SeededPlanReconciliationTests(unittest.TestCase):
    def test_refreshes_only_current_profile_seed_rows_and_preserves_calendar_metadata(self) -> None:
        seed_bike = _stored_day("2026-08-11", "seed")
        edited = _stored_day("2026-08-12", "edited")
        coach = _stored_day("2026-08-13", "coach")
        foreign = _stored_day("2026-08-14", "seed", profile="another-event")
        seed_brick = _stored_day("2026-08-15", "seed")
        seed_race = _stored_day("2026-08-16", "seed")

        with (
            patch.object(plan.db, "get_plan", return_value=[
                seed_bike, edited, coach, foreign, seed_brick, seed_race,
            ]) as get_plan,
            patch.object(plan.db, "upsert_plan_day") as upsert,
            patch("app.zones.hr_range", return_value=(140, 150)),
        ):
            result = plan.reconcile_seeded_plan(datetime.date(2026, 8, 11))

        get_plan.assert_called_once_with("2026-08-11", "2026-08-16")
        self.assertEqual(result["reconciled"], 3)
        self.assertEqual(result["skipped_modified"], 2)
        self.assertEqual(result["skipped_profile"], 1)
        self.assertEqual(upsert.call_count, 3)

        refreshed_by_date = {call.args[0]["date"]: call.args[0] for call in upsert.call_args_list}
        self.assertEqual(set(refreshed_by_date), {"2026-08-11", "2026-08-15", "2026-08-16"})
        for original in (seed_bike, seed_brick, seed_race):
            refreshed = refreshed_by_date[original["date"]]
            for field in ("date", "week_index", "phase", "start_time",
                          "gcal_event_id", "pos_updated_at"):
                self.assertEqual(refreshed[field], original[field])
            self.assertEqual(refreshed["source"], "seed")

        migrated_text = str(refreshed_by_date["2026-08-11"]["structure"])
        self.assertIn("HR 140-150", migrated_text)
        self.assertNotIn(" W", migrated_text)
        self.assertEqual(refreshed_by_date["2026-08-16"]["duration_min"], 345)

    def test_after_race_reconciliation_is_a_noop(self) -> None:
        with (
            patch.object(plan.db, "get_plan") as get_plan,
            patch.object(plan.db, "upsert_plan_day") as upsert,
        ):
            result = plan.reconcile_seeded_plan(datetime.date(2026, 8, 17))
        self.assertEqual(result["reconciled"], 0)
        get_plan.assert_not_called()
        upsert.assert_not_called()

    def test_bootstrap_wires_existing_plans_to_full_seed_reconciliation(self) -> None:
        source = pathlib.Path(plan.__file__).with_name("main.py").read_text()
        tree = ast.parse(source)
        bootstrap = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_bootstrap_plan"
        )
        calls = {
            f"{node.func.value.id}.{node.func.attr}"
            for node in ast.walk(bootstrap)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        self.assertIn("plan.reconcile_seeded_plan", calls)
        self.assertNotIn("plan.reconcile_event_day", calls)


if __name__ == "__main__":
    unittest.main()
