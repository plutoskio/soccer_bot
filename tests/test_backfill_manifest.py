from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.backfill_manifest import BackfillManifestBuilder


class BackfillManifestTests(unittest.TestCase):
    def test_unknown_team_is_new_not_ambiguous(self):
        builder = object.__new__(BackfillManifestBuilder)
        builder.team_source_map = {}
        builder.team_name_index = {}
        builder.warehouse = type("WarehouseStub", (), {"team_aliases": {}})()

        team_id, reason = builder._resolve_team(
            {"id": 123, "name": "Previously unseen FC"}, "club"
        )

        self.assertIsNone(team_id)
        self.assertIsNone(reason)

    def test_batches_only_requestable_actions_and_caps_at_twenty(self):
        rows = []
        for index in range(45):
            rows.append({
                "api_fixture_id": index,
                "league_id": 39,
                "season": 2025,
                "priority": 2,
                "kickoff": f"2025-01-{index % 28 + 1:02d}T12:00:00+00:00",
                "action": "REQUEST_API",
            })
        rows.extend([
            {
                "api_fixture_id": 100, "league_id": 39, "season": 2025,
                "priority": 2, "kickoff": "2025-01-01T12:00:00+00:00",
                "action": "COMPLETE",
            },
            {
                "api_fixture_id": 101, "league_id": 39, "season": 2025,
                "priority": 2, "kickoff": "2025-01-01T12:00:00+00:00",
                "action": "NEEDS_REVIEW",
            },
        ])
        batches = BackfillManifestBuilder._batches(rows)
        self.assertEqual([20, 20, 5], [batch["fixture_count"] for batch in batches])
        requested = {fixture_id for batch in batches for fixture_id in batch["fixture_ids"]}
        self.assertNotIn(100, requested)
        self.assertNotIn(101, requested)

    def test_champions_league_qualifiers_are_excluded(self):
        fixtures = [
            {"fixture": {"status": {"short": "FT"}}, "league": {"round": "2nd Qualifying Round"}},
            {"fixture": {"status": {"short": "FT"}}, "league": {"round": "League Stage - 1"}},
        ]
        selected = BackfillManifestBuilder._eligible_fixtures(
            fixtures, {"exclude_round_terms": ["qualifying", "preliminary"]}
        )
        self.assertEqual(1, len(selected))
        self.assertEqual("League Stage - 1", selected[0]["league"]["round"])


if __name__ == "__main__":
    unittest.main()
