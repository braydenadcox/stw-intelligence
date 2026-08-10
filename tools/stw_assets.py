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
            return _snapshot_summary(connection, existing["id"], idempotent=True)
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
                    object_key = f"{package or source.relative_path}::{name}"
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
            _normalize_snapshot(connection, snapshot_id, object_payloads)
            connection.execute(
                "UPDATE asset_snapshots SET status='ready', error_text=NULL WHERE id=?",
                (snapshot_id,),
            )
    except Exception as error:
        with connection:
            connection.execute(
                "UPDATE asset_snapshots SET status='failed', error_text=? WHERE id=?",
                (str(error), snapshot_id),
            )
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
    _normalize_effects(connection, snapshot_id, payloads)
    _normalize_ability_kits(connection, snapshot_id, payloads)
    _normalize_heroes(connection, snapshot_id, payloads)
    _link_modifier_curves(connection, snapshot_id)


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


def _normalize_effects(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    for object_id, export in payloads.items():
        properties = export.get("Properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("Modifiers"), list):
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
        for ordinal, modifier in enumerate(properties["Modifiers"]):
            if not isinstance(modifier, dict):
                continue
            magnitude = modifier.get("ModifierMagnitude") or {}
            scalable = magnitude.get("ScalableFloatMagnitude") or {}
            curve = scalable.get("Curve") or {}
            curve_table_path = _object_path(curve.get("CurveTable"))
            magnitude_kind = magnitude.get("MagnitudeCalculationType")
            operation = modifier.get("ModifierOp")
            supported = bool(operation and magnitude_kind and (curve_table_path or "Value" in scalable))
            connection.execute(
                """
                INSERT INTO catalog_effect_modifiers(
                    gameplay_effect_id, modifier_ordinal, attribute_name,
                    modifier_operation, magnitude_kind, literal_value,
                    curve_table_path, curve_row_name,
                    source_required_tags_json, source_ignored_tags_json,
                    target_required_tags_json, target_ignored_tags_json,
                    interpretation_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )


def _json_tags(modifier: dict[str, Any], group: str, key: str) -> str:
    tags = (modifier.get(group) or {}).get(key) or []
    return json.dumps(tags if isinstance(tags, list) else [], separators=(",", ":"))


def _is_ability_kit(export: dict[str, Any]) -> bool:
    return "AbilityKit" in str(export.get("Type")) or str(export.get("Name", "")).startswith("Kit_")


def _normalize_ability_kits(
    connection: sqlite3.Connection,
    snapshot_id: int,
    payloads: dict[int, dict[str, Any]],
) -> None:
    for object_id, export in payloads.items():
        if not _is_ability_kit(export):
            continue
        object_row = connection.execute(
            "SELECT package_path, object_name FROM asset_objects WHERE id=?", (object_id,)
        ).fetchone()
        kit_id = connection.execute(
            """
            INSERT INTO catalog_ability_kits(
                snapshot_id, source_object_id, package_path, kit_name
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, object_id, object_row["package_path"], object_row["object_name"]),
        ).lastrowid
        references = connection.execute(
            """
            SELECT ar.*, ge.id AS gameplay_effect_id
            FROM asset_references ar
            LEFT JOIN catalog_gameplay_effects ge ON ge.source_object_id=ar.target_object_id
            WHERE ar.source_object_id=?
            """,
            (object_id,),
        ).fetchall()
        for reference in references:
            target_name = reference["target_package_path"].rsplit("/", 1)[-1]
            path_lower = reference["property_path"].lower()
            if reference["gameplay_effect_id"] is not None or target_name.startswith("GE_"):
                kind = "gameplay_effect"
            elif "abilit" in path_lower or target_name.startswith(("GA_", "Ability_")):
                kind = "ability"
            else:
                kind = "reference"
            connection.execute(
                """
                INSERT INTO catalog_ability_kit_grants(
                    ability_kit_id, source_reference_id, grant_kind,
                    target_path, gameplay_effect_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kit_id,
                    reference["id"],
                    kind,
                    reference["target_path"],
                    reference["gameplay_effect_id"],
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
        class_object = (properties.get("HeroClassGameplayDefinition") or {}).get("ObjectName", "")
        hero_class = re.sub(r".*HCGD_", "", class_object).split("'")[0] or None
        hero_id = connection.execute(
            """
            INSERT INTO catalog_heroes(
                snapshot_id, source_object_id, hero_key, display_name,
                hero_class, statline_tags_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                object_id,
                export.get("Name", package.rsplit("/", 1)[-1]),
                export.get("Name", "Unknown hero"),
                hero_class,
                json.dumps(properties.get("HeroBaseStatlineTags") or [], separators=(",", ":")),
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
            kit_package = canonical_package_path(path)
            kit_name = kit_package.rsplit("/", 1)[-1] if kit_package else ""
            parsed = PERK_KIT_RE.match(kit_name)
            if not path or parsed is None:
                continue
            family, tier = parsed.group("family"), parsed.group("tier")
            kit_id = _kit_id_for_path(connection, snapshot_id, path)
            connection.execute(
                """
                INSERT INTO catalog_perks(
                    snapshot_id, perk_family, perk_tier, ability_kit_path, ability_kit_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, perk_family, perk_tier) DO UPDATE SET
                    ability_kit_path=excluded.ability_kit_path,
                    ability_kit_id=excluded.ability_kit_id
                """,
                (snapshot_id, family, tier, path, kit_id),
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
    abilities = connection.execute(
        """
        SELECT ability_ordinal, ability_kit_path, minimum_rarity,
               CASE WHEN ability_kit_id IS NULL THEN 'unresolved' ELSE 'resolved' END status
        FROM catalog_hero_abilities WHERE hero_id=? ORDER BY ability_ordinal
        """,
        (hero["id"],),
    ).fetchall()
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
               p.ability_kit_path, p.ability_kit_id
        FROM catalog_hero_perks hp
        JOIN catalog_perks p ON p.id=hp.perk_id
        WHERE hp.hero_id=? ORDER BY hp.perk_mode DESC
        """,
        (hero["id"],),
    ).fetchall()
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
        "abilities": [dict(row) for row in abilities],
        "perks": [_perk_provenance(connection, snapshot_id, row) for row in perks],
        "unresolved": unresolved_reference_report(
            connection, snapshot_id=snapshot_id, source_prefix=hero["package_path"].rsplit("/", 2)[0]
        ),
    }


def _perk_provenance(
    connection: sqlite3.Connection, snapshot_id: int, perk: sqlite3.Row
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": perk["perk_mode"],
        "family": perk["perk_family"],
        "tier": perk["perk_tier"],
        "ability_kit_path": perk["ability_kit_path"],
        "status": "unresolved_ability_kit" if perk["ability_kit_id"] is None else "resolved",
        "effects": [],
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
        modifiers = connection.execute(
            """
            SELECT ge.effect_name, ge.template_path, ge.stacking_type, ge.stack_limit,
                   em.attribute_name, em.modifier_operation, em.magnitude_kind,
                   em.curve_row_name, em.interpretation_status,
                   cp.output_value, cp.time_value,
                   af.source_path, af.content_sha256,
                   ctf.source_path AS curve_source_path,
                   ctf.content_sha256 AS curve_content_sha256
            FROM catalog_ability_kit_grants akg
            JOIN catalog_gameplay_effects ge ON ge.id=akg.gameplay_effect_id
            JOIN catalog_effect_modifiers em ON em.gameplay_effect_id=ge.id
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
            value = modifier["output_value"]
            percent_bonus = None
            if value is not None and operation.endswith("::Multiplicitive"):
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
                    "template_path": modifier["template_path"],
                    "source": {
                        "effect_path": modifier["source_path"],
                        "effect_sha256": modifier["content_sha256"],
                        "curve_path": modifier["curve_source_path"],
                        "curve_sha256": modifier["curve_content_sha256"],
                    },
                }
            )
        if not result["effects"]:
            result["status"] = "resolved_kit_without_supported_effect"
    expected_row = f"Perk.{perk['perk_family']}.{perk['perk_tier']}.DamageMult"
    raw_balance = connection.execute(
        """
        SELECT cp.output_value, cr.row_name, af.source_path, af.content_sha256
        FROM catalog_curve_rows cr
        JOIN catalog_curve_tables ct ON ct.id=cr.curve_table_id
        JOIN catalog_curve_points cp ON cp.curve_row_id=cr.id
        JOIN asset_objects ao ON ao.id=ct.source_object_id
        JOIN asset_files af ON af.id=ao.asset_file_id
        WHERE ct.snapshot_id=? AND cr.row_name=?
        ORDER BY cp.point_ordinal LIMIT 1
        """,
        (snapshot_id, expected_row),
    ).fetchone()
    if raw_balance is not None:
        result["unlinked_balance_evidence"] = {
            "row": raw_balance["row_name"],
            "value": raw_balance["output_value"],
            "source_path": raw_balance["source_path"],
            "source_sha256": raw_balance["content_sha256"],
            "note": "not interpreted unless a resolved gameplay-effect modifier links this row",
        }
    return result


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
        )
    }
    unresolved = unresolved_reference_report(connection, snapshot_id)
    return {
        "snapshot_id": snapshot_id,
        "status": snapshot["status"],
        "idempotent": idempotent,
        "build_key": snapshot["build_key"],
        "game_version": snapshot["game_version"],
        "changelist": snapshot["changelist"],
        "manifest_sha256": snapshot["manifest_sha256"],
        "source_root": snapshot["source_root"],
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
