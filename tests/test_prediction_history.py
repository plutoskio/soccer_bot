from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from soccer_bot.prediction_history import (
    PredictionHistoryError,
    build_prediction_history,
    validate_prediction_history,
)


ROOT = Path(__file__).resolve().parents[1]


class PredictionHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        snapshot = json.loads(
            (
                ROOT
                / "data/predictions/regulation_score_grid_v3_shadow/latest.json"
            ).read_text(encoding="utf-8")
        )
        self.prediction = deepcopy(snapshot["predictions"][0])
        self.prediction["fixture_id"] = "settled-fixture"
        self.prediction["fixture"] = {
            "fixture_id": "settled-fixture",
            "competition_name": "Example League",
            "home_team_name": "Home",
            "away_team_name": "Away",
        }
        self.prediction["kickoff"] = "2026-07-20T18:00:00+00:00"
        self.prediction["prediction_at"] = "2026-07-19T18:00:00+00:00"
        self.evidence = {
            "evidence_key": "evidence-1",
            "prediction": self.prediction,
        }
        self.record = {
            "evidence_key": "evidence-1",
            "eligible_for_prospective_gate": True,
            "fixture_id": "settled-fixture",
            "kickoff": self.prediction["kickoff"],
            "competition_id": "competition-1",
            "model_version": "regulation_score_grid_v3_prospective_shadow",
            "logical_model_sha256": "a" * 64,
            "information_state": "pre_lineup_24h_v1",
            "prediction_at": self.prediction["prediction_at"],
            "first_snapshot_created_at": "2026-07-19T18:01:00+00:00",
            "settled_at": "2026-07-20T21:00:00+00:00",
            "realized_regulation_score": {
                "home_goals": 2,
                "away_goals": 1,
                "result": "home_win",
            },
            "reference_contract_settlements": {
                "candidate": {
                    "total_goals": {
                        "2.5": {
                            "over": self._line("win"),
                            "under": self._line("loss"),
                        }
                    },
                    "goal_handicap": {
                        "-0.25": {
                            "home": self._line("win"),
                            "away": self._line("loss"),
                        }
                    },
                }
            },
        }

    @staticmethod
    def _line(realized: str) -> dict[str, object]:
        return {
            "forecast": {
                "win": 0.45,
                "half_win": 0.05,
                "push": 0.1,
                "half_loss": 0.05,
                "loss": 0.35,
            },
            "realized_outcome": realized,
        }

    def build(
        self,
        records: list[dict] | None = None,
        comparisons: dict | None = None,
    ) -> dict[str, object]:
        with (
            patch(
                "soccer_bot.prediction_history.load_forecast_evidence",
                return_value=[self.evidence],
            ),
            patch(
                "soccer_bot.prediction_history.load_prospective_settlement_ledger",
                return_value=(records if records is not None else [self.record], "b" * 64),
            ),
            patch(
                "soccer_bot.prediction_history._load_bookmaker_comparisons",
                return_value=comparisons or {},
            ),
        ):
            return build_prediction_history(
                evidence_directory=Path("unused-evidence"),
                ledger_path=Path("unused-ledger"),
                settlement_config_path=Path("unused-config"),
                generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
            )

    def test_builds_latest_first_settled_archive_with_full_market_groups(self) -> None:
        history = self.build()

        self.assertEqual(history["fixture_count"], 1)
        fixture = history["fixtures"][0]
        self.assertEqual(fixture["home_team_name"], "Home")
        self.assertEqual(fixture["result"]["home_goals"], 2)
        group = fixture["prediction_groups"][0]
        self.assertEqual(group["evidence_classification"], "published_forward")
        self.assertFalse(group["eligible_for_performance_claim"])
        market_groups = {market["group"] for market in group["markets"]}
        self.assertEqual(
            market_groups,
            {
                "Match result",
                "Exact score",
                "Total goals",
                "Home goals",
                "Away goals",
                "Goal difference",
                "Both teams to score",
                "Goal totals",
                "Goal handicap",
            },
        )
        exact = next(market for market in group["markets"] if market["market_id"] == "exact:2:1")
        self.assertEqual(exact["realized_settlement"], "win")
        differences = [
            int(market["label"])
            for market in group["markets"]
            if market["group"] == "Goal difference"
        ]
        self.assertGreaterEqual(min(differences), -8)
        self.assertLessEqual(max(differences), 8)
        validate_prediction_history(history)

    def test_timestamp_safe_bookmaker_consensus_enters_readiness_counter(self) -> None:
        quote = {
            "source": "api_football",
            "quote_type": "cutoff_consensus",
            "market_probability": 0.4,
            "market_decimal_multiplier": 2.5,
            "bookmaker_count": 4,
            "consensus_method": "median_proportional_devig",
            "observed_at": "2026-07-19T17:55:00+00:00",
            "retrieved_at": "2026-07-19T17:56:00+00:00",
        }
        comparisons = {
            (
                "settled-fixture",
                "pre_lineup_24h_v1",
                self.prediction["prediction_at"],
            ): {
                outcome: {
                    "model_probability": probability,
                    "quote": {**quote, "market_probability": market_probability, "market_decimal_multiplier": 1 / market_probability},
                }
                for outcome, probability, market_probability in (
                    ("home_win", self.prediction["parent_moneyline"]["home_win"], 0.4),
                    ("draw", self.prediction["parent_moneyline"]["draw"], 0.3),
                    ("away_win", self.prediction["parent_moneyline"]["away_win"], 0.3),
                )
            }
        }

        history = self.build(comparisons=comparisons)

        readiness = history["bookmaker_readiness"]
        self.assertEqual(readiness["settled_timestamp_safe_quotes"], 3)
        self.assertEqual(readiness["settled_fixture_horizons"], 1)
        self.assertEqual(readiness["calendar_months"], 1)
        self.assertEqual(readiness["status"], "collecting")
        self.assertIsNone(readiness["comparison"])

    def test_gate_ineligible_records_are_excluded(self) -> None:
        record = deepcopy(self.record)
        record["eligible_for_prospective_gate"] = False
        history = self.build([record])

        self.assertEqual(history["fixture_count"], 0)
        self.assertEqual(history["excluded_ineligible_records"], 1)

    def test_validator_rejects_post_kickoff_publication(self) -> None:
        history = self.build()
        history["fixtures"][0]["prediction_groups"][0]["first_published_at"] = (
            "2026-07-20T18:00:01+00:00"
        )

        with self.assertRaisesRegex(PredictionHistoryError, "not pre-kickoff"):
            validate_prediction_history(history)

    def test_missing_immutable_forecast_evidence_fails_closed(self) -> None:
        with (
            patch(
                "soccer_bot.prediction_history.load_forecast_evidence",
                return_value=[],
            ),
            patch(
                "soccer_bot.prediction_history.load_prospective_settlement_ledger",
                return_value=([self.record], "b" * 64),
            ),
        ):
            with self.assertRaisesRegex(PredictionHistoryError, "no immutable"):
                build_prediction_history(
                    evidence_directory=Path("unused-evidence"),
                    ledger_path=Path("unused-ledger"),
                    settlement_config_path=Path("unused-config"),
                    generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
                )


if __name__ == "__main__":
    unittest.main()
