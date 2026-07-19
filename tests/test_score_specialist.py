from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from soccer_bot.modeling.score_specialist import (
    RegulationScoreSpecialistModel,
    ScoreSpecialistError,
    ScoreSpecialistHorizon,
    dump_score_specialist_model,
    load_score_specialist_config,
    load_score_specialist_model,
    score_specialist_sha256,
    specialist_score_grid,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "models" / "regulation_score_specialist_v1.json"


class RegulationScoreSpecialistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_score_specialist_config(CONFIG_PATH)
        parameters = {
            name: 0.02 * (index + 1)
            for index, name in enumerate(self.config.candidate.feature_names)
        }
        self.model = RegulationScoreSpecialistModel(
            model_version=self.config.model_version,
            status=self.config.status,
            parent_rate_model_version=self.config.parent_rate_model_version,
            model_family=self.config.model_family,
            training_kickoff_end_exclusive=(
                self.config.training_kickoff_end_exclusive
            ),
            prospective_holdout_start=self.config.prospective_holdout_start,
            horizons=tuple(
                ScoreSpecialistHorizon(
                    information_state=state,
                    training_fixtures=6000,
                    training_kickoff_start=datetime(
                        2020, 1, 1, tzinfo=timezone.utc
                    ),
                    training_kickoff_end_exclusive=(
                        self.config.training_kickoff_end_exclusive
                    ),
                    parameters=parameters,
                    converged=True,
                    iterations=4,
                    objective=1.0,
                )
                for state in self.config.information_states
            ),
        )

    def test_frozen_specialist_explicitly_allows_moneyline_disagreement(self) -> None:
        grid = specialist_score_grid(
            self.model,
            self.config,
            information_state="pre_lineup_24h_v1",
            expected_home_goals=1.6,
            expected_away_goals=1.1,
        )

        self.assertAlmostEqual(sum(grid.values()), 1.0, places=12)
        self.assertGreater(len(grid), 100)

    def test_artifact_round_trip_preserves_logical_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_score_specialist_model(
                self.model,
                path,
                created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
            )

            loaded = load_score_specialist_model(path)

        self.assertEqual(loaded, self.model)
        self.assertEqual(
            score_specialist_sha256(loaded), score_specialist_sha256(self.model)
        )

    def test_tampered_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_score_specialist_model(
                self.model,
                path,
                created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
            )
            text = path.read_text(encoding="utf-8").replace(
                '"training_fixtures": 6000', '"training_fixtures": 6001', 1
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ScoreSpecialistError, "hash mismatch"):
                load_score_specialist_model(path)

    def test_model_rejects_unknown_horizon(self) -> None:
        with self.assertRaisesRegex(ScoreSpecialistError, "Unsupported"):
            specialist_score_grid(
                self.model,
                self.config,
                information_state="confirmed_lineup_v1",
                expected_home_goals=1.2,
                expected_away_goals=1.0,
            )


if __name__ == "__main__":
    unittest.main()
