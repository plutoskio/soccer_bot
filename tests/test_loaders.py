from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse
from soccer_bot.loaders import (
    RawCatalog,
    WarehouseLoader,
    api_player_identity_key,
    api_player_comparison_name,
    canonical_api_football_status,
    compatible_api_player_compound_names,
    compatible_api_player_names,
    parse_api_passes,
)


class ApiFootballPassParsingTests(unittest.TestCase):
    def test_completed_pass_count_format(self):
        self.assertEqual((20, 16, 80.0), parse_api_passes({"total": 20, "accuracy": "16"}))

    def test_historical_percentage_format(self):
        self.assertEqual((25, 22, 88.0), parse_api_passes({"total": 25, "accuracy": "88%"}))

    def test_numeric_percentage_larger_than_total(self):
        self.assertEqual((23, 20, 88.0), parse_api_passes({"total": 23, "accuracy": 88}))

    def test_missing_accuracy(self):
        self.assertEqual((10, None, None), parse_api_passes({"total": 10, "accuracy": None}))

    def test_player_name_alias_requires_same_surname_and_matching_initial(self):
        self.assertTrue(compatible_api_player_names("A. Zoubir", "Abdellah Zoubir"))
        self.assertTrue(compatible_api_player_names("N. Botis", "N. Botis"))
        self.assertTrue(compatible_api_player_names("O. Bjørtuft", "Odin Luras Bjørtuft"))
        self.assertTrue(compatible_api_player_names("Seol Young-Woo", "Young-woo Seol"))
        self.assertFalse(compatible_api_player_names("A. Silva", "B. Silva"))
        self.assertFalse(compatible_api_player_names("M. Sylla", "Mamadou Sarr"))

    def test_player_name_comparison_transliterates_special_latin_letters(self):
        self.assertEqual("adrian saether", api_player_comparison_name("Adrian Sæther"))
        self.assertEqual("sondre sorlokk", api_player_comparison_name("Sondre Sørløkk"))
        self.assertEqual(
            "lukasz dorde dor thor oezil strasse eli",
            api_player_comparison_name("Łukasz Đorđe Ðór Þór Œzil Straße Əli"),
        )
        self.assertTrue(compatible_api_player_names("A. Saether", "Adrian Sæther"))
        self.assertTrue(compatible_api_player_names("S. Sorlokk", "Sondre Sørløkk"))
        self.assertFalse(compatible_api_player_names("A. Saether", "Bjørn Sæther"))

    def test_transliteration_does_not_change_api_identity_keys(self):
        self.assertNotEqual(
            api_player_identity_key(1, "Sondre Sørløkk"),
            api_player_identity_key(1, "Sondre Sorlokk"),
        )

    def test_compound_surname_comparison_requires_abbreviation_and_subset(self):
        self.assertTrue(
            compatible_api_player_compound_names("M. Spiten-Nysaeter", "Mats Spiten")
        )
        self.assertTrue(
            compatible_api_player_compound_names(
                "S. Sjovold", "Stian Sjøvold Thorstensen"
            )
        )
        self.assertFalse(
            compatible_api_player_compound_names(
                "Rubén García", "Raúl García de Haro"
            )
        )
        self.assertFalse(compatible_api_player_compound_names("A. Smith", "B. Smith-Jones"))

    def test_reused_provider_player_id_is_disambiguated_by_name(self):
        self.assertNotEqual(
            api_player_identity_key(26389, "Renat Dadaşov"),
            api_player_identity_key(26389, "Rüfət Dadaşov"),
        )


class ApiFootballScheduleObservationTests(unittest.TestCase):
    def test_status_mapping_preserves_unknown_codes(self):
        self.assertEqual("scheduled", canonical_api_football_status("NS"))
        self.assertEqual("live", canonical_api_football_status("HT"))
        self.assertEqual("delayed", canonical_api_football_status("INT"))
        self.assertEqual("final", canonical_api_football_status("AET"))
        self.assertEqual("postponed", canonical_api_football_status("PST"))
        self.assertEqual("administrative_result", canonical_api_football_status("WO"))
        self.assertEqual("unknown", canonical_api_football_status("NEW_CODE"))
        self.assertEqual(
            "administrative_result",
            canonical_api_football_status("FT", administrative_unplayed=True),
        )

    def test_fixture_payload_appends_idempotent_schedule_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Warehouse(
                Path(directory) / "test.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
                payload = {
                    "response": [{
                        "fixture": {
                            "id": 123,
                            "date": "2026-07-10T18:00:00+00:00",
                            "status": {"short": "PST"},
                            "venue": {"name": "Test Ground"},
                        },
                        "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
                        "teams": {
                            "home": {"id": 1, "name": "Alpha FC"},
                            "away": {"id": 2, "name": "Beta FC"},
                        },
                        "score": {},
                    }],
                }
                item = {
                    "retrieved_at": "2026-07-10T12:00:00+00:00",
                    "content_sha256": "schedule-content",
                    "_raw_artifact_id": "schedule-artifact",
                }
                loader.load_api_football_payload(payload, item, "fixtures_by_date")
                loader.load_api_football_payload(payload, item, "fixtures_by_date")
                row = warehouse.connection.execute(
                    """
                    SELECT provider_status, canonical_status, scheduled_kickoff,
                           observed_at, retrieved_at, raw_artifact_id
                    FROM fixture_schedule_observation
                    """
                ).fetchone()
                self.assertEqual("PST", row[0])
                self.assertEqual("postponed", row[1])
                self.assertEqual(datetime(2026, 7, 10, 18, tzinfo=timezone.utc), row[2])
                self.assertIsNone(row[3])
                self.assertEqual(datetime(2026, 7, 10, 12, tzinfo=timezone.utc), row[4])
                self.assertEqual("schedule-artifact", row[5])
                self.assertEqual(
                    1,
                    warehouse.connection.execute(
                        "SELECT count(*) FROM fixture_schedule_observation"
                    ).fetchone()[0],
                )

                rescheduled_payload = json.loads(json.dumps(payload))
                rescheduled_fixture = rescheduled_payload["response"][0]["fixture"]
                rescheduled_fixture["date"] = "2026-07-12T18:00:00+00:00"
                rescheduled_fixture["status"]["short"] = "NS"
                loader.load_api_football_payload(
                    rescheduled_payload,
                    {
                        "retrieved_at": "2026-07-11T12:00:00+00:00",
                        "content_sha256": "rescheduled-content",
                        "_raw_artifact_id": "rescheduled-artifact",
                    },
                    "fixtures_by_date",
                )
                self.assertEqual(
                    2,
                    warehouse.connection.execute(
                        "SELECT count(*) FROM fixture_schedule_observation"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    [("PST", "postponed"), ("NS", "scheduled")],
                    warehouse.connection.execute(
                        """
                        SELECT provider_status, canonical_status
                        FROM fixture_schedule_observation
                        ORDER BY retrieved_at
                        """
                    ).fetchall(),
                )
            finally:
                warehouse.close()

    def test_unknown_status_creates_warning_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Warehouse(
                Path(directory) / "test.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
                payload = {
                    "response": [{
                        "fixture": {
                            "id": 456,
                            "date": "2026-07-10T18:00:00+00:00",
                            "status": {"short": "NEW_CODE"},
                            "venue": {},
                        },
                        "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
                        "teams": {
                            "home": {"id": 3, "name": "Gamma FC"},
                            "away": {"id": 4, "name": "Delta FC"},
                        },
                        "score": {},
                    }],
                }
                item = {
                    "retrieved_at": "2026-07-10T12:00:00+00:00",
                    "content_sha256": "unknown-content",
                    "_raw_artifact_id": "unknown-artifact",
                }
                loader.load_api_football_payload(payload, item, "fixtures_by_date")
                observation = warehouse.connection.execute(
                    "SELECT canonical_status FROM fixture_schedule_observation"
                ).fetchone()
                self.assertEqual(("unknown",), observation)
                issue = warehouse.connection.execute(
                    """
                    SELECT rule_code, severity, status, details
                    FROM data_quality_issue
                    WHERE rule_code = 'api_unknown_fixture_status'
                    """
                ).fetchone()
                self.assertEqual("api_unknown_fixture_status", issue[0])
                self.assertEqual("warning", issue[1])
                self.assertEqual("open", issue[2])
                self.assertEqual({"provider_status": "NEW_CODE"}, json.loads(issue[3]))
            finally:
                warehouse.close()


if __name__ == "__main__":
    unittest.main()
