from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.database import Warehouse
from soccer_bot.polymarket_contracts import (
    classify_polymarket_contract,
    load_polymarket_contract_policy,
    refresh_polymarket_contract_mappings,
)
from soccer_bot.polymarket_evidence import (
    PolymarketEvidenceError,
    capture_polymarket_market_evidence,
    taker_buy_quote,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "contracts" / "polymarket_regulation_v1.json"
REGULATION_RULE = (
    "This market refers only to the outcome within the first 90 minutes of "
    "regular play plus stoppage time."
)
EXACT_RULE = (
    "Considering only the result at the end of 90 minutes of regulation plus "
    "stoppage time; extra time and penalty shoot-outs are excluded."
)


class PolymarketContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy, cls.policy_hash = load_polymarket_contract_policy(POLICY_PATH)

    def classify(
        self,
        market_type: str,
        question: str,
        *,
        line=None,
        rules: str = REGULATION_RULE,
        outcomes=("Yes", "No"),
    ):
        return classify_polymarket_contract(
            self.policy,
            market_type=market_type,
            question=question,
            line_value=line,
            rules_text=rules,
            home_name="Alpha FC",
            away_name="Beta",
            outcomes=tuple((f"outcome-{index}", name) for index, name in enumerate(outcomes)),
        )

    def test_all_supported_regulation_contracts_map_deterministically(self) -> None:
        home = self.classify("moneyline", "Will Alpha FC win on 2026-07-20?")
        draw = self.classify(
            "moneyline", "Will Alpha FC vs. Beta end in a draw?"
        )
        away = self.classify("moneyline", "Will Beta win on 2026-07-20?")
        self.assertEqual("home_win", home.outcomes[0][1])
        self.assertEqual("draw", draw.outcomes[0][1])
        self.assertEqual("away_win", away.outcomes[0][1])

        total = self.classify(
            "totals", "Alpha FC vs. Beta: O/U 2.5", line=2.5,
            outcomes=("Over", "Under"),
        )
        self.assertEqual({"line": 2.5}, total.parameters)
        self.assertEqual({"over", "under"}, {row[1] for row in total.outcomes})

        spread = self.classify(
            "spreads", "Spread: Beta (-1.5)", line=-1.5,
            outcomes=("Alpha FC", "Beta"),
        )
        self.assertEqual({"home_handicap": 1.5}, spread.parameters)
        self.assertEqual(
            {"home_cover", "away_cover"}, {row[1] for row in spread.outcomes}
        )

        team_total = self.classify(
            "soccer_team_totals",
            "Alpha FC vs. Beta: Beta O/U 1.5",
            line=1.5,
            outcomes=("Over", "Under"),
        )
        self.assertEqual({"team": "away", "line": 1.5}, team_total.parameters)

        btts = self.classify(
            "both_teams_to_score", "Alpha FC vs. Beta: Both Teams to Score"
        )
        self.assertEqual("regulation_both_teams_to_score", btts.contract_key)

        exact = self.classify(
            "soccer_exact_score",
            "Exact Score: Alpha FC 2 - 1 Beta?",
            rules=EXACT_RULE,
        )
        self.assertEqual({"home_goals": 2, "away_goals": 1}, exact.parameters)
        other = self.classify(
            "soccer_exact_score", "Exact Score: Any Other Score?", rules=EXACT_RULE
        )
        self.assertEqual("other_score", other.outcomes[0][1])

        for decision in (home, draw, away, total, spread, team_total, btts, exact, other):
            self.assertEqual("accepted", decision.status)
            self.assertEqual("regulation_plus_stoppage_time", decision.period)

    def test_ambiguous_or_non_regulation_contracts_fail_closed(self) -> None:
        wrong_period = self.classify(
            "moneyline",
            "Will Alpha FC win on 2026-07-20?",
            rules="Includes extra time and penalties.",
        )
        self.assertEqual("rejected", wrong_period.status)
        self.assertEqual("regulation_period_language_missing", wrong_period.rejection_reason)

        bad_line = self.classify(
            "totals",
            "Alpha FC vs. Beta: O/U 3.5",
            line=2.5,
            outcomes=("Over", "Under"),
        )
        self.assertEqual("total_line_mismatch", bad_line.rejection_reason)

        bad_outcomes = self.classify(
            "moneyline",
            "Will Alpha FC win on 2026-07-20?",
            outcomes=("Alpha", "Beta"),
        )
        self.assertEqual("binary_outcomes_must_be_yes_no", bad_outcomes.rejection_reason)

        wrong_fixture = self.classify(
            "moneyline", "Will Gamma vs. Delta end in a draw?"
        )
        self.assertEqual(
            "moneyline_question_or_team_ambiguous",
            wrong_fixture.rejection_reason,
        )

    def test_controlled_team_aliases_apply_to_contract_mapping(self) -> None:
        decision = classify_polymarket_contract(
            self.policy,
            market_type="moneyline",
            question="Will GNK Dinamo Zagreb win on 2026-07-21?",
            line_value=None,
            rules_text=REGULATION_RULE,
            home_name="FC Thun",
            away_name="Dinamo Zagreb",
            outcomes=(("yes", "Yes"), ("no", "No")),
            team_aliases={"gnk dinamo zagreb": "dinamo zagreb"},
        )
        self.assertEqual("accepted", decision.status)
        self.assertEqual("away_win", decision.outcomes[0][1])


class DepthPricingTests(unittest.TestCase):
    def test_depth_fee_and_expected_value_are_computed_per_fill(self) -> None:
        quote = taker_buy_quote(
            [(0.40, 5), (0.45, 10)],
            requested_shares=10,
            model_probability=0.50,
            fee_rate=0.03,
            minimum_order_size=5,
        )
        self.assertTrue(quote["fully_filled"])
        self.assertAlmostEqual(4.25, quote["gross_cost"])
        expected_fee = 5 * 0.03 * 0.4 * 0.6 + 5 * 0.03 * 0.45 * 0.55
        self.assertAlmostEqual(expected_fee, quote["fee"])
        self.assertAlmostEqual(5 - 4.25 - expected_fee, quote["model_expected_profit"])
        self.assertAlmostEqual(0.025, quote["vwap_slippage"])

    def test_partial_fill_or_unknown_fee_is_not_economically_eligible(self) -> None:
        partial = taker_buy_quote(
            [(0.4, 2)], requested_shares=10, model_probability=0.5, fee_rate=0.03
        )
        self.assertFalse(partial["fully_filled"])
        self.assertIsNone(partial["model_expected_profit"])
        unknown_fee = taker_buy_quote(
            [(0.4, 20)], requested_shares=10, model_probability=0.5, fee_rate=None
        )
        self.assertFalse(unknown_fee["economically_eligible"])
        self.assertIsNone(unknown_fee["net_cost"])

    def test_invalid_or_duplicate_book_levels_are_rejected(self) -> None:
        with self.assertRaises(PolymarketEvidenceError):
            taker_buy_quote(
                [(0.4, 5), (0.4, 6)],
                requested_shares=10,
                model_probability=0.5,
                fee_rate=0.03,
            )


class ImmutableEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.warehouse = Warehouse(
            self.root / "soccer.duckdb", ROOT / "migrations"
        )
        self.warehouse.migrate()
        self.warehouse.register_sources()
        self.policy, self.policy_hash = load_polymarket_contract_policy(POLICY_PATH)
        self.kickoff = datetime(2026, 7, 20, 18, tzinfo=timezone.utc)
        self.prediction_at = self.kickoff - timedelta(days=1)
        self._seed_fixture_and_books()

    def tearDown(self) -> None:
        self.warehouse.close()
        self.tempdir.cleanup()

    def _seed_fixture_and_books(self) -> None:
        c = self.warehouse.connection
        c.execute(
            "INSERT INTO team (team_id,name,normalized_name,team_type) VALUES "
            "('home','Alpha FC','alpha fc','club'),('away','Beta','beta','club')"
        )
        c.execute(
            """
            INSERT INTO fixture (fixture_id,home_team_id,away_team_id,scheduled_kickoff)
            VALUES ('fixture-1','home','away',?)
            """,
            [self.kickoff],
        )
        c.execute(
            """
            INSERT INTO prediction_market_event (
                prediction_market_event_id,source_event_id,fixture_id,retrieved_at
            ) VALUES ('event-1','source-event-1','fixture-1',?)
            """,
            [self.prediction_at - timedelta(hours=1)],
        )
        questions = {
            "home_win": "Will Alpha FC win on 2026-07-20?",
            "draw": "Will Alpha FC vs. Beta end in a draw?",
            "away_win": "Will Beta win on 2026-07-20?",
        }
        retrieved = self.prediction_at - timedelta(minutes=6)
        c.execute(
            """
            INSERT INTO raw_artifact (
                raw_artifact_id,source_code,resource_name,retrieved_at,
                content_sha256,data_path,metadata_path
            ) VALUES ('raw-book','polymarket_clob','order_books_batch',?,?,'raw','meta')
            """,
            [retrieved, "a" * 64],
        )
        for index, (selection, question) in enumerate(questions.items()):
            market = f"market-{selection}"
            yes = f"outcome-{selection}-yes"
            no = f"outcome-{selection}-no"
            token = f"token-{selection}-yes"
            c.execute(
                """
                INSERT INTO prediction_market (
                    prediction_market_id,prediction_market_event_id,source_market_id,
                    question,market_type,rules_text,active,closed,retrieved_at,
                    fees_enabled
                ) VALUES (?, 'event-1', ?, ?, 'moneyline', ?, true, false, ?, false)
                """,
                [market, market, question, REGULATION_RULE, retrieved - timedelta(minutes=1)],
            )
            c.execute(
                """
                INSERT INTO prediction_market_outcome VALUES
                    (?, ?, ?, 'Yes', NULL),
                    (?, ?, ?, 'No', NULL)
                """,
                [yes, market, token, no, market, f"token-{selection}-no"],
            )
            c.execute(
                """
                INSERT INTO prediction_market_observation (
                    market_observation_id,prediction_market_id,raw_artifact_id,
                    active,closed,question,rules_text,retrieved_at,fees_enabled
                ) VALUES (?, ?, 'raw-book',true,false,?,?,?,false)
                """,
                [f"observation-{selection}", market, question, REGULATION_RULE, retrieved - timedelta(minutes=1)],
            )
            snapshot_id = f"snapshot-{selection}"
            bid = 0.20 + 0.1 * index
            ask = bid + 0.02
            c.execute(
                """
                INSERT INTO orderbook_snapshot (
                    orderbook_snapshot_id,outcome_id,source_token_id,
                    market_condition_id,observed_at,retrieved_at,best_bid,best_ask,
                    tick_size,minimum_order_size,raw_artifact_id,cadence_stage,
                    kickoff_known_at_retrieval,book_hash,book_complete,
                    capture_target_at,capture_window_start_at,capture_deadline_at,
                    capture_timing_valid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.01, 5, 'raw-book',
                          'market_t_minus_1440', ?, ?, true, ?, ?, ?, true)
                """,
                [
                    snapshot_id, yes, token, market, retrieved, retrieved, bid, ask,
                    self.kickoff, f"book-hash-{index}", self.prediction_at,
                    self.prediction_at - timedelta(minutes=16), self.prediction_at,
                ],
            )
            c.execute(
                """
                INSERT INTO orderbook_level VALUES
                    (?, 'bid', 0, ?, 1000),
                    (?, 'ask', 0, ?, 1000)
                """,
                [snapshot_id, bid, snapshot_id, ask],
            )
        counts = refresh_polymarket_contract_mappings(
            c,
            policy=self.policy,
            policy_sha256=self.policy_hash,
            mapped_at=retrieved,
        )
        self.assertEqual(3, counts["accepted"])

    def snapshot(self) -> dict:
        return {
            "snapshot_version": "upcoming_regulation_moneyline_snapshot_v2",
            "model_version": "regulation_champion_v1",
            "logical_model_sha256": "b" * 64,
            "prediction_rows_sha256": "c" * 64,
            "as_of": self.prediction_at.isoformat(),
            "predictions": [
                {
                    "fixture_id": "fixture-1",
                    "information_state": "pre_lineup_24h_v1",
                    "prediction_at": self.prediction_at.isoformat(),
                    "kickoff": self.kickoff.isoformat(),
                    "home_win_probability": 0.5,
                    "draw_probability": 0.3,
                    "away_win_probability": 0.2,
                }
            ],
        }

    def test_complete_pre_cutoff_books_create_one_immutable_evidence_record(self) -> None:
        output = self.root / "evidence"
        first = capture_polymarket_market_evidence(
            self.warehouse.connection,
            snapshot=self.snapshot(),
            policy=self.policy,
            policy_sha256=self.policy_hash,
            output_directory=output,
            captured_at=self.prediction_at + timedelta(hours=2),
        )
        self.assertEqual("updated", first["status"])
        self.assertEqual(1, first["evidence_records"])
        self.assertEqual(1, first["economically_executable_records"])
        self.assertEqual(1, first["coverage_universe_records"])
        coverage_path = next((output / "coverage_universe").rglob("*.json"))
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        self.assertTrue(coverage["market_evidence_available"])
        self.assertEqual(
            {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            coverage["model_probabilities"],
        )
        self.assertFalse(coverage["contains_realized_result_or_performance"])
        evidence_path = next((output / "evidence").rglob("*.json"))
        original = evidence_path.read_bytes()

        self.warehouse.connection.execute(
            "UPDATE orderbook_snapshot SET best_ask=best_ask+0.01"
        )
        self.warehouse.connection.execute(
            "UPDATE orderbook_level SET price=price+0.01 WHERE side='ask'"
        )
        second = capture_polymarket_market_evidence(
            self.warehouse.connection,
            snapshot=self.snapshot(),
            policy=self.policy,
            policy_sha256=self.policy_hash,
            output_directory=output,
            captured_at=self.prediction_at + timedelta(hours=3),
        )
        self.assertEqual("no_new_evidence", second["status"])
        self.assertEqual(original, evidence_path.read_bytes())
        stored = json.loads(original)
        self.assertFalse(stored["contains_realized_result_or_performance"])
        self.assertFalse(stored["trading_action_performed"])

    def test_book_at_exact_prediction_cutoff_is_ineligible(self) -> None:
        self.warehouse.connection.execute(
            "UPDATE orderbook_snapshot SET retrieved_at=?", [self.prediction_at]
        )
        receipt = capture_polymarket_market_evidence(
            self.warehouse.connection,
            snapshot=self.snapshot(),
            policy=self.policy,
            policy_sha256=self.policy_hash,
            output_directory=self.root / "late",
            captured_at=self.prediction_at + timedelta(hours=2),
        )
        self.assertEqual(0, receipt["evidence_records"])
        self.assertEqual(1, receipt["exclusion_counts"]["pre_cutoff_book_missing"])
        self.assertEqual(1, receipt["coverage_universe_records"])
        coverage = json.loads(
            next((self.root / "late" / "coverage_universe").rglob("*.json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(coverage["market_evidence_available"])
        self.assertEqual("pre_cutoff_book_missing", coverage["exclusion_reason"])


if __name__ == "__main__":
    unittest.main()
