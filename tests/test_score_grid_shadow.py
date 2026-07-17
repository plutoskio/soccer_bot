from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.predict_score_grid_v3_shadow import _write_immutable_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.score_grid_shadow import (
    RegulationScoreGridShadowModel,
    ScoreGridShadowError,
    ShadowHorizonParameters,
    dump_score_grid_shadow_model,
    fit_score_grid_shadow,
    load_score_grid_prospective_gate,
    load_score_grid_shadow_config,
    load_score_grid_shadow_model,
    predict_coherent_score_grid,
    score_grid_shadow_sha256,
)


@dataclass(frozen=True)
class RateRow:
    fixture_id: str
    information_state: str
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float


class ResultMarginalPreservingShadowTests(unittest.TestCase):
    def setUp(self):
        loaded = load_score_grid_shadow_config(
            ROOT / "config" / "models" / "regulation_score_grid_v3_shadow.json"
        )
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.config = replace(
            loaded,
            recipe_frozen_at=self.start + timedelta(days=100),
            training_kickoff_end_exclusive=self.start + timedelta(days=90),
            prospective_holdout_start=self.start + timedelta(days=100),
            minimum_fit_fixtures=1,
        )
        self.parent = {"home_win": 0.51, "draw": 0.27, "away_win": 0.22}

    def rows(self) -> list[RateRow]:
        scores = [(1, 0), (2, 0), (2, 1), (0, 0), (1, 1), (2, 2), (0, 1), (1, 2)]
        return [
            RateRow(
                fixture_id=str(index),
                information_state="pre_lineup_24h_v1",
                kickoff=self.start + timedelta(days=index),
                home_goals=score[0],
                away_goals=score[1],
                expected_home_goals=1.5 + 0.02 * index,
                expected_away_goals=1.1 + 0.01 * index,
            )
            for index, score in enumerate(scores, 1)
        ]

    def fitted_model(self) -> RegulationScoreGridShadowModel:
        return fit_score_grid_shadow(self.rows(), self.config)

    def test_fitted_grid_preserves_parent_moneyline_exactly(self):
        model = self.fitted_model()
        grid = predict_coherent_score_grid(
            expected_home_goals=1.8,
            expected_away_goals=1.0,
            parent_moneyline=self.parent,
            information_state="pre_lineup_24h_v1",
            model=model,
        )

        self.assertAlmostEqual(sum(grid.probabilities.values()), 1.0, places=12)
        for outcome, probability in self.parent.items():
            self.assertAlmostEqual(grid.moneyline()[outcome], probability, places=12)
        self.assertTrue(all(value > 0 for value in grid.probabilities.values()))

    def test_prospective_gate_matches_frozen_model_identity(self):
        production_config = load_score_grid_shadow_config(
            ROOT / "config" / "models" / "regulation_score_grid_v3_shadow.json"
        )
        model = fit_score_grid_shadow(
            self.rows(),
            replace(
                production_config,
                training_kickoff_end_exclusive=self.start + timedelta(days=90),
                minimum_fit_fixtures=1,
            ),
        )
        gate = load_score_grid_prospective_gate(
            ROOT
            / "config"
            / "models"
            / "regulation_score_grid_v3_prospective_gate.json",
            model=replace(
                model,
                prospective_holdout_start=production_config.prospective_holdout_start,
            ),
        )
        self.assertEqual(
            gate["status"], "frozen_before_first_eligible_shadow_prediction"
        )

    def test_parent_marginal_changes_do_not_change_within_result_shape(self):
        model = self.fitted_model()
        first = predict_coherent_score_grid(
            expected_home_goals=1.8,
            expected_away_goals=1.0,
            parent_moneyline=self.parent,
            information_state="pre_lineup_24h_v1",
            model=model,
        ).probabilities
        second_parent = {"home_win": 0.42, "draw": 0.31, "away_win": 0.27}
        second = predict_coherent_score_grid(
            expected_home_goals=1.8,
            expected_away_goals=1.0,
            parent_moneyline=second_parent,
            information_state="pre_lineup_24h_v1",
            model=model,
        ).probabilities

        self.assertAlmostEqual(first[(2, 0)] / first[(1, 0)], second[(2, 0)] / second[(1, 0)], places=12)
        self.assertAlmostEqual(first[(1, 1)] / first[(0, 0)], second[(1, 1)] / second[(0, 0)], places=12)
        self.assertAlmostEqual(first[(0, 2)] / first[(0, 1)], second[(0, 2)] / second[(0, 1)], places=12)

    def test_post_cutoff_fit_row_fails_closed(self):
        unsafe = replace(
            self.rows()[0], kickoff=self.config.training_kickoff_end_exclusive
        )
        with self.assertRaisesRegex(
            ScoreGridShadowError, "Post-training-cutoff"
        ):
            fit_score_grid_shadow([unsafe], self.config)

    def test_invalid_parent_moneyline_fails_closed(self):
        model = self.fitted_model()
        with self.assertRaisesRegex(ScoreGridShadowError, "sum to one"):
            predict_coherent_score_grid(
                expected_home_goals=1.5,
                expected_away_goals=1.1,
                parent_moneyline={
                    "home_win": 0.5,
                    "draw": 0.3,
                    "away_win": 0.3,
                },
                information_state="pre_lineup_24h_v1",
                model=model,
            )

    def test_model_round_trip_and_hash_tamper_detection(self):
        model = self.fitted_model()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_score_grid_shadow_model(model, path, created_at=self.start)
            loaded = load_score_grid_shadow_model(path)
            self.assertEqual(score_grid_shadow_sha256(loaded), score_grid_shadow_sha256(model))

            raw = json.loads(path.read_text())
            raw["model"]["horizons"][0]["coefficients"][0] += 0.1
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ScoreGridShadowError, "hash mismatch"):
                load_score_grid_shadow_model(path)

    def test_zero_tilt_still_backpropagates_parent_moneyline(self):
        horizon = ShadowHorizonParameters(
            information_state="pre_lineup_24h_v1",
            training_fixtures=10,
            training_kickoff_start=self.start,
            training_kickoff_end_exclusive=self.config.training_kickoff_end_exclusive,
            feature_names=self.config.feature_names,
            feature_scales=self.config.feature_scales,
            coefficients=tuple(0.0 for _ in self.config.feature_names),
            ridge_penalty=self.config.ridge_penalty,
            converged=True,
            iterations=1,
            penalized_objective=1.0,
        )
        model = RegulationScoreGridShadowModel(
            model_version=self.config.model_version,
            status=self.config.status,
            parent_moneyline_model_version=self.config.parent_moneyline_model_version,
            model_family=self.config.model_family,
            recipe_frozen_at=self.config.recipe_frozen_at,
            training_kickoff_end_exclusive=self.config.training_kickoff_end_exclusive,
            prospective_holdout_start=self.config.prospective_holdout_start,
            poisson_tail_tolerance=self.config.poisson_tail_tolerance,
            minimum_max_goals=self.config.minimum_max_goals,
            maximum_max_goals=self.config.maximum_max_goals,
            normalization_tolerance=self.config.normalization_tolerance,
            horizons=(horizon,),
        )
        grid = predict_coherent_score_grid(
            expected_home_goals=1.5,
            expected_away_goals=1.1,
            parent_moneyline=self.parent,
            information_state="pre_lineup_24h_v1",
            model=model,
        )
        self.assertEqual(set(grid.moneyline()), {"home_win", "draw", "away_win"})
        for outcome in self.parent:
            self.assertAlmostEqual(grid.moneyline()[outcome], self.parent[outcome], places=12)

    def test_timestamped_shadow_snapshot_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "20260717T174045Z.json"
            _write_immutable_json(path, {"value": 1})
            _write_immutable_json(path, {"value": 1})
            with self.assertRaisesRegex(RuntimeError, "Immutable shadow snapshot"):
                _write_immutable_json(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text()), {"value": 1})


if __name__ == "__main__":
    unittest.main()
