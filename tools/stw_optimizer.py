#!/usr/bin/env python3
"""Scenario-bound deterministic STW loadout search and explanation engine."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
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
class OptimizationConstraints:
    """User/inventory constraints applied before heuristic search."""

    owned_heroes: tuple[str, ...] = ()
    unavailable_heroes: tuple[str, ...] = ()
    locked_commander: str | None = None
    locked_supports: tuple[str, ...] = ()
    locked_team_perk: str | None = None
    owned_team_perks: tuple[str, ...] = ()
    locked_gadgets: tuple[str, ...] = ()
    owned_gadgets: tuple[str, ...] = ()
    allow_partial: bool = True
    allow_opaque: bool = True
    avoid_mechanics: tuple[str, ...] = ()
    locked_weapon_perks: tuple[tuple[int, str], ...] = ()
    owned_weapons: tuple[str, ...] = ()
    unavailable_weapons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptimizationRequest:
    weapon: str | None
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
    constraints: OptimizationConstraints = OptimizationConstraints()
    diagnostics: bool = False

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
    scores = profile.get("supported_potential_scores", {})
    return sum(weights.get(key, 0.0) * float(scores.get(key, 0.0)) for key in weights)


ATTRIBUTE_OBJECTIVES = {
    "stuntime": ("crowd_control",),
    "outgoingbaseimpactdamage": ("crowd_control",),
    "knockbackmagnitude": ("crowd_control",),
    "movement": ("crowd_control",),
    "movementspeed": ("crowd_control",),
    "health": ("survivability",),
    "shield": ("survivability",),
    "armor": ("survivability",),
    "damageresistance": ("survivability",),
    "healing": ("healing_sustain",),
    "healthregen": ("healing_sustain",),
    "shieldregen": ("healing_sustain",),
    "abilitycooldown": ("ability_uptime",),
    "cooldown": ("ability_uptime",),
    "weaponreloadspeed": ("weapon_uptime",),
    "weaponammoclipsize": ("weapon_uptime",),
    "weaponrateoffire": ("weapon_uptime", "sustained_damage"),
    "outgoingweapondamage": ("burst_damage", "sustained_damage"),
    # Ability damage is not interchangeable with weapon burst/sustained DPS.
    # Keep it visible as evidence until an ability evaluator can produce its
    # own scenario-bound metric; never feed it into the weapon evaluator.
    "outgoingabilitydamage": (),
}


def _tag_matches(actual: str, required: str) -> bool:
    left, right = actual.casefold(), required.casefold()
    return left == right or left.startswith(right + ".")


def _has_requirement(tags: Iterable[str], required: str) -> bool:
    return any(_tag_matches(tag, required) for tag in tags)


def _ability_requirement(required: str) -> bool:
    folded = required.casefold()
    return "abilitygroup" in folded or "abilityeffect" in folded


def _ability_matches(abilities: Iterable[str], required: str) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", required.casefold())
    ignored = {"asset", "ability", "abilitygroup", "abilityeffect", "hero", "damage"}
    segments = [
        segment for segment in re.split(r"[^a-z0-9]+", required.casefold())
        if len(segment) >= 5 and segment not in ignored
    ]
    normalized = [re.sub(r"[^a-z0-9]", "", item.casefold()) for item in abilities]
    return any(segment in ability or ability in folded for segment in segments for ability in normalized)


def _event_applies(event: str, context: Mapping[str, Any]) -> tuple[bool, str]:
    folded = event.casefold()
    excluded = {str(item).casefold().replace("_", " ") for item in context.get("excluded_events", ())}
    if any(term in folded for term in ("kill", "death", "eliminat")):
        allowed = not any("elimination" in item or "kill" in item for item in excluded)
        return allowed, "eliminations permitted" if allowed else "eliminations excluded"
    if "event.weapons." in folded:
        return bool(context.get("weapon_present")), "selected weapon"
    if "abilit" in folded:
        abilities = context.get("active_abilities", ())
        if ".hero.activate" in folded:
            return bool(abilities), "commander has active abilities"
        applies = _ability_matches(abilities, event)
        return applies, "matching commander ability" if applies else "no matching commander ability"
    if _has_requirement(context.get("source_tags", ()), event):
        return True, "event explicitly established by candidate/scenario"
    return False, "event is not established by candidate/scenario"


def _profile_evidence(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = profile.get("evidence")
    if isinstance(evidence, Mapping): return evidence
    semantics = profile.get("semantic_facts", {}).get("semantics")
    return semantics if isinstance(semantics, Mapping) else {}


def _supported_potential_scores(evidence: Mapping[str, Any]) -> dict[str, float]:
    scores: Counter[str] = Counter()
    for modifier in evidence.get("modifiers", ()):
        if modifier.get("interpretation_status") != "supported": continue
        raw = _modifier_value(modifier)
        if raw is None: continue
        for objective in _modifier_objectives(modifier):
            scores[objective] += raw
    return dict(scores)


def _modifier_value(modifier: Mapping[str, Any]) -> float | None:
    if modifier.get("curve_row_name") or "curve" in str(
        modifier.get("magnitude_kind") or ""
    ).casefold():
        return None
    literal = modifier.get("literal_value")
    if literal is None: return None
    value = float(literal)
    if modifier.get("modifier_operation") == "EGameplayModOp::Multiplicitive":
        return abs(value - 1.0)
    return abs(value)


def _modifier_objectives(modifier: Mapping[str, Any]) -> tuple[str, ...]:
    attribute = str(modifier.get("attribute_name") or "").casefold()
    if attribute != "outgoingabilitydamage":
        return ATTRIBUTE_OBJECTIVES.get(attribute, ())
    # Fortnite uses this shared attribute for multiple damage domains. The
    # required gameplay tags establish whether this particular modifier is a
    # weapon modifier. Ability-bound and unscoped instances cannot be folded
    # into weapon DPS without a separate runtime calculation.
    required = [
        *map(str, _json(modifier.get("source_required_tags_json"), [])),
        *map(str, _json(modifier.get("target_required_tags_json"), [])),
    ]
    if any(tag.casefold().startswith("weapon.") for tag in required):
        return ("burst_damage", "sustained_damage")
    return ()


def _applicability_trace(
    profile: Mapping[str, Any], context: Mapping[str, Any],
) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return supported numeric potential, audit rows, and unresolved mechanics."""
    evidence = _profile_evidence(profile)
    source_tags = set(context.get("source_tags", ()))
    target_tags = set(context.get("target_tags", ()))
    abilities = set(context.get("active_abilities", ()))
    mechanics = list(evidence.get("mechanics", ()))
    triggers = []
    unresolved = []
    for mechanic in mechanics:
        if mechanic.get("interpretation_status") != "supported":
            unresolved.append({
                "source": profile.get("display_name") or profile.get("perk_family"),
                "mechanic": mechanic.get("mechanic_type"), "status": mechanic.get("interpretation_status"),
                "reason": "mechanic is not fully interpreted", "numeric_credit": 0.0,
            })
            continue
        if mechanic.get("mechanic_type") == "trigger":
            triggers.extend(re.findall(r"Event\.[A-Za-z0-9_.]+", str(mechanic.get("value_json") or "")))
    event_results = [_event_applies(event, context) for event in triggers]
    event_blocked = any(not applies for applies, _ in event_results)
    scores: Counter[str] = Counter()
    traces = []
    for modifier in evidence.get("modifiers", ()):
        attribute = str(modifier.get("attribute_name") or "")
        objectives = _modifier_objectives(modifier)
        required_source = _json(modifier.get("source_required_tags_json"), [])
        ignored_source = _json(modifier.get("source_ignored_tags_json"), [])
        required_target = _json(modifier.get("target_required_tags_json"), [])
        ignored_target = _json(modifier.get("target_ignored_tags_json"), [])
        requirements = [
            *({"kind": "source_tag", "value": value} for value in required_source),
            *({"kind": "target_tag", "value": value} for value in required_target),
            *({"kind": "event", "value": value} for value in triggers),
        ]
        satisfied_by = [reason for applies, reason in event_results if applies]
        applicable = not event_blocked
        for requirement in required_source:
            if _has_requirement(source_tags, requirement):
                satisfied_by.append(requirement)
            elif _ability_requirement(requirement) and _ability_matches(abilities, requirement):
                satisfied_by.append(f"commander ability matching {requirement}")
            else:
                applicable = False
        for requirement in required_target:
            if _has_requirement(target_tags, requirement): satisfied_by.append(requirement)
            else: applicable = False
        if any(_has_requirement(source_tags, value) for value in ignored_source): applicable = False
        if any(_has_requirement(target_tags, value) for value in ignored_target): applicable = False
        status = str(modifier.get("interpretation_status") or "partial")
        raw = _modifier_value(modifier) if status == "supported" and applicable else None
        if not objectives: raw = None
        trace = {
            "source": profile.get("display_name") or profile.get("alteration_key") or profile.get("perk_family"),
            "mechanic": attribute or "unclassified modifier",
            "requirement": requirements,
            "requirement_satisfied_by": satisfied_by,
            "applicable": applicable,
            "raw_value": raw,
            "objective_mapping": list(objectives),
            "semantic_status": status,
            "weighted_score_contribution": 0.0,
            "credited": False,
        }
        traces.append(trace)
        if raw is not None:
            for objective in objectives: scores[objective] += raw
        elif status != "supported" or (applicable and objectives):
            unresolved.append(trace | {
                "reason": "unsupported or unresolved magnitude; no numeric credit",
                "numeric_credit": 0.0,
            })
    return dict(scores), traces, unresolved


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
               modifier.literal_value, modifier.magnitude_kind,
               modifier.curve_row_name, modifier.evaluation_channel,
               modifier.source_required_tags_json, modifier.source_ignored_tags_json,
               modifier.target_required_tags_json, modifier.target_ignored_tags_json
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
            "supported_potential_scores": _supported_potential_scores(value),
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
            "profile_kind": "hero_perk",
            "hero_key": row["hero_key"], "display_name": row["display_name"],
            "hero_class": row["hero_class"], "perk_tier": row["perk_tier"],
            "role": role, "perk_family": row["perk_family"],
            "semantic_status": profile["semantic_status"],
            "optimization_ready": profile["semantic_status"] == "supported",
            "reasons": (["direct perk graph contains unresolved/opaque mechanics"]
                        if profile["semantic_status"] != "supported" else []),
            "objective_scores": profile["objective_scores"], "tags": tags,
            "supported_potential_scores": profile["supported_potential_scores"],
            "evidence": profile["evidence"],
            "source": _source_for_object(connection, row["source_object_id"]),
        })
    return result["commander"], result["support"]


def _resolve_weapon_variants(
    connection: sqlite3.Connection, snapshot_id: int, query: str | None,
    owned: Sequence[str] = (), unavailable: Sequence[str] = (),
) -> list[sqlite3.Row]:
    rows = connection.execute("""
        SELECT variant.*, identity.display_name, identity.weapon_kind,
               stat.base_level AS weapon_base_level
        FROM catalog_weapon_variants variant
        JOIN catalog_weapon_identities identity ON identity.id=variant.identity_id
        LEFT JOIN catalog_weapon_stats stat ON stat.weapon_variant_id=variant.id
        WHERE variant.snapshot_id=? AND variant.slot_loadout_id IS NOT NULL
        ORDER BY COALESCE(stat.base_level,0) DESC, variant.variant_key
    """, (snapshot_id,)).fetchall()
    if query:
        rows = [row for row in rows if (
            row["variant_key"].casefold() == query.casefold()
            or str(row["display_name"] or "").casefold() == query.casefold()
            or query.casefold() in str(row["display_name"] or "").casefold()
        )]
    def matches(row: sqlite3.Row, choice: str) -> bool:
        wanted = choice.casefold()
        return wanted in {
            row["variant_key"].casefold(), str(row["display_name"] or "").casefold()
        }
    if owned:
        rows = [row for row in rows if any(matches(row, choice) for choice in owned)]
    if unavailable:
        rows = [row for row in rows if not any(matches(row, choice) for choice in unavailable)]
    if not rows:
        detail = f"weapon preference {query!r}" if query else "open weapon constraints"
        raise ValueError(f"{detail} resolved no catalog variants")
    exact = [row for row in rows if query and row["variant_key"].casefold() == query.casefold()]
    if exact: return exact
    tiers = [TIER_ORDER.get(str(row["tier"]).split("::")[-1], 0) for row in rows]
    if query and tiers:
        maximum = max(tiers)
        rows = [row for row in rows
                if TIER_ORDER.get(str(row["tier"]).split("::")[-1], 0) == maximum]
    return rows[:12] if query else rows


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
                     "profile_kind": "weapon_perk",
                     "primary_name": row["alteration_primary_asset_name"],
                     "exclusions": _json(row["exclusion_names_json"], []),
                     "semantic_status": row["semantic_status"],
                     "objective_scores": _objective_scores(dict(row)),
                     "semantic_facts": {"display_name": row["display_name"],
                                        "description": row["description"],
                                        "tags": _json(row["tags_json"], [])},
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
    seen_loadouts: set[tuple[int, int]] = set()
    for variant in variants:
        # Variants sharing one slot loadout have identical legal perk configurations;
        # retain only the highest-level representative for search.
        identity_loadout = (variant["identity_id"], variant["slot_loadout_id"])
        if identity_loadout in seen_loadouts: continue
        seen_loadouts.add(identity_loadout)
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
                            "weapon_kind": variant["weapon_kind"],
                            "tags": _json(variant["tags_json"], [])},
                "profiles": selected, "heuristic": score,
            })
    configurations.sort(key=lambda item: (-item["heuristic"], item["configuration"].variant_key))
    return configurations[:beam_width], theoretical, pruned


def _hero_beam(commanders: Sequence[dict[str, Any]], supports: Sequence[dict[str, Any]],
               weights: Mapping[str, float], support_slots: int, beam_width: int,
               stats: SearchStats,
               locked_supports: Sequence[dict[str, Any]] = (),
               profile_score: Any = None) -> list[dict[str, Any]]:
    score = profile_score or (lambda profile, _commander: _weighted(profile, weights))
    ranked_commanders = sorted(
        commanders, key=lambda item: (-score(item, item), item["hero_key"])
    )[:max(24, beam_width // 2)]
    locked_keys = {hero["hero_key"] for hero in locked_supports}
    beam = [{"commander": commander, "supports": list(locked_supports),
             "used": {commander["hero_key"], *locked_keys},
             "heuristic": score(commander, commander) +
                sum(score(hero, commander) for hero in locked_supports)}
            for commander in ranked_commanders if commander["hero_key"] not in locked_keys]
    for _ in range(support_slots - len(locked_supports)):
        expanded = []
        for state in beam:
            ranked_supports = sorted(
                supports,
                key=lambda item: (-score(item, state["commander"]), item["hero_key"]),
            )[:max(48, beam_width)]
            for support in ranked_supports:
                if support["hero_key"] in state["used"]: continue
                expanded.append({"commander": state["commander"],
                    "supports": state["supports"] + [support],
                    "used": state["used"] | {support["hero_key"]},
                    "heuristic": state["heuristic"] + score(support, state["commander"])})
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


def _matches_choice(profile: Mapping[str, Any], choice: str) -> bool:
    wanted = choice.casefold()
    return wanted in {
        str(profile.get("hero_key") or profile.get("key") or "").casefold(),
        str(profile.get("display_name") or "").casefold(),
    }


def _filter_profiles(
    profiles: Sequence[dict[str, Any]], owned: Sequence[str], unavailable: Sequence[str],
    avoid_mechanics: Sequence[str] = (),
) -> list[dict[str, Any]]:
    result = list(profiles)
    if owned:
        result = [item for item in result if any(_matches_choice(item, name) for name in owned)]
    if unavailable:
        result = [item for item in result if not any(_matches_choice(item, name) for name in unavailable)]
    if avoid_mechanics:
        result = [item for item in result if not any(
            _profile_matches_avoid(item, mechanic) for mechanic in avoid_mechanics
        )]
    return result


def _profile_matches_avoid(profile: Mapping[str, Any], mechanic: str) -> bool:
    # Only normalized semantic facts participate. Source paths and identity filenames
    # are provenance, not evidence that a mechanic exists.
    text = _flatten_text({
        "evidence": profile.get("evidence"),
        "semantic_facts": profile.get("semantic_facts"),
    })
    normalized = mechanic.casefold().replace("_", " ").replace("-", " ")
    aliases = {
        "elimination trigger": ("elimination", "onkill", "on kill", "kill trigger"),
    }.get(normalized, (normalized,))
    return any(alias in text for alias in aliases)


def _resolve_locked(
    profiles: Sequence[dict[str, Any]], choices: Sequence[str], kind: str
) -> list[dict[str, Any]]:
    resolved = []
    for choice in choices:
        matches = [item for item in profiles if _matches_choice(item, choice)]
        if len(matches) != 1:
            raise ValueError(f"locked {kind} must resolve exactly once: {choice!r}")
        resolved.append(matches[0])
    keys = [item.get("hero_key") or item.get("key") for item in resolved]
    if len(keys) != len(set(keys)):
        raise ValueError(f"locked {kind} choices must be unique")
    return resolved


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
        team.append({"profile_kind": "team_perk", "key": row["team_perk_key"], "display_name": row["display_name"],
                     "semantic_status": status,
                     "objective_scores": _objective_scores(value), "report": report,
                     "supported_potential_scores": semantics["supported_potential_scores"],
                     "semantic_facts": value,
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
        gadgets.append({"profile_kind": "gadget", "key": row["gadget_key"], "display_name": row["display_name"],
                        "semantic_status": status,
                        "objective_scores": _objective_scores(value),
                        "supported_potential_scores": semantics["supported_potential_scores"],
                        "semantic_facts": value,
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


def _collect_tag_strings(value: Any) -> set[str]:
    result = set()
    if isinstance(value, str):
        if "." in value and " " not in value: result.add(value)
    elif isinstance(value, Mapping):
        for item in value.values(): result.update(_collect_tag_strings(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value: result.update(_collect_tag_strings(item))
    return result


def _candidate_applicability_context(
    candidate: Mapping[str, Any], abilities: Sequence[Mapping[str, Any]],
    request: OptimizationRequest, resolved_context: Mapping[str, Any],
) -> dict[str, Any]:
    source_tags = _collect_tag_strings(candidate["weapon"]["variant"].get("tags", ()))
    source_tags.update(request.combat_scenario.active_source_tags)
    for perk in candidate["weapon"]["profiles"]:
        source_tags.update(_collect_tag_strings(perk.get("semantic_facts", {})))
    target_tags = set(request.combat_scenario.target_tags) | set(
        resolved_context.get("effective_target_tags", ())
    )
    target_tags.update(request.target.status_tags)
    if request.combat_scenario.target_afflicted:
        target_tags.add("Gameplay.Status.Afflicted")
    active_abilities = {
        str(value)
        for ability in abilities
        for value in (ability["identity"]["active_ability_key"],
                      ability["identity"]["display_name"])
    }
    return {
        "source_tags": source_tags,
        "target_tags": target_tags,
        "active_abilities": active_abilities,
        "excluded_events": request.constraints.avoid_mechanics,
        "health_fraction": request.combat_scenario.health_fraction,
        "shield_fraction": request.combat_scenario.shield_fraction,
        "weapon_present": True,
        "weapon_kind": candidate["weapon"]["variant"].get("weapon_kind"),
    }


def _context_for_profile(
    profile: Mapping[str, Any], applicability_context: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(applicability_context)
    granted_tags = {
        tag for tag in _collect_tag_strings(_profile_evidence(profile).get("tags", ()))
        if tag.casefold().startswith("granted.perk")
    }
    tier = str(profile.get("perk_tier") or "").casefold()
    if tier and tier != "unknown":
        granted_tags = {tag for tag in granted_tags if tag.casefold().endswith("." + tier)}
    result["source_tags"] = set(applicability_context.get("source_tags", ())) | granted_tags
    return result


def _applicable_profile_weight(
    profile: Mapping[str, Any], applicability_context: Mapping[str, Any],
    weights: Mapping[str, float],
) -> float:
    if profile.get("profile_kind") == "gadget":
        return 0.0
    scores, traces, _ = _applicability_trace(
        profile, _context_for_profile(profile, applicability_context)
    )
    for trace in traces:
        if not trace["applicable"] or trace["semantic_status"] != "supported": continue
        if trace["raw_value"] is not None:
            potential = float(trace["raw_value"])
        else:
            tier_match = re.search(r"(\d+)$", str(profile.get("perk_tier") or ""))
            potential = float(int(tier_match.group(1)) if tier_match else 1)
        for objective in trace["objective_mapping"]:
            scores[objective] = max(float(scores.get(objective, 0.0)), potential)
    return sum(
        weights.get(objective, 0.0) * value
        for objective, value in scores.items()
    )


def _profile_components(
    candidate: Mapping[str, Any], weights: Mapping[str, float],
    applicability_context: Mapping[str, Any],
) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    profiles = [candidate["commander"], *candidate["supports"], *candidate["weapon"]["profiles"]]
    if candidate.get("team_perk"): profiles.append(candidate["team_perk"])
    profiles.extend(candidate["gadgets"])
    supported = Counter()
    partial, opaque, traces = [], [], []
    for profile in profiles:
        profile_context = _context_for_profile(profile, applicability_context)
        scores, profile_traces, unresolved = _applicability_trace(
            profile, profile_context
        )
        if profile.get("profile_kind") == "gadget":
            scores = {}
            for trace in profile_traces:
                trace["raw_value"] = None
                trace["weighted_score_contribution"] = 0.0
        traces.extend(profile_traces)
        # Atomic profiles guide candidate generation, but final numeric credit comes
        # only from the deterministic evaluator after all candidate conditions apply.
        for item in unresolved:
            relevant = any(
                _objective_scores(item).get(objective, 0) for objective in weights
            ) or any(objective in weights for objective in item.get("objective_mapping", ()))
            if not relevant: continue
            status = item.get("semantic_status") or profile.get("semantic_status", "partial")
            (opaque if status == "opaque" else partial).append(
                item | {"unquantified": True}
            )
    return dict(supported), partial, opaque, traces


def _render_candidate(connection: sqlite3.Connection, snapshot_id: int, candidate: dict[str, Any],
                      request: OptimizationRequest, context: dict[str, Any],
                      evaluation_cache: dict[Any, Any], stats: SearchStats) -> dict[str, Any]:
    abilities = _active_abilities(connection, snapshot_id, candidate["commander"]["hero_key"])
    applicability_context = _candidate_applicability_context(
        candidate, abilities, request, context
    )
    _, partial, opaque, applicability_trace = _profile_components(
        candidate, request.weights(), applicability_context
    )
    weighted_objectives = set(request.weights())

    def numerically_applicable(profile: Mapping[str, Any]) -> bool:
        _, traces, _ = _applicability_trace(
            profile, _context_for_profile(profile, applicability_context)
        )
        return any(
            trace["applicable"] and trace["semantic_status"] == "supported"
            and weighted_objectives.intersection(trace["objective_mapping"])
            for trace in traces
        )

    numeric_commander = (
        candidate["commander"]["hero_key"]
        if numerically_applicable(candidate["commander"]) else None
    )
    numeric_supports = tuple(
        hero["hero_key"] for hero in candidate["supports"]
        if numerically_applicable(hero)
    )
    numeric_team = (
        candidate["team_perk"]["key"]
        if candidate.get("team_perk") and numerically_applicable(candidate["team_perk"])
        else None
    )
    ability_source_tags = {
        requirement["value"]
        for trace in applicability_trace if trace["applicable"]
        for requirement in trace.get("requirement", ())
        if requirement.get("kind") == "source_tag"
        and _ability_requirement(requirement["value"])
        and any("commander ability" in item for item in trace["requirement_satisfied_by"])
    }
    loadout = LoadoutContext(
        commander=numeric_commander,
        support_heroes=numeric_supports,
        source_tags=tuple(sorted(ability_source_tags)),
        team_perk=numeric_team,
        gadgets=(),
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
    raw_components: dict[str, float] = {}
    for contribution in evaluation.contributions:
        if not contribution.get("active") or contribution.get("magnitude") is None:
            continue
        objectives = ATTRIBUTE_OBJECTIVES.get(
            str(contribution.get("attribute") or "").casefold(), ()
        )
        raw = abs(float(contribution["magnitude"]) - 1.0) if (
            contribution.get("operation") == "EGameplayModOp::Multiplicitive"
        ) else abs(float(contribution["magnitude"]))
        mapped_objectives = [
            objective for objective in objectives
            if objective in weighted_objectives
        ]
        credited_objectives = [objective for objective in mapped_objectives
                               if objective not in {"burst_damage", "sustained_damage",
                                                    "weapon_uptime"}]
        for objective in credited_objectives:
            raw_components[objective] = raw_components.get(objective, 0.0) + raw
        if mapped_objectives:
            applicability_trace.append({
                "source": contribution.get("origin_name"),
                "mechanic": contribution.get("attribute"),
                "requirement": contribution.get("conditions", {}),
                "requirement_satisfied_by": ["deterministic combat evaluator"],
                "applicable": True, "raw_value": raw,
                "objective_mapping": mapped_objectives,
                "semantic_status": "supported",
                "credited": bool(credited_objectives),
                "weighted_score_contribution": 0.0,
            })
    if evaluation.metrics.get("burst_dps") is not None:
        raw_components["burst_damage"] = evaluation.metrics["burst_dps"]
        applicability_trace.append({
            "source": evaluation.weapon.get("display_name") or evaluation.weapon.get("variant_key"),
            "mechanic": "evaluated burst DPS", "requirement": [],
            "requirement_satisfied_by": ["deterministic combat evaluator"],
            "applicable": True, "raw_value": evaluation.metrics["burst_dps"],
            "objective_mapping": ["burst_damage"], "semantic_status": "supported",
            "weighted_score_contribution": 0.0, "metric": True, "credited": True,
        })
    if evaluation.metrics.get("sustained_dps") is not None:
        raw_components["sustained_damage"] = evaluation.metrics["sustained_dps"]
        applicability_trace.append({
            "source": evaluation.weapon.get("display_name") or evaluation.weapon.get("variant_key"),
            "mechanic": "evaluated sustained DPS", "requirement": [],
            "requirement_satisfied_by": ["deterministic combat evaluator"],
            "applicable": True, "raw_value": evaluation.metrics["sustained_dps"],
            "objective_mapping": ["sustained_damage"], "semantic_status": "supported",
            "weighted_score_contribution": 0.0, "metric": True, "credited": True,
        })
    target_text = " ".join((
        request.target.enemy,
        *map(str, context.get("effective_target_tags", ())),
    )).casefold()
    if ("mist_monster_boss" in weighted_objectives
            and any(term in target_text for term in ("mist", "smasher", "boss", "miniboss"))
            and evaluation.metrics.get("burst_dps") is not None):
        raw_components["mist_monster_boss"] = evaluation.metrics["burst_dps"]
        applicability_trace.append({
            "source": evaluation.weapon.get("display_name") or evaluation.weapon.get("variant_key"),
            "mechanic": "evaluated burst DPS against resolved boss/mist target",
            "requirement": [{"kind": "target_archetype", "value": request.target.enemy}],
            "requirement_satisfied_by": ["resolved enemy classification"],
            "applicable": True, "raw_value": evaluation.metrics["burst_dps"],
            "objective_mapping": ["mist_monster_boss"], "semantic_status": "supported",
            "weighted_score_contribution": 0.0, "metric": True, "credited": True,
        })
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
        "applicability_trace": applicability_trace,
        "numeric_applicability": {
            "commander": numeric_commander,
            "support_heroes": list(numeric_supports),
            "team_perk": numeric_team,
            "gadgets": [],
        },
        "partial_components": partial, "opaque_components": opaque,
        "comparison_class": "definitive" if not partial and not opaque else "uncertainty_aware",
        "confidence": "high" if not partial and not opaque else "low" if opaque else "medium",
        "key_synergies": [item for item in evaluation.contributions if item.get("active")],
        "limiting_conditions": [asdict(issue) for issue in evaluation.issues] + context["modifier_evaluations"],
        "combat_evaluation": evaluation.as_dict(), "provenance": provenance,
    }


def _normalize_and_rank(candidates: list[dict[str, Any]], weights: Mapping[str, float]) -> None:
    for item in candidates:
        components = {}
        total = 0.0
        for objective, weight in weights.items():
            value = item["raw_supported_components"].get(objective)
            if value is None:
                components[objective] = {"status": "unavailable", "weight": weight}
                continue
            comparison_score = math.asinh(float(value))
            weighted = comparison_score * weight
            components[objective] = {"status": "supported", "raw_value": value,
                                     "normalized_score": comparison_score,
                                     "comparison_score": comparison_score,
                                     "score_scale": "asinh_supported_units",
                                     "weight": weight, "weighted_score": weighted}
            total += weighted
        for trace in item.get("applicability_trace", ()):
            raw = trace.get("raw_value")
            contributions = {}
            if raw is not None and trace.get("credited"):
                for objective in trace.get("objective_mapping", ()):
                    component = components.get(objective)
                    objective_total = item["raw_supported_components"].get(objective)
                    if (component and component.get("status") == "supported"
                            and objective_total and (
                                objective not in {"burst_damage", "sustained_damage"}
                                or trace.get("metric")
                            )):
                        contributions[objective] = (
                            component["weighted_score"] * float(raw) / float(objective_total)
                        )
            trace["weighted_score_contribution"] = contributions
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


def _diagnostic_slots(
    connection: sqlite3.Connection, snapshot_id: int,
    selected: dict[str, Any], raw: dict[str, Any], request: OptimizationRequest,
    resolved_context: dict[str, Any], cache: dict[Any, Any], stats: SearchStats,
    commanders: Sequence[dict[str, Any]], supports: Sequence[dict[str, Any]],
    teams: Sequence[dict[str, Any]], gadgets: Sequence[dict[str, Any]],
    weapons: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    weights = request.weights()

    def legal(candidate: Mapping[str, Any]) -> bool:
        hero_keys = [candidate["commander"]["hero_key"], *(
            hero["hero_key"] for hero in candidate["supports"]
        )]
        if len(hero_keys) != len(set(hero_keys)): return False
        team = candidate.get("team_perk")
        if team:
            eligible, _, _ = _team_perk_eligible(
                team["report"], candidate["supports"], request.hero_progression
            )
            if not eligible: return False
        gadget_keys = [item["key"] for item in candidate["gadgets"]]
        return len(gadget_keys) == len(set(gadget_keys))

    def evaluate(candidate: dict[str, Any]) -> dict[str, Any] | None:
        if not legal(candidate): return None
        try:
            rendered = _render_candidate(
                connection, snapshot_id, candidate, request, resolved_context,
                cache, stats,
            )
            _normalize_and_rank([rendered], weights)
            return rendered
        except ValueError:
            return None

    alternatives: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    selected_hero_keys = {
        raw["commander"]["hero_key"], *(hero["hero_key"] for hero in raw["supports"])
    }
    alternatives["commander"] = [
        (item["display_name"], raw | {"commander": item})
        for item in commanders if item["hero_key"] not in selected_hero_keys
    ]
    for index, chosen in enumerate(raw["supports"]):
        others = selected_hero_keys - {chosen["hero_key"]}
        alternatives[f"support_{index + 1}"] = [
            (item["display_name"], raw | {
                "supports": [*raw["supports"][:index], item, *raw["supports"][index + 1:]]
            }) for item in supports
            if item["hero_key"] not in others and item["hero_key"] != chosen["hero_key"]
        ]
    alternatives["team_perk"] = [
        (item["display_name"], raw | {"team_perk": item})
        for item in teams if not raw.get("team_perk") or item["key"] != raw["team_perk"]["key"]
    ]
    for index, chosen in enumerate(raw["gadgets"]):
        others = {item["key"] for item in raw["gadgets"]} - {chosen["key"]}
        alternatives[f"gadget_{index + 1}"] = [
            (item["display_name"], raw | {
                "gadgets": [*raw["gadgets"][:index], item, *raw["gadgets"][index + 1:]]
            }) for item in gadgets
            if item["key"] not in others and item["key"] != chosen["key"]
    ]
    alternatives["weapon"] = [
        ("{} [{}]".format(
            item["variant"]["display_name"],
            ", ".join(perk.alteration_key for perk in item["configuration"].perks)
            or "no selected perks",
        ), raw | {"weapon": item})
        for item in weapons
        if item["configuration"] != raw["weapon"]["configuration"]
    ]
    selected_names = {
        "commander": selected["commander"]["display_name"],
        **{f"support_{index + 1}": item["display_name"]
           for index, item in enumerate(selected["support_heroes"])},
        "team_perk": (selected["team_perk"] or {}).get("display_name"),
        **{f"gadget_{index + 1}": item["display_name"]
           for index, item in enumerate(selected["gadgets"])},
        "weapon": selected["weapon"].get("display_name") or selected["weapon"].get("variant_key"),
    }
    result = []
    for slot, choices in alternatives.items():
        scored = []
        for name, candidate in choices:
            rendered = evaluate(candidate)
            if rendered is None: continue
            scored.append({
                "selection": name,
                "comparison_class": rendered["comparison_class"],
                "supported_weighted_score": rendered["supported_weighted_score"],
                "score_delta": rendered["supported_weighted_score"]
                               - selected["supported_weighted_score"],
            })
        scored.sort(key=lambda item: (-item["supported_weighted_score"], item["selection"]))
        owner = selected_names.get(slot)
        traces = [
            trace for trace in selected.get("applicability_trace", ())
            if owner and (
                str(trace.get("source") or "").casefold() == owner.casefold()
                or str(trace.get("source") or "").casefold().startswith(
                    owner.casefold() + " ("
                )
            )
        ]
        best_alternative = max(
            (item["supported_weighted_score"] for item in scored), default=0.0
        )
        result.append({
            "slot": slot, "selected": owner,
            "selection_trace": traces,
            "supported_weighted_contribution": max(
                0.0, selected["supported_weighted_score"] - best_alternative
            ),
            "marginal_method": "controlled one-slot replacement against best legal alternative",
            "top_rejected_alternatives": scored[:3],
        })
    return {
        "score_scale": "asinh_supported_units; independent of current search batch",
        "slots": result,
    }


def optimize_loadouts(connection: sqlite3.Connection, request: OptimizationRequest,
                      snapshot_id: int | None = None, progress: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    emit = progress or (lambda stage, detail=None: None)
    weights = request.weights()
    if not 0 <= request.support_slots <= 5 or not 0 <= request.gadget_slots <= 2:
        raise ValueError("support slots must be 0..5 and gadget slots must be 0..2")
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None: raise ValueError("no ready asset snapshot")
    stats = SearchStats()
    constraints = request.constraints
    emit("generating_legal_builds", "Generating legal weapons, heroes, team perks, and gadgets")
    variants = _resolve_weapon_variants(
        connection, snapshot_id, request.weapon,
        constraints.owned_weapons, constraints.unavailable_weapons,
    )
    weapon_configs, theoretical_weapons, weapon_pruned = _weapon_configurations(
        connection, variants, weights, request.beam_width, request.item_level)
    if constraints.avoid_mechanics:
        weapon_configs = [item for item in weapon_configs if not any(
            _profile_matches_avoid(profile, mechanic)
            for profile in item["profiles"]
            for mechanic in constraints.avoid_mechanics
        )]
        if not weapon_configs:
            raise ValueError("avoided mechanics leave no legal weapon perk configurations")
    if constraints.locked_weapon_perks:
        required = {(slot, key.casefold()) for slot, key in constraints.locked_weapon_perks}
        weapon_configs = [item for item in weapon_configs if required.issubset({
            (perk.slot_ordinal, perk.alteration_key.casefold())
            for perk in item["configuration"].perks
        })]
        if not weapon_configs:
            raise ValueError("locked weapon perks leave no legal weapon configurations")
    stats.weapon_configurations = theoretical_weapons
    stats.pruned_by_beam += weapon_pruned
    commanders, supports = _hero_profiles(connection, snapshot_id)
    commanders = _filter_profiles(
        commanders, constraints.owned_heroes, constraints.unavailable_heroes,
        constraints.avoid_mechanics,
    )
    supports = _filter_profiles(
        supports, constraints.owned_heroes, constraints.unavailable_heroes,
        constraints.avoid_mechanics,
    )
    if constraints.locked_commander:
        commanders = _resolve_locked(
            commanders, (constraints.locked_commander,), "commander"
        )
    locked_supports = _resolve_locked(
        supports, constraints.locked_supports, "support hero"
    )
    if len(locked_supports) > request.support_slots:
        raise ValueError("more locked supports than available support slots")
    if request.support_slots and len(supports) < request.support_slots:
        raise ValueError("catalog does not contain enough support heroes")
    if not commanders:
        raise ValueError("constraints leave no legal commander candidates")
    stats.theoretical_hero_loadouts = len(commanders) * (
        math.comb(max(0, len(supports) - 1), request.support_slots)
        if len(supports) - 1 >= request.support_slots else 0)
    context = scenario_report(connection, snapshot_id, request.target, request.mission)
    ability_cache = {
        commander["hero_key"]: _active_abilities(
            connection, snapshot_id, commander["hero_key"]
        ) for commander in commanders
    }
    hero_states = []
    per_weapon = max(1, min(8, request.beam_width // max(1, len(weapon_configs))))
    for weapon in weapon_configs:
        def score_profile(profile: Mapping[str, Any], commander: Mapping[str, Any]) -> float:
            candidate_context = _candidate_applicability_context(
                {"weapon": weapon}, ability_cache[commander["hero_key"]],
                request, context,
            )
            return _applicable_profile_weight(
                profile, candidate_context, weights
            )
        weapon_heroes = _hero_beam(
            commanders, supports, weights, request.support_slots,
            max(8, per_weapon * 4), stats, locked_supports, score_profile,
        )
        hero_states.extend(
            state | {"weapon": weapon,
                     "heuristic": state["heuristic"] + weapon["heuristic"]}
            for state in weapon_heroes[:per_weapon]
        )
    hero_states.sort(key=lambda item: -item["heuristic"])
    stats.pruned_by_beam += max(0, len(hero_states) - request.beam_width)
    hero_states = hero_states[:request.beam_width]
    if not hero_states:
        raise ValueError("constraints leave no legal commander/support combinations")
    team_profiles, gadget_profiles = _interaction_profiles(connection, snapshot_id)
    team_profiles = _filter_profiles(
        team_profiles, constraints.owned_team_perks, (), constraints.avoid_mechanics,
    )
    if constraints.locked_team_perk:
        team_profiles = _resolve_locked(
            team_profiles, (constraints.locked_team_perk,), "team perk"
        )
    gadget_profiles = _filter_profiles(
        gadget_profiles, constraints.owned_gadgets, (), constraints.avoid_mechanics,
    )
    locked_gadgets = _resolve_locked(
        gadget_profiles, constraints.locked_gadgets, "gadget"
    )
    if len(locked_gadgets) > request.gadget_slots:
        raise ValueError("more locked gadgets than available gadget slots")
    expanded = []
    for state in hero_states:
        if not team_profiles:
            expanded.append(state | {"team_perk": None})
            continue
        eligible = []
        state_context = _candidate_applicability_context(
            state, ability_cache[state["commander"]["hero_key"]], request, context
        )
        for team in team_profiles:
            stats.team_perk_checks += 1
            legal, eligibility_status, reasons = _team_perk_eligible(
                team["report"], state["supports"], request.hero_progression)
            if legal:
                stats.team_perk_eligible += 1
                eligible.append(team | {"eligibility_status": eligibility_status,
                                        "eligibility_reasons": reasons})
        choice_count = max(1, min(4, request.beam_width // max(1, len(hero_states))))
        for team in sorted(eligible, key=lambda item: (
            -_applicable_profile_weight(item, state_context, weights), item["key"]
        ))[:choice_count]:
            team_score = _applicable_profile_weight(team, state_context, weights)
            expanded.append(state | {"team_perk": team,
                "heuristic": state["heuristic"] + team_score})
    expanded.sort(key=lambda item: -item["heuristic"])
    stats.pruned_by_beam += max(0, len(expanded) - request.beam_width)
    expanded = expanded[:request.beam_width]
    locked_gadget_keys = {item["key"] for item in locked_gadgets}
    remaining_gadgets = [
        item for item in gadget_profiles if item["key"] not in locked_gadget_keys
    ]
    needed_gadgets = request.gadget_slots - len(locked_gadgets)
    gadget_sets = [
        (*locked_gadgets, *group)
        for group in itertools.combinations(remaining_gadgets, needed_gadgets)
    ] if needed_gadgets else [tuple(locked_gadgets)]
    if request.gadget_slots and not gadget_sets:
        raise ValueError("constraints leave no legal gadget combinations")
    stats.gadget_combinations = len(gadget_sets)
    with_gadgets = []
    gadget_choice_count = max(1, min(8, request.beam_width // max(1, len(expanded))))
    for state in expanded:
        state_context = _candidate_applicability_context(
            state, ability_cache[state["commander"]["hero_key"]], request, context
        )
        ranked_gadgets = sorted(
            gadget_sets,
            key=lambda group: -sum(
                _applicable_profile_weight(item, state_context, weights)
                for item in group
            ),
        )[:gadget_choice_count]
        for gadgets in ranked_gadgets:
            gadget_score = sum(
                _applicable_profile_weight(item, state_context, weights)
                for item in gadgets
            )
            with_gadgets.append(state | {"gadgets": list(gadgets),
                "heuristic": state["heuristic"] + gadget_score})
    with_gadgets.sort(key=lambda item: -item["heuristic"])
    stats.pruned_by_beam += max(0, len(with_gadgets) - request.beam_width)
    with_gadgets = with_gadgets[:request.beam_width]
    combined = with_gadgets
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
    cache: dict[Any, Any] = {}
    emit("evaluating_candidates", f"Evaluating {len(deduplicated)} candidate builds")
    rendered = [_render_candidate(connection, snapshot_id, candidate, request, context, cache, stats)
                for candidate in deduplicated]
    raw_by_rendered = {id(rendered_item): raw_item for rendered_item, raw_item in zip(
        rendered, deduplicated
    )}
    if not constraints.allow_opaque:
        rendered = [item for item in rendered if not item["opaque_components"]]
    if not constraints.allow_partial:
        rendered = [item for item in rendered if not item["partial_components"]]
    if not rendered:
        raise ValueError("constraints leave no candidates with an allowed evidence status")
    stats.evaluated_candidates = len(rendered)
    _normalize_and_rank(rendered, weights)
    stats.pareto_dominated = _mark_pareto(rendered, weights)
    definitive = sorted((item for item in rendered if item["comparison_class"] == "definitive"),
                        key=lambda item: (item["pareto_dominated"], -item["supported_weighted_score"]))
    uncertain = sorted((item for item in rendered if item["comparison_class"] != "definitive"),
                       key=lambda item: (item["pareto_dominated"], -item["supported_weighted_score"]))
    if request.diagnostics:
        diagnostic_targets = (definitive or uncertain)[:1]
        for selected in diagnostic_targets:
            selected["selection_diagnostics"] = _diagnostic_slots(
                connection, snapshot_id, selected, raw_by_rendered[id(selected)],
                request, context, cache, stats, commanders, supports,
                team_profiles, gadget_profiles, weapon_configs,
            )
    semantic_statuses = Counter(
        "opaque" if item["opaque_components"] else "partial"
        if item["partial_components"] else "supported" for item in rendered
    )
    emit("analyzing_uncertainty", "Classifying supported, partial, and opaque contributions")
    return {
        "snapshot_id": snapshot_id, "request": {**asdict(request), "normalized_weights": weights},
        "scenario_resolution": context,
        "search_space": {**asdict(stats),
            "upper_bound_complete_combinations": stats.theoretical_hero_loadouts * max(1, theoretical_weapons)
                * max(1, len(team_profiles)) * max(1, stats.gadget_combinations)},
        "strategy": ["early weapon/slot legality filtering", "semantic objective compatibility profiles",
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
    parser.add_argument("--diagnostics", action="store_true",
                        help="trace selected slots and one-slot rejected alternatives")
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
        diagnostics=args.diagnostics,
    )
    connection = connect(args.db)
    try: print(json.dumps(optimize_loadouts(connection, request), indent=2, sort_keys=True))
    finally: connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
