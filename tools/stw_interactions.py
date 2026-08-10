#!/usr/bin/env python3
"""Auditable reports and dependency closure for shared STW interactions."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Iterable

from stw_assets import (
    _queue_classification,
    canonical_package_path,
    latest_asset_snapshot_id,
)
from stw_pipeline import connect


TEAM_PERK_ROOT = "/SaveTheWorld/Abilities/Player/Perks/Leader/"


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _source(connection, object_id: int) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT object.package_path, object.object_name, object.object_type,
               file.relative_path, file.content_sha256
        FROM asset_objects object
        JOIN asset_files file ON file.id=object.asset_file_id
        WHERE object.id=?
        """,
        (object_id,),
    ).fetchone()
    return dict(row) if row else {"object_id": object_id}


def _semantic_graph_index(connection, snapshot_id: int) -> dict[str, Any]:
    objects: dict[int, tuple[str, str]] = {}
    packages: dict[str, list[int]] = {}
    for row in connection.execute(
        "SELECT id, package_path, object_type FROM asset_objects WHERE snapshot_id=?",
        (snapshot_id,),
    ):
        objects[row["id"]] = (row["package_path"], row["object_type"])
        packages.setdefault(row["package_path"], []).append(row["id"])
    references: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute(
        """
        SELECT source_object_id, property_path, target_path, target_package_path,
               target_object_id, resolution_status
        FROM asset_references WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ):
        source = objects.get(row["source_object_id"])
        if source is None:
            continue
        priority, category, reason = _queue_classification(
            source[1], row["property_path"], row["target_package_path"]
        )
        if priority <= 2:
            references.setdefault(row["source_object_id"], []).append(
                {**dict(row), "priority": priority, "category": category, "reason": reason}
            )
    return {"objects": objects, "packages": packages, "references": references}


def _semantic_closure(
    connection,
    snapshot_id: int,
    roots: Iterable[int],
    graph_index: dict[str, Any] | None = None,
) -> tuple[set[int], list[dict[str, Any]]]:
    closure = set(roots)
    frontier = list(closure)
    expanded_packages: set[str] = set()
    missing: dict[str, dict[str, Any]] = {}
    while frontier:
        source_id = frontier.pop()
        indexed_source = graph_index["objects"].get(source_id) if graph_index else None
        source = (
            {"package_path": indexed_source[0], "object_type": indexed_source[1]}
            if indexed_source
            else connection.execute(
                "SELECT package_path, object_type FROM asset_objects WHERE id=?",
                (source_id,),
            ).fetchone()
        )
        if source is None:
            continue
        if source["package_path"] not in expanded_packages:
            expanded_packages.add(source["package_path"])
            siblings = (
                graph_index["packages"].get(source["package_path"], [])
                if graph_index
                else [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM asset_objects WHERE snapshot_id=? AND package_path=?",
                        (snapshot_id, source["package_path"]),
                    )
                ]
            )
            for sibling_id in siblings:
                if sibling_id not in closure:
                    closure.add(sibling_id)
                    frontier.append(sibling_id)
        rows = (
            graph_index["references"].get(source_id, [])
            if graph_index
            else connection.execute(
                """
                SELECT property_path, target_path, target_package_path,
                       target_object_id, resolution_status
                FROM asset_references WHERE source_object_id=?
                """,
                (source_id,),
            )
        )
        for row in rows:
            if graph_index:
                priority, category, reason = row["priority"], row["category"], row["reason"]
            else:
                priority, category, reason = _queue_classification(
                    source["object_type"], row["property_path"], row["target_package_path"]
                )
            if priority > 2:
                continue
            if row["resolution_status"] == "resolved" and row["target_object_id"] is not None:
                if row["target_object_id"] not in closure:
                    closure.add(row["target_object_id"])
                    frontier.append(row["target_object_id"])
                continue
            item = missing.setdefault(
                row["target_package_path"],
                {
                    "package_path": row["target_package_path"],
                    "priority": priority,
                    "categories": set(),
                    "reasons": set(),
                    "source_packages": set(),
                },
            )
            item["priority"] = min(item["priority"], priority)
            item["categories"].add(category)
            item["reasons"].add(reason)
            item["source_packages"].add(source["package_path"])
    queue = [
        {
            **item,
            "categories": sorted(item["categories"]),
            "reasons": sorted(item["reasons"]),
            "source_packages": sorted(item["source_packages"]),
        }
        for item in missing.values()
    ]
    queue.sort(key=lambda item: (item["priority"], item["package_path"]))
    return closure, queue


def _team_perk_row(connection, snapshot_id: int, name: str):
    rows = connection.execute(
        """
        SELECT * FROM catalog_team_perks
        WHERE snapshot_id=? AND
          (lower(team_perk_key)=lower(?) OR lower(display_name)=lower(?))
        """,
        (snapshot_id, name, name),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"team perk must resolve exactly once: {name!r}")
    return rows[0]


def team_perk_report(
    connection,
    name: str,
    snapshot_id: int | None = None,
    *,
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    perk = _team_perk_row(connection, snapshot_id, name)
    roots = {perk["source_object_id"]}
    if perk["ability_kit_id"] is not None:
        roots.add(
            connection.execute(
                "SELECT source_object_id FROM catalog_ability_kits WHERE id=?",
                (perk["ability_kit_id"],),
            ).fetchone()[0]
        )
    closure, dependencies = _semantic_closure(
        connection, snapshot_id, roots, graph_index
    )
    rules = []
    for row in connection.execute(
        """
        SELECT * FROM catalog_team_perk_eligibility_rules
        WHERE team_perk_id=? ORDER BY rule_ordinal
        """,
        (perk["id"],),
    ):
        rules.append(
            {
                "rule_ordinal": row["rule_ordinal"],
                "required_count": row["required_count"],
                "query_expression": row["query_expression"],
                "required_tags": _json(row["required_tags_json"], []),
                "required_class_tags": _json(row["required_class_tags_json"], []),
                "required_keyword_tags": _json(row["required_keyword_tags_json"], []),
                "tier": {
                    "minimum": row["minimum_hero_tier"] if row["consider_minimum_tier"] else None,
                    "maximum": row["maximum_hero_tier"] if row["consider_maximum_tier"] else None,
                },
                "level": {
                    "minimum": row["minimum_hero_level"] if row["consider_minimum_level"] else None,
                    "maximum": row["maximum_hero_level"] if row["consider_maximum_level"] else None,
                },
                "rarity": {
                    "minimum": row["minimum_hero_rarity"] if row["consider_minimum_rarity"] else None,
                    "maximum": row["maximum_hero_rarity"] if row["consider_maximum_rarity"] else None,
                },
                "status": row["interpretation_status"],
                "raw_tag_query": _json(row["tag_query_json"], {}),
            }
        )
    grants = []
    if perk["ability_kit_id"] is not None:
        for row in connection.execute(
            """
            SELECT grant.grant_kind, grant.grant_operation, grant.target_path,
                   grant.grant_level, effect.effect_name, effect.source_object_id AS effect_object_id,
                   ability.ability_key, ability.semantic_status AS ability_status,
                   ability.source_object_id AS ability_object_id
            FROM catalog_ability_kit_grants grant
            LEFT JOIN catalog_gameplay_effects effect ON effect.id=grant.gameplay_effect_id
            LEFT JOIN catalog_abilities ability ON ability.id=grant.ability_id
            WHERE grant.ability_kit_id=? AND grant.grant_kind IN ('ability', 'gameplay_effect')
            ORDER BY grant.id
            """,
            (perk["ability_kit_id"],),
        ):
            object_id = row["effect_object_id"] or row["ability_object_id"]
            grants.append(
                {
                    "kind": row["grant_kind"],
                    "operation": row["grant_operation"],
                    "target_path": row["target_path"],
                    "grant_level": row["grant_level"],
                    "name": row["effect_name"] or row["ability_key"],
                    "semantic_status": row["ability_status"] if row["grant_kind"] == "ability" else None,
                    "source": _source(connection, object_id) if object_id else None,
                }
            )
    placeholders = ",".join("?" for _ in closure)
    mechanics = []
    modifiers = []
    opaque = []
    if closure:
        mechanics = [
            {
                **dict(row),
                "conditions": _json(row["conditions_json"], {}),
                "value": _json(row["value_json"], {}),
            }
            for row in connection.execute(
                f"""
                SELECT mechanic.mechanic_type, mechanic.property_path,
                       mechanic.conditions_json, mechanic.value_json,
                       mechanic.interpretation_status,
                       magnitude.calculation_type AS magnitude_calculation_type,
                       magnitude.literal_value AS magnitude_literal_value,
                       magnitude.coefficient AS magnitude_coefficient,
                       magnitude.curve_table_path AS magnitude_curve_table_path,
                       magnitude.curve_row_name AS magnitude_curve_row_name,
                       magnitude.set_by_caller_tag AS magnitude_set_by_caller_tag,
                       magnitude.interpretation_status AS magnitude_status
                FROM catalog_mechanics mechanic
                LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
                WHERE mechanic.source_object_id IN ({placeholders})
                ORDER BY mechanic.source_object_id, mechanic.property_path
                """,
                sorted(closure),
            )
        ]
        modifiers = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT effect.effect_name, modifier.attribute_name,
                       modifier.modifier_operation, modifier.interpretation_status,
                       magnitude.calculation_type, magnitude.literal_value,
                       magnitude.coefficient, magnitude.curve_table_path,
                       magnitude.curve_row_name, magnitude.set_by_caller_tag
                       , magnitude.interpretation_status AS magnitude_status
                FROM catalog_effect_modifiers modifier
                JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
                LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=modifier.magnitude_id
                WHERE effect.source_object_id IN ({placeholders})
                ORDER BY effect.effect_name, modifier.modifier_ordinal
                """,
                sorted(closure),
            )
        ]
        opaque = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT property_path, mechanic_kind, referenced_path, reason
                FROM catalog_opaque_mechanics
                WHERE source_object_id IN ({placeholders})
                ORDER BY source_object_id, property_path
                """,
                sorted(closure),
            )
        ]
    supported_facts = sum(
        row["interpretation_status"] == "supported"
        and row["magnitude_status"] in (None, "supported")
        for row in mechanics
    ) + sum(row["interpretation_status"] == "supported" for row in modifiers)
    incomplete = bool(dependencies or opaque) or any(
        row["interpretation_status"] != "supported"
        or ("magnitude_status" in row and row["magnitude_status"] not in (None, "supported"))
        for row in mechanics + modifiers
    ) or any(
        grant["kind"] == "ability" and grant["semantic_status"] != "supported"
        for grant in grants
    )
    effective_status = (
        "partial" if supported_facts and incomplete
        else "supported" if supported_facts
        else "opaque"
    )
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "team_perk_key": perk["team_perk_key"],
            "display_name": perk["display_name"],
            "item_description": perk["item_description"],
            "effect_description": perk["effect_description"],
            "requirement_description": perk["requirement_description"],
            "progressive_bonus": bool(perk["progressive_bonus"]),
            "traits": _json(perk["traits_json"], []),
            "source": _source(connection, perk["source_object_id"]),
        },
        "eligibility": {
            "status": perk["eligibility_status"],
            "required_support_slots": sum(rule["required_count"] for rule in rules),
            "rules": rules,
        },
        "semantics": {
            "status": effective_status,
            "normalized_direct_status": perk["semantic_status"],
            "ability_kit_path": perk["ability_kit_path"],
            "grants": grants,
            "mechanics": mechanics,
            "modifiers": modifiers,
            "opaque_boundaries": opaque,
            "transitive_asset_objects": len(closure),
        },
        "unresolved_dependencies": dependencies,
    }


def team_perk_coverage(connection, snapshot_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "team_perks": []}
    names = [
        row[0]
        for row in connection.execute(
            "SELECT team_perk_key FROM catalog_team_perks WHERE snapshot_id=? ORDER BY display_name",
            (snapshot_id,),
        )
    ]
    graph_index = _semantic_graph_index(connection, snapshot_id)
    reports = [
        team_perk_report(connection, name, snapshot_id, graph_index=graph_index)
        for name in names
    ]
    eligibility = {status: 0 for status in ("supported", "partial", "opaque")}
    semantics = {status: 0 for status in ("supported", "partial", "opaque")}
    mechanic_types: dict[str, int] = {}
    missing: dict[str, dict[str, Any]] = {}
    fact_status = {status: 0 for status in ("supported", "partial", "opaque")}
    grant_count = resolved_grant_count = removed_grant_count = 0
    for report in reports:
        eligibility[report["eligibility"]["status"]] += 1
        semantics[report["semantics"]["status"]] += 1
        for mechanic in report["semantics"]["mechanics"]:
            key = mechanic["mechanic_type"]
            mechanic_types[key] = mechanic_types.get(key, 0) + 1
            status = mechanic["interpretation_status"]
            if mechanic["magnitude_status"] in ("partial", "opaque"):
                status = mechanic["magnitude_status"]
            fact_status[status] += 1
        for modifier in report["semantics"]["modifiers"]:
            status = modifier["interpretation_status"]
            if modifier["magnitude_status"] in ("partial", "opaque"):
                status = modifier["magnitude_status"]
            fact_status["partial" if status == "unsupported" else status] += 1
        for boundary in report["semantics"]["opaque_boundaries"]:
            fact_status["opaque"] += 1
        grant_count += len(report["semantics"]["grants"])
        resolved_grant_count += sum(
            grant["source"] is not None for grant in report["semantics"]["grants"]
        )
        removed_grant_count += sum(
            grant["operation"] == "removed" for grant in report["semantics"]["grants"]
        )
        for dependency in report["unresolved_dependencies"]:
            missing.setdefault(dependency["package_path"], dependency)
    total = len(reports)
    return {
        "snapshot_id": snapshot_id,
        "catalog_scope": {
            "source_type": "FortTeamPerkItemDefinition",
            "package_index_identity_count": total,
            "catalog_awareness_complete": total > 0,
        },
        "counts": {
            "team_perks": total,
            "eligibility_status": eligibility,
            "semantic_status": semantics,
            "resolved_ability_kits": sum(bool(r["semantics"]["ability_kit_path"]) for r in reports),
            "interaction_mechanic_types": mechanic_types,
            "interaction_fact_status_occurrences": fact_status,
            "semantic_grants": grant_count,
            "resolved_semantic_grants": resolved_grant_count,
            "removed_gameplay_effect_grants": removed_grant_count,
            "deduplicated_missing_dependencies": len(missing),
        },
        "coverage": {
            "identity": 1.0 if total else None,
            "eligibility_supported": eligibility["supported"] / total if total else None,
            "semantics_fully_supported": semantics["supported"] / total if total else None,
            "semantics_known_or_partial": (semantics["supported"] + semantics["partial"]) / total if total else None,
            "semantic_grants_resolved": resolved_grant_count / grant_count if grant_count else None,
            "supported_interaction_fact_occurrences": (
                fact_status["supported"] / sum(fact_status.values())
                if sum(fact_status.values()) else None
            ),
        },
        "team_perks": [
            {
                "team_perk_key": report["identity"]["team_perk_key"],
                "display_name": report["identity"]["display_name"],
                "eligibility_status": report["eligibility"]["status"],
                "required_support_slots": report["eligibility"]["required_support_slots"],
                "semantic_status": report["semantics"]["status"],
                "unresolved_dependency_count": len(report["unresolved_dependencies"]),
            }
            for report in reports
        ],
        "unresolved_dependencies": sorted(missing.values(), key=lambda item: (item["priority"], item["package_path"])),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _ability_kit_interaction_semantics(
    connection,
    snapshot_id: int,
    ability_kit_id: int,
    root_object_ids: set[int],
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report shared AbilityKit/Ability/Effect semantics for any interaction owner."""
    kit = connection.execute(
        "SELECT source_object_id FROM catalog_ability_kits WHERE id=?",
        (ability_kit_id,),
    ).fetchone()
    roots = set(root_object_ids)
    if kit:
        roots.add(kit["source_object_id"])
    closure, dependencies = _semantic_closure(
        connection, snapshot_id, roots, graph_index
    )
    grants = [
        dict(row)
        for row in connection.execute(
            """
            SELECT grant_kind, grant_operation, target_path, grant_level,
                   ability_id, gameplay_effect_id,
                   CASE WHEN ability_id IS NOT NULL OR gameplay_effect_id IS NOT NULL
                        THEN 1 ELSE 0 END AS resolved
            FROM catalog_ability_kit_grants
            WHERE ability_kit_id=? ORDER BY id
            """,
            (ability_kit_id,),
        )
    ]
    mechanics: list[dict[str, Any]] = []
    modifiers: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    opaque: list[dict[str, Any]] = []
    if closure:
        placeholders = ",".join("?" for _ in closure)
        params = sorted(closure)
        mechanics = [
            {
                **dict(row),
                "conditions": _json(row["conditions_json"], {}),
                "value": _json(row["value_json"], {}),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT mechanic.source_object_id, object.package_path,
                       object.object_name, object.object_type,
                       mechanic.owner_domain, mechanic.mechanic_type,
                       mechanic.property_path, mechanic.conditions_json,
                       mechanic.value_json, mechanic.interpretation_status,
                       magnitude.calculation_type AS magnitude_calculation_type,
                       magnitude.literal_value AS magnitude_literal_value,
                       magnitude.coefficient AS magnitude_coefficient,
                       magnitude.pre_additive AS magnitude_pre_additive,
                       magnitude.post_additive AS magnitude_post_additive,
                       magnitude.curve_table_path AS magnitude_curve_table_path,
                       magnitude.curve_row_name AS magnitude_curve_row_name,
                       magnitude.custom_calculation_path AS magnitude_custom_calculation_path,
                       magnitude.set_by_caller_tag AS magnitude_set_by_caller_tag,
                       magnitude.interpretation_status AS magnitude_status
                FROM catalog_mechanics mechanic
                JOIN asset_objects object ON object.id=mechanic.source_object_id
                LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
                WHERE mechanic.source_object_id IN ({placeholders})
                ORDER BY object.package_path, mechanic.property_path
                """,
                params,
            )
        ]
        modifiers = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT effect.source_object_id, effect.effect_name,
                       modifier.attribute_name, modifier.modifier_operation,
                       modifier.evaluation_channel,
                       modifier.source_required_tags_json,
                       modifier.source_ignored_tags_json,
                       modifier.target_required_tags_json,
                       modifier.target_ignored_tags_json,
                       modifier.interpretation_status,
                       magnitude.calculation_type, magnitude.literal_value,
                       magnitude.coefficient, magnitude.pre_additive,
                       magnitude.post_additive, magnitude.curve_table_path,
                       magnitude.curve_row_name, magnitude.custom_calculation_path,
                       magnitude.set_by_caller_tag,
                       magnitude.interpretation_status AS magnitude_status
                FROM catalog_effect_modifiers modifier
                JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
                LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=modifier.magnitude_id
                WHERE effect.source_object_id IN ({placeholders})
                ORDER BY effect.effect_name, modifier.modifier_ordinal
                """,
                params,
            )
        ]
        effects = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT source_object_id, effect_name, package_path, template_path,
                       stacking_type, stack_limit
                FROM catalog_gameplay_effects
                WHERE source_object_id IN ({placeholders})
                ORDER BY package_path, effect_name
                """,
                params,
            )
        ]
        tags = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT occurrence.source_object_id, tag.tag_name,
                       occurrence.property_path, occurrence.semantic_role
                FROM catalog_gameplay_tag_occurrences occurrence
                JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
                WHERE occurrence.source_object_id IN ({placeholders})
                ORDER BY tag.tag_name, occurrence.property_path
                """,
                params,
            )
        ]
        opaque = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT source_object_id, property_path, mechanic_kind,
                       referenced_path, reason
                FROM catalog_opaque_mechanics
                WHERE source_object_id IN ({placeholders})
                ORDER BY source_object_id, property_path
                """,
                params,
            )
        ]
    supported_facts = sum(
        item["interpretation_status"] == "supported"
        and item["magnitude_status"] in (None, "supported")
        for item in mechanics + modifiers
    )
    incomplete = bool(dependencies or opaque) or any(
        item["interpretation_status"] != "supported"
        or item["magnitude_status"] not in (None, "supported")
        for item in mechanics + modifiers
    ) or any(not item["resolved"] for item in grants)
    status = "partial" if supported_facts and incomplete else (
        "supported" if supported_facts else "opaque"
    )
    return {
        "status": status,
        "grants": grants,
        "gameplay_effects": effects,
        "mechanics": mechanics,
        "modifiers": modifiers,
        "gameplay_tags": tags,
        "opaque_boundaries": opaque,
        "transitive_asset_objects": len(closure),
        "unresolved_dependencies": dependencies,
    }


def _gadget_row(connection, snapshot_id: int, name: str):
    rows = connection.execute(
        """
        SELECT * FROM catalog_gadgets
        WHERE snapshot_id=? AND
          (lower(gadget_key)=lower(?) OR lower(display_name)=lower(?))
        ORDER BY id
        """,
        (snapshot_id, name, name),
    ).fetchall()
    if len(rows) != 1:
        matches = [row["gadget_key"] for row in rows]
        suffix = f"; matching keys: {matches}" if matches else ""
        raise ValueError(f"gadget must resolve exactly once: {name!r}{suffix}")
    return rows[0]


def gadget_report(
    connection,
    name: str,
    snapshot_id: int | None = None,
    *,
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    gadget = _gadget_row(connection, snapshot_id, name)
    levels = []
    for row in connection.execute(
        "SELECT * FROM catalog_gadget_levels WHERE gadget_id=? ORDER BY level_ordinal",
        (gadget["id"],),
    ):
        item = dict(row)
        item["gameplay_effect_rows"] = _json(item.pop("gameplay_effect_rows_json"), [])
        item["cost"] = _json(item.pop("cost_json"), [])
        item["unlock_facts"] = _json(item.pop("unlock_facts_json"), {})
        levels.append(item)
    semantics = _ability_kit_interaction_semantics(
        connection,
        snapshot_id,
        gadget["ability_kit_id"],
        {gadget["source_object_id"]},
        graph_index,
    )
    unresolved_upgrade_rows = sorted(
        {value for level in levels for value in level["gameplay_effect_rows"]}
    )
    if unresolved_upgrade_rows and semantics["status"] == "supported":
        semantics["status"] = "partial"
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "gadget_key": gadget["gadget_key"],
            "display_name": gadget["display_name"],
            "package_path": gadget["package_path"],
            "source": _source(connection, gadget["source_object_id"]),
        },
        "levels": levels,
        "semantics": {
            **semantics,
            "normalized_direct_status": gadget["semantic_status"],
            "unresolved_upgrade_effect_rows": unresolved_upgrade_rows,
        },
        "unresolved_dependencies": semantics["unresolved_dependencies"],
    }


def gadget_coverage(connection, snapshot_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "gadgets": []}
    graph_index = _semantic_graph_index(connection, snapshot_id)
    keys = [
        row[0]
        for row in connection.execute(
            "SELECT gadget_key FROM catalog_gadgets WHERE snapshot_id=? ORDER BY display_name",
            (snapshot_id,),
        )
    ]
    reports = [
        gadget_report(connection, key, snapshot_id, graph_index=graph_index)
        for key in keys
    ]
    status_counts = {status: 0 for status in ("supported", "partial", "opaque")}
    fact_counts = {status: 0 for status in ("supported", "partial", "opaque")}
    mechanic_types: dict[str, int] = {}
    missing: dict[str, dict[str, Any]] = {}
    level_total = level_supported = 0
    opaque_boundaries = unresolved_upgrade_rows = 0
    for report in reports:
        status_counts[report["semantics"]["status"]] += 1
        for level in report["levels"]:
            level_total += 1
            level_supported += int(level["interpretation_status"] == "supported")
            unresolved_upgrade_rows += len(level["gameplay_effect_rows"])
        opaque_boundaries += len(report["semantics"]["opaque_boundaries"])
        for item in report["semantics"]["mechanics"] + report["semantics"]["modifiers"]:
            status = item["interpretation_status"]
            if item["magnitude_status"] in ("partial", "opaque"):
                status = item["magnitude_status"]
            fact_counts[status] += 1
            mechanic = item.get("mechanic_type") or "effect_modifier"
            mechanic_types[mechanic] = mechanic_types.get(mechanic, 0) + 1
        for dependency in report["unresolved_dependencies"]:
            current = missing.get(dependency["package_path"])
            if current is None or dependency["priority"] < current["priority"]:
                missing[dependency["package_path"]] = dependency
    total = len(reports)
    complete = status_counts["supported"]
    known = complete + status_counts["partial"]
    fact_total = sum(fact_counts.values())
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "gadget_identities": total,
            "levels": level_total,
            "supported_levels": level_supported,
            "supported_gadgets": complete,
            "partial_gadgets": status_counts["partial"],
            "opaque_gadgets": status_counts["opaque"],
            "supported_facts": fact_counts["supported"],
            "partial_facts": fact_counts["partial"],
            "opaque_facts": fact_counts["opaque"],
            "opaque_boundaries": opaque_boundaries,
            "unresolved_upgrade_effect_rows": unresolved_upgrade_rows,
            "unresolved_dependencies": len(missing),
        },
        "ratios": {
            "structural_coverage": known / total if total else None,
            "semantic_coverage": complete / total if total else None,
            "semantic_known_or_partial": known / total if total else None,
            "supported_interaction_fact_coverage": (
                fact_counts["supported"] / fact_total if fact_total else None
            ),
            "level_interpretation_coverage": level_supported / level_total if level_total else None,
        },
        "mechanic_types": dict(sorted(mechanic_types.items())),
        "gadgets": [
            {
                "gadget_key": report["identity"]["gadget_key"],
                "display_name": report["identity"]["display_name"],
                "semantic_status": report["semantics"]["status"],
                "level_count": len(report["levels"]),
                "unresolved_dependency_count": len(report["unresolved_dependencies"]),
                "opaque_boundary_count": len(report["semantics"]["opaque_boundaries"]),
            }
            for report in reports
        ],
        "unresolved_dependencies": sorted(
            missing.values(), key=lambda item: (item["priority"], item["package_path"])
        ),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _active_ability_row(connection, snapshot_id: int, name: str):
    rows = connection.execute(
        """
        SELECT * FROM catalog_active_abilities
        WHERE snapshot_id=? AND
          (lower(active_ability_key)=lower(?) OR lower(display_name)=lower(?))
        ORDER BY id
        """,
        (snapshot_id, name, name),
    ).fetchall()
    if len(rows) != 1:
        matches = [row["active_ability_key"] for row in rows]
        suffix = f"; matching keys: {matches}" if matches else ""
        raise ValueError(f"active ability must resolve exactly once: {name!r}{suffix}")
    return rows[0]


def _resolved_data_row(connection, snapshot_id: int, handle: Any) -> dict[str, Any] | None:
    if not isinstance(handle, dict):
        return None
    table_reference = handle.get("DataTable") or {}
    table_path = canonical_package_path(
        table_reference.get("ObjectPath") or table_reference.get("AssetPathName")
    )
    row_name = handle.get("RowName")
    if not table_path or not row_name:
        return None
    row = connection.execute(
        """
        SELECT data_row.row_name, data_row.row_json,
               data_table.source_object_id, data_table.package_path,
               data_table.table_name
        FROM catalog_data_rows data_row
        JOIN catalog_data_tables data_table ON data_table.id=data_row.data_table_id
        WHERE data_table.snapshot_id=? AND data_table.package_path=?
          AND data_row.row_name=?
        """,
        (snapshot_id, table_path, row_name),
    ).fetchone()
    if row is None:
        return {
            "status": "unresolved",
            "table_path": table_path,
            "row_name": row_name,
        }
    return {
        "status": "resolved",
        "table_path": row["package_path"],
        "table_name": row["table_name"],
        "row_name": row["row_name"],
        "row": _json(row["row_json"], {}),
        "source": _source(connection, row["source_object_id"]),
    }


def active_ability_report(
    connection,
    name: str,
    snapshot_id: int | None = None,
    *,
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    active = _active_ability_row(connection, snapshot_id, name)
    closure, dependencies = _semantic_closure(
        connection,
        snapshot_id,
        {active["source_object_id"]},
        graph_index,
    )

    grantees = []
    for row in connection.execute(
        """
        SELECT grant.*, hero.display_name AS hero_name,
               hero.hero_class, hero.source_object_id AS hero_object_id,
               hero_class.display_name AS class_name,
               hero_class.source_object_id AS class_object_id,
               reference.property_path AS evidence_property_path,
               reference.target_path AS evidence_target_path,
               reference.resolution_status AS evidence_resolution_status
        FROM catalog_active_ability_grants grant
        LEFT JOIN catalog_heroes hero ON hero.id=grant.hero_id
        LEFT JOIN catalog_hero_classes hero_class ON hero_class.id=grant.hero_class_id
        LEFT JOIN asset_references reference ON reference.id=grant.source_reference_id
        WHERE grant.active_ability_id=?
        ORDER BY grant.grant_domain, grant.grantee_key, grant.grant_ordinal
        """,
        (active["id"],),
    ):
        object_id = row["hero_object_id"] or row["class_object_id"]
        grantees.append(
            {
                "domain": row["grant_domain"],
                "grantee_key": row["grantee_key"],
                "display_name": row["hero_name"] or row["class_name"],
                "hero_class": row["hero_class"],
                "ordinal": row["grant_ordinal"],
                "minimum_rarity": row["minimum_rarity"],
                "evidence": {
                    "property_path": row["evidence_property_path"],
                    "target_path": row["evidence_target_path"],
                    "resolution_status": row["evidence_resolution_status"],
                    "source": _source(connection, object_id) if object_id else None,
                },
            }
        )

    grants = []
    implementation_ids: set[int] = set()
    for row in connection.execute(
        """
        SELECT grant.grant_kind, grant.grant_operation, grant.target_path,
               grant.grant_level, grant.ability_id, grant.gameplay_effect_id,
               ability.ability_key, ability.display_name AS ability_name,
               ability.semantic_status AS ability_status,
               ability.source_object_id AS ability_object_id,
               effect.effect_name, effect.source_object_id AS effect_object_id
        FROM catalog_ability_kit_grants grant
        LEFT JOIN catalog_abilities ability ON ability.id=grant.ability_id
        LEFT JOIN catalog_gameplay_effects effect ON effect.id=grant.gameplay_effect_id
        WHERE grant.ability_kit_id=?
          AND grant.grant_kind IN ('ability', 'gameplay_effect')
        ORDER BY grant.id
        """,
        (active["ability_kit_id"],),
    ):
        object_id = row["ability_object_id"] or row["effect_object_id"]
        if row["ability_id"] is not None:
            implementation_ids.add(row["ability_id"])
        grants.append(
            {
                "kind": row["grant_kind"],
                "operation": row["grant_operation"],
                "target_path": row["target_path"],
                "grant_level": row["grant_level"],
                "name": row["ability_name"] or row["ability_key"] or row["effect_name"],
                "semantic_status": row["ability_status"],
                "resolved": object_id is not None,
                "source": _source(connection, object_id) if object_id else None,
            }
        )

    links = []
    frontier = list(implementation_ids)
    while frontier:
        source_id = frontier.pop()
        for row in connection.execute(
            """
            SELECT link.target_path, link.resolution_status,
                   source.ability_key AS source_key,
                   target.id AS target_id, target.ability_key AS target_key,
                   target.display_name, target.semantic_status,
                   target.source_object_id
            FROM catalog_ability_links link
            JOIN catalog_abilities source ON source.id=link.source_ability_id
            LEFT JOIN catalog_abilities target ON target.id=link.target_ability_id
            WHERE link.source_ability_id=? ORDER BY link.id
            """,
            (source_id,),
        ):
            if row["target_id"] is not None and row["target_id"] not in implementation_ids:
                implementation_ids.add(row["target_id"])
                frontier.append(row["target_id"])
            links.append(
                {
                    "source_key": row["source_key"],
                    "target_path": row["target_path"],
                    "target_key": row["target_key"],
                    "display_name": row["display_name"],
                    "resolution_status": row["resolution_status"],
                    "semantic_status": row["semantic_status"],
                    "source": (
                        _source(connection, row["source_object_id"])
                        if row["source_object_id"] else None
                    ),
                }
            )

    mechanics: list[dict[str, Any]] = []
    modifiers: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    opaque: list[dict[str, Any]] = []
    if closure:
        placeholders = ",".join("?" for _ in closure)
        for row in connection.execute(
            f"""
            SELECT mechanic.source_object_id, object.package_path,
                   object.object_name, object.object_type,
                   mechanic.owner_domain, mechanic.mechanic_type,
                   mechanic.property_path, mechanic.conditions_json,
                   mechanic.value_json, mechanic.interpretation_status,
                   magnitude.calculation_type AS magnitude_calculation_type,
                   magnitude.literal_value AS magnitude_literal_value,
                   magnitude.coefficient AS magnitude_coefficient,
                   magnitude.pre_additive AS magnitude_pre_additive,
                   magnitude.post_additive AS magnitude_post_additive,
                   magnitude.curve_table_path AS magnitude_curve_table_path,
                   magnitude.curve_row_name AS magnitude_curve_row_name,
                   magnitude.custom_calculation_path AS magnitude_custom_calculation_path,
                   magnitude.set_by_caller_tag AS magnitude_set_by_caller_tag,
                   magnitude.interpretation_status AS magnitude_status
            FROM catalog_mechanics mechanic
            JOIN asset_objects object ON object.id=mechanic.source_object_id
            LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
            WHERE mechanic.source_object_id IN ({placeholders})
            ORDER BY object.package_path, mechanic.property_path
            """,
            sorted(closure),
        ):
            item = {
                **dict(row),
                "conditions": _json(row["conditions_json"], {}),
                "value": _json(row["value_json"], {}),
                "source": _source(connection, row["source_object_id"]),
            }
            item.pop("conditions_json", None)
            item.pop("value_json", None)
            if row["mechanic_type"] == "damage_stat_row":
                item["resolved_data_row"] = _resolved_data_row(
                    connection, snapshot_id, item["value"]
                )
            mechanics.append(item)
        modifiers = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT effect.source_object_id, effect.effect_name,
                       modifier.attribute_name, modifier.modifier_operation,
                       modifier.evaluation_channel,
                       modifier.source_required_tags_json,
                       modifier.source_ignored_tags_json,
                       modifier.target_required_tags_json,
                       modifier.target_ignored_tags_json,
                       modifier.interpretation_status,
                       magnitude.calculation_type, magnitude.literal_value,
                       magnitude.coefficient, magnitude.pre_additive,
                       magnitude.post_additive, magnitude.curve_table_path,
                       magnitude.curve_row_name, magnitude.custom_calculation_path,
                       magnitude.set_by_caller_tag,
                       magnitude.interpretation_status AS magnitude_status
                FROM catalog_effect_modifiers modifier
                JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
                LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=modifier.magnitude_id
                WHERE effect.source_object_id IN ({placeholders})
                ORDER BY effect.effect_name, modifier.modifier_ordinal
                """,
                sorted(closure),
            )
        ]
        effects = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT source_object_id, effect_name, package_path, template_path,
                       stacking_type, stack_limit
                FROM catalog_gameplay_effects
                WHERE source_object_id IN ({placeholders})
                ORDER BY package_path, effect_name
                """,
                sorted(closure),
            )
        ]
        tags = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT occurrence.source_object_id, tag.tag_name,
                       occurrence.property_path, occurrence.semantic_role
                FROM catalog_gameplay_tag_occurrences occurrence
                JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
                WHERE occurrence.source_object_id IN ({placeholders})
                ORDER BY tag.tag_name, occurrence.property_path
                """,
                sorted(closure),
            )
        ]
        opaque = [
            {
                **dict(row),
                "source": _source(connection, row["source_object_id"]),
            }
            for row in connection.execute(
                f"""
                SELECT source_object_id, property_path, mechanic_kind,
                       referenced_path, reason
                FROM catalog_opaque_mechanics
                WHERE source_object_id IN ({placeholders})
                ORDER BY source_object_id, property_path
                """,
                sorted(closure),
            )
        ]

    supported_facts = sum(
        row["interpretation_status"] == "supported"
        and row["magnitude_status"] in (None, "supported")
        for row in mechanics
    ) + sum(
        row["interpretation_status"] == "supported"
        and row["magnitude_status"] in (None, "supported")
        for row in modifiers
    )
    incomplete = bool(dependencies or opaque) or any(
        row["interpretation_status"] != "supported"
        or row["magnitude_status"] not in (None, "supported")
        for row in mechanics + modifiers
    ) or any(not grant["resolved"] for grant in grants) or any(
        link["resolution_status"] != "resolved" for link in links
    )
    status = (
        "partial" if supported_facts and incomplete
        else "supported" if supported_facts
        else "opaque"
    )
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "active_ability_key": active["active_ability_key"],
            "display_name": active["display_name"],
            "package_path": active["package_path"],
            "source": _source(connection, active["source_object_id"]),
        },
        "grantees": grantees,
        "semantics": {
            "status": status,
            "normalized_direct_status": active["semantic_status"],
            "grants": grants,
            "gameplay_ability_links": links,
            "gameplay_effects": effects,
            "mechanics": mechanics,
            "modifiers": modifiers,
            "gameplay_tags": tags,
            "opaque_boundaries": opaque,
            "transitive_asset_objects": len(closure),
        },
        "unresolved_dependencies": dependencies,
    }


def active_ability_coverage(
    connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "active_abilities": []}
    keys = [
        row[0]
        for row in connection.execute(
            """
            SELECT active_ability_key FROM catalog_active_abilities
            WHERE snapshot_id=? ORDER BY display_name, active_ability_key
            """,
            (snapshot_id,),
        )
    ]
    graph_index = _semantic_graph_index(connection, snapshot_id)
    reports = [
        active_ability_report(connection, key, snapshot_id, graph_index=graph_index)
        for key in keys
    ]
    status_counts = {status: 0 for status in ("supported", "partial", "opaque")}
    fact_status = {status: 0 for status in ("supported", "partial", "opaque")}
    mechanic_types: dict[str, int] = {}
    missing: dict[str, dict[str, Any]] = {}
    grant_total = grant_resolved = data_rows = data_rows_resolved = 0
    hero_loadout_keys: set[str] = set()
    class_keys: set[str] = set()
    hero_grantees: set[str] = set()
    class_grantees: set[str] = set()
    structural_grants = resolved_structural_grants = 0
    for report in reports:
        status_counts[report["semantics"]["status"]] += 1
        for grantee in report["grantees"]:
            (hero_loadout_keys if grantee["domain"] == "hero_loadout" else class_keys).add(
                report["identity"]["active_ability_key"]
            )
            (hero_grantees if grantee["domain"] == "hero_loadout" else class_grantees).add(
                grantee["grantee_key"]
            )
            structural_grants += 1
            resolved_structural_grants += int(
                grantee["evidence"]["resolution_status"] == "resolved"
            )
        for mechanic in report["semantics"]["mechanics"]:
            mechanic_types[mechanic["mechanic_type"]] = (
                mechanic_types.get(mechanic["mechanic_type"], 0) + 1
            )
            status = mechanic["interpretation_status"]
            if mechanic["magnitude_status"] in ("partial", "opaque"):
                status = mechanic["magnitude_status"]
            fact_status[status] += 1
            if mechanic["mechanic_type"] == "damage_stat_row":
                data_rows += 1
                data_rows_resolved += int(
                    (mechanic.get("resolved_data_row") or {}).get("status") == "resolved"
                )
        for modifier in report["semantics"]["modifiers"]:
            status = modifier["interpretation_status"]
            if modifier["magnitude_status"] in ("partial", "opaque"):
                status = modifier["magnitude_status"]
            fact_status["partial" if status == "unsupported" else status] += 1
        fact_status["opaque"] += len(report["semantics"]["opaque_boundaries"])
        grant_total += len(report["semantics"]["grants"])
        grant_resolved += sum(g["resolved"] for g in report["semantics"]["grants"])
        for dependency in report["unresolved_dependencies"]:
            missing.setdefault(dependency["package_path"], dependency)
    total_facts = sum(fact_status.values())
    return {
        "snapshot_id": snapshot_id,
        "catalog_scope": {
            "identity_evidence": ["TierAbilityKits", "ClassAbilityKits"],
            "catalog_awareness_complete": bool(reports),
        },
        "counts": {
            "active_ability_identities": len(reports),
            "hero_loadout_ability_identities": len(hero_loadout_keys),
            "class_granted_kit_identities": len(class_keys),
            "heroes_with_loadout_abilities": len(hero_grantees),
            "hero_classes_with_granted_kits": len(class_grantees),
            "structural_grants": structural_grants,
            "resolved_structural_grants": resolved_structural_grants,
            "semantic_status": status_counts,
            "interaction_mechanic_types": mechanic_types,
            "interaction_fact_status_occurrences": fact_status,
            "semantic_grants": grant_total,
            "resolved_semantic_grants": grant_resolved,
            "damage_stat_rows": data_rows,
            "resolved_damage_stat_rows": data_rows_resolved,
            "deduplicated_missing_dependencies": len(missing),
        },
        "coverage": {
            "identity": 1.0 if reports else None,
            "structural_grants_resolved": (
                resolved_structural_grants / structural_grants
                if structural_grants else None
            ),
            "semantics_fully_supported": status_counts["supported"] / len(reports) if reports else None,
            "semantics_known_or_partial": (
                status_counts["supported"] + status_counts["partial"]
            ) / len(reports) if reports else None,
            "semantic_grants_resolved": grant_resolved / grant_total if grant_total else None,
            "damage_stat_rows_resolved": data_rows_resolved / data_rows if data_rows else None,
            "supported_interaction_fact_occurrences": (
                fact_status["supported"] / total_facts if total_facts else None
            ),
        },
        "active_abilities": [
            {
                "active_ability_key": report["identity"]["active_ability_key"],
                "display_name": report["identity"]["display_name"],
                "grant_domains": sorted({g["domain"] for g in report["grantees"]}),
                "hero_grant_count": sum(g["domain"] == "hero_loadout" for g in report["grantees"]),
                "semantic_status": report["semantics"]["status"],
                "mechanic_count": len(report["semantics"]["mechanics"]),
                "modifier_count": len(report["semantics"]["modifiers"]),
                "opaque_boundary_count": len(report["semantics"]["opaque_boundaries"]),
                "unresolved_dependency_count": len(report["unresolved_dependencies"]),
            }
            for report in reports
        ],
        "unresolved_dependencies": sorted(
            missing.values(), key=lambda item: (item["priority"], item["package_path"])
        ),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("team-perks", help="report team-perk catalog coverage")
    coverage.add_argument("--snapshot-id", type=int)
    detail = commands.add_parser("team-perk", help="show one team perk's interaction graph")
    detail.add_argument("name")
    detail.add_argument("--snapshot-id", type=int)
    abilities = commands.add_parser("abilities", help="report hero/class active-ability coverage")
    abilities.add_argument("--snapshot-id", type=int)
    ability = commands.add_parser("ability", help="show one active ability's interaction graph")
    ability.add_argument("name")
    ability.add_argument("--snapshot-id", type=int)
    gadgets = commands.add_parser("gadgets", help="report selectable gadget coverage")
    gadgets.add_argument("--snapshot-id", type=int)
    gadget = commands.add_parser("gadget", help="show one gadget's interaction graph")
    gadget.add_argument("name")
    gadget.add_argument("--snapshot-id", type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "team-perks":
            payload = team_perk_coverage(connection, args.snapshot_id)
        elif args.command == "team-perk":
            payload = team_perk_report(connection, args.name, args.snapshot_id)
        elif args.command == "abilities":
            payload = active_ability_coverage(connection, args.snapshot_id)
        elif args.command == "ability":
            payload = active_ability_report(connection, args.name, args.snapshot_id)
        elif args.command == "gadgets":
            payload = gadget_coverage(connection, args.snapshot_id)
        else:
            payload = gadget_report(connection, args.name, args.snapshot_id)
    finally:
        connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
