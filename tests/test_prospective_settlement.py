from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

import duckdb

from soccer_bot.modeling.score_grid_shadow import (
    load_score_grid_prospective_gate,
    load_score_grid_shadow_model,
    predict_coherent_score_grid,
    score_grid_shadow_sha256,
)
from soccer_bot.prospective_evidence import materialize_snapshot_evidence
from soccer_bot.prospective_settlement import (
    ProspectiveSettlementError,
    update_prospective_settlement_ledger,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "data/models/regulation_score_grid_v3_shadow/model.json"
GATE_PATH = ROOT / "config/models/regulation_score_grid_v3_prospective_gate.json"
CONFIG_PATH = ROOT / "config/models/regulation_score_grid_v3_settlement.json"


class ProspectiveSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp = Path(self.tempdir.name)
        self.warehouse = self.temp / "warehouse.duckdb"
        self.shadow_output = self.temp / "shadow"
        self.output = self.temp / "settlement"
        self.fixture_id = "future-fixture"
        self.kickoff = datetime(2026, 7, 18, 18, tzinfo=timezone.utc)
        self.snapshot_as_of = self.kickoff - timedelta(hours=23, minutes=59)
        self.snapshot_created_at = self.snapshot_as_of + timedelta(seconds=10)
        self.result_retrieved_at = self.kickoff + timedelta(hours=3)
        self.model = load_score_grid_shadow_model(MODEL_PATH)
        self.gate = load_score_grid_prospective_gate(GATE_PATH, model=self.model)
        self.parent = {"home_win": 0.47, "draw": 0.28, "away_win": 0.25}
        self._create_warehouse()
        self._write_evidence()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _create_warehouse(self) -> None:
        connection = duckdb.connect(str(self.warehouse))
        try:
            connection.execute(
                """
                CREATE TABLE fixture (
                    fixture_id VARCHAR PRIMARY KEY,
                    competition_id VARCHAR NOT NULL,
                    season_id VARCHAR NOT NULL,
                    scheduled_kickoff TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE fixture_model_eligibility (
                    fixture_id VARCHAR PRIMARY KEY,
                    eligible_result_models BOOLEAN NOT NULL,
                    reason_codes JSON NOT NULL
                );
                CREATE TABLE fixture_result_observation (
                    observation_id VARCHAR PRIMARY KEY,
                    fixture_id VARCHAR NOT NULL,
                    source_code VARCHAR NOT NULL,
                    raw_artifact_id VARCHAR,
                    retrieved_at TIMESTAMPTZ NOT NULL,
                    home_score_regulation INTEGER,
                    away_score_regulation INTEGER,
                    result_status VARCHAR NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO fixture VALUES (?, 'competition', 'season', ?)",
                [self.fixture_id, self.kickoff],
            )
            connection.execute(
                "INSERT INTO fixture_model_eligibility VALUES (?, TRUE, '[]')",
                [self.fixture_id],
            )
        finally:
            connection.close()

    def _write_evidence(self) -> None:
        grid = predict_coherent_score_grid(
            expected_home_goals=1.63,
            expected_away_goals=1.08,
            parent_moneyline=self.parent,
            information_state="pre_lineup_24h_v1",
            model=self.model,
        )
        probabilities = grid.probabilities
        prediction = {
            "fixture_id": self.fixture_id,
            "information_state": "pre_lineup_24h_v1",
            "prediction_at": (self.kickoff - timedelta(hours=24)).isoformat(),
            "kickoff": self.kickoff.isoformat(),
            "expected_home_goals": 1.63,
            "expected_away_goals": 1.08,
            "parent_moneyline": self.parent,
            "implied_moneyline": grid.moneyline(),
            "score_grid": [
                {
                    "home_goals": score[0],
                    "away_goals": score[1],
                    "probability": probability,
                }
                for score, probability in sorted(probabilities.items())
            ],
            "score_grid_sha256": self._grid_sha256(probabilities),
        }
        snapshot = {
            "snapshot_version": "regulation_score_grid_v3_shadow_snapshot_v1",
            "created_at": self.snapshot_created_at.isoformat(),
            "as_of": self.snapshot_as_of.isoformat(),
            "model_version": self.model.model_version,
            "parent_model_version": self.model.parent_moneyline_model_version,
            "logical_model_sha256": score_grid_shadow_sha256(self.model),
            "prospective_gate_version": self.gate["gate_version"],
            "prospective_holdout_start": self.model.prospective_holdout_start.isoformat(),
            "sources": {
                "parent_snapshot": {"path": "immutable-parent.json", "sha256": "a" * 64},
                "shadow_model": {"path": str(MODEL_PATH), "sha256": "b" * 64},
                "prospective_gate": {"path": str(GATE_PATH), "sha256": "c" * 64},
            },
            "predictions": [prediction],
        }
        materialize_snapshot_evidence(
            output_directory=self.shadow_output,
            snapshot=snapshot,
        )

    @staticmethod
    def _grid_sha256(probabilities: dict[tuple[int, int], float]) -> str:
        body = json.dumps(
            [
                [score[0], score[1], probability]
                for score, probability in sorted(probabilities.items())
            ],
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _insert_result(
        self,
        *,
        home_goals: int = 2,
        away_goals: int = 1,
        observation_id: str = "result-1",
        source_code: str = "api_football",
        retrieved_at: datetime | None = None,
        status: str = "final",
    ) -> None:
        connection = duckdb.connect(str(self.warehouse))
        try:
            connection.execute(
                "INSERT INTO fixture_result_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    observation_id,
                    self.fixture_id,
                    source_code,
                    f"raw-{observation_id}",
                    retrieved_at or self.result_retrieved_at,
                    home_goals,
                    away_goals,
                    status,
                ],
            )
        finally:
            connection.close()

    def _settle(
        self,
        *,
        settled_at: datetime | None = None,
        config_path: Path = CONFIG_PATH,
    ) -> dict[str, object]:
        return update_prospective_settlement_ledger(
            root=ROOT,
            warehouse_path=self.warehouse,
            evidence_directory=self.shadow_output / "evidence",
            model_path=MODEL_PATH,
            gate_path=GATE_PATH,
            settlement_config_path=config_path,
            output_directory=self.output,
            settled_at=settled_at or self.kickoff + timedelta(hours=4),
        )

    def _ledger(self) -> list[dict]:
        path = self.output / "ledger.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_settlement_records_provenance_metrics_and_contract_distributions(self) -> None:
        self._insert_result()
        result = self._settle()
        record = self._ledger()[0]

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["records_added"], 1)
        self.assertEqual(result["ledger_records"], 1)
        self.assertTrue(record["eligible_for_prospective_gate"])
        self.assertTrue(all(record["integrity_checks"].values()))
        self.assertEqual(
            record["realized_regulation_score"],
            {
                "home_goals": 2,
                "away_goals": 1,
                "result": "home_win",
                "total_goals": 3,
                "goal_difference": 1,
                "both_teams_to_score": True,
            },
        )
        candidate = record["metrics"]["candidate"]
        baseline = record["metrics"]["baseline"]
        self.assertTrue(math.isfinite(candidate["exact_score_log_loss"]))
        self.assertAlmostEqual(
            candidate["exact_score_log_loss"],
            -math.log(candidate["exact_score_probability"]),
            places=12,
        )
        self.assertLessEqual(
            candidate["maximum_absolute_parent_moneyline_difference"], 1e-10
        )
        self.assertLessEqual(
            baseline["maximum_absolute_parent_moneyline_difference"], 1e-10
        )
        total = record["reference_contract_settlements"]["candidate"][
            "total_goals"
        ]["2.5"]["over"]
        self.assertAlmostEqual(sum(total["forecast"].values()), 1.0, places=12)
        self.assertEqual(total["realized_outcome"], "win")
        handicap = record["reference_contract_settlements"]["candidate"][
            "goal_handicap"
        ]["-0.25"]["home"]
        self.assertEqual(handicap["realized_outcome"], "win")
        self.assertEqual(record["previous_record_sha256"], None)
        self.assertEqual(len(record["record_sha256"]), 64)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertFalse(manifest["performance_aggregates_written"])
        self.assertFalse(manifest["gate_decision_written"])
        self.assertNotIn("mean", json.dumps(manifest).lower())

    def test_second_run_and_later_warehouse_correction_never_rewrite_record(self) -> None:
        self._insert_result()
        self._settle()
        original = (self.output / "ledger.jsonl").read_bytes()

        connection = duckdb.connect(str(self.warehouse))
        try:
            connection.execute(
                "UPDATE fixture_result_observation SET home_score_regulation=9 WHERE observation_id='result-1'"
            )
        finally:
            connection.close()
        result = self._settle(settled_at=self.kickoff + timedelta(days=1))

        self.assertEqual(result["status"], "no_new_settlements")
        self.assertEqual(result["records_added"], 0)
        self.assertEqual((self.output / "ledger.jsonl").read_bytes(), original)

    def test_tampered_ledger_hash_chain_fails_closed(self) -> None:
        self._insert_result()
        self._settle()
        path = self.output / "ledger.jsonl"
        record = json.loads(path.read_text())
        record["realized_regulation_score"]["home_goals"] = 99
        path.write_text(json.dumps(record) + "\n")

        with self.assertRaisesRegex(ProspectiveSettlementError, "record hash"):
            self._settle()

    def test_unreviewed_conflicting_final_scores_fail_closed(self) -> None:
        self._insert_result()
        self._insert_result(
            home_goals=1,
            away_goals=1,
            observation_id="result-2",
            source_code="other_provider",
        )

        with self.assertRaisesRegex(ProspectiveSettlementError, "conflicting final"):
            self._settle()
        self.assertFalse((self.output / "ledger.jsonl").exists())

    def test_ineligible_fixture_is_not_added(self) -> None:
        self._insert_result()
        connection = duckdb.connect(str(self.warehouse))
        try:
            connection.execute(
                "UPDATE fixture_model_eligibility SET eligible_result_models=FALSE, reason_codes='[\"administrative_result\"]'"
            )
        finally:
            connection.close()

        result = self._settle()

        self.assertEqual(result["records_added"], 0)
        self.assertEqual(result["ineligible_results"], 1)
        self.assertFalse((self.output / "ledger.jsonl").exists())

    def test_nonfinal_observation_remains_pending(self) -> None:
        self._insert_result(status="scheduled")

        result = self._settle()

        self.assertEqual(result["pending_forecasts"], 1)
        self.assertEqual(result["records_added"], 0)

    def test_temporal_integrity_violation_is_recorded_but_gate_ineligible(self) -> None:
        self._insert_result(retrieved_at=self.snapshot_created_at - timedelta(seconds=1))

        self._settle()
        record = self._ledger()[0]

        self.assertFalse(
            record["integrity_checks"]["result_retrieved_after_forecast_creation"]
        )
        self.assertFalse(record["integrity_checks"]["result_retrieved_after_kickoff"])
        self.assertFalse(record["eligible_for_prospective_gate"])

    def test_settlement_timestamp_before_result_retrieval_is_gate_ineligible(self) -> None:
        self._insert_result()

        self._settle(settled_at=self.result_retrieved_at - timedelta(seconds=1))
        record = self._ledger()[0]

        self.assertFalse(
            record["integrity_checks"][
                "settlement_run_at_or_after_all_result_retrievals"
            ]
        )
        self.assertFalse(record["eligible_for_prospective_gate"])

    def test_duplicate_pairing_evidence_fails_before_writing_ledger(self) -> None:
        self._insert_result()
        evidence_path = next((self.shadow_output / "evidence").glob("*.json"))
        duplicate_path = evidence_path.with_name("f" * 64 + ".json")
        duplicate_path.write_bytes(evidence_path.read_bytes())

        with self.assertRaisesRegex(ProspectiveSettlementError, "multiple forecast"):
            self._settle()
        self.assertFalse((self.output / "ledger.jsonl").exists())

    def test_frozen_artifact_hash_mismatch_fails_closed(self) -> None:
        config = json.loads(CONFIG_PATH.read_text())
        config["frozen_artifact_sha256"]["contract_registry"] = "0" * 64
        changed = self.temp / "changed-settlement.json"
        changed.write_text(json.dumps(config))

        with self.assertRaisesRegex(ProspectiveSettlementError, "contract_registry"):
            self._settle(config_path=changed)

    def test_kickoff_revision_is_recorded_but_gate_ineligible(self) -> None:
        self._insert_result()
        connection = duckdb.connect(str(self.warehouse))
        try:
            connection.execute(
                "UPDATE fixture SET scheduled_kickoff=? WHERE fixture_id=?",
                [self.kickoff + timedelta(hours=2), self.fixture_id],
            )
        finally:
            connection.close()

        self._settle()
        record = self._ledger()[0]

        self.assertFalse(
            record["integrity_checks"]["current_kickoff_matches_prediction"]
        )
        self.assertFalse(record["eligible_for_prospective_gate"])


if __name__ == "__main__":
    unittest.main()
