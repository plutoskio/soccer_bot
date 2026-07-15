from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.features import RegulationFeatureRow
from soccer_bot.modeling.walk_forward import (
    evaluate_walk_forward,
    load_walk_forward_config,
    prediction_rows_sha256,
    summarize_predictions,
)


def feature_row(
    fixture_id: str,
    kickoff: datetime,
    home_goals: int,
    away_goals: int,
    *,
    information_state: str = "pre_lineup_24h_v1",
    hours_before: int = 24,
    expected_home_goals: float = 1.0,
    expected_away_goals: float = 1.0,
) -> RegulationFeatureRow:
    return RegulationFeatureRow(
        feature_version="test_features_v1",
        fixture_id=fixture_id,
        information_state=information_state,
        prediction_at=kickoff - timedelta(hours=hours_before),
        kickoff=kickoff,
        competition_id="competition",
        season_id="season",
        home_team_id=f"home-{fixture_id}",
        away_team_id=f"away-{fixture_id}",
        neutral_venue=False,
        home_goals=home_goals,
        away_goals=away_goals,
        home_attack_mean=0.0,
        home_attack_std=0.3,
        home_defense_mean=0.0,
        home_defense_std=0.3,
        away_attack_mean=0.0,
        away_attack_std=0.3,
        away_defense_mean=0.0,
        away_defense_std=0.3,
        competition_log_goal_level=0.0,
        competition_log_goal_level_std=0.2,
        competition_home_advantage=0.0,
        competition_home_advantage_std=0.2,
        applied_home_advantage=0.0,
        home_log_matchup_strength=0.0,
        away_log_matchup_strength=0.0,
        expected_home_goals=expected_home_goals,
        expected_away_goals=expected_away_goals,
        home_history_matches=10,
        away_history_matches=10,
        competition_history_matches=100,
        home_rest_days=7.0,
        away_rest_days=7.0,
        rest_difference_days=0.0,
        home_matches_last_7d=1,
        home_matches_last_14d=2,
        home_matches_last_30d=4,
        away_matches_last_7d=1,
        away_matches_last_14d=2,
        away_matches_last_30d=4,
        home_cold_start=False,
        away_cold_start=False,
    )


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        loaded = load_walk_forward_config(
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        )
        self.config = replace(loaded, minimum_training_fixtures=1)
        self.start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)

    def model(self, predictions, fixture_id, model_key="independent_poisson"):
        return next(
            row
            for row in predictions
            if row.fixture_id == fixture_id and row.model_key == model_key
        )

    def probability_signature(self, prediction):
        return (
            prediction.expected_home_goals,
            prediction.expected_away_goals,
            prediction.home_win_probability,
            prediction.draw_probability,
            prediction.away_win_probability,
            prediction.dixon_coles_rho,
        )

    def test_match_n_minus_one_updates_match_n(self):
        first = feature_row("first", self.start, 4, 0)
        second = feature_row("second", self.start + timedelta(days=3), 1, 1)

        prediction = self.model(
            evaluate_walk_forward([first, second], self.config), "second"
        )

        self.assertEqual(prediction.training_fixtures, 1)
        self.assertGreater(prediction.home_rate_scale, 1.0)
        self.assertLess(prediction.away_rate_scale, 1.0)

    def test_target_outcome_cannot_change_its_own_prediction(self):
        first = feature_row("first", self.start, 1, 1)
        target = feature_row("target", self.start + timedelta(days=3), 0, 0)
        changed = replace(target, home_goals=6, away_goals=0)

        baseline = evaluate_walk_forward([first, target], self.config)
        mutated = evaluate_walk_forward([first, changed], self.config)

        for model_key in ("independent_poisson", "dixon_coles"):
            self.assertEqual(
                self.probability_signature(
                    self.model(baseline, "target", model_key)
                ),
                self.probability_signature(
                    self.model(mutated, "target", model_key)
                ),
            )

    def test_unavailable_previous_result_cannot_enter_prediction(self):
        current = feature_row("current", self.start + timedelta(days=3), 1, 0)
        prior = feature_row(
            "prior", current.prediction_at - timedelta(hours=1), 5, 0
        )

        predictions = evaluate_walk_forward([prior, current], self.config)

        self.assertFalse(any(row.fixture_id == "current" for row in predictions))

    def test_result_available_at_exact_prediction_time_is_still_not_visible(self):
        current = feature_row("current", self.start + timedelta(days=3), 1, 0)
        prior = feature_row(
            "prior",
            current.prediction_at
            - timedelta(minutes=self.config.result_availability_delay_minutes),
            5,
            0,
        )

        predictions = evaluate_walk_forward([prior, current], self.config)

        self.assertFalse(any(row.fixture_id == "current" for row in predictions))

    def test_simultaneous_batches_are_input_order_invariant(self):
        first = feature_row("first", self.start, 4, 0)
        second = feature_row("second", self.start, 0, 3)
        later = feature_row("later", self.start + timedelta(days=3), 1, 1)

        forward = evaluate_walk_forward([first, second, later], self.config)
        reverse = evaluate_walk_forward([later, second, first], self.config)

        self.assertEqual(
            prediction_rows_sha256(forward), prediction_rows_sha256(reverse)
        )
        self.assertEqual(self.model(forward, "later").training_fixtures, 2)

    def test_horizons_have_independent_online_training_state(self):
        first = feature_row("first", self.start, 4, 0)
        other_horizon = feature_row(
            "other",
            self.start + timedelta(days=3),
            1,
            1,
            information_state="pre_lineup_72h_clean_v1",
            hours_before=72,
        )

        predictions = evaluate_walk_forward([first, other_horizon], self.config)

        self.assertEqual(predictions, [])

    def test_probabilities_are_coherent_and_positive(self):
        first = feature_row("first", self.start, 1, 1)
        second = feature_row("second", self.start + timedelta(days=3), 0, 0)

        predictions = evaluate_walk_forward([first, second], self.config)

        for prediction in predictions:
            probabilities = (
                prediction.home_win_probability,
                prediction.draw_probability,
                prediction.away_win_probability,
            )
            self.assertAlmostEqual(sum(probabilities), 1.0, places=12)
            self.assertTrue(all(0 < value < 1 for value in probabilities))
            self.assertGreater(prediction.exact_score_probability, 0)

    def test_paired_block_bootstrap_is_deterministic(self):
        first = feature_row("first", self.start, 1, 1)
        second = feature_row("second", self.start + timedelta(days=3), 0, 0)
        config = replace(self.config, bootstrap_replicates=100)
        predictions = evaluate_walk_forward([first, second], config)

        first_summary = summarize_predictions(predictions, config)
        second_summary = summarize_predictions(predictions, config)

        self.assertEqual(first_summary, second_summary)
        self.assertEqual(len(first_summary["paired_model_comparisons"]), 3)
        self.assertTrue(
            all(
                comparison["fixtures"] == 1
                for comparison in first_summary["paired_model_comparisons"]
            )
        )


if __name__ == "__main__":
    unittest.main()
