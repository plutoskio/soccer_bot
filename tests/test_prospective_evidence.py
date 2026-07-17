from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from soccer_bot.prospective_evidence import (
    ProspectiveEvidenceError,
    load_forecast_evidence,
    materialize_legacy_evidence,
    materialize_snapshot_evidence,
    validate_forecast_evidence,
)


MODEL_HASH = "d" * 64


class ProspectiveEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.output = Path(self.tempdir.name)
        self.kickoff = datetime(2026, 7, 18, 18, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def snapshot(self, *, as_of: datetime, expected_home_goals: float = 1.5) -> dict:
        probabilities = {(0, 0): 0.3, (0, 1): 0.3, (1, 0): 0.4}
        grid = [
            {"home_goals": score[0], "away_goals": score[1], "probability": probability}
            for score, probability in sorted(probabilities.items())
        ]
        grid_body = json.dumps(
            [[score[0], score[1], probability] for score, probability in sorted(probabilities.items())],
            separators=(",", ":"),
            allow_nan=False,
        )
        return {
            "snapshot_version": "regulation_score_grid_v3_shadow_snapshot_v1",
            "created_at": (as_of + timedelta(minutes=1)).isoformat(),
            "as_of": as_of.isoformat(),
            "model_version": "regulation_score_grid_v3_prospective_shadow",
            "parent_model_version": "regulation_champion_v1",
            "logical_model_sha256": MODEL_HASH,
            "prospective_gate_version": "regulation_score_grid_v3_prospective_gate_v1",
            "prospective_holdout_start": "2026-07-17T00:00:00+00:00",
            "sources": {
                "parent_snapshot": {"sha256": "a" * 64},
                "shadow_model": {"sha256": "b" * 64},
                "prospective_gate": {"sha256": "c" * 64},
            },
            "predictions": [
                {
                    "fixture_id": "fixture-1",
                    "information_state": "pre_lineup_24h_v1",
                    "prediction_at": (self.kickoff - timedelta(hours=24)).isoformat(),
                    "kickoff": self.kickoff.isoformat(),
                    "expected_home_goals": expected_home_goals,
                    "expected_away_goals": 1.0,
                    "parent_moneyline": {
                        "home_win": 0.4,
                        "draw": 0.3,
                        "away_win": 0.3,
                    },
                    "implied_moneyline": {
                        "home_win": 0.4,
                        "draw": 0.3,
                        "away_win": 0.3,
                    },
                    "score_grid": grid,
                    "score_grid_sha256": hashlib.sha256(
                        grid_body.encode("utf-8")
                    ).hexdigest(),
                }
            ],
        }

    def test_first_valid_forecast_is_immutable_canonical_evidence(self) -> None:
        first_as_of = self.kickoff - timedelta(hours=23, minutes=59)
        first = materialize_snapshot_evidence(
            output_directory=self.output,
            snapshot=self.snapshot(as_of=first_as_of),
        )
        second = materialize_snapshot_evidence(
            output_directory=self.output,
            snapshot=self.snapshot(
                as_of=first_as_of + timedelta(minutes=5), expected_home_goals=9.0
            ),
        )
        evidence = load_forecast_evidence(self.output / "evidence")

        self.assertEqual(first["new_evidence"], 1)
        self.assertEqual(second["new_evidence"], 0)
        self.assertEqual(second["existing_evidence"], 1)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["prediction"]["expected_home_goals"], 1.5)
        self.assertEqual(evidence[0]["first_snapshot_as_of"], first_as_of.isoformat())
        self.assertEqual(len(list((self.output / "receipts").glob("*.json"))), 1)

    def test_legacy_import_selects_oldest_snapshot_once(self) -> None:
        earliest = self.kickoff - timedelta(hours=23, minutes=58)
        later = earliest + timedelta(minutes=5)
        (self.output / "20260717T180000Z.json").write_text(
            json.dumps(self.snapshot(as_of=later, expected_home_goals=9.0)),
            encoding="utf-8",
        )
        (self.output / "20260717T175500Z.json").write_text(
            json.dumps(self.snapshot(as_of=earliest)), encoding="utf-8"
        )

        first = materialize_legacy_evidence(self.output)
        second = materialize_legacy_evidence(self.output)
        evidence = load_forecast_evidence(self.output / "evidence")

        self.assertEqual(first, {"legacy_snapshots": 2, "new_evidence": 1})
        self.assertEqual(second, {"legacy_snapshots": 0, "new_evidence": 0})
        self.assertEqual(evidence[0]["prediction"]["expected_home_goals"], 1.5)

    def test_tampering_with_evidence_is_detected(self) -> None:
        materialize_snapshot_evidence(
            output_directory=self.output,
            snapshot=self.snapshot(as_of=self.kickoff - timedelta(hours=23)),
        )
        path = next((self.output / "evidence").glob("*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        value["prediction"]["expected_home_goals"] = 99

        with self.assertRaisesRegex(ProspectiveEvidenceError, "record hash"):
            validate_forecast_evidence(value)

    def test_invalid_grid_cannot_become_first_immutable_evidence(self) -> None:
        snapshot = self.snapshot(as_of=self.kickoff - timedelta(hours=23))
        snapshot["predictions"][0]["score_grid"][0]["probability"] = -0.1

        with self.assertRaisesRegex(ProspectiveEvidenceError, "probability"):
            materialize_snapshot_evidence(
                output_directory=self.output,
                snapshot=snapshot,
            )
        self.assertEqual(list((self.output / "evidence").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
