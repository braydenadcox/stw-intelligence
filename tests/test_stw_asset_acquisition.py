from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stw_asset_acquisition import (  # noqa: E402
    AcquisitionError,
    PublicFModelSettings,
    export_manifest,
    load_public_fmodel_settings,
    queue_manifest,
    run_exporter,
    write_manifest,
)
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice  # noqa: E402


def _write_settings(root: Path) -> tuple[Path, PublicFModelSettings]:
    output = root / "FModelOutput"
    game = root / "Paks"
    mapping = output / ".data" / "mappings" / "Fortnite.usmap"
    export = output / "Exports"
    game.mkdir(parents=True)
    export.mkdir(parents=True)
    mapping.parent.mkdir(parents=True)
    mapping.write_bytes(b"mapping")
    settings_path = root / "AppSettings.json"
    settings_path.write_text(
        json.dumps(
            {
                "OutputDirectory": str(output),
                "RawDataDirectory": str(export),
                "GameDirectory": str(game),
                "LastAuthResponse": {"access_token": "must-never-be-emitted"},
                "PerDirectory": {
                    str(game): {
                        "GameDirectory": str(game),
                        "UeVersion": 84410368,
                        "AesKeys": {
                            "mainKey": "0x" + "1" * 64,
                            "dynamicKeys": [
                                {
                                    "guid": "2" * 32,
                                    "key": "0x" + "3" * 64,
                                    "name": "pakchunk-test",
                                }
                            ],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    public = PublicFModelSettings(
        settings_path=settings_path.resolve(),
        game_directory=game.resolve(),
        output_directory=output.resolve(),
        export_directory=export.resolve(),
        mapping_path=mapping.resolve(),
        ue_version=84410368,
        aes_configured=True,
        dynamic_key_count=1,
    )
    return settings_path, public


class AssetAcquisitionTests(unittest.TestCase):
    def test_public_settings_expose_health_but_not_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path, _ = _write_settings(Path(directory))
            result = load_public_fmodel_settings(settings_path)
        serialized = repr(result)
        self.assertTrue(result.aes_configured)
        self.assertEqual(result.dynamic_key_count, 1)
        self.assertNotIn("must-never-be-emitted", serialized)
        self.assertNotIn("0x" + "1" * 64, serialized)

    def test_manifest_is_deduplicated_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            result = write_manifest(
                path,
                [
                    {"kind": "package", "path": "/SaveTheWorld/B"},
                    {"kind": "package", "path": "/SaveTheWorld/A"},
                    {"kind": "package", "path": "/SaveTheWorld/A"},
                ],
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result, persisted)
        self.assertEqual(
            [scope["path"] for scope in result["scopes"]],
            ["/SaveTheWorld/A", "/SaveTheWorld/B"],
        )

    def test_queue_manifest_contains_only_exact_graph_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exports = root / "exports"
            files = write_golden_slice(exports)
            for path in files:
                if path.name.startswith("Kit_Perk_H_AssaultDamage_"):
                    path.unlink()
            connection = connect(root / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(connection, exports, build_key="queue")
                result = queue_manifest(
                    connection,
                    root / "queue.json",
                    snapshot_id=summary["snapshot_id"],
                )
            finally:
                connection.close()
        paths = [scope["path"] for scope in result["manifest"]["scopes"]]
        self.assertIn(
            "/SaveTheWorld/Abilities/Player/Perks/Hero/AssaultDamage/"
            "Kit_Perk_H_AssaultDamage_T01",
            paths,
        )
        self.assertEqual(len(paths), len(set(paths)))

    def test_export_refuses_raw_output_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path, public = _write_settings(Path(directory))
            manifest = Path(directory) / "manifest.json"
            write_manifest(
                manifest,
                [{"kind": "package", "path": "/SaveTheWorld/Test"}],
            )
            with patch(
                "stw_asset_acquisition.load_public_fmodel_settings",
                return_value=public,
            ):
                with self.assertRaisesRegex(AcquisitionError, "outside the Git"):
                    export_manifest(
                        manifest,
                        settings_path=settings_path,
                        output_root=Path(__file__).resolve().parents[1] / "raw",
                    )

    def test_exporter_result_is_machine_read_and_archive_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path, public = _write_settings(root)
            manifest = root / "manifest.json"
            write_manifest(
                manifest,
                [{"kind": "package", "path": "/SaveTheWorld/Test"}],
            )
            completed = type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {"status": "dry_run", "matched_package_count": 1}
                    ),
                    "stderr": "",
                },
            )()
            with (
                patch(
                    "stw_asset_acquisition.load_public_fmodel_settings",
                    return_value=public,
                ),
                patch("stw_asset_acquisition._dotnet_path", return_value=Path("dotnet")),
                patch("stw_asset_acquisition.build_exporter"),
                patch("stw_asset_acquisition.subprocess.run", return_value=completed),
                patch(
                    "stw_asset_acquisition._archive_fingerprint",
                    side_effect=["before", "after"],
                ),
            ):
                with self.assertRaisesRegex(AcquisitionError, "changed during"):
                    run_exporter(
                        manifest,
                        settings_path=settings_path,
                        output_root=public.export_directory,
                    )


if __name__ == "__main__":
    unittest.main()
