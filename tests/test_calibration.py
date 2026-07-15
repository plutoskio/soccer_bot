from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.calibration import (
    fit_and_apply_temperature_calibration,
    summarize_calibration,
)
from soccer_bot.modeling.walk_forward import (
    evaluate_walk_forward,
    load_walk_forward_config,
)
from tests.test_walk_forward import feature_row


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        loaded = load_walk_forward_config(
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        )
        self.config = replace(
            loaded,
            minimum_training_fixtures=1,
            calibration_minimum_fixtures=1,
            bootstrap_replicates=100,
        )
        self.rows = [
            feature_row(
                "warmup", datetime(2024, 7, 2, 12, tzinfo=timezone.utc), 1, 1
            ),
            feature_row(
                "calibration",
                datetime(2024, 8, 2, 12, tzinfo=timezone.utc),
                0,
                0,
            ),
            feature_row(
                "test", datetime(2025, 8, 2, 12, tzinfo=timezone.utc), 2, 0
            ),
        ]

    def test_fit_uses_calibration_and_outputs_only_test_predictions(self):
        predictions = evaluate_walk_forward(self.rows, self.config)

        fits, calibrated = fit_and_apply_temperature_calibration(
            predictions, self.config
        )

        self.assertEqual(len(fits), 2)
        self.assertEqual(len(calibrated), 2)
        self.assertEqual({row.fixture_id for row in calibrated}, {"test"})
        self.assertTrue(
            all(row.fold_key == self.config.calibration_apply_fold for row in calibrated)
        )
        for row in calibrated:
            self.assertAlmostEqual(
                row.home_win_probability
                + row.draw_probability
                + row.away_win_probability,
                1.0,
            )

    def test_mutating_test_outcome_cannot_change_fit_or_probabilities(self):
        predictions = evaluate_walk_forward(self.rows, self.config)
        mutated = [
            replace(row, result="away_win")
            if row.fixture_id == "test"
            else row
            for row in predictions
        ]

        fits, calibrated = fit_and_apply_temperature_calibration(
            predictions, self.config
        )
        mutated_fits, mutated_calibrated = fit_and_apply_temperature_calibration(
            mutated, self.config
        )

        self.assertEqual(fits, mutated_fits)
        original = {
            row.model_key: (
                row.home_win_probability,
                row.draw_probability,
                row.away_win_probability,
            )
            for row in calibrated
        }
        changed = {
            row.model_key: (
                row.home_win_probability,
                row.draw_probability,
                row.away_win_probability,
            )
            for row in mutated_calibrated
        }
        self.assertEqual(original, changed)

    def test_calibration_summary_is_paired_against_base_test_predictions(self):
        predictions = evaluate_walk_forward(self.rows, self.config)
        fits, calibrated = fit_and_apply_temperature_calibration(
            predictions, self.config
        )

        summary = summarize_calibration(
            fits, calibrated, predictions, self.config
        )

        self.assertEqual(summary["fit_fold"], "calibration")
        self.assertEqual(summary["apply_fold"], "test")
        self.assertEqual(len(summary["paired_model_comparisons"]), 4)


if __name__ == "__main__":
    unittest.main()
