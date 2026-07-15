from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_json
from soccer_bot.datasets.features import RegulationInferenceFeatureRow
from soccer_bot.modeling.production import (
    champion_model_sha256,
    fit_regulation_champion,
    predict_regulation_moneyline,
)
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    FixturePerformance,
    load_rich_rate_config,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config
from tests.test_walk_forward import feature_row


class ProductionModelTests(unittest.TestCase):
    def setUp(self):
        self.rich_config = load_rich_rate_config(
            ROOT / "config" / "features" / "regulation_rich_rate_v1.json"
        )
        self.walk = load_walk_forward_config(
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        )
        self.specification = load_json(
            ROOT / "config" / "models" / "regulation_champion_v1.json"
        )
        start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
        self.features = [
            feature_row("24-one", start, 2, 0),
            feature_row("24-two", start + timedelta(days=7), 1, 1),
            feature_row(
                "72-one",
                start,
                2,
                0,
                information_state="pre_lineup_72h_clean_v1",
                hours_before=72,
            ),
            feature_row(
                "72-two",
                start + timedelta(days=7),
                1,
                1,
                information_state="pre_lineup_72h_clean_v1",
                hours_before=72,
            ),
        ]
        performance = {
            row.fixture_id: FixturePerformance(
                row.fixture_id, 1.25, 1.25, 12.0, 12.0
            )
            for row in self.features
        }
        self.rich = ChronologicalRichRateBuilder(self.rich_config).build(
            self.features, performance
        )

    def test_all_history_refit_and_inference_are_coherent(self):
        model = fit_regulation_champion(
            self.features,
            self.rich,
            temperatures={
                "pre_lineup_24h_v1": 1.1,
                "pre_lineup_72h_clean_v1": 1.2,
            },
            model_specification=self.specification,
            rich_config=self.rich_config,
            walk_forward_config=self.walk,
        )
        source = self.features[1]
        value = asdict(source)
        value.pop("home_goals")
        value.pop("away_goals")
        inference = RegulationInferenceFeatureRow(**value)
        rich = replace(
            next(
                row
                for row in self.rich
                if row.information_state == "pre_lineup_24h_v1"
            ),
            fixture_id=inference.fixture_id,
            prediction_at=inference.prediction_at,
            kickoff=inference.kickoff,
            home_team_id=inference.home_team_id,
            away_team_id=inference.away_team_id,
        )

        predictions = predict_regulation_moneyline(
            [inference],
            [rich],
            model,
            rich_config=self.rich_config,
            walk_forward_config=self.walk,
        )

        self.assertEqual({item.training_fixtures for item in model.horizons}, {2})
        self.assertEqual(len(champion_model_sha256(model)), 64)
        prediction = predictions[0]
        self.assertAlmostEqual(
            prediction.home_win_probability
            + prediction.draw_probability
            + prediction.away_win_probability,
            1.0,
        )
        self.assertGreater(prediction.expected_home_goals, 0)
        self.assertIn(
            "moneyline_calibration_not_score_grid_coherent",
            prediction.warnings,
        )


if __name__ == "__main__":
    unittest.main()
