#!/usr/bin/env python3
"""Ingest versioned, read-only FModel exports into an auditable STW catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from stw_pipeline import connect


PERK_KIT_RE = re.compile(r"^Kit_Perk_H_(?P<family>.+)_(?P<tier>T\d+)$")
NORMALIZER_VERSION = "phase2-v11"
ROSTER_PLAN_VERSION = "phase2-roster-v1"

STW_HERO_CLASSES = ("Commando", "Constructor", "Ninja", "Outlander")
SEMANTIC_DEPENDENCY_CATEGORIES = {
    "hero_perk_kit",
    "granted_gameplay_effect",
    "referenced_gameplay_effect",
    "referenced_ability_logic",
    "active_ability_logic",
    "inheritance",
    "balance_curve",
    "custom_calculation",
    "granted_ability",
    "ability_mechanic",
}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    content_sha256: str
    size_bytes: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory_exports(source_root: Path) -> tuple[list[SourceFile], str]:
    root = source_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    files = [
        SourceFile(
            path=path.resolve(),
            relative_path=path.resolve().relative_to(root).as_posix(),
            content_sha256=_sha256(path),
            size_bytes=path.stat().st_size,
        )
        for path in sorted(root.rglob("*.json"))
        if path.is_file()
    ]
    if not files:
        raise ValueError(f"no JSON exports found under {root}")
    manifest = hashlib.sha256()
    for item in files:
        manifest.update(item.relative_path.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(item.content_sha256.encode("ascii"))
        manifest.update(b"\n")
    return files, manifest.hexdigest()


def canonical_package_path(path: str | None) -> str | None:
    if not path or not path.startswith("/"):
        return None
    value = path.strip()
    final_slash = value.rfind("/")
    final_dot = value.rfind(".")
    if final_dot > final_slash:
        value = value[:final_dot]
    return value


def _target_selector(path: str) -> str | int | None:
    final_slash = path.rfind("/")
    final_dot = path.rfind(".")
    if final_dot <= final_slash:
        return None
    suffix = path[final_dot + 1 :]
    return int(suffix) if suffix.isdigit() else suffix


def _object_path(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("ObjectPath", "AssetPathName"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.startswith("/"):
            return candidate
    return None


def _walk_references(value: Any, property_path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        direct_keys = [
            key
            for key in ("ObjectPath", "AssetPathName")
            if isinstance(value.get(key), str) and value[key].startswith("/")
        ]
        for key in direct_keys:
            yield f"{property_path}.{key}", value[key]
        for key, child in value.items():
            if key not in direct_keys:
                yield from _walk_references(child, f"{property_path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_references(child, f"{property_path}[{index}]")


def _load_exports(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"expected an export object array in {path}")
    return payload


def _file_package(exports: list[dict[str, Any]]) -> str | None:
    for export in exports:
        package = canonical_package_path(export.get("Package"))
        if package:
            return package
    for export in exports:
        for _, target in _walk_references(export):
            package = canonical_package_path(target)
            if package:
                return package
    return None


def _class_path(value: Any) -> str | None:
    if isinstance(value, str):
        match = re.search(r"'(?P<path>/[^']+)'", value)
        return match.group("path") if match else value
    return None


def ingest_asset_directory(
    connection: sqlite3.Connection,
    source_root: Path,
    *,
    build_key: str,
    game_version: str | None = None,
    changelist: str | None = None,
    exporter_name: str = "FModel",
    exporter_version: str | None = None,
    build_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not build_key.strip():
        raise ValueError("build_key is required and must not be guessed")
    files, manifest_sha256 = inventory_exports(source_root)
    root = source_root.resolve()
    metadata_json = json.dumps(build_metadata or {}, sort_keys=True, separators=(",", ":"))
    existing_snapshot_id: int | None = None

    with connection:
        connection.execute(
            """
            INSERT INTO game_builds(build_key, game_version, changelist, metadata_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(build_key) DO UPDATE SET
                game_version=COALESCE(game_builds.game_version, excluded.game_version),
                changelist=COALESCE(game_builds.changelist, excluded.changelist)
            """,
            (build_key, game_version, changelist, metadata_json),
        )
        build_id = connection.execute(
            "SELECT id FROM game_builds WHERE build_key=?", (build_key,)
        ).fetchone()["id"]
        existing = connection.execute(
            """
            SELECT id, status FROM asset_snapshots
            WHERE game_build_id=? AND manifest_sha256=?
            """,
            (build_id, manifest_sha256),
        ).fetchone()
        if existing is not None:
            if existing["status"] != "ready":
                raw_file_count = connection.execute(
                    "SELECT COUNT(*) FROM asset_files WHERE snapshot_id=?",
                    (existing["id"],),
                ).fetchone()[0]
                if existing["status"] == "failed" and raw_file_count == 0:
                    connection.execute(
                        "DELETE FROM asset_snapshots WHERE id=?", (existing["id"],)
                    )
                    existing = None
                else:
                    raise RuntimeError(
                        f"asset snapshot {existing['id']} is not ready: "
                        f"{existing['status']}"
                    )
        if existing is not None:
            completed = connection.execute(
                """
                SELECT id FROM asset_normalization_runs
                WHERE snapshot_id=? AND normalizer_version=? AND status='ready'
                """,
                (existing["id"], NORMALIZER_VERSION),
            ).fetchone()
            if completed is not None:
                return _snapshot_summary(connection, existing["id"], idempotent=True)
            existing_snapshot_id = existing["id"]
            snapshot_id = existing["id"]
        else:
            cursor = connection.execute(
                """
                INSERT INTO asset_snapshots(
                    game_build_id, source_root, exporter_name, exporter_version,
                    manifest_sha256, file_count, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'ingesting')
                """,
                (
                    build_id,
                    str(root),
                    exporter_name,
                    exporter_version,
                    manifest_sha256,
                    len(files),
                ),
            )
            snapshot_id = cursor.lastrowid

    if existing_snapshot_id is not None:
        return _renormalize_existing_snapshot(connection, snapshot_id)

    try:
        with connection:
            object_payloads: dict[int, dict[str, Any]] = {}
            for source in files:
                file_id = connection.execute(
                    """
                    INSERT INTO asset_files(
                        snapshot_id, relative_path, source_path, content_sha256, size_bytes
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        source.relative_path,
                        str(source.path),
                        source.content_sha256,
                        source.size_bytes,
                    ),
                ).lastrowid
                exports = _load_exports(source.path)
                fallback_package = _file_package(exports)
                for export_index, export in enumerate(exports):
                    package = canonical_package_path(export.get("Package")) or fallback_package
                    name = str(export.get("Name") or f"export_{export_index}")
                    object_type = str(export.get("Type") or "Unknown")
                    # Unreal packages may legitimately contain repeated object names
                    # (particle distributions are a common example).  Preserve every
                    # export by its immutable file position; name/package remain query
                    # attributes and reference resolution stays conservative when a
                    # textual selector matches more than one object.
                    object_key = (
                        f"{package or ''}::{name}::"
                        f"{source.relative_path}::{export_index}"
                    )
                    object_id = connection.execute(
                        """
                        INSERT INTO asset_objects(
                            snapshot_id, asset_file_id, export_index, package_path,
                            object_name, object_type, class_path, object_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            file_id,
                            export_index,
                            package,
                            name,
                            object_type,
                            _class_path(export.get("Class")),
                            object_key,
                        ),
                    ).lastrowid
                    object_payloads[object_id] = export

            for object_id, export in object_payloads.items():
                seen: set[tuple[str, str]] = set()
                for property_path, target_path in _walk_references(export):
                    key = (property_path, target_path)
                    package = canonical_package_path(target_path)
                    if key in seen or package is None:
                        continue
                    seen.add(key)
                    connection.execute(
                        """
                        INSERT INTO asset_references(
                            snapshot_id, source_object_id, property_path, target_path,
                            target_package_path, resolution_status
                        ) VALUES (?, ?, ?, ?, ?, 'unresolved')
                        """,
                        (snapshot_id, object_id, property_path, target_path, package),
                    )

            _resolve_references(connection, snapshot_id)
            _start_normalization_run(connection, snapshot_id)
            _normalize_snapshot(connection, snapshot_id, object_payloads)
            _finish_normalization_run(connection, snapshot_id, "ready")
            connection.execute(
                "UPDATE asset_snapshots SET status='ready', error_text=NULL WHERE id=?",
                (snapshot_id,),
            )
    except Exception as error:
        with connection:
            _finish_normalization_run(connection, snapshot_id, "failed", str(error))
            connection.execute(
                "UPDATE asset_snapshots SET status='failed', error_text=? WHERE id=?",
                (str(error), snapshot_id),
            )
        raise

    return _snapshot_summary(connection, snapshot_id, idempotent=False)


def _start_normalization_run(connection: sqlite3.Connection, snapshot_id: int) -> None:
    connection.execute(
        """
        INSERT INTO asset_normalization_runs(
            snapshot_id, normalizer_version, status
        ) VALUES (?, ?, 'running')
        ON CONFLICT(snapshot_id, normalizer_version) DO UPDATE SET
            status='running', error_text=NULL, started_at=CURRENT_TIMESTAMP,
            completed_at=NULL
        """,
        (snapshot_id, NORMALIZER_VERSION),
    )


def _finish_normalization_run(
    connection: sqlite3.Connection,
    snapshot_id: int,
    status: str,
    error_text: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE asset_normalization_runs
        SET status=?, error_text=?, completed_at=CURRENT_TIMESTAMP
        WHERE snapshot_id=? AND normalizer_version=?
        """,
        (status, error_text, snapshot_id, NORMALIZER_VERSION),
    )


def _snapshot_payloads(
    connection: sqlite3.Connection, snapshot_id: int
) -> dict[int, dict[str, Any]]:
    payloads: dict[int, dict[str, Any]] = {}
    files = connection.execute(
        "SELECT id, source_path FROM asset_files WHERE snapshot_id=? ORDER BY id",
        (snapshot_id,),
    ).fetchall()
    for file_row in files:
        exports = _load_exports(Path(file_row["source_path"]))
        object_rows = connection.execute(
            """
            SELECT id, export_index FROM asset_objects
            WHERE asset_file_id=? ORDER BY export_index
            """,
            (file_row["id"],),
        ).fetchall()
        for object_row in object_rows:
            index = object_row["export_index"]
            if index >= len(exports):
                raise ValueError(
                    f"export index {index} missing from {file_row['source_path']}"
                )
            payloads[object_row["id"]] = exports[index]
    return payloads


def _clear_normalized_snapshot(
    connection: sqlite3.Connection, snapshot_id: int
) -> None:
    for table in (
        "catalog_mechanics",
        "catalog_opaque_mechanics",
        "catalog_inheritance_edges",
        "catalog_hero_class_kits",
        "catalog_ability_links",
        "catalog_gameplay_tags",
        "catalog_heroes",
        "catalog_perks",
        "catalog_ability_kits",
        "catalog_abilities",
        "catalog_gameplay_effects",
        "catalog_magnitudes",
        "catalog_hero_classes",
        "catalog_data_tables",
        "catalog_curve_tables",
    ):
        connection.execute(f"DELETE FROM {table} WHERE snapshot_id=?", (snapshot_id,))


def _renormalize_existing_snapshot(
    connection: sqlite3.Connection, snapshot_id: int
) -> dict[str, Any]:
    payloads = _snapshot_payloads(connection, snapshot_id)
    with connection:
        _start_normalization_run(connection, snapshot_id)
    try:
        with connection:
            _clear_normalized_snapshot(connection, snapshot_id)
            _resolve_references(connection, snapshot_id)
            _normalize_snapshot(connection, snapshot_id, payloads)
        with connection:
            _finish_normalization_run(connection, snapshot_id, "ready")
    except Exception as error:
        with connection:
            _finish_normalization_run(connection, snapshot_id, "failed", str(error))
        raise
    return _snapshot_summary(connection, snapshot_id, idempotent=False)


def _resolve_references(connection: sqlite3.Connection, snapshot_id: int) -> None:
    objects = connection.execute(
        """
        SELECT id, package_path, object_name, export_index
        FROM asset_objects WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ).fetchall()
    by_package: dict[str, list[sqlite3.Row]] = {}
    for row in objects:
        if row["package_path"]:
            by_package.setdefault(row["package_path"], []).append(row)
    references = connection.execute(
        "SELECT id, target_path, target_package_path FROM asset_references WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchall()
    for reference in references:
        candidates = by_package.get(reference["target_package_path"], [])
        selector = _target_selector(reference["target_path"])
        selected = candidates
        if isinstance(selector, int):
            exact = [row for row in candidates if row["export_index"] == selector]
            if exact:
                selected = exact
        elif isinstance(selector, str):
            exact = [row for row in candidates if row["object_name"] == selector]
            if exact:
                selected = exact
        if len(selected) == 1:
            status, target_id = "resolved", selected[0]["id"]
        elif selected:
            status, target_id = "ambiguous", None
        else:
            status, target_id = "unresolved", None
        connection.execute(
            """
            UPDATE asset_references SET resolution_status=?, target_object_id=? WHERE id=?
            """,
            (status, target_id, reference["id"]),
        )


def _normalize_snapshot(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    _normalize_curves(connection, snapshot_id, payloads)
    _normalize_data_tables(connection, snapshot_id, payloads)
    _normalize_inheritance(connection, snapshot_id)
    _normalize_hero_classes(connection, snapshot_id, payloads)
    _normalize_effects(connection, snapshot_id, payloads)
    _normalize_ability_kits(connection, snapshot_id, payloads)
    _normalize_hero_class_kits(connection, snapshot_id)
    _normalize_heroes(connection, snapshot_id, payloads)
    _normalize_gameplay_tags(connection, snapshot_id, payloads)
    _link_modifier_curves(connection, snapshot_id)
    _link_magnitude_curves(connection, snapshot_id)


def _normalize_curves(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    for object_id, export in payloads.items():
        if export.get("Type") != "CurveTable" or not isinstance(export.get("Rows"), dict):
            continue
        package = canonical_package_path(export.get("Package"))
        if package is None:
            package = connection.execute(
                "SELECT package_path FROM asset_objects WHERE id=?", (object_id,)
            ).fetchone()["package_path"]
        table_id = connection.execute(
            """
            INSERT INTO catalog_curve_tables(
                snapshot_id, source_object_id, package_path, table_name
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, object_id, package, export.get("Name", "Unknown")),
        ).lastrowid
        for row_name, row_payload in export["Rows"].items():
            row_id = connection.execute(
                "INSERT INTO catalog_curve_rows(curve_table_id, row_name) VALUES (?, ?)",
                (table_id, row_name),
            ).lastrowid
            keys = row_payload.get("Keys", []) if isinstance(row_payload, dict) else []
            for ordinal, point in enumerate(keys):
                if not isinstance(point, dict):
                    continue
                try:
                    time_value = float(point.get("Time", 0.0))
                    output_value = float(point["Value"])
                except (KeyError, TypeError, ValueError):
                    continue
                connection.execute(
                    """
                    INSERT INTO catalog_curve_points(
                        curve_row_id, point_ordinal, time_value, output_value, interpolation
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (row_id, ordinal, time_value, output_value, point.get("InterpMode")),
                )


def _walk_data_table_handles(value: Any) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        table_path = _object_path(value.get("DataTable"))
        row_name = value.get("RowName")
        package = canonical_package_path(table_path)
        if package and isinstance(row_name, str) and row_name not in {"", "None"}:
            yield package, row_name
        for child in value.values():
            yield from _walk_data_table_handles(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_data_table_handles(child)


def _normalize_data_tables(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    requested_rows = {
        handle for payload in payloads.values() for handle in _walk_data_table_handles(payload)
    }
    for object_id, export in payloads.items():
        if export.get("Type") != "DataTable" or not isinstance(export.get("Rows"), dict):
            continue
        object_row = connection.execute(
            "SELECT package_path FROM asset_objects WHERE id=?", (object_id,)
        ).fetchone()
        package = object_row["package_path"]
        table_id = connection.execute(
            """
            INSERT INTO catalog_data_tables(
                snapshot_id, source_object_id, package_path, table_name
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, object_id, package, export.get("Name", "Unknown")),
        ).lastrowid
        for row_name, row_payload in export["Rows"].items():
            if (package, row_name) not in requested_rows:
                continue
            connection.execute(
                """
                INSERT INTO catalog_data_rows(data_table_id, row_name, row_json)
                VALUES (?, ?, ?)
                """,
                (
                    table_id,
                    row_name,
                    json.dumps(row_payload, sort_keys=True, separators=(",", ":")),
                ),
            )


def _normalize_inheritance(connection: sqlite3.Connection, snapshot_id: int) -> None:
    references = connection.execute(
        """
        SELECT id, source_object_id, property_path, target_path,
               target_object_id, resolution_status
        FROM asset_references
        WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ).fetchall()
    for reference in references:
        path = reference["property_path"].lower()
        if ".template." in path:
            relation = "template"
        elif ".super." in path:
            relation = "super"
        elif ".archetype." in path:
            relation = "archetype"
        else:
            continue
        connection.execute(
            """
            INSERT INTO catalog_inheritance_edges(
                snapshot_id, source_object_id, source_reference_id, relation,
                target_path, target_object_id, resolution_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                reference["source_object_id"],
                reference["id"],
                relation,
                reference["target_path"],
                reference["target_object_id"],
                reference["resolution_status"],
            ),
        )


def _normalize_hero_classes(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    structural_ids = {
        row["target_object_id"]
        for row in connection.execute(
            """
            SELECT target_object_id FROM asset_references
            WHERE snapshot_id=? AND target_object_id IS NOT NULL
              AND property_path LIKE '%HeroClassGameplayDefinition.%'
            """,
            (snapshot_id,),
        )
    }
    for object_id, export in payloads.items():
        if (
            export.get("Type") != "FortHeroClassGameplayDefinition"
            and object_id not in structural_ids
        ):
            continue
        row = connection.execute(
            "SELECT package_path, object_name FROM asset_objects WHERE id=?",
            (object_id,),
        ).fetchone()
        properties = export.get("Properties") or {}
        display = (
            _localized_text(properties.get("DisplayName"))
            or _localized_text(properties.get("ClassName"))
        )
        connection.execute(
            """
            INSERT INTO catalog_hero_classes(
                snapshot_id, source_object_id, class_key, display_name, package_path
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (snapshot_id, object_id, row["object_name"], display, row["package_path"]),
        )


def _normalize_effects(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    effect_property_keys = {
        "Modifiers",
        "DurationPolicy",
        "DurationMagnitude",
        "Period",
        "ChanceToApplyToTarget",
        "Executions",
        "GameplayEffectExecutionDefinitions",
        "StackingType",
        "GEComponents",
    }
    candidate_ids: set[int] = set()
    for object_id, export in payloads.items():
        properties = export.get("Properties")
        if isinstance(properties, dict) and effect_property_keys.intersection(properties):
            candidate_ids.add(object_id)
    structural_packages = {
        row["target_package_path"]
        for row in connection.execute(
            """
            SELECT target_package_path FROM asset_references
            WHERE snapshot_id=? AND lower(property_path) LIKE '%gameplayeffect%'
            """,
            (snapshot_id,),
        )
    }
    for package in structural_packages:
        semantic_id = _gameplay_effect_object_for_package(
            connection, snapshot_id, package, payloads
        )
        if semantic_id is not None:
            candidate_ids.add(semantic_id)
    for object_id in sorted(candidate_ids):
        export = payloads[object_id]
        properties = export.get("Properties")
        if not isinstance(properties, dict):
            continue
        package = connection.execute(
            "SELECT package_path FROM asset_objects WHERE id=?", (object_id,)
        ).fetchone()["package_path"]
        template_path = _object_path(export.get("Template"))
        effect_id = connection.execute(
            """
            INSERT INTO catalog_gameplay_effects(
                snapshot_id, source_object_id, package_path, effect_name,
                template_path, stacking_type, stack_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                object_id,
                package,
                export.get("Name", "Unknown"),
                template_path,
                properties.get("StackingType"),
                properties.get("StackLimitCount"),
            ),
        ).lastrowid
        for ordinal, modifier in enumerate(properties.get("Modifiers") or []):
            if not isinstance(modifier, dict):
                continue
            magnitude = modifier.get("ModifierMagnitude") or {}
            scalable = magnitude.get("ScalableFloatMagnitude") or {}
            curve = scalable.get("Curve") or {}
            curve_table_path = _object_path(curve.get("CurveTable"))
            magnitude_kind = magnitude.get("MagnitudeCalculationType")
            operation = modifier.get("ModifierOp")
            supported = bool(operation and magnitude_kind and (curve_table_path or "Value" in scalable))
            magnitude_id = _insert_magnitude(
                connection,
                snapshot_id,
                object_id,
                f"$.Properties.Modifiers[{ordinal}].ModifierMagnitude",
                "effect_modifier",
                magnitude,
            )
            connection.execute(
                """
                INSERT INTO catalog_effect_modifiers(
                    gameplay_effect_id, modifier_ordinal, attribute_name,
                    modifier_operation, magnitude_kind, literal_value,
                    curve_table_path, curve_row_name,
                    source_required_tags_json, source_ignored_tags_json,
                    target_required_tags_json, target_ignored_tags_json,
                    interpretation_status, magnitude_id, evaluation_channel
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    effect_id,
                    ordinal,
                    (modifier.get("Attribute") or {}).get("AttributeName"),
                    operation,
                    magnitude_kind,
                    scalable.get("Value"),
                    canonical_package_path(curve_table_path),
                    curve.get("RowName"),
                    _json_tags(modifier, "SourceTags", "RequireTags"),
                    _json_tags(modifier, "SourceTags", "IgnoreTags"),
                    _json_tags(modifier, "TargetTags", "RequireTags"),
                    _json_tags(modifier, "TargetTags", "IgnoreTags"),
                    "supported" if supported else "unsupported",
                    magnitude_id,
                    (modifier.get("EvaluationChannelSettings") or {}).get("Channel"),
                ),
            )
        _normalize_effect_mechanics(
            connection, snapshot_id, object_id, effect_id, properties
        )


def _json_tags(modifier: dict[str, Any], group: str, key: str) -> str:
    tags = (modifier.get(group) or {}).get(key) or []
    return json.dumps(tags if isinstance(tags, list) else [], separators=(",", ":"))


def _compact_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= 4:
        return {"truncated": True}
    if isinstance(value, list):
        return [_compact_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, child in value.items():
            if key in {"TokenStream", "QueryTokenStream"} and isinstance(child, list):
                compact[key] = {"entry_count": len(child)}
            else:
                compact[key] = _compact_value(child, depth + 1)
        return compact
    return str(value)


def _insert_magnitude(
    connection: sqlite3.Connection,
    snapshot_id: int,
    source_object_id: int,
    property_path: str,
    purpose: str,
    magnitude: Any,
) -> int | None:
    if isinstance(magnitude, (int, float)):
        calculation_type = "Literal"
        literal_value = float(magnitude)
        scalable: dict[str, Any] = {}
        attribute: dict[str, Any] = {}
        custom: dict[str, Any] = {}
        set_by_caller: dict[str, Any] = {}
        status = "supported"
    elif isinstance(magnitude, dict):
        calculation_type = magnitude.get("MagnitudeCalculationType")
        scalable = magnitude.get("ScalableFloatMagnitude") or {}
        attribute = magnitude.get("AttributeBasedMagnitude") or {}
        custom = magnitude.get("CustomMagnitude") or {}
        set_by_caller = magnitude.get("SetByCallerMagnitude") or {}
        if calculation_type is None and (
            "Value" in magnitude
            or "Curve" in magnitude
            or bool(scalable)
        ):
            calculation_type = "ScalableFloat"
            if not scalable:
                scalable = magnitude
        literal_value = scalable.get("Value")
        if "Custom" in str(calculation_type) or _object_path(custom.get("CalculationClassMagnitude")):
            status = "opaque"
        elif "ScalableFloat" in str(calculation_type) or calculation_type == "Literal":
            status = "supported"
        elif calculation_type:
            status = "partial"
        else:
            status = "opaque"
    else:
        return None

    coefficient_shape = attribute.get("Coefficient") or custom.get("Coefficient") or {}
    curve = scalable.get("Curve") or {}
    if not _object_path(curve.get("CurveTable")) and isinstance(
        coefficient_shape, dict
    ):
        coefficient_curve = coefficient_shape.get("Curve") or {}
        if _object_path(coefficient_curve.get("CurveTable")):
            curve = coefficient_curve
    curve_table_path = canonical_package_path(_object_path(curve.get("CurveTable")))
    custom_path = _object_path(custom.get("CalculationClassMagnitude"))
    caller_tag = ((set_by_caller.get("DataTag") or {}).get("TagName"))
    coefficient = coefficient_shape.get("Value")
    pre_additive = (
        attribute.get("PreMultiplyAdditiveValue")
        or custom.get("PreMultiplyAdditiveValue")
        or {}
    ).get("Value")
    post_additive = (
        attribute.get("PostMultiplyAdditiveValue")
        or custom.get("PostMultiplyAdditiveValue")
        or {}
    ).get("Value")
    cursor = connection.execute(
        """
        INSERT INTO catalog_magnitudes(
            snapshot_id, source_object_id, property_path, purpose,
            calculation_type, literal_value, coefficient, pre_additive,
            post_additive, curve_table_path, curve_row_name,
            custom_calculation_path, set_by_caller_tag,
            interpretation_status, shape_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            source_object_id,
            property_path,
            purpose,
            calculation_type,
            literal_value,
            coefficient,
            pre_additive,
            post_additive,
            curve_table_path,
            curve.get("RowName"),
            custom_path,
            caller_tag,
            status,
            json.dumps(_compact_value(magnitude), sort_keys=True, separators=(",", ":")),
        ),
    )
    magnitude_id = cursor.lastrowid
    if status == "opaque":
        connection.execute(
            """
            INSERT OR IGNORE INTO catalog_opaque_mechanics(
                snapshot_id, source_object_id, property_path, mechanic_kind,
                referenced_path, reason
            ) VALUES (?, ?, ?, 'custom_magnitude', ?, ?)
            """,
            (
                snapshot_id,
                source_object_id,
                property_path,
                custom_path,
                "custom or unsupported magnitude calculation requires explicit modeling",
            ),
        )
    return magnitude_id


def _insert_mechanic(
    connection: sqlite3.Connection,
    snapshot_id: int,
    source_object_id: int,
    owner_domain: str,
    owner_id: int | None,
    mechanic_type: str,
    property_path: str,
    *,
    magnitude_id: int | None = None,
    conditions: Any = None,
    value: Any = None,
    status: str = "supported",
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO catalog_mechanics(
            snapshot_id, source_object_id, owner_domain, owner_id,
            mechanic_type, property_path, magnitude_id, conditions_json,
            value_json, interpretation_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            source_object_id,
            owner_domain,
            owner_id,
            mechanic_type,
            property_path,
            magnitude_id,
            json.dumps(_compact_value(conditions or {}), sort_keys=True, separators=(",", ":")),
            json.dumps(_compact_value(value or {}), sort_keys=True, separators=(",", ":")),
            status,
        ),
    )


def _normalize_effect_mechanics(
    connection: sqlite3.Connection,
    snapshot_id: int,
    source_object_id: int,
    effect_id: int,
    properties: dict[str, Any],
) -> None:
    if "DurationPolicy" in properties:
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "gameplay_effect",
            effect_id,
            "duration_policy",
            "$.Properties.DurationPolicy",
            value={"policy": properties["DurationPolicy"]},
        )
    if "DurationMagnitude" in properties:
        magnitude_id = _insert_magnitude(
            connection,
            snapshot_id,
            source_object_id,
            "$.Properties.DurationMagnitude",
            "effect_duration",
            properties["DurationMagnitude"],
        )
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "gameplay_effect",
            effect_id,
            "duration",
            "$.Properties.DurationMagnitude",
            magnitude_id=magnitude_id,
            status="opaque" if magnitude_id is None else "supported",
        )
    for key, mechanic_type in (
        ("Period", "period"),
        ("ChanceToApplyToTarget", "application_chance"),
    ):
        if key not in properties:
            continue
        magnitude_id = _insert_magnitude(
            connection,
            snapshot_id,
            source_object_id,
            f"$.Properties.{key}",
            f"effect_{mechanic_type}",
            properties[key],
        )
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "gameplay_effect",
            effect_id,
            mechanic_type,
            f"$.Properties.{key}",
            magnitude_id=magnitude_id,
            value=None if magnitude_id is not None else {"raw": properties[key]},
            status="supported" if magnitude_id is not None else "partial",
        )
    if "StackingType" in properties or "StackLimitCount" in properties:
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "gameplay_effect",
            effect_id,
            "stacking",
            "$.Properties.Stacking",
            value={
                "type": properties.get("StackingType"),
                "limit": properties.get("StackLimitCount"),
            },
        )
    for key in (
        "ApplicationTagRequirements",
        "OngoingTagRequirements",
        "RemovalTagRequirements",
        "GrantedApplicationImmunityTags",
        "InheritableGameplayEffectTags",
        "InheritableOwnedTagsContainer",
    ):
        if key in properties:
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "gameplay_effect",
                effect_id,
                "tag_condition",
                f"$.Properties.{key}",
                conditions=properties[key],
                status="partial",
            )
    for key in ("Executions", "GameplayEffectExecutionDefinitions"):
        executions = properties.get(key)
        if not isinstance(executions, list):
            continue
        for ordinal, execution in enumerate(executions):
            path = f"$.Properties.{key}[{ordinal}]"
            references = list(_walk_references(execution, path))
            referenced = references[0][1] if references else None
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "gameplay_effect",
                effect_id,
                "execution",
                path,
                value={"referenced_path": referenced},
                status="opaque",
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO catalog_opaque_mechanics(
                    snapshot_id, source_object_id, property_path, mechanic_kind,
                    referenced_path, reason
                ) VALUES (?, ?, ?, 'execution_calculation', ?, ?)
                """,
                (
                    snapshot_id,
                    source_object_id,
                    path,
                    referenced,
                    "GameplayEffect execution calculations are not evaluated in Phase 2",
                ),
            )
            if not isinstance(execution, dict):
                continue
            for modifier_ordinal, modifier in enumerate(
                execution.get("CalculationModifiers") or []
            ):
                if not isinstance(modifier, dict):
                    continue
                modifier_path = f"{path}.CalculationModifiers[{modifier_ordinal}]"
                magnitude_id = _insert_magnitude(
                    connection,
                    snapshot_id,
                    source_object_id,
                    f"{modifier_path}.ModifierMagnitude",
                    "effect_execution_modifier",
                    modifier.get("ModifierMagnitude"),
                )
                captured = modifier.get("CapturedAttribute") or {}
                attribute_to_capture = captured.get("AttributeToCapture") or {}
                _insert_mechanic(
                    connection,
                    snapshot_id,
                    source_object_id,
                    "gameplay_effect",
                    effect_id,
                    "execution_modifier",
                    modifier_path,
                    magnitude_id=magnitude_id,
                    conditions={
                        "source": modifier.get("SourceTags") or {},
                        "target": modifier.get("TargetTags") or {},
                    },
                    value={
                        "attribute": attribute_to_capture.get("AttributeName"),
                        "operation": modifier.get("ModifierOp"),
                        "aggregator_type": modifier.get("AggregatorType"),
                    },
                    status="partial" if magnitude_id is not None else "opaque",
                )


def _is_ability_kit(
    export: dict[str, Any], object_id: int, structurally_referenced: set[int]
) -> bool:
    return "AbilityKit" in str(export.get("Type")) or object_id in structurally_referenced


def _normalize_ability_kits(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    structural_kit_ids = {
        row["target_object_id"]
        for row in connection.execute(
            """
            SELECT target_object_id FROM asset_references
            WHERE snapshot_id=? AND target_object_id IS NOT NULL
              AND property_path LIKE '%GrantedAbilityKit.%'
            """,
            (snapshot_id,),
        )
    }
    for object_id, export in payloads.items():
        if not _is_ability_kit(export, object_id, structural_kit_ids):
            continue
        object_row = connection.execute(
            "SELECT package_path, object_name FROM asset_objects WHERE id=?", (object_id,)
        ).fetchone()
        kit_id = connection.execute(
            """
            INSERT INTO catalog_ability_kits(
                snapshot_id, source_object_id, package_path, kit_name, display_name
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                object_id,
                object_row["package_path"],
                object_row["object_name"],
                _localized_text((export.get("Properties") or {}).get("DisplayName")),
            ),
        ).lastrowid
        references = connection.execute(
            """
            SELECT ar.*
            FROM asset_references ar
            WHERE ar.source_object_id=?
            """,
            (object_id,),
        ).fetchall()
        for reference in references:
            path_lower = reference["property_path"].lower()
            structural_effect = "gameplayeffect" in path_lower
            structural_ability = (
                "grantedabilities" in path_lower
                or "grantedgameplayabilities" in path_lower
                or "gameplayabilities" in path_lower
                or "gadgets" in path_lower
            )
            semantic_object_id = None
            if structural_effect:
                semantic_object_id = _gameplay_effect_object_for_package(
                    connection,
                    snapshot_id,
                    reference["target_package_path"],
                    payloads,
                )
            elif structural_ability:
                semantic_object_id = _semantic_object_for_package(
                    connection,
                    snapshot_id,
                    reference["target_package_path"],
                    payloads,
                )
            effect_row = connection.execute(
                "SELECT id FROM catalog_gameplay_effects WHERE source_object_id=?",
                (semantic_object_id or reference["target_object_id"],),
            ).fetchone()
            gameplay_effect_id = effect_row["id"] if effect_row else None
            ability_row = connection.execute(
                "SELECT id FROM catalog_abilities WHERE source_object_id=?",
                (semantic_object_id or reference["target_object_id"],),
            ).fetchone()
            ability_id = ability_row["id"] if ability_row else None
            if structural_ability and reference["target_object_id"] is not None:
                ability_id = _ensure_ability(
                    connection,
                    snapshot_id,
                    semantic_object_id or reference["target_object_id"],
                    payloads.get(
                        semantic_object_id or reference["target_object_id"], {}
                    ),
                )
            if gameplay_effect_id is not None or structural_effect:
                kind = "gameplay_effect"
            elif ability_id is not None or structural_ability:
                kind = "ability"
            else:
                kind = "reference"
            connection.execute(
                """
                INSERT INTO catalog_ability_kit_grants(
                    ability_kit_id, source_reference_id, grant_kind,
                    target_path, gameplay_effect_id, ability_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kit_id,
                    reference["id"],
                    kind,
                    reference["target_path"],
                    gameplay_effect_id,
                    ability_id,
                ),
            )
    _normalize_linked_abilities(connection, snapshot_id, payloads)


def _normalize_linked_abilities(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    source_abilities = connection.execute(
        """
        SELECT id, source_object_id FROM catalog_abilities
        WHERE snapshot_id=? ORDER BY id
        """,
        (snapshot_id,),
    ).fetchall()
    for source in source_abilities:
        references = connection.execute(
            """
            SELECT * FROM asset_references
            WHERE source_object_id=?
              AND lower(property_path) LIKE '%gameplayability%'
            ORDER BY id
            """,
            (source["source_object_id"],),
        ).fetchall()
        for reference in references:
            target_ability_id = None
            resolution_status = reference["resolution_status"]
            if reference["target_object_id"] is not None:
                semantic_object_id = _semantic_object_for_package(
                    connection,
                    snapshot_id,
                    reference["target_package_path"],
                    payloads,
                )
                target_object_id = semantic_object_id or reference["target_object_id"]
                target_ability_id = _ensure_ability(
                    connection,
                    snapshot_id,
                    target_object_id,
                    payloads.get(target_object_id, {}),
                )
                resolution_status = "resolved"
            connection.execute(
                """
                INSERT INTO catalog_ability_links(
                    snapshot_id, source_ability_id, source_reference_id, relation,
                    target_path, target_ability_id, resolution_status
                ) VALUES (?, ?, ?, 'gameplay_ability', ?, ?, ?)
                """,
                (
                    snapshot_id,
                    source["id"],
                    reference["id"],
                    reference["target_path"],
                    target_ability_id,
                    resolution_status,
                ),
            )


def _normalize_hero_class_kits(
    connection: sqlite3.Connection, snapshot_id: int
) -> None:
    rows = connection.execute(
        """
        SELECT hero_class.id AS hero_class_id, reference.id AS reference_id,
               reference.property_path, reference.target_path,
               kit.id AS ability_kit_id
        FROM catalog_hero_classes hero_class
        JOIN asset_references reference
          ON reference.source_object_id=hero_class.source_object_id
        LEFT JOIN catalog_ability_kits kit
          ON kit.snapshot_id=hero_class.snapshot_id
         AND kit.package_path=reference.target_package_path
        WHERE hero_class.snapshot_id=?
          AND lower(reference.property_path) LIKE '%classabilitykits%'
        ORDER BY hero_class.id, reference.property_path
        """,
        (snapshot_id,),
    ).fetchall()
    ordinals: dict[int, int] = {}
    for row in rows:
        match = re.search(r"ClassAbilityKits\[(\d+)\]", row["property_path"], re.I)
        ordinal = int(match.group(1)) if match else ordinals.get(row["hero_class_id"], 0)
        ordinals[row["hero_class_id"]] = ordinal + 1
        connection.execute(
            """
            INSERT INTO catalog_hero_class_kits(
                snapshot_id, hero_class_id, kit_ordinal, source_reference_id,
                ability_kit_path, ability_kit_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                row["hero_class_id"],
                ordinal,
                row["reference_id"],
                row["target_path"],
                row["ability_kit_id"],
            ),
        )


def _semantic_object_for_package(
    connection: sqlite3.Connection,
    snapshot_id: int,
    package_path: str,
    payloads: dict[int, dict[str, Any]],
) -> int | None:
    candidates = connection.execute(
        """
        SELECT id, object_type FROM asset_objects
        WHERE snapshot_id=? AND package_path=? ORDER BY export_index
        """,
        (snapshot_id, package_path),
    ).fetchall()
    scored: list[tuple[int, int]] = []
    for row in candidates:
        payload = payloads.get(row["id"], {})
        score = 0
        properties = payload.get("Properties")
        if isinstance(properties, dict):
            score += 2
            if properties:
                score += 2
        if row["object_type"] != "BlueprintGeneratedClass":
            score += 1
        scored.append((score, row["id"]))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


def _gameplay_effect_object_for_package(
    connection: sqlite3.Connection,
    snapshot_id: int,
    package_path: str,
    payloads: dict[int, dict[str, Any]],
) -> int | None:
    candidates = connection.execute(
        """
        SELECT id, object_type, object_name FROM asset_objects
        WHERE snapshot_id=? AND package_path=? ORDER BY export_index
        """,
        (snapshot_id, package_path),
    ).fetchall()
    scored: list[tuple[int, int]] = []
    effect_keys = {
        "DurationPolicy",
        "DurationMagnitude",
        "Period",
        "ChanceToApplyToTarget",
        "Executions",
        "GameplayEffectExecutionDefinitions",
        "StackingType",
        "StackLimitCount",
    }
    for row in candidates:
        payload = payloads.get(row["id"], {})
        properties = payload.get("Properties")
        score = 0
        if isinstance(properties, dict):
            if "Modifiers" in properties:
                score += 100
            if "GEComponents" in properties:
                score += 50
            score += 10 * len(effect_keys.intersection(properties))
        if str(row["object_name"]).startswith("Default__"):
            score += 5
        if row["object_type"] != "BlueprintGeneratedClass":
            score += 1
        scored.append((score, row["id"]))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


def _ensure_ability(
    connection: sqlite3.Connection,
    snapshot_id: int,
    source_object_id: int,
    export: dict[str, Any],
) -> int:
    existing = connection.execute(
        "SELECT id FROM catalog_abilities WHERE source_object_id=?", (source_object_id,)
    ).fetchone()
    if existing is not None:
        return existing["id"]
    row = connection.execute(
        "SELECT package_path, object_name FROM asset_objects WHERE id=?",
        (source_object_id,),
    ).fetchone()
    properties = export.get("Properties") or {}
    display = (
        _localized_text(properties.get("DisplayName"))
        or _localized_text(properties.get("AbilityName"))
    )
    ability_id = connection.execute(
        """
        INSERT INTO catalog_abilities(
            snapshot_id, source_object_id, ability_key, display_name,
            package_path, semantic_status
        ) VALUES (?, ?, ?, ?, ?, 'partial')
        """,
        (snapshot_id, source_object_id, row["object_name"], display, row["package_path"]),
    ).lastrowid
    _normalize_ability_mechanics(
        connection, snapshot_id, source_object_id, ability_id, properties
    )
    return ability_id


def _normalize_ability_mechanics(
    connection: sqlite3.Connection,
    snapshot_id: int,
    source_object_id: int,
    ability_id: int,
    properties: dict[str, Any],
) -> None:
    recognized = False
    recognized_keys: set[str] = set()
    for key, mechanic_type in (
        ("CooldownDuration", "cooldown"),
        ("AbilityCooldown", "cooldown"),
        ("ChargeTime", "charge_time"),
        ("AbilityDuration", "duration"),
    ):
        if key not in properties:
            continue
        recognized = True
        recognized_keys.add(key)
        magnitude_id = _insert_magnitude(
            connection,
            snapshot_id,
            source_object_id,
            f"$.Properties.{key}",
            f"ability_{mechanic_type}",
            properties[key],
        )
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "ability",
            ability_id,
            mechanic_type,
            f"$.Properties.{key}",
            magnitude_id=magnitude_id,
            status="supported" if magnitude_id is not None else "partial",
        )
    costs = properties.get("Costs")
    if costs is not None:
        recognized = True
        recognized_keys.add("Costs")
        cost_items = costs if isinstance(costs, list) else [costs]
        for ordinal, cost in enumerate(cost_items):
            if not isinstance(cost, dict):
                continue
            property_path = f"$.Properties.Costs[{ordinal}]"
            magnitude_id = _insert_magnitude(
                connection,
                snapshot_id,
                source_object_id,
                f"{property_path}.CostValue",
                "ability_cost",
                cost.get("CostValue"),
            )
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                "cost",
                property_path,
                magnitude_id=magnitude_id,
                value=cost,
                status="supported" if magnitude_id is not None else "partial",
            )
    for key, mechanic_type in (
        ("CooldownGameplayEffectClass", "cooldown_effect"),
        ("CostGameplayEffectClass", "cost_effect"),
        ("AbilityTriggers", "trigger"),
        ("ActivationRequiredTags", "activation_condition"),
        ("ActivationBlockedTags", "activation_condition"),
        ("AbilityTags", "ability_tags"),
        ("ActivationOwnedTags", "owned_tags"),
        ("DamageStatHandle", "damage_stat_row"),
        ("EffectContainers", "effect_container"),
    ):
        if key not in properties:
            continue
        recognized = True
        recognized_keys.add(key)
        _insert_mechanic(
            connection,
            snapshot_id,
            source_object_id,
            "ability",
            ability_id,
            mechanic_type,
            f"$.Properties.{key}",
            conditions=properties[key] if "Tags" in key else None,
            value=properties[key] if "Tags" not in key else None,
            status="partial",
        )

    # Hero perks commonly grant a GameplayAbility whose Blueprint defaults hold
    # the actual balance inputs.  Preserve those explicit inputs structurally;
    # do not pretend to execute the Blueprint graph or infer how its variables
    # interact.
    for key, value in properties.items():
        if key in recognized_keys:
            continue
        property_path = f"$.Properties.{key}"
        magnitude_like = isinstance(value, dict) and (
            "MagnitudeCalculationType" in value
            or "ScalableFloatMagnitude" in value
            or ("Value" in value and "Curve" in value)
        )
        if magnitude_like:
            recognized = True
            magnitude_id = _insert_magnitude(
                connection,
                snapshot_id,
                source_object_id,
                property_path,
                "ability_parameter",
                value,
            )
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                "parameter",
                property_path,
                magnitude_id=magnitude_id,
                value={"name": key},
                status="supported" if magnitude_id is not None else "partial",
            )
            continue

        direct_path = _object_path(value)
        if direct_path:
            recognized = True
            target_package = canonical_package_path(direct_path)
            target_name = (target_package or "").rsplit("/", 1)[-1]
            mechanic_type = (
                "referenced_effect"
                if target_name.startswith(("GE_", "GET_"))
                else "referenced_asset"
            )
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                mechanic_type,
                property_path,
                value={"name": key, "target_path": direct_path},
                status="partial",
            )
            continue

        nested_references = list(_walk_references(value, property_path))
        effect_references = [
            target
            for _, target in nested_references
            if (canonical_package_path(target) or "").rsplit("/", 1)[-1].startswith(
                ("GE_", "GET_")
            )
        ]
        if effect_references:
            recognized = True
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                "effect_map",
                property_path,
                value={"name": key, "target_paths": effect_references},
                status="partial",
            )
        elif key.startswith("TC_"):
            recognized = True
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                "tag_condition",
                property_path,
                conditions=value,
                status="partial",
            )
        elif key.startswith("Att_"):
            recognized = True
            _insert_mechanic(
                connection,
                snapshot_id,
                source_object_id,
                "ability",
                ability_id,
                "attribute_reference",
                property_path,
                value=value,
                status="partial",
            )
    connection.execute(
        "UPDATE catalog_abilities SET semantic_status=? WHERE id=?",
        ("partial" if recognized else "opaque", ability_id),
    )
    if not recognized:
        connection.execute(
            """
            INSERT OR IGNORE INTO catalog_opaque_mechanics(
                snapshot_id, source_object_id, property_path, mechanic_kind,
                reason
            ) VALUES (?, ?, '$.Properties', 'ability_behavior', ?)
            """,
            (
                snapshot_id,
                source_object_id,
                "ability behavior has no currently supported structural mechanics",
            ),
        )


def _normalize_heroes(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    heroes_by_package: dict[str, int] = {}
    for object_id, export in payloads.items():
        if export.get("Type") != "FortHeroGameplayDefinition":
            continue
        properties = export.get("Properties") or {}
        package = connection.execute(
            "SELECT package_path FROM asset_objects WHERE id=?", (object_id,)
        ).fetchone()["package_path"]
        class_reference = properties.get("HeroClassGameplayDefinition") or {}
        class_path = _object_path(class_reference)
        class_package = canonical_package_path(class_path)
        class_object = class_reference.get("ObjectName", "")
        hero_class = re.sub(r".*HCGD_", "", class_object).split("'")[0] or None
        class_row = connection.execute(
            """
            SELECT id FROM catalog_hero_classes
            WHERE snapshot_id=? AND package_path=?
            """,
            (snapshot_id, class_package),
        ).fetchone()
        hero_id = connection.execute(
            """
            INSERT INTO catalog_heroes(
                snapshot_id, source_object_id, hero_key, display_name,
                hero_class, statline_tags_json, hero_class_path, hero_class_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                object_id,
                export.get("Name", package.rsplit("/", 1)[-1]),
                export.get("Name", "Unknown hero"),
                hero_class,
                json.dumps(properties.get("HeroBaseStatlineTags") or [], separators=(",", ":")),
                class_path,
                class_row["id"] if class_row else None,
            ),
        ).lastrowid
        heroes_by_package[package] = hero_id
        for ordinal, ability in enumerate(properties.get("TierAbilityKits") or []):
            granted = ability.get("GrantedAbilityKit") if isinstance(ability, dict) else None
            path = _object_path(granted)
            if not path:
                continue
            kit_id = _kit_id_for_path(connection, snapshot_id, path)
            connection.execute(
                """
                INSERT INTO catalog_hero_abilities(
                    hero_id, ability_ordinal, ability_kit_path, ability_kit_id, minimum_rarity
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (hero_id, ordinal, path, kit_id, ability.get("MinimumHeroRarity")),
            )
        for mode, property_name in (("support", "HeroPerk"), ("commander", "CommanderPerk")):
            path = _object_path((properties.get(property_name) or {}).get("GrantedAbilityKit"))
            if not path:
                continue
            kit_package = canonical_package_path(path)
            kit_name = kit_package.rsplit("/", 1)[-1] if kit_package else path
            parsed = PERK_KIT_RE.match(kit_name)
            if parsed is not None:
                family, tier = parsed.group("family"), parsed.group("tier")
                identity_status = "structured_identifier"
            else:
                family, tier = kit_name, "unknown"
                identity_status = "explicit_unparsed"
            kit_id = _kit_id_for_path(connection, snapshot_id, path)
            reference = connection.execute(
                """
                SELECT id FROM asset_references
                WHERE source_object_id=? AND target_path=?
                ORDER BY id LIMIT 1
                """,
                (object_id, path),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO catalog_perks(
                    snapshot_id, perk_family, perk_tier, ability_kit_path, ability_kit_id,
                    perk_key, identity_status, source_reference_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, perk_family, perk_tier) DO UPDATE SET
                    ability_kit_path=excluded.ability_kit_path,
                    ability_kit_id=excluded.ability_kit_id,
                    perk_key=excluded.perk_key,
                    identity_status=excluded.identity_status,
                    source_reference_id=excluded.source_reference_id
                """,
                (
                    snapshot_id,
                    family,
                    tier,
                    path,
                    kit_id,
                    kit_package,
                    identity_status,
                    reference["id"] if reference else None,
                ),
            )
            perk_id = connection.execute(
                """
                SELECT id FROM catalog_perks
                WHERE snapshot_id=? AND perk_family=? AND perk_tier=?
                """,
                (snapshot_id, family, tier),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO catalog_hero_perks(hero_id, perk_id, perk_mode) VALUES (?, ?, ?)",
                (hero_id, perk_id, mode),
            )

    for object_id, export in payloads.items():
        if export.get("Type") != "FortHeroType":
            continue
        properties = export.get("Properties") or {}
        hgd_path = _object_path(properties.get("HeroGameplayDefinition"))
        hero_id = heroes_by_package.get(canonical_package_path(hgd_path))
        if hero_id is None:
            continue
        display = _localized_text(properties.get("ItemName")) or export.get("Name", "Unknown hero")
        rarity = tier = None
        for item in properties.get("DataList") or []:
            if not isinstance(item, dict):
                continue
            rarity = item.get("Rarity", rarity)
            tier = item.get("Tier", tier)
        init = properties.get("AttributeInitKey") or {}
        connection.execute(
            """
            INSERT INTO catalog_hero_variants(
                hero_id, source_object_id, variant_key, display_name,
                rarity, tier, attribute_init_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hero_id,
                object_id,
                export.get("Name", "Unknown"),
                display,
                rarity,
                tier,
                init.get("AttributeInitSubCategory"),
            ),
        )
        connection.execute(
            "UPDATE catalog_heroes SET display_name=? WHERE id=?", (display, hero_id)
        )


def _localized_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("LocalizedString") or value.get("SourceString")


def _kit_id_for_path(
    connection: sqlite3.Connection, snapshot_id: int, path: str
) -> int | None:
    package = canonical_package_path(path)
    row = connection.execute(
        """
        SELECT id FROM catalog_ability_kits
        WHERE snapshot_id=? AND package_path=?
        """,
        (snapshot_id, package),
    ).fetchone()
    return row["id"] if row else None


def _link_modifier_curves(connection: sqlite3.Connection, snapshot_id: int) -> None:
    connection.execute(
        """
        UPDATE catalog_effect_modifiers
        SET curve_row_id=(
            SELECT cr.id
            FROM catalog_curve_rows cr
            JOIN catalog_curve_tables ct ON ct.id=cr.curve_table_id
            WHERE ct.snapshot_id=?
              AND ct.package_path=catalog_effect_modifiers.curve_table_path
              AND cr.row_name=catalog_effect_modifiers.curve_row_name
        )
        WHERE gameplay_effect_id IN (
            SELECT id FROM catalog_gameplay_effects WHERE snapshot_id=?
        )
        """,
        (snapshot_id, snapshot_id),
    )


def _link_magnitude_curves(connection: sqlite3.Connection, snapshot_id: int) -> None:
    connection.execute(
        """
        UPDATE catalog_magnitudes
        SET curve_row_id=(
            SELECT cr.id
            FROM catalog_curve_rows cr
            JOIN catalog_curve_tables ct ON ct.id=cr.curve_table_id
            WHERE ct.snapshot_id=?
              AND ct.package_path=catalog_magnitudes.curve_table_path
              AND cr.row_name=catalog_magnitudes.curve_row_name
        )
        WHERE snapshot_id=?
        """,
        (snapshot_id, snapshot_id),
    )


def _looks_like_gameplay_tag(value: str) -> bool:
    return (
        "." in value
        and not value.startswith("/")
        and "::" not in value
        and " " not in value
        and value not in {"None", "Invalid"}
    )


def _tag_values(value: Any, property_path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str) and _looks_like_gameplay_tag(value):
        yield property_path, value
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _tag_values(child, f"{property_path}[{index}]")
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _tag_values(child, f"{property_path}.{key}")


def _walk_gameplay_tags(
    value: Any, property_path: str = "$"
) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{property_path}.{key}"
            key_lower = key.lower()
            if key == "TagName" or "tags" in key_lower or key_lower.endswith("tag"):
                yield from _tag_values(child, child_path)
            yield from _walk_gameplay_tags(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_gameplay_tags(child, f"{property_path}[{index}]")


def _tag_role(property_path: str) -> str:
    path = property_path.lower()
    if "sourcetags" in path and "require" in path:
        return "source_required"
    if "sourcetags" in path and "ignore" in path:
        return "source_ignored"
    if "targettags" in path and "require" in path:
        return "target_required"
    if "targettags" in path and "ignore" in path:
        return "target_ignored"
    if "blocked" in path or "ignore" in path:
        return "blocked"
    if "required" in path or "require" in path:
        return "required"
    if "granted" in path or "owned" in path:
        return "granted"
    if "activation" in path:
        return "activation"
    return "declared"


def _normalize_gameplay_tags(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    for object_id, export in payloads.items():
        seen: set[tuple[str, str, str]] = set()
        for property_path, tag_name in _walk_gameplay_tags(export):
            role = _tag_role(property_path)
            occurrence = (property_path, tag_name, role)
            if occurrence in seen:
                continue
            seen.add(occurrence)
            connection.execute(
                """
                INSERT INTO catalog_gameplay_tags(snapshot_id, tag_name)
                VALUES (?, ?) ON CONFLICT(snapshot_id, tag_name) DO NOTHING
                """,
                (snapshot_id, tag_name),
            )
            tag_id = connection.execute(
                """
                SELECT id FROM catalog_gameplay_tags
                WHERE snapshot_id=? AND tag_name=?
                """,
                (snapshot_id, tag_name),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT OR IGNORE INTO catalog_gameplay_tag_occurrences(
                    tag_id, source_object_id, property_path, semantic_role
                ) VALUES (?, ?, ?, ?)
                """,
                (tag_id, object_id, property_path, role),
            )


def latest_asset_snapshot_id(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT id FROM asset_snapshots WHERE status='ready' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def hero_provenance(
    connection: sqlite3.Connection,
    hero_name: str,
    snapshot_id: int | None = None,
) -> dict[str, Any] | None:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return None
    hero = connection.execute(
        """
        SELECT h.*, ao.package_path, af.source_path, af.content_sha256,
               gb.build_key, gb.game_version, s.manifest_sha256
        FROM catalog_heroes h
        JOIN asset_objects ao ON ao.id=h.source_object_id
        JOIN asset_files af ON af.id=ao.asset_file_id
        JOIN asset_snapshots s ON s.id=h.snapshot_id
        JOIN game_builds gb ON gb.id=s.game_build_id
        WHERE h.snapshot_id=?
          AND (lower(h.display_name)=lower(?) OR lower(h.hero_key)=lower(?))
        """,
        (snapshot_id, hero_name, hero_name),
    ).fetchone()
    if hero is None:
        return None
    ability_rows = connection.execute(
        """
        SELECT ha.ability_ordinal, ha.ability_kit_path, ha.minimum_rarity,
               ha.ability_kit_id, ak.display_name
        FROM catalog_hero_abilities ha
        LEFT JOIN catalog_ability_kits ak ON ak.id=ha.ability_kit_id
        WHERE ha.hero_id=? ORDER BY ha.ability_ordinal
        """,
        (hero["id"],),
    ).fetchall()
    abilities = []
    for row in ability_rows:
        evidence = _ability_kit_semantic_status(connection, row["ability_kit_id"])
        abilities.append(
            {
                "ability_ordinal": row["ability_ordinal"],
                "ability_kit_path": row["ability_kit_path"],
                "minimum_rarity": row["minimum_rarity"],
                "display_name": row["display_name"],
                **evidence,
            }
        )
    variants = connection.execute(
        """
        SELECT hv.variant_key, hv.display_name, hv.rarity, hv.tier,
               hv.attribute_init_key, ao.package_path, af.source_path,
               af.content_sha256
        FROM catalog_hero_variants hv
        JOIN asset_objects ao ON ao.id=hv.source_object_id
        JOIN asset_files af ON af.id=ao.asset_file_id
        WHERE hv.hero_id=? ORDER BY hv.id
        """,
        (hero["id"],),
    ).fetchall()
    perks = connection.execute(
        """
        SELECT hp.perk_mode, p.id AS perk_id, p.perk_family, p.perk_tier,
               p.ability_kit_path, p.ability_kit_id, p.perk_key, p.identity_status
        FROM catalog_hero_perks hp
        JOIN catalog_perks p ON p.id=hp.perk_id
        WHERE hp.hero_id=? ORDER BY hp.perk_mode DESC
        """,
        (hero["id"],),
    ).fetchall()
    class_kits = connection.execute(
        """
        SELECT class_kit.kit_ordinal, class_kit.ability_kit_path,
               class_kit.ability_kit_id, kit.kit_name, kit.display_name
        FROM catalog_hero_class_kits class_kit
        LEFT JOIN catalog_ability_kits kit ON kit.id=class_kit.ability_kit_id
        WHERE class_kit.hero_class_id=? ORDER BY class_kit.kit_ordinal
        """,
        (hero["hero_class_id"],),
    ).fetchall() if hero["hero_class_id"] else []
    return {
        "snapshot_id": snapshot_id,
        "build": {
            "build_key": hero["build_key"],
            "game_version": hero["game_version"],
            "manifest_sha256": hero["manifest_sha256"],
        },
        "hero": {
            "name": hero["display_name"],
            "key": hero["hero_key"],
            "class": hero["hero_class"],
            "class_path": hero["hero_class_path"],
            "class_status": "resolved" if hero["hero_class_id"] else "unresolved",
            "class_kits": [
                {
                    "ordinal": row["kit_ordinal"],
                    "ability_kit_path": row["ability_kit_path"],
                    "kit_name": row["kit_name"],
                    "display_name": row["display_name"],
                    **_ability_kit_semantic_status(
                        connection, row["ability_kit_id"]
                    ),
                }
                for row in class_kits
            ],
            "statline_tags": json.loads(hero["statline_tags_json"]),
            "source": _source_evidence(hero),
        },
        "variants": [
            {
                "key": row["variant_key"],
                "name": row["display_name"],
                "rarity": row["rarity"],
                "tier": row["tier"],
                "attribute_init_key": row["attribute_init_key"],
                "source": _source_evidence(row),
            }
            for row in variants
        ],
        "abilities": abilities,
        "perks": [_perk_provenance(connection, snapshot_id, row) for row in perks],
        "unresolved": unresolved_reference_report(
            connection, snapshot_id=snapshot_id, source_prefix=hero["package_path"].rsplit("/", 2)[0]
        ),
    }


def _ability_kit_semantic_status(
    connection: sqlite3.Connection, ability_kit_id: int | None
) -> dict[str, Any]:
    if ability_kit_id is None:
        return {"status": "unresolved", "unresolved_grants": []}
    grants = connection.execute(
        """
        SELECT grant_row.grant_kind, grant_row.target_path,
               grant_row.gameplay_effect_id, grant_row.ability_id,
               reference.resolution_status,
               ability.source_object_id AS ability_source_object_id
        FROM catalog_ability_kit_grants grant_row
        JOIN asset_references reference ON reference.id=grant_row.source_reference_id
        LEFT JOIN catalog_abilities ability ON ability.id=grant_row.ability_id
        WHERE grant_row.ability_kit_id=?
          AND grant_row.grant_kind IN ('ability', 'gameplay_effect')
        ORDER BY grant_row.target_path
        """,
        (ability_kit_id,),
    ).fetchall()
    unresolved = [
        row["target_path"]
        for row in grants
        if row["resolution_status"] != "resolved"
        or (
            row["grant_kind"] == "ability" and row["ability_id"] is None
        )
        or (
            row["grant_kind"] == "gameplay_effect"
            and row["gameplay_effect_id"] is None
        )
    ]
    implementations: list[dict[str, Any]] = []
    for row in grants:
        if row["grant_kind"] != "ability" or row["ability_source_object_id"] is None:
            continue
        links = connection.execute(
            """
            SELECT link.target_path, link.resolution_status,
                   target.id AS target_ability_id,
                   target.display_name, target.ability_key,
                   target.package_path, target.semantic_status,
                   target.source_object_id
            FROM catalog_ability_links link
            LEFT JOIN catalog_abilities target ON target.id=link.target_ability_id
            WHERE link.source_ability_id=? ORDER BY link.target_path
            """,
            (row["ability_id"],),
        ).fetchall()
        unresolved.extend(
            linked["target_path"]
            for linked in links
            if linked["resolution_status"] != "resolved"
        )
        for linked in links:
            dependencies: list[str] = []
            if linked["source_object_id"] is not None:
                references = connection.execute(
                    """
                    SELECT reference.property_path, reference.target_path,
                           reference.target_package_path,
                           reference.resolution_status,
                           object.object_type AS source_type
                    FROM asset_references reference
                    JOIN asset_objects object ON object.id=reference.source_object_id
                    WHERE reference.source_object_id=?
                      AND reference.resolution_status <> 'resolved'
                    ORDER BY reference.target_path
                    """,
                    (linked["source_object_id"],),
                ).fetchall()
                for reference in references:
                    priority, _, _ = _queue_classification(
                        reference["source_type"],
                        reference["property_path"],
                        reference["target_package_path"],
                    )
                    if priority <= 1:
                        dependencies.append(reference["target_path"])
            unresolved.extend(dependencies)
            implementations.append(
                {
                    "target_path": linked["target_path"],
                    "status": linked["resolution_status"],
                    "ability_key": linked["ability_key"],
                    "display_name": linked["display_name"],
                    "package_path": linked["package_path"],
                    "semantic_status": linked["semantic_status"],
                    "unresolved_dependencies": sorted(set(dependencies)),
                    "mechanics": (
                        _ability_mechanics(connection, linked["target_ability_id"])
                        if linked["target_ability_id"] is not None
                        else []
                    ),
                }
            )
    unresolved = sorted(set(unresolved))
    if not grants:
        status = "partial_no_semantic_grants"
    elif unresolved:
        status = "partial_missing_grants"
    else:
        status = "resolved"
    return {
        "status": status,
        "unresolved_grants": unresolved,
        "implementations": implementations,
    }


def _ability_mechanics(
    connection: sqlite3.Connection, ability_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT mechanic.mechanic_type, mechanic.property_path,
               mechanic.conditions_json, mechanic.value_json,
               mechanic.interpretation_status,
               magnitude.calculation_type, magnitude.literal_value,
               magnitude.curve_table_path, magnitude.curve_row_name,
               magnitude.interpretation_status AS magnitude_status,
               (SELECT point.output_value
                FROM catalog_curve_points point
                WHERE point.curve_row_id=magnitude.curve_row_id
                ORDER BY point.point_ordinal LIMIT 1) AS curve_output_value
        FROM catalog_mechanics mechanic
        LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
        WHERE owner_domain='ability' AND owner_id=? ORDER BY mechanic.id
        """,
        (ability_id,),
    ).fetchall()
    return [
        {
            "type": row["mechanic_type"],
            "property_path": row["property_path"],
            "conditions": json.loads(row["conditions_json"]),
            "value": json.loads(row["value_json"]),
            "status": row["interpretation_status"],
            "magnitude": (
                {
                    "calculation_type": row["calculation_type"],
                    "literal_value": row["literal_value"],
                    "curve_table_path": row["curve_table_path"],
                    "curve_row_name": row["curve_row_name"],
                    "curve_output_value": row["curve_output_value"],
                    "status": row["magnitude_status"],
                }
                if row["calculation_type"] is not None
                else None
            ),
        }
        for row in rows
    ]


def _ability_effect_references(
    connection: sqlite3.Connection, source_object_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT reference.target_path, reference.resolution_status,
               effect.id AS gameplay_effect_id, effect.effect_name,
               effect.template_path, effect.source_object_id,
               file.source_path, file.content_sha256
        FROM asset_references reference
        LEFT JOIN catalog_gameplay_effects effect
          ON effect.snapshot_id=reference.snapshot_id
         AND effect.package_path=reference.target_package_path
        LEFT JOIN asset_objects object ON object.id=effect.source_object_id
        LEFT JOIN asset_files file ON file.id=object.asset_file_id
        WHERE reference.source_object_id=?
        ORDER BY reference.target_path
        """,
        (source_object_id,),
    ).fetchall()
    rows = [
        row
        for row in rows
        if (canonical_package_path(row["target_path"]) or "")
        .rsplit("/", 1)[-1]
        .startswith(("GE_", "GET_"))
    ]
    result: list[dict[str, Any]] = []
    for row in rows:
        modifiers: list[dict[str, Any]] = []
        if row["gameplay_effect_id"] is not None:
            modifier_rows = connection.execute(
                """
                SELECT modifier.attribute_name, modifier.modifier_operation,
                       modifier.curve_row_name, modifier.interpretation_status,
                       magnitude.calculation_type, magnitude.literal_value,
                       magnitude.custom_calculation_path,
                       magnitude.set_by_caller_tag,
                       magnitude.interpretation_status AS magnitude_status,
                       (SELECT point.output_value
                        FROM catalog_curve_points point
                        WHERE point.curve_row_id=magnitude.curve_row_id
                        ORDER BY point.point_ordinal LIMIT 1) AS curve_output_value
                FROM catalog_effect_modifiers modifier
                LEFT JOIN catalog_magnitudes magnitude
                  ON magnitude.id=modifier.magnitude_id
                WHERE modifier.gameplay_effect_id=?
                ORDER BY modifier.modifier_ordinal
                """,
                (row["gameplay_effect_id"],),
            ).fetchall()
            modifiers = [dict(modifier) for modifier in modifier_rows]
        result.append(
            {
                "target_path": row["target_path"],
                "resolution_status": row["resolution_status"],
                "effect": row["effect_name"],
                "template_path": row["template_path"],
                "modifiers": modifiers,
                "mechanics": (
                    _effect_mechanics(connection, row["gameplay_effect_id"])
                    if row["gameplay_effect_id"] is not None
                    else []
                ),
                "source": (
                    {
                        "source_path": row["source_path"],
                        "source_sha256": row["content_sha256"],
                    }
                    if row["source_path"] is not None
                    else None
                ),
            }
        )
    return result


def _perk_ability_implementations(
    connection: sqlite3.Connection,
    snapshot_id: int,
    perk_family: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT perk.perk_tier, grant_row.target_path,
               reference.resolution_status, ability.id AS ability_id,
               ability.ability_key, ability.package_path,
               ability.semantic_status, ability.source_object_id,
               file.source_path, file.content_sha256
        FROM catalog_perks perk
        JOIN catalog_ability_kit_grants grant_row
          ON grant_row.ability_kit_id=perk.ability_kit_id
        JOIN asset_references reference ON reference.id=grant_row.source_reference_id
        LEFT JOIN catalog_abilities ability ON ability.id=grant_row.ability_id
        LEFT JOIN asset_objects object ON object.id=ability.source_object_id
        LEFT JOIN asset_files file ON file.id=object.asset_file_id
        WHERE perk.snapshot_id=? AND perk.perk_family=?
          AND grant_row.grant_kind='ability'
        ORDER BY perk.perk_tier, grant_row.target_path
        """,
        (snapshot_id, perk_family),
    ).fetchall()
    return [
        {
            "granting_tier": row["perk_tier"],
            "target_path": row["target_path"],
            "resolution_status": row["resolution_status"],
            "ability": row["ability_key"],
            "package_path": row["package_path"],
            "semantic_status": row["semantic_status"],
            "mechanics": (
                _ability_mechanics(connection, row["ability_id"])
                if row["ability_id"] is not None
                else []
            ),
            "referenced_effects": (
                _ability_effect_references(connection, row["source_object_id"])
                if row["source_object_id"] is not None
                else []
            ),
            "source": (
                {
                    "source_path": row["source_path"],
                    "source_sha256": row["content_sha256"],
                }
                if row["source_path"] is not None
                else None
            ),
        }
        for row in rows
    ]


def _perk_provenance(
    connection: sqlite3.Connection, snapshot_id: int, perk: sqlite3.Row
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": perk["perk_mode"],
        "family": perk["perk_family"],
        "tier": perk["perk_tier"],
        "key": perk["perk_key"],
        "identity_status": perk["identity_status"],
        "ability_kit_path": perk["ability_kit_path"],
        "status": "unresolved_ability_kit" if perk["ability_kit_id"] is None else "resolved",
        "effects": [],
        "family_ability_implementations": _perk_ability_implementations(
            connection, snapshot_id, perk["perk_family"]
        ),
    }
    if perk["ability_kit_id"] is not None:
        kit_source = connection.execute(
            """
            SELECT ak.package_path, af.source_path, af.content_sha256
            FROM catalog_ability_kits ak
            JOIN asset_objects ao ON ao.id=ak.source_object_id
            JOIN asset_files af ON af.id=ao.asset_file_id
            WHERE ak.id=?
            """,
            (perk["ability_kit_id"],),
        ).fetchone()
        if kit_source is not None:
            result["ability_kit_source"] = _source_evidence(kit_source)
        unresolved_grants = connection.execute(
            """
            SELECT grant_row.target_path
            FROM catalog_ability_kit_grants grant_row
            JOIN asset_references reference
              ON reference.id=grant_row.source_reference_id
            WHERE grant_row.ability_kit_id=?
              AND grant_row.grant_kind='gameplay_effect'
              AND (reference.resolution_status <> 'resolved'
                   OR grant_row.gameplay_effect_id IS NULL)
            ORDER BY grant_row.target_path
            """,
            (perk["ability_kit_id"],),
        ).fetchall()
        result["unresolved_grants"] = [row["target_path"] for row in unresolved_grants]
        modifiers = connection.execute(
            """
            SELECT ge.effect_name, ge.template_path, ge.stacking_type, ge.stack_limit,
                   ge.id AS gameplay_effect_id, ge.source_object_id AS effect_object_id,
                   em.attribute_name, em.modifier_operation, em.magnitude_kind,
                   em.curve_row_name, em.interpretation_status, em.literal_value,
                   em.evaluation_channel, em.source_required_tags_json,
                   em.source_ignored_tags_json, em.target_required_tags_json,
                   em.target_ignored_tags_json,
                   mag.calculation_type, mag.coefficient, mag.pre_additive,
                   mag.post_additive, mag.custom_calculation_path,
                   mag.set_by_caller_tag, mag.interpretation_status AS magnitude_status,
                   cp.output_value, cp.time_value,
                   af.source_path, af.content_sha256,
                   ctf.source_path AS curve_source_path,
                   ctf.content_sha256 AS curve_content_sha256
            FROM catalog_ability_kit_grants akg
            JOIN catalog_gameplay_effects ge ON ge.id=akg.gameplay_effect_id
            JOIN catalog_effect_modifiers em ON em.gameplay_effect_id=ge.id
            LEFT JOIN catalog_magnitudes mag ON mag.id=em.magnitude_id
            JOIN asset_objects ao ON ao.id=ge.source_object_id
            JOIN asset_files af ON af.id=ao.asset_file_id
            LEFT JOIN catalog_curve_rows cr ON cr.id=em.curve_row_id
            LEFT JOIN catalog_curve_points cp ON cp.curve_row_id=cr.id
            LEFT JOIN catalog_curve_tables ct ON ct.id=cr.curve_table_id
            LEFT JOIN asset_objects cto ON cto.id=ct.source_object_id
            LEFT JOIN asset_files ctf ON ctf.id=cto.asset_file_id
            WHERE akg.ability_kit_id=? AND akg.grant_kind='gameplay_effect'
            ORDER BY ge.id, em.modifier_ordinal, cp.point_ordinal
            """,
            (perk["ability_kit_id"],),
        ).fetchall()
        for modifier in modifiers:
            operation = modifier["modifier_operation"] or ""
            value = (
                modifier["output_value"]
                if modifier["output_value"] is not None
                else modifier["literal_value"]
            )
            percent_bonus = None
            if (
                value is not None
                and operation.endswith("::Multiplicitive")
                and modifier["magnitude_status"] != "opaque"
            ):
                percent_bonus = round((float(value) - 1.0) * 100.0, 6)
            result["effects"].append(
                {
                    "effect": modifier["effect_name"],
                    "attribute": modifier["attribute_name"],
                    "operation": modifier["modifier_operation"],
                    "curve_row": modifier["curve_row_name"],
                    "value": value,
                    "percent_bonus": percent_bonus,
                    "interpretation_status": modifier["interpretation_status"],
                    "magnitude": {
                        "calculation_type": modifier["calculation_type"],
                        "coefficient": modifier["coefficient"],
                        "pre_additive": modifier["pre_additive"],
                        "post_additive": modifier["post_additive"],
                        "custom_calculation_path": modifier["custom_calculation_path"],
                        "set_by_caller_tag": modifier["set_by_caller_tag"],
                        "status": modifier["magnitude_status"],
                    },
                    "applicability": {
                        "source_required_tags": json.loads(
                            modifier["source_required_tags_json"]
                        ),
                        "source_ignored_tags": json.loads(
                            modifier["source_ignored_tags_json"]
                        ),
                        "target_required_tags": json.loads(
                            modifier["target_required_tags_json"]
                        ),
                        "target_ignored_tags": json.loads(
                            modifier["target_ignored_tags_json"]
                        ),
                    },
                    "evaluation_channel": modifier["evaluation_channel"],
                    "template_path": modifier["template_path"],
                    "mechanics": _effect_mechanics(
                        connection, modifier["gameplay_effect_id"]
                    ),
                    "inheritance": _effect_inheritance(
                        connection, modifier["effect_object_id"]
                    ),
                    "source": {
                        "effect_path": modifier["source_path"],
                        "effect_sha256": modifier["content_sha256"],
                        "curve_path": modifier["curve_source_path"],
                        "curve_sha256": modifier["curve_content_sha256"],
                    },
                }
            )
        if result["unresolved_grants"]:
            result["status"] = "partial_missing_grants"
        elif result["family_ability_implementations"]:
            unresolved_abilities = any(
                item["resolution_status"] != "resolved"
                for item in result["family_ability_implementations"]
            )
            result["status"] = (
                "partial_blueprint_behavior"
                if unresolved_abilities
                else "structured_blueprint_behavior"
            )
        elif not result["effects"]:
            result["status"] = "resolved_kit_without_supported_effect"
    return result


def _effect_mechanics(connection: sqlite3.Connection, effect_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT mechanic.mechanic_type, mechanic.property_path,
               mechanic.conditions_json, mechanic.value_json,
               mechanic.interpretation_status,
               magnitude.calculation_type, magnitude.literal_value,
               magnitude.coefficient, magnitude.pre_additive,
               magnitude.post_additive, magnitude.curve_table_path,
               magnitude.curve_row_name, magnitude.custom_calculation_path,
               magnitude.set_by_caller_tag,
               magnitude.interpretation_status AS magnitude_status,
               (SELECT point.output_value
                FROM catalog_curve_points point
                WHERE point.curve_row_id=magnitude.curve_row_id
                ORDER BY point.point_ordinal LIMIT 1) AS curve_output_value
        FROM catalog_mechanics mechanic
        LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
        WHERE mechanic.owner_domain='gameplay_effect' AND mechanic.owner_id=?
        ORDER BY mechanic.id
        """,
        (effect_id,),
    ).fetchall()
    return [
        {
            "type": row["mechanic_type"],
            "property_path": row["property_path"],
            "conditions": json.loads(row["conditions_json"]),
            "value": json.loads(row["value_json"]),
            "status": row["interpretation_status"],
            "magnitude": (
                {
                    "calculation_type": row["calculation_type"],
                    "literal_value": row["literal_value"],
                    "coefficient": row["coefficient"],
                    "pre_additive": row["pre_additive"],
                    "post_additive": row["post_additive"],
                    "curve_table_path": row["curve_table_path"],
                    "curve_row_name": row["curve_row_name"],
                    "curve_output_value": row["curve_output_value"],
                    "custom_calculation_path": row["custom_calculation_path"],
                    "set_by_caller_tag": row["set_by_caller_tag"],
                    "status": row["magnitude_status"],
                }
                if row["calculation_type"] is not None
                else None
            ),
        }
        for row in rows
    ]


def _effect_inheritance(
    connection: sqlite3.Connection, source_object_id: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT relation, target_path, resolution_status
            FROM catalog_inheritance_edges
            WHERE source_object_id=? ORDER BY id
            """,
            (source_object_id,),
        )
    ]


def _source_evidence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "package_path": row["package_path"],
        "source_path": row["source_path"],
        "source_sha256": row["content_sha256"],
    }


def unresolved_reference_report(
    connection: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    source_prefix: str | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "references": []}
    parameters: list[Any] = [snapshot_id]
    prefix_clause = ""
    if source_prefix:
        prefix_clause = " AND ao.package_path LIKE ?"
        parameters.append(f"{source_prefix}%")
    rows = connection.execute(
        f"""
        SELECT ar.resolution_status, ao.package_path AS source_package,
               ar.property_path, ar.target_path
        FROM asset_references ar
        JOIN asset_objects ao ON ao.id=ar.source_object_id
        WHERE ar.snapshot_id=? AND ar.resolution_status <> 'resolved'{prefix_clause}
        ORDER BY ao.package_path, ar.property_path, ar.target_path
        """,
        parameters,
    ).fetchall()
    counts = {"unresolved": 0, "ambiguous": 0}
    for row in rows:
        counts[row["resolution_status"]] += 1
    return {
        "snapshot_id": snapshot_id,
        "counts": counts,
        "references": [dict(row) for row in rows],
    }


def _hero_reference_closure(
    connection: sqlite3.Connection, snapshot_id: int, hero_name: str
) -> set[int]:
    hero = connection.execute(
        """
        SELECT id, source_object_id FROM catalog_heroes
        WHERE snapshot_id=?
          AND (lower(display_name)=lower(?) OR lower(hero_key)=lower(?))
        """,
        (snapshot_id, hero_name, hero_name),
    ).fetchone()
    if hero is None:
        raise ValueError(f"hero not found in snapshot {snapshot_id}: {hero_name}")
    roots = {hero["source_object_id"]}
    roots.update(
        row["source_object_id"]
        for row in connection.execute(
            "SELECT source_object_id FROM catalog_hero_variants WHERE hero_id=?",
            (hero["id"],),
        )
    )
    closure = set(roots)
    frontier = list(roots)
    while frontier:
        source_id = frontier.pop()
        for row in connection.execute(
            """
            SELECT target_object_id FROM asset_references
            WHERE source_object_id=? AND resolution_status='resolved'
              AND target_object_id IS NOT NULL
            """,
            (source_id,),
        ):
            target_id = row["target_object_id"]
            if target_id not in closure:
                closure.add(target_id)
                frontier.append(target_id)
    return closure


def _queue_classification(
    source_type: str, property_path: str, target_package: str
) -> tuple[int, str, str]:
    path = property_path.lower()
    target = target_package.lower()
    target_name = target.rsplit("/", 1)[-1]
    if target.startswith("/script/") or target == "/script":
        return 99, "engine_native", "engine/script objects are not FModel export targets"
    if any(
        token in path
        for token in (
            "cosmetic",
            "icon",
            "feedback",
            "frontend",
            "sacrificerecipe",
            "leveltoxp",
            "leveltosacrificexp",
        )
    ):
        return 4, "out_of_scope", "presentation or progression data is outside Phase 2"
    if "heroperk" in path or "commanderperk" in path:
        return 0, "hero_perk_kit", "closes a hero support/commander perk grant"
    if "tierabilitykits" in path or "grantedabilitykit" in path:
        return 0, "active_ability_kit", "closes a hero active-ability or perk kit"
    if "gadgets" in path:
        return (
            0,
            "active_ability_implementation",
            "resolves the active ability gadget implementation",
        )
    if "gameplayability" in path or "gameplayabilities" in path:
        return (
            0,
            "active_ability_logic",
            "resolves the GameplayAbility explicitly selected by the gadget",
        )
    if ".items." in path:
        return (
            1,
            "active_ability_resource",
            "resolves an item or resource explicitly used by an active ability kit",
        )
    if "combinedstatges" in path:
        return 2, "hero_stat_effect", "supports later hero-stat evaluation"
    if "damagestathandle" in path:
        return 1, "ability_scaling", "resolves an active ability's damage-stat row"
    if "classabilitykits" in path:
        return 1, "hero_class_perk", "closes a class-granted perk kit"
    if "additionalitemstoloadwhenequipped" in path:
        return 1, "ability_payload", "resolves an item used by an active ability"
    if "gameplayeffect" in path:
        return 0, "granted_gameplay_effect", "reveals an explicitly granted GameplayEffect"
    if target_name.startswith(("ge_", "get_")):
        return (
            0,
            "referenced_gameplay_effect",
            "resolves a GameplayEffect explicitly referenced by Blueprint defaults",
        )
    if target_name.startswith(("ga_", "gat_")):
        return (
            0,
            "referenced_ability_logic",
            "resolves GameplayAbility logic explicitly referenced by the perk graph",
        )
    if ".template." in path or ".super." in path or ".archetype." in path:
        return 0, "inheritance", "closes inherited/shared gameplay semantics"
    if "curvetable" in path:
        return 0, "balance_curve", "resolves a mechanic's exact numerical magnitude"
    if "calculationclass" in path or "execution" in path:
        return 0, "custom_calculation", "identifies currently opaque custom behavior"
    if "heroclassgameplaydefinition" in path:
        return 1, "hero_class", "closes the hero class definition"
    if "grantedabilities" in path or "abilitytriggers" in path:
        return 1, "granted_ability", "closes an explicitly granted or triggered ability"
    if "cooldown" in path or "cost" in path:
        return 1, "ability_mechanic", "resolves ability cooldown or cost mechanics"
    if "tag" in path and ("datatable" in source_type.lower() or "dictionary" in path):
        return 2, "gameplay_tags", "expands gameplay-tag identity or hierarchy"
    return 3, "unclassified", "reference is not yet classified as Phase 2-critical"


def asset_export_queue(
    connection: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    hero_name: str | None = None,
    include_low_priority: bool = False,
    max_priority: int = 2,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "hero": hero_name, "assets": [], "counts": {}}
    closure = (
        _hero_reference_closure(connection, snapshot_id, hero_name)
        if hero_name
        else None
    )
    parameters: list[Any] = [snapshot_id]
    closure_clause = ""
    if closure is not None:
        placeholders = ",".join("?" for _ in closure)
        closure_clause = f" AND ar.source_object_id IN ({placeholders})"
        parameters.extend(sorted(closure))
    rows = connection.execute(
        f"""
        SELECT ar.resolution_status, ar.property_path, ar.target_path,
               ar.target_package_path, ao.package_path AS source_package,
               ao.object_type AS source_type
        FROM asset_references ar
        JOIN asset_objects ao ON ao.id=ar.source_object_id
        WHERE ar.snapshot_id=? AND ar.resolution_status <> 'resolved'
        {closure_clause}
        ORDER BY ar.target_package_path, ar.property_path
        """,
        parameters,
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        priority, category, unlock = _queue_classification(
            row["source_type"], row["property_path"], row["target_package_path"]
        )
        priority_limit = 4 if include_low_priority else max_priority
        if priority == 99 or priority > priority_limit:
            continue
        entry = grouped.setdefault(
            row["target_package_path"],
            {
                "package_path": row["target_package_path"],
                "priority": priority,
                "categories": set(),
                "unlocks": set(),
                "target_paths": set(),
                "source_packages": set(),
                "reference_count": 0,
                "resolution_statuses": set(),
            },
        )
        entry["priority"] = min(entry["priority"], priority)
        entry["categories"].add(category)
        entry["unlocks"].add(unlock)
        entry["target_paths"].add(row["target_path"])
        entry["source_packages"].add(row["source_package"])
        entry["resolution_statuses"].add(row["resolution_status"])
        entry["reference_count"] += 1
    assets: list[dict[str, Any]] = []
    for entry in grouped.values():
        assets.append(
            {
                **entry,
                "categories": sorted(entry["categories"]),
                "unlocks": sorted(entry["unlocks"]),
                "target_paths": sorted(entry["target_paths"]),
                "source_packages": sorted(entry["source_packages"]),
                "resolution_statuses": sorted(entry["resolution_statuses"]),
            }
        )
    assets.sort(key=lambda item: (item["priority"], item["package_path"]))
    counts: dict[str, int] = {}
    for asset in assets:
        key = f"priority_{asset['priority']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "snapshot_id": snapshot_id,
        "hero": hero_name,
        "max_priority": 4 if include_low_priority else max_priority,
        "selection_rule": (
            "only exact unresolved/ambiguous references already present in exported data; "
            "paths are never synthesized from naming conventions"
        ),
        "counts": counts,
        "assets": assets,
    }


def _semantic_graph_index(
    connection: sqlite3.Connection, snapshot_id: int
) -> tuple[dict[int, list[sqlite3.Row]], dict[str, list[int]]]:
    references: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(
        """
        SELECT reference.source_object_id, reference.resolution_status,
               reference.property_path, reference.target_path,
               reference.target_package_path, reference.target_object_id,
               source.package_path AS source_package,
               source.object_type AS source_type
        FROM asset_references reference
        JOIN asset_objects source ON source.id=reference.source_object_id
        WHERE reference.snapshot_id=? ORDER BY reference.id
        """,
        (snapshot_id,),
    ):
        references.setdefault(row["source_object_id"], []).append(row)
    package_objects: dict[str, list[int]] = {}
    for row in connection.execute(
        """
        SELECT id, package_path FROM asset_objects
        WHERE snapshot_id=? AND package_path IS NOT NULL
        ORDER BY package_path, export_index
        """,
        (snapshot_id,),
    ):
        package_objects.setdefault(row["package_path"], []).append(row["id"])
    return references, package_objects


def _semantic_perk_closure(
    connection: sqlite3.Connection,
    snapshot_id: int,
    perk_family: str,
    *,
    reference_index: dict[int, list[sqlite3.Row]] | None = None,
    package_object_index: dict[str, list[int]] | None = None,
) -> tuple[set[int], list[dict[str, Any]], list[sqlite3.Row]]:
    perks = connection.execute(
        """
        SELECT perk.*, kit.source_object_id AS kit_source_object_id,
               reference.resolution_status AS kit_resolution_status,
               reference.target_path AS kit_target_path,
               reference.target_package_path AS kit_target_package,
               source.package_path AS kit_source_package,
               source.object_type AS kit_source_type,
               reference.property_path AS kit_property_path
        FROM catalog_perks perk
        LEFT JOIN catalog_ability_kits kit ON kit.id=perk.ability_kit_id
        LEFT JOIN asset_references reference ON reference.id=perk.source_reference_id
        LEFT JOIN asset_objects source ON source.id=reference.source_object_id
        WHERE perk.snapshot_id=? AND perk.perk_family=?
        ORDER BY perk.perk_tier
        """,
        (snapshot_id, perk_family),
    ).fetchall()
    closure = {
        row["kit_source_object_id"]
        for row in perks
        if row["kit_source_object_id"] is not None
    }
    unresolved: dict[str, dict[str, Any]] = {}

    def record_dependency(
        *,
        target_package: str,
        target_path: str,
        property_path: str,
        source_package: str | None,
        source_type: str,
        resolution_status: str,
    ) -> None:
        priority, category, reason = _queue_classification(
            source_type, property_path, target_package
        )
        if priority == 99 or category not in SEMANTIC_DEPENDENCY_CATEGORIES:
            return
        entry = unresolved.setdefault(
            target_package,
            {
                "package_path": target_package,
                "priority": priority,
                "categories": set(),
                "reasons": set(),
                "target_paths": set(),
                "source_packages": set(),
                "resolution_statuses": set(),
                "reference_count": 0,
            },
        )
        entry["priority"] = min(entry["priority"], priority)
        entry["categories"].add(category)
        entry["reasons"].add(reason)
        entry["target_paths"].add(target_path)
        if source_package:
            entry["source_packages"].add(source_package)
        entry["resolution_statuses"].add(resolution_status)
        entry["reference_count"] += 1

    for row in perks:
        if row["ability_kit_id"] is not None or row["kit_target_package"] is None:
            continue
        record_dependency(
            target_package=row["kit_target_package"],
            target_path=row["kit_target_path"],
            property_path=row["kit_property_path"],
            source_package=row["kit_source_package"],
            source_type=row["kit_source_type"] or "FortHeroGameplayDefinition",
            resolution_status=row["kit_resolution_status"] or "unresolved",
        )

    frontier = list(closure)
    while frontier:
        source_object_id = frontier.pop()
        rows = (
            reference_index.get(source_object_id, [])
            if reference_index is not None
            else connection.execute(
                """
                SELECT reference.resolution_status, reference.property_path,
                       reference.target_path, reference.target_package_path,
                       reference.target_object_id,
                       source.package_path AS source_package,
                       source.object_type AS source_type
                FROM asset_references reference
                JOIN asset_objects source ON source.id=reference.source_object_id
                WHERE reference.source_object_id=?
                ORDER BY reference.id
                """,
                (source_object_id,),
            ).fetchall()
        )
        for row in rows:
            priority, category, _ = _queue_classification(
                row["source_type"], row["property_path"], row["target_package_path"]
            )
            if priority == 99 or category not in SEMANTIC_DEPENDENCY_CATEGORIES:
                continue
            if (
                row["resolution_status"] == "resolved"
                and row["target_object_id"] is not None
            ):
                # FModel commonly emits a generated class and its default object
                # in one package.  A structural reference may select either one,
                # while normalized mechanics live on the other.  Once the package
                # itself has resolved, traverse every exported object in that exact
                # package; do not synthesize sibling package paths.
                package_objects = (
                    package_object_index.get(row["target_package_path"], [])
                    if package_object_index is not None
                    else [
                        package_object["id"]
                        for package_object in connection.execute(
                            """
                            SELECT id FROM asset_objects
                            WHERE snapshot_id=? AND package_path=?
                            ORDER BY export_index
                            """,
                            (snapshot_id, row["target_package_path"]),
                        )
                    ]
                )
                for target_id in package_objects:
                    if target_id not in closure:
                        closure.add(target_id)
                        frontier.append(target_id)
                continue
            record_dependency(
                target_package=row["target_package_path"],
                target_path=row["target_path"],
                property_path=row["property_path"],
                source_package=row["source_package"],
                source_type=row["source_type"],
                resolution_status=row["resolution_status"],
            )

    dependencies = [
        {
            **entry,
            "categories": sorted(entry["categories"]),
            "reasons": sorted(entry["reasons"]),
            "target_paths": sorted(entry["target_paths"]),
            "source_packages": sorted(entry["source_packages"]),
            "resolution_statuses": sorted(entry["resolution_statuses"]),
        }
        for entry in unresolved.values()
    ]
    dependencies.sort(key=lambda item: (item["priority"], item["package_path"]))
    return closure, dependencies, perks


def perk_family_semantic_report(
    connection: sqlite3.Connection,
    perk_family: str,
    snapshot_id: int | None = None,
    *,
    reference_index: dict[int, list[sqlite3.Row]] | None = None,
    package_object_index: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    closure, dependencies, perks = _semantic_perk_closure(
        connection,
        snapshot_id,
        perk_family,
        reference_index=reference_index,
        package_object_index=package_object_index,
    )
    if not perks:
        raise ValueError(f"perk family not found in snapshot {snapshot_id}: {perk_family}")
    supported_mechanics = supported_modifiers = opaque_mechanics = 0
    blueprint_partial = blueprint_opaque = 0
    if closure:
        placeholders = ",".join("?" for _ in closure)
        parameters = sorted(closure)
        supported_mechanics = connection.execute(
            f"""
            SELECT COUNT(*) FROM catalog_mechanics
            WHERE source_object_id IN ({placeholders})
              AND interpretation_status='supported'
            """,
            parameters,
        ).fetchone()[0]
        supported_modifiers = connection.execute(
            f"""
            SELECT COUNT(*) FROM catalog_effect_modifiers modifier
            JOIN catalog_gameplay_effects effect
              ON effect.id=modifier.gameplay_effect_id
            WHERE effect.source_object_id IN ({placeholders})
              AND modifier.interpretation_status='supported'
            """,
            parameters,
        ).fetchone()[0]
        opaque_mechanics = connection.execute(
            f"""
            SELECT COUNT(*) FROM (
              SELECT source_object_id, property_path FROM catalog_opaque_mechanics
              WHERE source_object_id IN ({placeholders})
              UNION
              SELECT source_object_id, property_path FROM catalog_mechanics
              WHERE source_object_id IN ({placeholders})
                AND interpretation_status='opaque'
            )
            """,
            parameters + parameters,
        ).fetchone()[0]
        blueprint_partial, blueprint_opaque = connection.execute(
            f"""
            SELECT
              COALESCE(SUM(CASE WHEN semantic_status='partial' THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN semantic_status='opaque' THEN 1 ELSE 0 END), 0)
            FROM catalog_abilities
            WHERE source_object_id IN ({placeholders})
            """,
            parameters,
        ).fetchone()
    supported_facts = supported_mechanics + supported_modifiers
    missing_kits = [
        row["ability_kit_path"] for row in perks if row["ability_kit_id"] is None
    ]
    reasons: list[dict[str, Any]] = []
    if missing_kits:
        reasons.append(
            {"code": "missing_perk_kits", "count": len(missing_kits), "assets": missing_kits}
        )
    if dependencies:
        reasons.append(
            {
                "code": "unresolved_semantic_dependencies",
                "count": len(dependencies),
                "assets": [item["package_path"] for item in dependencies],
            }
        )
    if blueprint_partial or blueprint_opaque:
        reasons.append(
            {
                "code": "blueprint_behavior_not_executed",
                "count": blueprint_partial + blueprint_opaque,
            }
        )
    if opaque_mechanics:
        reasons.append({"code": "opaque_custom_mechanics", "count": opaque_mechanics})
    if supported_facts == 0:
        reasons.append({"code": "no_supported_semantic_facts", "count": 1})

    if supported_facts == 0 and (opaque_mechanics or blueprint_opaque):
        status = "opaque"
    elif reasons:
        status = "partial"
    else:
        status = "resolved"
    return {
        "snapshot_id": snapshot_id,
        "perk_family": perk_family,
        "tiers": [row["perk_tier"] for row in perks],
        "status": status,
        "optimization_ready": status == "resolved",
        "evidence": {
            "transitive_asset_objects": len(closure),
            "supported_mechanics": supported_mechanics,
            "supported_modifiers": supported_modifiers,
            "opaque_mechanics": opaque_mechanics,
            "blueprint_partial_abilities": blueprint_partial,
            "blueprint_opaque_abilities": blueprint_opaque,
        },
        "reasons": reasons,
        "unresolved_dependencies": dependencies,
    }


def roster_coverage_report(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "summary": {}, "heroes": [], "perk_families": []}
    families = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT perk_family FROM catalog_perks
            WHERE snapshot_id=? ORDER BY perk_family
            """,
            (snapshot_id,),
        )
    ]
    reference_index, package_object_index = _semantic_graph_index(
        connection, snapshot_id
    )
    family_reports = {
        family: perk_family_semantic_report(
            connection,
            family,
            snapshot_id,
            reference_index=reference_index,
            package_object_index=package_object_index,
        )
        for family in families
    }
    heroes: list[dict[str, Any]] = []
    hero_status_counts = {"resolved": 0, "partial": 0, "opaque": 0}
    rows = connection.execute(
        """
        SELECT id, hero_key, display_name, hero_class
        FROM catalog_heroes WHERE snapshot_id=? ORDER BY display_name, hero_key
        """,
        (snapshot_id,),
    ).fetchall()
    for hero in rows:
        variants = connection.execute(
            """
            SELECT variant_key, display_name, rarity, tier
            FROM catalog_hero_variants WHERE hero_id=? ORDER BY variant_key
            """,
            (hero["id"],),
        ).fetchall()
        assignments = connection.execute(
            """
            SELECT hero_perk.perk_mode, perk.perk_family, perk.perk_tier
            FROM catalog_hero_perks hero_perk
            JOIN catalog_perks perk ON perk.id=hero_perk.perk_id
            WHERE hero_perk.hero_id=? ORDER BY hero_perk.perk_mode
            """,
            (hero["id"],),
        ).fetchall()
        modes = {row["perk_mode"] for row in assignments}
        statuses = [family_reports[row["perk_family"]]["status"] for row in assignments]
        missing_modes = sorted({"support", "commander"} - modes)
        if missing_modes or "partial" in statuses:
            hero_status = "partial"
        elif statuses and all(status == "opaque" for status in statuses):
            hero_status = "opaque"
        elif statuses and all(status == "resolved" for status in statuses):
            hero_status = "resolved"
        else:
            hero_status = "partial"
        hero_status_counts[hero_status] += 1
        heroes.append(
            {
                "hero_key": hero["hero_key"],
                "display_name": hero["display_name"],
                "hero_class": hero["hero_class"],
                "status": hero_status,
                "missing_perk_modes": missing_modes,
                "variant_count": len(variants),
                "variants": [dict(row) for row in variants],
                "perks": [
                    {
                        **dict(row),
                        "semantic_status": family_reports[row["perk_family"]]["status"],
                        "optimization_ready": family_reports[row["perk_family"]][
                            "optimization_ready"
                        ],
                    }
                    for row in assignments
                ],
            }
        )
    raw_hids = connection.execute(
        """
        SELECT COUNT(*) FROM asset_objects
        WHERE snapshot_id=? AND object_type='FortHeroType'
        """,
        (snapshot_id,),
    ).fetchone()[0]
    mapped_hids = sum(hero["variant_count"] for hero in heroes)
    family_status_counts = {"resolved": 0, "partial": 0, "opaque": 0}
    for report in family_reports.values():
        family_status_counts[report["status"]] += 1
    optimization_ready = family_status_counts["resolved"]
    identities_with_variants = sum(hero["variant_count"] > 0 for hero in heroes)
    heroes_missing_perks = sum(bool(hero["missing_perk_modes"]) for hero in heroes)
    hero_perk_assignments = sum(len(hero["perks"]) for hero in heroes)
    observed_classes = sorted(
        {hero["hero_class"] for hero in heroes if hero["hero_class"]}
    )
    required_folder_scopes, required_asset_scopes = _required_roster_export_scopes()
    observed_export_scopes, missing_export_scopes = _observed_roster_export_scopes(
        connection, snapshot_id
    )
    receipt = connection.execute(
        """
        SELECT plan_version, attestation_text, recorded_at
        FROM asset_roster_export_receipts WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ).fetchone()
    all_heroes_have_perks = all(not hero["missing_perk_modes"] for hero in heroes)
    complete_roster_claimed = (
        receipt is not None
        and receipt["plan_version"] == ROSTER_PLAN_VERSION
        and not missing_export_scopes
        and raw_hids == mapped_hids
        and all_heroes_have_perks
        and set(observed_classes) == set(STW_HERO_CLASSES)
        and bool(heroes)
    )
    return {
        "snapshot_id": snapshot_id,
        "catalog_awareness": {
            "status": (
                "controlled_roster_batches_observed"
                if complete_roster_claimed
                else "incomplete_or_unverified_folder_export"
            ),
            "identity_rule": (
                "one canonical hero per exported FortHeroGameplayDefinition; "
                "FortHeroType rarity/evolution records remain linked variants"
            ),
            "observed_classes": observed_classes,
            "expected_class_folders": list(STW_HERO_CLASSES),
            "observed_export_scope_count": len(observed_export_scopes),
            "required_export_scope_count": (
                len(required_folder_scopes) + len(required_asset_scopes)
            ),
            "missing_export_scopes": missing_export_scopes,
            "recursive_export_receipt": dict(receipt) if receipt else None,
            "complete_roster_claimed": complete_roster_claimed,
        },
        "summary": {
            "unique_hero_gameplay_identities": len(heroes),
            "hero_identities_with_hid_variants": identities_with_variants,
            "hero_identities_without_hid_variants": len(heroes) - identities_with_variants,
            "raw_hid_objects": raw_hids,
            "mapped_hid_variants": mapped_hids,
            "unmapped_hid_variants": raw_hids - mapped_hids,
            "unique_perk_families": len(family_reports),
            "hero_perk_assignments": hero_perk_assignments,
            "heroes_missing_support_or_commander": heroes_missing_perks,
            "hero_status_counts": hero_status_counts,
            "perk_family_status_counts": family_status_counts,
            "optimization_ready_perk_families": optimization_ready,
            "optimization_ready_percentage": (
                optimization_ready / len(family_reports) if family_reports else None
            ),
        },
        "heroes": heroes,
        "perk_families": list(family_reports.values()),
    }


def _fmodel_path(package_path: str, *, folder: bool = False) -> str:
    if package_path == "/SaveTheWorld" or package_path.startswith("/SaveTheWorld/"):
        suffix = package_path[len("/SaveTheWorld") :].lstrip("/")
        base = "FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content"
    elif package_path == "/Game" or package_path.startswith("/Game/"):
        suffix = package_path[len("/Game") :].lstrip("/")
        base = "FortniteGame/Content"
    else:
        return package_path
    path = f"{base}/{suffix}" if suffix else base
    return path if folder else f"{path}.uasset"


def _required_roster_export_scopes() -> tuple[list[str], list[str]]:
    folders = [
        _fmodel_path(f"/SaveTheWorld/Heroes/{hero_class}/GameplayDefinition", folder=True)
        for hero_class in STW_HERO_CLASSES
    ] + [
        _fmodel_path(f"/SaveTheWorld/Heroes/{hero_class}/ItemDefinition", folder=True)
        for hero_class in STW_HERO_CLASSES
    ] + [
        _fmodel_path("/SaveTheWorld/Abilities/Player/Perks/Hero", folder=True),
        _fmodel_path("/SaveTheWorld/Abilities/Player/Perks/Leader", folder=True),
        _fmodel_path("/SaveTheWorld/GameplayEffectTemplates", folder=True),
        _fmodel_path("/Game/GameplayEffectTemplates", folder=True),
        _fmodel_path("/Game/Abilities/Player/Parents", folder=True),
    ]
    assets = [_fmodel_path("/Game/Balance/DataTables/CombatEffects_HeroAbilities")]
    return folders, assets


def _observed_roster_export_scopes(
    connection: sqlite3.Connection, snapshot_id: int
) -> tuple[list[str], list[str]]:
    folders, assets = _required_roster_export_scopes()
    source_paths = [
        row[0].replace("\\", "/")
        for row in connection.execute(
            "SELECT source_path FROM asset_files WHERE snapshot_id=?", (snapshot_id,)
        )
    ]

    def folder_observed(scope: str) -> bool:
        marker = f"/{scope.strip('/')}/"
        return any(marker in f"/{path.strip('/')}/" for path in source_paths)

    def asset_observed(scope: str) -> bool:
        expected_json = f"{scope[:-7]}.json" if scope.endswith(".uasset") else scope
        return any(path.endswith(expected_json) for path in source_paths)

    observed = [scope for scope in folders if folder_observed(scope)] + [
        scope for scope in assets if asset_observed(scope)
    ]
    missing = sorted((set(folders) | set(assets)) - set(observed))
    return observed, missing


def record_roster_export_receipt(
    connection: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    confirm_complete_recursive_export: bool = False,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    if not confirm_complete_recursive_export:
        raise ValueError(
            "recording a roster receipt requires explicit confirmation that every "
            "batch-plan folder was exported recursively"
        )
    observed, missing = _observed_roster_export_scopes(connection, snapshot_id)
    if missing:
        raise ValueError(
            "cannot record complete-roster export receipt; missing scopes: "
            + ", ".join(missing)
        )
    attestation = (
        "operator confirmed every phase2-roster-v1 folder was exported recursively; "
        "scope presence was verified against this immutable snapshot manifest"
    )
    with connection:
        connection.execute(
            """
            INSERT INTO asset_roster_export_receipts(
                snapshot_id, plan_version, scopes_json, attestation_text
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                plan_version=excluded.plan_version,
                scopes_json=excluded.scopes_json,
                attestation_text=excluded.attestation_text,
                recorded_at=CURRENT_TIMESTAMP
            """,
            (
                snapshot_id,
                ROSTER_PLAN_VERSION,
                json.dumps(observed, separators=(",", ":")),
                attestation,
            ),
        )
    return {
        "snapshot_id": snapshot_id,
        "plan_version": ROSTER_PLAN_VERSION,
        "observed_scope_count": len(observed),
        "attestation": attestation,
    }


def full_roster_batch_plan(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    roster = roster_coverage_report(connection, snapshot_id)
    if snapshot_id is None:
        return {"snapshot_id": None, "batches": [], "follow_up_batches": []}
    hero_count = roster["summary"]["unique_hero_gameplay_identities"]
    family_count = roster["summary"]["unique_perk_families"]
    hero_names = [hero["display_name"] for hero in roster["heroes"]]
    family_names = [report["perk_family"] for report in roster["perk_families"]]
    dependency_families: dict[str, set[str]] = {}
    for report in roster["perk_families"]:
        for dependency in report["unresolved_dependencies"]:
            dependency_families.setdefault(dependency["package_path"], set()).add(
                report["perk_family"]
            )

    def dependencies_under(prefixes: tuple[str, ...]) -> tuple[list[str], list[str]]:
        packages = sorted(
            package
            for package in dependency_families
            if any(package == prefix or package.startswith(f"{prefix}/") for prefix in prefixes)
        )
        affected = sorted(
            {
                family
                for package in packages
                for family in dependency_families.get(package, set())
            }
        )
        return packages, affected

    template_prefixes = (
        "/SaveTheWorld/GameplayEffectTemplates",
        "/Game/GameplayEffectTemplates",
        "/Game/Abilities/Player/Parents",
    )
    template_dependencies, template_families = dependencies_under(template_prefixes)
    gameplay_folders = [
        _fmodel_path(f"/SaveTheWorld/Heroes/{hero_class}/GameplayDefinition", folder=True)
        for hero_class in STW_HERO_CLASSES
    ]
    item_folders = [
        _fmodel_path(f"/SaveTheWorld/Heroes/{hero_class}/ItemDefinition", folder=True)
        for hero_class in STW_HERO_CLASSES
    ]
    batches = [
        {
            "priority": 0,
            "batch_id": "roster-gameplay-identities",
            "exact_fmodel_folders_or_searches": gameplay_folders,
            "expected_relevant_asset_types": ["FortHeroGameplayDefinition"],
            "why": (
                "enumerates canonical HGD gameplay identities and their explicit "
                "support/commander perk references"
            ),
            "unlocks": [
                "complete unique-hero count",
                "all support and commander perk assignments",
            ],
            "currently_known_heroes": hero_count,
            "currently_known_hero_names": hero_names,
            "deduplicated_export_scope_count": 4,
            "deduplicated_dependency_count": None,
        },
        {
            "priority": 1,
            "batch_id": "roster-hid-variants",
            "exact_fmodel_folders_or_searches": item_folders,
            "expected_relevant_asset_types": ["FortHeroType"],
            "why": (
                "maps every rarity/evolution HID to its explicitly referenced HGD "
                "without counting variants as heroes"
            ),
            "unlocks": ["friendly hero names", "variant and orphan-HID auditing"],
            "currently_mapped_variants": roster["summary"]["mapped_hid_variants"],
            "deduplicated_export_scope_count": 4,
            "deduplicated_dependency_count": None,
        },
        {
            "priority": 2,
            "batch_id": "hero-perk-implementations",
            "exact_fmodel_folders_or_searches": [
                _fmodel_path("/SaveTheWorld/Abilities/Player/Perks/Hero", folder=True),
                _fmodel_path("/SaveTheWorld/Abilities/Player/Perks/Leader", folder=True),
            ],
            "expected_relevant_asset_types": [
                "FortAbilityKit",
                "GameplayAbility Blueprint defaults",
                "GameplayEffect",
            ],
            "why": (
                "captures all HGD-selected support/commander kits and their shared "
                "Blueprint/effect implementations in two relevant folder exports"
            ),
            "unlocks": ["all currently discoverable hero perk families"],
            "currently_known_perk_families": family_count,
            "currently_known_perk_family_names": family_names,
            "deduplicated_export_scope_count": 2,
            "deduplicated_dependency_count": family_count,
        },
        {
            "priority": 3,
            "batch_id": "shared-perk-semantic-bases",
            "exact_fmodel_folders_or_searches": [
                _fmodel_path("/SaveTheWorld/GameplayEffectTemplates", folder=True),
                _fmodel_path("/Game/GameplayEffectTemplates", folder=True),
                _fmodel_path("/Game/Abilities/Player/Parents", folder=True),
            ],
            "expected_relevant_asset_types": [
                "GameplayEffect templates",
                "GameplayAbility parent classes",
            ],
            "why": (
                "closes inherited modifier, duration, healing, damage, and generic "
                "triggered-ability semantics"
            ),
            "unlocks": ["shared semantic bases reused across many perk families"],
            "currently_known_dependency_packages": template_dependencies,
            "currently_blocked_perk_families": template_families,
            "deduplicated_export_scope_count": 3,
            "deduplicated_dependency_count": len(template_dependencies),
        },
        {
            "priority": 4,
            "batch_id": "hero-perk-balance-table",
            "exact_fmodel_folders_or_searches": [
                _fmodel_path("/Game/Balance/DataTables/CombatEffects_HeroAbilities")
            ],
            "expected_relevant_asset_types": ["CurveTable"],
            "why": "resolves numerical curve rows referenced by hero perk mechanics",
            "unlocks": ["exact perk magnitudes, durations, chances, and thresholds"],
            "deduplicated_export_scope_count": 1,
            "deduplicated_dependency_count": 1,
        },
    ]

    covered_virtual_folders = (
        "/SaveTheWorld/Abilities/Player/Perks/Hero",
        "/SaveTheWorld/Abilities/Player/Perks/Leader",
        "/SaveTheWorld/GameplayEffectTemplates",
        "/Game/GameplayEffectTemplates",
        "/Game/Abilities/Player/Parents",
    )
    grouped: dict[str, dict[str, Any]] = {}
    for report in roster["perk_families"]:
        for dependency in report["unresolved_dependencies"]:
            package = dependency["package_path"]
            covered_by_initial_batch = any(
                package == folder or package.startswith(f"{folder}/")
                for folder in covered_virtual_folders
            )
            if (
                covered_by_initial_batch
                and not roster["catalog_awareness"]["complete_roster_claimed"]
            ):
                continue
            folder = package.rsplit("/", 1)[0]
            entry = grouped.setdefault(
                folder,
                {
                    "priority": 5,
                    "exact_fmodel_folder": _fmodel_path(folder, folder=True),
                    "dependency_packages": set(),
                    "perk_families": set(),
                    "categories": set(),
                    "inside_completed_initial_scope": covered_by_initial_batch,
                },
            )
            entry["dependency_packages"].add(package)
            entry["perk_families"].add(report["perk_family"])
            entry["categories"].update(dependency["categories"])
    follow_up = [
        {
            **entry,
            "dependency_packages": sorted(entry["dependency_packages"]),
            "perk_families": sorted(entry["perk_families"]),
            "categories": sorted(entry["categories"]),
            "deduplicated_dependency_count": len(entry["dependency_packages"]),
            "action": (
                "retry the listed exact packages; their containing recursive batch "
                "was completed but they remain absent"
                if entry["inside_completed_initial_scope"]
                else "export this folder recursively"
            ),
        }
        for entry in grouped.values()
    ]
    follow_up.sort(
        key=lambda item: (-item["deduplicated_dependency_count"], item["exact_fmodel_folder"])
    )
    return {
        "snapshot_id": snapshot_id,
        "strategy": (
            "export five ordered, relevant batches once; re-ingest the same read-only "
            "root; then use only graph-derived follow-up folders"
        ),
        "source_of_truth": (
            "HGD/HID and perk dependencies are accepted only through explicit asset "
            "references; folder names select export scope and are not semantic evidence"
        ),
        "batches": batches,
        "follow_up_batches": follow_up,
        "excluded_as_irrelevant": [
            "UI icons and portraits",
            "cosmetic outfits and backblings",
            "feedback and frontend animation assets",
            "XP, sacrifice, and progression tables",
            "active ability assets unless later required by optimizer scope",
        ],
    }


def catalog_coverage(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "ratios": {}}
    count_queries = {
        "heroes": "SELECT COUNT(*) FROM catalog_heroes WHERE snapshot_id=?",
        "hero_variants": """
            SELECT COUNT(*) FROM catalog_hero_variants hv
            JOIN catalog_heroes h ON h.id=hv.hero_id WHERE h.snapshot_id=?
        """,
        "hero_classes": "SELECT COUNT(*) FROM catalog_hero_classes WHERE snapshot_id=?",
        "hero_class_kits": "SELECT COUNT(*) FROM catalog_hero_class_kits WHERE snapshot_id=?",
        "resolved_hero_class_kit_files": """
            SELECT COUNT(*) FROM catalog_hero_class_kits
            WHERE snapshot_id=? AND ability_kit_id IS NOT NULL
        """,
        "abilities": "SELECT COUNT(*) FROM catalog_abilities WHERE snapshot_id=?",
        "perks": "SELECT COUNT(*) FROM catalog_perks WHERE snapshot_id=?",
        "perk_families": """
            SELECT COUNT(DISTINCT perk_family) FROM catalog_perks WHERE snapshot_id=?
        """,
        "resolved_perk_kit_files": """
            SELECT COUNT(*) FROM catalog_perks
            WHERE snapshot_id=? AND ability_kit_id IS NOT NULL
        """,
        "fully_resolved_perks": """
            SELECT COUNT(*) FROM catalog_perks perk
            WHERE perk.snapshot_id=? AND perk.ability_kit_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_ability_kit_grants grant_row
                  JOIN asset_references reference
                    ON reference.id=grant_row.source_reference_id
                  WHERE grant_row.ability_kit_id=perk.ability_kit_id
                    AND grant_row.grant_kind IN ('ability', 'gameplay_effect')
                    AND (reference.resolution_status <> 'resolved'
                         OR (grant_row.grant_kind='gameplay_effect'
                             AND grant_row.gameplay_effect_id IS NULL)
                         OR (grant_row.grant_kind='ability'
                             AND grant_row.ability_id IS NULL))
              )
        """,
        "perk_families_with_supported_effects": """
            SELECT COUNT(DISTINCT perk.perk_family)
            FROM catalog_perks perk
            JOIN catalog_ability_kit_grants grant_row
              ON grant_row.ability_kit_id=perk.ability_kit_id
            JOIN catalog_effect_modifiers modifier
              ON modifier.gameplay_effect_id=grant_row.gameplay_effect_id
            WHERE perk.snapshot_id=?
              AND modifier.interpretation_status='supported'
        """,
        "perk_families_with_blueprint_behavior": """
            SELECT COUNT(DISTINCT perk.perk_family)
            FROM catalog_perks perk
            JOIN catalog_ability_kit_grants grant_row
              ON grant_row.ability_kit_id=perk.ability_kit_id
            JOIN catalog_abilities ability ON ability.id=grant_row.ability_id
            WHERE perk.snapshot_id=?
              AND EXISTS (
                  SELECT 1 FROM catalog_mechanics mechanic
                  WHERE mechanic.owner_domain='ability'
                    AND mechanic.owner_id=ability.id
              )
        """,
        "hero_active_kits": """
            SELECT COUNT(*) FROM catalog_hero_abilities ability_row
            JOIN catalog_heroes hero ON hero.id=ability_row.hero_id
            WHERE hero.snapshot_id=?
        """,
        "resolved_active_kit_files": """
            SELECT COUNT(*) FROM catalog_hero_abilities ability_row
            JOIN catalog_heroes hero ON hero.id=ability_row.hero_id
            WHERE hero.snapshot_id=? AND ability_row.ability_kit_id IS NOT NULL
        """,
        "fully_resolved_active_kits": """
            SELECT COUNT(*) FROM catalog_hero_abilities hero_ability
            JOIN catalog_heroes hero ON hero.id=hero_ability.hero_id
            WHERE hero.snapshot_id=? AND hero_ability.ability_kit_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_ability_kit_grants grant_row
                  JOIN asset_references reference
                    ON reference.id=grant_row.source_reference_id
                  LEFT JOIN catalog_abilities ability
                    ON ability.id=grant_row.ability_id
                  WHERE grant_row.ability_kit_id=hero_ability.ability_kit_id
                    AND grant_row.grant_kind IN ('ability', 'gameplay_effect')
                    AND (
                        reference.resolution_status <> 'resolved'
                        OR (grant_row.grant_kind='ability'
                            AND grant_row.ability_id IS NULL)
                        OR (grant_row.grant_kind='gameplay_effect'
                            AND grant_row.gameplay_effect_id IS NULL)
                        OR EXISTS (
                            SELECT 1 FROM asset_references linked_reference
                            WHERE linked_reference.source_object_id=ability.source_object_id
                              AND lower(linked_reference.property_path)
                                  LIKE '%gameplayability%'
                              AND linked_reference.resolution_status <> 'resolved'
                        )
                        OR EXISTS (
                            SELECT 1 FROM catalog_ability_links ability_link
                            JOIN catalog_abilities linked_ability
                              ON linked_ability.id=ability_link.target_ability_id
                            JOIN asset_references dependency
                              ON dependency.source_object_id=linked_ability.source_object_id
                            WHERE ability_link.source_ability_id=ability.id
                              AND dependency.resolution_status <> 'resolved'
                              AND (
                                  lower(dependency.property_path) LIKE '%gameplayeffect%'
                                  OR lower(dependency.property_path) LIKE '%.template.%'
                                  OR lower(dependency.property_path) LIKE '%.super.%'
                                  OR lower(dependency.property_path) LIKE '%.archetype.%'
                              )
                        )
                    )
              )
        """,
        "ability_links": "SELECT COUNT(*) FROM catalog_ability_links WHERE snapshot_id=?",
        "data_tables": "SELECT COUNT(*) FROM catalog_data_tables WHERE snapshot_id=?",
        "referenced_data_rows": """
            SELECT COUNT(*) FROM catalog_data_rows row
            JOIN catalog_data_tables table_row ON table_row.id=row.data_table_id
            WHERE table_row.snapshot_id=?
        """,
        "unresolved_gameplay_ability_links": """
            SELECT COUNT(*) FROM asset_references reference
            JOIN catalog_abilities ability
              ON ability.source_object_id=reference.source_object_id
            WHERE ability.snapshot_id=?
              AND lower(reference.property_path) LIKE '%gameplayability%'
              AND reference.resolution_status <> 'resolved'
        """,
        "unresolved_active_semantic_dependencies": """
            SELECT COUNT(*) FROM catalog_ability_links link
            JOIN catalog_abilities target ON target.id=link.target_ability_id
            JOIN asset_references dependency
              ON dependency.source_object_id=target.source_object_id
            WHERE link.snapshot_id=? AND dependency.resolution_status <> 'resolved'
              AND (
                  lower(dependency.property_path) LIKE '%gameplayeffect%'
                  OR lower(dependency.property_path) LIKE '%.template.%'
                  OR lower(dependency.property_path) LIKE '%.super.%'
                  OR lower(dependency.property_path) LIKE '%.archetype.%'
              )
        """,
        "unresolved_semantic_grants": """
            SELECT COUNT(*) FROM catalog_ability_kit_grants grant_row
            JOIN catalog_ability_kits kit ON kit.id=grant_row.ability_kit_id
            JOIN asset_references reference ON reference.id=grant_row.source_reference_id
            WHERE kit.snapshot_id=?
              AND grant_row.grant_kind IN ('ability', 'gameplay_effect')
              AND (reference.resolution_status <> 'resolved'
                   OR (grant_row.grant_kind='ability' AND grant_row.ability_id IS NULL)
                   OR (grant_row.grant_kind='gameplay_effect'
                       AND grant_row.gameplay_effect_id IS NULL))
        """,
        "ability_kits": "SELECT COUNT(*) FROM catalog_ability_kits WHERE snapshot_id=?",
        "gameplay_effects": "SELECT COUNT(*) FROM catalog_gameplay_effects WHERE snapshot_id=?",
        "effect_modifiers": """
            SELECT COUNT(*) FROM catalog_effect_modifiers em
            JOIN catalog_gameplay_effects ge ON ge.id=em.gameplay_effect_id
            WHERE ge.snapshot_id=?
        """,
        "gameplay_tags": "SELECT COUNT(*) FROM catalog_gameplay_tags WHERE snapshot_id=?",
        "magnitudes": "SELECT COUNT(*) FROM catalog_magnitudes WHERE snapshot_id=?",
        "opaque_mechanics": "SELECT COUNT(*) FROM catalog_opaque_mechanics WHERE snapshot_id=?",
        "inheritance_edges": "SELECT COUNT(*) FROM catalog_inheritance_edges WHERE snapshot_id=?",
        "unresolved_inheritance": """
            SELECT COUNT(*) FROM catalog_inheritance_edges
            WHERE snapshot_id=? AND resolution_status <> 'resolved'
        """,
    }
    counts = {
        name: connection.execute(sql, (snapshot_id,)).fetchone()[0]
        for name, sql in count_queries.items()
    }
    queue = asset_export_queue(connection, snapshot_id)
    perks = counts["perks"]
    inheritance = counts["inheritance_edges"]
    return {
        "snapshot_id": snapshot_id,
        "counts": counts,
        "ratios": {
            "perk_kit_file_resolution": counts["resolved_perk_kit_files"] / perks if perks else None,
            "perk_semantic_resolution": counts["fully_resolved_perks"] / perks if perks else None,
            "perk_family_supported_effect_coverage": (
                counts["perk_families_with_supported_effects"] / counts["perk_families"]
                if counts["perk_families"]
                else None
            ),
            "perk_family_blueprint_behavior_coverage": (
                counts["perk_families_with_blueprint_behavior"] / counts["perk_families"]
                if counts["perk_families"]
                else None
            ),
            "inheritance_resolution": (
                (inheritance - counts["unresolved_inheritance"]) / inheritance
                if inheritance
                else None
            ),
        },
        "critical_export_queue": queue["counts"],
    }


def _snapshot_summary(
    connection: sqlite3.Connection, snapshot_id: int, *, idempotent: bool
) -> dict[str, Any]:
    snapshot = connection.execute(
        """
        SELECT s.*, gb.build_key, gb.game_version, gb.changelist
        FROM asset_snapshots s JOIN game_builds gb ON gb.id=s.game_build_id
        WHERE s.id=?
        """,
        (snapshot_id,),
    ).fetchone()
    counts = {
        table: connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()[0]
        for table in (
            "asset_files",
            "asset_objects",
            "asset_references",
            "catalog_heroes",
            "catalog_perks",
            "catalog_ability_kits",
            "catalog_gameplay_effects",
            "catalog_curve_tables",
            "catalog_hero_classes",
            "catalog_abilities",
            "catalog_ability_links",
            "catalog_hero_class_kits",
            "catalog_data_tables",
            "catalog_gameplay_tags",
            "catalog_magnitudes",
            "catalog_mechanics",
            "catalog_opaque_mechanics",
            "catalog_inheritance_edges",
        )
    }
    unresolved = unresolved_reference_report(connection, snapshot_id)
    normalization = connection.execute(
        """
        SELECT normalizer_version, status, completed_at
        FROM asset_normalization_runs
        WHERE snapshot_id=? ORDER BY id DESC LIMIT 1
        """,
        (snapshot_id,),
    ).fetchone()
    return {
        "snapshot_id": snapshot_id,
        "status": snapshot["status"],
        "idempotent": idempotent,
        "build_key": snapshot["build_key"],
        "game_version": snapshot["game_version"],
        "changelist": snapshot["changelist"],
        "manifest_sha256": snapshot["manifest_sha256"],
        "source_root": snapshot["source_root"],
        "normalization": dict(normalization) if normalization else None,
        "counts": counts,
        "unresolved_counts": unresolved["counts"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest a read-only FModel export directory")
    ingest.add_argument("source_root", type=Path)
    ingest.add_argument("--build-id", required=True)
    ingest.add_argument("--game-version")
    ingest.add_argument("--changelist")
    ingest.add_argument("--exporter-version")
    hero = commands.add_parser("hero", help="show a hero's normalized provenance chain")
    hero.add_argument("name")
    hero.add_argument("--snapshot-id", type=int)
    unresolved = commands.add_parser("unresolved", help="show unresolved asset references")
    unresolved.add_argument("--snapshot-id", type=int)
    queue = commands.add_parser(
        "queue", help="prioritize exact unresolved FModel export paths"
    )
    queue.add_argument("--snapshot-id", type=int)
    queue.add_argument("--hero")
    queue.add_argument("--all", action="store_true", dest="include_low_priority")
    queue.add_argument("--max-priority", type=int, choices=(0, 1, 2, 3, 4), default=2)
    queue.add_argument("--paths-only", action="store_true")
    coverage = commands.add_parser("coverage", help="show normalized catalog coverage")
    coverage.add_argument("--snapshot-id", type=int)
    roster = commands.add_parser(
        "roster", help="show canonical hero/perk roster and semantic readiness"
    )
    roster.add_argument("--snapshot-id", type=int)
    perk = commands.add_parser(
        "perk", help="show transitive semantic closure for one perk family"
    )
    perk.add_argument("family")
    perk.add_argument("--snapshot-id", type=int)
    batches = commands.add_parser(
        "batch-plan", help="show the controlled complete-roster FModel export plan"
    )
    batches.add_argument("--snapshot-id", type=int)
    receipt = commands.add_parser(
        "roster-receipt",
        help="record completion of every recursive folder in the roster batch plan",
    )
    receipt.add_argument("--snapshot-id", type=int)
    receipt.add_argument(
        "--confirm-complete-recursive-export", action="store_true", required=True
    )
    commands.add_parser("snapshot", help="show the latest snapshot summary")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "ingest":
            payload = ingest_asset_directory(
                connection,
                args.source_root,
                build_key=args.build_id,
                game_version=args.game_version,
                changelist=args.changelist,
                exporter_version=args.exporter_version,
            )
        elif args.command == "hero":
            payload = hero_provenance(connection, args.name, args.snapshot_id)
            if payload is None:
                raise SystemExit(f"hero not found: {args.name}")
        elif args.command == "unresolved":
            payload = unresolved_reference_report(connection, args.snapshot_id)
        elif args.command == "queue":
            payload = asset_export_queue(
                connection,
                args.snapshot_id,
                hero_name=args.hero,
                include_low_priority=args.include_low_priority,
                max_priority=args.max_priority,
            )
            if args.paths_only:
                paths = [asset["package_path"] for asset in payload["assets"]]
                print("\n".join(paths))
                return 0
        elif args.command == "coverage":
            payload = catalog_coverage(connection, args.snapshot_id)
        elif args.command == "roster":
            payload = roster_coverage_report(connection, args.snapshot_id)
        elif args.command == "perk":
            payload = perk_family_semantic_report(
                connection, args.family, args.snapshot_id
            )
        elif args.command == "batch-plan":
            payload = full_roster_batch_plan(connection, args.snapshot_id)
        elif args.command == "roster-receipt":
            payload = record_roster_export_receipt(
                connection,
                args.snapshot_id,
                confirm_complete_recursive_export=args.confirm_complete_recursive_export,
            )
        else:
            snapshot_id = latest_asset_snapshot_id(connection)
            payload = (
                _snapshot_summary(connection, snapshot_id, idempotent=True)
                if snapshot_id is not None
                else None
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
