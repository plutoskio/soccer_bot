from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.contracts import ScoreGrid
from soccer_bot.modeling.score_grid import (
    ScoreGridResearchError,
    confirmation_gate,
    evaluate_score_grid_window,
    fit_score_grid_candidate,
    load_score_grid_research_config,
    poisson_score_grid,
    select_candidate,
    transform_score_grid,
)


@dataclass(frozen=True)
class RateRow:
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float


class CoherentScoreGridTests(unittest.TestCase):
    def setUp(self):
        loaded = load_score_grid_research_config(
            ROOT / "config" / "models" / "regulation_score_grid_v2.json"
        )
        self.start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        self.window = replace(
            loaded.windows[0],
            fit_start_inclusive=self.start,
            fit_end_exclusive=self.start + timedelta(days=10),
            validation_start_inclusive=self.start + timedelta(days=10),
            validation_end_exclusive=self.start + timedelta(days=20),
        )
        self.temperature = replace(
            loaded.candidates[0], minimum_fit_fixtures=1
        )
        self.tilt = replace(loaded.candidates[1], minimum_fit_fixtures=1)
        self.config = replace(
            loaded,
            windows=(self.window,),
            candidates=(self.temperature, self.tilt),
            forbidden_kickoff_start=self.start + timedelta(days=30),
            moneyline_control_minimum_fit_fixtures=1,
            bootstrap_replicates=100,
        )

    def row(
        self,
        fixture_id: str,
        day: int,
        home_goals: int,
        away_goals: int,
        *,
        home_rate: float = 1.5,
        away_rate: float = 1.1,
    ) -> RateRow:
        kickoff = self.start + timedelta(days=day)
        return RateRow(
            fixture_id=fixture_id,
            information_state="pre_lineup_24h_v1",
            prediction_at=kickoff - timedelta(hours=24),
            kickoff=kickoff,
            home_goals=home_goals,
            away_goals=away_goals,
            expected_home_goals=home_rate,
            expected_away_goals=away_rate,
        )

    def test_poisson_grid_is_normalized_and_contract_coherent(self):
        probabilities = poisson_score_grid(2.2, 0.9, self.config)
        grid = ScoreGrid(probabilities)

        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)
        moneyline = grid.moneyline()
        self.assertAlmostEqual(sum(moneyline.values()), 1.0, places=12)
        self.assertGreaterEqual(max(score[0] for score in probabilities), 12)
        self.assertGreaterEqual(max(score[1] for score in probabilities), 12)
        self.assertAlmostEqual(
            grid.both_teams_to_score()["yes"],
            sum(
                probability
                for (home, away), probability in probabilities.items()
                if home > 0 and away > 0
            ),
        )

    def test_identity_temperature_and_zero_tilt_preserve_grid(self):
        baseline = poisson_score_grid(1.7, 1.2, self.config)
        temperature = transform_score_grid(
            baseline, self.temperature, {"temperature": 1.0}
        )
        tilt = transform_score_grid(
            baseline,
            self.tilt,
            {name: 0.0 for name in self.tilt.feature_names},
        )

        for score in baseline:
            self.assertAlmostEqual(temperature[score], baseline[score], places=15)
            self.assertAlmostEqual(tilt[score], baseline[score], places=15)

    def test_fitted_candidates_converge_and_return_normalized_grids(self):
        rows = [
            self.row("a", 1, 0, 0),
            self.row("b", 2, 3, 0),
            self.row("c", 3, 1, 1),
            self.row("d", 4, 2, 2),
            self.row("e", 5, 4, 1),
            self.row("f", 6, 0, 1),
        ]
        for candidate in (self.temperature, self.tilt):
            fit = fit_score_grid_candidate(
                rows,
                candidate,
                config=self.config,
                window_key=self.window.window_key,
                information_state="pre_lineup_24h_v1",
            )
            self.assertTrue(fit.converged)
            self.assertTrue(all(math.isfinite(value) for value in fit.parameters.values()))
            transformed = transform_score_grid(
                poisson_score_grid(1.6, 1.0, self.config),
                candidate,
                fit.parameters,
            )
            self.assertAlmostEqual(sum(transformed.values()), 1.0, places=12)
            self.assertTrue(all(value > 0 for value in transformed.values()))

    def test_validation_outcome_cannot_change_fits_or_other_probabilities(self):
        fit_rows = [
            self.row("fit-a", 1, 1, 0),
            self.row("fit-b", 2, 2, 1),
            self.row("fit-c", 3, 0, 0),
        ]
        first = self.row("validation-a", 11, 1, 1)
        target = self.row("validation-b", 12, 0, 0)
        changed = replace(target, home_goals=6, away_goals=2)

        fits_a, evaluations_a, _ = evaluate_score_grid_window(
            [*fit_rows, first, target],
            window=self.window,
            candidates=(self.temperature,),
            config=self.config,
        )
        fits_b, evaluations_b, _ = evaluate_score_grid_window(
            [*fit_rows, first, changed],
            window=self.window,
            candidates=(self.temperature,),
            config=self.config,
        )

        self.assertEqual(fits_a, fits_b)
        signature_a = [
            (
                row.model_key,
                row.home_win_probability,
                row.draw_probability,
                row.away_win_probability,
            )
            for row in evaluations_a
            if row.fixture_id == "validation-a"
        ]
        signature_b = [
            (
                row.model_key,
                row.home_win_probability,
                row.draw_probability,
                row.away_win_probability,
            )
            for row in evaluations_b
            if row.fixture_id == "validation-a"
        ]
        self.assertEqual(signature_a, signature_b)

    def test_opened_final_test_rows_fail_closed(self):
        unsafe = self.row("unsafe", 30, 1, 0)
        with self.assertRaisesRegex(
            ScoreGridResearchError, "Opened final-test row"
        ):
            fit_score_grid_candidate(
                [unsafe],
                self.temperature,
                config=self.config,
                window_key="unsafe",
                information_state="pre_lineup_24h_v1",
            )

    def test_window_evaluation_emits_paired_distribution_metrics(self):
        rows = [
            self.row("fit-a", 1, 1, 0),
            self.row("fit-b", 2, 2, 1),
            self.row("fit-c", 3, 0, 0),
            self.row("validation-a", 11, 1, 1),
            self.row("validation-b", 12, 2, 0),
        ]
        fits, evaluations, summary = evaluate_score_grid_window(
            rows,
            window=self.window,
            candidates=(self.temperature,),
            config=self.config,
        )

        self.assertEqual(len(fits), 2)
        self.assertEqual(len(evaluations), 6)
        self.assertEqual(len(summary["metrics"]), 3)
        self.assertEqual(
            {item["metric"] for item in summary["paired_model_comparisons"]},
            {
                "exact_score_log_loss",
                "home_goals_log_loss",
                "away_goals_log_loss",
                "total_goals_log_loss",
                "goal_difference_log_loss",
                "moneyline_log_loss",
                "moneyline_brier",
                "both_teams_to_score_log_loss",
                "both_teams_to_score_brier",
                "total_goals_rps",
                "goal_difference_rps",
            },
        )
        for comparison in summary["paired_model_comparisons"]:
            expected_baseline = (
                self.config.moneyline_control_model_key
                if comparison["metric"] in {"moneyline_log_loss", "moneyline_brier"}
                else self.config.baseline_model_key
            )
            self.assertEqual(comparison["baseline_model"], expected_baseline)

    def test_selection_and_confirmation_policies_are_explicit(self):
        selection_metrics = (
            "exact_score_log_loss",
            *self.config.selection_tie_break_metrics,
        )
        selection = {
            "paired_model_comparisons": [
                {
                    "challenger_model": "candidate",
                    "information_state": state,
                    "metric": metric,
                    "fixtures": 100,
                    "mean_delta_challenger_minus_baseline": -0.01,
                }
                for state in ("pre_lineup_24h_v1", "pre_lineup_72h_clean_v1")
                for metric in selection_metrics
            ]
        }
        selected = select_candidate(selection, self.config)
        self.assertTrue(selected["selection_gate_passed"])
        self.assertEqual(selected["selected_model"], "candidate")

        comparisons = []
        for state in ("pre_lineup_24h_v1", "pre_lineup_72h_clean_v1"):
            for metric in (
                "exact_score_log_loss",
                "home_goals_log_loss",
                "away_goals_log_loss",
                "total_goals_log_loss",
                "goal_difference_log_loss",
                "moneyline_log_loss",
            ):
                comparisons.append(
                    {
                        "challenger_model": "candidate",
                        "information_state": state,
                        "metric": metric,
                        "mean_delta_challenger_minus_baseline": -0.001,
                        "paired_month_block_bootstrap_95_upper": -0.0001,
                    }
                )
        gate = confirmation_gate(
            {"paired_model_comparisons": comparisons}, "candidate", self.config
        )
        self.assertTrue(gate["confirmation_gate_passed"])
        self.assertIn("await_new_forward_holdout", gate["production_status"])

        failed_comparisons = [dict(item) for item in comparisons]
        for item in failed_comparisons:
            if item["metric"] == "moneyline_log_loss":
                item["mean_delta_challenger_minus_baseline"] = 0.002
        failed_gate = confirmation_gate(
            {"paired_model_comparisons": failed_comparisons},
            "candidate",
            self.config,
        )
        self.assertFalse(failed_gate["confirmation_gate_passed"])
        self.assertEqual(
            failed_gate["production_status"],
            "research_candidate_failed_confirmation_gate",
        )


if __name__ == "__main__":
    unittest.main()
