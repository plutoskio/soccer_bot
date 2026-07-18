from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
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


class PredictionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "latest.json"
        self.path.write_text(json.dumps(sample_snapshot()), encoding="utf-8")
        self.client = TestClient(create_app(SnapshotStore(self.path)))

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
