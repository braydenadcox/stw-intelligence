"""Evidence-only hero perk search and deterministic loadout assembly.

This module deliberately does not simulate damage or interpret opaque Blueprint logic.
It ranks exact semantic matches already proven by the versioned asset catalog.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from stw_assets import (
    _semantic_graph_index,
    _semantic_perk_closure,
    latest_asset_snapshot_id,
    roster_coverage_report,
)
from stw_pipeline import connect


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    decoded = json.loads(value)
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _json_value(value: str | None) -> Any:
    return json.loads(value) if value else None


def _source(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "package_path": row["package_path"],
        "object_name": row["object_name"],
        "relative_path": row["relative_path"],
        "content_sha256": row["content_sha256"],
    }


def _curve_points(
    connection: sqlite3.Connection, curve_row_id: int | None
) -> list[dict[str, Any]]:
    if curve_row_id is None:
        return []
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT time_value, output_value, interpolation
            FROM catalog_curve_points WHERE curve_row_id=? ORDER BY point_ordinal
            """,
            (curve_row_id,),
        )
    ]


def _magnitude(row: sqlite3.Row, prefix: str = "") -> dict[str, Any]:
    def get(name: str) -> Any:
        key = f"{prefix}{name}"
        return row[key] if key in row.keys() else None

    curve_row_id = get("curve_row_id")
    return {
        "kind": get("magnitude_kind") or get("calculation_type"),
        "literal_value": get("literal_value"),
        "coefficient": get("coefficient"),
        "pre_additive": get("pre_additive"),
        "post_additive": get("post_additive"),
        "curve_table_path": get("curve_table_path"),
        "curve_row_name": get("curve_row_name"),
        "curve_points": [],  # populated by the caller
        "custom_calculation_path": get("custom_calculation_path"),
        "set_by_caller_tag": get("set_by_caller_tag"),
    } | {"curve_row_id": curve_row_id}


def _family_facts(
    connection: sqlite3.Connection,
    snapshot_id: int,
    family: str,
    reference_index: dict[int, list[sqlite3.Row]],
    package_object_index: dict[str, list[int]],
) -> dict[str, Any]:
    _, _, perks = _semantic_perk_closure(
        connection,
        snapshot_id,
        family,
        reference_index=reference_index,
        package_object_index=package_object_index,
    )
    perk_ids = [row["id"] for row in perks]
    placeholders = ",".join("?" for _ in perk_ids)
    # Recommendation ownership is intentionally narrower than semantic closure.
    # A perk may reference the active ability it modifies; the active ability's
    # own damage must not be misrepresented as damage granted by the perk.
    owned_rows = connection.execute(
        f"""
        SELECT kit.source_object_id, perk.perk_tier
        FROM catalog_perks perk
        JOIN catalog_ability_kits kit ON kit.id=perk.ability_kit_id
        WHERE perk.id IN ({placeholders})
        UNION ALL
        SELECT effect.source_object_id, perk.perk_tier
        FROM catalog_perks perk
        JOIN catalog_ability_kit_grants grant_row
          ON grant_row.ability_kit_id=perk.ability_kit_id
        JOIN catalog_gameplay_effects effect
          ON effect.id=grant_row.gameplay_effect_id
        WHERE perk.id IN ({placeholders})
        UNION ALL
        SELECT ability.source_object_id, perk.perk_tier
        FROM catalog_perks perk
        JOIN catalog_ability_kit_grants grant_row
          ON grant_row.ability_kit_id=perk.ability_kit_id
        JOIN catalog_abilities ability ON ability.id=grant_row.ability_id
        WHERE perk.id IN ({placeholders})
        """,
        perk_ids + perk_ids + perk_ids,
    ).fetchall()
    owned_tiers: dict[int, set[str]] = {}
    for row in owned_rows:
        if row[0] is not None:
            owned_tiers.setdefault(row[0], set()).add(row[1])
    closure = set(owned_tiers)
    attributes: set[str] = set()
    mechanics: set[str] = set()
    tags: set[str] = set()
    facts: list[dict[str, Any]] = []
    if not closure:
        return {"attributes": [], "mechanics": [], "tags": [], "facts": []}
    placeholders = ",".join("?" for _ in closure)
    parameters = sorted(closure)

    modifier_rows = connection.execute(
        f"""
        SELECT modifier.*, effect.source_object_id, effect.package_path,
               object.object_name,
               file.relative_path, file.content_sha256
        FROM catalog_effect_modifiers modifier
        JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
        JOIN asset_objects object ON object.id=effect.source_object_id
        JOIN asset_files file ON file.id=object.asset_file_id
        WHERE effect.source_object_id IN ({placeholders})
          AND modifier.interpretation_status='supported'
        ORDER BY effect.package_path, modifier.modifier_ordinal
        """,
        parameters,
    ).fetchall()
    for row in modifier_rows:
        condition_tags = sorted(
            set(
                _json_list(row["source_required_tags_json"])
                + _json_list(row["source_ignored_tags_json"])
                + _json_list(row["target_required_tags_json"])
                + _json_list(row["target_ignored_tags_json"])
            )
        )
        attributes.add(row["attribute_name"])
        tags.update(condition_tags)
        magnitude = _magnitude(row)
        magnitude["curve_points"] = _curve_points(
            connection, magnitude.pop("curve_row_id")
        )
        facts.append(
            {
                "fact_type": "modifier",
                "attribute": row["attribute_name"],
                "operation": row["modifier_operation"],
                "magnitude": magnitude,
                "conditions": {
                    "source_required_tags": _json_list(
                        row["source_required_tags_json"]
                    ),
                    "source_ignored_tags": _json_list(
                        row["source_ignored_tags_json"]
                    ),
                    "target_required_tags": _json_list(
                        row["target_required_tags_json"]
                    ),
                    "target_ignored_tags": _json_list(
                        row["target_ignored_tags_json"]
                    ),
                },
                "granting_tiers": sorted(owned_tiers[row["source_object_id"]]),
                "source": _source(row),
            }
        )

    mechanic_rows = connection.execute(
        f"""
        SELECT mechanic.mechanic_type, mechanic.property_path,
               mechanic.conditions_json, mechanic.value_json,
               magnitude.calculation_type AS magnitude_calculation_type,
               magnitude.literal_value AS magnitude_literal_value,
               magnitude.coefficient AS magnitude_coefficient,
               magnitude.pre_additive AS magnitude_pre_additive,
               magnitude.post_additive AS magnitude_post_additive,
               magnitude.curve_table_path AS magnitude_curve_table_path,
               magnitude.curve_row_name AS magnitude_curve_row_name,
               magnitude.curve_row_id AS magnitude_curve_row_id,
               magnitude.custom_calculation_path AS magnitude_custom_calculation_path,
               magnitude.set_by_caller_tag AS magnitude_set_by_caller_tag,
               object.id AS source_object_id, object.package_path, object.object_name,
               file.relative_path, file.content_sha256
        FROM catalog_mechanics mechanic
        JOIN asset_objects object ON object.id=mechanic.source_object_id
        JOIN asset_files file ON file.id=object.asset_file_id
        LEFT JOIN catalog_magnitudes magnitude ON magnitude.id=mechanic.magnitude_id
        WHERE mechanic.source_object_id IN ({placeholders})
          AND mechanic.interpretation_status='supported'
        ORDER BY object.package_path, mechanic.property_path
        """,
        parameters,
    ).fetchall()
    for row in mechanic_rows:
        mechanics.add(row["mechanic_type"])
        magnitude = _magnitude(row, "magnitude_")
        magnitude["curve_points"] = _curve_points(
            connection, magnitude.pop("curve_row_id")
        )
        facts.append(
            {
                "fact_type": "mechanic",
                "mechanic": row["mechanic_type"],
                "property_path": row["property_path"],
                "conditions": _json_value(row["conditions_json"]),
                "value": _json_value(row["value_json"]),
                "magnitude": magnitude,
                "granting_tiers": sorted(owned_tiers[row["source_object_id"]]),
                "source": _source(row),
            }
        )

    tag_rows = connection.execute(
        f"""
        SELECT tag.tag_name, occurrence.semantic_role, occurrence.property_path,
               object.id AS source_object_id, object.package_path, object.object_name,
               file.relative_path, file.content_sha256
        FROM catalog_gameplay_tag_occurrences occurrence
        JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
        JOIN asset_objects object ON object.id=occurrence.source_object_id
        JOIN asset_files file ON file.id=object.asset_file_id
        WHERE occurrence.source_object_id IN ({placeholders})
          AND occurrence.semantic_role <> 'declared'
        ORDER BY tag.tag_name, occurrence.semantic_role, object.package_path
        """,
        parameters,
    ).fetchall()
    seen_tag_facts: set[tuple[str, str, str]] = set()
    for row in tag_rows:
        tags.add(row["tag_name"])
        key = (row["tag_name"], row["semantic_role"], row["package_path"])
        if key in seen_tag_facts:
            continue
        seen_tag_facts.add(key)
        facts.append(
            {
                "fact_type": "tag",
                "tag": row["tag_name"],
                "role": row["semantic_role"],
                "property_path": row["property_path"],
                "granting_tiers": sorted(owned_tiers[row["source_object_id"]]),
                "source": _source(row),
            }
        )
    return {
        "attributes": sorted(attributes),
        "mechanics": sorted(mechanics),
        "tags": sorted(tags),
        "facts": facts,
    }


def build_semantic_catalog(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no ready asset snapshot")
    roster = roster_coverage_report(connection, snapshot_id)
    statuses = {row["perk_family"]: row for row in roster["perk_families"]}
    assignments: dict[str, dict[str, list[dict[str, str]]]] = {}
    for hero in roster["heroes"]:
        for perk in hero["perks"]:
            assignments.setdefault(perk["perk_family"], {}).setdefault(
                perk["perk_mode"], []
            ).append(
                {
                    "hero_key": hero["hero_key"],
                    "display_name": hero["display_name"],
                    "hero_class": hero["hero_class"],
                    "perk_tier": perk["perk_tier"],
                }
            )
    reference_index, package_object_index = _semantic_graph_index(
        connection, snapshot_id
    )
    families = []
    for family in sorted(statuses):
        semantic = statuses[family]
        families.append(
            {
                "perk_family": family,
                "status": semantic["status"],
                "optimization_ready": semantic["optimization_ready"],
                "reasons": semantic["reasons"],
                "assignments": {
                    mode: sorted(rows, key=lambda item: (item["display_name"], item["hero_key"]))
                    for mode, rows in assignments.get(family, {}).items()
                },
                **_family_facts(
                    connection,
                    snapshot_id,
                    family,
                    reference_index,
                    package_object_index,
                ),
            }
        )
    return {
        "snapshot_id": snapshot_id,
        "catalog_awareness": roster["catalog_awareness"],
        "families": families,
    }


def _canonical(values: Sequence[str]) -> set[str]:
    return {value.casefold() for value in values}


def _criteria(
    attributes: Sequence[str],
    tags: Sequence[str],
    mechanics: Sequence[str],
    prefer_attributes: Sequence[str],
    prefer_tags: Sequence[str],
    prefer_mechanics: Sequence[str],
) -> dict[str, list[str]]:
    return {
        "required_attributes": sorted(set(attributes)),
        "required_tags": sorted(set(tags)),
        "required_mechanics": sorted(set(mechanics)),
        "preferred_attributes": sorted(set(prefer_attributes)),
        "preferred_tags": sorted(set(prefer_tags)),
        "preferred_mechanics": sorted(set(prefer_mechanics)),
    }


def _matches(family: dict[str, Any], criteria: dict[str, list[str]]) -> bool:
    required_attributes = _canonical(criteria["required_attributes"])
    required_tags = _canonical(criteria["required_tags"])
    if required_attributes and required_tags:
        # An attribute and its condition must coexist on the same modifier. This
        # prevents an unrelated tag elsewhere in a transitive perk graph from
        # turning into a fabricated compound claim.
        modifiers = [
            fact for fact in family["facts"] if fact["fact_type"] == "modifier"
        ]
        for attribute in required_attributes:
            if not any(
                fact["attribute"].casefold() == attribute
                and required_tags
                <= {
                    tag.casefold()
                    for values in fact["conditions"].values()
                    for tag in values
                }
                for fact in modifiers
            ):
                return False
    else:
        if not required_attributes <= _canonical(family["attributes"]):
            return False
        if not required_tags <= _canonical(family["tags"]):
            return False
    return _canonical(criteria["required_mechanics"]) <= _canonical(
        family["mechanics"]
    )


def _match_score(family: dict[str, Any], criteria: dict[str, list[str]]) -> int:
    return sum(
        len(_canonical(criteria[key]) & _canonical(family[field]))
        for key, field in (
            ("preferred_attributes", "attributes"),
            ("preferred_tags", "tags"),
            ("preferred_mechanics", "mechanics"),
        )
    )


def _matching_evidence(
    family: dict[str, Any], criteria: dict[str, list[str]]
) -> list[dict[str, Any]]:
    attributes = _canonical(
        criteria["required_attributes"] + criteria["preferred_attributes"]
    )
    tags = _canonical(criteria["required_tags"] + criteria["preferred_tags"])
    mechanics = _canonical(
        criteria["required_mechanics"] + criteria["preferred_mechanics"]
    )
    evidence = []
    for fact in family["facts"]:
        fact_tags: set[str] = set()
        if fact["fact_type"] == "modifier":
            for values in fact["conditions"].values():
                fact_tags.update(_canonical(values))
        elif fact["fact_type"] == "tag":
            fact_tags.add(fact["tag"].casefold())
        attribute_match = fact.get("attribute", "").casefold() in attributes
        if attribute_match and criteria["required_tags"]:
            attribute_match = _canonical(criteria["required_tags"]) <= fact_tags
        if (
            attribute_match
            or (fact.get("mechanic", "").casefold() in mechanics)
            or bool(fact_tags & tags)
        ):
            evidence.append(fact)
    return evidence


def search_perks(
    connection: sqlite3.Connection,
    *,
    attributes: Sequence[str] = (),
    tags: Sequence[str] = (),
    mechanics: Sequence[str] = (),
    prefer_attributes: Sequence[str] = (),
    prefer_tags: Sequence[str] = (),
    prefer_mechanics: Sequence[str] = (),
    snapshot_id: int | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    criteria = _criteria(
        attributes,
        tags,
        mechanics,
        prefer_attributes,
        prefer_tags,
        prefer_mechanics,
    )
    if not any(criteria.values()):
        raise ValueError("at least one required or preferred criterion is required")
    catalog = catalog or build_semantic_catalog(connection, snapshot_id)
    has_required = any(
        criteria[key]
        for key in ("required_attributes", "required_tags", "required_mechanics")
    )
    matches = [
        row
        for row in catalog["families"]
        if _matches(row, criteria)
        and (has_required or _match_score(row, criteria) > 0)
    ]
    ranked = sorted(
        matches,
        key=lambda row: (-_match_score(row, criteria), row["perk_family"].casefold()),
    )

    def render(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "perk_family": row["perk_family"],
            "semantic_status": row["status"],
            "match_score": _match_score(row, criteria),
            "matched_evidence": _matching_evidence(row, criteria),
            "assignments": row["assignments"],
            "reasons": row["reasons"],
        }

    accepted = [render(row) for row in ranked if row["optimization_ready"]]
    excluded = [render(row) for row in ranked if not row["optimization_ready"]]
    return {
        "snapshot_id": catalog["snapshot_id"],
        "criteria": criteria,
        "selection_rule": (
            "all required terms must exactly match normalized facts; preferred exact "
            "matches determine relevance order; magnitudes are evidence, never an "
            "invented cross-mechanic power score"
        ),
        "counts": {
            "optimization_ready_matches": len(accepted),
            "excluded_partial_or_opaque_matches": len(excluded),
        },
        "results": accepted,
        "excluded": excluded,
    }


def assemble_loadout(search: dict[str, Any], support_slots: int = 5) -> dict[str, Any]:
    if support_slots < 0:
        raise ValueError("support_slots cannot be negative")
    ready = search["results"]
    commander_family = next(
        (row for row in ready if row["assignments"].get("commander")), None
    )
    commander = None
    used_heroes: set[str] = set()
    if commander_family:
        choices = commander_family["assignments"]["commander"]
        chosen = choices[0]
        used_heroes.add(chosen["hero_key"])
        commander = {
            **chosen,
            "perk_family": commander_family["perk_family"],
            "match_score": commander_family["match_score"],
            "evidence": [
                fact
                for fact in commander_family["matched_evidence"]
                if chosen["perk_tier"] in fact["granting_tiers"]
            ],
            "equivalent_hero_choices": choices[1:],
        }
    supports = []
    for family in ready:
        if len(supports) >= support_slots:
            break
        if commander_family and family["perk_family"] == commander_family["perk_family"]:
            continue
        choices = [
            hero
            for hero in family["assignments"].get("support", [])
            if hero["hero_key"] not in used_heroes
        ]
        if not choices:
            continue
        chosen = choices[0]
        used_heroes.add(chosen["hero_key"])
        supports.append(
            {
                **chosen,
                "perk_family": family["perk_family"],
                "match_score": family["match_score"],
                "evidence": [
                    fact
                    for fact in family["matched_evidence"]
                    if chosen["perk_tier"] in fact["granting_tiers"]
                ],
                "equivalent_hero_choices": choices[1:],
            }
        )
    complete = commander is not None and len(supports) == support_slots
    return {
        "snapshot_id": search["snapshot_id"],
        "status": "complete" if complete else "incomplete",
        "criteria": search["criteria"],
        "commander": commander,
        "supports": supports,
        "requested_support_slots": support_slots,
        "unfilled_support_slots": support_slots - len(supports),
        "excluded_partial_or_opaque": search["excluded"],
        "explanation": (
            "This is a deterministic semantic-relevance assembly from exact asset "
            "facts. It is not a DPS simulation and does not model weapons, enemies, "
            "team perks, mission scaling, or opaque Blueprint behavior."
        ),
    }


def recommend_loadout(
    connection: sqlite3.Connection,
    *,
    attributes: Sequence[str] = (),
    tags: Sequence[str] = (),
    mechanics: Sequence[str] = (),
    prefer_attributes: Sequence[str] = (),
    prefer_tags: Sequence[str] = (),
    prefer_mechanics: Sequence[str] = (),
    support_slots: int = 5,
    snapshot_id: int | None = None,
) -> dict[str, Any]:
    catalog = build_semantic_catalog(connection, snapshot_id)
    search = search_perks(
        connection,
        attributes=attributes,
        tags=tags,
        mechanics=mechanics,
        prefer_attributes=prefer_attributes,
        prefer_tags=prefer_tags,
        prefer_mechanics=prefer_mechanics,
        catalog=catalog,
    )
    return assemble_loadout(search, support_slots)


def semantic_vocabulary(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    catalog = build_semantic_catalog(connection, snapshot_id)
    ready = [row for row in catalog["families"] if row["optimization_ready"]]
    return {
        "snapshot_id": catalog["snapshot_id"],
        "optimization_ready_perk_families": len(ready),
        "attributes": sorted({item for row in ready for item in row["attributes"]}),
        "mechanics": sorted({item for row in ready for item in row["mechanics"]}),
        "tags": sorted({item for row in ready for item in row["tags"]}),
    }


def _add_criteria(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attribute", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--mechanic", action="append", default=[])
    parser.add_argument("--prefer-attribute", action="append", default=[])
    parser.add_argument("--prefer-tag", action="append", default=[])
    parser.add_argument("--prefer-mechanic", action="append", default=[])
    parser.add_argument("--snapshot-id", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    vocabulary = commands.add_parser("vocabulary", help="list grounded query terms")
    vocabulary.add_argument("--snapshot-id", type=int)
    search = commands.add_parser("search", help="find optimization-ready perk families")
    _add_criteria(search)
    recommend = commands.add_parser(
        "recommend", help="assemble one evidence-only commander/support loadout"
    )
    _add_criteria(recommend)
    recommend.add_argument("--support-slots", type=int, default=5)
    return parser


def _criteria_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "attributes": args.attribute,
        "tags": args.tag,
        "mechanics": args.mechanic,
        "prefer_attributes": args.prefer_attribute,
        "prefer_tags": args.prefer_tag,
        "prefer_mechanics": args.prefer_mechanic,
        "snapshot_id": args.snapshot_id,
    }


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "vocabulary":
            payload = semantic_vocabulary(connection, args.snapshot_id)
        elif args.command == "search":
            payload = search_perks(connection, **_criteria_kwargs(args))
        else:
            payload = recommend_loadout(
                connection,
                support_slots=args.support_slots,
                **_criteria_kwargs(args),
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
