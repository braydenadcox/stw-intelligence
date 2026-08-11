#!/usr/bin/env python3
"""Canonical, auditable STW elemental and status-effect reports."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from stw_assets import latest_asset_snapshot_id
from stw_interactions import _semantic_closure, _semantic_graph_index, _source
from stw_pipeline import connect
from stw_runtime import runtime_semantics_report


def _domain_index(connection, snapshot_id: int, graph_index: dict[str, Any]) -> dict[int, set[str]]:
    """Map objects to owner domains through the existing reference graph."""
    domain_queries = {
        "hero_perks": """
            SELECT kit.source_object_id FROM catalog_perks perk
              JOIN catalog_ability_kits kit ON kit.id=perk.ability_kit_id
              WHERE perk.snapshot_id=?
        """,
        "team_perks": """
            SELECT source_object_id FROM catalog_team_perks WHERE snapshot_id=?
            UNION SELECT kit.source_object_id FROM catalog_team_perks perk
              JOIN catalog_ability_kits kit ON kit.id=perk.ability_kit_id
              WHERE perk.snapshot_id=?
        """,
        "active_abilities": """
            SELECT source_object_id FROM catalog_active_abilities WHERE snapshot_id=?
            UNION SELECT kit.source_object_id FROM catalog_active_abilities ability
              JOIN catalog_ability_kits kit ON kit.id=ability.ability_kit_id
              WHERE ability.snapshot_id=?
        """,
        "gadgets": """
            SELECT source_object_id FROM catalog_gadgets WHERE snapshot_id=?
            UNION SELECT kit.source_object_id FROM catalog_gadgets gadget
              JOIN catalog_ability_kits kit ON kit.id=gadget.ability_kit_id
              WHERE gadget.snapshot_id=?
        """,
        "weapon_perks": """
            SELECT source_object_id FROM catalog_alterations WHERE snapshot_id=?
            UNION SELECT kit.source_object_id FROM catalog_alterations alteration
              JOIN catalog_ability_kits kit ON kit.id=alteration.ability_kit_id
              WHERE alteration.snapshot_id=?
        """,
        "signatures": """
            SELECT source_object_id FROM catalog_signature_effects WHERE snapshot_id=?
            UNION SELECT kit.source_object_id FROM catalog_signature_effects signature
              JOIN catalog_ability_kits kit ON kit.id=signature.ability_kit_id
              WHERE signature.snapshot_id=?
        """,
        "weapons": "SELECT source_object_id FROM catalog_weapon_variants WHERE snapshot_id=?",
    }
    result: dict[int, set[str]] = defaultdict(set)
    for domain, query in domain_queries.items():
        parameter_count = query.count("?")
        roots = {row[0] for row in connection.execute(query, (snapshot_id,) * parameter_count)}
        if not roots:
            continue
        closure, _ = _semantic_closure(connection, snapshot_id, roots, graph_index)
        for object_id in closure:
            result[object_id].add(domain)
    for object_id, (package_path, _) in graph_index["objects"].items():
        if package_path.startswith(("/SaveTheWorld/Abilities/NPC/", "/SaveTheWorld/Characters/Enemies/")):
            result[object_id].add("enemies")
        if "/GameplayModifiers/" in package_path:
            result[object_id].add("mission_modifiers")
    return result


def _occurrence_report(
    connection, tag_ids: list[int], domains: dict[int, set[str]], *, limit: int = 12
) -> dict[str, Any]:
    if not tag_ids:
        return {"count": 0, "semantic_roles": {}, "domains": {}, "examples": []}
    placeholders = ",".join("?" for _ in tag_ids)
    rows = connection.execute(
        f"""
        SELECT occurrence.source_object_id, tag.tag_name, occurrence.property_path,
               occurrence.semantic_role
        FROM catalog_gameplay_tag_occurrences occurrence
        JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
        WHERE occurrence.tag_id IN ({placeholders})
        ORDER BY tag.tag_name, occurrence.id
        """,
        tag_ids,
    ).fetchall()
    role_counts = Counter(row["semantic_role"] for row in rows)
    domain_counts: Counter[str] = Counter()
    for row in rows:
        for domain in domains.get(row["source_object_id"], {"unowned_asset"}):
            domain_counts[domain] += 1
    examples = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        key = (row["source_object_id"], row["tag_name"])
        if key in seen:
            continue
        seen.add(key)
        examples.append({
            "tag_name": row["tag_name"],
            "semantic_role": row["semantic_role"],
            "domains": sorted(domains.get(row["source_object_id"], {"unowned_asset"})),
            "source": _source(connection, row["source_object_id"]),
        })
        if len(examples) >= limit:
            break
    return {
        "count": len(rows),
        "semantic_roles": dict(sorted(role_counts.items())),
        "domains": dict(sorted(domain_counts.items())),
        "examples": examples,
    }


def _mechanic_summary(connection, snapshot_id: int, tag_ids: list[int]) -> dict[str, Any]:
    if not tag_ids:
        return {"mechanic_types": {}, "modifier_attributes": {}, "opaque_boundaries": 0}
    placeholders = ",".join("?" for _ in tag_ids)
    source_ids = [row[0] for row in connection.execute(
        f"SELECT DISTINCT source_object_id FROM catalog_gameplay_tag_occurrences WHERE tag_id IN ({placeholders})",
        tag_ids,
    )]
    if not source_ids:
        return {"mechanic_types": {}, "modifier_attributes": {}, "opaque_boundaries": 0}
    source_placeholders = ",".join("?" for _ in source_ids)
    mechanics = Counter()
    statuses = Counter()
    for row in connection.execute(
        f"SELECT mechanic_type, interpretation_status FROM catalog_mechanics WHERE snapshot_id=? AND source_object_id IN ({source_placeholders})",
        (snapshot_id, *source_ids),
    ):
        mechanics[row["mechanic_type"]] += 1
        statuses[row["interpretation_status"]] += 1
    modifiers = Counter(
        (row[0] or "unknown") for row in connection.execute(
            f"""SELECT modifier.attribute_name FROM catalog_effect_modifiers modifier
                JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
                WHERE effect.source_object_id IN ({source_placeholders})""",
            source_ids,
        )
    )
    opaque = connection.execute(
        f"SELECT COUNT(*) FROM catalog_opaque_mechanics WHERE snapshot_id=? AND source_object_id IN ({source_placeholders})",
        (snapshot_id, *source_ids),
    ).fetchone()[0]
    return {
        "mechanic_types": dict(sorted(mechanics.items())),
        "interpretation_statuses": dict(sorted(statuses.items())),
        "modifier_attributes": dict(sorted(modifiers.items())),
        "opaque_boundaries": opaque,
    }


def elemental_matchup_report(connection, snapshot_id: int) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT effect.source_object_id, effect.effect_name,
               modifier.modifier_operation, modifier.curve_row_name,
               modifier.source_required_tags_json, curve_row.id AS curve_row_id,
               MIN(point.output_value) AS minimum_value,
               MAX(point.output_value) AS maximum_value
        FROM catalog_gameplay_effects effect
        JOIN catalog_effect_modifiers modifier ON modifier.gameplay_effect_id=effect.id
        JOIN catalog_curve_rows curve_row ON curve_row.id=modifier.curve_row_id
        JOIN catalog_curve_points point ON point.curve_row_id=curve_row.id
        WHERE effect.snapshot_id=? AND modifier.attribute_name='DamageResistance'
          AND modifier.curve_row_name LIKE 'Elemental.DamageResist.%'
        GROUP BY effect.id, modifier.id
        ORDER BY effect.effect_name, modifier.modifier_ordinal
        """,
        (snapshot_id,),
    ).fetchall()
    if not rows:
        return runtime_semantics_report(connection, snapshot_id)["rules"]["elemental_matchups"]
    tag_to_element = {
        row["internal_damage_tag"]: row["display_name"]
        for row in connection.execute(
            "SELECT internal_damage_tag, display_name FROM catalog_element_identities WHERE snapshot_id=?",
            (snapshot_id,),
        )
    }
    by_effect: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        by_effect[row["source_object_id"]].append(row)
    rules = []
    for source_object_id, effect_rows in by_effect.items():
        granted = connection.execute(
            """
            SELECT tag.tag_name FROM catalog_gameplay_tag_occurrences occurrence
            JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
            WHERE occurrence.source_object_id=? AND occurrence.semantic_role='granted'
            """,
            (source_object_id,),
        ).fetchall()
        defender_tags = [row[0] for row in granted if row[0] in tag_to_element]
        defender = tag_to_element[defender_tags[0]] if len(set(defender_tags)) == 1 else None
        default = next(
            (row["minimum_value"] for row in effect_rows
             if row["curve_row_name"] == "Elemental.DamageResist.Default"
             and row["minimum_value"] == row["maximum_value"]),
            None,
        )
        for row in effect_rows:
            if row["minimum_value"] != row["maximum_value"]:
                value = None
            else:
                value = row["minimum_value"]
            required = json.loads(row["source_required_tags_json"] or "[]")
            attacker = tag_to_element.get(required[0]) if len(required) == 1 else None
            relationship = row["curve_row_name"].removeprefix("Elemental.DamageResist.")
            rules.append({
                "defender_element": defender,
                "attacker_element": attacker,
                "relationship": relationship,
                "modifier_operation": row["modifier_operation"],
                "resistance_adjustment": value,
                "total_damage_resistance": (
                    default + value if default is not None and value is not None
                    and relationship != "Default" else value
                ),
                "condition_tag": required[0] if len(required) == 1 else None,
                "source": _source(connection, source_object_id),
                "curve_row": row["curve_row_name"],
            })
    freeze_rows = [dict(row) for row in connection.execute(
        """
        SELECT curve_row.row_name, MIN(point.output_value) AS minimum_value,
               MAX(point.output_value) AS maximum_value,
               object.package_path, file.relative_path, file.content_sha256
        FROM catalog_curve_tables curve_table
        JOIN catalog_curve_rows curve_row ON curve_row.curve_table_id=curve_table.id
        JOIN catalog_curve_points point ON point.curve_row_id=curve_row.id
        JOIN asset_objects object ON object.id=curve_table.source_object_id
        JOIN asset_files file ON file.id=object.asset_file_id
        WHERE curve_table.snapshot_id=?
          AND curve_row.row_name LIKE 'Elemental.FreezeDurationMult.%'
        GROUP BY curve_row.id ORDER BY curve_row.row_name
        """,
        (snapshot_id,),
    )]
    constant_rules = sum(rule["resistance_adjustment"] is not None for rule in rules)
    return {
        "status": "partial",
        "proven": (
            "enemy element effects grant structural defender tags and apply conditional, "
            "additive DamageResistance curve values for exact attacker tags"
        ),
        "rules": rules,
        "constant_rule_count": constant_rules,
        "freeze_duration_rules": freeze_rows,
        "remaining_boundary": (
            "FortDamageFormulaExecutionCalculation converts aggregated DamageResistance "
            "into final damage; no final damage multiplier is inferred"
        ),
    }


def _interaction_fact_counts(connection, snapshot_id: int) -> dict[str, int]:
    source_ids = [row[0] for row in connection.execute(
        """
        SELECT DISTINCT occurrence.source_object_id
        FROM catalog_gameplay_tag_occurrences occurrence
        WHERE occurrence.tag_id IN (
          SELECT tag_id FROM catalog_element_tags element_tag
          JOIN catalog_element_identities element ON element.id=element_tag.element_id
          WHERE element.snapshot_id=?
          UNION
          SELECT tag_id FROM catalog_status_tags status_tag
          JOIN catalog_status_identities status ON status.id=status_tag.status_id
          WHERE status.snapshot_id=?
        )
        """,
        (snapshot_id, snapshot_id),
    )]
    counts = Counter({"supported": 0, "partial": 0, "opaque": 0})
    if not source_ids:
        return dict(counts)
    placeholders = ",".join("?" for _ in source_ids)
    for row in connection.execute(
        f"SELECT interpretation_status FROM catalog_mechanics WHERE snapshot_id=? AND source_object_id IN ({placeholders})",
        (snapshot_id, *source_ids),
    ):
        counts[row[0]] += 1
    for row in connection.execute(
        f"""SELECT modifier.interpretation_status,
                   magnitude.interpretation_status AS magnitude_status
            FROM catalog_effect_modifiers modifier
            JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
            LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=modifier.magnitude_id
            WHERE effect.source_object_id IN ({placeholders})""",
        source_ids,
    ):
        if row["magnitude_status"] == "opaque":
            counts["opaque"] += 1
        elif row["interpretation_status"] == "supported" and row["magnitude_status"] in (None, "supported"):
            counts["supported"] += 1
        else:
            counts["partial"] += 1
    counts["opaque"] += connection.execute(
        f"SELECT COUNT(*) FROM catalog_opaque_mechanics WHERE snapshot_id=? AND source_object_id IN ({placeholders})",
        (snapshot_id, *source_ids),
    ).fetchone()[0]
    return dict(counts)


def element_report(
    connection, name: str, snapshot_id: int | None = None, *,
    graph_index: dict[str, Any] | None = None,
    domains: dict[int, set[str]] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    rows = connection.execute(
        """SELECT * FROM catalog_element_identities WHERE snapshot_id=? AND
             (lower(element_key)=lower(?) OR lower(display_name)=lower(?)
              OR lower(internal_damage_tag)=lower(?))""",
        (snapshot_id, name, name, name),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"element must resolve exactly once: {name!r}")
    row = rows[0]
    tag_rows = connection.execute(
        """SELECT tag.id, tag.tag_name, link.tag_role, link.evidence_object_id
           FROM catalog_element_tags link JOIN catalog_gameplay_tags tag ON tag.id=link.tag_id
           WHERE link.element_id=? ORDER BY link.tag_role, tag.tag_name""",
        (row["id"],),
    ).fetchall()
    graph_index = graph_index or _semantic_graph_index(connection, snapshot_id)
    domains = domains or _domain_index(connection, snapshot_id, graph_index)
    tag_ids = sorted({item["id"] for item in tag_rows})
    alterations = [dict(item) for item in connection.execute(
        """SELECT DISTINCT alteration.alteration_key, alteration.display_name,
                  alteration.description, alteration.semantic_status
           FROM catalog_alterations alteration
           JOIN catalog_gameplay_tag_occurrences occurrence
             ON occurrence.source_object_id=alteration.source_object_id
           WHERE occurrence.tag_id IN ({}) ORDER BY alteration.display_name""".format(
               ",".join("?" for _ in tag_ids)
           ), tag_ids,
    )] if tag_ids else []
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "element_key": row["element_key"], "display_name": row["display_name"],
            "internal_damage_tag": row["internal_damage_tag"],
            "identity_evidence": row["identity_evidence"],
            "semantic_status": row["semantic_status"],
        },
        "tags": [
            {"tag_name": item["tag_name"], "role": item["tag_role"],
             "evidence": _source(connection, item["evidence_object_id"])
             if item["evidence_object_id"] else None}
            for item in tag_rows
        ],
        "occurrences": _occurrence_report(connection, tag_ids, domains),
        "mechanics": _mechanic_summary(connection, snapshot_id, tag_ids),
        "element_changing_perks": alterations,
        "matchup_rule": elemental_matchup_report(connection, snapshot_id),
    }


def status_report(
    connection, name: str, snapshot_id: int | None = None, *,
    graph_index: dict[str, Any] | None = None,
    domains: dict[int, set[str]] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    rows = connection.execute(
        """SELECT status.*, parent.status_key AS parent_status_key
           FROM catalog_status_identities status
           LEFT JOIN catalog_status_identities parent ON parent.id=status.parent_status_id
           WHERE status.snapshot_id=? AND (
             lower(status.status_key)=lower(?) OR lower(status.display_name)=lower(?)
             OR lower(replace(status.status_key, 'gameplay.status.', ''))=lower(?)
           )""",
        (snapshot_id, name, name, name),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"status must resolve exactly once: {name!r}")
    row = rows[0]
    tags = connection.execute(
        """SELECT tag.id, tag.tag_name, link.tag_role
           FROM catalog_status_tags link JOIN catalog_gameplay_tags tag ON tag.id=link.tag_id
           WHERE link.status_id=? ORDER BY link.tag_role, tag.tag_name""",
        (row["id"],),
    ).fetchall()
    graph_index = graph_index or _semantic_graph_index(connection, snapshot_id)
    domains = domains or _domain_index(connection, snapshot_id, graph_index)
    tag_ids = [tag["id"] for tag in tags]
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "status_key": row["status_key"], "display_name": row["display_name"],
            "status_family": row["status_family"],
            "parent_status_key": row["parent_status_key"],
            "semantic_status": row["semantic_status"],
        },
        "tags": [{"tag_name": tag["tag_name"], "role": tag["tag_role"]} for tag in tags],
        "occurrences": _occurrence_report(connection, tag_ids, domains),
        "mechanics": _mechanic_summary(connection, snapshot_id, tag_ids),
    }


def element_status_coverage(connection, snapshot_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}}
    graph = _semantic_graph_index(connection, snapshot_id)
    domains = _domain_index(connection, snapshot_id, graph)
    elements = [
        element_report(connection, row[0], snapshot_id, graph_index=graph, domains=domains)
        for row in connection.execute(
            "SELECT element_key FROM catalog_element_identities WHERE snapshot_id=? ORDER BY display_name",
            (snapshot_id,),
        )
    ]
    statuses = [
        status_report(connection, row[0], snapshot_id, graph_index=graph, domains=domains)
        for row in connection.execute(
            "SELECT status_key FROM catalog_status_identities WHERE snapshot_id=? ORDER BY status_key",
            (snapshot_id,),
        )
    ]
    status_counts = Counter(item["identity"]["semantic_status"] for item in statuses)
    interaction_facts = _interaction_fact_counts(connection, snapshot_id)
    domain_counts: Counter[str] = Counter()
    for item in elements + statuses:
        domain_counts.update(item["occurrences"]["domains"])
    validation_names = (
        "afflicted", "snare", "frozen", "stun", "vulnerable", "knockbackimmunity"
    )
    validations = {
        name: next((item for item in statuses if item["identity"]["display_name"].casefold() == name), None)
        for name in validation_names
    }
    nocturno = connection.execute(
        """SELECT COUNT(DISTINCT element.element_key)
           FROM catalog_weapon_identities identity
           JOIN catalog_weapon_variants variant ON variant.identity_id=identity.id
           JOIN catalog_weapon_slots slot ON slot.slot_loadout_id=variant.slot_loadout_id
           JOIN catalog_weapon_slot_options option_row ON option_row.weapon_slot_id=slot.id
           JOIN catalog_alterations alteration ON alteration.id=option_row.alteration_id
           JOIN catalog_gameplay_tag_occurrences occurrence
             ON occurrence.source_object_id=alteration.source_object_id
           JOIN catalog_element_tags element_tag ON element_tag.tag_id=occurrence.tag_id
           JOIN catalog_element_identities element ON element.id=element_tag.element_id
           WHERE identity.snapshot_id=? AND lower(identity.display_name)='nocturno'
             AND element_tag.tag_role='damage'""",
        (snapshot_id,),
    ).fetchone()[0]
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "element_identities": len(elements),
            "status_identities": len(statuses),
            "supported_statuses": status_counts["supported"],
            "partial_statuses": status_counts["partial"],
            "opaque_statuses": status_counts["opaque"],
            "enemy_element_identities": sum("enemies" in item["occurrences"]["domains"] for item in elements),
            "nocturno_selectable_elements": nocturno,
            "supported_interaction_facts": interaction_facts["supported"],
            "partial_interaction_facts": interaction_facts["partial"],
            "opaque_interaction_facts": interaction_facts["opaque"],
        },
        "interaction_occurrences_by_domain": dict(sorted(domain_counts.items())),
        "elements": [item["identity"] | {
            "occurrence_count": item["occurrences"]["count"],
            "domains": item["occurrences"]["domains"],
            "element_changing_perks": len(item["element_changing_perks"]),
        } for item in elements],
        "status_families": dict(sorted(Counter(
            item["identity"]["status_family"] for item in statuses
        ).items())),
        "statuses": [item["identity"] | {
            "occurrence_count": item["occurrences"]["count"],
            "domains": item["occurrences"]["domains"],
        } for item in statuses],
        "validation_cases": validations,
        "elemental_matchups": elemental_matchup_report(connection, snapshot_id),
        "boundary": (
            "element and status tags, conditions, durations, periods, chances, stacking, "
            "modifiers, and executions are reported from assets; native/Blueprint behavior "
            "and elemental matchup multipliers remain explicit rather than inferred"
        ),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("coverage")
    coverage.add_argument("--snapshot-id", type=int)
    element = commands.add_parser("element")
    element.add_argument("name")
    element.add_argument("--snapshot-id", type=int)
    status = commands.add_parser("status")
    status.add_argument("name")
    status.add_argument("--snapshot-id", type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "coverage":
            payload = element_status_coverage(connection, args.snapshot_id)
        elif args.command == "element":
            payload = element_report(connection, args.name, args.snapshot_id)
        else:
            payload = status_report(connection, args.name, args.snapshot_id)
    finally:
        connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
