from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

from soccer_bot.config import load_json
from soccer_bot.modeling.reproducibility import (
    ChampionReproducibilityError,
    SERVING_BUNDLE_FILES,
    build_champion_reproducibility_manifest,
    champion_training_recipe_sha256,
    validate_champion_reproducibility,
)


class ChampionReproducibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.directory.name)
        self.model = self.model_dir / "model.json"
        self.model.write_bytes(
            (ROOT / "data/models/regulation_champion_v1/model.json").read_bytes()
        )
        self.specification_path = (
            ROOT / "config/models/regulation_champion_v1.json"
        )
        self.specification = load_json(self.specification_path)
        self.manifest = build_champion_reproducibility_manifest(
            repository_root=ROOT,
            model_path=self.model,
            specification=self.specification,
            training_identity={
                "targets": 38_445,
                "feature_rows": 73_258,
                "feature_rows_sha256": "a" * 64,
                "rich_rows_sha256": "b" * 64,
            },
            warehouse_path=None,
            legacy_warehouse_evidence={"recorded_size_bytes": 123},
            legacy_training_implementation=True,
        )
        (self.model_dir / "reproducibility.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_manifest_is_small_and_validates_without_copying_the_warehouse(self):
        observed = validate_champion_reproducibility(
            model_path=self.model,
            model_config_path=self.specification_path,
            repository_root=ROOT,
        )

        self.assertEqual(
            observed["training_warehouse"]["status"],
            "unavailable_for_legacy_artifact",
        )
        self.assertLess(
            (self.model_dir / "reproducibility.json").stat().st_size,
            50_000,
        )

    def test_tampered_model_is_rejected(self):
        self.model.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(
            ChampionReproducibilityError, "artifact hash mismatch"
        ):
            validate_champion_reproducibility(
                model_path=self.model,
                model_config_path=self.specification_path,
                repository_root=ROOT,
            )

    def test_serving_only_policy_does_not_change_training_recipe_identity(self):
        changed = deepcopy(self.specification)
        changed["issuance"]["maximum_issue_delay_minutes"] = 5

        self.assertEqual(
            champion_training_recipe_sha256(self.specification),
            champion_training_recipe_sha256(changed),
        )

    def test_training_policy_change_changes_recipe_identity(self):
        changed = deepcopy(self.specification)
        changed["production_refit"]["temperature"] = "different"

        self.assertNotEqual(
            champion_training_recipe_sha256(self.specification),
            champion_training_recipe_sha256(changed),
        )

    def test_changed_serving_code_is_rejected(self):
        repository = self.model_dir / "repository"
        for relative in SERVING_BUNDLE_FILES:
            destination = repository / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        package = repository / "artifact"
        package.mkdir()
        model = package / "model.json"
        shutil.copy2(ROOT / "data/models/regulation_champion_v1/model.json", model)
        specification_path = repository / "config/models/regulation_champion_v1.json"
        manifest = build_champion_reproducibility_manifest(
            repository_root=repository,
            model_path=model,
            specification=load_json(specification_path),
            training_identity={
                "targets": 1,
                "feature_rows": 1,
                "feature_rows_sha256": "a" * 64,
                "rich_rows_sha256": "b" * 64,
            },
            warehouse_path=None,
            legacy_warehouse_evidence={"recorded_size_bytes": 1},
            legacy_training_implementation=True,
        )
        (package / "reproducibility.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        changed = repository / "src/soccer_bot/datasets/upcoming.py"
        changed.write_text(changed.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ChampionReproducibilityError, "serving bundle mismatch"
        ):
            validate_champion_reproducibility(
                model_path=model,
                model_config_path=specification_path,
                repository_root=repository,
            )


if __name__ == "__main__":
    unittest.main()
