#!/usr/bin/env python3
"""Auditable STW signature-weapon and sixth-perk interaction reports."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Iterable

from stw_assets import latest_asset_snapshot_id
from stw_interactions import (
    _ability_kit_interaction_semantics,
    _semantic_graph_index,
    _source,
)
from stw_pipeline import connect
from stw_runtime import nocturno_signature_report


def _signature_row(connection, snapshot_id: int, name: str):
    rows = connection.execute(
        """
        SELECT * FROM catalog_signature_effects
        WHERE snapshot_id=? AND (
          lower(signature_key)=lower(?)
          OR lower(coalesce(display_name, ''))=lower(?)
        ) ORDER BY id
        """,
        (snapshot_id, name, name),
    ).fetchall()
    if len(rows) != 1:
        matches = [row["signature_key"] for row in rows]
        suffix = f"; matching keys: {matches}" if matches else ""
        raise ValueError(f"signature effect must resolve exactly once: {name!r}{suffix}")
    return rows[0]


def signature_report(
    connection,
    name: str,
    snapshot_id: int | None = None,
    *,
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    signature = _signature_row(connection, snapshot_id, name)
    semantics = _ability_kit_interaction_semantics(
        connection,
        snapshot_id,
        signature["ability_kit_id"],
        {signature["source_object_id"]},
        graph_index,
    )
    owner_rows = connection.execute(
        """
        SELECT owner.ownership_kind, owner.slot_ordinal, owner.perk_rarity,
               identity.identity_key, identity.display_name AS weapon_name,
               identity.weapon_kind, variant.variant_key, variant.package_path,
               variant.rarity, variant.tier,
               COUNT(DISTINCT schematic.id) AS schematic_count
        FROM catalog_signature_weapon_owners owner
        JOIN catalog_weapon_identities identity ON identity.id=owner.weapon_identity_id
        JOIN catalog_weapon_variants variant ON variant.id=owner.weapon_variant_id
        LEFT JOIN catalog_schematics schematic
          ON schematic.weapon_variant_id=variant.id
        WHERE owner.signature_effect_id=?
        GROUP BY owner.id
        ORDER BY identity.display_name, variant.variant_key, owner.perk_rarity
        """,
        (signature["id"],),
    ).fetchall()
    owners = [dict(row) for row in owner_rows]
    interaction_tags = sorted({tag["tag_name"] for tag in semantics["gameplay_tags"]})
    condition_tags = sorted(
        {
            tag["tag_name"]
            for tag in semantics["gameplay_tags"]
            if tag.get("semantic_role") != "declared"
        }
    )
    event_tags = sorted(tag for tag in interaction_tags if tag.startswith("Event."))
    event_mechanics = [
        item
        for item in semantics["mechanics"]
        if item["mechanic_type"] in {
            "trigger",
            "effect_container",
            "application_chance",
            "duration",
            "period",
            "stacking",
            "execution",
            "execution_modifier",
            "damage_stat_row",
            "parameter",
            "referenced_effect",
            "spawned_entity",
        }
    ]
    specialized = None
    if signature["signature_key"] == "aid_g_weapon_onreload_explode":
        specialized = nocturno_signature_report(connection, snapshot_id)
    return {
        "snapshot_id": snapshot_id,
        "identity": {
            "signature_key": signature["signature_key"],
            "display_name": signature["display_name"],
            "description": signature["description"],
            "signature_kind": signature["signature_kind"],
            "source": _source(connection, signature["source_object_id"]),
        },
        "ownership": {
            "weapon_families": len({row["identity_key"] for row in owners}),
            "eligible_variants": len({row["variant_key"] for row in owners}),
            "linked_schematics": sum(row["schematic_count"] for row in owners),
            "variants": owners,
        },
        "semantics": {
            **semantics,
            "normalized_direct_status": signature["semantic_status"],
            "event_mechanics": event_mechanics,
            "event_tags": event_tags,
            "condition_tags": condition_tags,
            "interaction_tags": interaction_tags,
            "cross_system_boundary": (
                "shared gameplay tags and conditions are normalized for hero, team-perk, "
                "gadget, weapon-perk, enemy, and mission contexts; runtime compatibility "
                "is not inferred from tag overlap alone"
            ),
        },
        "specialized_analysis": specialized,
        "unresolved_dependencies": semantics["unresolved_dependencies"],
    }


def signature_coverage(connection, snapshot_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        return {"snapshot_id": None, "counts": {}, "signatures": []}
    graph_index = _semantic_graph_index(connection, snapshot_id)
    keys = [
        row[0]
        for row in connection.execute(
            "SELECT signature_key FROM catalog_signature_effects WHERE snapshot_id=? ORDER BY id",
            (snapshot_id,),
        )
    ]
    reports = [
        signature_report(connection, key, snapshot_id, graph_index=graph_index)
        for key in keys
    ]
    statuses = {status: 0 for status in ("supported", "partial", "opaque")}
    facts = {status: 0 for status in ("supported", "partial", "opaque")}
    kinds: dict[str, int] = {}
    mechanics: dict[str, int] = {}
    missing: dict[str, dict[str, Any]] = {}
    families: set[str] = set()
    variants: set[str] = set()
    opaque_boundaries = 0
    for report in reports:
        statuses[report["semantics"]["status"]] += 1
        kind = report["identity"]["signature_kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
        opaque_boundaries += len(report["semantics"]["opaque_boundaries"])
        for owner in report["ownership"]["variants"]:
            families.add(owner["identity_key"])
            variants.add(owner["variant_key"])
        for item in report["semantics"]["mechanics"] + report["semantics"]["modifiers"]:
            status = item["interpretation_status"]
            if item["magnitude_status"] in ("partial", "opaque"):
                status = item["magnitude_status"]
            facts[status] += 1
            mechanic = item.get("mechanic_type") or "effect_modifier"
            mechanics[mechanic] = mechanics.get(mechanic, 0) + 1
        for dependency in report["unresolved_dependencies"]:
            current = missing.get(dependency["package_path"])
            if current is None or dependency["priority"] < current["priority"]:
                missing[dependency["package_path"]] = dependency
    total_weapon_families = connection.execute(
        "SELECT COUNT(*) FROM catalog_weapon_identities WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()[0]
    fact_total = sum(facts.values())
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "signature_identities": len(reports),
            "signature_kinds": kinds,
            "weapon_families": len(families),
            "eligible_variants": len(variants),
            "supported_signatures": statuses["supported"],
            "partial_signatures": statuses["partial"],
            "opaque_signatures": statuses["opaque"],
            "supported_facts": facts["supported"],
            "partial_facts": facts["partial"],
            "opaque_facts": facts["opaque"],
            "opaque_boundaries": opaque_boundaries,
            "unresolved_dependencies": len(missing),
        },
        "ratios": {
            "weapon_family_coverage": (
                len(families) / total_weapon_families if total_weapon_families else None
            ),
            "semantics_fully_supported": (
                statuses["supported"] / len(reports) if reports else None
            ),
            "semantics_known_or_partial": (
                (statuses["supported"] + statuses["partial"]) / len(reports)
                if reports else None
            ),
            "supported_interaction_fact_coverage": (
                facts["supported"] / fact_total if fact_total else None
            ),
        },
        "mechanic_types": dict(sorted(mechanics.items())),
        "signatures": [
            {
                "signature_key": report["identity"]["signature_key"],
                "display_name": report["identity"]["display_name"],
                "signature_kind": report["identity"]["signature_kind"],
                "semantic_status": report["semantics"]["status"],
                "weapon_family_count": report["ownership"]["weapon_families"],
                "eligible_variant_count": report["ownership"]["eligible_variants"],
                "opaque_boundary_count": len(report["semantics"]["opaque_boundaries"]),
                "unresolved_dependency_count": len(report["unresolved_dependencies"]),
            }
            for report in reports
        ],
        "nocturno": next(
            (
                report["specialized_analysis"]
                for report in reports
                if report["identity"]["signature_key"]
                == "aid_g_weapon_onreload_explode"
            ),
            None,
        ),
        "unresolved_dependencies": sorted(
            missing.values(), key=lambda item: (item["priority"], item["package_path"])
        ),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("signatures", help="report signature-effect coverage")
    coverage.add_argument("--snapshot-id", type=int)
    detail = commands.add_parser("signature", help="show one signature interaction graph")
    detail.add_argument("name")
    detail.add_argument("--snapshot-id", type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        payload = (
            signature_coverage(connection, args.snapshot_id)
            if args.command == "signatures"
            else signature_report(connection, args.name, args.snapshot_id)
        )
    finally:
        connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
