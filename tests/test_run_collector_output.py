from __future__ import annotations

import json
import unittest

from scripts.run_collector import compact_runtime_summary


class RunCollectorOutputTests(unittest.TestCase):
    def test_writable_summary_is_bounded_and_keeps_alert_codes(self) -> None:
        summary = {
            "planned_jobs": [f"planned-{index}" for index in range(10_000)],
            "executed_jobs": [f"executed-{index}" for index in range(5_000)],
            "api_football_calls": 4,
            "polymarket_calls": 2,
            "selected_fixtures": 30,
            "market_fixture_scope": 20,
            "health": {"severity": "warning", "report_date": "2026-07-21"},
            "prediction_publication": {
                "status": "uploaded",
                "prediction_rows": 20,
                "large_internal_detail": ["x"] * 10_000,
            },
            "operational_watchdog": {
                "overall_status": "warning",
                "should_fail_run": False,
                "alerts": [
                    {"code": "persistent_volume_warning", "summary": "large"}
                ],
                "checks": {"large": ["x"] * 10_000},
            },
        }

        compact = compact_runtime_summary(summary, dry_run=False)
        encoded = json.dumps(compact)

        self.assertEqual(compact["planned_job_count"], 10_000)
        self.assertEqual(compact["executed_job_count"], 5_000)
        self.assertEqual(
            compact["operational_watchdog"]["alert_codes"],
            ["persistent_volume_warning"],
        )
        self.assertNotIn("large_internal_detail", encoded)
        self.assertNotIn("checks", compact["operational_watchdog"])
        self.assertLess(len(encoded), 2_000)

    def test_dry_run_retains_full_plan_for_human_review(self) -> None:
        summary = {"planned_jobs": ["one", "two"]}

        self.assertIs(compact_runtime_summary(summary, dry_run=True), summary)


if __name__ == "__main__":
    unittest.main()
