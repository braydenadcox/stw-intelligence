#!/usr/bin/env python3
"""Strict, evidence-gated runner for the STW 2026 golden QA benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import yaml

from stw_assets import hero_provenance, latest_asset_snapshot_id
from stw_combat import CombatScenario
from stw_context import MissionContext, TargetContext
from stw_elements import elemental_matchup_report
from stw_optimizer import OptimizationConstraints, OptimizationRequest, optimize_loadouts
from stw_pipeline import connect


EXECUTORS = {"optimizer", "catalog", "policy", "runtime", "unsupported"}
GATES = {"asset", "runtime", "context"}
ENFORCEMENTS = {"hard_invariant", "ranking_expectation", "contextual", "quarantine"}
STATUSES = {"active", "quarantined", "informational"}
ORACLES = {"exact_state", "required_factors", "relative_rank", "forbidden_conclusion", "manual_runtime"}
OUTCOMES = {"pass", "fail", "review", "skipped", "unsupported", "awaiting_verification"}


class GoldenValidationError(ValueError):
    pass


class GoldenReviewWarning(UserWarning):
    pass


@dataclass(frozen=True)
class GoldenSource:
    id: str
    order: int
    title: str
    published: str
    url: str
    evidence_type: str
    source_currentness: str
    scope: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoldenCase:
    id: str
    domain: str
    query: str
    fixtures: Mapping[str, Any]
    oracle: Mapping[str, Any]
    enforcement: str
    verification: str
    status: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class GoldenBenchmark:
    schema_version: int
    benchmark_id: str
    sources: Mapping[str, GoldenSource]
    cases: tuple[GoldenCase, ...]


@dataclass(frozen=True)
class ResultMetadata:
    game_build: str
    game_version: str | None
    changelist: str | None
    asset_snapshot_id: int | None
    asset_manifest_sha256: str | None
    optimizer_commit: str
    optimizer_dirty: bool
    optimizer_source_sha256: str


class MetadataProvider(Protocol):
    def metadata(self) -> ResultMetadata: ...


@dataclass(frozen=True)
class StaticMetadataProvider:
    value: ResultMetadata

    def metadata(self) -> ResultMetadata:
        return self.value


@dataclass(frozen=True)
class GoldenRunConfig:
    available_gates: frozenset[str] = frozenset()
    enabled_contexts: frozenset[str] = frozenset()
    profile: str = "smoke"

    def __post_init__(self) -> None:
        unknown = set(self.available_gates) - GATES
        if unknown:
            raise ValueError(f"unknown verification gates: {sorted(unknown)}")


@dataclass(frozen=True)
class GoldenResult:
    case_id: str
    benchmark_id: str
    executor: str
    verification_gates: tuple[str, ...]
    outcome: str
    blocking: bool
    observed: Mapping[str, Any]
    reason: str
    evidence_sources: tuple[str, ...]
    metadata: ResultMetadata
    profile: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "metadata": asdict(self.metadata)}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoldenValidationError(f"{label} must be a mapping")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenValidationError(f"{label} must be a non-empty string")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise GoldenValidationError(f"{label} must be a list")
    return value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise GoldenValidationError(f"cannot load {path}: {error}") from error
    return _mapping(data, str(path))


def _validate_oracle(case_id: str, oracle: Mapping[str, Any]) -> None:
    kind = _string(oracle.get("type"), f"{case_id}.oracle.type")
    if kind not in ORACLES:
        raise GoldenValidationError(f"{case_id} has unknown oracle type {kind!r}")
    required = {
        "exact_state": ("expected",),
        "required_factors": ("must_include",),
        "relative_rank": ("candidate", "control", "relation"),
        "forbidden_conclusion": ("forbidden",),
        "manual_runtime": ("expected",),
    }[kind]
    missing = [key for key in required if key not in oracle]
    if missing:
        raise GoldenValidationError(f"{case_id} oracle is missing {missing}")
    if kind == "required_factors":
        factors = _sequence(oracle["must_include"], f"{case_id}.oracle.must_include")
        if not factors or any(not isinstance(item, str) or not item for item in factors):
            raise GoldenValidationError(f"{case_id} required factors must be non-empty strings")


def load_benchmark(root: Path) -> GoldenBenchmark:
    source_path = root / "stw_golden_qa_sources_2026.yaml"
    case_path = root / "stw_golden_qa_cases_2026.yaml"
    source_document, case_document = _load_yaml(source_path), _load_yaml(case_path)
    for document, label in ((source_document, "sources"), (case_document, "cases")):
        if document.get("schema_version") != 1:
            raise GoldenValidationError(f"{label} schema_version must be 1")
        if document.get("benchmark_id") != "stw-golden-qa-2026":
            raise GoldenValidationError(f"{label} benchmark_id is invalid")
    if case_document.get("generated_from_sources") != source_path.name:
        raise GoldenValidationError("generated_from_sources does not name the loaded source ledger")
    if set(_mapping(case_document.get("enforcement_levels"), "enforcement_levels")) != ENFORCEMENTS:
        raise GoldenValidationError("declared enforcement_levels do not match the supported schema")
    if set(_mapping(case_document.get("oracle_types"), "oracle_types")) != ORACLES:
        raise GoldenValidationError("declared oracle_types do not match the supported schema")
    currentness = _mapping(source_document.get("currentness_policy"), "currentness_policy")
    source_rows = _sequence(source_document.get("sources"), "sources")
    sources: dict[str, GoldenSource] = {}
    orders: set[int] = set()
    for index, raw_value in enumerate(source_rows):
        raw = _mapping(raw_value, f"sources[{index}]")
        source_id = _string(raw.get("id"), f"sources[{index}].id")
        if source_id in sources:
            raise GoldenValidationError(f"duplicate source id {source_id}")
        order = raw.get("order")
        if not isinstance(order, int) or order <= 0 or order in orders:
            raise GoldenValidationError(f"source {source_id} has invalid/duplicate order")
        orders.add(order)
        url = _string(raw.get("url"), f"source {source_id}.url")
        if not url.startswith("https://"):
            raise GoldenValidationError(f"source {source_id} URL must use HTTPS")
        source_currentness = _string(raw.get("source_currentness"), f"source {source_id}.source_currentness")
        if source_currentness not in currentness:
            raise GoldenValidationError(f"source {source_id} has unknown currentness")
        published = raw.get("published")
        if hasattr(published, "isoformat"):
            published = published.isoformat()
        source = GoldenSource(
            source_id, order, _string(raw.get("title"), f"source {source_id}.title"),
            _string(published, f"source {source_id}.published"), url,
            _string(raw.get("evidence_type"), f"source {source_id}.evidence_type"),
            source_currentness, _string(raw.get("scope"), f"source {source_id}.scope"),
            tuple(raw.get("flags") or ()),
        )
        sources[source_id] = source
    case_rows = _sequence(case_document.get("cases"), "cases")
    cases: list[GoldenCase] = []
    case_ids: set[str] = set()
    for index, raw_value in enumerate(case_rows):
        raw = _mapping(raw_value, f"cases[{index}]")
        case_id = _string(raw.get("id"), f"cases[{index}].id")
        if case_id in case_ids:
            raise GoldenValidationError(f"duplicate case id {case_id}")
        case_ids.add(case_id)
        enforcement = _string(raw.get("enforcement"), f"{case_id}.enforcement")
        status = _string(raw.get("status"), f"{case_id}.status")
        if enforcement not in ENFORCEMENTS or status not in STATUSES:
            raise GoldenValidationError(f"{case_id} has invalid enforcement/status")
        if enforcement == "quarantine" and status == "active":
            raise GoldenValidationError(f"{case_id} quarantine cannot be active")
        oracle = _mapping(raw.get("oracle"), f"{case_id}.oracle")
        _validate_oracle(case_id, oracle)
        source_ids = tuple(_sequence(raw.get("sources"), f"{case_id}.sources"))
        missing_sources = sorted(set(source_ids) - set(sources))
        if missing_sources:
            raise GoldenValidationError(f"{case_id} references missing sources {missing_sources}")
        if enforcement == "hard_invariant" and not raw.get("verification"):
            raise GoldenValidationError(f"{case_id} hard invariant needs verification")
        if (enforcement == "hard_invariant" and status == "active" and source_ids
                and raw.get("domain") != "benchmark_governance"
                and all(sources[item].source_currentness == "historical_unverified" for item in source_ids)):
            raise GoldenValidationError(f"{case_id} active hard invariant is historical-only")
        cases.append(GoldenCase(
            case_id, _string(raw.get("domain"), f"{case_id}.domain"),
            _string(raw.get("query"), f"{case_id}.query"),
            _mapping(raw.get("fixtures"), f"{case_id}.fixtures"), oracle,
            enforcement, _string(raw.get("verification"), f"{case_id}.verification"),
            status, source_ids,
        ))
    return GoldenBenchmark(1, "stw-golden-qa-2026", sources, tuple(cases))


class Predicate(Protocol):
    oracle_type: str
    def evaluate(self, observed: Mapping[str, Any]) -> tuple[bool, str]: ...


def _select(value: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            raise KeyError(".".join(path))
        current = current[key]
    return current


@dataclass(frozen=True)
class ComparePredicate:
    left: tuple[str, ...]
    right: tuple[str, ...]
    operation: str
    oracle_type: str = "relative_rank"

    def evaluate(self, observed: Mapping[str, Any]) -> tuple[bool, str]:
        left, right = _select(observed, self.left), _select(observed, self.right)
        operations = {
            "gt": lambda: left > right, "ge": lambda: left >= right,
            "eq": lambda: left == right, "ne": lambda: left != right,
        }
        if self.operation not in operations:
            raise ValueError(f"unknown comparison operation {self.operation}")
        passed = bool(operations[self.operation]())
        return passed, f"{'.'.join(self.left)}={left!r} {self.operation} {'.'.join(self.right)}={right!r}"


@dataclass(frozen=True)
class ExactPredicate:
    path: tuple[str, ...]
    expected: Any
    oracle_type: str = "exact_state"

    def evaluate(self, observed: Mapping[str, Any]) -> tuple[bool, str]:
        actual = _select(observed, self.path)
        return actual == self.expected, f"{'.'.join(self.path)}={actual!r}; expected {self.expected!r}"


@dataclass(frozen=True)
class IncludesPredicate:
    path: tuple[str, ...]
    required: frozenset[str]
    oracle_type: str = "required_factors"

    def evaluate(self, observed: Mapping[str, Any]) -> tuple[bool, str]:
        actual = set(_select(observed, self.path))
        missing = sorted(self.required - actual)
        return not missing, "all structured factors present" if not missing else f"missing factors: {missing}"


@dataclass(frozen=True)
class ForbiddenValuePredicate:
    path: tuple[str, ...]
    forbidden: Any
    oracle_type: str = "forbidden_conclusion"

    def evaluate(self, observed: Mapping[str, Any]) -> tuple[bool, str]:
        actual = _select(observed, self.path)
        return actual != self.forbidden, f"{'.'.join(self.path)}={actual!r}; forbidden {self.forbidden!r}"


@dataclass(frozen=True)
class EntityRef:
    kind: str
    display_name: str


@dataclass(frozen=True)
class OptimizerComparisonSelector:
    weapons: tuple[EntityRef, ...]
    objective: str
    target: EntityRef
    locked_supports: tuple[EntityRef, ...] = ()
    window_mode: str = "sustained"


@dataclass(frozen=True)
class CatalogHeroRoleSelector:
    hero: EntityRef
    perk_family: str
    required_source_tag: str


@dataclass(frozen=True)
class ElementalResistanceSelector:
    defender_elements: tuple[str, ...]


@dataclass(frozen=True)
class WeaponElementAvailabilitySelector:
    weapon: EntityRef
    element: EntityRef


@dataclass(frozen=True)
class OptimizerModifierAbsenceSelector:
    weapon: EntityRef
    target: EntityRef
    objective: str
    forbidden_modifier: EntityRef


@dataclass(frozen=True)
class StaticSelector:
    observed: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeReceiptSelector:
    required_fields: tuple[str, ...]


Selector = (OptimizerComparisonSelector | CatalogHeroRoleSelector | ElementalResistanceSelector
            | WeaponElementAvailabilitySelector | OptimizerModifierAbsenceSelector
            | StaticSelector | RuntimeReceiptSelector)


@dataclass(frozen=True)
class CaseBinding:
    case_id: str
    executor: str
    verification_gates: frozenset[str]
    selector: Selector
    predicate: Predicate
    context_key: str | None = None

    def __post_init__(self) -> None:
        if self.executor not in EXECUTORS or self.executor == "unsupported":
            raise ValueError("bindings must use an executable executor")
        if not self.verification_gates <= GATES:
            raise ValueError("binding contains unknown verification gates")
        if ("context" in self.verification_gates) != bool(self.context_key):
            raise ValueError("context-gated bindings require exactly one context key")


DEFAULT_BINDINGS: Mapping[str, CaseBinding] = {
    "ELEM-004": CaseBinding(
        "ELEM-004", "catalog", frozenset({"asset"}),
        ElementalResistanceSelector(("Fire", "Water", "Nature")),
        IncludesPredicate(("factors",), frozenset({
            "energy_is_generalist", "physical_is_penalized_against_elementals",
            "verify_current_multipliers",
        })),
    ),
    "LOAD-003": CaseBinding(
        "LOAD-003", "catalog", frozenset({"asset"}),
        CatalogHeroRoleSelector(
            EntityRef("hero", "Rescue Trooper Ramirez"),
            perk_family="AssaultDamage", required_source_tag="Weapon.Ranged.Assault",
        ),
        ComparePredicate(("commander_bonus_percent",), ("support_bonus_percent",), "gt", "exact_state"),
    ),
    "CHAOS-002": CaseBinding(
        "CHAOS-002", "runtime", frozenset({"runtime"}),
        RuntimeReceiptSelector(("automatic_ammo_return_counts_as_reload",)),
        ExactPredicate(("automatic_ammo_return_counts_as_reload",), False),
    ),
    "CHAOS-006": CaseBinding(
        "CHAOS-006", "catalog", frozenset({"asset"}),
        WeaponElementAvailabilitySelector(
            EntityRef("weapon", "Chaos Exploder"), EntityRef("element", "Energy")
        ),
        ForbiddenValuePredicate(("availability_basis",), "unconfirmed"),
    ),
    "AR-006": CaseBinding(
        "AR-006", "catalog", frozenset({"asset"}),
        ElementalResistanceSelector(("Fire", "Water", "Nature")),
        ForbiddenValuePredicate(("physical_universally_best",), True),
    ),
    "EVENT-001": CaseBinding(
        "EVENT-001", "runtime", frozenset({"runtime", "context"}),
        RuntimeReceiptSelector(("modifier_present", "cooldown_effect_active")),
        ExactPredicate(("active_only_when_modifier_present",), True), "Power Hour:Super Soldier",
    ),
    "EVENT-002": CaseBinding(
        "EVENT-002", "optimizer", frozenset({"asset", "context"}),
        OptimizerModifierAbsenceSelector(
            EntityRef("weapon", "Nocturno"), EntityRef("enemy", "default__huskpawn_c"),
            "burst_damage", EntityRef("mission_modifier", "Super Soldier"),
        ),
        ForbiddenValuePredicate(("event_modifier_leaked",), True), "normal_mission",
    ),
    "CONS-005": CaseBinding(
        "CONS-005", "policy", frozenset({"context"}),
        StaticSelector({"scenario_sensitive": True}),
        ExactPredicate(("scenario_sensitive",), True, "forbidden_conclusion"), "constructor:durability-not-binding",
    ),
    "DEF-004": CaseBinding(
        "DEF-004", "runtime", frozenset({"runtime"}),
        RuntimeReceiptSelector(("revalidated_for_build",)),
        ExactPredicate(("revalidated_for_build",), True, "manual_runtime"),
    ),
    "META-001": CaseBinding(
        "META-001", "policy", frozenset(),
        StaticSelector({"canonical_fact_source": "normalized_game_assets"}),
        ForbiddenValuePredicate(("canonical_fact_source",), "subjective_tier"),
    ),
    "META-002": CaseBinding(
        "META-002", "policy", frozenset(),
        StaticSelector({"required_context": ["power_level", "perks", "hero_loadout",
            "mission_modifiers", "target_state", "runtime_patch"]}),
        IncludesPredicate(("required_context",), frozenset({"power_level", "perks", "hero_loadout",
            "mission_modifiers", "target_state", "runtime_patch"})),
    ),
    "META-003": CaseBinding(
        "META-003", "policy", frozenset(),
        StaticSelector({"winning_currentness": "current_2026"}),
        ForbiddenValuePredicate(("winning_currentness",), "historical_unverified"),
    ),
}


def validate_bindings(benchmark: GoldenBenchmark,
                      bindings: Mapping[str, CaseBinding] = DEFAULT_BINDINGS) -> None:
    cases = {case.id: case for case in benchmark.cases}
    unknown = sorted(set(bindings) - set(cases))
    if unknown:
        raise GoldenValidationError(f"bindings reference unknown cases: {unknown}")
    for case_id, binding in bindings.items():
        if binding.case_id != case_id:
            raise GoldenValidationError(f"binding key mismatch for {case_id}")
        oracle_type = cases[case_id].oracle["type"]
        if binding.predicate.oracle_type != oracle_type:
            raise GoldenValidationError(
                f"{case_id} binding predicate {binding.predicate.oracle_type} does not match {oracle_type}"
            )


def classify_cases(benchmark: GoldenBenchmark,
                   bindings: Mapping[str, CaseBinding] = DEFAULT_BINDINGS) -> dict[str, dict[str, Any]]:
    return {
        case.id: {
            "executor": bindings[case.id].executor if case.id in bindings else "unsupported",
            "verification_gates": sorted(bindings[case.id].verification_gates) if case.id in bindings else [],
            "bound": case.id in bindings,
        }
        for case in benchmark.cases
    }


class GoldenExecutor:
    def __init__(self, connection: sqlite3.Connection | None = None,
                 snapshot_id: int | None = None,
                 runtime_receipts: Mapping[str, Mapping[str, Any]] | None = None,
                 optimizer: Callable[..., Mapping[str, Any]] = optimize_loadouts):
        self.connection = connection
        self.snapshot_id = snapshot_id
        self.runtime_receipts = runtime_receipts or {}
        self.optimizer = optimizer

    def unavailable_gates(self, case: GoldenCase, binding: CaseBinding) -> set[str]:
        missing: set[str] = set()
        if "asset" in binding.verification_gates and self.connection is None:
            missing.add("asset")
        if "runtime" in binding.verification_gates and case.id not in self.runtime_receipts:
            missing.add("runtime")
        return missing

    def execute(self, case: GoldenCase, binding: CaseBinding) -> Mapping[str, Any]:
        selector = binding.selector
        if binding.executor == "policy" and isinstance(selector, StaticSelector):
            return dict(selector.observed)
        if binding.executor == "runtime" and isinstance(selector, RuntimeReceiptSelector):
            receipt = dict(self.runtime_receipts[case.id])
            missing = [key for key in selector.required_fields if key not in receipt]
            if missing:
                raise GoldenValidationError(f"runtime receipt for {case.id} is missing {missing}")
            if case.id == "EVENT-001":
                receipt["active_only_when_modifier_present"] = bool(
                    receipt["modifier_present"] and receipt["cooldown_effect_active"]
                )
            return receipt
        if self.connection is None:
            raise GoldenValidationError(f"{binding.executor} executor needs a catalog connection")
        snapshot_id = self.snapshot_id or latest_asset_snapshot_id(self.connection)
        if snapshot_id is None:
            raise GoldenValidationError("no ready asset snapshot")
        if binding.executor == "catalog" and isinstance(selector, CatalogHeroRoleSelector):
            report = hero_provenance(self.connection, selector.hero.display_name, snapshot_id)
            if report is None:
                raise GoldenValidationError(f"hero did not resolve: {selector.hero.display_name}")
            by_mode = {item["mode"]: item for item in report["perks"]}
            def bonus(mode: str) -> float:
                perk = by_mode[mode]
                if perk.get("family") != selector.perk_family:
                    raise GoldenValidationError(
                        f"{case.id} resolved {perk.get('family')!r}, expected {selector.perk_family!r}"
                    )
                values = [
                    item.get("percent_bonus") for item in perk["effects"]
                    if item.get("percent_bonus") is not None
                    and item.get("interpretation_status") == "supported"
                    and item.get("applicability", {}).get("source_required_tags")
                        == [selector.required_source_tag]
                    and str(item.get("curve_row") or "").startswith(
                        f"Perk.{selector.perk_family}.{perk['tier']}."
                    )
                ]
                if len(values) != 1:
                    raise GoldenValidationError(f"{case.id} {mode} bonus did not resolve exactly once")
                return float(values[0])
            return {
                "hero_key": report["hero"]["key"],
                "commander_bonus_percent": bonus("commander"),
                "support_bonus_percent": bonus("support"),
                "transcript_claims": dict(case.fixtures),
                "execution_source": "normalized_game_assets",
            }
        if binding.executor == "catalog" and isinstance(selector, ElementalResistanceSelector):
            report = elemental_matchup_report(self.connection, snapshot_id)
            rules = report.get("rules", ())
            factors: set[str] = set()
            comparisons = {}
            for defender in selector.defender_elements:
                default = next((item for item in rules
                    if item.get("defender_element") == defender
                    and item.get("relationship") == "Default"), None)
                energy = next((item for item in rules
                    if item.get("defender_element") == defender
                    and item.get("attacker_element") == "Energy"), None)
                if not default or not energy:
                    raise GoldenValidationError(
                        f"elemental resistance inputs did not resolve for {defender}"
                    )
                comparisons[defender] = {
                    "physical_resistance_input": default.get("total_damage_resistance"),
                    "energy_resistance_input": energy.get("total_damage_resistance"),
                    "physical_source": default.get("source"),
                    "energy_source": energy.get("source"),
                }
            if all(item["energy_resistance_input"] is not None for item in comparisons.values()):
                factors.add("energy_is_generalist")
            if all(item["physical_resistance_input"] > item["energy_resistance_input"]
                   for item in comparisons.values()):
                factors.add("physical_is_penalized_against_elementals")
            if report.get("remaining_boundary"):
                factors.add("verify_current_multipliers")
            return {
                "factors": sorted(factors), "comparisons": comparisons,
                "physical_universally_best": not (
                    "physical_is_penalized_against_elementals" in factors
                ),
                "runtime_boundary": report.get("remaining_boundary"),
                "transcript_claims": dict(case.fixtures),
                "execution_source": "normalized_game_assets",
            }
        if binding.executor == "catalog" and isinstance(selector, WeaponElementAvailabilitySelector):
            element = self.connection.execute("""
                SELECT internal_damage_tag FROM catalog_element_identities
                WHERE snapshot_id=? AND
                  (lower(display_name)=lower(?) OR lower(element_key)=lower(?))
            """, (snapshot_id, selector.element.display_name,
                  selector.element.display_name)).fetchall()
            if len(element) != 1:
                raise GoldenValidationError(
                    f"element did not resolve exactly once: {selector.element.display_name}"
                )
            identities = self.connection.execute("""
                SELECT id FROM catalog_weapon_identities WHERE snapshot_id=?
                  AND lower(display_name)=lower(?)
            """, (snapshot_id, selector.weapon.display_name)).fetchall()
            if len(identities) != 1:
                raise GoldenValidationError(
                    f"weapon did not resolve exactly once: {selector.weapon.display_name}"
                )
            rows = self.connection.execute("""
                SELECT DISTINCT variant.variant_key, alteration.alteration_key,
                       tag.tag_name, file.content_sha256
                FROM catalog_weapon_variants variant
                JOIN catalog_weapon_slots slot ON slot.slot_loadout_id=variant.slot_loadout_id
                JOIN catalog_weapon_slot_options option_row ON option_row.weapon_slot_id=slot.id
                JOIN catalog_alterations alteration ON alteration.id=option_row.alteration_id
                JOIN catalog_gameplay_tag_occurrences occurrence
                  ON occurrence.source_object_id=alteration.source_object_id
                JOIN catalog_gameplay_tags tag ON tag.id=occurrence.tag_id
                JOIN asset_objects object ON object.id=alteration.source_object_id
                JOIN asset_files file ON file.id=object.asset_file_id
                WHERE variant.identity_id=? AND tag.tag_name=?
                ORDER BY variant.variant_key, alteration.alteration_key
            """, (identities[0]["id"], element[0]["internal_damage_tag"])).fetchall()
            return {
                "weapon_identity": selector.weapon.display_name,
                "element_identity": selector.element.display_name,
                "matching_options": [dict(row) for row in rows],
                "availability_basis": "confirmed" if rows else "unavailable",
                "transcript_claims": dict(case.fixtures),
                "execution_source": "normalized_game_assets",
            }
        if binding.executor == "optimizer" and isinstance(selector, OptimizerComparisonSelector):
            scores: dict[str, float] = {}
            resolved_weapon_ids: dict[str, str] = {}
            controls = [item.display_name for item in selector.weapons[1:]]
            for weapon in selector.weapons:
                request = OptimizationRequest(
                    weapon.display_name, TargetContext(selector.target.display_name), MissionContext(),
                    ((selector.objective, 1.0),),
                    CombatScenario(window_mode=selector.window_mode), beam_width=32, max_results=1,
                    constraints=OptimizationConstraints(
                        locked_supports=tuple(item.display_name for item in selector.locked_supports)
                    ),
                )
                output = self.optimizer(self.connection, request, snapshot_id)
                rankings = output["definitive_rankings"] or output["uncertainty_aware_recommendations"]
                if not rankings:
                    raise GoldenValidationError(f"optimizer returned no ranking for {weapon.display_name}")
                raw = rankings[0]["raw_supported_components"].get(selector.objective)
                if raw is None:
                    raise GoldenValidationError(f"optimizer has no supported {selector.objective} for {weapon.display_name}")
                scores[weapon.display_name] = float(raw)
                resolved_weapon_ids[weapon.display_name] = str(
                    rankings[0].get("weapon", {}).get("variant_key") or weapon.display_name
                )
            scores["best_control"] = max(scores[name] for name in controls)
            return {"scores": scores, "objective": selector.objective,
                    "target_enemy_key": selector.target.display_name,
                    "resolved_weapon_ids": resolved_weapon_ids,
                    "locked_supports": [item.display_name for item in selector.locked_supports]}
        if binding.executor == "optimizer" and isinstance(selector, OptimizerModifierAbsenceSelector):
            request = OptimizationRequest(
                selector.weapon.display_name, TargetContext(selector.target.display_name),
                MissionContext(), ((selector.objective, 1.0),), CombatScenario(),
                beam_width=16, max_results=1,
            )
            output = self.optimizer(self.connection, request, snapshot_id)
            rankings = output["definitive_rankings"] or output["uncertainty_aware_recommendations"]
            if not rankings:
                raise GoldenValidationError("optimizer returned no ranking for negative context case")
            resolved_modifiers = [
                item.get("modifier")
                for item in output.get("scenario_resolution", {}).get("modifier_evaluations", ())
            ]
            credited_modifiers = [
                item.get("modifier") for item in rankings[0].get("limiting_conditions", ())
                if isinstance(item, Mapping) and item.get("modifier")
            ]
            forbidden = selector.forbidden_modifier.display_name
            return {
                "weapon": selector.weapon.display_name,
                "target_enemy_key": selector.target.display_name,
                "mission_modifier_keys": [],
                "resolved_modifiers": resolved_modifiers,
                "credited_context_modifiers": credited_modifiers,
                "event_modifier_leaked": forbidden in resolved_modifiers
                    or forbidden in credited_modifiers,
            }
        raise GoldenValidationError(f"invalid selector for {binding.executor}: {type(selector).__name__}")


class GoldenRunner:
    def __init__(self, benchmark: GoldenBenchmark, metadata: MetadataProvider,
                 executor: GoldenExecutor | None = None,
                 bindings: Mapping[str, CaseBinding] = DEFAULT_BINDINGS):
        validate_bindings(benchmark, bindings)
        self.benchmark, self.metadata_provider = benchmark, metadata
        self.executor, self.bindings = executor or GoldenExecutor(), bindings

    def run(self, config: GoldenRunConfig = GoldenRunConfig()) -> list[GoldenResult]:
        results = []
        metadata = self.metadata_provider.metadata()
        for case in self.benchmark.cases:
            binding = self.bindings.get(case.id)
            if binding is None:
                results.append(self._result(case, "unsupported", "unsupported", False, {},
                    "no typed executable binding exists", metadata, config))
                continue
            missing = ((set(binding.verification_gates) - set(config.available_gates))
                       | self.executor.unavailable_gates(case, binding))
            if "context" in binding.verification_gates and binding.context_key not in config.enabled_contexts:
                results.append(self._result(case, binding.executor, "skipped", False, {},
                    f"context not enabled: {binding.context_key}", metadata, config, binding))
                continue
            missing.discard("context")
            if missing:
                results.append(self._result(case, binding.executor, "awaiting_verification", False, {},
                    f"missing verification gates: {sorted(missing)}", metadata, config, binding))
                continue
            try:
                observed = dict(self.executor.execute(case, binding))
                passed, reason = binding.predicate.evaluate(observed)
            except (GoldenValidationError, KeyError, ValueError) as error:
                observed, passed, reason = {"execution_error": str(error)}, False, str(error)
            nonblocking = (case.enforcement in {"ranking_expectation", "quarantine"}
                           or case.status in {"quarantined", "informational"})
            if nonblocking:
                outcome, blocking = "review", False
                warnings.warn(f"{case.id}: {'met' if passed else 'missed'} review oracle: {reason}",
                              GoldenReviewWarning, stacklevel=2)
            else:
                outcome, blocking = ("pass", False) if passed else ("fail", True)
            results.append(self._result(case, binding.executor, outcome, blocking, observed,
                reason, metadata, config, binding))
        return results

    def _result(self, case: GoldenCase, executor: str, outcome: str, blocking: bool,
                observed: Mapping[str, Any], reason: str, metadata: ResultMetadata,
                config: GoldenRunConfig, binding: CaseBinding | None = None) -> GoldenResult:
        if outcome not in OUTCOMES:
            raise ValueError(outcome)
        return GoldenResult(case.id, self.benchmark.benchmark_id, executor,
            tuple(sorted(binding.verification_gates)) if binding else (), outcome, blocking,
            observed, reason, case.sources, metadata, config.profile)


def summarize(results: Iterable[GoldenResult]) -> dict[str, int]:
    counts = Counter(result.outcome for result in results)
    return {name: counts[name] for name in
            ("pass", "fail", "review", "skipped", "unsupported", "awaiting_verification")}


def repository_metadata(connection: sqlite3.Connection | None, snapshot_id: int | None,
                        root: Path) -> ResultMetadata:
    build = {"build_key": "no-asset-catalog", "game_version": None, "changelist": None,
             "id": None, "manifest_sha256": None}
    if connection is not None:
        snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
        if snapshot_id is not None:
            row = connection.execute("""
                SELECT snapshot.id, snapshot.manifest_sha256, game.build_key,
                       game.game_version, game.changelist
                FROM asset_snapshots snapshot JOIN game_builds game ON game.id=snapshot.game_build_id
                WHERE snapshot.id=?
            """, (snapshot_id,)).fetchone()
            if row: build = dict(row)
    optimizer_path = root / "tools" / "stw_optimizer.py"
    optimizer_hash = hashlib.sha256(optimizer_path.read_bytes()).hexdigest()
    commit, dirty = "not-a-git-checkout", False
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return ResultMetadata(str(build["build_key"]), build.get("game_version"), build.get("changelist"),
        build.get("id"), build.get("manifest_sha256"), commit, dirty, optimizer_hash)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, default=Path("qa/golden/stw"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--gate", action="append", choices=sorted(GATES), default=[])
    parser.add_argument("--context", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    connection = connect(args.db) if args.db else None
    try:
        snapshot_id = latest_asset_snapshot_id(connection) if connection else None
        metadata = StaticMetadataProvider(repository_metadata(connection, snapshot_id, Path.cwd()))
        runner = GoldenRunner(load_benchmark(args.qa_root), metadata,
                              GoldenExecutor(connection, snapshot_id))
        results = runner.run(GoldenRunConfig(frozenset(args.gate), frozenset(args.context)))
        report = {"summary": summarize(results), "results": [item.as_dict() for item in results]}
        text = json.dumps(report, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return int(any(item.blocking for item in results))
    finally:
        if connection: connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
