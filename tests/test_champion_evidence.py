from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from soccer_bot.champion_evidence import (
    ChampionEvidenceError,
    freeze_first_valid_predictions,
)


class ChampionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.cutoff = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        self.kickoff = self.cutoff + timedelta(hours=24)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def row(self, *, home_probability: float = 0.5) -> dict:
        return {
            "model_version": "regulation_champion_v1",
            "fixture_id": "fixture-1",
            "information_state": "pre_lineup_24h_v1",
            "prediction_at": self.cutoff.isoformat(),
            "kickoff": self.kickoff.isoformat(),
            "competition_id": "competition-1",
            "season_id": "season-1",
            "home_team_id": "home",
            "away_team_id": "away",
            "expected_home_goals": 1.4,
            "expected_away_goals": 1.0,
            "raw_home_win_probability": 0.5,
            "raw_draw_probability": 0.3,
            "raw_away_win_probability": 0.2,
            "home_win_probability": home_probability,
            "draw_probability": 0.3,
            "away_win_probability": 1.0 - home_probability - 0.3,
            "home_history_matches": 10,
            "away_history_matches": 10,
            "home_xg_history": 8,
            "away_xg_history": 8,
            "home_shots_history": 9,
            "away_shots_history": 9,
            "source_max_retrieved_at": (
                self.cutoff - timedelta(minutes=1)
            ).isoformat(),
            "warnings": [],
        }

    def freeze(self, rows: list[dict], *, as_of: datetime | None = None):
        effective_as_of = as_of or self.cutoff + timedelta(minutes=2)
        return freeze_first_valid_predictions(
            output_directory=self.output,
            predictions=rows,
            as_of=effective_as_of,
            created_at=effective_as_of + timedelta(seconds=1),
            model_version="regulation_champion_v1",
            logical_model_sha256="a" * 64,
            strict_prediction_at_start=self.cutoff - timedelta(days=1),
            maximum_issue_delay=timedelta(minutes=10),
            issuance_policy_version="immutable_champion_forecast_v1",
            availability_policy_version="forward_observation_availability_v1",
        )

    def test_first_prediction_is_returned_forever_even_if_recomputed_value_changes(self):
        first, first_audit = self.freeze([self.row(home_probability=0.5)])
        second, second_audit = self.freeze([self.row(home_probability=0.6)])

        self.assertEqual(first, second)
        self.assertEqual(first_audit["new_frozen_predictions"], 1)
        self.assertEqual(second_audit["existing_frozen_predictions"], 1)
        self.assertEqual(first[0]["issuance_status"], "strict_forward_frozen")
        self.assertEqual(len(list((self.output / "evidence").glob("*.json"))), 1)

    def test_missed_strict_horizon_is_skipped_instead_of_backdated(self):
        rows, audit = self.freeze(
            [self.row()], as_of=self.cutoff + timedelta(minutes=11)
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            audit["skipped_predictions"][0]["reason"],
            "strict_issue_window_missed",
        )

    def test_observation_retrieved_after_cutoff_is_rejected(self):
        row = self.row()
        row["source_max_retrieved_at"] = (
            self.cutoff + timedelta(seconds=1)
        ).isoformat()

        with self.assertRaisesRegex(ChampionEvidenceError, "retrieved after"):
            self.freeze([row])


if __name__ == "__main__":
    unittest.main()
