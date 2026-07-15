from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from soccer_bot.prediction_publication import run_prediction_publication


ROOT = Path(__file__).resolve().parents[1]
LOGICAL_HASH = "8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a"


class FakeRunner:
    def __init__(self, snapshot: dict, *, generation_exit: int = 0, upload_exit: int = 0):
        self.snapshot = snapshot
        self.generation_exit = generation_exit
        self.upload_exit = upload_exit
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if command[1].endswith("predict_upcoming_regulation.py"):
            if not self.generation_exit:
                output = Path(command[command.index("--output-dir") + 1])
                output.mkdir(parents=True, exist_ok=True)
                (output / "latest.json").write_text(
                    json.dumps(self.snapshot), encoding="utf-8"
                )
            return subprocess.CompletedProcess(
                command, self.generation_exit, stdout="{}", stderr="provider secret"
            )
        return subprocess.CompletedProcess(
            command,
            self.upload_exit,
            stdout=json.dumps({"status": "uploaded"}),
            stderr="storage secret",
        )


class PredictionPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        model = self.root / "artifacts" / "production" / "regulation_champion_v1"
        model.mkdir(parents=True)
        model.joinpath("model.json").write_text(
            (ROOT / "artifacts" / "production" / "regulation_champion_v1" / "model.json")
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.as_of = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        self.config = {
            "prediction_publication": {
                "enabled": True,
                "model_version": "regulation_champion_v1",
                "logical_model_sha256": LOGICAL_HASH,
                "model_path": "artifacts/production/regulation_champion_v1/model.json",
                "output_directory": "data/predictions/regulation_champion_v1",
                "report_directory": "data/reports/predictions",
                "minimum_prediction_rows": 1,
                "timeout_seconds": 30,
            }
        }
        self.environment = {
            "SOCCER_SNAPSHOT_S3_BUCKET": "private-bucket",
            "SOCCER_SNAPSHOT_S3_ENDPOINT": "https://private-endpoint.invalid",
            "AWS_ACCESS_KEY_ID": "private-access-key",
            "AWS_SECRET_ACCESS_KEY": "private-secret-key",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def snapshot(self, *, predictions: int = 1, as_of: datetime | None = None) -> dict:
        effective_as_of = as_of or self.as_of
        rows = [
            {
                "fixture_id": f"fixture-{index}",
                "information_state": "pre_lineup_24h_v1",
                "prediction_at": (effective_as_of - timedelta(hours=1)).isoformat(),
                "kickoff": (effective_as_of + timedelta(hours=5)).isoformat(),
            }
            for index in range(predictions)
        ]
        return {
            "snapshot_version": "upcoming_regulation_moneyline_snapshot_v2",
            "model_version": "regulation_champion_v1",
            "logical_model_sha256": LOGICAL_HASH,
            "prediction_rows_sha256": "a" * 64,
            "as_of": effective_as_of.isoformat(),
            "predictions": rows,
        }

    def publish(self, runner: FakeRunner, *, health: str = "warning") -> dict:
        return run_prediction_publication(
            root=self.root,
            warehouse_path=self.root / "data" / "warehouse" / "soccer.duckdb",
            collector_config=self.config,
            environment=self.environment,
            as_of=self.as_of,
            health_severity=health,
            command_runner=runner,
        )

    def test_valid_candidate_is_published_and_reported_without_secrets(self) -> None:
        runner = FakeRunner(self.snapshot())
        result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["prediction_rows"], 1)
        self.assertEqual(len(runner.commands), 2)
        command_text = " ".join(part for command in runner.commands for part in command)
        for secret in self.environment.values():
            self.assertNotIn(secret, command_text)
        report = (
            self.root / "data" / "reports" / "predictions" / "publication.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn('"status":"uploaded"', report)
        for secret in self.environment.values():
            self.assertNotIn(secret, report)

    def test_blocking_collector_health_never_runs_generation(self) -> None:
        runner = FakeRunner(self.snapshot())
        result = self.publish(runner, health="blocking")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "blocking_collector_health")
        self.assertEqual(runner.commands, [])

    def test_generation_failure_is_isolated_and_never_uploads(self) -> None:
        runner = FakeRunner(self.snapshot(), generation_exit=17)
        result = self.publish(runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "prediction_generation_exit_17")
        self.assertEqual(len(runner.commands), 1)
        self.assertNotIn("provider secret", json.dumps(result))

    def test_empty_candidate_is_rejected_before_upload(self) -> None:
        runner = FakeRunner(self.snapshot(predictions=0))
        result = self.publish(runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "snapshot_below_minimum_prediction_rows")
        self.assertEqual(len(runner.commands), 1)

    def test_mismatched_as_of_is_rejected_before_upload(self) -> None:
        runner = FakeRunner(self.snapshot(as_of=self.as_of - timedelta(minutes=5)))
        result = self.publish(runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "snapshot_as_of_mismatch")
        self.assertEqual(len(runner.commands), 1)

    def test_upload_failure_is_isolated(self) -> None:
        runner = FakeRunner(self.snapshot(), upload_exit=23)
        result = self.publish(runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "snapshot_publication_exit_23")
        self.assertEqual(len(runner.commands), 2)
        self.assertNotIn("storage secret", json.dumps(result))

    def test_report_write_failure_does_not_fail_collection_or_publication(self) -> None:
        runner = FakeRunner(self.snapshot())
        with patch(
            "soccer_bot.prediction_publication._write_report",
            side_effect=OSError("disk full"),
        ):
            result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["report_status"], "failed")


if __name__ == "__main__":
    unittest.main()
