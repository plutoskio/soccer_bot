from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from soccer_bot.datasets.corner_features import (
    ChronologicalCornerFeatureBuilder,
    CornerFeatureError,
    load_corner_feature_config,
)
from soccer_bot.datasets.corners import CornerTarget
from soccer_bot.datasets.features import RegulationInferenceFixture


ROOT = Path(__file__).resolve().parents[1]


class CornerFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_corner_feature_config(
            ROOT / "config/models/joint_corners_v1.json"
        )

    def test_target_does_not_enter_its_own_prediction(self) -> None:
        target = _target("f1", datetime(2025, 1, 5, 15, tzinfo=timezone.utc), 20, 0)
        row = ChronologicalCornerFeatureBuilder(self.config).build([target])
        self.assertEqual(len(row), 2)
        expected_home = row[0].expected_home_corners
        self.assertAlmostEqual(expected_home, row[1].expected_home_corners)
        self.assertEqual(row[0].home_history_matches, 0)
        self.assertLess(expected_home, 10.0)

    def test_past_result_changes_later_prediction(self) -> None:
        first = _target("f1", datetime(2025, 1, 1, 15, tzinfo=timezone.utc), 14, 2)
        second = _target("f2", datetime(2025, 1, 10, 15, tzinfo=timezone.utc), 5, 4)
        rows = ChronologicalCornerFeatureBuilder(self.config).build([first, second])
        later = [row for row in rows if row.fixture_id == "f2"]
        self.assertEqual(len(later), 2)
        self.assertEqual(later[0].home_history_matches, 1)
        self.assertGreater(later[0].expected_home_corners, 5.0)

    def test_simultaneous_updates_are_order_invariant(self) -> None:
        kickoff = datetime(2025, 1, 1, 15, tzinfo=timezone.utc)
        targets = [
            _target("f1", kickoff, 8, 2, home="a", away="b"),
            _target("f2", kickoff, 3, 9, home="c", away="d"),
            _target("f3", kickoff + timedelta(days=8), 5, 5, home="a", away="d"),
        ]
        first = ChronologicalCornerFeatureBuilder(self.config).build(targets)
        second = ChronologicalCornerFeatureBuilder(self.config).build(list(reversed(targets)))
        self.assertEqual(first, second)

    def test_clean_72h_skips_intervening_fixture(self) -> None:
        first = _target("f1", datetime(2025, 1, 2, 12, tzinfo=timezone.utc), 5, 4)
        second = _target("f2", datetime(2025, 1, 4, 12, tzinfo=timezone.utc), 6, 3, away="c")
        rows = ChronologicalCornerFeatureBuilder(self.config).build([first, second])
        later_states = {row.information_state for row in rows if row.fixture_id == "f2"}
        self.assertEqual(later_states, {"pre_lineup_24h_v1"})

    def test_duplicate_fixture_fails(self) -> None:
        target = _target("f1", datetime(2025, 1, 1, tzinfo=timezone.utc), 5, 5)
        with self.assertRaisesRegex(CornerFeatureError, "unique"):
            ChronologicalCornerFeatureBuilder(self.config).build([target, target])

    def test_inference_replays_only_available_history(self) -> None:
        first = _target(
            "f1", datetime(2025, 1, 1, 12, tzinfo=timezone.utc), 12, 2
        )
        kickoff = datetime(2025, 1, 10, 12, tzinfo=timezone.utc)
        upcoming = RegulationInferenceFixture(
            fixture_id="future",
            competition_id="league",
            season_id="season",
            home_team_id="a",
            away_team_id="b",
            neutral_venue=False,
            kickoff=kickoff,
            allowed_information_states=("pre_lineup_24h_v1",),
        )
        rows = ChronologicalCornerFeatureBuilder(self.config).build_inference(
            [first], [upcoming], as_of=kickoff - timedelta(hours=12)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].home_history_matches, 1)
        self.assertGreater(rows[0].expected_home_corners, 5.0)


def _target(
    fixture_id: str,
    kickoff: datetime,
    home_corners: int,
    away_corners: int,
    *,
    home: str = "a",
    away: str = "b",
) -> CornerTarget:
    return CornerTarget(
        fixture_id=fixture_id,
        competition_id="league",
        season_id="season",
        home_team_id=home,
        away_team_id=away,
        neutral_venue=False,
        kickoff=kickoff,
        prediction_at=kickoff - timedelta(hours=24),
        home_corners=home_corners,
        away_corners=away_corners,
        total_corners=home_corners + away_corners,
        corner_difference=home_corners - away_corners,
        agreeing_source_codes=("api_football",),
    )


if __name__ == "__main__":
    unittest.main()
