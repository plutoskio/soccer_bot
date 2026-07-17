from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.prospective_evaluation import (
    ProspectiveEvaluationError,
    _assert_count_only,
    _write_once_json,
    build_count_only_readiness,
    evaluate_selected_records,
    paired_month_block_bootstrap,
    run_one_shot_evaluation,
    update_evaluation_readiness,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "artifacts/production/regulation_score_grid_v3_shadow/model.json"
GATE_PATH = ROOT / "config/models/regulation_score_grid_v3_prospective_gate.json"
SETTLEMENT_CONFIG_PATH = ROOT / "config/models/regulation_score_grid_v3_settlement.json"
EVALUATION_CONFIG_PATH = ROOT / "config/models/regulation_score_grid_v3_evaluation.json"
HORIZONS = ("pre_lineup_24h_v1", "pre_lineup_72h_clean_v1")


class ProspectiveEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(EVALUATION_CONFIG_PATH.read_text())
        self.gate = json.loads(GATE_PATH.read_text())
        self.gate["minimum_evidence"] = {
            "complete_calendar_month_blocks": 6,
            "fixtures_per_horizon": 6,
            "minimum_competitions_per_horizon": 5,
        }

    def record(
        self,
        *,
        fixture: str,
        horizon: str,
        month: int,
        competition: int,
        exact_delta: float = -0.1,
        secondary_delta: float = -0.001,
        allowed_delta: float = 0.0005,
        moneyline_difference: float = 1e-12,
        eligible: bool = True,
    ) -> dict:
        baseline = {
            "exact_score_log_loss": 1.0,
            "total_goals_log_loss": 1.0,
            "goal_difference_log_loss": 1.0,
            "home_goals_log_loss": 1.0,
            "away_goals_log_loss": 1.0,
            "both_teams_to_score_log_loss": 1.0,
            "total_goals_rps": 1.0,
            "goal_difference_rps": 1.0,
            "maximum_absolute_parent_moneyline_difference": moneyline_difference,
        }
        candidate = {
            **baseline,
            "exact_score_log_loss": 1.0 + exact_delta,
            "total_goals_log_loss": 1.0 + secondary_delta,
            "goal_difference_log_loss": 1.0 + secondary_delta,
            "home_goals_log_loss": 1.0 + allowed_delta,
            "away_goals_log_loss": 1.0 + allowed_delta,
            "both_teams_to_score_log_loss": 1.0 + allowed_delta,
            "total_goals_rps": 1.0 + allowed_delta,
            "goal_difference_rps": 1.0 + allowed_delta,
        }
        deltas = {
            key: candidate[key] - baseline[key]
            for key in candidate
            if key.endswith(("_log_loss", "_rps"))
        }
        return {
            "fixture_id": fixture,
            "information_state": horizon,
            "competition_id": f"competition-{competition}",
            "kickoff": f"2026-{month:02d}-15T18:00:00+00:00",
            "eligible_for_prospective_gate": eligible,
            "metrics": {
                "candidate": candidate,
                "baseline": baseline,
                "candidate_minus_baseline": deltas,
            },
        }

    def six_month_records(self) -> list[dict]:
        values = []
        for horizon in HORIZONS:
            for offset, month in enumerate(range(8, 13), 1):
                values.append(
                    self.record(
                        fixture=f"{horizon}-{month}",
                        horizon=horizon,
                        month=month,
                        competition=offset,
                    )
                )
            values.append(
                self.record(
                    fixture=f"{horizon}-jan",
                    horizon=horizon,
                    month=1,
                    competition=1,
                )
            )
            values[-1]["kickoff"] = "2027-01-15T18:00:00+00:00"
        return values

    def ledger_record(
        self,
        *,
        fixture: str,
        horizon: str,
        kickoff: str,
        competition: int,
        previous: str | None,
        include_metrics: bool = True,
    ) -> dict:
        month = int(kickoff[5:7])
        record = self.record(
            fixture=fixture,
            horizon=horizon,
            month=month,
            competition=competition,
        )
        record["kickoff"] = kickoff
        record.update(
            {
                "ledger_version": self.config["ledger_version"],
                "evidence_key": f"evidence-{fixture}",
                "model_version": self.config["model_version"],
                "logical_model_sha256": self.config["logical_model_sha256"],
                "prospective_gate_version": self.config[
                    "prospective_gate_version"
                ],
                "settlement_config_sha256": self.file_sha256(
                    SETTLEMENT_CONFIG_PATH
                ),
                "prospective_gate_file_sha256": self.file_sha256(GATE_PATH),
                "shadow_model_artifact_sha256": self.file_sha256(MODEL_PATH),
                "integrity_checks": {"all_source_checks": True},
                "previous_record_sha256": previous,
            }
        )
        if not include_metrics:
            del record["metrics"]
        record["record_sha256"] = self.logical_sha256(record)
        return record

    @staticmethod
    def logical_sha256(value: dict) -> str:
        body = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def file_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write_ledger(path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_readiness_excludes_partial_holdout_month_and_waits_seven_days(self) -> None:
        records = self.six_month_records()
        records.append(
            self.record(
                fixture="partial-july",
                horizon=HORIZONS[0],
                month=7,
                competition=1,
            )
        )
        before_maturity = build_count_only_readiness(
            records,
            as_of=datetime(2027, 2, 7, 23, 59, tzinfo=timezone.utc),
            config=self.config,
            gate=self.gate,
        )
        at_maturity = build_count_only_readiness(
            records,
            as_of=datetime(2027, 2, 8, 0, 0, tzinfo=timezone.utc),
            config=self.config,
            gate=self.gate,
        )

        self.assertEqual(before_maturity["first_full_calendar_month"], "2026-08")
        self.assertEqual(before_maturity["latest_matured_calendar_month"], "2026-12")
        self.assertFalse(before_maturity["all_requirements_met"])
        self.assertEqual(
            at_maturity["deterministic_evaluation_cutoff_month"], "2027-01"
        )
        self.assertTrue(at_maturity["all_requirements_met"])
        self.assertEqual(
            at_maturity["horizons"][HORIZONS[0]]["eligible_settled_fixtures"], 6
        )

    def test_readiness_is_counts_only(self) -> None:
        readiness = build_count_only_readiness(
            self.six_month_records(),
            as_of=datetime(2027, 2, 8, tzinfo=timezone.utc),
            config=self.config,
            gate=self.gate,
        )
        artifact = {
            "performance_statistics_exposed": False,
            **readiness,
        }

        _assert_count_only(artifact)
        body = json.dumps(artifact)
        self.assertNotIn("exact_score_log_loss", body)
        self.assertNotIn("candidate_minus_baseline", body)

    def test_each_evidence_minimum_fails_closed_independently(self) -> None:
        gate = deepcopy(self.gate)
        gate["minimum_evidence"]["fixtures_per_horizon"] = 7
        gate["minimum_evidence"]["minimum_competitions_per_horizon"] = 6

        readiness = build_count_only_readiness(
            self.six_month_records(),
            as_of=datetime(2027, 2, 8, tzinfo=timezone.utc),
            config=self.config,
            gate=gate,
        )

        self.assertFalse(readiness["all_requirements_met"])
        for horizon in HORIZONS:
            criteria = readiness["horizons"][horizon]["minimums_met"]
            self.assertTrue(criteria["complete_calendar_month_blocks"])
            self.assertFalse(criteria["settled_fixtures"])
            self.assertFalse(criteria["competitions"])

    def test_clear_synthetic_improvement_passes_every_gate(self) -> None:
        result = evaluate_selected_records(
            self.six_month_records(),
            cutoff_month="2027-01",
            config=self.config,
            gate=self.gate,
        )

        self.assertTrue(result["all_gates_pass"])
        for horizon in HORIZONS:
            self.assertTrue(result["horizons"][horizon]["all_gates_pass"])
            interval = result["horizons"][horizon][
                "exact_score_log_loss_month_block_bootstrap_95_interval"
            ]
            self.assertLess(interval["upper"], 0)

    def test_negative_mean_but_uncertain_months_fails_primary_bound(self) -> None:
        records = self.six_month_records()
        deltas = [-1.0, -1.0, -1.0, -1.0, -1.0, 4.0]
        horizon_records = [
            record for record in records if record["information_state"] == HORIZONS[1]
        ]
        for record, delta in zip(horizon_records, deltas, strict=True):
            record["metrics"]["candidate"]["exact_score_log_loss"] = 1.0 + delta
            record["metrics"]["candidate_minus_baseline"][
                "exact_score_log_loss"
            ] = delta

        result = evaluate_selected_records(
            records,
            cutoff_month="2027-01",
            config=self.config,
            gate=self.gate,
        )
        failed = result["horizons"][HORIZONS[1]]

        self.assertLess(failed["mean_candidate_minus_baseline"]["exact_score_log_loss"], 0)
        self.assertGreater(
            failed["exact_score_log_loss_month_block_bootstrap_95_interval"]["upper"],
            0,
        )
        self.assertFalse(failed["all_gates_pass"])
        self.assertFalse(result["all_gates_pass"])

    def test_secondary_or_moneyline_breach_fails(self) -> None:
        records = self.six_month_records()
        first = records[0]
        first["metrics"]["candidate"]["home_goals_log_loss"] = 1.02
        first["metrics"]["candidate_minus_baseline"]["home_goals_log_loss"] = 0.02
        first["metrics"]["candidate"][
            "maximum_absolute_parent_moneyline_difference"
        ] = 1e-9

        result = evaluate_selected_records(
            records,
            cutoff_month="2027-01",
            config=self.config,
            gate=self.gate,
        )
        failed = result["horizons"][HORIZONS[0]]

        self.assertFalse(
            failed["gate_checks"]["home_goals_log_loss_mean_at_most_0.001"]
        )
        self.assertFalse(
            failed["gate_checks"][
                "maximum_absolute_parent_moneyline_difference_within_limit"
            ]
        )
        self.assertFalse(result["all_gates_pass"])

    def test_bootstrap_is_deterministic_and_fixture_weighted(self) -> None:
        records = self.six_month_records()[:6]
        first = paired_month_block_bootstrap(
            records,
            metric="exact_score_log_loss",
            replicates=2000,
            seed=20260717,
            lower_quantile=0.025,
            upper_quantile=0.975,
        )
        second = paired_month_block_bootstrap(
            records,
            metric="exact_score_log_loss",
            replicates=2000,
            seed=20260717,
            lower_quantile=0.025,
            upper_quantile=0.975,
        )

        self.assertEqual(first, second)
        self.assertAlmostEqual(first["point_estimate"], -0.1)

    def test_pre_ready_one_shot_command_writes_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_one_shot_evaluation(
                ledger_path=root / "missing-ledger.jsonl",
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                evaluated_at=datetime(2026, 7, 17, 22, tzinfo=timezone.utc),
            )

            self.assertEqual(result["status"], "locked_insufficient_evidence")
            self.assertFalse(result["performance_statistics_exposed"])
            self.assertFalse((root / "output/decision.json").exists())

    def test_production_readiness_output_has_no_performance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = update_evaluation_readiness(
                ledger_path=root / "missing-ledger.jsonl",
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                as_of=datetime(2026, 7, 17, 22, tzinfo=timezone.utc),
            )

            _assert_count_only(result)
            self.assertEqual(result["ledger_records"], 0)
            self.assertEqual(result["status"], "locked_insufficient_evidence")

    def test_automatic_readiness_never_requires_or_reads_metric_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            record = self.ledger_record(
                fixture="counts-only-envelope",
                horizon=HORIZONS[0],
                kickoff="2026-08-15T18:00:00+00:00",
                competition=1,
                previous=None,
                include_metrics=False,
            )
            self.write_ledger(ledger, [record])

            result = update_evaluation_readiness(
                ledger_path=ledger,
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                as_of=datetime(2026, 9, 8, tzinfo=timezone.utc),
            )

            self.assertEqual(result["ledger_records"], 1)
            self.assertFalse(result["performance_statistics_exposed"])
            self.assertEqual(result["status"], "locked_insufficient_evidence")

    def test_full_frozen_minimum_runs_real_one_shot_and_is_immutable(self) -> None:
        months = (
            "2026-08",
            "2026-09",
            "2026-10",
            "2026-11",
            "2026-12",
            "2027-01",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            records = []
            previous = None
            for horizon in HORIZONS:
                for index in range(2000):
                    month = months[index % len(months)]
                    fixture = f"{horizon}-{index:04d}"
                    record = self.ledger_record(
                        fixture=fixture,
                        horizon=horizon,
                        kickoff=f"{month}-15T18:00:00+00:00",
                        competition=(index % 5) + 1,
                        previous=previous,
                    )
                    previous = record["record_sha256"]
                    records.append(record)
            self.write_ledger(ledger, records)

            first = run_one_shot_evaluation(
                ledger_path=ledger,
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                evaluated_at=datetime(2027, 2, 8, tzinfo=timezone.utc),
            )
            decision_path = root / "output/decision.json"
            original = decision_path.read_bytes()
            decision = json.loads(original)

            self.assertEqual(first["status"], "evaluation_completed")
            self.assertEqual(first["decision"], "pass")
            self.assertEqual(
                first["deterministic_evaluation_cutoff_month"], "2027-01"
            )
            self.assertEqual(decision["input_ledger"]["selected_record_count"], 4000)
            for horizon in HORIZONS:
                self.assertEqual(decision["results"][horizon]["fixtures"], 2000)
                self.assertEqual(decision["results"][horizon]["competitions"], 5)
                self.assertEqual(
                    decision["results"][horizon]["calendar_month_blocks"], 6
                )

            second = run_one_shot_evaluation(
                ledger_path=ledger,
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                evaluated_at=datetime(2027, 2, 9, tzinfo=timezone.utc),
            )
            self.assertEqual(second["status"], "decision_already_exists")
            self.assertEqual(decision_path.read_bytes(), original)

            appended = self.ledger_record(
                fixture="post-decision-february",
                horizon=HORIZONS[0],
                kickoff="2027-02-15T18:00:00+00:00",
                competition=1,
                previous=previous,
            )
            self.write_ledger(ledger, [*records, appended])
            after_append = run_one_shot_evaluation(
                ledger_path=ledger,
                model_path=MODEL_PATH,
                gate_path=GATE_PATH,
                settlement_config_path=SETTLEMENT_CONFIG_PATH,
                evaluation_config_path=EVALUATION_CONFIG_PATH,
                output_directory=root / "output",
                evaluated_at=datetime(2027, 2, 9, tzinfo=timezone.utc),
            )
            self.assertEqual(after_append["status"], "decision_already_exists")
            self.assertEqual(decision_path.read_bytes(), original)

            self.write_ledger(ledger, records[:-1])
            with self.assertRaisesRegex(
                ProspectiveEvaluationError, "decision ledger prefix is missing"
            ):
                run_one_shot_evaluation(
                    ledger_path=ledger,
                    model_path=MODEL_PATH,
                    gate_path=GATE_PATH,
                    settlement_config_path=SETTLEMENT_CONFIG_PATH,
                    evaluation_config_path=EVALUATION_CONFIG_PATH,
                    output_directory=root / "output",
                    evaluated_at=datetime(2027, 2, 9, tzinfo=timezone.utc),
                )

    def test_write_once_decision_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decision.json"
            _write_once_json(path, {"decision": "pass"})

            with self.assertRaisesRegex(ProspectiveEvaluationError, "already exists"):
                _write_once_json(path, {"decision": "fail"})
            self.assertEqual(json.loads(path.read_text()), {"decision": "pass"})

    def test_count_readiness_ignores_gate_ineligible_rows(self) -> None:
        records = self.six_month_records()
        ineligible = deepcopy(records[0])
        ineligible["fixture_id"] = "ineligible-extra"
        ineligible["eligible_for_prospective_gate"] = False
        records.append(ineligible)

        readiness = build_count_only_readiness(
            records,
            as_of=datetime(2027, 2, 8, tzinfo=timezone.utc),
            config=self.config,
            gate=self.gate,
        )

        self.assertEqual(
            readiness["horizons"][HORIZONS[0]]["eligible_settled_fixtures"], 6
        )


if __name__ == "__main__":
    unittest.main()
