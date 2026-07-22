from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.history_store import HistoryStore, HistoryValidationError
from apps.api.platform_store import (
    PlatformSnapshotStore,
    PlatformSnapshotValidationError,
)
from apps.api.snapshot_store import (
    S3SnapshotStore,
    SnapshotStore,
    SnapshotValidationError,
)
from soccer_bot.prediction_integrity import champion_prediction_rows_sha256


def sample_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    snapshot = {
        "snapshot_version": "upcoming_regulation_moneyline_snapshot_v2",
        "model_version": "regulation_champion_v1",
        "logical_model_sha256": "logical-hash",
        "prediction_rows_sha256": "",
        "created_at": now.isoformat(),
        "as_of": now.isoformat(),
        "supported_output": "regulation_moneyline",
        "distribution_limitation": "not_score_grid_coherent",
        "training_evidence": {
            "horizon_training_fixtures": {
                "pre_lineup_24h_v1": 38_445,
                "pre_lineup_72h_clean_v1": 34_813,
            },
            "minimum_training_fixtures": 1_000,
            "team_cold_start_below_matches": 5,
            "full_signal_history_matches": 20,
        },
        "source_snapshot": {"warehouse": "/private/warehouse.duckdb"},
        "predictions": [
            {
                "fixture_id": "fixture-1",
                "fixture": {
                    "fixture_id": "fixture-1",
                    "home_team_name": "Home",
                    "away_team_name": "Away",
                    "competition_name": "Competition",
                },
                "kickoff": (now + timedelta(days=1)).isoformat(),
                "prediction_at": now.isoformat(),
                "information_state": "pre_lineup_24h_v1",
                "model_version": "regulation_champion_v1",
                "competition_id": "competition-1",
                "season_id": "season-1",
                "home_team_id": "team-home",
                "away_team_id": "team-away",
                "home_win_probability": 0.5,
                "draw_probability": 0.3,
                "away_win_probability": 0.2,
                "raw_home_win_probability": 0.52,
                "raw_draw_probability": 0.28,
                "raw_away_win_probability": 0.2,
                "expected_home_goals": 1.5,
                "expected_away_goals": 0.9,
                "home_history_matches": 10,
                "away_history_matches": 9,
                "home_xg_history": 7,
                "away_xg_history": 6,
                "home_shots_history": 10,
                "away_shots_history": 9,
                "warnings": [],
            }
        ],
    }
    snapshot["prediction_rows_sha256"] = champion_prediction_rows_sha256(
        snapshot["predictions"]
    )
    return snapshot


def sample_v3_snapshot() -> dict:
    snapshot = sample_snapshot()
    snapshot["snapshot_version"] = "upcoming_regulation_moneyline_snapshot_v3"
    snapshot["model_reproducibility_sha256"] = "f" * 64
    snapshot["availability_policy"] = {
        "policy_version": "forward_observation_availability_v1"
    }
    snapshot["issuance_policy"] = {
        "policy_version": "immutable_champion_forecast_v1"
    }
    row = snapshot["predictions"][0]
    prediction_at = datetime.fromisoformat(row["prediction_at"])
    row.update(
        {
            "source_max_retrieved_at": (
                prediction_at - timedelta(minutes=1)
            ).isoformat(),
            "issued_at": prediction_at.isoformat(),
            "issuance_status": "strict_forward_frozen",
            "issuance_policy_version": "immutable_champion_forecast_v1",
            "availability_policy_version": "forward_observation_availability_v1",
        }
    )
    row["immutable_prediction_sha256"] = champion_prediction_rows_sha256([row])
    snapshot["prediction_rows_sha256"] = champion_prediction_rows_sha256(
        snapshot["predictions"]
    )
    return snapshot


def sample_platform_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    state = {
        "fixture_id": "fixture-1",
        "fixture": {
            "fixture_id": "fixture-1",
            "home_team_name": "Home",
            "away_team_name": "Away",
            "competition_name": "Competition",
        },
        "kickoff": (now + timedelta(days=1)).isoformat(),
        "prediction_at": now.isoformat(),
        "issued_at": now.isoformat(),
        "information_state": "pre_lineup_24h_v1",
        "families": [
            {
                "family_key": "regulation_moneyline",
                "display_name": "Match result",
                "status": "validated",
                "model_version": "regulation_champion_v1",
                "logical_model_sha256": "f" * 64,
                "eligible_for_ranking": True,
                "unavailable_reason": None,
                "evidence": {"warnings": []},
                "markets": [
                    {
                        "market_id": "regulation_moneyline:home_win",
                        "contract_key": "regulation_moneyline",
                        "group": "Match result",
                        "label": "Home",
                        "selection": {"outcome": "home_win"},
                        "line": None,
                        "probability": 0.5,
                        "fair_decimal_multiplier": 2.0,
                        "settlement_probabilities": None,
                        "market_comparison": None,
                    }
                ],
            },
            {
                "family_key": "corners",
                "display_name": "Corners",
                "status": "experimental",
                "model_version": "joint_corners_v1",
                "logical_model_sha256": "e" * 64,
                "eligible_for_ranking": False,
                "unavailable_reason": None,
                "evidence": {"warnings": ["experimental"]},
                "markets": [
                    {
                        "market_id": "match_corner_total:over:9.5",
                        "contract_key": "match_corner_total",
                        "group": "Match corners",
                        "label": "Over 9.5",
                        "selection": {"side": "over", "line": 9.5},
                        "line": 9.5,
                        "probability": 0.4,
                        "fair_decimal_multiplier": 2.5,
                        "settlement_probabilities": {
                            "win": 0.4,
                            "half_win": 0.0,
                            "push": 0.0,
                            "half_loss": 0.0,
                            "loss": 0.6,
                        },
                        "market_comparison": None,
                    }
                ],
            },
        ],
    }
    encoded = json.dumps(
        [state], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        "snapshot_version": "specialized_bet_platform_snapshot_v1",
        "created_at": now.isoformat(),
        "as_of": now.isoformat(),
        "family_registry_version": "specialized_family_registry_v1",
        "market_comparison_status": "unavailable",
        "ranking_policy": "validated_families_only",
        "states": [state],
        "models": {},
        "state_rows_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }


def sample_history() -> dict:
    now = datetime.now(timezone.utc)
    fixture = {
        "fixture_id": "fixture-history-1",
        "kickoff": (now - timedelta(days=1)).isoformat(),
        "competition_id": "competition-1",
        "competition_name": "Competition",
        "home_team_name": "Home",
        "away_team_name": "Away",
        "result": {
            "status": "settled",
            "home_goals": 2,
            "away_goals": 1,
            "outcome": "home_win",
            "settled_at": now.isoformat(),
        },
        "prediction_groups": [
            {
                "prediction_key": "prediction-key",
                "evidence_classification": "published_forward",
                "evidence_label": "PUBLISHED BEFORE KICKOFF",
                "family_key": "regulation_score",
                "display_name": "Score and goals",
                "model_version": "score-model",
                "logical_model_sha256": "a" * 64,
                "model_status_at_prediction": "experimental",
                "information_state": "pre_lineup_24h_v1",
                "prediction_at": (now - timedelta(days=2)).isoformat(),
                "first_published_at": (now - timedelta(days=2)).isoformat(),
                "eligible_for_performance_claim": False,
                "expected_home_goals": 1.7,
                "expected_away_goals": 1.1,
                "warnings": [],
                "markets": [
                    {
                        "market_id": "moneyline:home_win",
                        "group": "Match result",
                        "label": "Home",
                        "probability": 0.5,
                        "fair_decimal_multiplier": 2.0,
                        "settlement_probabilities": None,
                        "realized_settlement": "win",
                        "market_comparison": None,
                    }
                ],
            }
        ],
    }
    encoded = json.dumps([fixture], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "history_version": "published_prediction_history_v1",
        "generated_at": now.isoformat(),
        "as_of": now.isoformat(),
        "fixture_count": 1,
        "prediction_group_count": 1,
        "excluded_ineligible_records": 0,
        "ledger_head_sha256": "b" * 64,
        "history_rows_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "bookmaker_readiness": {
            "status": "collecting",
            "settled_timestamp_safe_quotes": 0,
            "settled_fixture_horizons": 0,
            "calendar_months": 0,
            "minimum_settled_fixture_horizons": 500,
            "minimum_calendar_months": 3,
            "performance_statistics_exposed": False,
            "gate_policy": "minimums_frozen_before_first_forward_comparison",
            "comparison": None,
        },
        "fixtures": [fixture],
    }


class PredictionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "latest.json"
        self.path.write_text(json.dumps(sample_snapshot()), encoding="utf-8")
        self.platform_path = Path(self.tempdir.name) / "platform.json"
        self.platform_path.write_text(
            json.dumps(sample_platform_snapshot()), encoding="utf-8"
        )
        self.history_path = Path(self.tempdir.name) / "history.json"
        self.history_path.write_text(json.dumps(sample_history()), encoding="utf-8")
        self.client = TestClient(
            create_app(
                SnapshotStore(self.path),
                PlatformSnapshotStore(self.platform_path),
                HistoryStore(self.history_path),
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def test_snapshot_strips_private_source_paths(self) -> None:
        response = self.client.get("/v1/snapshot")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("source_snapshot", payload)
        self.assertEqual(payload["fixture_count"], 1)
        self.assertFalse(payload["is_stale"])
        self.assertEqual(
            payload["training_evidence"]["horizon_training_fixtures"]
            ["pre_lineup_24h_v1"],
            38_445,
        )

    def test_snapshot_freshness_uses_data_as_of_not_publish_time(self) -> None:
        value = sample_snapshot()
        value["as_of"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        value["created_at"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with TestClient(create_app(SnapshotStore(self.path))) as client:
            payload = client.get("/v1/snapshot").json()
        self.assertTrue(payload["is_stale"])

    def test_invalid_training_evidence_fails_closed(self) -> None:
        value = sample_snapshot()
        value["training_evidence"]["minimum_training_fixtures"] = 0
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(SnapshotValidationError):
            SnapshotStore(self.path).load()

    def test_liveness_does_not_require_snapshot_io(self) -> None:
        missing_store = SnapshotStore(Path(self.tempdir.name) / "missing.json")
        with TestClient(create_app(missing_store)) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")
            self.assertEqual(client.get("/ready").status_code, 503)

    def test_prices_supported_selection(self) -> None:
        response = self.client.post(
            "/v1/price",
            json={
                "fixture_id": "fixture-1",
                "information_state": "pre_lineup_24h_v1",
                "contract_key": "regulation_moneyline",
                "selection": "draw",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["probability"], 0.3)
        self.assertEqual(response.json()["fair_decimal_odds"], 3.3333)

    def test_rejects_unsupported_contract(self) -> None:
        response = self.client.post(
            "/v1/price",
            json={
                "fixture_id": "fixture-1",
                "information_state": "pre_lineup_24h_v1",
                "contract_key": "regulation_total_goals",
                "selection": "draw",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_fixture_not_found(self) -> None:
        response = self.client.get("/v1/fixtures/missing")
        self.assertEqual(response.status_code, 404)

    def test_platform_snapshot_and_generic_price(self) -> None:
        snapshot = self.client.get("/v2/platform-snapshot")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["fixture_count"], 1)
        response = self.client.post(
            "/v2/price",
            json={
                "fixture_id": "fixture-1",
                "information_state": "pre_lineup_24h_v1",
                "family_key": "corners",
                "market_id": "match_corner_total:over:9.5",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["market"]["fair_decimal_multiplier"], 2.5)
        self.assertFalse(response.json()["eligible_for_ranking"])

    def test_published_history_list_and_detail(self) -> None:
        response = self.client.get("/v2/history?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fixture_count"], 1)
        self.assertEqual(response.json()["fixtures"][0]["result"]["home_goals"], 2)
        detail = self.client.get("/v2/history/fixture-history-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["fixture"]["prediction_groups"][0]["markets"][0]["realized_settlement"], "win")

    def test_published_history_rejects_post_kickoff_publication(self) -> None:
        value = sample_history()
        value["fixtures"][0]["prediction_groups"][0]["first_published_at"] = value["fixtures"][0]["result"]["settled_at"]
        encoded = json.dumps(value["fixtures"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        value["history_rows_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        self.history_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(HistoryValidationError):
            HistoryStore(self.history_path).load()

    def test_platform_freshness_is_recomputed_on_cache_hits(self) -> None:
        value = sample_platform_snapshot()
        value["as_of"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        store = PlatformSnapshotStore(self.platform_path)

        with patch.dict("os.environ", {"SOCCER_SNAPSHOT_STALE_SECONDS": "1"}):
            first = store.load()
            second = store.load()

        self.assertTrue(first["is_stale"])
        self.assertTrue(second["is_stale"])
        self.assertGreaterEqual(second["snapshot_age_seconds"], 2)

    def test_platform_rejects_experimental_ranking(self) -> None:
        value = sample_platform_snapshot()
        value["states"][0]["families"][1]["eligible_for_ranking"] = True
        encoded = json.dumps(
            value["states"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        value["state_rows_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(PlatformSnapshotValidationError):
            PlatformSnapshotStore(self.platform_path).load()

    def test_platform_context_is_public_and_rejects_post_cutoff_results(self) -> None:
        value = sample_platform_snapshot()
        state = value["states"][0]
        cutoff = datetime.fromisoformat(state["prediction_at"])
        match = {
            "fixture_id": "previous",
            "kickoff": (cutoff - timedelta(days=4)).isoformat(),
            "available_at": (cutoff - timedelta(days=3, hours=21)).isoformat(),
            "competition_name": "Competition",
            "opponent_name": "Opponent",
            "venue": "home",
            "neutral_venue": False,
            "team_score": 2,
            "opponent_score": 1,
            "outcome": "win",
        }
        trend = {
            "sample_size": 1,
            "wins": 1,
            "draws": 0,
            "losses": 0,
            "goals_for_per_match": 2.0,
            "goals_against_per_match": 1.0,
            "clean_sheet_rate": 0.0,
            "both_teams_scored_rate": 1.0,
        }
        team = {
            "team_id": "team-home",
            "rest_days": 4.0,
            "matches_last_7d": 1,
            "matches_last_14d": 1,
            "matches_last_30d": 1,
            "recent_matches": [match],
            "trends": {"last_5": trend, "last_10": trend},
        }
        state["match_context"] = {
            "cutoff_at": state["prediction_at"],
            "home": team,
            "away": {**team, "team_id": "team-away"},
        }
        state["model_expectation"] = {
            "expected_home_goals": 1.5,
            "expected_away_goals": 0.9,
        }
        value["state_rows_sha256"] = hashlib.sha256(
            json.dumps(
                value["states"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        loaded = PlatformSnapshotStore(self.platform_path).load()
        self.assertEqual(
            loaded["states"][0]["match_context"]["home"]["recent_matches"][0]["team_score"],
            2,
        )

        state["match_context"]["home"]["recent_matches"][0]["available_at"] = (
            cutoff + timedelta(seconds=1)
        ).isoformat()
        value["state_rows_sha256"] = hashlib.sha256(
            json.dumps(
                value["states"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(PlatformSnapshotValidationError):
            PlatformSnapshotStore(self.platform_path).load()

    def test_platform_accepts_bookmaker_consensus_and_rejects_polymarket_quote(self) -> None:
        value = sample_platform_snapshot()
        market = value["states"][0]["families"][0]["markets"][0]
        market["live_market"] = None
        market["market_comparison"] = {
            "source": "api_football",
            "quote_type": "cutoff_consensus",
            "market_probability": 0.5,
            "market_decimal_multiplier": 2.0,
            "bookmaker_count": 5,
            "consensus_method": "median_proportional_devig",
            "observed_at": value["as_of"],
            "retrieved_at": value["as_of"],
        }
        value["state_rows_sha256"] = hashlib.sha256(
            json.dumps(
                value["states"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        loaded = PlatformSnapshotStore(self.platform_path).load()
        self.assertEqual(
            "api_football",
            loaded["states"][0]["families"][0]["markets"][0][
                "market_comparison"
            ]["source"],
        )

        market["market_comparison"]["source"] = "polymarket"
        value["state_rows_sha256"] = hashlib.sha256(
            json.dumps(
                value["states"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.platform_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(PlatformSnapshotValidationError):
            PlatformSnapshotStore(self.platform_path).load()

    def test_invalid_probability_sum_fails_closed(self) -> None:
        value = sample_snapshot()
        value["predictions"][0]["home_win_probability"] = 0.6
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(SnapshotValidationError):
            SnapshotStore(self.path).load()

    def test_changed_prediction_with_stale_hash_fails_closed(self) -> None:
        value = sample_snapshot()
        value["predictions"][0]["home_win_probability"] = 0.51
        value["predictions"][0]["draw_probability"] = 0.29
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(SnapshotValidationError, "SHA-256 mismatch"):
            SnapshotStore(self.path).load()

    def test_v3_immutable_forward_snapshot_is_accepted(self) -> None:
        self.path.write_text(json.dumps(sample_v3_snapshot()), encoding="utf-8")

        value = SnapshotStore(self.path).load()

        self.assertEqual(
            value["snapshot_version"],
            "upcoming_regulation_moneyline_snapshot_v3",
        )

    def test_v3_rejects_data_retrieved_after_the_forecast_cutoff(self) -> None:
        value = sample_v3_snapshot()
        row = value["predictions"][0]
        prediction_at = datetime.fromisoformat(row["prediction_at"])
        row["source_max_retrieved_at"] = (
            prediction_at + timedelta(seconds=1)
        ).isoformat()
        unhashed = dict(row)
        unhashed.pop("immutable_prediction_sha256")
        row["immutable_prediction_sha256"] = champion_prediction_rows_sha256(
            [unhashed]
        )
        value["prediction_rows_sha256"] = champion_prediction_rows_sha256(
            value["predictions"]
        )
        self.path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(SnapshotValidationError, "retrieved after"):
            SnapshotStore(self.path).load()

    def test_invalid_ui_evidence_fails_closed(self) -> None:
        value = sample_snapshot()
        value["predictions"][0]["warnings"] = [""]
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(SnapshotValidationError):
            SnapshotStore(self.path).load()

    def test_s3_store_validates_and_caches_snapshot(self) -> None:
        class Body:
            def read(self) -> bytes:
                return json.dumps(sample_snapshot()).encode("utf-8")

        class Client:
            calls = 0

            def get_object(self, **kwargs):
                self.calls += 1
                self.kwargs = kwargs
                return {"Body": Body(), "ETag": '"snapshot-etag"'}

        client = Client()
        store = S3SnapshotStore(
            client=client,
            bucket="predictions",
            key="champion/latest.json",
            cache_seconds=60,
        )
        first = store.load()
        second = store.load()
        self.assertEqual(client.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(client.kwargs["Bucket"], "predictions")
        self.assertNotIn("source_snapshot", first)


if __name__ == "__main__":
    unittest.main()
