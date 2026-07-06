from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.loaders import (
    api_player_identity_key,
    api_player_comparison_name,
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


if __name__ == "__main__":
    unittest.main()
