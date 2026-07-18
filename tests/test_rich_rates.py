from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.features import RegulationInferenceFeatureRow
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    FixturePerformance,
    RichRateResearchError,
    evaluate_promoted_rich_rate_candidate,
    load_rich_rate_config,
    research_rich_rate_candidate,
    rich_feature_rows_sha256,
)
from soccer_bot.modeling.walk_forward import (
    evaluate_walk_forward,
    load_walk_forward_config,
)
from tests.test_walk_forward import feature_row


class RichRateTests(unittest.TestCase):
    def setUp(self):
        loaded = load_rich_rate_config(
            ROOT / "config" / "features" / "regulation_rich_rate_v1.json"
        )
        self.config = replace(loaded, minimum_fit_fixtures=1)
        self.start = datetime(2023, 1, 1, 12, tzinfo=timezone.utc)

    def _shared_teams(self, row, home="team-a", away="team-b"):
        return replace(row, home_team_id=home, away_team_id=away)

    def test_fixture_observation_cannot_change_its_own_snapshot(self):
        first = self._shared_teams(feature_row("first", self.start, 1, 0))
        later = self._shared_teams(
            feature_row("later", self.start + timedelta(days=7), 1, 0),
            away="team-c",
        )
        low = {
            "first": FixturePerformance("first", 0.2, 0.2, 3.0, 3.0)
        }
        high = {
            "first": FixturePerformance("first", 4.0, 0.2, 25.0, 3.0)
        }

        low_rows = ChronologicalRichRateBuilder(self.config).build(
            [first, later], low
        )
        high_rows = ChronologicalRichRateBuilder(self.config).build(
            [first, later], high
        )

        self.assertEqual(low_rows[0], high_rows[0])
        self.assertNotEqual(
            low_rows[1].home_xg_attack, high_rows[1].home_xg_attack
        )
        self.assertNotEqual(
            low_rows[1].home_shots_attack, high_rows[1].home_shots_attack
        )

    def test_result_at_exact_snapshot_time_is_not_visible(self):
        current = self._shared_teams(
            feature_row("current", self.start + timedelta(days=2), 1, 0),
            away="team-c",
        )
        prior_kickoff = current.prediction_at - timedelta(
            minutes=self.config.result_availability_delay_minutes
        )
        prior = self._shared_teams(feature_row("prior", prior_kickoff, 3, 0))
        performance = {
            "prior": FixturePerformance("prior", 4.0, 0.2, 25.0, 3.0)
        }

        rows = ChronologicalRichRateBuilder(self.config).build(
            [prior, current], performance
        )
        current_row = next(row for row in rows if row.fixture_id == "current")

        self.assertEqual(current_row.home_xg_history, 0)
        self.assertEqual(current_row.home_shots_history, 0)

    def test_simultaneous_batches_are_input_order_invariant(self):
        first = self._shared_teams(feature_row("first", self.start, 1, 0))
        second = self._shared_teams(
            feature_row("second", self.start, 0, 1),
            home="team-c",
            away="team-a",
        )
        later = self._shared_teams(
            feature_row("later", self.start + timedelta(days=7), 1, 0),
            away="team-d",
        )
        performance = {
            "first": FixturePerformance("first", 2.0, 0.5, 16.0, 6.0),
            "second": FixturePerformance("second", 0.5, 2.0, 6.0, 16.0),
        }

        forward = ChronologicalRichRateBuilder(self.config).build(
            [first, second, later], performance
        )
        reverse = ChronologicalRichRateBuilder(self.config).build(
            [later, second, first], performance
        )

        self.assertEqual(
            rich_feature_rows_sha256(forward), rich_feature_rows_sha256(reverse)
        )

    def test_inference_replays_only_prior_rich_observations(self):
        historical = self._shared_teams(
            feature_row("history", self.start, 1, 0)
        )
        source = self._shared_teams(
            feature_row("upcoming", self.start + timedelta(days=7), 0, 0),
            away="team-c",
        )
        value = asdict(source)
        value.pop("home_goals")
        value.pop("away_goals")
        inference = RegulationInferenceFeatureRow(**value)
        performance = {
            "history": FixturePerformance("history", 3.0, 0.5, 20.0, 5.0)
        }

        rows = ChronologicalRichRateBuilder(self.config).build_inference(
            [historical], [inference], performance
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].home_xg_history, 1)
        self.assertEqual(rows[0].home_shots_history, 1)
        self.assertGreater(rows[0].home_xg_attack, self.config.xg.prior_mean)

    def test_late_rich_stats_skip_earlier_forecast_but_update_a_later_game(self):
        historical = self._shared_teams(
            feature_row("history", self.start, 1, 0)
        )
        first_source = self._shared_teams(
            feature_row("first", self.start + timedelta(days=7), 0, 0),
            away="team-c",
        )
        later_source = self._shared_teams(
            feature_row("later", self.start + timedelta(days=14), 0, 0),
            away="team-d",
        )

        def inference(source):
            value = asdict(source)
            value.pop("home_goals")
            value.pop("away_goals")
            return RegulationInferenceFeatureRow(**value)

        first = inference(first_source)
        later = inference(later_source)
        retrieved_at = first.prediction_at + timedelta(hours=1)
        performance = {
            "history": FixturePerformance(
                "history",
                3.0,
                0.5,
                20.0,
                5.0,
                available_at=retrieved_at,
                source_max_retrieved_at=retrieved_at,
            )
        }

        first_row = ChronologicalRichRateBuilder(self.config).build_inference(
            [historical], [first], performance
        )[0]
        later_row = ChronologicalRichRateBuilder(self.config).build_inference(
            [historical], [later], performance
        )[0]

        self.assertEqual(first_row.home_xg_history, 0)
        self.assertEqual(later_row.home_xg_history, 1)
        self.assertEqual(later_row.source_max_retrieved_at, retrieved_at)

    def test_candidate_fit_and_validation_never_use_later_folds(self):
        walk = replace(
            load_walk_forward_config(
                ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
            ),
            minimum_training_fixtures=1,
            bootstrap_replicates=100,
        )
        rows = [
            feature_row("warmup", datetime(2021, 1, 1, tzinfo=timezone.utc), 1, 0),
            feature_row("fit", datetime(2022, 1, 1, tzinfo=timezone.utc), 2, 0),
            feature_row(
                "validation", datetime(2023, 8, 1, tzinfo=timezone.utc), 1, 1
            ),
            feature_row("test", datetime(2025, 8, 1, tzinfo=timezone.utc), 0, 1),
        ]
        performance = {
            row.fixture_id: FixturePerformance(
                row.fixture_id, 1.25, 1.25, 12.0, 12.0
            )
            for row in rows
        }
        rich_rows = ChronologicalRichRateBuilder(self.config).build(
            rows, performance
        )
        baseline = evaluate_walk_forward(rows, walk)

        fits, predictions, summary = research_rich_rate_candidate(
            rich_rows,
            baseline,
            config=self.config,
            walk_forward_config=walk,
        )

        self.assertEqual({fit.fit_fixtures for fit in fits}, {1})
        self.assertEqual({row.fixture_id for row in predictions}, {"validation"})
        self.assertTrue(
            all(row.kickoff < self.config.validation_end_exclusive for row in predictions)
        )
        self.assertFalse(summary["test_fold_accessed"])

    def test_final_evaluation_requires_a_passing_development_gate(self):
        walk = replace(
            load_walk_forward_config(
                ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
            ),
            minimum_training_fixtures=1,
            bootstrap_replicates=100,
        )
        rows = [
            feature_row("warmup", datetime(2021, 1, 1, tzinfo=timezone.utc), 1, 0),
            feature_row("fit", datetime(2022, 1, 1, tzinfo=timezone.utc), 2, 0),
            feature_row(
                "validation", datetime(2023, 8, 1, tzinfo=timezone.utc), 1, 1
            ),
            feature_row(
                "calibration", datetime(2024, 8, 1, tzinfo=timezone.utc), 1, 0
            ),
            feature_row("test", datetime(2025, 8, 1, tzinfo=timezone.utc), 0, 1),
        ]
        performance = {
            row.fixture_id: FixturePerformance(
                row.fixture_id, 1.25, 1.25, 12.0, 12.0
            )
            for row in rows
        }
        rich_rows = ChronologicalRichRateBuilder(self.config).build(
            rows, performance
        )
        baseline = evaluate_walk_forward(rows, walk)
        passing = {
            "research_scope": "development_only",
            "test_fold_accessed": False,
            "metrics": [
                {
                    "information_state": "pre_lineup_24h_v1",
                    "moneyline_log_loss": {
                        "paired_month_block_bootstrap_95_upper": -0.001
                    },
                    "moneyline_brier": {
                        "paired_month_block_bootstrap_95_upper": -0.001
                    },
                }
            ],
        }

        fits, predictions = evaluate_promoted_rich_rate_candidate(
            rich_rows,
            baseline,
            config=self.config,
            walk_forward_config=walk,
            selection_evidence=passing,
        )

        self.assertEqual({fit.fit_fixtures for fit in fits}, {2})
        self.assertEqual(
            {row.fixture_id for row in predictions}, {"calibration", "test"}
        )
        failing = json.loads(json.dumps(passing))
        failing["metrics"][0]["moneyline_log_loss"][
            "paired_month_block_bootstrap_95_upper"
        ] = 0.001
        with self.assertRaises(RichRateResearchError):
            evaluate_promoted_rich_rate_candidate(
                rich_rows,
                baseline,
                config=self.config,
                walk_forward_config=walk,
                selection_evidence=failing,
            )


if __name__ == "__main__":
    unittest.main()
