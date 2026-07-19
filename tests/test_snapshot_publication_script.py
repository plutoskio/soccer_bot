from __future__ import annotations

import json
import unittest

from scripts.publish_prediction_snapshot import upload_and_verify
from scripts.publish_platform_snapshot import upload_and_verify as upload_platform
from soccer_bot.prediction_integrity import champion_prediction_rows_sha256


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value


class _Client:
    def __init__(self, *, corrupt_readback: bool = False):
        self.corrupt_readback = corrupt_readback
        self.object: bytes | None = None

    def put_object(self, **kwargs) -> None:
        self.object = kwargs["Body"]

    def get_object(self, **_kwargs) -> dict:
        value = b"{}" if self.corrupt_readback else self.object
        return {"Body": _Body(value)}


def _snapshot() -> dict:
    snapshot = {
        "snapshot_version": "upcoming_regulation_moneyline_snapshot_v2",
        "model_version": "regulation_champion_v1",
        "logical_model_sha256": "a" * 64,
        "prediction_rows_sha256": "",
        "as_of": "2026-07-15T12:00:00+00:00",
        "created_at": "2026-07-15T12:00:01+00:00",
        "supported_output": "regulation_moneyline",
        "training_evidence": {
            "horizon_training_fixtures": {
                "pre_lineup_72h_clean_v1": 34813,
                "pre_lineup_24h_v1": 38445,
            },
            "minimum_training_fixtures": 1000,
            "team_cold_start_below_matches": 5,
            "full_signal_history_matches": 20,
        },
        "predictions": [],
    }
    snapshot["prediction_rows_sha256"] = champion_prediction_rows_sha256(
        snapshot["predictions"]
    )
    return snapshot


class SnapshotPublicationScriptTests(unittest.TestCase):
    def test_upload_is_read_back_and_revalidated(self) -> None:
        snapshot = _snapshot()
        raw = json.dumps(snapshot).encode("utf-8")
        client = _Client()

        upload_and_verify(
            client,
            bucket="bucket",
            key="latest.json",
            raw=raw,
            snapshot=snapshot,
        )

        self.assertEqual(client.object, raw)

    def test_corrupt_readback_is_rejected(self) -> None:
        snapshot = _snapshot()
        raw = json.dumps(snapshot).encode("utf-8")

        with self.assertRaisesRegex(RuntimeError, "byte-for-byte"):
            upload_and_verify(
                _Client(corrupt_readback=True),
                bucket="bucket",
                key="latest.json",
                raw=raw,
                snapshot=snapshot,
            )

    def test_platform_upload_is_read_back_and_revalidated(self) -> None:
        now = "2026-07-15T12:00:00+00:00"
        states: list[dict] = []
        encoded = json.dumps(
            states, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        snapshot = {
            "snapshot_version": "specialized_bet_platform_snapshot_v1",
            "created_at": now,
            "as_of": now,
            "family_registry_version": "specialized_family_registry_v1",
            "ranking_policy": "validated_families_only",
            "states": states,
            "models": {},
            "state_rows_sha256": __import__("hashlib").sha256(
                encoded.encode()
            ).hexdigest(),
        }
        raw = json.dumps(snapshot).encode("utf-8")
        client = _Client()

        upload_platform(
            client,
            bucket="bucket",
            key="specialized/latest.json",
            raw=raw,
            snapshot=snapshot,
        )
        self.assertEqual(client.object, raw)


if __name__ == "__main__":
    unittest.main()
