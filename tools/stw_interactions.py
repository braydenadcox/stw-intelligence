#!/usr/bin/env python3
"""Auditable reports and dependency closure for shared STW interactions."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Iterable

from stw_assets import _queue_classification, latest_asset_snapshot_id
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("team-perks", help="report team-perk catalog coverage")
    coverage.add_argument("--snapshot-id", type=int)
    detail = commands.add_parser("team-perk", help="show one team perk's interaction graph")
    detail.add_argument("name")
    detail.add_argument("--snapshot-id", type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        payload = (
            team_perk_coverage(connection, args.snapshot_id)
            if args.command == "team-perks"
            else team_perk_report(connection, args.name, args.snapshot_id)
        )
    finally:
        connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
