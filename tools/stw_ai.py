#!/usr/bin/env python3
"""Evidence-constrained AI orchestration for STW loadout intelligence.

The provider interprets language and selects evidence. Fortnite facts and all rendered
claims come from deterministic catalog/evaluator/optimizer tools.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from stw_assets import latest_asset_snapshot_id, perk_family_semantic_report
from stw_combat import (
    CombatScenario,
    LoadoutContext,
    WeaponConfiguration,
    WeaponPerkSelection,
    evaluate_combat,
)
from stw_context import (
    MissionContext, TargetContext, enemy_report, mission_report, modifier_report,
)
from stw_elements import element_report, status_report
from stw_interactions import active_ability_report, gadget_report, team_perk_report
from stw_optimizer import (
    OBJECTIVES,
    OptimizationConstraints,
    OptimizationRequest,
    _hero_profiles,
    _interaction_profiles,
    _compatible_option,
    _resolve_weapon_variants,
    _team_perk_eligible,
    _weapon_option_profiles,
    optimize_loadouts,
)
from stw_pipeline import connect
from stw_signatures import signature_report


INTENT_SCHEMA_VERSION = "stw.build-intent.v1"
TOOL_SCHEMA_VERSION = "stw.ai-tools.v1"
PROMPT_VERSION = "stw.reasoning.v1"
REASONING_POLICY = """Interpret user intent and select supplied evidence IDs only.
Never provide Fortnite mechanics, values, legality judgments, rankings, or claims from
model memory. Ask for clarification when a material structured input is missing. Keep
supported, partial, and opaque evidence distinct; unknown contributions are not zero."""
BUILD_INTENT_FIELDS = {
    "schema_version", "user_request", "mode", "weapon", "target_enemy",
    "target_element", "target_statuses", "enemy_modifiers", "mission",
    "power_level", "four_player", "elemental_storm", "mission_modifiers",
    "objective_weights", "owned_heroes", "unavailable_heroes", "owned_weapons",
    "unavailable_weapons", "locked_commander", "locked_supports",
    "locked_team_perk", "owned_team_perks", "locked_gadgets", "owned_gadgets",
    "locked_weapon_perks", "avoid_conditions", "allow_partial", "allow_opaque",
    "requested_alternatives", "support_slots", "gadget_slots", "beam_width",
    "current_loadout", "comparison_loadouts",
}

OBJECTIVE_LANGUAGE = {
    "burst_damage": ("burst", "one shot", "delet", "vaporize", "nuke"),
    "sustained_damage": ("sustained", "dps", "damage", "strongest"),
    "crowd_clear": ("crowd clear", "aoe", "wave clear", "trash clear"),
    "mist_monster_boss": ("mist monster", "smasher", "boss", "miniboss"),
    "survivability": ("survive", "survivable", "survivability", "tanky", "tank", "defense"),
    "healing_sustain": ("healing", "heal", "sustain", "regeneration"),
    "crowd_control": ("crowd control", "crowd-control", "stun", "snare", "freeze", "slow"),
    "ability_uptime": ("ability uptime", "uptime", "cooldown", "spam"),
    "weapon_uptime": ("weapon uptime", "reload", "magazine"),
    "condition_reliability": ("reliable", "reliability", "without kills", "no kills", "rely on kills"),
}

CATALOG_SPECS = {
    "hero": ("catalog_heroes", "hero_key", "display_name", "'supported'"),
    "hero_perk": ("catalog_perks", "perk_family || ':' || perk_tier", "perk_family", "'partial'"),
    "weapon": ("catalog_weapon_identities", "identity_key", "display_name", "'supported'"),
    "schematic": ("catalog_schematics", "schematic_key", "schematic_key", "'partial'"),
    "weapon_perk": ("catalog_alterations", "alteration_key", "display_name", "semantic_status"),
    "team_perk": ("catalog_team_perks", "team_perk_key", "display_name", "semantic_status"),
    "ability": ("catalog_active_abilities", "active_ability_key", "display_name", "semantic_status"),
    "gadget": ("catalog_gadgets", "gadget_key", "display_name", "semantic_status"),
    "signature": ("catalog_signature_effects", "signature_key", "display_name", "semantic_status"),
    "element": ("catalog_element_identities", "element_key", "display_name", "semantic_status"),
    "status": ("catalog_status_identities", "status_key", "display_name", "semantic_status"),
    "enemy": ("catalog_enemy_archetypes", "enemy_key", "display_name", "semantic_status"),
    "mission": ("catalog_mission_objectives", "objective_key", "display_name", "semantic_status"),
    "modifier": ("catalog_context_modifiers", "modifier_key", "display_name", "semantic_status"),
}


def _tuple_strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def _weapon_perks(value: Any, name: str) -> tuple[tuple[int, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    parsed = []
    for item in value:
        if (not isinstance(item, Mapping) or not isinstance(item.get("slot"), int)
                or isinstance(item.get("slot"), bool) or item["slot"] < 0
                or not isinstance(item.get("perk"), str) or not item["perk"].strip()):
            raise ValueError(f"each {name} entry requires integer slot and string perk")
        parsed.append((item["slot"], item["perk"]))
    if len({slot for slot, _ in parsed}) != len(parsed):
        raise ValueError(f"{name} cannot lock one slot more than once")
    return tuple(parsed)


@dataclass(frozen=True)
class SpecifiedLoadout:
    weapon: str
    weapon_perks: tuple[tuple[int, str], ...] = ()
    commander: str | None = None
    support_heroes: tuple[str, ...] = ()
    team_perk: str | None = None
    gadgets: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SpecifiedLoadout":
        weapon = value.get("weapon")
        if not isinstance(weapon, str) or not weapon.strip():
            raise ValueError("current_loadout.weapon is required")
        for name in ("commander", "team_perk"):
            if value.get(name) is not None and not isinstance(value[name], str):
                raise ValueError(f"current_loadout.{name} must be a string or null")
        return cls(
            weapon.strip(), _weapon_perks(value.get("weapon_perks"), "weapon perk"), value.get("commander"),
            _tuple_strings(value.get("support_heroes"), "current_loadout.support_heroes"),
            value.get("team_perk"),
            _tuple_strings(value.get("gadgets"), "current_loadout.gadgets"),
        )


@dataclass(frozen=True)
class BuildIntent:
    user_request: str
    mode: str = "recommend"
    weapon: str | None = None
    target_enemy: str | None = None
    target_element: str | None = None
    target_statuses: tuple[str, ...] = ()
    enemy_modifiers: tuple[str, ...] = ()
    mission: str | None = None
    power_level: int | None = None
    four_player: bool | None = None
    elemental_storm: str | None = None
    mission_modifiers: tuple[str, ...] = ()
    objective_weights: tuple[tuple[str, float], ...] = (("sustained_damage", 1.0),)
    owned_heroes: tuple[str, ...] = ()
    unavailable_heroes: tuple[str, ...] = ()
    owned_weapons: tuple[str, ...] = ()
    unavailable_weapons: tuple[str, ...] = ()
    locked_commander: str | None = None
    locked_supports: tuple[str, ...] = ()
    locked_team_perk: str | None = None
    owned_team_perks: tuple[str, ...] = ()
    locked_gadgets: tuple[str, ...] = ()
    owned_gadgets: tuple[str, ...] = ()
    locked_weapon_perks: tuple[tuple[int, str], ...] = ()
    avoid_conditions: tuple[str, ...] = ()
    allow_partial: bool = True
    allow_opaque: bool = True
    requested_alternatives: int = 3
    support_slots: int = 5
    gadget_slots: int = 2
    beam_width: int = 64
    current_loadout: SpecifiedLoadout | None = None
    comparison_loadouts: tuple[SpecifiedLoadout, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], user_request: str = "") -> "BuildIntent":
        unknown_fields = set(value) - BUILD_INTENT_FIELDS
        if unknown_fields:
            raise ValueError(f"unknown BuildIntent fields: {sorted(unknown_fields)}")
        if value.get("schema_version", INTENT_SCHEMA_VERSION) != INTENT_SCHEMA_VERSION:
            raise ValueError("unsupported BuildIntent schema version")
        for name in ("weapon", "target_enemy", "target_element", "mission",
                     "elemental_storm", "locked_commander", "locked_team_perk"):
            if value.get(name) is not None and not isinstance(value[name], str):
                raise ValueError(f"{name} must be a string or null")
        if value.get("power_level") is not None and (
            not isinstance(value["power_level"], int) or isinstance(value["power_level"], bool)
            or value["power_level"] <= 0
        ):
            raise ValueError("power_level must be a positive integer or null")
        if value.get("four_player") is not None and not isinstance(value["four_player"], bool):
            raise ValueError("four_player must be boolean or null")
        for name in ("allow_partial", "allow_opaque"):
            if name in value and not isinstance(value[name], bool):
                raise ValueError(f"{name} must be boolean")
        mode = value.get("mode", "recommend")
        if mode not in {"recommend", "analyze", "compare"}:
            raise ValueError("mode must be recommend, analyze, or compare")
        raw_weights = value.get("objective_weights") or {"sustained_damage": 1.0}
        if not isinstance(raw_weights, Mapping):
            raise ValueError("objective_weights must be an object")
        weights = []
        for key, raw in raw_weights.items():
            if key not in OBJECTIVES or not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
                raise ValueError(f"invalid objective weight: {key!r}")
            if raw:
                weights.append((key, float(raw)))
        if not weights:
            raise ValueError("at least one positive objective weight is required")
        alternatives = value.get("requested_alternatives", 3)
        if not isinstance(alternatives, int) or not 1 <= alternatives <= 10:
            raise ValueError("requested_alternatives must be between 1 and 10")
        support_slots = value.get("support_slots", 5)
        gadget_slots = value.get("gadget_slots", 2)
        beam_width = value.get("beam_width", 64)
        if not isinstance(support_slots, int) or not 0 <= support_slots <= 5:
            raise ValueError("support_slots must be between 0 and 5")
        if not isinstance(gadget_slots, int) or not 0 <= gadget_slots <= 2:
            raise ValueError("gadget_slots must be between 0 and 2")
        if not isinstance(beam_width, int) or not 1 <= beam_width <= 1024:
            raise ValueError("beam_width must be between 1 and 1024")
        current = value.get("current_loadout")
        raw_comparisons = value.get("comparison_loadouts", [])
        if not isinstance(raw_comparisons, list):
            raise ValueError("comparison_loadouts must be a list")
        if not all(isinstance(item, Mapping) for item in raw_comparisons):
            raise ValueError("each comparison loadout must be an object")
        return cls(
            user_request=user_request or str(value.get("user_request", "")), mode=mode,
            weapon=value.get("weapon"), target_enemy=value.get("target_enemy"),
            target_element=value.get("target_element"), mission=value.get("mission"),
            target_statuses=_tuple_strings(value.get("target_statuses"), "target_statuses"),
            enemy_modifiers=_tuple_strings(value.get("enemy_modifiers"), "enemy_modifiers"),
            power_level=value.get("power_level"), four_player=value.get("four_player"),
            elemental_storm=value.get("elemental_storm"),
            mission_modifiers=_tuple_strings(value.get("mission_modifiers"), "mission_modifiers"),
            objective_weights=tuple(weights),
            owned_heroes=_tuple_strings(value.get("owned_heroes"), "owned_heroes"),
            unavailable_heroes=_tuple_strings(value.get("unavailable_heroes"), "unavailable_heroes"),
            owned_weapons=_tuple_strings(value.get("owned_weapons"), "owned_weapons"),
            unavailable_weapons=_tuple_strings(value.get("unavailable_weapons"), "unavailable_weapons"),
            locked_commander=value.get("locked_commander"),
            locked_supports=_tuple_strings(value.get("locked_supports"), "locked_supports"),
            locked_team_perk=value.get("locked_team_perk"),
            owned_team_perks=_tuple_strings(value.get("owned_team_perks"), "owned_team_perks"),
            locked_gadgets=_tuple_strings(value.get("locked_gadgets"), "locked_gadgets"),
            owned_gadgets=_tuple_strings(value.get("owned_gadgets"), "owned_gadgets"),
            locked_weapon_perks=_weapon_perks(value.get("locked_weapon_perks"), "locked weapon perk"),
            avoid_conditions=_tuple_strings(value.get("avoid_conditions"), "avoid_conditions"),
            allow_partial=bool(value.get("allow_partial", True)),
            allow_opaque=bool(value.get("allow_opaque", True)),
            requested_alternatives=alternatives,
            support_slots=support_slots, gadget_slots=gadget_slots,
            beam_width=beam_width,
            current_loadout=SpecifiedLoadout.from_dict(current) if isinstance(current, Mapping) else None,
            comparison_loadouts=tuple(
                SpecifiedLoadout.from_dict(item) for item in raw_comparisons
            ),
        )


class ReasoningProvider(Protocol):
    """Vendor-neutral language interpretation/evidence-selection boundary."""

    provider_id: str

    def interpret(
        self, user_text: str, grounded_entities: Sequence[Mapping[str, Any]],
        conversation: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]: ...

    def select_evidence(self, intent: BuildIntent, evidence: Sequence[Mapping[str, Any]]) -> Sequence[str]: ...


class DeterministicReasoningProvider:
    """Offline provider used by the CLI, API, and tests; it invents no game facts."""

    provider_id = "deterministic-local-v1"

    def interpret(
        self, user_text: str, grounded_entities: Sequence[Mapping[str, Any]],
        conversation: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]:
        text = user_text.casefold()
        weights = {
            objective: float(sum(term in text for term in terms))
            for objective, terms in OBJECTIVE_LANGUAGE.items()
        }
        weights = {key: value for key, value in weights.items() if value}
        if not weights:
            weights = {"sustained_damage": 1.0}
        by_kind: dict[str, list[Mapping[str, Any]]] = {}
        for entity in grounded_entities:
            by_kind.setdefault(str(entity["kind"]), []).append(entity)
        def first(kind: str, *, use_key: bool = False) -> str | None:
            rows = by_kind.get(kind, [])
            return str(rows[0]["entity_key" if use_key else "display_name"]) if rows else None
        hero = first("hero")
        build_around = bool(re.search(r"\b(build around|as commander|commander)\b", text))
        unavailable = []
        if re.search(r"\b(?:don't|dont|do not) own\b", text):
            unavailable = [str(row["display_name"]) for row in by_kind.get("hero", [])]
        power = re.search(r"\b(?:pl\s*)?(\d{2,3})s?\b", text)
        return {
            "schema_version": INTENT_SCHEMA_VERSION,
            "mode": "analyze" if "my current loadout" in text or "what sucks" in text else "recommend",
            "weapon": first("weapon"), "target_enemy": first("enemy", use_key=True),
            "mission": first("mission"), "power_level": int(power.group(1)) if power else None,
            "four_player": True if "4 player" in text or "four player" in text or "160" in text else None,
            "elemental_storm": next((name for name in ("Fire", "Water", "Nature") if f"{name.casefold()} storm" in text), None),
            "objective_weights": weights,
            "locked_commander": hero if build_around else None,
            "unavailable_heroes": unavailable,
            "avoid_conditions": ["elimination_trigger"] if any(
                phrase in text for phrase in ("without kills", "no kills", "doesn't rely on kills", "does not rely on kills")
            ) else [],
            "allow_partial": True, "allow_opaque": True,
            "requested_alternatives": 3,
        }

    def select_evidence(self, intent: BuildIntent, evidence: Sequence[Mapping[str, Any]]) -> Sequence[str]:
        return [str(item["id"]) for item in evidence[:12]]


class StwAiTools:
    """Targeted structured tool boundary over the authoritative local systems."""

    def __init__(self, connection: sqlite3.Connection, snapshot_id: int | None = None):
        self.connection = connection
        self.snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
        if self.snapshot_id is None:
            raise ValueError("no ready asset snapshot")

    @staticmethod
    def schemas() -> dict[str, Any]:
        return {
            "schema_version": TOOL_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "reasoning_policy": REASONING_POLICY,
            "build_intent": {"schema_version": INTENT_SCHEMA_VERSION,
                             "allowed_fields": sorted(BUILD_INTENT_FIELDS),
                             "objective_names": list(OBJECTIVES)},
            "tools": {
                "catalog.search": {"required": ["kind", "query"], "limit": "1..25"},
                "catalog.inspect": {"required": ["kind", "key_or_name"]},
                "loadout.validate": {"required": ["loadout"]},
                "loadout.evaluate": {"required": ["loadout", "scenario"]},
                "loadout.optimize": {"required": ["BuildIntent"]},
                "loadout.compare": {"required": ["loadouts", "scenario"]},
                "evidence.provenance": {"required": ["recommendation"]},
            },
        }

    def search_catalog(self, kind: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if kind not in CATALOG_SPECS:
            raise ValueError(f"unsupported catalog kind: {kind}")
        table, key, display, status = CATALOG_SPECS[kind]
        rows = self.connection.execute(
            f"""SELECT {key} AS entity_key, {display} AS display_name,
                       {status} AS semantic_status
                FROM {table} WHERE snapshot_id=? AND
                  (lower({key}) LIKE lower(?) OR lower(COALESCE({display},'')) LIKE lower(?))
                ORDER BY CASE WHEN lower(COALESCE({display},''))=lower(?) THEN 0 ELSE 1 END,
                         {display}, {key} LIMIT ?""",
            (self.snapshot_id, f"%{query}%", f"%{query}%", query, max(1, min(limit, 25))),
        ).fetchall()
        return [{"kind": kind, **dict(row)} for row in rows]

    def grounded_mentions(self, text: str, limit: int = 30) -> list[dict[str, Any]]:
        folded = text.casefold()
        matches = []
        for kind, (table, key, display, status) in CATALOG_SPECS.items():
            for row in self.connection.execute(
                f"SELECT {key} entity_key, {display} display_name, {status} semantic_status FROM {table} WHERE snapshot_id=?",
                (self.snapshot_id,),
            ):
                names = [str(row["entity_key"]), str(row["display_name"] or "")]
                names = [name for name in names if len(name) >= 3 and name.casefold() in folded]
                if names:
                    matches.append({"kind": kind, **dict(row), "matched_text": max(names, key=len)})
        # Enemy display rows can share one stat label across inherited variants. Map a
        # natural noun to an identity only when the catalog graph exposes exactly one
        # root archetype; inherited event/boss variants are never chosen by row order.
        for token in re.findall(r"[a-z0-9]+", folded):
            terms = {token, token[:-1]} if len(token) >= 6 and token.endswith("s") else {token}
            for term in terms:
                if len(term) < 5: continue
                rows = self.connection.execute(
                    """SELECT enemy_key AS entity_key, display_name, semantic_status
                       FROM catalog_enemy_archetypes
                       WHERE snapshot_id=? AND parent_enemy_id IS NULL AND
                         (lower(enemy_key) LIKE ? OR lower(display_name) LIKE ?)""",
                    (self.snapshot_id, f"%{term}%", f"%{term}%"),
                ).fetchall()
                if len(rows) == 1:
                    matches.append({"kind": "enemy", **dict(rows[0]),
                                    "matched_text": token})
                    continue
                weapon_rows = self.connection.execute(
                    """SELECT identity_key AS entity_key, display_name,
                              'supported' AS semantic_status
                       FROM catalog_weapon_identities
                       WHERE snapshot_id=? AND
                         (lower(identity_key) LIKE ? OR lower(display_name) LIKE ?)""",
                    (self.snapshot_id, f"%{term}%", f"%{term}%"),
                ).fetchall()
                if len(weapon_rows) == 1:
                    matches.append({"kind": "weapon", **dict(weapon_rows[0]),
                                    "matched_text": token})
        matches.sort(key=lambda item: (-len(item["matched_text"]), item["kind"], item["entity_key"]))
        deduped = []
        seen = set()
        for item in matches:
            marker = (item["kind"], item["entity_key"])
            if marker not in seen:
                deduped.append(item); seen.add(marker)
        return deduped[:limit]

    def resolve_enemy_input(self, value: str) -> str | None:
        """Resolve a user-facing enemy label only when the graph is unambiguous."""
        exact = self.connection.execute(
            """SELECT enemy_key FROM catalog_enemy_archetypes
               WHERE snapshot_id=? AND lower(enemy_key)=lower(?)""",
            (self.snapshot_id, value.strip()),
        ).fetchall()
        if len(exact) == 1:
            return str(exact[0]["enemy_key"])
        roots = self.connection.execute(
            """SELECT enemy_key FROM catalog_enemy_archetypes
               WHERE snapshot_id=? AND parent_enemy_id IS NULL
                 AND lower(COALESCE(display_name,''))=lower(?)""",
            (self.snapshot_id, value.strip()),
        ).fetchall()
        if len(roots) == 1:
            return str(roots[0]["enemy_key"])
        grounded = {
            str(item["entity_key"])
            for item in self.grounded_mentions(value)
            if item["kind"] == "enemy"
        }
        return next(iter(grounded)) if len(grounded) == 1 else None

    def baseline_enemy(self) -> dict[str, Any] | None:
        """Return the structurally named default Husk pawn, never a fuzzy guess."""
        row = self.connection.execute(
            """SELECT enemy_key, display_name, semantic_status
               FROM catalog_enemy_archetypes
               WHERE snapshot_id=? AND lower(enemy_key)='default__huskpawn_c'""",
            (self.snapshot_id,),
        ).fetchone()
        return {"kind": "enemy", **dict(row)} if row else None

    def inspect_entity(self, kind: str, key_or_name: str) -> dict[str, Any]:
        if kind == "hero":
            commanders, supports = _hero_profiles(self.connection, self.snapshot_id)
            rows = [item for item in (*commanders, *supports) if key_or_name.casefold() in {
                item["hero_key"].casefold(), item["display_name"].casefold()}]
            if not rows:
                raise ValueError(f"hero must resolve at least one perk role: {key_or_name!r}")
            return {"identity": {key: rows[0][key] for key in ("hero_key", "display_name", "hero_class")},
                    "perk_roles": rows, "semantic_status": "supported" if all(
                        row["semantic_status"] == "supported" for row in rows
                    ) else "partial"}
        if kind == "hero_perk":
            return perk_family_semantic_report(
                self.connection, key_or_name.split(":", 1)[0], self.snapshot_id
            )
        if kind == "team_perk":
            return team_perk_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "ability":
            return active_ability_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "gadget":
            return gadget_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "signature":
            return signature_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "element":
            return element_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "status":
            return status_report(self.connection, key_or_name, self.snapshot_id)
        if kind == "enemy":
            return enemy_report(self.connection, self.snapshot_id, key_or_name)
        if kind == "mission":
            return mission_report(self.connection, self.snapshot_id, key_or_name)
        if kind == "modifier":
            return modifier_report(self.connection, self.snapshot_id, key_or_name)
        if kind == "weapon":
            variants = _resolve_weapon_variants(
                self.connection, self.snapshot_id, key_or_name
            )
            return {"identity": key_or_name, "variants": [
                {"variant_key": row["variant_key"], "display_name": row["display_name"],
                 "weapon_kind": row["weapon_kind"], "rarity": row["rarity"],
                 "tier": row["tier"], "legal_slots": _weapon_option_profiles(
                     self.connection, row["slot_loadout_id"]
                 )}
                for row in variants
            ]}
        matches = self.search_catalog(kind, key_or_name, 25)
        exact = [item for item in matches if key_or_name.casefold() in {
            item["entity_key"].casefold(), str(item["display_name"]).casefold()}]
        if len(exact) != 1:
            raise ValueError(f"{kind} must resolve exactly once: {key_or_name!r}")
        return exact[0]

    def validate_loadout(self, loadout: SpecifiedLoadout) -> dict[str, Any]:
        errors, warnings = [], []
        commanders, supports = _hero_profiles(self.connection, self.snapshot_id)
        def resolve(rows: Sequence[dict[str, Any]], name: str | None, kind: str) -> dict[str, Any] | None:
            if not name: return None
            found = [row for row in rows if name.casefold() in {
                row["hero_key"].casefold(), row["display_name"].casefold()}]
            if len(found) != 1: errors.append(f"{kind} must resolve exactly once: {name!r}"); return None
            return found[0]
        commander = resolve(commanders, loadout.commander, "commander")
        resolved_supports = [resolve(supports, name, "support") for name in loadout.support_heroes]
        resolved_supports = [item for item in resolved_supports if item]
        keys = [item["hero_key"] for item in resolved_supports]
        if len(keys) != len(set(keys)): errors.append("support heroes must be unique")
        if commander and commander["hero_key"] in keys: errors.append("commander cannot occupy support")
        if len(keys) > 5: errors.append("at most five support heroes are legal")
        weapon_variant = None
        variant_row = None
        try:
            variants = _resolve_weapon_variants(
                self.connection, self.snapshot_id, loadout.weapon
            )
            if len(variants) != 1:
                errors.append(
                    "specified loadout requires one exact weapon variant; "
                    f"{loadout.weapon!r} resolved {len(variants)}"
                )
            else:
                weapon_variant = variants[0]["variant_key"]
                variant_row = variants[0]
        except ValueError as error:
            errors.append(str(error))
        if variant_row is not None and loadout.weapon_perks:
            selected_profiles = []
            by_slot = {
                options[0]["slot_ordinal"]: options
                for options in _weapon_option_profiles(
                    self.connection, variant_row["slot_loadout_id"]
                ) if options
            }
            seen_slots = set()
            for slot, perk in loadout.weapon_perks:
                if slot in seen_slots:
                    errors.append(f"weapon perk slot {slot} is selected more than once")
                    continue
                seen_slots.add(slot)
                matches = [item for item in by_slot.get(slot, [])
                           if item["alteration_key"].casefold() == perk.casefold()]
                if len(matches) != 1:
                    errors.append(f"weapon perk {perk!r} is not legal in slot {slot}")
                    continue
                if not _compatible_option(selected_profiles, matches[0]):
                    errors.append(f"weapon perk {perk!r} conflicts with another selection")
                selected_profiles.append(matches[0])
        teams, gadgets = _interaction_profiles(self.connection, self.snapshot_id)
        if loadout.team_perk:
            team = [row for row in teams if loadout.team_perk.casefold() in {
                row["key"].casefold(), row["display_name"].casefold()}]
            if len(team) != 1: errors.append(f"team perk must resolve exactly once: {loadout.team_perk!r}")
            elif len(keys) == 5:
                legal, status, reasons = _team_perk_eligible(team[0]["report"], resolved_supports,
                                                              OptimizationRequest.__dataclass_fields__["hero_progression"].default)
                if not legal: errors.extend(reasons)
                if status != "supported": warnings.append("team-perk eligibility is partial")
        gadget_names = [name.casefold() for name in loadout.gadgets]
        if len(gadget_names) > 2: errors.append("at most two gadgets are legal")
        if len(gadget_names) != len(set(gadget_names)): errors.append("gadgets must be unique")
        for name in loadout.gadgets:
            if not any(name.casefold() in {row["key"].casefold(), row["display_name"].casefold()} for row in gadgets):
                errors.append(f"gadget does not resolve: {name!r}")
        return {"legal": not errors, "errors": errors, "warnings": warnings,
                "resolved": {"commander": commander, "supports": resolved_supports,
                             "weapon_variant": weapon_variant}}

    def evaluate_loadout(self, loadout: SpecifiedLoadout, scenario: CombatScenario) -> dict[str, Any]:
        legality = self.validate_loadout(loadout)
        if not legality["legal"]:
            return {"legality": legality, "evaluation": None}
        configuration = WeaponConfiguration(
            legality["resolved"]["weapon_variant"],
            tuple(WeaponPerkSelection(slot, perk) for slot, perk in loadout.weapon_perks),
        )
        evaluation = evaluate_combat(
            self.connection, configuration,
            LoadoutContext(loadout.commander, loadout.support_heroes, (), loadout.team_perk, loadout.gadgets),
            scenario, self.snapshot_id,
        )
        return {"legality": legality, "evaluation": evaluation.as_dict()}

    def optimization_request(self, intent: BuildIntent) -> OptimizationRequest:
        if not intent.weapon:
            raise ValueError("a weapon preference is required for deterministic optimization")
        if not intent.target_enemy:
            raise ValueError("an enemy context is required for deterministic optimization")
        if intent.owned_weapons and intent.weapon.casefold() not in {
            item.casefold() for item in intent.owned_weapons
        }:
            raise ValueError("requested weapon is not present in owned_weapons")
        if intent.weapon.casefold() in {
            item.casefold() for item in intent.unavailable_weapons
        }:
            raise ValueError("requested weapon is listed in unavailable_weapons")
        return OptimizationRequest(
            intent.weapon, TargetContext(
                intent.target_enemy, intent.target_element,
                intent.target_statuses, intent.enemy_modifiers,
            ),
            MissionContext(intent.mission, intent.power_level, intent.four_player,
                           intent.elemental_storm, intent.mission_modifiers),
            intent.objective_weights,
            CombatScenario(target_element=intent.target_element),
            max_results=intent.requested_alternatives,
            support_slots=intent.support_slots, gadget_slots=intent.gadget_slots,
            beam_width=intent.beam_width,
            constraints=OptimizationConstraints(
                intent.owned_heroes, intent.unavailable_heroes, intent.locked_commander,
                intent.locked_supports, intent.locked_team_perk, intent.owned_team_perks,
                intent.locked_gadgets, intent.owned_gadgets,
                intent.allow_partial, intent.allow_opaque, intent.avoid_conditions,
                intent.locked_weapon_perks,
            ),
        )

    def optimize(self, intent: BuildIntent, progress: Any = None) -> dict[str, Any]:
        return optimize_loadouts(
            self.connection, self.optimization_request(intent), self.snapshot_id,
            progress,
        )

    def compare(self, loadouts: Sequence[SpecifiedLoadout], scenario: CombatScenario) -> dict[str, Any]:
        evaluations = [self.evaluate_loadout(loadout, scenario) for loadout in loadouts]
        comparable = [item for item in evaluations if item["evaluation"]]
        statuses = {item["evaluation"]["status"] for item in comparable}
        definitive = bool(comparable) and statuses == {"supported"}
        metric_rows = []
        for index, item in enumerate(evaluations):
            evaluation = item["evaluation"]
            metric_rows.append({"index": index, "status": evaluation["status"] if evaluation else "illegal",
                                "metrics": evaluation["metrics"] if evaluation else None})
        return {"same_scenario": asdict(scenario), "definitive": definitive,
                "reason": None if definitive else "partial, opaque, or illegal builds prevent a definitive winner",
                "builds": metric_rows}

    @staticmethod
    def provenance(recommendation: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(item) for item in recommendation.get("provenance", [])
            if isinstance(item, Mapping) and
            (item.get("content_sha256") or item.get("package_path"))
        ]


def _evidence_for_recommendation(recommendation: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    def add(kind: str, text: str, status: str = "supported", source: Any = None) -> None:
        evidence.append({"id": f"e{len(evidence) + 1}", "kind": kind, "text": text,
                         "status": status, "source": source})
    add("selection", f"Commander: {recommendation['commander']['display_name']}")
    add("selection", f"Weapon: {recommendation['weapon'].get('display_name') or recommendation['weapon'].get('variant_key')}")
    for hero in recommendation.get("support_heroes", []):
        add("selection", f"Support: {hero['display_name']} ({hero['perk_family']})")
    if recommendation.get("team_perk"):
        add("selection", f"Team perk: {recommendation['team_perk']['display_name']}")
    for objective, component in recommendation.get("supported_score_components", {}).items():
        if component.get("status") == "supported":
            add("score", f"{objective} has supported normalized score {component['normalized_score']:.1f}")
    for item in recommendation.get("partial_components", [])[:4]:
        add("uncertainty", f"Partial contribution from {item.get('owner') or 'unknown source'} remains unquantified", "partial")
    for item in recommendation.get("opaque_components", [])[:4]:
        add("uncertainty", f"Opaque contribution from {item.get('owner') or 'unknown source'} remains unquantified", "opaque")
    if recommendation.get("comparison_class") != "definitive":
        add("limitation", "This recommendation is uncertainty-aware, not a definitive strongest-build claim", "partial")
    for contribution in recommendation.get("key_synergies", [])[:6]:
        add(
            "synergy",
            f"{contribution.get('origin_label') or contribution.get('origin_kind')} applies "
            f"{contribution.get('attribute')} via {contribution.get('operation')}",
            "supported",
        )
    for source in recommendation.get("provenance", [])[:8]:
        add("provenance", f"Evidence asset: {source.get('package_path') or source.get('relative_path')}", "supported", source)
    return evidence


def _synergy_chain(recommendation: Mapping[str, Any]) -> list[dict[str, Any]]:
    chain = [{"stage": "commander", "value": recommendation["commander"]}]
    chain.extend({"stage": "supported_effect", "value": item}
                 for item in recommendation.get("key_synergies", []))
    if recommendation.get("team_perk"):
        chain.append({"stage": "team_perk", "value": recommendation["team_perk"]})
    chain.extend({"stage": "support", "value": item}
                 for item in recommendation.get("support_heroes", []))
    chain.append({"stage": "scenario", "value": recommendation.get("scenario")})
    chain.append({"stage": "supported_result", "value": recommendation.get("supported_score_components")})
    return chain


def _render_evidence(selected: Sequence[str], evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    index = {item["id"]: item for item in evidence}
    unknown = sorted(set(selected) - set(index))
    if unknown:
        raise ValueError(f"reasoning provider cited unknown evidence IDs: {unknown}")
    selected_rows = [index[item] for item in selected]
    return {"summary": " ".join(item["text"] for item in selected_rows if item["kind"] != "provenance"),
            "evidence": selected_rows}


def build_intent_payload(intent: BuildIntent) -> dict[str, Any]:
    value = asdict(intent)
    value["schema_version"] = INTENT_SCHEMA_VERSION
    value["objective_weights"] = dict(intent.objective_weights)
    def loadout_payload(loadout: SpecifiedLoadout | None) -> dict[str, Any] | None:
        if loadout is None: return None
        result = asdict(loadout)
        result["weapon_perks"] = [
            {"slot": slot, "perk": perk} for slot, perk in loadout.weapon_perks
        ]
        return result
    value["current_loadout"] = loadout_payload(intent.current_loadout)
    value["comparison_loadouts"] = [
        loadout_payload(loadout) for loadout in intent.comparison_loadouts
    ]
    value["locked_weapon_perks"] = [
        {"slot": slot, "perk": perk} for slot, perk in intent.locked_weapon_perks
    ]
    return value


def _merge_followup_intent(
    previous: Mapping[str, Any], patch: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(previous)
    union_fields = {"unavailable_heroes", "unavailable_weapons", "avoid_conditions"}
    for key, value in patch.items():
        if key in {"schema_version", "user_request"}: continue
        if value is None or value == [] or value == () or value == {}: continue
        if key in union_fields:
            merged[key] = list(dict.fromkeys([*merged.get(key, []), *value]))
        else:
            merged[key] = value
    merged["schema_version"] = INTENT_SCHEMA_VERSION
    return merged


def analyze_existing_loadout(tools: StwAiTools, intent: BuildIntent) -> dict[str, Any]:
    if not intent.current_loadout:
        raise ValueError("existing-loadout analysis requires current_loadout")
    evaluated = tools.evaluate_loadout(intent.current_loadout, CombatScenario(target_element=intent.target_element))
    legality = evaluated["legality"]
    if not legality["legal"]:
        return {"legality": legality, "major_synergies": [], "weak_links": legality["errors"],
                "replacement_candidates": [], "evaluation": None}
    result = evaluated["evaluation"]
    supports = legality["resolved"]["supports"]
    families: dict[str, list[str]] = {}
    for hero in supports:
        families.setdefault(hero["perk_family"], []).append(hero["display_name"])
    redundant = [{"perk_family": family, "heroes": names,
                  "reason": "multiple support slots share one normalized perk family"}
                 for family, names in families.items() if len(names) > 1]
    weak = [issue["message"] for issue in result["issues"]]
    inactive = [item for item in result["contributions"] if not item.get("active")]
    replacements = []
    try:
        optimization = tools.optimize(replace(
            intent, mode="recommend",
            weapon=intent.weapon or intent.current_loadout.weapon,
        ))
        candidates = optimization["definitive_rankings"] or optimization["uncertainty_aware_recommendations"]
        for candidate in candidates[:intent.requested_alternatives]:
            changes = []
            if candidate["commander"]["display_name"] != intent.current_loadout.commander:
                changes.append({"slot": "commander", "replacement": candidate["commander"]})
            current_supports = {name.casefold() for name in intent.current_loadout.support_heroes}
            for hero in candidate["support_heroes"]:
                if hero["display_name"].casefold() not in current_supports:
                    changes.append({"slot": "support", "replacement": hero})
            if candidate.get("team_perk") and candidate["team_perk"]["display_name"] != intent.current_loadout.team_perk:
                changes.append({"slot": "team_perk", "replacement": candidate["team_perk"]})
            candidate_metrics = candidate["combat_evaluation"]["metrics"]
            metric_comparison = {}
            for metric in ("burst_dps", "sustained_dps"):
                current_value = result["metrics"].get(metric)
                candidate_value = candidate_metrics.get(metric)
                if current_value is not None and candidate_value is not None:
                    metric_comparison[metric] = {
                        "current": current_value, "candidate": candidate_value,
                        "difference": candidate_value - current_value,
                        "definitive": result["status"] == "supported" and
                                      candidate["comparison_class"] == "definitive",
                    }
            replacements.append({"comparison_class": candidate["comparison_class"],
                                 "supported_weighted_score": candidate["supported_weighted_score"],
                                 "changes": changes, "supported_metric_comparison": metric_comparison,
                                 "limitations": candidate["limiting_conditions"]})
    except ValueError as error:
        weak.append(f"replacement search unavailable: {error}")
    return {
        "legality": legality,
        "major_synergies": [item for item in result["contributions"] if item.get("active")],
        "wasted_or_redundant_slots": redundant,
        "conflicting_conditions": inactive,
        "weak_links": weak,
        "missing_opportunities": [item for item in replacements if item["changes"]],
        "mission_enemy_suitability": {"target_enemy": intent.target_enemy, "mission": intent.mission,
                                        "status": result["status"]},
        "partial_or_opaque": [issue for issue in result["issues"] if issue["severity"] in {"partial", "opaque"}],
        "replacement_candidates": replacements,
        "evaluation": result,
    }


class AiOrchestrator:
    def __init__(self, tools: StwAiTools, provider: ReasoningProvider | None = None):
        self.tools = tools
        self.provider = provider or DeterministicReasoningProvider()

    def run(
        self, user_text: str,
        intent_override: Mapping[str, Any] | None = None,
        *,
        previous_intent: Mapping[str, Any] | None = None,
        intent_patch: Mapping[str, Any] | None = None,
        conversation: Sequence[Mapping[str, Any]] = (),
        progress: Any = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        emit = progress or (lambda stage, detail=None: None)
        emit("understanding_request", "Grounding request against the local catalog")
        grounding = self.tools.grounded_mentions(user_text)
        raw_intent = intent_override or self.provider.interpret(
            user_text, grounding, conversation
        )
        if previous_intent and intent_override is None:
            raw_intent = _merge_followup_intent(previous_intent, raw_intent)
        if intent_patch:
            raw_intent = {**raw_intent, **intent_patch,
                          "schema_version": INTENT_SCHEMA_VERSION}
        if raw_intent.get("target_enemy"):
            resolved_target = self.tools.resolve_enemy_input(
                str(raw_intent["target_enemy"])
            )
            if resolved_target:
                raw_intent["target_enemy"] = resolved_target
        intent = BuildIntent.from_dict(raw_intent, user_text)
        emit("resolving_constraints", "Validating inventory, locks, mission, and target")
        assumptions = []
        if intent.mode == "recommend" and not intent.target_enemy:
            baseline = self.tools.baseline_enemy()
            if baseline:
                intent = replace(intent, target_enemy=baseline["enemy_key"])
                assumptions.append(
                    f"No enemy was specified; used catalog baseline {baseline['display_name']} "
                    f"({baseline['enemy_key']})."
                )
        if intent.mode == "analyze":
            if not intent.current_loadout:
                return self._clarification(intent, grounding, ["Provide current_loadout with at least a weapon."])
            emit("evaluating_candidates", "Evaluating the supplied loadout")
            analysis = analyze_existing_loadout(self.tools, intent)
            emit("preparing_recommendation", "Preparing the evidence-backed analysis")
            return {"schema_version": "stw.ai-response.v1", "provider": self.provider.provider_id,
                    "prompt_version": PROMPT_VERSION, "intent": build_intent_payload(intent), "grounding": grounding,
                    "analysis": analysis, "elapsed_ms": (time.perf_counter() - started) * 1000.0}
        if intent.mode == "compare":
            if len(intent.comparison_loadouts) < 2:
                return self._clarification(
                    intent, grounding, ["Provide at least two complete comparison_loadouts."]
                )
            emit("evaluating_candidates", "Comparing builds under one scenario")
            comparison = self.tools.compare(
                intent.comparison_loadouts,
                CombatScenario(target_element=intent.target_element),
            )
            return {"schema_version": "stw.ai-response.v1", "provider": self.provider.provider_id,
                    "prompt_version": PROMPT_VERSION, "intent": build_intent_payload(intent), "grounding": grounding,
                    "comparison": comparison,
                    "safeguards": {"same_scenario_required": True,
                                   "definitive": comparison["definitive"]},
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0}
        missing = []
        if not intent.weapon: missing.append("Which weapon or schematic should the build use?")
        if not intent.target_enemy: missing.append("Which enemy context should it be evaluated against?")
        if missing: return self._clarification(intent, grounding, missing)
        optimization = self.tools.optimize(intent, emit)
        recommendations = optimization["definitive_rankings"] or optimization["uncertainty_aware_recommendations"]
        if not recommendations:
            return self._clarification(intent, grounding, ["No candidate satisfies all supplied constraints."])
        top = recommendations[0]
        evidence = _evidence_for_recommendation(top)
        selected = self.provider.select_evidence(intent, evidence)
        explanation = _render_evidence(selected, evidence)
        emit("preparing_recommendation", "Preparing the recommendation and evidence")
        return {
            "schema_version": "stw.ai-response.v1", "provider": self.provider.provider_id,
            "prompt_version": PROMPT_VERSION, "intent": build_intent_payload(intent), "grounding": grounding,
            "recommendation": top, "alternatives": recommendations[1:intent.requested_alternatives],
            "synergy_chain": _synergy_chain(top),
            "optimizer_summary": {"counts": optimization["counts"], "search_space": optimization["search_space"],
                                  "elapsed_ms": optimization["elapsed_ms"]},
            "explanation": explanation,
            "safeguards": {"facts_source": "catalog/evaluator/optimizer only",
                           "unknown_mechanics_are_zero": False,
                           "evidence_ids_validated": True,
                           "definitive": top["comparison_class"] == "definitive"},
            "assumptions": assumptions,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }

    def _clarification(self, intent: BuildIntent, grounding: Sequence[Mapping[str, Any]], questions: Sequence[str]) -> dict[str, Any]:
        return {"schema_version": "stw.ai-response.v1", "provider": self.provider.provider_id,
                "prompt_version": PROMPT_VERSION, "status": "needs_clarification",
                "intent": build_intent_payload(intent), "grounding": list(grounding), "questions": list(questions)}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="Natural-language loadout request")
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    parser.add_argument("--intent-json", type=Path, help="Optional validated BuildIntent override")
    args = parser.parse_args(argv)
    override = json.loads(args.intent_json.read_text(encoding="utf-8")) if args.intent_json else None
    connection = connect(args.db)
    try:
        print(json.dumps(AiOrchestrator(StwAiTools(connection)).run(args.request, override),
                         indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
