from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from scripts.check_public_prediction_health import (
    PublicPredictionHealthError,
    evaluate_public_snapshot,
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


if __name__ == "__main__":
    unittest.main()
