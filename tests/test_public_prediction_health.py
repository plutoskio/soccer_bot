from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from scripts.check_public_prediction_health import (
    PublicPredictionHealthError,
    evaluate_public_platform_snapshot,
    evaluate_public_snapshot,
    extract_public_platform_snapshot,
    extract_public_snapshot,
)


MODEL_HASH = "8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a"


def heartbeat(
    as_of: str, *, model_hash: str = MODEL_HASH, predictions: int = 25
) -> str:
    return json.dumps(
        {
            "heartbeat_version": "public_prediction_heartbeat_v1",
            "model_version": "regulation_champion_v1",
            "logical_model_sha256": model_hash,
            "as_of": as_of,
            "prediction_count": predictions,
            "fixture_count": 15,
        }
    )


def family_registry() -> dict:
    return {
        "registry_version": "specialized_family_registry_v1",
        "families": [
            {
                "family_key": "regulation_moneyline",
                "models": [
                    {
                        "model_version": "regulation_champion_v1",
                        "logical_sha256": MODEL_HASH,
                    }
                ],
            },
            {
                "family_key": "player_events",
                "models": [
                    {
                        "model_version": "confirmed_lineup_player_v1",
                        "logical_sha256": "1" * 64,
                    }
                ],
            },
        ],
    }


def platform_snapshot(as_of: str) -> str:
    return json.dumps(
        {
            "snapshot_version": "specialized_bet_platform_snapshot_v1",
            "as_of": as_of,
            "family_registry_version": "specialized_family_registry_v1",
            "ranking_policy": "validated_families_only",
            "state_count": 20,
            "fixture_count": 16,
            "state_rows_sha256": "2" * 64,
            "available_information_states": ["pre_lineup_24h_v1"],
            "models": {
                "regulation_moneyline": {
                    "model_version": "regulation_champion_v1",
                    "logical_sha256": MODEL_HASH,
                    "status": "validated",
                },
                "player_events": {
                    "model_version": "confirmed_lineup_player_v1",
                    "status": "unavailable",
                },
            },
        }
    )


class PublicPredictionHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)

    def evaluate(self, page: str) -> dict:
        return evaluate_public_snapshot(
            extract_public_snapshot(page),
            expected_model_version="regulation_champion_v1",
            expected_logical_hash=MODEL_HASH,
            stale_after_seconds=1200,
            now=self.now,
        )

    def test_fresh_expected_snapshot_passes(self) -> None:
        result = self.evaluate(
            heartbeat((self.now - timedelta(minutes=5)).isoformat())
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["prediction_count"], 25)

    def test_stale_snapshot_fails(self) -> None:
        result = self.evaluate(
            heartbeat((self.now - timedelta(minutes=21)).isoformat())
        )

        self.assertIn("public_champion_snapshot_stale", result["failures"])

    def test_identity_and_zero_rows_fail(self) -> None:
        result = self.evaluate(
            heartbeat(self.now.isoformat(), model_hash="0" * 64, predictions=0)
        )

        self.assertIn("public_logical_model_hash_mismatch", result["failures"])
        self.assertIn("public_prediction_rows_zero", result["failures"])

    def test_missing_or_invalid_metadata_fails_closed(self) -> None:
        with self.assertRaises(PublicPredictionHealthError):
            extract_public_snapshot("<html>loading</html>")
        page = json.dumps(
            {
                "heartbeat_version": "public_prediction_heartbeat_v1",
                "model_version": "regulation_champion_v1",
            }
        )
        with self.assertRaises(PublicPredictionHealthError):
            extract_public_snapshot(page)

    def test_fresh_platform_snapshot_and_registered_models_pass(self) -> None:
        page = platform_snapshot((self.now - timedelta(minutes=5)).isoformat())

        result = evaluate_public_platform_snapshot(
            extract_public_platform_snapshot(page),
            family_registry=family_registry(),
            expected_model_version="regulation_champion_v1",
            expected_logical_hash=MODEL_HASH,
            stale_after_seconds=1200,
            now=self.now,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failures"], [])

    def test_platform_staleness_and_model_drift_fail(self) -> None:
        value = json.loads(
            platform_snapshot((self.now - timedelta(minutes=21)).isoformat())
        )
        value["models"]["regulation_moneyline"]["logical_sha256"] = "0" * 64
        value["models"]["player_events"]["model_version"] = "unknown_player"

        result = evaluate_public_platform_snapshot(
            extract_public_platform_snapshot(json.dumps(value)),
            family_registry=family_registry(),
            expected_model_version="regulation_champion_v1",
            expected_logical_hash=MODEL_HASH,
            stale_after_seconds=1200,
            now=self.now,
        )

        self.assertIn("public_platform_snapshot_stale", result["failures"])
        self.assertIn(
            "public_platform_champion_hash_mismatch", result["failures"]
        )
        self.assertIn(
            "public_platform_model_unregistered:player_events", result["failures"]
        )

    def test_platform_contract_fails_closed(self) -> None:
        with self.assertRaises(PublicPredictionHealthError):
            extract_public_platform_snapshot("[]")
        value = json.loads(platform_snapshot(self.now.isoformat()))
        value["state_rows_sha256"] = "short"
        with self.assertRaises(PublicPredictionHealthError):
            extract_public_platform_snapshot(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
