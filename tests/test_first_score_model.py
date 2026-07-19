from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.modeling.timing import (
    FirstScoreModelError,
    FirstScoreObservation,
    baseline_first_team_probabilities,
    dump_first_score_model,
    first_score_model_sha256,
    first_team_probabilities,
    fit_first_score_timing_model,
    load_first_score_config,
    load_first_score_model,
)


ROOT = Path(__file__).resolve().parents[1]


class FirstScoreModelTests(unittest.TestCase):
    def test_baseline_is_normalized_and_directional(self) -> None:
        values = baseline_first_team_probabilities(2.0, 0.5)
        self.assertAlmostEqual(sum(values.values()), 1.0)
        self.assertGreater(values["home_first"], values["away_first"])
        self.assertAlmostEqual(values["no_goal"], 0.0820849986238988)

    def test_fit_is_deterministic_and_round_trips(self) -> None:
        config = load_first_score_config(
            ROOT / "config/models/first_score_timing_v1.json"
        )
        rows = _observations(config.minimum_fit_fixtures + 20)
        first = fit_first_score_timing_model(rows, config)
        second = fit_first_score_timing_model(list(reversed(rows)), config)
        self.assertEqual(first_score_model_sha256(first), first_score_model_sha256(second))
        for horizon in first.horizons:
            self.assertTrue(horizon.converged)
            self.assertLess(horizon.mean_log_loss_after, horizon.mean_log_loss_before)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_first_score_model(first, path, created_at=datetime.now(timezone.utc))
            loaded = load_first_score_model(path)
        self.assertEqual(first, loaded)
        probabilities = first_team_probabilities(
            loaded,
            information_state="pre_lineup_24h_v1",
            expected_home_goals=1.4,
            expected_away_goals=1.0,
        )
        self.assertAlmostEqual(sum(probabilities.values()), 1.0)

    def test_duplicate_rows_fail_closed(self) -> None:
        config = load_first_score_config(
            ROOT / "config/models/first_score_timing_v1.json"
        )
        rows = _observations(config.minimum_fit_fixtures)
        rows.append(rows[0])
        with self.assertRaisesRegex(FirstScoreModelError, "Duplicate"):
            fit_first_score_timing_model(rows, config)

    def test_artifact_hash_tampering_fails(self) -> None:
        config = load_first_score_config(
            ROOT / "config/models/first_score_timing_v1.json"
        )
        model = fit_first_score_timing_model(
            _observations(config.minimum_fit_fixtures), config
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_first_score_model(model, path, created_at=datetime.now(timezone.utc))
            value = json.loads(path.read_text())
            value["model"]["horizons"][0]["temperature"] = 1.234
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(FirstScoreModelError, "hash mismatch"):
                load_first_score_model(path)


def _observations(per_horizon: int) -> list[FirstScoreObservation]:
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    rows = []
    for horizon, hours in (
        ("pre_lineup_72h_clean_v1", 72),
        ("pre_lineup_24h_v1", 24),
    ):
        for index in range(per_horizon):
            kickoff = start + timedelta(hours=3 * index)
            home = 1.2 + 0.1 * (index % 4)
            away = 0.8 + 0.1 * (index % 3)
            # Deliberately more no-goal outcomes than the raw goal-race baseline,
            # so the calibrated fit has a stable improvement to recover.
            selector = index % 10
            outcome = (
                "no_goal"
                if selector < 2
                else "home_first"
                if selector < 7
                else "away_first"
            )
            rows.append(
                FirstScoreObservation(
                    fixture_id=f"{horizon}-{index}",
                    information_state=horizon,
                    prediction_at=kickoff - timedelta(hours=hours),
                    kickoff=kickoff,
                    outcome=outcome,
                    expected_home_goals=home,
                    expected_away_goals=away,
                )
            )
    return rows


if __name__ == "__main__":
    unittest.main()
