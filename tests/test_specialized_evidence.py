from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.specialized_evidence import (
    SpecializedEvidenceError,
    materialize_specialized_evidence,
    validate_specialized_evidence,
)


class SpecializedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.output = Path(self.tempdir.name)
        self.as_of = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
        self.kickoff = self.as_of + timedelta(hours=5)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def snapshot(self, *, probability: float = 0.4) -> dict:
        family = {
            "family_key": "corners",
            "display_name": "Corners",
            "status": "experimental",
            "model_version": "joint_corners_v1",
            "logical_model_sha256": "c" * 64,
            "eligible_for_ranking": False,
            "unavailable_reason": None,
            "evidence": {
                "prospective_holdout_start": "2026-07-21T00:00:00+00:00",
                "warnings": ["experimental_not_eligible_for_automatic_ranking"],
            },
            "markets": [
                {
                    "market_id": "match_corner_total:over:9.5",
                    "contract_key": "match_corner_total",
                    "group": "Match corners",
                    "label": "Over 9.5",
                    "selection": {"side": "over", "line": 9.5},
                    "line": 9.5,
                    "probability": probability,
                    "fair_decimal_multiplier": 1 / probability,
                    "settlement_probabilities": None,
                    "market_comparison": None,
                }
            ],
        }
        state = {
            "fixture_id": "fixture-1",
            "fixture": {"fixture_id": "fixture-1"},
            "kickoff": self.kickoff.isoformat(),
            "prediction_at": (self.kickoff - timedelta(hours=24)).isoformat(),
            "issued_at": (self.as_of + timedelta(minutes=1)).isoformat(),
            "information_state": "pre_lineup_24h_v1",
            "families": [family],
        }
        encoded = json.dumps(
            [state], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return {
            "snapshot_version": "specialized_bet_platform_snapshot_v1",
            "created_at": (self.as_of + timedelta(minutes=1)).isoformat(),
            "as_of": self.as_of.isoformat(),
            "family_registry_version": "specialized_family_registry_v1",
            "ranking_policy": "validated_families_only",
            "states": [state],
            "state_rows_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "source_hashes": {"corner_model": "a" * 64},
        }

    def test_first_experimental_forecast_is_immutable(self) -> None:
        first = materialize_specialized_evidence(
            output_directory=self.output, snapshot=self.snapshot()
        )
        second = materialize_specialized_evidence(
            output_directory=self.output, snapshot=self.snapshot(probability=0.9)
        )
        path = next((self.output / "forward_evidence").glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(first["new_evidence"], 1)
        self.assertEqual(second["new_evidence"], 0)
        self.assertEqual(second["existing_evidence"], 1)
        self.assertEqual(stored["family"]["markets"][0]["probability"], 0.4)
        validate_specialized_evidence(stored)

    def test_tampered_evidence_fails_closed(self) -> None:
        materialize_specialized_evidence(
            output_directory=self.output, snapshot=self.snapshot()
        )
        path = next((self.output / "forward_evidence").glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["family"]["markets"][0]["probability"] = 0.99

        with self.assertRaisesRegex(SpecializedEvidenceError, "record hash"):
            validate_specialized_evidence(stored)

    def test_pre_holdout_forecast_is_rejected(self) -> None:
        snapshot = self.snapshot()
        snapshot["as_of"] = "2026-07-20T23:59:00+00:00"
        with self.assertRaisesRegex(SpecializedEvidenceError, "predates holdout"):
            materialize_specialized_evidence(
                output_directory=self.output, snapshot=snapshot
            )


if __name__ == "__main__":
    unittest.main()
