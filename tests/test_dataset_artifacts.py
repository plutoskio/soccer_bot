from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import (
    read_regulation_feature_artifact,
    write_regulation_feature_artifact,
)
from soccer_bot.datasets.features import feature_rows_sha256
from tests.test_walk_forward import feature_row


class DatasetArtifactTests(unittest.TestCase):
    def test_feature_parquet_round_trip_and_manifest(self):
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        rows = [
            feature_row("one", start, 1, 0),
            feature_row("two", start.replace(day=2), 0, 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            warehouse = root / "warehouse.duckdb"
            warehouse.write_bytes(b"test warehouse identity")
            source = root / "config.json"
            source.write_text("{}\n", encoding="utf-8")

            manifest = write_regulation_feature_artifact(
                rows,
                output_dir=root / "artifact",
                warehouse_path=warehouse,
                source_files={"configuration": source},
            )
            restored = read_regulation_feature_artifact(
                root / "artifact" / "features.parquet"
            )

        self.assertEqual(restored, rows)
        self.assertEqual(manifest["dataset"]["rows"], 2)
        self.assertEqual(manifest["dataset"]["fixtures"], 2)
        self.assertEqual(
            manifest["dataset"]["horizon_rows"], {"pre_lineup_24h_v1": 2}
        )
        self.assertEqual(
            manifest["dataset"]["logical_rows_sha256"],
            feature_rows_sha256(rows),
        )
        self.assertEqual(feature_rows_sha256(restored), feature_rows_sha256(rows))


if __name__ == "__main__":
    unittest.main()
