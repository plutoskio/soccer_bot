from __future__ import annotations

import copy
from pathlib import Path
import unittest

from soccer_bot.config import load_json
from soccer_bot.modeling.family_registry import (
    FamilyRegistryError,
    load_specialized_family_registry,
    parse_specialized_family_registry,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "models" / "specialized_family_registry_v1.json"


class SpecializedFamilyRegistryTests(unittest.TestCase):
    def test_repository_registry_has_unambiguous_family_ownership(self) -> None:
        registry = load_specialized_family_registry(REGISTRY_PATH)

        self.assertEqual(registry.registry_version, "specialized_family_registry_v1")
        self.assertEqual(
            registry.family_for_contract("regulation_moneyline").family_key,
            "regulation_moneyline",
        )
        score = registry.family("regulation_score")
        self.assertIsNone(score.designated_model)
        self.assertEqual(
            score.model("regulation_score_grid_v3_prospective_shadow").status,
            "experimental",
        )
        self.assertEqual(
            registry.family("regulation_moneyline").designated_model.model_version,
            "regulation_champion_v1",
        )

    def test_contract_cannot_belong_to_two_families(self) -> None:
        value = load_json(REGISTRY_PATH)
        value["families"][1]["contract_keys"].append("regulation_moneyline")

        with self.assertRaisesRegex(FamilyRegistryError, "multiple owners"):
            parse_specialized_family_registry(value)

    def test_trained_model_cannot_be_designated(self) -> None:
        value = load_json(REGISTRY_PATH)
        score = value["families"][1]
        score["designated_model_version"] = "regulation_score_specialist_v1"
        score["models"][1]["role"] = "designated"

        with self.assertRaisesRegex(FamilyRegistryError, "unreleased model"):
            parse_specialized_family_registry(value)

    def test_validated_model_requires_artifact_hash_and_evaluation(self) -> None:
        value = load_json(REGISTRY_PATH)
        del value["families"][0]["models"][0]["logical_sha256"]

        with self.assertRaisesRegex(FamilyRegistryError, "logical_sha256"):
            parse_specialized_family_registry(value)

    def test_model_paths_cannot_escape_repository(self) -> None:
        value = copy.deepcopy(load_json(REGISTRY_PATH))
        value["families"][0]["models"][0]["artifact_path"] = "../model.json"

        with self.assertRaisesRegex(FamilyRegistryError, "inside the repository"):
            parse_specialized_family_registry(value)

    def test_market_evidence_cannot_become_independent_model_feature(self) -> None:
        value = load_json(REGISTRY_PATH)
        value["market_evidence"]["independent_model_feature"] = True

        with self.assertRaisesRegex(FamilyRegistryError, "cannot be"):
            parse_specialized_family_registry(value)


if __name__ == "__main__":
    unittest.main()
