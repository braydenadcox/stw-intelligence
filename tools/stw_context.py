#!/usr/bin/env python3
"""Auditable enemy, mission, and modifier scenario context reports."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stw_assets import latest_asset_snapshot_id
from stw_interactions import _source
from stw_pipeline import connect


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True)
class TargetContext:
    enemy: str
    element: str | None = None
    status_tags: tuple[str, ...] = ()
    modifier_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MissionContext:
    objective: str | None = None
    power_level: int | None = None
    four_player: bool | None = None
    elemental_storm: str | None = None
    modifier_keys: tuple[str, ...] = ()


def _one(connection, query: str, parameters: tuple[Any, ...], label: str):
    rows = connection.execute(query, parameters).fetchall()
    if len(rows) != 1:
        raise ValueError(f"{label} must resolve exactly once; found {len(rows)}")
    return rows[0]


def enemy_report(connection, snapshot_id: int, name: str) -> dict[str, Any]:
    row = _one(connection, """
        SELECT enemy.*, parent.display_name AS parent_name,
               data_row.row_json AS stat_row_json
        FROM catalog_enemy_archetypes enemy
        LEFT JOIN catalog_enemy_archetypes parent ON parent.id=enemy.parent_enemy_id
        LEFT JOIN catalog_data_rows data_row ON data_row.id=enemy.pawn_stat_data_row_id
        WHERE enemy.snapshot_id=? AND (
          lower(enemy.enemy_key)=lower(?) OR lower(enemy.display_name)=lower(?) OR
          lower(COALESCE(enemy.pawn_stat_row_name,''))=lower(?) OR
          lower(COALESCE(enemy.character_family_tag,''))=lower(?))
    """, (snapshot_id, name, name, name, name), f"enemy {name!r}")
    ability_sets = [dict(item) for item in connection.execute("""
        SELECT grant.target_path, grant.resolution_status, kit.kit_name
        FROM catalog_enemy_ability_sets grant
        LEFT JOIN catalog_ability_kits kit ON kit.id=grant.ability_kit_id
        WHERE grant.enemy_id=? ORDER BY grant.grant_ordinal
    """, (row["id"],))]
    zones = [{**dict(item), "bones": _json(item["bones_json"], []),
              "facts": _json(item["facts_json"], {})} for item in connection.execute(
        "SELECT * FROM catalog_enemy_damage_zones WHERE enemy_id=? ORDER BY property_path",
        (row["id"],))]
    return {
        "identity": {key: row[key] for key in (
            "enemy_key", "display_name", "identity_evidence", "pawn_stat_table_path",
            "pawn_stat_row_name", "character_family_tag", "parent_name", "semantic_status")},
        "classification_tags": _json(row["classification_tags_json"], []),
        "attack_tags": _json(row["attack_tags_json"], []),
        "movement_facts": _json(row["movement_facts_json"], {}),
        "stat_row": _json(row["stat_row_json"], None),
        "ability_sets": ability_sets,
        "damage_zones": zones,
        "provenance": _source(connection, row["source_object_id"]),
    }


def mission_report(connection, snapshot_id: int, name: str) -> dict[str, Any]:
    row = _one(connection, """
        SELECT * FROM catalog_mission_objectives WHERE snapshot_id=? AND
          (lower(objective_key)=lower(?) OR lower(display_name)=lower(?) OR
           lower(primary_mission_path)=lower(?))
    """, (snapshot_id, name, name, name), f"mission {name!r}")
    variants = [{**dict(item), "generation_facts": _json(item["generation_facts_json"], {})}
                for item in connection.execute("""
        SELECT variant_key, package_path, generation_facts_json, interpretation_status
        FROM catalog_mission_variants WHERE objective_id=? ORDER BY package_path
    """, (row["id"],))]
    return {"objective_key": row["objective_key"], "display_name": row["display_name"],
            "description": row["description"], "primary_mission_path": row["primary_mission_path"],
            "semantic_status": row["semantic_status"], "variants": variants,
            "provenance": _source(connection, row["source_object_id"])}


def modifier_report(connection, snapshot_id: int, name: str) -> dict[str, Any]:
    row = _one(connection, """
        SELECT * FROM catalog_context_modifiers WHERE snapshot_id=? AND
          (lower(modifier_key)=lower(?) OR lower(display_name)=lower(?))
    """, (snapshot_id, name, name), f"modifier {name!r}")
    grants = []
    for grant in connection.execute("""
        SELECT grant.*, kit.kit_name, effect.effect_name
        FROM catalog_context_modifier_grants grant
        LEFT JOIN catalog_ability_kits kit ON kit.id=grant.ability_kit_id
        LEFT JOIN catalog_gameplay_effects effect ON effect.id=grant.gameplay_effect_id
        WHERE grant.context_modifier_id=? ORDER BY grant.grant_ordinal
    """, (row["id"],)):
        item = dict(grant)
        item["delivery_conditions"] = _json(item.pop("delivery_conditions_json"), {})
        grants.append(item)
    return {"modifier_key": row["modifier_key"], "display_name": row["display_name"],
            "description": row["description"], "target_scope": row["target_scope"],
            "semantic_status": row["semantic_status"],
            "delivery_facts": _json(row["delivery_facts_json"], []), "grants": grants,
            "provenance": _source(connection, row["source_object_id"])}


def _delivery_applies(conditions: Any, target_tags: set[str]) -> tuple[str, list[str]]:
    if not isinstance(conditions, dict):
        return "unknown", ["delivery requirements are not structured"]
    required: set[str] = set()
    ignored: set[str] = set()
    def collect(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value} if "." in value else set()
        if isinstance(value, list):
            result: set[str] = set()
            for item in value: result |= collect(item)
            return result
        if isinstance(value, dict) and isinstance(value.get("TagName"), str):
            return {value["TagName"]}
        return set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, dict):
            for key, child in value.items():
                folded = key.casefold()
                tags = collect(child)
                if "require" in folded and "tag" in folded: required.update(tags)
                elif (("ignore" in folded or "ignored" in folded or "exclude" in folded)
                      and "tag" in folded): ignored.update(tags)
                walk(child)
    walk(conditions)

    def matches(expected: str, actual: str) -> bool:
        return actual == expected or actual.startswith(expected + ".")
    exclusions = sorted(expected for expected in ignored
                        if any(matches(expected, actual) for actual in target_tags))
    if exclusions:
        return "excluded", exclusions
    missing = {expected for expected in required
               if not any(matches(expected, actual) for actual in target_tags)}
    if missing:
        return "unknown", [f"required target tag not observed: {tag}" for tag in sorted(missing)]
    return "applies", []


def scenario_report(connection, snapshot_id: int, target: TargetContext,
                    mission: MissionContext) -> dict[str, Any]:
    enemy = enemy_report(connection, snapshot_id, target.enemy)
    tags = set(enemy["classification_tags"]) | set(target.status_tags)
    if target.element:
        element = connection.execute("""
            SELECT tag.tag_name FROM catalog_element_identities element
            JOIN catalog_element_tags link ON link.element_id=element.id
            JOIN catalog_gameplay_tags tag ON tag.id=link.tag_id
            WHERE element.snapshot_id=? AND lower(element.display_name)=lower(?)
              AND link.tag_role IN ('internal_damage','enemy_identity')
        """, (snapshot_id, target.element)).fetchall()
        tags.update(row[0] for row in element)
    encounter_context = []
    if mission.elemental_storm:
        storm_tag = f"NPC.Elemental.{mission.elemental_storm}"
        tags.add(storm_tag)
        for row in connection.execute(
            "SELECT * FROM catalog_encounter_modifiers WHERE snapshot_id=? AND modifier_tags_json LIKE ?",
            (snapshot_id, f'%"{storm_tag}"%'),
        ):
            encounter_context.append({"encounter_modifier_key": row["encounter_modifier_key"],
                                      "modifier_tags": _json(row["modifier_tags_json"], []),
                                      "semantic_status": row["semantic_status"],
                                      "provenance": _source(connection, row["source_object_id"])})
    objective = mission_report(connection, snapshot_id, mission.objective) if mission.objective else None
    evaluations = []
    for key in (*target.modifier_keys, *mission.modifier_keys):
        modifier = modifier_report(connection, snapshot_id, key)
        grant_results = []
        for grant in modifier["grants"]:
            applicability, reasons = _delivery_applies(grant["delivery_conditions"], tags)
            grant_results.append({"grant_kind": grant["grant_kind"], "target_path": grant["target_path"],
                                  "resolution_status": grant["resolution_status"],
                                  "applicability": applicability, "reasons": reasons})
        evaluations.append({"modifier": modifier["display_name"], "target_scope": modifier["target_scope"],
                            "semantic_status": modifier["semantic_status"], "grants": grant_results,
                            "provenance": modifier["provenance"]})
    return {"target_context": asdict(target), "mission_context": asdict(mission),
            "resolved_target": enemy["identity"], "resolved_objective": objective,
            "effective_target_tags": sorted(tags), "encounter_context": encounter_context,
            "modifier_evaluations": evaluations,
            "four_player_scaling": {"requested": mission.four_player,
                "status": "partial" if mission.four_player is not None else "unknown",
                "boundary": "proven scaling inputs are cataloged; final native enemy health/damage evaluation is not inferred"},
            "evaluation_status": "partial" if evaluations or mission.four_player is not None else "structural"}


def context_coverage(connection, snapshot_id: int) -> dict[str, Any]:
    def grouped(table: str, column: str) -> dict[str, int]:
        return dict(connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} WHERE snapshot_id=? GROUP BY {column} ORDER BY {column}",
            (snapshot_id,)).fetchall())
    enemies = connection.execute("SELECT COUNT(*) FROM catalog_enemy_archetypes WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]
    with_stats = connection.execute("SELECT COUNT(*) FROM catalog_enemy_archetypes WHERE snapshot_id=? AND pawn_stat_data_row_id IS NOT NULL", (snapshot_id,)).fetchone()[0]
    grants = connection.execute("""SELECT grant.resolution_status, COUNT(*) FROM catalog_enemy_ability_sets grant
        JOIN catalog_enemy_archetypes enemy ON enemy.id=grant.enemy_id WHERE enemy.snapshot_id=? GROUP BY grant.resolution_status""", (snapshot_id,)).fetchall()
    interaction_counts = Counter()
    for query in (
        "SELECT semantic_status FROM catalog_enemy_archetypes WHERE snapshot_id=?",
        """SELECT zone.interpretation_status FROM catalog_enemy_damage_zones zone
            JOIN catalog_enemy_archetypes enemy ON enemy.id=zone.enemy_id WHERE enemy.snapshot_id=?""",
        "SELECT semantic_status FROM catalog_context_modifiers WHERE snapshot_id=?",
        "SELECT semantic_status FROM catalog_encounter_modifiers WHERE snapshot_id=?",
    ):
        interaction_counts.update(row[0] for row in connection.execute(query, (snapshot_id,)))
    elemental_enemies = connection.execute("""
        SELECT COUNT(DISTINCT enemy.id) FROM catalog_enemy_archetypes enemy
        JOIN catalog_enemy_ability_sets grant ON grant.enemy_id=enemy.id
        JOIN catalog_ability_kits kit ON kit.id=grant.ability_kit_id
        JOIN catalog_gameplay_tag_occurrences occurrence ON occurrence.source_object_id=kit.source_object_id
        JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
        WHERE enemy.snapshot_id=? AND tag.tag_name LIKE 'Gameplay.Damage.Elemental.%'
    """, (snapshot_id,)).fetchone()[0]
    return {"snapshot_id": snapshot_id, "enemies": {"identities": enemies,
            "stat_rows_resolved": with_stats, "stat_row_coverage_pct": round(100 * with_stats / enemies, 2) if enemies else 0,
            "semantic_statuses": grouped("catalog_enemy_archetypes", "semantic_status"),
            "ability_set_resolution": dict(grants), "elemental_identities": elemental_enemies},
            "missions": {"objectives": connection.execute("SELECT COUNT(*) FROM catalog_mission_objectives WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0],
                         "variants": connection.execute("""SELECT COUNT(*) FROM catalog_mission_variants variant JOIN catalog_mission_objectives objective ON objective.id=variant.objective_id WHERE objective.snapshot_id=?""", (snapshot_id,)).fetchone()[0],
                         "semantic_statuses": grouped("catalog_mission_objectives", "semantic_status")},
            "context_modifiers": {"identities": connection.execute("SELECT COUNT(*) FROM catalog_context_modifiers WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0],
                                  "semantic_statuses": grouped("catalog_context_modifiers", "semantic_status"),
                                  "target_scopes": grouped("catalog_context_modifiers", "target_scope")},
            "encounters": {"option_sets": connection.execute("SELECT COUNT(*) FROM catalog_encounter_option_sets WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0],
                           "modifiers": connection.execute("SELECT COUNT(*) FROM catalog_encounter_modifiers WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0],
                           "modifier_statuses": grouped("catalog_encounter_modifiers", "semantic_status")},
            "interaction_statuses": dict(sorted(interaction_counts.items())),
            "native_boundaries": ["final enemy health/damage scaling", "AI behavior and spawn selection", "Blueprint/native boss and special attack execution"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    parser.add_argument("command", choices=("coverage", "enemy", "mission", "modifier", "scenario"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--objective")
    parser.add_argument("--element")
    parser.add_argument("--elemental-storm")
    parser.add_argument("--four-player", action="store_true")
    parser.add_argument("--modifier", action="append", default=[])
    args = parser.parse_args()
    connection = connect(args.db)
    snapshot_id = latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise SystemExit("no ready asset snapshot")
    if args.command == "coverage": result = context_coverage(connection, snapshot_id)
    elif not args.name: raise SystemExit("this command requires a name")
    elif args.command == "enemy": result = enemy_report(connection, snapshot_id, args.name)
    elif args.command == "mission": result = mission_report(connection, snapshot_id, args.name)
    elif args.command == "modifier": result = modifier_report(connection, snapshot_id, args.name)
    else: result = scenario_report(connection, snapshot_id, TargetContext(args.name, args.element),
                                   MissionContext(args.objective, four_player=args.four_player,
                                                  elemental_storm=args.elemental_storm,
                                                  modifier_keys=tuple(args.modifier)))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
