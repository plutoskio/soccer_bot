from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.markets import (
    build_market_benchmarks,
    load_market_benchmark_config,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config
from tests.test_walk_forward import feature_row


class MarketBenchmarkTests(unittest.TestCase):
    def setUp(self):
        loaded = load_market_benchmark_config(
            ROOT / "config" / "models" / "regulation_market_benchmark_v1.json"
        )
        self.config = replace(
            loaded,
            polymarket_minimum_fixtures=1,
            bookmaker_minimum_fixtures=1,
            bootstrap_replicates=100,
        )
        self.folds = load_walk_forward_config(
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        ).folds
        self.kickoff = datetime(2025, 8, 2, 12, tzinfo=timezone.utc)
        self.feature = feature_row("fixture", self.kickoff, 2, 0)
        self.connection = duckdb.connect(":memory:")
        self._create_schema()
        self._insert_fixture_and_markets()

    def tearDown(self):
        self.connection.close()

    def _create_schema(self):
        self.connection.execute(
            """
            CREATE TABLE team(team_id VARCHAR, name VARCHAR);
            CREATE TABLE fixture(
                fixture_id VARCHAR, home_team_id VARCHAR, away_team_id VARCHAR,
                scheduled_kickoff TIMESTAMPTZ
            );
            CREATE TABLE prediction_market_event(
                prediction_market_event_id VARCHAR, fixture_id VARCHAR
            );
            CREATE TABLE prediction_market(
                prediction_market_id VARCHAR,
                prediction_market_event_id VARCHAR,
                market_type VARCHAR,
                question VARCHAR
            );
            CREATE TABLE prediction_market_outcome(
                outcome_id VARCHAR,
                prediction_market_id VARCHAR,
                outcome_name VARCHAR
            );
            CREATE TABLE orderbook_snapshot(
                outcome_id VARCHAR,
                retrieved_at TIMESTAMPTZ,
                best_bid DOUBLE,
                best_ask DOUBLE,
                kickoff_known_at_retrieval TIMESTAMPTZ
            );
            CREATE TABLE bookmaker_quote(
                fixture_id VARCHAR,
                source_code VARCHAR,
                bookmaker_name VARCHAR,
                market_type VARCHAR,
                selection VARCHAR,
                decimal_odds DOUBLE,
                quote_type VARCHAR,
                quoted_at TIMESTAMPTZ
            );
            """
        )

    def _insert_fixture_and_markets(self):
        self.connection.execute(
            "INSERT INTO team VALUES ('home', 'Alpha FC'), ('away', 'Beta FC')"
        )
        self.connection.execute(
            "INSERT INTO fixture VALUES ('fixture','home','away',?)", [self.kickoff]
        )
        self.connection.execute(
            "INSERT INTO prediction_market_event VALUES ('event','fixture')"
        )
        questions = {
            "home": "Will Alpha FC win on 2025-08-02?",
            "draw": "Will Alpha FC vs. Beta FC end in a draw?",
            "away": "Will Beta FC win on 2025-08-02?",
        }
        prices = {"home": (0.49, 0.51), "draw": (0.24, 0.26), "away": (0.24, 0.26)}
        retrieved_at = self.feature.prediction_at - timedelta(minutes=5)
        for key in ("home", "draw", "away"):
            self.connection.execute(
                "INSERT INTO prediction_market VALUES (?,?, 'moneyline', ?)",
                [f"market-{key}", "event", questions[key]],
            )
            self.connection.execute(
                "INSERT INTO prediction_market_outcome VALUES (?,?, 'Yes')",
                [f"outcome-{key}", f"market-{key}"],
            )
            self.connection.execute(
                "INSERT INTO orderbook_snapshot VALUES (?,?,?,?,?)",
                [
                    f"outcome-{key}",
                    retrieved_at,
                    prices[key][0],
                    prices[key][1],
                    self.kickoff,
                ],
            )
        for selection, odds in (("home", 2.0), ("draw", 4.0), ("away", 4.0)):
            self.connection.execute(
                """
                INSERT INTO bookmaker_quote VALUES (
                    'fixture','football_data_uk','market_average','moneyline',
                    ?,?,'closing',NULL
                )
                """,
                [selection, odds],
            )

    def test_complete_pre_cutoff_books_and_closing_quotes_are_normalized(self):
        rows, audit = build_market_benchmarks(
            self.connection,
            [self.feature],
            config=self.config,
            folds=self.folds,
        )

        self.assertEqual(len(rows), 2)
        self.assertTrue(audit["timestamped_polymarket"]["coverage_gate_passed"])
        by_model = {row.model_key: row for row in rows}
        timestamped = by_model["polymarket_timestamped_no_vig"]
        retrospective = by_model["football_data_closing_consensus_no_vig"]
        self.assertAlmostEqual(
            timestamped.home_win_probability
            + timestamped.draw_probability
            + timestamped.away_win_probability,
            1.0,
        )
        self.assertEqual(timestamped.timing_class, "timestamped_pre_cutoff_orderbook")
        self.assertIsNone(retrospective.snapshot_at)
        self.assertEqual(
            retrospective.timing_class,
            "retrospective_closing_without_quote_timestamp",
        )
        self.assertFalse(audit["retrospective_bookmaker"]["feature_eligible"])

    def test_post_cutoff_books_are_excluded(self):
        self.connection.execute(
            "UPDATE orderbook_snapshot SET retrieved_at=?",
            [self.feature.prediction_at + timedelta(minutes=1)],
        )

        rows, audit = build_market_benchmarks(
            self.connection,
            [self.feature],
            config=self.config,
            folds=self.folds,
        )

        self.assertEqual(
            {row.model_key for row in rows},
            {"football_data_closing_consensus_no_vig"},
        )
        self.assertEqual(audit["timestamped_polymarket"]["eligible_rows"], 0)
        self.assertEqual(
            audit["timestamped_polymarket"]["exclusion_reasons"],
            {"incomplete_three_way_pre_cutoff_books": 1},
        )


if __name__ == "__main__":
    unittest.main()
