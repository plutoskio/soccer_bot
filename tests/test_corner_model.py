from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.datasets.corner_features import CornerFeatureRow
from soccer_bot.modeling.corners import (
    CANDIDATES,
    CornerModelError,
    corner_joint_probability,
    corner_model_sha256,
    corner_score_grid,
    corner_total_distribution,
    dump_corner_model,
    fit_joint_corner_model,
    load_corner_model,
    load_corner_model_config,
)


ROOT = Path(__file__).resolve().parents[1]


class CornerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_corner_model_config(
            ROOT / "config/models/joint_corners_v1.json"
        )

    def test_fit_is_deterministic_and_round_trips(self) -> None:
        rows = _rows(self.config.minimum_fit_fixtures + 5)
        first = fit_joint_corner_model(rows, self.config)
        second = fit_joint_corner_model(list(reversed(rows)), self.config)
        self.assertEqual(corner_model_sha256(first), corner_model_sha256(second))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_corner_model(first, path, created_at=datetime.now(timezone.utc))
            loaded = load_corner_model(path)
        self.assertEqual(first, loaded)
        for candidate in CANDIDATES:
            probability = corner_joint_probability(
                loaded,
                candidate=candidate,
                information_state="pre_lineup_24h_v1",
                expected_home_corners=5.4,
                expected_away_corners=4.2,
                home_corners=5,
                away_corners=4,
            )
            self.assertGreater(probability, 0)

    def test_grids_normalize(self) -> None:
        model = fit_joint_corner_model(
            _rows(self.config.minimum_fit_fixtures), self.config
        )
        for candidate in CANDIDATES:
            grid = corner_score_grid(
                model,
                self.config,
                candidate=candidate,
                information_state="pre_lineup_24h_v1",
                expected_home_corners=5.0,
                expected_away_corners=4.0,
            )
            self.assertAlmostEqual(sum(grid.values()), 1.0)
            totals = corner_total_distribution(
                model,
                self.config,
                candidate=candidate,
                information_state="pre_lineup_24h_v1",
                expected_home_corners=5.0,
                expected_away_corners=4.0,
            )
            self.assertAlmostEqual(sum(totals), 1.0)

    def test_duplicate_rows_fail_closed(self) -> None:
        rows = _rows(self.config.minimum_fit_fixtures)
        rows.append(rows[0])
        with self.assertRaisesRegex(CornerModelError, "Duplicate"):
            fit_joint_corner_model(rows, self.config)

    def test_hash_tampering_fails(self) -> None:
        model = fit_joint_corner_model(
            _rows(self.config.minimum_fit_fixtures), self.config
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            dump_corner_model(model, path, created_at=datetime.now(timezone.utc))
            value = json.loads(path.read_text())
            value["model"]["horizons"][0]["home_nb_shape"] = 123.0
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(CornerModelError, "hash mismatch"):
                load_corner_model(path)


def _rows(per_horizon: int) -> list[CornerFeatureRow]:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    output = []
    for state, hours in (
        ("pre_lineup_72h_clean_v1", 72),
        ("pre_lineup_24h_v1", 24),
    ):
        for index in range(per_horizon):
            kickoff = start + timedelta(hours=4 * index)
            home_rate = 4.5 + 0.2 * (index % 5)
            away_rate = 3.8 + 0.2 * (index % 4)
            # Mildly overdispersed deterministic fixture sequence.
            home = max(0, int(round(home_rate + ((index % 7) - 3) * 0.8)))
            away = max(0, int(round(away_rate + ((index % 5) - 2) * 0.7)))
            output.append(
                CornerFeatureRow(
                    feature_version="corner_team_state_v1",
                    fixture_id=f"{state}-{index}",
                    information_state=state,
                    prediction_at=kickoff - timedelta(hours=hours),
                    kickoff=kickoff,
                    competition_id="league",
                    season_id="season",
                    home_team_id="home",
                    away_team_id="away",
                    neutral_venue=False,
                    home_corners=home,
                    away_corners=away,
                    expected_home_corners=home_rate,
                    expected_away_corners=away_rate,
                    home_attack_mean=0.0,
                    home_defense_mean=0.0,
                    away_attack_mean=0.0,
                    away_defense_mean=0.0,
                    competition_log_corner_level=1.6,
                    competition_home_advantage=0.08,
                    home_history_matches=index,
                    away_history_matches=index,
                    competition_history_matches=index,
                    home_cold_start=index < 5,
                    away_cold_start=index < 5,
                )
            )
    return output


if __name__ == "__main__":
    unittest.main()
