from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from soccer_bot.database import Warehouse
from soccer_bot.modeling.platform_markets import moneyline_markets
from soccer_bot.platform_market_quotes import attach_polymarket_quotes
from soccer_bot.polymarket_contracts import load_polymarket_contract_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config/contracts/polymarket_regulation_v1.json"


class PlatformMarketQuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.warehouse = Warehouse(
            Path(self.temp.name) / "warehouse.duckdb", ROOT / "migrations"
        )
        self.warehouse.migrate()
        self.policy, self.policy_hash = load_polymarket_contract_policy(POLICY_PATH)
        self.kickoff = datetime(2026, 7, 21, 18, tzinfo=timezone.utc)
        self.prediction_at = self.kickoff - timedelta(days=1)
        self.created_at = self.prediction_at + timedelta(hours=2)
        self._seed_market()

    def tearDown(self) -> None:
        self.warehouse.close()
        self.temp.cleanup()

    def _seed_market(self) -> None:
        c = self.warehouse.connection
        c.execute(
            """
            INSERT INTO prediction_market_event (
                prediction_market_event_id,source_event_id,fixture_id,slug,retrieved_at
            ) VALUES ('event','source-event','fixture','ucl-alpha-beta',?)
            """,
            [self.prediction_at - timedelta(days=1)],
        )
        c.execute(
            """
            INSERT INTO prediction_market (
                prediction_market_id,prediction_market_event_id,source_market_id,
                market_type,active,closed,retrieved_at
            ) VALUES ('market','event','source-market','moneyline',true,false,?)
            """,
            [self.prediction_at - timedelta(days=1)],
        )
        c.execute(
            """
            INSERT INTO prediction_market_outcome
            VALUES ('outcome','market','token','Yes',0.5)
            """
        )
        c.execute(
            """
            INSERT INTO polymarket_contract_mapping (
                mapping_id,prediction_market_id,fixture_id,mapping_version,
                mapping_policy_sha256,provider_market_type,contract_key,period,
                parameters,mapping_status,rejection_reason,rules_sha256,mapped_at
            ) VALUES (
                'mapping','market','fixture',?,?,'moneyline',
                'regulation_moneyline','regulation_plus_stoppage_time','{}',
                'accepted',NULL,repeat('a',64),?
            )
            """,
            [self.policy["mapping_version"], self.policy_hash, self.prediction_at],
        )
        c.execute(
            """
            INSERT INTO polymarket_contract_outcome_mapping
            VALUES ('mapping','outcome','home_win',1)
            """
        )
        c.execute(
            """
            INSERT INTO orderbook_snapshot (
                orderbook_snapshot_id,outcome_id,source_token_id,observed_at,
                retrieved_at,best_bid,best_ask,cadence_stage,
                kickoff_known_at_retrieval,book_complete,capture_target_at,
                capture_window_start_at,capture_deadline_at,capture_timing_valid
            ) VALUES
              ('cutoff','outcome','token',?,?,0.44,0.46,'market_t_minus_1440',
               ?,true,?,?,?,true),
              ('live','outcome','token',?,?,0.49,0.51,'market_live',
               ?,true,NULL,?,NULL,true)
            """,
            [
                self.prediction_at - timedelta(seconds=5),
                self.prediction_at - timedelta(seconds=5),
                self.kickoff,
                self.prediction_at,
                self.prediction_at - timedelta(minutes=16),
                self.prediction_at,
                self.created_at - timedelta(minutes=5),
                self.created_at - timedelta(minutes=5),
                self.kickoff,
                self.created_at - timedelta(minutes=10),
            ],
        )

    def _state(self, information_state: str, prediction_at: datetime) -> dict:
        return {
            "fixture_id": "fixture",
            "kickoff": self.kickoff.isoformat(),
            "prediction_at": prediction_at.isoformat(),
            "information_state": information_state,
            "families": [{
                "family_key": "regulation_moneyline",
                "markets": moneyline_markets(
                    {"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
                    home_name="Alpha",
                    away_name="Beta",
                ),
            }],
        }

    def test_live_and_exact_cutoff_quotes_remain_separate(self) -> None:
        state = self._state("pre_lineup_24h_v1", self.prediction_at)
        summary = attach_polymarket_quotes(
            self.warehouse.connection,
            states=[state],
            policy=self.policy,
            policy_sha256=self.policy_hash,
            created_at=self.created_at,
            live_max_age_minutes=20,
        )
        home = state["families"][0]["markets"][0]
        self.assertAlmostEqual(0.45, home["market_comparison"]["market_probability"])
        self.assertAlmostEqual(0.50, home["live_market"]["market_probability"])
        self.assertEqual("cutoff", home["market_comparison"]["quote_type"])
        self.assertEqual("live", home["live_market"]["quote_type"])
        self.assertEqual(1, summary["live_market_fixture_count"])

    def test_live_quote_never_backfills_a_missing_cutoff(self) -> None:
        state = self._state(
            "pre_lineup_72h_clean_v1", self.kickoff - timedelta(days=3)
        )
        attach_polymarket_quotes(
            self.warehouse.connection,
            states=[state],
            policy=self.policy,
            policy_sha256=self.policy_hash,
            created_at=self.created_at,
            live_max_age_minutes=20,
        )
        home = state["families"][0]["markets"][0]
        self.assertIsNone(home["market_comparison"])
        self.assertIsNotNone(home["live_market"])


if __name__ == "__main__":
    unittest.main()
