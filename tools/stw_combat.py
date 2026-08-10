#!/usr/bin/env python3
"""Evidence-gated deterministic combat evaluation for specified STW builds.

The evaluator intentionally produces catalog-stat damage units. It does not pretend that
weapon-level scaling, hero F.O.R.T. stats, target resistance, or opaque Blueprint procs
are known when those runtime rules are absent from the normalized asset graph.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from stw_assets import latest_asset_snapshot_id
from stw_pipeline import connect
from stw_runtime import crit_rating_to_chance, curve_lookup


SUPPORTED_OPERATIONS = {
    "EGameplayModOp::Additive",
    "EGameplayModOp::Multiplicitive",
}
RANGE_COLUMNS = {
    "point_blank": "damage_point_blank",
    "mid": "damage_mid",
    "long": "damage_long",
    "max": "damage_max_range",
}


@dataclass(frozen=True)
class WeaponPerkSelection:
    slot_ordinal: int
    alteration_key: str


@dataclass(frozen=True)
class WeaponConfiguration:
    variant_key: str
    perks: tuple[WeaponPerkSelection, ...] = ()
    item_level: int | None = None


@dataclass(frozen=True)
class LoadoutContext:
    commander: str | None = None
    support_heroes: tuple[str, ...] = ()
    source_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CombatScenario:
    range_band: str = "point_blank"
    target_element: str | None = None
    target_afflicted: bool | None = None
    target_tags: tuple[str, ...] = ()
    active_source_tags: tuple[str, ...] = ()
    health_fraction: float | None = None
    shield_fraction: float | None = None
    headshot: bool = False
    critical_hit: bool = False
    crit_probability: float | None = None
    window_seconds: float = 10.0
    window_mode: str = "sustained"
    effective_magazine_rounds: int | None = None
    effective_reload_seconds: float | None = None
    active_effects: tuple[str, ...] = ()


@dataclass
class EvaluationIssue:
    code: str
    message: str
    severity: str = "partial"
    origin: str | None = None


@dataclass
class CombatEvaluation:
    snapshot_id: int
    status: str
    weapon: dict[str, Any]
    scenario: dict[str, Any]
    loadout: dict[str, Any]
    attributes: dict[str, Any]
    metrics: dict[str, Any]
    contributions: list[dict[str, Any]]
    provenance: list[dict[str, Any]]
    assumptions: list[dict[str, Any]]
    issues: list[EvaluationIssue]
    elapsed_ms: float

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = [asdict(issue) for issue in self.issues]
        return result


@dataclass
class _EvaluationState:
    connection: sqlite3.Connection
    snapshot_id: int
    source_tags: set[str]
    target_tags: set[str]
    contributions: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    issues: list[EvaluationIssue] = field(default_factory=list)
    modifier_inputs: list[dict[str, Any]] = field(default_factory=list)
    _issue_keys: set[tuple[str, str | None]] = field(default_factory=set)
    _provenance_keys: set[tuple[Any, ...]] = field(default_factory=set)

    def issue(
        self, code: str, message: str, severity: str = "partial", origin: str | None = None
    ) -> None:
        key = (code, origin)
        if key in self._issue_keys:
            return
        self._issue_keys.add(key)
        self.issues.append(EvaluationIssue(code, message, severity, origin))

    def add_provenance(self, item: dict[str, Any]) -> None:
        key = (
            item.get("kind"),
            item.get("package_path"),
            item.get("object_name"),
            item.get("row_name"),
            item.get("content_sha256"),
        )
        if key not in self._provenance_keys:
            self._provenance_keys.add(key)
            self.provenance.append(item)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _all_tags(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, str) and "." in value:
        result.add(value)
    elif isinstance(value, list):
        for item in value:
            result.update(_all_tags(item))
    elif isinstance(value, dict):
        for item in value.values():
            result.update(_all_tags(item))
    return result


def _source_for_object(
    connection: sqlite3.Connection, object_id: int, kind: str
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT object.package_path, object.object_name, file.relative_path,
               file.content_sha256
        FROM asset_objects object
        JOIN asset_files file ON file.id=object.asset_file_id
        WHERE object.id=?
        """,
        (object_id,),
    ).fetchone()
    return {"kind": kind, **dict(row)} if row else {"kind": kind, "object_id": object_id}


def _tag_matches(actual: str, required: str) -> bool:
    actual_folded = actual.casefold()
    required_folded = required.casefold()
    return actual_folded == required_folded or actual_folded.startswith(
        required_folded + "."
    )


def _has_tag(tags: Iterable[str], required: str) -> bool:
    return any(_tag_matches(actual, required) for actual in tags)


def _conditions_apply(
    source_tags: set[str], target_tags: set[str], modifier: sqlite3.Row
) -> tuple[bool, dict[str, list[str]]]:
    conditions = {
        "source_required": _json(modifier["source_required_tags_json"], []),
        "source_ignored": _json(modifier["source_ignored_tags_json"], []),
        "target_required": _json(modifier["target_required_tags_json"], []),
        "target_ignored": _json(modifier["target_ignored_tags_json"], []),
    }
    active = (
        all(_has_tag(source_tags, tag) for tag in conditions["source_required"])
        and not any(_has_tag(source_tags, tag) for tag in conditions["source_ignored"])
        and all(_has_tag(target_tags, tag) for tag in conditions["target_required"])
        and not any(_has_tag(target_tags, tag) for tag in conditions["target_ignored"])
    )
    return active, conditions


def _curve_value(
    state: _EvaluationState,
    curve_row_id: int | None,
    level: float | None,
    origin: str,
) -> tuple[float | None, dict[str, Any] | None]:
    if curve_row_id is None:
        return 1.0, None
    rows = state.connection.execute(
        """
        SELECT point.time_value, point.output_value, point.interpolation,
               curve_row.row_name, curve_table.package_path,
               object.id AS source_object_id
        FROM catalog_curve_points point
        JOIN catalog_curve_rows curve_row ON curve_row.id=point.curve_row_id
        JOIN catalog_curve_tables curve_table ON curve_table.id=curve_row.curve_table_id
        JOIN asset_objects object ON object.id=curve_table.source_object_id
        WHERE point.curve_row_id=? ORDER BY point.point_ordinal
        """,
        (curve_row_id,),
    ).fetchall()
    if not rows:
        state.issue("missing_curve_points", "The referenced magnitude curve has no points.", origin=origin)
        return None, None
    curve_source = _source_for_object(
        state.connection, rows[0]["source_object_id"], "curve_table"
    ) | {"row_name": rows[0]["row_name"]}
    state.add_provenance(curve_source)
    if len(rows) == 1:
        return float(rows[0]["output_value"]), curve_source
    if level is None:
        state.issue(
            "missing_grant_level",
            "A multi-point scalable curve cannot be evaluated without the ability-set grant level.",
            origin=origin,
        )
        return None, curve_source
    for row in rows:
        if math.isclose(float(row["time_value"]), level, rel_tol=0.0, abs_tol=1e-9):
            return float(row["output_value"]), curve_source
    lower = None
    upper = None
    for row in rows:
        time_value = float(row["time_value"])
        if time_value < level:
            lower = row
        elif time_value > level:
            upper = row
            break
    if lower is None or upper is None:
        state.issue(
            "curve_extrapolation_not_modeled",
            f"Grant level {level:g} is outside the normalized curve point range.",
            origin=origin,
        )
        return None, curve_source
    interpolations = {str(lower["interpolation"]), str(upper["interpolation"])}
    if not all("RCIM_Linear" in value for value in interpolations):
        state.issue(
            "unsupported_curve_interpolation",
            f"Curve interpolation is not proven linear: {sorted(interpolations)}.",
            origin=origin,
        )
        return None, curve_source
    lower_time = float(lower["time_value"])
    upper_time = float(upper["time_value"])
    fraction = (level - lower_time) / (upper_time - lower_time)
    value = float(lower["output_value"]) + fraction * (
        float(upper["output_value"]) - float(lower["output_value"])
    )
    return value, curve_source


def _effect_chain_object_ids(
    connection: sqlite3.Connection, snapshot_id: int, root_object_id: int
) -> dict[int, int]:
    parents: dict[int, set[int]] = {}
    for row in connection.execute(
        """
        SELECT source_object_id, target_object_id FROM catalog_inheritance_edges
        WHERE snapshot_id=? AND target_object_id IS NOT NULL
        """,
        (snapshot_id,),
    ):
        parents.setdefault(row["source_object_id"], set()).add(row["target_object_id"])
    result = {root_object_id: 0}
    frontier = [root_object_id]
    while frontier:
        child = frontier.pop(0)
        for parent in parents.get(child, set()):
            if parent not in result:
                result[parent] = result[child] + 1
                frontier.append(parent)
    return result


def _kit_modifiers(
    state: _EvaluationState,
    ability_kit_id: int,
    origin_kind: str,
    origin_name: str,
    origin_details: dict[str, Any],
) -> int:
    grants = state.connection.execute(
        """
        SELECT grant_row.id, grant_row.grant_level,
               effect.source_object_id AS effect_object_id,
               effect.package_path AS granted_effect_path
        FROM catalog_ability_kit_grants grant_row
        JOIN catalog_gameplay_effects effect ON effect.id=grant_row.gameplay_effect_id
        WHERE grant_row.ability_kit_id=? AND grant_row.grant_kind='gameplay_effect'
        """,
        (ability_kit_id,),
    ).fetchall()
    found = 0
    for grant in grants:
        object_depths = _effect_chain_object_ids(
            state.connection, state.snapshot_id, grant["effect_object_id"]
        )
        object_ids = sorted(object_depths)
        placeholders = ",".join("?" for _ in object_ids)
        modifiers = state.connection.execute(
            f"""
            SELECT modifier.*, effect.source_object_id, effect.package_path,
                   object.object_name, file.relative_path, file.content_sha256
            FROM catalog_effect_modifiers modifier
            JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
            JOIN asset_objects object ON object.id=effect.source_object_id
            JOIN asset_files file ON file.id=object.asset_file_id
            WHERE effect.source_object_id IN ({placeholders})
            ORDER BY effect.package_path, modifier.modifier_ordinal
            """,
            object_ids,
        ).fetchall()
        if modifiers:
            nearest_depth = min(object_depths[row["source_object_id"]] for row in modifiers)
            modifiers = [
                row
                for row in modifiers
                if object_depths[row["source_object_id"]] == nearest_depth
            ]
        seen_modifier_ids: set[int] = set()
        for modifier in modifiers:
            if modifier["id"] in seen_modifier_ids:
                continue
            seen_modifier_ids.add(modifier["id"])
            found += 1
            modifier_origin = f"{origin_kind}:{origin_name}"
            effect_source = {
                "kind": "gameplay_effect",
                "package_path": modifier["package_path"],
                "object_name": modifier["object_name"],
                "relative_path": modifier["relative_path"],
                "content_sha256": modifier["content_sha256"],
            }
            state.add_provenance(effect_source)
            curve_factor, curve_source = _curve_value(
                state,
                modifier["curve_row_id"],
                grant["grant_level"],
                modifier_origin,
            )
            literal = modifier["literal_value"]
            magnitude = (
                float(literal if literal is not None else 1.0) * curve_factor
                if curve_factor is not None
                else None
            )
            active, conditions = _conditions_apply(
                state.source_tags, state.target_tags, modifier
            )
            contribution = {
                "origin_kind": origin_kind,
                "origin_name": origin_name,
                **origin_details,
                "attribute": modifier["attribute_name"],
                "operation": modifier["modifier_operation"],
                "evaluation_channel": modifier["evaluation_channel"],
                "magnitude": magnitude,
                "grant_level": grant["grant_level"],
                "active": active,
                "conditions": conditions,
                "effect": effect_source,
                "curve": curve_source,
            }
            state.contributions.append(contribution)
            if modifier["interpretation_status"] != "supported":
                state.issue(
                    "unsupported_modifier_magnitude",
                    "The catalog does not support this GameplayEffect modifier magnitude.",
                    origin=modifier_origin,
                )
            elif modifier["modifier_operation"] not in SUPPORTED_OPERATIONS:
                state.issue(
                    "unsupported_modifier_operation",
                    f"Modifier operation {modifier['modifier_operation']} is not evaluated.",
                    origin=modifier_origin,
                )
            elif modifier["evaluation_channel"] not in (None, "", "EGameplayModEvaluationChannel::Channel0"):
                state.issue(
                    "unsupported_evaluation_channel",
                    f"Evaluation channel {modifier['evaluation_channel']} is not modeled.",
                    origin=modifier_origin,
                )
            elif magnitude is not None and active:
                state.modifier_inputs.append(contribution)
    return found


def _load_variant(
    state: _EvaluationState, configuration: WeaponConfiguration
) -> sqlite3.Row:
    rows = state.connection.execute(
        """
        SELECT variant.*, identity.display_name, identity.weapon_kind,
               stat.*, variant.id AS variant_id,
               variant.source_object_id AS variant_source_object_id,
               stat.source_data_row_id AS stat_source_data_row_id,
               data_row.row_name AS stat_source_row_name,
               data_table.source_object_id AS stat_table_source_object_id
        FROM catalog_weapon_variants variant
        JOIN catalog_weapon_identities identity ON identity.id=variant.identity_id
        LEFT JOIN catalog_weapon_stats stat ON stat.weapon_variant_id=variant.id
        LEFT JOIN catalog_data_rows data_row ON data_row.id=stat.source_data_row_id
        LEFT JOIN catalog_data_tables data_table ON data_table.id=data_row.data_table_id
        WHERE variant.snapshot_id=? AND lower(variant.variant_key)=lower(?)
        """,
        (state.snapshot_id, configuration.variant_key),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"weapon variant must resolve exactly once: {configuration.variant_key!r}"
        )
    row = rows[0]
    state.source_tags.update(_all_tags(_json(row["tags_json"], [])))
    state.add_provenance(
        _source_for_object(state.connection, row["variant_source_object_id"], "weapon_variant")
    )
    if row["stat_table_source_object_id"] is not None:
        state.add_provenance(
            _source_for_object(
                state.connection, row["stat_table_source_object_id"], "weapon_stat_table"
            )
            | {"row_name": row["stat_source_row_name"]}
        )
    if row["interpretation_status"] != "supported":
        state.issue(
            "partial_weapon_variant",
            "The selected weapon variant lacks a fully resolved stat/slot chain.",
            origin=configuration.variant_key,
        )
    return row


def _resolve_weapon_perks(
    state: _EvaluationState,
    variant: sqlite3.Row,
    selections: Sequence[WeaponPerkSelection],
) -> list[dict[str, Any]]:
    if len({selection.slot_ordinal for selection in selections}) != len(selections):
        raise ValueError("weapon configuration selects more than one perk for a slot")
    result: list[dict[str, Any]] = []
    resolved: list[tuple[WeaponPerkSelection, sqlite3.Row]] = []
    for selection in selections:
        rows = state.connection.execute(
            """
            SELECT alteration.*, slot.slot_ordinal,
                   option.perk_rarity, alteration.source_object_id
            FROM catalog_weapon_slots slot
            JOIN catalog_weapon_slot_options option ON option.weapon_slot_id=slot.id
            JOIN catalog_alterations alteration ON alteration.id=option.alteration_id
            WHERE slot.slot_loadout_id=? AND slot.slot_ordinal=?
              AND lower(alteration.alteration_key)=lower(?)
            """,
            (variant["slot_loadout_id"], selection.slot_ordinal, selection.alteration_key),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"perk {selection.alteration_key!r} is not one exact option in slot "
                f"{selection.slot_ordinal} for {variant['variant_key']}"
            )
        resolved.append((selection, rows[0]))
    # Conditions are evaluated only after all selected alterations have contributed
    # their explicit include tags. This prevents slot order from changing a result.
    for _, alteration in resolved:
        state.source_tags.update(_all_tags(_json(alteration["tags_json"], [])))
    for selection, alteration in resolved:
        origin = f"slot {selection.slot_ordinal}:{alteration['alteration_key']}"
        source = _source_for_object(
            state.connection, alteration["source_object_id"], "weapon_alteration"
        )
        state.add_provenance(source)
        item = {
            "slot_ordinal": selection.slot_ordinal,
            "alteration_key": alteration["alteration_key"],
            "display_name": alteration["display_name"],
            "description": alteration["description"],
            "semantic_status": alteration["semantic_status"],
            "source": source,
        }
        result.append(item)
        if alteration["semantic_status"] != "supported":
            state.issue(
                "partial_or_opaque_weapon_perk",
                f"{alteration['display_name'] or alteration['alteration_key']} is "
                f"{alteration['semantic_status']}; its custom behavior is excluded.",
                origin=origin,
            )
        if alteration["ability_kit_id"] is not None:
            found = _kit_modifiers(
                state,
                alteration["ability_kit_id"],
                "weapon_perk",
                alteration["alteration_key"],
                {"slot_ordinal": selection.slot_ordinal},
            )
            if not found and alteration["semantic_status"] == "supported":
                state.issue(
                    "supported_perk_without_combat_modifier",
                    "The perk is structurally supported but exposes no directly evaluable combat modifier.",
                    origin=origin,
                )
    return sorted(result, key=lambda item: item["slot_ordinal"])


def _resolve_schematics(
    state: _EvaluationState, variant_id: int
) -> list[dict[str, Any]]:
    rows = state.connection.execute(
        """
        SELECT schematic.schematic_key, schematic.link_status,
               schematic.result_primary_asset_name, schematic.source_object_id
               , schematic.rating_curve_path, schematic.rating_row_name
        FROM catalog_schematics schematic
        WHERE schematic.snapshot_id=? AND schematic.weapon_variant_id=?
        ORDER BY schematic.schematic_key
        """,
        (state.snapshot_id, variant_id),
    ).fetchall()
    result = []
    for row in rows:
        source = _source_for_object(
            state.connection, row["source_object_id"], "weapon_schematic"
        )
        state.add_provenance(source)
        result.append(
            {
                "schematic_key": row["schematic_key"],
                "result_primary_asset_name": row["result_primary_asset_name"],
                "link_status": row["link_status"],
                "rating_row_name": row["rating_row_name"],
                "rating_curve_path": row["rating_curve_path"],
                "source": source,
            }
        )
    return result


def _resolve_hero(
    state: _EvaluationState, hero_name: str, role: str
) -> dict[str, Any]:
    rows = state.connection.execute(
        """
        SELECT hero.id AS hero_id, hero.display_name, hero.hero_key,
               hero.source_object_id, perk.perk_family, perk.perk_tier,
               perk.ability_kit_id, kit.source_object_id AS kit_source_object_id
        FROM catalog_heroes hero
        JOIN catalog_hero_perks assignment ON assignment.hero_id=hero.id
        JOIN catalog_perks perk ON perk.id=assignment.perk_id
        LEFT JOIN catalog_ability_kits kit ON kit.id=perk.ability_kit_id
        WHERE hero.snapshot_id=? AND lower(hero.display_name)=lower(?)
          AND assignment.perk_mode=?
        """,
        (state.snapshot_id, hero_name, role),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"{role} hero must resolve exactly once: {hero_name!r}")
    row = rows[0]
    hero_source = _source_for_object(
        state.connection, row["source_object_id"], "hero_gameplay_definition"
    )
    state.add_provenance(hero_source)
    if row["kit_source_object_id"] is not None:
        state.add_provenance(
            _source_for_object(state.connection, row["kit_source_object_id"], "hero_perk_kit")
        )
    origin_name = f"{row['display_name']} ({role})"
    if row["ability_kit_id"] is None:
        state.issue(
            "unresolved_hero_perk_kit",
            "The selected hero perk kit is unresolved.",
            origin=origin_name,
        )
    else:
        found = _kit_modifiers(
            state,
            row["ability_kit_id"],
            "hero_perk",
            origin_name,
            {"role": role, "perk_family": row["perk_family"], "perk_tier": row["perk_tier"]},
        )
        if not found:
            state.issue(
                "hero_perk_without_static_modifier",
                "The hero perk has no directly evaluable static GameplayEffect modifier.",
                origin=origin_name,
            )
    return {
        "display_name": row["display_name"],
        "role": role,
        "perk_family": row["perk_family"],
        "perk_tier": row["perk_tier"],
        "source": hero_source,
    }


def _aggregate_attributes(
    state: _EvaluationState, bases: dict[str, float]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for contribution in state.modifier_inputs:
        attribute = contribution.get("attribute")
        if attribute:
            grouped.setdefault(attribute, []).append(contribution)
    result: dict[str, dict[str, Any]] = {}
    for attribute in sorted(set(bases) | set(grouped)):
        base = float(bases.get(attribute, 0.0))
        additive = 0.0
        multiplicative_delta = 0.0
        applied: list[dict[str, Any]] = []
        for contribution in grouped.get(attribute, []):
            operation = contribution["operation"]
            magnitude = float(contribution["magnitude"])
            if operation == "EGameplayModOp::Additive":
                additive += magnitude
            elif operation == "EGameplayModOp::Multiplicitive":
                multiplicative_delta += magnitude - 1.0
            else:
                continue
            applied.append(
                {
                    "origin_kind": contribution["origin_kind"],
                    "origin_name": contribution["origin_name"],
                    "operation": operation,
                    "magnitude": magnitude,
                }
            )
        multiplier = 1.0 + multiplicative_delta
        value = (base + additive) * multiplier
        result[attribute] = {
            "base": base,
            "additive_total": additive,
            "multiplicative_bucket": multiplier,
            "value": value,
            "applied": applied,
            "rule": "unreal_gas_default_channel_bias_aggregation",
        }
    return result


def _validate_inputs(
    configuration: WeaponConfiguration,
    loadout: LoadoutContext,
    scenario: CombatScenario,
) -> None:
    if scenario.range_band not in RANGE_COLUMNS:
        raise ValueError(f"unsupported range band: {scenario.range_band}")
    if scenario.window_mode not in {"burst", "sustained"}:
        raise ValueError("window_mode must be 'burst' or 'sustained'")
    if scenario.window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    for name, value in (
        ("health_fraction", scenario.health_fraction),
        ("shield_fraction", scenario.shield_fraction),
        ("crit_probability", scenario.crit_probability),
    ):
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
    if scenario.critical_hit and scenario.crit_probability is not None:
        raise ValueError("critical_hit and crit_probability are mutually exclusive")
    if len(loadout.support_heroes) > 5:
        raise ValueError("at most five support heroes are allowed")
    names = [name.casefold() for name in loadout.support_heroes]
    if len(names) != len(set(names)):
        raise ValueError("support heroes must be unique")
    if loadout.commander and loadout.commander.casefold() in names:
        raise ValueError("the commander cannot also occupy a support slot")
    if configuration.item_level is not None and configuration.item_level < 1:
        raise ValueError("item_level must be positive")


def evaluate_combat(
    connection: sqlite3.Connection,
    configuration: WeaponConfiguration,
    loadout: LoadoutContext,
    scenario: CombatScenario,
    snapshot_id: int | None = None,
) -> CombatEvaluation:
    started = time.perf_counter()
    _validate_inputs(configuration, loadout, scenario)
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no asset snapshot is available")
    target_tags = set(scenario.target_tags)
    if scenario.target_afflicted is True:
        target_tags.add("Gameplay.Status.Afflicted")
    state = _EvaluationState(
        connection=connection,
        snapshot_id=snapshot_id,
        source_tags=set(loadout.source_tags) | set(scenario.active_source_tags),
        target_tags=target_tags,
    )
    variant = _load_variant(state, configuration)
    schematics = _resolve_schematics(state, variant["variant_id"])
    perk_rows = _resolve_weapon_perks(state, variant, configuration.perks)

    commander = (
        _resolve_hero(state, loadout.commander, "commander")
        if loadout.commander
        else None
    )
    supports = [
        _resolve_hero(state, hero, "support") for hero in loadout.support_heroes
    ]

    raw_stats = _json(variant["raw_stats_json"], {})
    item_rating = None
    if configuration.item_level is not None:
        rating_rows = {
            schematic["rating_row_name"]
            for schematic in schematics
            if schematic["rating_row_name"]
        }
        if len(rating_rows) == 1:
            item_rating, rating_source, rating_error = curve_lookup(
                connection,
                snapshot_id,
                next(iter(rating_rows)),
                configuration.item_level,
                next(
                    (
                        schematic["rating_curve_path"]
                        for schematic in schematics
                        if schematic["rating_row_name"] in rating_rows
                    ),
                    None,
                ),
            )
            if rating_source:
                state.add_provenance(rating_source)
            if rating_error:
                state.issue(
                    "item_rating_lookup_unavailable",
                    f"Item rating lookup could not be evaluated: {rating_error}.",
                )
        else:
            state.issue(
                "item_rating_row_unavailable",
                "The linked schematic does not expose one exact item-rating curve row.",
            )
    bases = {
        "OutgoingAbilityDamage": 1.0,
        "WeaponRateOfFire": float(variant["fire_rate"] or 0.0),
        "WeaponAmmoClipSize": float(variant["magazine_size"] or 0.0),
        "WeaponReloadSpeed": 1.0,
        "DiceCritMultiplier": float(variant["crit_damage_bonus"] or 0.0),
        "ZoneCritMultiplier": float(raw_stats.get("DamageZone_Critical", 1.0)),
        "CritRating": 0.0,
    }
    attributes = _aggregate_attributes(state, bases)

    damage_base = variant[RANGE_COLUMNS[scenario.range_band]]
    if damage_base is None:
        state.issue(
            "missing_range_damage",
            f"The selected weapon has no {scenario.range_band} damage value.",
            severity="unsupported",
        )
        damage_base = 0.0
    damage_multiplier = attributes["OutgoingAbilityDamage"]["value"]
    body_noncritical = float(damage_base) * damage_multiplier
    crit_bonus = attributes["DiceCritMultiplier"]["value"]
    crit_multiplier = 1.0 + crit_bonus
    headshot_multiplier = attributes["ZoneCritMultiplier"]["value"]
    profiles = {
        "body_noncritical": body_noncritical,
        "body_critical": body_noncritical * crit_multiplier,
        "headshot_noncritical": body_noncritical * headshot_multiplier,
        "headshot_critical": body_noncritical * headshot_multiplier * crit_multiplier,
    }

    crit_rating = attributes["CritRating"]["value"]
    base_crit_chance = float(variant["crit_chance"] or 0.0)
    crit_rating_bonus = None
    if crit_rating != 0.0:
        crit_rating_bonus, crit_source, crit_error = crit_rating_to_chance(
            connection, snapshot_id, crit_rating
        )
        if crit_source:
            state.add_provenance(crit_source)
        if crit_error:
            state.issue(
                "crit_rating_curve_unavailable",
                f"CritRating lookup could not be evaluated: {crit_error}.",
            )
    crit_probability = scenario.crit_probability
    if crit_probability is not None:
        state.assumptions.append(
            {
                "code": "explicit_crit_probability",
                "value": crit_probability,
                "reason": "The scenario supplies the effective runtime probability.",
            }
        )
    elif crit_rating != 0.0:
        state.issue(
            "crit_chance_combination_rule_unavailable",
            "The CritRating-to-chance curve is proven, but its native combination with DiceCritChance is not.",
        )
    else:
        crit_probability = base_crit_chance
    expected_body = (
        body_noncritical * (1.0 + crit_probability * crit_bonus)
        if crit_probability is not None
        else None
    )
    expected_headshot = (
        expected_body * headshot_multiplier if expected_body is not None else None
    )

    if scenario.crit_probability is not None:
        configured_damage = expected_headshot if scenario.headshot else expected_body
    else:
        profile = (
            "headshot_critical"
            if scenario.headshot and scenario.critical_hit
            else "headshot_noncritical"
            if scenario.headshot
            else "body_critical"
            if scenario.critical_hit
            else "body_noncritical"
        )
        configured_damage = profiles[profile]

    fire_rate = attributes["WeaponRateOfFire"]["value"]
    raw_magazine = attributes["WeaponAmmoClipSize"]["value"]
    if scenario.effective_magazine_rounds is not None:
        magazine_rounds = scenario.effective_magazine_rounds
        state.assumptions.append(
            {
                "code": "explicit_effective_magazine",
                "value": magazine_rounds,
                "reason": "The scenario supplies the observed integer capacity.",
            }
        )
    elif math.isclose(raw_magazine, round(raw_magazine), abs_tol=1e-9):
        magazine_rounds = int(round(raw_magazine))
    else:
        magazine_rounds = None
        state.issue(
            "magazine_rounding_rule_unavailable",
            f"The modified clip attribute is {raw_magazine:g}; Fortnite's integer rounding rule is not proven.",
        )

    reload_speed = attributes["WeaponReloadSpeed"]["value"]
    base_reload = float(variant["reload_time"] or 0.0)
    if scenario.effective_reload_seconds is not None:
        reload_seconds = scenario.effective_reload_seconds
        state.assumptions.append(
            {
                "code": "explicit_effective_reload",
                "value": reload_seconds,
                "reason": "The scenario supplies the observed effective reload duration.",
            }
        )
    elif math.isclose(reload_speed, 1.0, abs_tol=1e-9):
        reload_seconds = base_reload
    else:
        reload_seconds = None
        state.issue(
            "reload_speed_time_rule_unavailable",
            f"The catalog proves WeaponReloadSpeed={reload_speed:g}, but not the runtime conversion to seconds.",
        )

    burst_dps = configured_damage * fire_rate if configured_damage is not None else None
    sustained_dps = None
    magazine_damage = None
    if configured_damage is not None and magazine_rounds is not None:
        magazine_damage = configured_damage * magazine_rounds
        if fire_rate > 0 and reload_seconds is not None:
            cycle_seconds = magazine_rounds / fire_rate + reload_seconds
            sustained_dps = magazine_damage / cycle_seconds
    selected_dps = burst_dps if scenario.window_mode == "burst" else sustained_dps
    window_damage = None
    if selected_dps is not None:
        if (
            scenario.window_mode == "burst"
            and magazine_rounds is not None
            and fire_rate > 0
            and scenario.window_seconds > magazine_rounds / fire_rate
        ):
            state.issue(
                "burst_window_exceeds_magazine",
                "The requested burst window exceeds one magazine; no reload-free window damage was reported.",
            )
        else:
            window_damage = selected_dps * scenario.window_seconds

    state.assumptions.extend(
        [
            {
                "code": "gas_default_channel_aggregation",
                "reason": "Default-channel additive and multiplicative GameplayEffect modifiers use Unreal GAS bias aggregation.",
            },
            {
                "code": "analytic_fire_rate_metrics",
                "reason": "Burst and sustained metrics use stat-table shots/second throughput, not frame-level animation simulation.",
            },
        ]
    )
    state.issue(
        "live_damage_scaling_not_evaluated",
        "Results are catalog-stat damage units; weapon item-level scaling, hero F.O.R.T. offense, and mission/target scaling are not yet proven.",
    )
    if configuration.item_level is not None:
        state.issue(
            "item_level_formula_unavailable",
            f"Item level {configuration.item_level} was specified, but DmgScale application is not proven and was not applied.",
        )
    if scenario.target_element is not None:
        state.issue(
            "element_matchup_rule_unavailable",
            f"Target element {scenario.target_element!r} is recorded but no elemental resistance multiplier was applied.",
        )
    if scenario.active_effects:
        state.issue(
            "named_active_effects_not_resolved",
            "Named active effects are preserved in the scenario but require exact tags/mechanics before application.",
        )

    severities = {issue.severity for issue in state.issues}
    status = "unsupported" if "unsupported" in severities else "partial" if state.issues else "supported"
    metrics = {
        "unit": "catalog_weapon_stat_damage",
        "range_band": scenario.range_band,
        "base_damage": float(damage_base),
        "item_rating": item_rating,
        "damage_profiles": profiles,
        "base_crit_chance": base_crit_chance,
        "crit_rating": crit_rating,
        "crit_rating_bonus_chance": crit_rating_bonus,
        "effective_crit_probability": crit_probability,
        "expected_body_damage": expected_body,
        "expected_headshot_damage": expected_headshot,
        "configured_damage_per_shot": configured_damage,
        "fire_rate_per_second": fire_rate,
        "raw_modified_magazine_attribute": raw_magazine,
        "effective_magazine_rounds": magazine_rounds,
        "base_reload_seconds": base_reload,
        "weapon_reload_speed_attribute": reload_speed,
        "effective_reload_seconds": reload_seconds,
        "burst_dps": burst_dps,
        "magazine_damage": magazine_damage,
        "sustained_dps": sustained_dps,
        "window_mode": scenario.window_mode,
        "window_seconds": scenario.window_seconds,
        "analytic_window_damage": window_damage,
        "live_target_damage": None,
    }
    weapon = {
        "variant_key": variant["variant_key"],
        "primary_asset_name": variant["primary_asset_name"],
        "display_name": variant["display_name"],
        "weapon_kind": variant["weapon_kind"],
        "tier": variant["tier"],
        "rarity": variant["rarity"],
        "base_level": variant["base_level"],
        "dmg_scale": variant["damage_scale"],
        "source_tags": sorted(state.source_tags, key=str.casefold),
        "schematics": schematics,
        "selected_perks": perk_rows,
    }
    return CombatEvaluation(
        snapshot_id=snapshot_id,
        status=status,
        weapon=weapon,
        scenario=asdict(scenario),
        loadout={"commander": commander, "support_heroes": supports},
        attributes=attributes,
        metrics=metrics,
        contributions=state.contributions,
        provenance=state.provenance,
        assumptions=state.assumptions,
        issues=state.issues,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def nocturno_demonstration(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    variant = "WID_Assault_Auto_Founders_SR_Ore_T05"
    signature = WeaponPerkSelection(5, "aid_g_weapon_onreload_explode")
    configurations = (
        (
            "afflicted_damage",
            WeaponConfiguration(
                variant,
                (
                    WeaponPerkSelection(0, "aid_att_damage_t05"),
                    WeaponPerkSelection(1, "aid_att_critdamage_t05"),
                    WeaponPerkSelection(2, "aid_ele_energy_t05"),
                    WeaponPerkSelection(3, "aid_att_firerate_ranged_t05"),
                    WeaponPerkSelection(4, "aid_conditional_afflicted_dmgbonus_t05"),
                    signature,
                ),
            ),
            LoadoutContext(),
            CombatScenario(target_afflicted=True, window_mode="burst", window_seconds=1.0),
        ),
        (
            "critical_hit",
            WeaponConfiguration(
                variant,
                (
                    WeaponPerkSelection(0, "aid_att_damage_t05"),
                    WeaponPerkSelection(1, "aid_att_critchance_t05"),
                    WeaponPerkSelection(2, "aid_ele_energy_t05"),
                    WeaponPerkSelection(3, "aid_att_critdamage_t05"),
                    WeaponPerkSelection(4, "aid_att_critdamage_t05"),
                    signature,
                ),
            ),
            LoadoutContext(),
            CombatScenario(critical_hit=True, window_mode="burst", window_seconds=1.0),
        ),
        (
            "rescue_trooper_commander",
            WeaponConfiguration(
                variant,
                (
                    WeaponPerkSelection(0, "aid_att_damage_t05"),
                    WeaponPerkSelection(1, "aid_att_critdamage_t05"),
                    WeaponPerkSelection(2, "aid_ele_energy_t05"),
                    WeaponPerkSelection(3, "aid_att_firerate_ranged_t05"),
                    WeaponPerkSelection(4, "aid_conditional_afflicted_dmgbonus_t05"),
                    signature,
                ),
            ),
            LoadoutContext(commander="Rescue Trooper Ramirez"),
            CombatScenario(target_afflicted=True, window_mode="burst", window_seconds=1.0),
        ),
    )
    results = []
    for name, configuration, loadout, scenario in configurations:
        evaluation = evaluate_combat(
            connection, configuration, loadout, scenario, snapshot_id
        )
        results.append({"name": name, "evaluation": evaluation.as_dict()})
    return {"snapshot_id": results[0]["evaluation"]["snapshot_id"], "results": results}


def _perk_argument(value: str) -> WeaponPerkSelection:
    try:
        slot, alteration = value.split(":", 1)
        return WeaponPerkSelection(int(slot), alteration)
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("perk must be SLOT:alteration_key") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate", help="evaluate one exact weapon/loadout scenario")
    evaluate.add_argument("--variant", required=True)
    evaluate.add_argument("--item-level", type=int)
    evaluate.add_argument("--perk", type=_perk_argument, action="append", default=[])
    evaluate.add_argument("--commander")
    evaluate.add_argument("--support", action="append", default=[])
    evaluate.add_argument("--range-band", choices=sorted(RANGE_COLUMNS), default="point_blank")
    evaluate.add_argument("--target-afflicted", action="store_true")
    evaluate.add_argument("--target-tag", action="append", default=[])
    evaluate.add_argument("--headshot", action="store_true")
    evaluate.add_argument("--critical-hit", action="store_true")
    evaluate.add_argument("--crit-probability", type=float)
    evaluate.add_argument("--window-seconds", type=float, default=10.0)
    evaluate.add_argument("--window-mode", choices=("burst", "sustained"), default="sustained")
    evaluate.add_argument("--effective-magazine-rounds", type=int)
    evaluate.add_argument("--effective-reload-seconds", type=float)
    subparsers.add_parser("nocturno-demo", help="run three real Nocturno vertical slices")
    args = parser.parse_args(argv)
    connection = connect(args.db)
    try:
        if args.command == "nocturno-demo":
            result = nocturno_demonstration(connection)
        else:
            result = evaluate_combat(
                connection,
                WeaponConfiguration(args.variant, tuple(args.perk), args.item_level),
                LoadoutContext(args.commander, tuple(args.support)),
                CombatScenario(
                    range_band=args.range_band,
                    target_afflicted=True if args.target_afflicted else None,
                    target_tags=tuple(args.target_tag),
                    headshot=args.headshot,
                    critical_hit=args.critical_hit,
                    crit_probability=args.crit_probability,
                    window_seconds=args.window_seconds,
                    window_mode=args.window_mode,
                    effective_magazine_rounds=args.effective_magazine_rounds,
                    effective_reload_seconds=args.effective_reload_seconds,
                ),
            ).as_dict()
    finally:
        connection.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
