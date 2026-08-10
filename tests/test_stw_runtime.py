from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from stw_runtime import (  # noqa: E402
    crit_rating_to_chance,
    homebase_rating_to_difficulty,
    runtime_semantics_report,
)
from test_stw_assets import _write_export, write_golden_slice, write_weapon_slice  # noqa: E402


class RuntimeSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_weapon_slice(exports)
        write_golden_slice(exports)
        _write_export(
            exports,
            "Tables/HomebaseRatingDifficultyMapping.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "HomebaseRatingDifficultyMapping",
                    "Package": "/Game/Balance/DataTables/HomebaseRatingDifficultyMapping",
                    "Properties": {
                        "RowStruct": {
                            "ObjectName": "Class'HomebaseRatingDifficultyMappingData'",
                            "ObjectPath": "/Script/FortniteGame",
                        }
                    },
                    "Rows": {
                        "0": {"Difficulty": 1},
                        "160": {"Difficulty": 52},
                    },
                }
            ],
        )
        self.connection = connect(root / "runtime.sqlite3")
        self.summary = ingest_asset_directory(
            self.connection,
            exports,
            build_key="runtime-golden",
            game_version="test",
            exporter_version="test",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_crit_rating_curve_lookup_is_exact_and_provenanced(self) -> None:
        chance, source, error = crit_rating_to_chance(
            self.connection, self.summary["snapshot_id"], 30.0
        )
        self.assertIsNone(error)
        self.assertAlmostEqual(0.375, chance)
        self.assertEqual("Item.All.CritRatingToCritChance", source["row_name"])
        self.assertTrue(source["content_sha256"])

    def test_curve_does_not_guess_extrapolation(self) -> None:
        chance, source, error = crit_rating_to_chance(
            self.connection, self.summary["snapshot_id"], 10001.0
        )
        self.assertIsNone(chance)
        self.assertIsNotNone(source)
        self.assertEqual("curve_extrapolation_unproven", error)

    def test_runtime_row_struct_preserves_complete_lookup_table(self) -> None:
        rows = self.connection.execute(
            """
            SELECT row.row_name, row.row_json
            FROM catalog_data_rows row
            JOIN catalog_data_tables table_row ON table_row.id=row.data_table_id
            WHERE table_row.snapshot_id=?
              AND table_row.table_name='HomebaseRatingDifficultyMapping'
            ORDER BY CAST(row.row_name AS INTEGER)
            """,
            (self.summary["snapshot_id"],),
        ).fetchall()
        self.assertEqual(["0", "160"], [row["row_name"] for row in rows])
        difficulty, source, error = homebase_rating_to_difficulty(
            self.connection, self.summary["snapshot_id"], 160
        )
        self.assertIsNone(error)
        self.assertEqual(52, difficulty)
        self.assertEqual("160", source["row_name"])

    def test_report_keeps_native_runtime_boundaries_explicit(self) -> None:
        report = runtime_semantics_report(
            self.connection, self.summary["snapshot_id"]
        )
        self.assertEqual("supported", report["rules"]["crit_rating_conversion"]["status"])
        self.assertEqual("opaque", report["rules"]["reload_speed"]["status"])
        self.assertEqual("opaque", report["rules"]["elemental_matchups"]["status"])
        self.assertFalse(report["absolute_live_damage_defensible"])
        self.assertEqual("unsupported", report["nocturno_signature"]["status"])


if __name__ == "__main__":
    unittest.main()
