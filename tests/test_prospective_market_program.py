from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from soccer_bot.polymarket_contracts import canonical_json_sha256
from soccer_bot.polymarket_evidence import (
    COVERAGE_UNIVERSE_VERSION,
    EVIDENCE_VERSION,
    taker_buy_quote,
)
from soccer_bot.prospective_market_evaluation import (
    build_count_only_market_readiness,
    evaluate_market_records,
    run_one_shot_market_evaluation,
)
from soccer_bot.prospective_market_settlement import (
    ProspectiveMarketSettlementError,
    load_market_settlement_ledger,
    update_market_settlement_ledger,
)


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
HORIZON = "pre_lineup_24h_v1"


def logical_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class MarketSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.coverage = self.root / "coverage"
        self.evidence = self.root / "evidence"
        self.output = self.root / "settlement"
        self.score_ledger = self.root / "score.jsonl"
        self.policy_path = ROOT / "config/contracts/polymarket_regulation_v1.json"
        self.score_config = ROOT / "config/models/regulation_score_grid_v3_settlement.json"
        self.settlement_config = (
            ROOT / "config/models/polymarket_regulation_market_settlement_v1.json"
        )
        self.policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        self.policy_canonical_hash = canonical_json_sha256(self.policy)
        self.model_hash = "b" * 64
        self.prediction_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
        self.kickoff = self.prediction_at + timedelta(hours=24)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def score_row(self, fixture: str, outcome: str, previous: str | None) -> dict:
        row = {
            "ledger_version": "regulation_score_grid_v3_prospective_settlement_v1",
            "evidence_key": f"score-evidence-{fixture}",
            "fixture_id": fixture,
            "competition_id": "competition-1",
            "information_state": HORIZON,
            "prediction_at": self.prediction_at.isoformat(),
            "kickoff": self.kickoff.isoformat(),
            "eligible_for_prospective_gate": True,
            "realized_regulation_score": {
                "home_goals": 1 if outcome == "home_win" else 0,
                "away_goals": 1 if outcome == "away_win" else 0,
                "result": outcome,
            },
            "reference_contract_settlements": {
                "baseline": {
                    "goal_handicap": {
                        "0": {
                            "home": {
                                "forecast": {
                                    "win": 0.6,
                                    "push": 0.25,
                                    "loss": 0.15,
                                }
                            }
                        }
                    }
                }
            },
            "previous_record_sha256": previous,
        }
        row["record_sha256"] = logical_hash(row)
        return row

    def write_score_rows(self, outcomes: list[tuple[str, str]]) -> None:
        rows = []
        previous = None
        for fixture, outcome in outcomes:
            row = self.score_row(fixture, outcome, previous)
            previous = row["record_sha256"]
            rows.append(row)
        self.score_ledger.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )

    def write_coverage(
        self,
        fixture: str,
        *,
        covered: bool,
        probabilities: dict[str, float] | None = None,
        row_identity: str | None = None,
        first_observed_minutes: int = 1,
    ) -> dict:
        probabilities = probabilities or {
            "home_win": 0.6,
            "draw": 0.25,
            "away_win": 0.15,
        }
        prediction_row_hash = hashlib.sha256(
            (row_identity or fixture).encode()
        ).hexdigest()
        coverage_id = hashlib.sha256(
            "|".join(
                (
                    COVERAGE_UNIVERSE_VERSION,
                    fixture,
                    HORIZON,
                    self.prediction_at.isoformat(),
                    prediction_row_hash,
                    self.model_hash,
                    self.policy_canonical_hash,
                )
            ).encode()
        ).hexdigest()
        evidence_id = hashlib.sha256(f"evidence-{fixture}".encode()).hexdigest()
        row = {
            "coverage_universe_version": COVERAGE_UNIVERSE_VERSION,
            "coverage_id": coverage_id,
            "first_observed_at": (
                self.prediction_at + timedelta(minutes=first_observed_minutes)
            ).isoformat(),
            "fixture_id": fixture,
            "information_state": HORIZON,
            "prediction_at": self.prediction_at.isoformat(),
            "kickoff": self.kickoff.isoformat(),
            "model_version": "regulation_champion_v1",
            "logical_model_sha256": self.model_hash,
            "prediction_rows_sha256": "c" * 64,
            "prediction_row_sha256": prediction_row_hash,
            "prediction_snapshot_sha256": "d" * 64,
            "model_probabilities": probabilities,
            "policy_version": self.policy["policy_version"],
            "mapping_version": self.policy["mapping_version"],
            "policy_sha256": self.policy_canonical_hash,
            "cadence_stage": "market_t_minus_1440",
            "market_evidence_available": covered,
            "evidence_id": evidence_id if covered else None,
            "economically_executable": covered,
            "exclusion_reason": None if covered else "moneyline_mapping_incomplete",
            "coverage_funnel": {
                "complete_moneyline_mappings": covered,
                "pre_cutoff_complete_books": covered,
                "valid_bid_ask_books": covered,
            },
            "canonical_policy": "first_observed_coverage_state_per_prediction_row",
            "contains_realized_result_or_performance": False,
            "trading_action_performed": False,
        }
        path = self.coverage / fixture / f"{coverage_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")
        if covered:
            self.write_evidence(row)
        return row

    def test_forecast_upgrade_duplicates_match_settled_probabilities_then_first_seen(self) -> None:
        self.write_score_rows(
            [("fixture-corrected", "home_win"), ("fixture-equivalent", "draw")]
        )
        correct = self.write_coverage(
            "fixture-corrected",
            covered=False,
            row_identity="corrected-original",
            first_observed_minutes=1,
        )
        self.write_coverage(
            "fixture-corrected",
            covered=False,
            probabilities={"home_win": 0.55, "draw": 0.25, "away_win": 0.20},
            row_identity="corrected-replacement",
            first_observed_minutes=2,
        )
        earliest = self.write_coverage(
            "fixture-equivalent",
            covered=False,
            row_identity="equivalent-old-envelope",
            first_observed_minutes=1,
        )
        self.write_coverage(
            "fixture-equivalent",
            covered=False,
            row_identity="equivalent-new-envelope",
            first_observed_minutes=2,
        )

        receipt = update_market_settlement_ledger(
            coverage_universe_directory=self.coverage,
            evidence_directory=self.evidence,
            score_settlement_ledger_path=self.score_ledger,
            score_settlement_config_path=self.score_config,
            market_policy_path=self.policy_path,
            settlement_config_path=self.settlement_config,
            output_directory=self.output,
            settled_at=self.kickoff + timedelta(hours=3),
        )

        self.assertEqual(2, receipt["records_added"])
        rows, _ = load_market_settlement_ledger(
            ledger_path=self.output / "ledger.jsonl",
            settlement_config_path=self.settlement_config,
        )
        by_fixture = {row["fixture_id"]: row for row in rows}
        self.assertEqual(
            correct["coverage_id"],
            by_fixture["fixture-corrected"]["coverage_id"],
        )
        self.assertEqual(
            earliest["coverage_id"],
            by_fixture["fixture-equivalent"]["coverage_id"],
        )
        self.assertAlmostEqual(
            -math.log(0.6),
            by_fixture["fixture-corrected"]["model_metrics"]["log_loss"],
        )

    def write_evidence(self, coverage: dict) -> None:
        probabilities = coverage["model_probabilities"]
        market = {"home_win": 0.45, "draw": 0.30, "away_win": 0.25}
        selections = {}
        retrieved = self.prediction_at - timedelta(seconds=30)
        for key in ("home_win", "draw", "away_win"):
            ask = market[key]
            fee_rate = 0.03
            asks = [{"price": ask, "size": 1000.0}]
            quotes = [
                taker_buy_quote(
                    [(ask, 1000.0)],
                    requested_shares=float(quantity),
                    model_probability=probabilities[key],
                    fee_rate=fee_rate,
                    minimum_order_size=5.0,
                )
                for quantity in self.policy["execution"]["share_quantities"]
            ]
            selections[key] = {
                "model_probability": probabilities[key],
                "market_no_vig_probability": market[key],
                "model_minus_market_no_vig": probabilities[key] - market[key],
                "retrieved_at": retrieved.isoformat(),
                "capture_target_at": self.prediction_at.isoformat(),
                "capture_deadline_at": self.prediction_at.isoformat(),
                "minimum_order_size": 5.0,
                "fee_rate": fee_rate,
                "asks": asks,
                "taker_buy_quotes": quotes,
            }
        row = {
            "evidence_version": EVIDENCE_VERSION,
            "evidence_id": coverage["evidence_id"],
            "fixture_id": coverage["fixture_id"],
            "information_state": HORIZON,
            "prediction_at": self.prediction_at.isoformat(),
            "kickoff": self.kickoff.isoformat(),
            "logical_model_sha256": self.model_hash,
            "prediction_row_sha256": coverage["prediction_row_sha256"],
            "policy_sha256": self.policy_canonical_hash,
            "selections": selections,
            "contains_realized_result_or_performance": False,
            "trading_action_performed": False,
        }
        path = self.evidence / coverage["fixture_id"] / f"{coverage['evidence_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")

    def test_covered_and_uncovered_rows_settle_without_imputation_or_trading(self) -> None:
        self.write_score_rows([("fixture-covered", "home_win"), ("fixture-missing", "draw")])
        self.write_coverage("fixture-covered", covered=True)
        self.write_coverage("fixture-missing", covered=False)
        receipt = update_market_settlement_ledger(
            coverage_universe_directory=self.coverage,
            evidence_directory=self.evidence,
            score_settlement_ledger_path=self.score_ledger,
            score_settlement_config_path=self.score_config,
            market_policy_path=self.policy_path,
            settlement_config_path=self.settlement_config,
            output_directory=self.output,
            settled_at=self.kickoff + timedelta(hours=3),
        )
        self.assertEqual(2, receipt["records_added"])
        self.assertEqual(1, receipt["covered_market_records"])
        self.assertFalse(receipt["orders_or_trading_actions_performed"])
        rows, head = load_market_settlement_ledger(
            ledger_path=self.output / "ledger.jsonl",
            settlement_config_path=self.settlement_config,
        )
        self.assertEqual(receipt["ledger_head_sha256"], head)
        covered, missing = rows
        self.assertAlmostEqual(-math.log(0.6), covered["model_metrics"]["log_loss"])
        action = covered["execution_research"]["10"]
        self.assertEqual("home_win", action["selection"])
        self.assertGreater(action["realized_profit"], 0)
        self.assertIsNone(missing["market_metrics"])
        self.assertIsNone(missing["execution_research"])
        self.assertEqual("moneyline_mapping_incomplete", missing["coverage_exclusion_reason"])

        repeated = update_market_settlement_ledger(
            coverage_universe_directory=self.coverage,
            evidence_directory=self.evidence,
            score_settlement_ledger_path=self.score_ledger,
            score_settlement_config_path=self.score_config,
            market_policy_path=self.policy_path,
            settlement_config_path=self.settlement_config,
            output_directory=self.output,
            settled_at=self.kickoff + timedelta(hours=4),
        )
        self.assertEqual("no_new_settlements", repeated["status"])
        self.assertEqual(0, repeated["records_added"])

    def test_hash_chain_tampering_fails_closed(self) -> None:
        self.write_score_rows([("fixture-covered", "home_win")])
        self.write_coverage("fixture-covered", covered=True)
        update_market_settlement_ledger(
            coverage_universe_directory=self.coverage,
            evidence_directory=self.evidence,
            score_settlement_ledger_path=self.score_ledger,
            score_settlement_config_path=self.score_config,
            market_policy_path=self.policy_path,
            settlement_config_path=self.settlement_config,
            output_directory=self.output,
            settled_at=self.kickoff + timedelta(hours=3),
        )
        ledger = self.output / "ledger.jsonl"
        row = json.loads(ledger.read_text(encoding="utf-8"))
        row["realized_regulation_result"] = "away_win"
        ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(ProspectiveMarketSettlementError):
            load_market_settlement_ledger(
                ledger_path=ledger, settlement_config_path=self.settlement_config
            )


class MarketEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (
                ROOT / "config/models/polymarket_regulation_market_evaluation_v1.json"
            ).read_text(encoding="utf-8")
        )
        self.config["bootstrap"]["replicates"] = 100
        self.config["horizons"] = [HORIZON]
        self.config["minimum_evidence"] = {
            key: 1 for key in self.config["minimum_evidence"]
        }

    def row(self, fixture: str, *, covered: bool, month: int = 8) -> dict:
        kickoff = datetime(2026, month, 15, tzinfo=UTC)
        model = {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}
        market = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
        action = {
            "strategy_action": "paper_buy_yes_as_taker",
            "selection": "home_win",
            "realized_profit": 1.0,
            "capital_committed": 5.0,
            "won": True,
        }
        row = {
            "ledger_version": "polymarket_regulation_market_settlement_v1",
            "fixture_id": fixture,
            "competition_id": "competition-1",
            "information_state": HORIZON,
            "kickoff": kickoff.isoformat(),
            "eligible_for_market_evaluation": True,
            "market_evidence_available": covered,
            "economically_executable": covered,
            "realized_regulation_result": "home_win",
            "model_metrics": {
                "probabilities": model,
                "log_loss": -math.log(model["home_win"]),
                "brier": 0.245,
            },
            "market_metrics": None,
            "execution_research": None,
            "record_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
        }
        if covered:
            row["market_metrics"] = {
                "model_probabilities": model,
                "market_no_vig_probabilities": market,
                "model_log_loss": -math.log(model["home_win"]),
                "market_log_loss": -math.log(market["home_win"]),
                "model_minus_market_log_loss": math.log(market["home_win"] / model["home_win"]),
                "model_brier": 0.245,
                "market_brier": 0.38,
                "model_minus_market_brier": -0.135,
                "maximum_absolute_disagreement": 0.1,
                "disagreement": {
                    key: model[key] - market[key] for key in model
                },
            }
            row["execution_research"] = {
                str(int(quantity)): dict(action)
                for quantity in (10.0, 50.0, 100.0, 250.0)
            }
        return row

    def test_five_questions_remain_separate_and_math_is_deterministic(self) -> None:
        records = [self.row(f"fixture-{index}", covered=True) for index in range(6)]
        result = evaluate_market_records(records, config=self.config)[HORIZON]
        self.assertEqual(
            {
                "population",
                "1_predictive_accuracy",
                "2_calibration",
                "3_market_disagreement",
                "4_executable_edge",
                "5_selection_bias",
            },
            set(result),
        )
        accuracy = result["1_predictive_accuracy"]["log_loss"]
        self.assertLess(accuracy["model_minus_market_mean"], 0)
        self.assertAlmostEqual(
            0.2,
            result["4_executable_edge"]["10"]["realized_return_on_cost"],
        )
        self.assertEqual(0, result["4_executable_edge"]["10"]["actual_orders_or_trades"])

    def test_readiness_is_count_only_and_requires_mature_complete_month(self) -> None:
        records = [self.row("fixture-1", covered=True)]
        readiness = build_count_only_market_readiness(
            records,
            as_of=datetime(2026, 9, 8, tzinfo=UTC),
            config=self.config,
        )
        self.assertTrue(readiness["all_requirements_met"])
        self.assertEqual("2026-08", readiness["deterministic_evaluation_cutoff_month"])
        serialized = json.dumps(readiness).lower()
        for forbidden in ("log_loss", "brier", "profit", "roi", "calibration"):
            self.assertNotIn(forbidden, serialized)

    def test_real_frozen_program_stays_locked_and_writes_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = run_one_shot_market_evaluation(
                ledger_path=root / "missing-ledger.jsonl",
                settlement_config_path=(
                    ROOT / "config/models/polymarket_regulation_market_settlement_v1.json"
                ),
                evaluation_config_path=(
                    ROOT / "config/models/polymarket_regulation_market_evaluation_v1.json"
                ),
                output_directory=root / "evaluation",
                evaluated_at=datetime(2026, 9, 8, tzinfo=UTC),
            )
            self.assertEqual("locked_insufficient_evidence", result["status"])
            self.assertFalse(result["performance_statistics_exposed"])
            self.assertFalse((root / "evaluation/report.json").exists())


if __name__ == "__main__":
    unittest.main()
