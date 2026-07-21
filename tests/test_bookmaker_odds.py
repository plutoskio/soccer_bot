from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from soccer_bot.bookmaker_odds import (
    attach_bookmaker_consensus,
    persist_api_football_moneyline_odds,
)
from soccer_bot.database import Warehouse
from soccer_bot.modeling.platform_markets import moneyline_markets


ROOT = Path(__file__).resolve().parents[1]


class BookmakerOddsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.warehouse = Warehouse(
            Path(self.temp.name) / "warehouse.duckdb", ROOT / "migrations"
        )
        self.warehouse.migrate()
        self.kickoff = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
        self.prediction_at = self.kickoff - timedelta(days=1)

    def tearDown(self) -> None:
        self.warehouse.close()
        self.temp.cleanup()

    def test_persists_complete_api_moneyline_books_and_attaches_devigged_consensus(self):
        retrieved_at = self.prediction_at - timedelta(minutes=5)
        payload = {
            "response": [
                {
                    "fixture": {"id": 123},
                    "update": (retrieved_at - timedelta(minutes=1)).isoformat(),
                    "bookmakers": [
                        self._book(1, "Alpha", 2.00, 3.00, 4.00),
                        self._book(2, "Beta", 2.10, 3.10, 3.80),
                        self._book(3, "Gamma", 1.95, 3.20, 4.10),
                    ],
                }
            ]
        }
        stats = persist_api_football_moneyline_odds(
            self.warehouse.connection,
            payload=payload,
            raw_item={
                "_raw_artifact_id": "raw-odds",
                "retrieved_at": retrieved_at.isoformat(),
            },
            fixture_id="fixture",
            fixture_source_id="123",
            quote_type="bookmaker_t_minus_1440",
            bet_id=1,
        )
        self.assertEqual(
            {"inserted_quotes": 9, "complete_bookmakers": 3}, stats
        )

        states = [self._state()]
        summary = attach_bookmaker_consensus(
            self.warehouse.connection,
            states=states,
            stage_window_minutes=16,
            minimum_bookmakers=3,
        )
        quotes = [
            market["market_comparison"]
            for market in states[0]["families"][0]["markets"]
        ]
        self.assertTrue(all(quote is not None for quote in quotes))
        self.assertAlmostEqual(1.0, sum(quote["market_probability"] for quote in quotes))
        self.assertEqual({3}, {quote["bookmaker_count"] for quote in quotes})
        self.assertEqual("api_football", quotes[0]["source"])
        self.assertEqual("cutoff_consensus", quotes[0]["quote_type"])
        self.assertEqual(1, summary["cutoff_market_fixture_count"])
        self.assertEqual(3, summary["cutoff_market_quote_count"])
        self.assertTrue(
            all(
                market["live_market"] is None
                for market in states[0]["families"][0]["markets"]
            )
        )

    def test_after_cutoff_response_cannot_be_used_as_cutoff_consensus(self):
        payload = {
            "response": [
                {
                    "fixture": {"id": 123},
                    "update": self.prediction_at.isoformat(),
                    "bookmakers": [
                        self._book(1, "Alpha", 2.00, 3.00, 4.00),
                        self._book(2, "Beta", 2.10, 3.10, 3.80),
                        self._book(3, "Gamma", 1.95, 3.20, 4.10),
                    ],
                }
            ]
        }
        persist_api_football_moneyline_odds(
            self.warehouse.connection,
            payload=payload,
            raw_item={
                "_raw_artifact_id": "late-raw",
                "retrieved_at": (self.prediction_at + timedelta(seconds=1)).isoformat(),
            },
            fixture_id="fixture",
            fixture_source_id="123",
            quote_type="bookmaker_t_minus_1440",
            bet_id=1,
        )
        states = [self._state()]
        summary = attach_bookmaker_consensus(
            self.warehouse.connection,
            states=states,
            stage_window_minutes=16,
            minimum_bookmakers=3,
        )
        self.assertEqual(0, summary["cutoff_market_fixture_count"])
        self.assertTrue(
            all(
                market["market_comparison"] is None
                for market in states[0]["families"][0]["markets"]
            )
        )

    def _state(self) -> dict:
        return {
            "fixture_id": "fixture",
            "kickoff": self.kickoff.isoformat(),
            "prediction_at": self.prediction_at.isoformat(),
            "information_state": "pre_lineup_24h_v1",
            "families": [
                {
                    "markets": deepcopy(
                        moneyline_markets(
                            {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
                            home_name="Home",
                            away_name="Away",
                        )
                    )
                }
            ],
        }

    @staticmethod
    def _book(
        bookmaker_id: int,
        name: str,
        home: float,
        draw: float,
        away: float,
    ) -> dict:
        return {
            "id": bookmaker_id,
            "name": name,
            "bets": [
                {
                    "id": 1,
                    "name": "Match Winner",
                    "values": [
                        {"value": "Home", "odd": str(home)},
                        {"value": "Draw", "odd": str(draw)},
                        {"value": "Away", "odd": str(away)},
                    ],
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
