"""Automate read-only Fortnite asset export and iterative STW catalog closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from stw_assets import (
    asset_export_queue,
    ingest_asset_directory,
    latest_asset_snapshot_id,
)
from stw_pipeline import connect


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PROJECT = REPO_ROOT / "tools" / "cue4parse-exporter" / "StwAssetExporter.csproj"
EXPORTER_DLL = (
    REPO_ROOT
    / "tools"
    / "cue4parse-exporter"
    / "bin"
    / "Release"
    / "net10.0"
    / "StwAssetExporter.dll"
)
LOCAL_DOTNET = REPO_ROOT / ".tools" / "dotnet" / "dotnet.exe"
DEFAULT_FMODEL_SETTINGS = (
    Path(os.environ.get("APPDATA", "")) / "FModel" / "AppSettings.json"
)
EXPORTER_VERSION = "CUE4Parse-1.2.2.202608"


class AcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicFModelSettings:
    settings_path: Path
    game_directory: Path
    output_directory: Path
    export_directory: Path
    mapping_path: Path
    ue_version: int
    aes_configured: bool
    dynamic_key_count: int


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_public_fmodel_settings(path: Path = DEFAULT_FMODEL_SETTINGS) -> PublicFModelSettings:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise AcquisitionError(f"FModel settings not found: {path}")
    payload = _load_json(path)
    per_directory = payload.get("PerDirectory")
    if not isinstance(per_directory, dict) or not per_directory:
        raise AcquisitionError("FModel has no configured game directory")
    configured_game = Path(str(payload.get("GameDirectory") or ""))
    candidates = [value for value in per_directory.values() if isinstance(value, dict)]
    game = next(
        (
            candidate
            for candidate in candidates
            if configured_game
            and Path(str(candidate.get("GameDirectory") or "")) == configured_game
        ),
        candidates[0],
    )
    output = Path(str(payload.get("OutputDirectory") or "")).expanduser().resolve()
    export = Path(
        str(payload.get("RawDataDirectory") or output / "Exports")
    ).expanduser().resolve()
    game_directory = Path(str(game.get("GameDirectory") or "")).expanduser().resolve()
    mappings = output / ".data" / "mappings"
    mapping = max(
        mappings.glob("*.usmap"),
        key=lambda candidate: candidate.stat().st_mtime_ns,
        default=None,
    )
    if mapping is None:
        raise AcquisitionError(f"FModel mapping not found beneath {mappings}")
    aes = game.get("AesKeys") if isinstance(game.get("AesKeys"), dict) else {}
    main_key = aes.get("mainKey")
    dynamic_keys = aes.get("dynamicKeys") if isinstance(aes.get("dynamicKeys"), list) else []
    return PublicFModelSettings(
        settings_path=path,
        game_directory=game_directory,
        output_directory=output,
        export_directory=export,
        mapping_path=mapping.resolve(),
        ue_version=int(game.get("UeVersion") or 0),
        aes_configured=isinstance(main_key, str) and len(main_key) == 66,
        dynamic_key_count=len(dynamic_keys),
    )


def _dotnet_path() -> Path:
    if LOCAL_DOTNET.is_file():
        return LOCAL_DOTNET
    located = shutil.which("dotnet")
    if located:
        return Path(located)
    raise AcquisitionError(
        "a .NET 10 SDK/runtime is required; run the documented one-time setup"
    )


def _subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    environment["DOTNET_NOLOGO"] = "1"
    environment["DOTNET_CLI_HOME"] = str(REPO_ROOT / ".tools" / "dotnet-home")
    return environment


def build_exporter(*, restore: bool = False) -> dict[str, Any]:
    dotnet = _dotnet_path()
    command = [
        str(dotnet),
        "build",
        str(EXPORTER_PROJECT),
        "--configuration",
        "Release",
    ]
    assets_file = EXPORTER_PROJECT.parent / "obj" / "project.assets.json"
    if not restore and assets_file.is_file():
        command.append("--no-restore")
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AcquisitionError(
            "CUE4Parse exporter build failed:\n"
            + (completed.stdout + completed.stderr).strip()
        )
    return {
        "status": "built",
        "exporter_dll": str(EXPORTER_DLL),
        "dotnet": str(dotnet),
    }


def _process_running(image_name: str) -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return image_name.casefold() in completed.stdout.casefold()


def _archive_fingerprint(settings: PublicFModelSettings) -> str:
    digest = hashlib.sha256()
    for pattern in ("*.pak", "*.utoc", "*.uondemandtoc"):
        for path in sorted(settings.game_directory.glob(pattern)):
            stat = path.stat()
            digest.update(path.name.encode("utf-8"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    stat = settings.mapping_path.stat()
    digest.update(settings.mapping_path.name.encode("utf-8"))
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def write_manifest(path: Path, scopes: Sequence[dict[str, str]]) -> dict[str, Any]:
    normalized = sorted(
        {
            (str(scope["kind"]).lower(), str(scope["path"]))
            for scope in scopes
        },
        key=lambda item: (item[0], item[1].casefold()),
    )
    payload = {
        "schema_version": 1,
        "scopes": [{"kind": kind, "path": value} for kind, value in normalized],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def run_exporter(
    manifest: Path,
    *,
    settings_path: Path = DEFAULT_FMODEL_SETTINGS,
    output_root: Path | None = None,
    max_packages: int = 5000,
    dry_run: bool = True,
    contains: str | None = None,
) -> dict[str, Any]:
    settings = load_public_fmodel_settings(settings_path)
    output_root = (output_root or settings.export_directory).expanduser().resolve()
    if not EXPORTER_DLL.is_file():
        build_exporter()
    command = [
        str(_dotnet_path()),
        str(EXPORTER_DLL),
        "--fmodel-settings",
        str(settings.settings_path),
        "--manifest",
        str(manifest.expanduser().resolve()),
        "--output",
        str(output_root),
        "--max-packages",
        str(max_packages),
    ]
    if dry_run:
        command.append("--dry-run")
    if contains:
        command.extend(("--contains", contains))
    before = _archive_fingerprint(settings)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AcquisitionError(
            "asset exporter returned invalid output: "
            + (completed.stdout + completed.stderr).strip()
        ) from error
    after = _archive_fingerprint(settings)
    if before != after:
        raise AcquisitionError(
            "Fortnite archives or FModel mappings changed during acquisition; "
            "discard this run and retry after the game update finishes"
        )
    payload["exit_code"] = completed.returncode
    if payload.get("status") == "error":
        raise AcquisitionError(payload.get("message") or "asset exporter failed")
    return payload


def export_manifest(
    manifest: Path,
    *,
    settings_path: Path = DEFAULT_FMODEL_SETTINGS,
    output_root: Path | None = None,
    max_packages: int = 5000,
    confirm_export: bool = False,
    contains: str | None = None,
) -> dict[str, Any]:
    settings = load_public_fmodel_settings(settings_path)
    output_root = (output_root or settings.export_directory).expanduser().resolve()
    if output_root == REPO_ROOT or REPO_ROOT in output_root.parents:
        raise AcquisitionError("raw Fortnite exports must remain outside the Git repository")
    preview = run_exporter(
        manifest,
        settings_path=settings_path,
        output_root=output_root,
        max_packages=max_packages,
        dry_run=True,
        contains=contains,
    )
    if not confirm_export:
        return {"status": "preview_only", "preview": preview}
    if _process_running("FModel.exe"):
        raise AcquisitionError("close FModel before an automated export writes its output")
    if preview.get("status") == "refused":
        raise AcquisitionError(
            f"export refused: {preview.get('matched_package_count')} packages exceed "
            f"the limit of {max_packages}"
        )
    if int(preview.get("matched_package_count") or 0) == 0:
        return {"status": "no_matches", "preview": preview}
    exported = run_exporter(
        manifest,
        settings_path=settings_path,
        output_root=output_root,
        max_packages=max_packages,
        dry_run=False,
        contains=contains,
    )
    return {"status": exported["status"], "preview": preview, "export": exported}


def queue_manifest(
    connection: sqlite3.Connection,
    output_path: Path,
    *,
    snapshot_id: int | None = None,
    max_priority: int = 2,
    include_low_priority: bool = False,
) -> dict[str, Any]:
    queue = asset_export_queue(
        connection,
        snapshot_id,
        max_priority=max_priority,
        include_low_priority=include_low_priority,
    )
    manifest = write_manifest(
        output_path,
        [
            {"kind": "package", "path": asset["package_path"]}
            for asset in queue["assets"]
        ],
    )
    return {
        "snapshot_id": queue["snapshot_id"],
        "asset_count": len(queue["assets"]),
        "queue_counts": queue["counts"],
        "manifest_path": str(output_path.resolve()),
        "manifest": manifest,
    }


def close_dependencies(
    connection: sqlite3.Connection,
    *,
    settings_path: Path = DEFAULT_FMODEL_SETTINGS,
    output_root: Path | None = None,
    max_priority: int = 2,
    max_packages: int = 500,
    max_rounds: int = 5,
    confirm_export: bool = False,
) -> dict[str, Any]:
    if not confirm_export:
        raise AcquisitionError("--confirm-export is required for iterative closure")
    settings = load_public_fmodel_settings(settings_path)
    output_root = (output_root or settings.export_directory).expanduser().resolve()
    snapshot_id = latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise AcquisitionError("the catalog has no ready asset snapshot")
    build = connection.execute(
        """
        SELECT build.build_key, build.game_version, build.changelist
        FROM asset_snapshots snapshot
        JOIN game_builds build ON build.id=snapshot.game_build_id
        WHERE snapshot.id=?
        """,
        (snapshot_id,),
    ).fetchone()
    rounds: list[dict[str, Any]] = []
    prior_queue: tuple[str, ...] | None = None
    with tempfile.TemporaryDirectory(prefix="stw-asset-closure-") as temporary:
        manifest_path = Path(temporary) / "queue.json"
        for round_number in range(1, max_rounds + 1):
            queued = queue_manifest(
                connection,
                manifest_path,
                snapshot_id=snapshot_id,
                max_priority=max_priority,
            )
            paths = tuple(scope["path"] for scope in queued["manifest"]["scopes"])
            if not paths:
                return {
                    "status": "closed",
                    "snapshot_id": snapshot_id,
                    "rounds": rounds,
                    "remaining_queue_count": 0,
                }
            if paths == prior_queue:
                return {
                    "status": "stalled",
                    "snapshot_id": snapshot_id,
                    "rounds": rounds,
                    "remaining_queue_count": len(paths),
                    "reason": "the exact unresolved queue did not change",
                }
            prior_queue = paths
            acquired = export_manifest(
                manifest_path,
                settings_path=settings_path,
                output_root=output_root,
                max_packages=max_packages,
                confirm_export=True,
            )
            export_result = acquired.get("export") or {}
            if int(export_result.get("exported_package_count") or 0) == 0:
                return {
                    "status": "blocked",
                    "snapshot_id": snapshot_id,
                    "rounds": rounds,
                    "remaining_queue_count": len(paths),
                    "reason": "none of the queued packages could be exported",
                    "acquisition": {
                        "status": acquired.get("status"),
                        "matched_package_count": acquired.get("preview", {}).get(
                            "matched_package_count", 0
                        ),
                        "unmatched_scope_count": len(
                            acquired.get("preview", {}).get("unmatched_scopes", [])
                        ),
                        "failed_package_count": export_result.get(
                            "failed_package_count", 0
                        ),
                        "failure_sample": export_result.get("failures", [])[:10],
                    },
                }
            ingested = ingest_asset_directory(
                connection,
                output_root,
                build_key=build["build_key"],
                game_version=build["game_version"],
                changelist=build["changelist"],
                exporter_name="CUE4Parse",
                exporter_version=EXPORTER_VERSION,
            )
            snapshot_id = ingested["snapshot_id"]
            rounds.append(
                {
                    "round": round_number,
                    "queued_package_count": len(paths),
                    "matched_package_count": acquired["preview"]["matched_package_count"],
                    "exported_package_count": export_result["exported_package_count"],
                    "failed_package_count": export_result["failed_package_count"],
                    "snapshot_id": snapshot_id,
                    "manifest_sha256": ingested["manifest_sha256"],
                    "unresolved_counts": ingested["unresolved_counts"],
                }
            )
    remaining = asset_export_queue(
        connection, snapshot_id, max_priority=max_priority
    )["assets"]
    return {
        "status": "round_limit_reached" if remaining else "closed",
        "snapshot_id": snapshot_id,
        "rounds": rounds,
        "remaining_queue_count": len(remaining),
    }


def doctor(settings_path: Path = DEFAULT_FMODEL_SETTINGS) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        settings = load_public_fmodel_settings(settings_path)
    except Exception as error:
        return {
            "status": "failed",
            "checks": [{"name": "fmodel_settings", "ok": False, "detail": str(error)}],
        }
    for name, ok, detail in (
        ("fmodel_settings", settings.settings_path.is_file(), str(settings.settings_path)),
        ("fortnite_archives", settings.game_directory.is_dir(), str(settings.game_directory)),
        ("fmodel_mapping", settings.mapping_path.is_file(), settings.mapping_path.name),
        ("fmodel_aes", settings.aes_configured, f"{settings.dynamic_key_count} dynamic keys"),
        ("fmodel_output", settings.export_directory.is_dir(), str(settings.export_directory)),
        ("dotnet", _dotnet_path().is_file(), str(_dotnet_path())),
        ("exporter_project", EXPORTER_PROJECT.is_file(), str(EXPORTER_PROJECT)),
        ("exporter_built", EXPORTER_DLL.is_file(), str(EXPORTER_DLL)),
        ("fmodel_closed", not _process_running("FModel.exe"), "required only for writes"),
    ):
        checks.append({"name": name, "ok": ok, "detail": detail})
    return {
        "status": "healthy" if all(check["ok"] for check in checks[:-1]) else "failed",
        "checks": checks,
        "security": {
            "keys_loaded_by_exporter_from_fmodel_settings": True,
            "keys_emitted_or_stored_in_git": False,
            "fortnite_archives_opened_read_only": True,
            "network_on_demand_downloads_enabled": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    parser.add_argument("--fmodel-settings", type=Path, default=DEFAULT_FMODEL_SETTINGS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="verify local exporter prerequisites without secrets")
    build = commands.add_parser("build", help="build the local CUE4Parse exporter")
    build.add_argument("--restore", action="store_true")
    queue = commands.add_parser("queue", help="write an exact manifest from unresolved references")
    queue.add_argument("output", type=Path)
    queue.add_argument("--snapshot-id", type=int)
    queue.add_argument("--max-priority", type=int, choices=(0, 1, 2, 3, 4), default=2)
    queue.add_argument("--all", action="store_true", dest="include_low_priority")
    export = commands.add_parser("export", help="preview or run one controlled manifest")
    export.add_argument("manifest", type=Path)
    export.add_argument("--output", type=Path)
    export.add_argument("--max-packages", type=int, default=5000)
    export.add_argument("--contains")
    export.add_argument("--confirm-export", action="store_true")
    close = commands.add_parser("close", help="export, ingest, and recompute exact dependencies")
    close.add_argument("--output", type=Path)
    close.add_argument("--max-priority", type=int, choices=(0, 1, 2, 3, 4), default=2)
    close.add_argument("--max-packages", type=int, default=500)
    close.add_argument("--max-rounds", type=int, default=5)
    close.add_argument("--confirm-export", action="store_true", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        payload = doctor(args.fmodel_settings)
    elif args.command == "build":
        payload = build_exporter(restore=args.restore)
    elif args.command == "export":
        payload = export_manifest(
            args.manifest,
            settings_path=args.fmodel_settings,
            output_root=args.output,
            max_packages=args.max_packages,
            confirm_export=args.confirm_export,
            contains=args.contains,
        )
    else:
        connection = connect(args.db)
        try:
            if args.command == "queue":
                payload = queue_manifest(
                    connection,
                    args.output,
                    snapshot_id=args.snapshot_id,
                    max_priority=args.max_priority,
                    include_low_priority=args.include_low_priority,
                )
            else:
                payload = close_dependencies(
                    connection,
                    settings_path=args.fmodel_settings,
                    output_root=args.output,
                    max_priority=args.max_priority,
                    max_packages=args.max_packages,
                    max_rounds=args.max_rounds,
                    confirm_export=args.confirm_export,
                )
        finally:
            connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"failed", "error"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcquisitionError as error:
        print(json.dumps({"status": "error", "message": str(error)}, indent=2))
        raise SystemExit(2)
