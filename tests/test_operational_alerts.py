from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.operational_alerts import (
    OperationalAlertError,
    run_operational_watchdog,
)


DiskUsage = namedtuple("DiskUsage", "total used free")
CHAMPION_HASH = "8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a"
SHADOW_HASH = "d17aa0334ad85914a396089430ad588ef8ca9381227de044106c1c777cbe00c7"


class OperationalAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "data").mkdir()
        self.now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        self.config = {
            "operations": {
                "enabled": True,
                "report_directory": "data/reports/operations",
                "publication_stale_after_seconds": 1200,
                "cycle_stale_after_seconds": 1200,
                "volume_warning_percent": 80,
                "volume_critical_percent": 95,
                "fail_run_on_critical": True,
            },
            "prediction_publication": {
                "model_version": "regulation_champion_v1",
                "logical_model_sha256": CHAMPION_HASH,
                "minimum_prediction_rows": 1,
                "report_directory": "data/reports/predictions",
                "shadow_score_grid": {
                    "enabled": True,
                    "model_version": "regulation_score_grid_v3_prospective_shadow",
                    "logical_model_sha256": SHADOW_HASH,
                    "minimum_prediction_rows": 1,
                },
            },
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def result(self, **overrides) -> dict:
        value = {
            "status": "uploaded",
            "as_of": self.now.isoformat(),
            "model_version": "regulation_champion_v1",
            "logical_model_sha256": CHAMPION_HASH,
            "prediction_rows": 25,
            "shadow_score_grid": {
                "status": "written_to_persistent_shadow_store",
                "model_version": "regulation_score_grid_v3_prospective_shadow",
                "logical_model_sha256": SHADOW_HASH,
                "prediction_rows": 25,
            },
        }
        value.update(overrides)
        return value

    def watchdog(self, result: dict | None = None, *, used_percent: float = 50) -> dict:
        total = 10_000
        used = round(total * used_percent / 100)
        return run_operational_watchdog(
            root=self.root,
            collector_config=self.config,
            publication_result=result or self.result(),
            now=self.now,
            disk_usage=lambda _path: DiskUsage(total, used, total - used),
        )

    def codes(self, status: dict) -> set[str]:
        return {str(alert["code"]) for alert in status["alerts"]}

    def write_receipt(self, value: dict) -> None:
        path = self.root / "data" / "reports" / "predictions"
        path.mkdir(parents=True, exist_ok=True)
        path.joinpath("publication.jsonl").write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )

    def test_healthy_cycle_has_no_alert_and_writes_atomic_status(self) -> None:
        status = self.watchdog()

        self.assertEqual(status["overall_status"], "ok")
        self.assertFalse(status["should_fail_run"])
        self.assertEqual(status["alerts"], [])
        stored = json.loads(
            (
                self.root / "data" / "reports" / "operations" / "current.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stored, status)
        self.assertFalse(
            (self.root / "data" / "reports" / "operations" / "current.json.tmp").exists()
        )

    def test_publication_failure_is_immediate_even_if_prior_success_is_fresh(self) -> None:
        self.write_receipt(
            {
                "status": "uploaded",
                "as_of": (self.now - timedelta(minutes=5)).isoformat(),
            }
        )
        status = self.watchdog(
            {
                "status": "failed",
                "as_of": self.now.isoformat(),
                "error": "prediction_generation_exit_17",
            }
        )

        self.assertIn("champion_publication_failed", self.codes(status))
        self.assertNotIn("champion_publication_stale", self.codes(status))
        self.assertIn("shadow_score_grid_failed", self.codes(status))
        self.assertTrue(status["should_fail_run"])

    def test_stale_prior_success_is_a_separate_alert(self) -> None:
        self.write_receipt(
            {
                "status": "uploaded",
                "as_of": (self.now - timedelta(minutes=21)).isoformat(),
            }
        )
        status = self.watchdog({"status": "failed", "error": "upload_failed"})

        self.assertIn("champion_publication_failed", self.codes(status))
        self.assertIn("champion_publication_stale", self.codes(status))

    def test_champion_and_shadow_identity_mismatches_fail_closed(self) -> None:
        result = self.result(logical_model_sha256="0" * 64)
        result["shadow_score_grid"] = {
            **result["shadow_score_grid"],
            "logical_model_sha256": "1" * 64,
        }
        status = self.watchdog(result)

        self.assertIn("champion_model_identity_mismatch", self.codes(status))
        self.assertIn("shadow_model_identity_mismatch", self.codes(status))

    def test_shadow_failure_and_row_mismatch_are_critical(self) -> None:
        failed = self.result()
        failed["shadow_score_grid"] = {
            "status": "failed",
            "error": "shadow_generation_exit_29",
        }
        self.assertIn("shadow_score_grid_failed", self.codes(self.watchdog(failed)))

        mismatched = self.result()
        mismatched["shadow_score_grid"] = {
            **mismatched["shadow_score_grid"],
            "prediction_rows": 24,
        }
        self.assertIn(
            "shadow_parent_row_count_mismatch", self.codes(self.watchdog(mismatched))
        )

    def test_zero_rows_are_never_treated_as_healthy(self) -> None:
        result = self.result(prediction_rows=0)
        result["shadow_score_grid"] = {
            **result["shadow_score_grid"],
            "prediction_rows": 0,
        }
        status = self.watchdog(result)

        self.assertIn("champion_prediction_rows_below_minimum", self.codes(status))
        self.assertIn("shadow_prediction_rows_below_minimum", self.codes(status))

        failed = self.watchdog(
            {
                "status": "failed",
                "as_of": self.now.isoformat(),
                "error": "snapshot_below_minimum_prediction_rows",
            }
        )
        self.assertIn("champion_prediction_rows_below_minimum", self.codes(failed))

    def test_receipt_write_failure_is_critical(self) -> None:
        status = self.watchdog(self.result(report_status="failed"))

        self.assertIn("publication_receipt_write_failed", self.codes(status))
        self.assertTrue(status["should_fail_run"])

    def test_volume_thresholds_have_warning_and_critical_levels(self) -> None:
        warning = self.watchdog(used_percent=80)
        self.assertEqual(warning["overall_status"], "warning")
        self.assertFalse(warning["should_fail_run"])
        self.assertIn("persistent_volume_warning", self.codes(warning))

        critical = self.watchdog(used_percent=95)
        self.assertEqual(critical["overall_status"], "critical")
        self.assertTrue(critical["should_fail_run"])
        self.assertIn("persistent_volume_critical", self.codes(critical))

    def test_transition_log_deduplicates_and_records_recovery(self) -> None:
        self.watchdog(used_percent=80)
        self.watchdog(used_percent=80)
        self.watchdog(used_percent=50)
        events = [
            json.loads(line)
            for line in (
                self.root / "data" / "reports" / "operations" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]

        self.assertEqual([event["event"] for event in events], ["opened", "resolved"])
        self.assertTrue(
            all(event["code"] == "persistent_volume_warning" for event in events)
        )

    def test_unreadable_previous_status_fails_closed(self) -> None:
        path = self.root / "data" / "reports" / "operations"
        path.mkdir(parents=True)
        path.joinpath("current.json").write_text("not-json", encoding="utf-8")

        with self.assertRaises(OperationalAlertError):
            self.watchdog()


if __name__ == "__main__":
    unittest.main()
