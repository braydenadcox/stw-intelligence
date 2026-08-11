#!/usr/bin/env python3
"""Scenario-bound deterministic STW loadout search and explanation engine."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from stw_assets import latest_asset_snapshot_id
from stw_combat import (
    CombatScenario,
    LoadoutContext,
    WeaponConfiguration,
    WeaponPerkSelection,
    evaluate_combat,
)
from stw_context import MissionContext, TargetContext, scenario_report
from stw_pipeline import connect


OBJECTIVES = (
    "burst_damage", "sustained_damage", "crowd_clear",
    "mist_monster_boss", "survivability", "healing_sustain",
    "crowd_control", "ability_uptime", "weapon_uptime",
    "condition_reliability",
)

OBJECTIVE_TERMS = {
    "burst_damage": ("damage", "crit", "headshot", "firerate", "weapon.ranged"),
    "sustained_damage": ("damage", "firerate", "reload", "magazine", "ammo"),
    "crowd_clear": ("area", "aoe", "chain", "explode", "explosion", "afflict", "ondeath"),
    "mist_monster_boss": ("boss", "mist", "smasher", "taker", "blaster", "flinger", "damage"),
    "survivability": ("health", "shield", "armor", "resist", "fortitude", "damage reduction"),
    "healing_sustain": ("heal", "healing", "regen", "lifesteal", "lifeleech", "health"),
    "crowd_control": ("stun", "snare", "slow", "freeze", "knockback", "impact", "control"),
    "ability_uptime": ("cooldown", "ability", "duration", "charge", "energy"),
    "weapon_uptime": ("reload", "magazine", "clip", "ammo", "firerate", "durability"),
    "condition_reliability": ("chance", "duration", "stack", "trigger", "condition", "always"),
}

TIER_ORDER = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
RARITY_ORDER = {
    "Common": 1, "Uncommon": 2, "Rare": 3, "Epic": 4,
    "Legendary": 5, "Mythic": 6,
}


@dataclass(frozen=True)
class HeroProgression:
    tier: str = "EFortItemTier::V"
    level: int = 50
    rarity: str = "EFortRarity::Legendary"


@dataclass(frozen=True)
class OptimizationRequest:
    weapon: str
    target: TargetContext
    mission: MissionContext
    objective_weights: tuple[tuple[str, float], ...]
    combat_scenario: CombatScenario = CombatScenario()
    hero_progression: HeroProgression = HeroProgression()
    support_slots: int = 5
    gadget_slots: int = 2
    beam_width: int = 128
    max_results: int = 10
    item_level: int | None = None

    def weights(self) -> dict[str, float]:
        weights = dict(self.objective_weights)
        unknown = set(weights) - set(OBJECTIVES)
        if unknown:
            raise ValueError(f"unsupported optimization objectives: {sorted(unknown)}")
        if not weights or any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
            raise ValueError("objective weights must contain at least one positive value")
        total = sum(weights.values())
        return {key: value / total for key, value in weights.items() if value > 0}


@dataclass
class SearchStats:
    theoretical_hero_loadouts: int = 0
    weapon_configurations: int = 0
    team_perk_checks: int = 0
    team_perk_eligible: int = 0
    gadget_combinations: int = 0
    heuristic_candidates: int = 0
    evaluated_candidates: int = 0
    evaluator_cache_hits: int = 0
    evaluator_cache_misses: int = 0
    pruned_by_beam: int = 0
    deduplicated_candidates: int = 0
    pareto_dominated: int = 0


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _flatten_text(value: Any) -> str:
    if isinstance(value, str): return value.casefold()
    if isinstance(value, Mapping): return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)): return " ".join(_flatten_text(item) for item in value)
    return ""


def _objective_scores(value: Any) -> dict[str, float]:
    text = _flatten_text(value)
    return {
        objective: float(sum(term in text for term in terms))
        for objective, terms in OBJECTIVE_TERMS.items()
    }


def _weighted(profile: Mapping[str, Any], weights: Mapping[str, float]) -> float:
    scores = profile.get("objective_scores", {})
    return sum(weights.get(key, 0.0) * float(scores.get(key, 0.0)) for key in weights)


def _source_for_object(connection: sqlite3.Connection, object_id: int) -> dict[str, Any]:
    row = connection.execute("""
        SELECT object.package_path, object.object_name, file.relative_path, file.content_sha256
        FROM asset_objects object JOIN asset_files file ON file.id=object.asset_file_id
        WHERE object.id=?
    """, (object_id,)).fetchone()
    return dict(row) if row else {"object_id": object_id}


def _kit_profile(connection: sqlite3.Connection, snapshot_id: int,
                 ability_kit_id: int) -> dict[str, Any]:
    source_ids = {connection.execute(
        "SELECT source_object_id FROM catalog_ability_kits WHERE id=?", (ability_kit_id,)
    ).fetchone()[0]}
    unresolved = 0
    for row in connection.execute("""
        SELECT grant.ability_id, grant.gameplay_effect_id,
               ability.source_object_id AS ability_source,
               effect.source_object_id AS effect_source
        FROM catalog_ability_kit_grants grant
        LEFT JOIN catalog_abilities ability ON ability.id=grant.ability_id
        LEFT JOIN catalog_gameplay_effects effect ON effect.id=grant.gameplay_effect_id
        WHERE grant.ability_kit_id=?
    """, (ability_kit_id,)):
        target = row["ability_source"] or row["effect_source"]
        if target: source_ids.add(target)
        elif row["ability_id"] is None and row["gameplay_effect_id"] is None: unresolved += 1
    placeholders = ",".join("?" for _ in source_ids)
    mechanics = [dict(row) for row in connection.execute(f"""
        SELECT mechanic_type, interpretation_status, conditions_json, value_json,
               source_object_id FROM catalog_mechanics
        WHERE snapshot_id=? AND source_object_id IN ({placeholders})
    """, (snapshot_id, *source_ids))]
    modifiers = [dict(row) for row in connection.execute(f"""
        SELECT modifier.attribute_name, modifier.modifier_operation,
               modifier.interpretation_status, effect.source_object_id,
               modifier.source_required_tags_json, modifier.target_required_tags_json
        FROM catalog_effect_modifiers modifier
        JOIN catalog_gameplay_effects effect ON effect.id=modifier.gameplay_effect_id
        WHERE effect.snapshot_id=? AND effect.source_object_id IN ({placeholders})
    """, (snapshot_id, *source_ids))]
    tags = [row[0] for row in connection.execute(f"""
        SELECT DISTINCT tag.tag_name FROM catalog_gameplay_tag_occurrences occurrence
        JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
        WHERE occurrence.source_object_id IN ({placeholders})
    """, tuple(source_ids))]
    opaque_count = connection.execute(f"""
        SELECT COUNT(*) FROM catalog_opaque_mechanics
        WHERE snapshot_id=? AND source_object_id IN ({placeholders})
    """, (snapshot_id, *source_ids)).fetchone()[0]
    supported = sum(row["interpretation_status"] == "supported" for row in mechanics + modifiers)
    incomplete = unresolved + opaque_count + sum(row["interpretation_status"] != "supported" for row in mechanics + modifiers)
    status = "partial" if supported and incomplete else "supported" if supported else "opaque"
    sources = [_source_for_object(connection, object_id) for object_id in sorted(source_ids)]
    value = {"mechanics": mechanics, "modifiers": modifiers, "tags": tags}
    return {"semantic_status": status, "objective_scores": _objective_scores(value),
            "evidence": value, "sources": sources, "unresolved_grants": unresolved,
            "opaque_count": opaque_count}


def _hero_profiles(connection: sqlite3.Connection, snapshot_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_tags: dict[int, list[str]] = {}
    for row in connection.execute("""
        SELECT occurrence.source_object_id, tag.tag_name
        FROM catalog_gameplay_tag_occurrences occurrence
        JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
        JOIN asset_objects object ON object.id=occurrence.source_object_id
        WHERE object.snapshot_id=?
    """, (snapshot_id,)):
        source_tags.setdefault(row["source_object_id"], []).append(row["tag_name"])
    result = {"commander": [], "support": []}
    kit_cache: dict[int, dict[str, Any]] = {}
    for row in connection.execute("""
        SELECT hero.hero_key, hero.display_name, hero.hero_class, hero.source_object_id,
               assignment.perk_mode, perk.perk_family, perk.perk_tier, perk.ability_kit_id
        FROM catalog_heroes hero
        JOIN catalog_hero_perks assignment ON assignment.hero_id=hero.id
        JOIN catalog_perks perk ON perk.id=assignment.perk_id
        WHERE hero.snapshot_id=? AND perk.ability_kit_id IS NOT NULL
        ORDER BY hero.hero_key, assignment.perk_mode
    """, (snapshot_id,)):
        role = row["perk_mode"]
        if role not in result: continue
        if row["ability_kit_id"] not in kit_cache:
            kit_cache[row["ability_kit_id"]] = _kit_profile(
                connection, snapshot_id, row["ability_kit_id"]
            )
        profile = kit_cache[row["ability_kit_id"]]
        tags = sorted(set(source_tags.get(row["source_object_id"], [])) | {
            f"Hero.Class.{row['hero_class']}"})
        result[role].append({
            "hero_key": row["hero_key"], "display_name": row["display_name"],
            "hero_class": row["hero_class"], "perk_tier": row["perk_tier"],
            "role": role, "perk_family": row["perk_family"],
            "semantic_status": profile["semantic_status"],
            "optimization_ready": profile["semantic_status"] == "supported",
            "reasons": (["direct perk graph contains unresolved/opaque mechanics"]
                        if profile["semantic_status"] != "supported" else []),
            "objective_scores": profile["objective_scores"], "tags": tags,
            "evidence": profile["evidence"],
            "source": _source_for_object(connection, row["source_object_id"]),
        })
    return result["commander"], result["support"]


def _resolve_weapon_variants(connection: sqlite3.Connection, snapshot_id: int, query: str) -> list[sqlite3.Row]:
    rows = connection.execute("""
        SELECT variant.*, identity.display_name, identity.weapon_kind,
               stat.base_level AS weapon_base_level
        FROM catalog_weapon_variants variant
        JOIN catalog_weapon_identities identity ON identity.id=variant.identity_id
        LEFT JOIN catalog_weapon_stats stat ON stat.weapon_variant_id=variant.id
        WHERE variant.snapshot_id=? AND variant.slot_loadout_id IS NOT NULL AND
          (lower(variant.variant_key)=lower(?) OR lower(identity.display_name)=lower(?)
           OR lower(identity.display_name) LIKE lower(?))
        ORDER BY COALESCE(stat.base_level,0) DESC, variant.variant_key
    """, (snapshot_id, query, query, f"%{query}%")).fetchall()
    if not rows:
        raise ValueError(f"weapon preference resolved no catalog variants: {query!r}")
    exact = [row for row in rows if row["variant_key"].casefold() == query.casefold()]
    if exact: return exact
    tiers = [TIER_ORDER.get(str(row["tier"]).split("::")[-1], 0) for row in rows]
    if tiers:
        maximum = max(tiers)
        rows = [row for row in rows
                if TIER_ORDER.get(str(row["tier"]).split("::")[-1], 0) == maximum]
    return rows[:12]


def _weapon_option_profiles(connection: sqlite3.Connection, slot_loadout_id: int) -> list[list[dict[str, Any]]]:
    slots = []
    for slot in connection.execute("""
        SELECT * FROM catalog_weapon_slots WHERE slot_loadout_id=? ORDER BY slot_ordinal
    """, (slot_loadout_id,)):
        options = []
        for row in connection.execute("""
            SELECT option.*, alteration.alteration_key, alteration.display_name,
                   alteration.description, alteration.tags_json, alteration.semantic_status,
                   alteration.source_object_id
            FROM catalog_weapon_slot_options option
            JOIN catalog_alterations alteration ON alteration.id=option.alteration_id
            WHERE option.weapon_slot_id=? ORDER BY option.option_ordinal
        """, (slot["id"],)):
            value = {"slot_ordinal": slot["slot_ordinal"], "alteration_key": row["alteration_key"],
                     "primary_name": row["alteration_primary_asset_name"],
                     "exclusions": _json(row["exclusion_names_json"], []),
                     "semantic_status": row["semantic_status"],
                     "objective_scores": _objective_scores(dict(row)),
                     "perk_rarity": row["perk_rarity"],
                     "source": _source_for_object(connection, row["source_object_id"])}
            options.append(value)
        if options: slots.append(options)
    return slots


def _compatible_option(selected: Sequence[dict[str, Any]], option: dict[str, Any]) -> bool:
    names = {item["primary_name"].casefold() for item in selected if item["primary_name"]}
    exclusions = {str(item).casefold() for item in option["exclusions"]}
    if names & exclusions: return False
    option_name = (option["primary_name"] or "").casefold()
    return not any(option_name in {str(value).casefold() for value in item["exclusions"]}
                   for item in selected)


def _weapon_configurations(connection: sqlite3.Connection, variants: Sequence[sqlite3.Row],
                           weights: Mapping[str, float], beam_width: int,
                           item_level: int | None) -> tuple[list[dict[str, Any]], int, int]:
    configurations: list[dict[str, Any]] = []
    theoretical = pruned = 0
    seen_loadouts: set[int] = set()
    for variant in variants:
        # Variants sharing one slot loadout have identical legal perk configurations;
        # retain only the highest-level representative for search.
        if variant["slot_loadout_id"] in seen_loadouts: continue
        seen_loadouts.add(variant["slot_loadout_id"])
        slots = _weapon_option_profiles(connection, variant["slot_loadout_id"])
        theoretical += math.prod(len(slot) for slot in slots) if slots else 1
        beam: list[tuple[list[dict[str, Any]], float]] = [([], 0.0)]
        for options in slots:
            expanded = []
            for selected, score in beam:
                for option in options:
                    if _compatible_option(selected, option):
                        rarity = _enum_value(option["perk_rarity"], "", RARITY_ORDER) or 0
                        expanded.append((selected + [option], score + _weighted(option, weights) + rarity * 1e-4))
            expanded.sort(key=lambda item: (-item[1], tuple(x["alteration_key"] for x in item[0])))
            pruned += max(0, len(expanded) - beam_width)
            beam = expanded[:beam_width]
        for selected, score in beam:
            configurations.append({
                "configuration": WeaponConfiguration(
                    variant["variant_key"],
                    tuple(WeaponPerkSelection(item["slot_ordinal"], item["alteration_key"])
                          for item in selected), item_level),
                "variant": {"variant_key": variant["variant_key"], "display_name": variant["display_name"],
                            "weapon_kind": variant["weapon_kind"]},
                "profiles": selected, "heuristic": score,
            })
    configurations.sort(key=lambda item: (-item["heuristic"], item["configuration"].variant_key))
    return configurations[:beam_width], theoretical, pruned


def _hero_beam(commanders: Sequence[dict[str, Any]], supports: Sequence[dict[str, Any]],
               weights: Mapping[str, float], support_slots: int, beam_width: int,
               stats: SearchStats) -> list[dict[str, Any]]:
    ranked_commanders = sorted(commanders, key=lambda item: (-_weighted(item, weights), item["hero_key"]))[:max(24, beam_width // 2)]
    ranked_supports = sorted(supports, key=lambda item: (-_weighted(item, weights), item["hero_key"]))[:max(48, beam_width)]
    beam = [{"commander": commander, "supports": [], "used": {commander["hero_key"]},
             "heuristic": _weighted(commander, weights)} for commander in ranked_commanders]
    for _ in range(support_slots):
        expanded = []
        for state in beam:
            for support in ranked_supports:
                if support["hero_key"] in state["used"]: continue
                expanded.append({"commander": state["commander"],
                    "supports": state["supports"] + [support],
                    "used": state["used"] | {support["hero_key"]},
                    "heuristic": state["heuristic"] + _weighted(support, weights)})
        expanded.sort(key=lambda item: (-item["heuristic"], item["commander"]["hero_key"],
                                        tuple(x["hero_key"] for x in item["supports"])))
        # Preserve commander diversity at every depth. A plain global beam quickly
        # collapses into cosmetic variations of one high-token commander before the
        # combat evaluator has a chance to compare their actual numeric effects.
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in expanded:
            groups.setdefault(item["commander"]["hero_key"], []).append(item)
        diverse = []
        rank = 0
        ordered_groups = sorted(groups.values(), key=lambda rows: -rows[0]["heuristic"])
        while len(diverse) < beam_width and any(rank < len(rows) for rows in ordered_groups):
            for rows in ordered_groups:
                if rank < len(rows):
                    diverse.append(rows[rank])
                    if len(diverse) >= beam_width: break
            rank += 1
        stats.pruned_by_beam += max(0, len(expanded) - len(diverse))
        beam = diverse
    return beam


def _enum_value(value: str | None, prefix: str, order: Mapping[str, int]) -> int | None:
    if not value: return None
    tail = value.split("::")[-1].removeprefix(prefix)
    return order.get(tail)


def _team_perk_eligible(report: Mapping[str, Any], supports: Sequence[dict[str, Any]],
                        progression: HeroProgression) -> tuple[bool, str, list[str]]:
    reasons = []
    if report["eligibility"]["status"] != "supported":
        return False, "partial", ["team-perk eligibility is not fully interpreted"]
    hero_tier = _enum_value(progression.tier, "", TIER_ORDER)
    hero_rarity = _enum_value(progression.rarity, "", RARITY_ORDER)
    for rule in report["eligibility"]["rules"]:
        if rule["status"] != "supported": return False, "partial", ["eligibility rule is partial"]
        required = set(rule["required_tags"] + rule["required_class_tags"] + rule["required_keyword_tags"])
        matching = sum(all(any(tag == expected or tag.startswith(expected + ".") for tag in hero["tags"])
                           for expected in required) for hero in supports)
        if matching < rule["required_count"]:
            reasons.append(f"requires {rule['required_count']} supports matching {sorted(required)}; found {matching}")
        minimum_tier = _enum_value(rule["tier"]["minimum"], "", TIER_ORDER)
        maximum_tier = _enum_value(rule["tier"]["maximum"], "", TIER_ORDER)
        minimum_rarity = _enum_value(rule["rarity"]["minimum"], "", RARITY_ORDER)
        maximum_rarity = _enum_value(rule["rarity"]["maximum"], "", RARITY_ORDER)
        if minimum_tier and (hero_tier is None or hero_tier < minimum_tier): reasons.append("hero tier below minimum")
        if maximum_tier and (hero_tier is None or hero_tier > maximum_tier): reasons.append("hero tier above maximum")
        if rule["level"]["minimum"] and progression.level < rule["level"]["minimum"]: reasons.append("hero level below minimum")
        if rule["level"]["maximum"] and progression.level > rule["level"]["maximum"]: reasons.append("hero level above maximum")
        if minimum_rarity and (hero_rarity is None or hero_rarity < minimum_rarity): reasons.append("hero rarity below minimum")
        if maximum_rarity and (hero_rarity is None or hero_rarity > maximum_rarity): reasons.append("hero rarity above maximum")
    return not reasons, "supported", reasons


def _interaction_profiles(connection: sqlite3.Connection, snapshot_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    team = []
    kit_cache: dict[int, dict[str, Any]] = {}
    for row in connection.execute("SELECT * FROM catalog_team_perks WHERE snapshot_id=? ORDER BY team_perk_key", (snapshot_id,)):
        if row["ability_kit_id"] not in kit_cache:
            kit_cache[row["ability_kit_id"]] = _kit_profile(
                connection, snapshot_id, row["ability_kit_id"])
        semantics = kit_cache[row["ability_kit_id"]]
        rules = []
        for rule in connection.execute("SELECT * FROM catalog_team_perk_eligibility_rules WHERE team_perk_id=? ORDER BY rule_ordinal", (row["id"],)):
            rules.append({"required_count": rule["required_count"], "status": rule["interpretation_status"],
                "required_tags": _json(rule["required_tags_json"], []),
                "required_class_tags": _json(rule["required_class_tags_json"], []),
                "required_keyword_tags": _json(rule["required_keyword_tags_json"], []),
                "tier": {"minimum": rule["minimum_hero_tier"] if rule["consider_minimum_tier"] else None,
                         "maximum": rule["maximum_hero_tier"] if rule["consider_maximum_tier"] else None},
                "level": {"minimum": rule["minimum_hero_level"] if rule["consider_minimum_level"] else None,
                          "maximum": rule["maximum_hero_level"] if rule["consider_maximum_level"] else None},
                "rarity": {"minimum": rule["minimum_hero_rarity"] if rule["consider_minimum_rarity"] else None,
                           "maximum": rule["maximum_hero_rarity"] if rule["consider_maximum_rarity"] else None}})
        report = {"eligibility": {"status": row["eligibility_status"], "rules": rules}}
        value = {"description": row["effect_description"], "semantics": semantics["evidence"]}
        direct_status = row["semantic_status"]
        status = "opaque" if "opaque" in {direct_status, semantics["semantic_status"]} else (
            "partial" if "partial" in {direct_status, semantics["semantic_status"]} else "supported")
        team.append({"key": row["team_perk_key"], "display_name": row["display_name"],
                     "semantic_status": status,
                     "objective_scores": _objective_scores(value), "report": report,
                     "source": _source_for_object(connection, row["source_object_id"])})
    gadgets = []
    for row in connection.execute("SELECT * FROM catalog_gadgets WHERE snapshot_id=? ORDER BY gadget_key", (snapshot_id,)):
        if row["ability_kit_id"] not in kit_cache:
            kit_cache[row["ability_kit_id"]] = _kit_profile(
                connection, snapshot_id, row["ability_kit_id"]
            )
        semantics = kit_cache[row["ability_kit_id"]]
        value = {"display_name": row["display_name"], "semantics": semantics["evidence"]}
        direct_status = row["semantic_status"]
        status = "opaque" if "opaque" in {direct_status, semantics["semantic_status"]} else (
            "partial" if "partial" in {direct_status, semantics["semantic_status"]} else "supported")
        gadgets.append({"key": row["gadget_key"], "display_name": row["display_name"],
                        "semantic_status": status,
                        "objective_scores": _objective_scores(value),
                        "source": _source_for_object(connection, row["source_object_id"])})
    return team, gadgets


def _active_abilities(connection: sqlite3.Connection, snapshot_id: int, hero_key: str) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT DISTINCT ability.active_ability_key, ability.display_name,
               ability.semantic_status, ability.source_object_id
        FROM catalog_active_abilities ability
        JOIN catalog_active_ability_grants grant_row ON grant_row.active_ability_id=ability.id
        LEFT JOIN catalog_heroes hero ON hero.id=grant_row.hero_id
        LEFT JOIN catalog_hero_classes class_row ON class_row.id=grant_row.hero_class_id
        JOIN catalog_heroes selected ON selected.snapshot_id=ability.snapshot_id
        WHERE ability.snapshot_id=? AND selected.hero_key=? AND
          ((grant_row.grant_domain='hero_loadout' AND hero.id=selected.id) OR
           (grant_row.grant_domain='hero_class' AND class_row.id=selected.hero_class_id))
        ORDER BY ability.active_ability_key
    """, (snapshot_id, hero_key)).fetchall()
    return [{"identity": {"active_ability_key": row["active_ability_key"],
                           "display_name": row["display_name"],
                           "source": _source_for_object(connection, row["source_object_id"])},
             "semantics": {"status": row["semantic_status"]}} for row in rows]


def _candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (candidate["commander"]["hero_key"],
            tuple(sorted(hero["hero_key"] for hero in candidate["supports"])),
            candidate["team_perk"]["key"] if candidate.get("team_perk") else None,
            tuple(sorted(gadget["key"] for gadget in candidate["gadgets"])),
            candidate["weapon"]["configuration"])


def _profile_components(candidate: Mapping[str, Any], weights: Mapping[str, float]) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]]]:
    profiles = [candidate["commander"], *candidate["supports"], *candidate["weapon"]["profiles"]]
    if candidate.get("team_perk"): profiles.append(candidate["team_perk"])
    profiles.extend(candidate["gadgets"])
    supported = Counter()
    partial, opaque = [], []
    for profile in profiles:
        for objective in weights:
            score = float(profile.get("objective_scores", {}).get(objective, 0.0))
            if score <= 0: continue
            status = profile.get("semantic_status", "partial")
            item = {"objective": objective, "owner": profile.get("display_name") or profile.get("alteration_key") or profile.get("perk_family"),
                    "evidence_units": score, "semantic_status": status}
            if status == "supported": supported[objective] += score
            elif status == "opaque": opaque.append(item | {"unquantified": True})
            else: partial.append(item | {"unquantified": True})
    return dict(supported), partial, opaque


def _render_candidate(connection: sqlite3.Connection, snapshot_id: int, candidate: dict[str, Any],
                      request: OptimizationRequest, context: dict[str, Any],
                      evaluation_cache: dict[Any, Any], stats: SearchStats) -> dict[str, Any]:
    loadout = LoadoutContext(
        commander=candidate["commander"]["hero_key"],
        support_heroes=tuple(hero["hero_key"] for hero in candidate["supports"]),
        team_perk=candidate["team_perk"]["key"] if candidate.get("team_perk") else None,
        gadgets=tuple(gadget["key"] for gadget in candidate["gadgets"]),
    )
    combat = replace(request.combat_scenario,
        target_element=request.target.element,
        target_tags=tuple(sorted(set(request.combat_scenario.target_tags) | set(context["effective_target_tags"]))))
    cache_key = (candidate["weapon"]["configuration"], loadout, combat, snapshot_id)
    if cache_key in evaluation_cache:
        stats.evaluator_cache_hits += 1
        evaluation = evaluation_cache[cache_key]
    else:
        stats.evaluator_cache_misses += 1
        evaluation = evaluate_combat(connection, candidate["weapon"]["configuration"], loadout, combat, snapshot_id)
        evaluation_cache[cache_key] = evaluation
    semantic, partial, opaque = _profile_components(candidate, request.weights())
    raw_components = dict(semantic)
    if evaluation.metrics.get("burst_dps") is not None: raw_components["burst_damage"] = evaluation.metrics["burst_dps"]
    if evaluation.metrics.get("sustained_dps") is not None: raw_components["sustained_damage"] = evaluation.metrics["sustained_dps"]
    if evaluation.metrics.get("burst_dps") and evaluation.metrics.get("sustained_dps") is not None:
        raw_components["weapon_uptime"] = evaluation.metrics["sustained_dps"] / evaluation.metrics["burst_dps"]
    relevant_issue_codes = {
        "partial_or_opaque_weapon_perk", "partial_or_opaque_team_perk",
        "partial_or_opaque_gadget", "unsupported_modifier_magnitude",
        "unsupported_modifier_operation", "named_active_effects_not_resolved",
    }
    for issue in evaluation.issues:
        if issue.code in relevant_issue_codes:
            partial.append({"owner": issue.origin, "issue": issue.message,
                            "severity": issue.severity, "unquantified": True})
    abilities = _active_abilities(connection, snapshot_id, candidate["commander"]["hero_key"])
    if request.weights().get("ability_uptime"):
        for ability in abilities:
            status = ability["semantics"]["status"]
            if status != "supported":
                (opaque if status == "opaque" else partial).append({
                    "owner": ability["identity"]["display_name"], "objective": "ability_uptime",
                    "semantic_status": status, "unquantified": True})
    provenance = list(evaluation.provenance)
    seen = {(item.get("package_path"), item.get("content_sha256")) for item in provenance}
    profiles_with_sources = [candidate["commander"], *candidate["supports"], *candidate["gadgets"]]
    if candidate.get("team_perk"): profiles_with_sources.append(candidate["team_perk"])
    profiles_with_sources.extend({"source": ability["identity"]["source"]} for ability in abilities)
    for profile in profiles_with_sources:
        source = profile.get("source") or profile.get("report", {}).get("identity", {}).get("source")
        if source and (source.get("package_path"), source.get("content_sha256")) not in seen:
            provenance.append(source); seen.add((source.get("package_path"), source.get("content_sha256")))
    return {
        "commander": {key: candidate["commander"][key] for key in ("hero_key", "display_name", "perk_family")},
        "support_heroes": [{key: hero[key] for key in ("hero_key", "display_name", "perk_family")} for hero in candidate["supports"]],
        "team_perk": ({"key": candidate["team_perk"]["key"], "display_name": candidate["team_perk"]["display_name"]} if candidate.get("team_perk") else None),
        "weapon": evaluation.weapon, "gadgets": [{"key": gadget["key"], "display_name": gadget["display_name"]} for gadget in candidate["gadgets"]],
        "active_abilities": [{"key": ability["identity"]["active_ability_key"], "display_name": ability["identity"]["display_name"],
                              "semantic_status": ability["semantics"]["status"]} for ability in abilities],
        "scenario": {"target": asdict(request.target), "mission": asdict(request.mission), "combat": asdict(combat)},
        "objective_weights": request.weights(), "raw_supported_components": raw_components,
        "partial_components": partial, "opaque_components": opaque,
        "comparison_class": "definitive" if not partial and not opaque else "uncertainty_aware",
        "confidence": "high" if not partial and not opaque else "low" if opaque else "medium",
        "key_synergies": [item for item in evaluation.contributions if item.get("active")],
        "limiting_conditions": [asdict(issue) for issue in evaluation.issues] + context["modifier_evaluations"],
        "combat_evaluation": evaluation.as_dict(), "provenance": provenance,
    }


def _normalize_and_rank(candidates: list[dict[str, Any]], weights: Mapping[str, float]) -> None:
    bounds = {}
    for objective in weights:
        values = [item["raw_supported_components"].get(objective) for item in candidates]
        values = [float(value) for value in values if value is not None]
        bounds[objective] = (min(values), max(values)) if values else None
    for item in candidates:
        components = {}
        total = 0.0
        for objective, weight in weights.items():
            value = item["raw_supported_components"].get(objective)
            bound = bounds[objective]
            if value is None or bound is None:
                components[objective] = {"status": "unavailable", "weight": weight}
                continue
            low, high = bound
            normalized = 100.0 if math.isclose(low, high) else 100.0 * (float(value) - low) / (high - low)
            weighted = normalized * weight
            components[objective] = {"status": "supported", "raw_value": value,
                                     "normalized_score": normalized, "weight": weight,
                                     "weighted_score": weighted}
            total += weighted
        item["supported_score_components"] = components
        item["supported_weighted_score"] = total


def _mark_pareto(candidates: list[dict[str, Any]], weights: Mapping[str, float]) -> int:
    dominated_count = 0
    objectives = tuple(weights)
    for candidate in candidates:
        values = [candidate["raw_supported_components"].get(key) for key in objectives]
        dominated = False
        for other in candidates:
            if other is candidate or other["comparison_class"] != candidate["comparison_class"]:
                continue
            other_values = [other["raw_supported_components"].get(key) for key in objectives]
            if all(left is not None and right is not None for left, right in zip(values, other_values)):
                if all(right >= left for left, right in zip(values, other_values)) and any(
                    right > left for left, right in zip(values, other_values)
                ):
                    dominated = True
                    break
        candidate["pareto_dominated"] = dominated
        dominated_count += int(dominated)
    return dominated_count


def optimize_loadouts(connection: sqlite3.Connection, request: OptimizationRequest,
                      snapshot_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    weights = request.weights()
    if not 0 <= request.support_slots <= 5 or not 0 <= request.gadget_slots <= 2:
        raise ValueError("support slots must be 0..5 and gadget slots must be 0..2")
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None: raise ValueError("no ready asset snapshot")
    stats = SearchStats()
    variants = _resolve_weapon_variants(connection, snapshot_id, request.weapon)
    weapon_configs, theoretical_weapons, weapon_pruned = _weapon_configurations(
        connection, variants, weights, request.beam_width, request.item_level)
    stats.weapon_configurations = theoretical_weapons
    stats.pruned_by_beam += weapon_pruned
    commanders, supports = _hero_profiles(connection, snapshot_id)
    if request.support_slots and len(supports) < request.support_slots:
        raise ValueError("catalog does not contain enough support heroes")
    stats.theoretical_hero_loadouts = len(commanders) * (
        math.comb(max(0, len(supports) - 1), request.support_slots)
        if len(supports) - 1 >= request.support_slots else 0)
    hero_states = _hero_beam(commanders, supports, weights, request.support_slots,
                             request.beam_width, stats)
    team_profiles, gadget_profiles = _interaction_profiles(connection, snapshot_id)
    expanded = []
    for state in hero_states:
        if not team_profiles:
            expanded.append(state | {"team_perk": None})
            continue
        eligible = []
        for team in team_profiles:
            stats.team_perk_checks += 1
            legal, eligibility_status, reasons = _team_perk_eligible(
                team["report"], state["supports"], request.hero_progression)
            if legal:
                stats.team_perk_eligible += 1
                eligible.append(team | {"eligibility_status": eligibility_status,
                                        "eligibility_reasons": reasons})
        choice_count = max(1, min(4, request.beam_width // max(1, len(hero_states))))
        for team in sorted(eligible, key=lambda item: (-_weighted(item, weights), item["key"]))[:choice_count]:
            expanded.append(state | {"team_perk": team,
                "heuristic": state["heuristic"] + _weighted(team, weights)})
    expanded.sort(key=lambda item: -item["heuristic"])
    stats.pruned_by_beam += max(0, len(expanded) - request.beam_width)
    expanded = expanded[:request.beam_width]
    gadget_sets = list(itertools.combinations(gadget_profiles, request.gadget_slots)) if request.gadget_slots else [()]
    stats.gadget_combinations = len(gadget_sets)
    with_gadgets = []
    gadget_choice_count = max(1, min(8, request.beam_width // max(1, len(expanded))))
    ranked_gadgets = sorted(gadget_sets, key=lambda group: -sum(_weighted(item, weights) for item in group))[:gadget_choice_count]
    for state in expanded:
        for gadgets in ranked_gadgets:
            with_gadgets.append(state | {"gadgets": list(gadgets),
                "heuristic": state["heuristic"] + sum(_weighted(item, weights) for item in gadgets)})
    with_gadgets.sort(key=lambda item: -item["heuristic"])
    stats.pruned_by_beam += max(0, len(with_gadgets) - request.beam_width)
    with_gadgets = with_gadgets[:request.beam_width]
    combined = []
    weapon_choice_count = max(1, request.beam_width // max(1, len(with_gadgets)))
    for state in with_gadgets:
        for weapon in weapon_configs[:weapon_choice_count]:
            combined.append(state | {"weapon": weapon,
                "heuristic": state["heuristic"] + weapon["heuristic"]})
    combined.sort(key=lambda item: -item["heuristic"])
    stats.heuristic_candidates = len(combined)
    stats.pruned_by_beam += max(0, len(combined) - request.beam_width)
    deduplicated = []
    seen = set()
    for candidate in combined:
        key = _candidate_key(candidate)
        if key in seen:
            stats.deduplicated_candidates += 1; continue
        seen.add(key); deduplicated.append(candidate)
        if len(deduplicated) >= request.beam_width: break
    context = scenario_report(connection, snapshot_id, request.target, request.mission)
    cache: dict[Any, Any] = {}
    rendered = [_render_candidate(connection, snapshot_id, candidate, request, context, cache, stats)
                for candidate in deduplicated]
    stats.evaluated_candidates = len(rendered)
    _normalize_and_rank(rendered, weights)
    stats.pareto_dominated = _mark_pareto(rendered, weights)
    definitive = sorted((item for item in rendered if item["comparison_class"] == "definitive"),
                        key=lambda item: (item["pareto_dominated"], -item["supported_weighted_score"]))
    uncertain = sorted((item for item in rendered if item["comparison_class"] != "definitive"),
                       key=lambda item: (item["pareto_dominated"], -item["supported_weighted_score"]))
    semantic_statuses = Counter(
        "opaque" if item["opaque_components"] else "partial"
        if item["partial_components"] else "supported" for item in rendered
    )
    return {
        "snapshot_id": snapshot_id, "request": {**asdict(request), "normalized_weights": weights},
        "scenario_resolution": context,
        "search_space": {**asdict(stats),
            "upper_bound_complete_combinations": stats.theoretical_hero_loadouts * max(1, theoretical_weapons)
                * max(1, len(team_profiles)) * max(1, stats.gadget_combinations)},
        "strategy": ["early exact weapon/slot legality filtering", "semantic objective compatibility profiles",
                     "team-perk requirement pruning", "bounded hero/component beam search",
                     "candidate deduplication", "memoized combat evaluation", "status-partitioned ranking"],
        "definitive_rankings": definitive[:request.max_results],
        "uncertainty_aware_recommendations": uncertain[:request.max_results],
        "counts": {"definitive": len(definitive), "uncertainty_aware": len(uncertain),
                   "supported": semantic_statuses["supported"],
                   "partial": semantic_statuses["partial"], "opaque": semantic_statuses["opaque"],
                   "evaluated": len(rendered)},
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def _weights(value: str) -> tuple[tuple[str, float], ...]:
    result = []
    for item in value.split(","):
        name, separator, raw = item.partition("=")
        result.append((name.strip(), float(raw) if separator else 1.0))
    return tuple(result)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    parser.add_argument("--weapon", required=True)
    parser.add_argument("--enemy", required=True)
    parser.add_argument("--objective", required=True, help="comma list such as burst_damage=2,crowd_clear=1")
    parser.add_argument("--mission")
    parser.add_argument("--mission-modifier", action="append", default=[])
    parser.add_argument("--element")
    parser.add_argument("--elemental-storm")
    parser.add_argument("--four-player", action="store_true")
    parser.add_argument("--range", default="point_blank", choices=("point_blank", "mid", "long", "max"))
    parser.add_argument("--beam-width", type=int, default=128)
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--item-level", type=int)
    args = parser.parse_args(argv)
    request = OptimizationRequest(
        weapon=args.weapon,
        target=TargetContext(args.enemy, args.element),
        mission=MissionContext(args.mission, four_player=args.four_player,
                               elemental_storm=args.elemental_storm,
                               modifier_keys=tuple(args.mission_modifier)),
        objective_weights=_weights(args.objective),
        combat_scenario=CombatScenario(range_band=args.range),
        beam_width=args.beam_width, max_results=args.max_results, item_level=args.item_level,
    )
    connection = connect(args.db)
    try: print(json.dumps(optimize_loadouts(connection, request), indent=2, sort_keys=True))
    finally: connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
