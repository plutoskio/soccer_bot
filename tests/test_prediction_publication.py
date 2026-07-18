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
    def __init__(
        self,
        snapshot: dict,
        *,
        generation_exit: int = 0,
        upload_exit: int = 0,
        shadow_exit: int = 0,
        settlement_exit: int = 0,
        readiness_exit: int = 0,
    ):
        self.snapshot = snapshot
        self.generation_exit = generation_exit
        self.upload_exit = upload_exit
        self.shadow_exit = shadow_exit
        self.settlement_exit = settlement_exit
        self.readiness_exit = readiness_exit
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
        if command[1].endswith("predict_score_grid_v3_shadow.py"):
            if not self.shadow_exit:
                output = Path(command[command.index("--output-dir") + 1])
                output.mkdir(parents=True, exist_ok=True)
                as_of = datetime.fromisoformat(self.snapshot["as_of"])
                rows = [
                    {
                        "fixture_id": row["fixture_id"],
                        "information_state": row["information_state"],
                        "kickoff": row["kickoff"],
                        "parent_moneyline": {
                            "home_win": 0.4,
                            "draw": 0.3,
                            "away_win": 0.3,
                        },
                        "implied_moneyline": {
                            "home_win": 0.4,
                            "draw": 0.3,
                            "away_win": 0.3,
                        },
                        "score_grid": [
                            {"home_goals": 1, "away_goals": 0, "probability": 0.4},
                            {"home_goals": 0, "away_goals": 0, "probability": 0.3},
                            {"home_goals": 0, "away_goals": 1, "probability": 0.3},
                        ],
                    }
                    for row in self.snapshot["predictions"]
                ]
                shadow = {
                    "snapshot_version": "regulation_score_grid_v3_shadow_snapshot_v1",
                    "created_at": (as_of + timedelta(minutes=1)).isoformat(),
                    "as_of": self.snapshot["as_of"],
                    "model_version": "regulation_score_grid_v3_prospective_shadow",
                    "parent_model_version": "regulation_champion_v1",
                    "logical_model_sha256": (
                        "d17aa0334ad85914a396089430ad588ef8ca9381227de044106c1c777cbe00c7"
                    ),
                    "prospective_gate_version": (
                        "regulation_score_grid_v3_prospective_gate_v1"
                    ),
                    "predictions": rows,
                }
                (output / "latest.json").write_text(
                    json.dumps(shadow), encoding="utf-8"
                )
            return subprocess.CompletedProcess(
                command, self.shadow_exit, stdout="{}", stderr="shadow secret"
            )
        if command[1].endswith("settle_score_grid_v3_prospective.py"):
            return subprocess.CompletedProcess(
                command,
                self.settlement_exit,
                stdout=json.dumps(
                    {
                        "status": "no_new_settlements",
                        "records_added": 0,
                        "ledger_records": 0,
                        "pending_forecasts": len(self.snapshot["predictions"]),
                        "ineligible_results": 0,
                        "reviewed_exclusions": 0,
                        "ledger_head_sha256": None,
                        "performance_aggregates_written": False,
                        "gate_decision_written": False,
                    }
                ),
                stderr="settlement secret",
            )
        if command[1].endswith("check_score_grid_v3_evaluation_readiness.py"):
            return subprocess.CompletedProcess(
                command,
                self.readiness_exit,
                stdout=json.dumps(
                    {
                        "readiness_version": "regulation_score_grid_v3_evaluation_readiness_v1",
                        "status": "locked_insufficient_evidence",
                        "evaluation_config_sha256": (
                            "5d0926eea8c670a1d6815bf33f0542aabe4b3a97a8ce18aa2287ca832810adb5"
                        ),
                        "ledger_records": 0,
                        "horizons": {
                            horizon: {
                                "eligible_settled_fixtures": 0,
                                "nonempty_mature_calendar_month_blocks": 0,
                                "competitions": 0,
                            }
                            for horizon in (
                                "pre_lineup_24h_v1",
                                "pre_lineup_72h_clean_v1",
                            )
                        },
                        "performance_statistics_exposed": False,
                        "automatic_decision_execution": False,
                        "explicit_one_shot_command_required": True,
                    }
                ),
                stderr="readiness secret",
            )
        if command[1].endswith("capture_polymarket_market_evidence.py"):
            policy_hash = command[command.index("--expected-policy-sha256") + 1]
            rows = len(self.snapshot["predictions"])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "status": "no_new_evidence",
                        "policy_version": "polymarket_regulation_market_evidence_v1",
                        "mapping_version": "polymarket_regulation_contract_mapping_v1",
                        "policy_sha256": policy_hash,
                        "prediction_rows": rows,
                        "new_evidence_records": 0,
                        "existing_evidence_records": 0,
                        "evidence_records": 0,
                        "economically_executable_records": 0,
                        "horizons": {
                            "pre_lineup_24h_v1": {
                                "prediction_rows": rows,
                                "complete_moneyline_mappings": 0,
                                "pre_cutoff_complete_books": 0,
                                "valid_bid_ask_books": 0,
                                "evidence_records": 0,
                                "economically_executable_records": 0,
                            }
                        },
                        "exclusion_counts": {},
                        "outcome_or_performance_fields_written": False,
                        "orders_or_trading_actions_performed": False,
                    }
                ),
                stderr="market secret",
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
        shadow_model = (
            self.root
            / "artifacts"
            / "production"
            / "regulation_score_grid_v3_shadow"
        )
        shadow_model.mkdir(parents=True)
        shadow_model.joinpath("model.json").write_text(
            (
                ROOT
                / "artifacts"
                / "production"
                / "regulation_score_grid_v3_shadow"
                / "model.json"
            ).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        gate = self.root / "config" / "models"
        gate.mkdir(parents=True)
        gate.joinpath("regulation_score_grid_v3_prospective_gate.json").write_text(
            (
                ROOT
                / "config"
                / "models"
                / "regulation_score_grid_v3_prospective_gate.json"
            ).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.as_of = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
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
                "shadow_score_grid": {
                    "enabled": True,
                    "model_version": "regulation_score_grid_v3_prospective_shadow",
                    "logical_model_sha256": (
                        "d17aa0334ad85914a396089430ad588ef8ca9381227de044106c1c777cbe00c7"
                    ),
                    "model_path": (
                        "artifacts/production/regulation_score_grid_v3_shadow/model.json"
                    ),
                    "prospective_gate_path": (
                        "config/models/regulation_score_grid_v3_prospective_gate.json"
                    ),
                    "output_directory": (
                        "data/predictions/regulation_score_grid_v3_shadow"
                    ),
                    "minimum_prediction_rows": 1,
                    "settlement_ledger": {
                        "enabled": True,
                        "config_path": (
                            "config/models/regulation_score_grid_v3_settlement.json"
                        ),
                        "output_directory": (
                            "data/predictions/regulation_score_grid_v3_settlement"
                        ),
                        "timeout_seconds": 30,
                        "evaluation_program": {
                            "enabled": True,
                            "config_path": (
                                "config/models/regulation_score_grid_v3_evaluation.json"
                            ),
                            "evaluation_config_sha256": (
                                "5d0926eea8c670a1d6815bf33f0542aabe4b3a97a8ce18aa2287ca832810adb5"
                            ),
                            "output_directory": (
                                "data/predictions/regulation_score_grid_v3_evaluation"
                            ),
                            "timeout_seconds": 30,
                        },
                    },
                },
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
        self.assertEqual(len(runner.commands), 5)
        self.assertEqual(
            result["shadow_score_grid"]["status"],
            "written_to_persistent_shadow_store",
        )
        self.assertEqual(
            result["prospective_settlement_ledger"]["status"],
            "no_new_settlements",
        )
        self.assertEqual(
            result["prospective_evaluation_readiness"]["status"],
            "locked_insufficient_evidence",
        )
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

    def test_shadow_failure_is_isolated_after_parent_upload(self) -> None:
        runner = FakeRunner(self.snapshot(), shadow_exit=29)
        result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["shadow_score_grid"]["status"], "failed")
        self.assertEqual(
            result["shadow_score_grid"]["error"], "shadow_generation_exit_29"
        )
        self.assertEqual(len(runner.commands), 3)
        self.assertNotIn("shadow secret", json.dumps(result))

    def test_settlement_failure_is_isolated_after_parent_and_shadow(self) -> None:
        runner = FakeRunner(self.snapshot(), settlement_exit=31)
        result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(
            result["shadow_score_grid"]["status"],
            "written_to_persistent_shadow_store",
        )
        self.assertEqual(result["prospective_settlement_ledger"]["status"], "failed")
        self.assertEqual(
            result["prospective_settlement_ledger"]["error"],
            "prospective_settlement_exit_31",
        )
        self.assertEqual(len(runner.commands), 4)
        self.assertNotIn("settlement secret", json.dumps(result))

    def test_readiness_failure_is_isolated_and_sanitized(self) -> None:
        runner = FakeRunner(self.snapshot(), readiness_exit=37)
        result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(
            result["prospective_evaluation_readiness"]["status"], "failed"
        )
        self.assertEqual(
            result["prospective_evaluation_readiness"]["error"],
            "prospective_readiness_exit_37",
        )
        self.assertEqual(len(runner.commands), 5)
        self.assertNotIn("readiness secret", json.dumps(result))

    def test_report_write_failure_does_not_fail_collection_or_publication(self) -> None:
        runner = FakeRunner(self.snapshot())
        with patch(
            "soccer_bot.prediction_publication._write_report",
            side_effect=OSError("disk full"),
        ):
            result = self.publish(runner)

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["report_status"], "failed")

    def test_polymarket_evidence_is_failure_isolated_and_receipt_validated(self) -> None:
        self.config["prediction_publication"]["polymarket_market_evidence"] = {
            "enabled": True,
            "policy_path": "config/contracts/polymarket_regulation_v1.json",
            "policy_sha256": "f" * 64,
            "output_directory": "data/predictions/polymarket_market_evidence_v1",
            "timeout_seconds": 30,
        }
        runner = FakeRunner(self.snapshot())
        result = self.publish(runner)

        self.assertEqual("uploaded", result["status"])
        self.assertEqual(
            "no_new_evidence", result["polymarket_market_evidence"]["status"]
        )
        self.assertEqual(0, result["polymarket_market_evidence"]["evidence_records"])
        self.assertEqual(6, len(runner.commands))


if __name__ == "__main__":
    unittest.main()
