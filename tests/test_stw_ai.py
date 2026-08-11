from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stw_ai import (  # noqa: E402
    AiOrchestrator,
    BuildIntent,
    DeterministicReasoningProvider,
    INTENT_SCHEMA_VERSION,
    SpecifiedLoadout,
    StwAiTools,
)
from stw_app import ApiApplication  # noqa: E402
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_combat import CombatScenario  # noqa: E402
from stw_optimizer import OptimizationConstraints, OptimizationRequest, optimize_loadouts  # noqa: E402
from stw_context import MissionContext, TargetContext  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice, write_weapon_slice  # noqa: E402
from test_stw_context import write_context_slice  # noqa: E402


class InvalidEvidenceProvider(DeterministicReasoningProvider):
    provider_id = "invalid-evidence-test"

    def select_evidence(self, intent, evidence):
        return ["fabricated-evidence-id"]


class AiReasoningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_weapon_slice(exports)
        write_golden_slice(exports)
        write_context_slice(exports)
        self.database = root / "catalog.sqlite3"
        self.connection = connect(self.database)
        self.snapshot = ingest_asset_directory(
            self.connection, exports, build_key="ai-test", exporter_version="test"
        )["snapshot_id"]
        self.tools = StwAiTools(self.connection, self.snapshot)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def intent(**changes):
        value = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "weapon": "WID_Test_SR_Ore_T05",
            "target_enemy": "HuskGeneric",
            "mission": "Ride the Lightning",
            "power_level": 160,
            "four_player": True,
            "objective_weights": {"burst_damage": 2, "crowd_clear": 1},
            "support_slots": 0,
            "gadget_slots": 0,
            "beam_width": 8,
            "requested_alternatives": 2,
        }
        value.update(changes)
        return value

    def test_build_intent_schema_rejects_unknown_objectives_and_bad_constraints(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid objective"):
            BuildIntent.from_dict(self.intent(objective_weights={"magic_damage": 1}))
        with self.assertRaisesRegex(ValueError, "support_slots"):
            BuildIntent.from_dict(self.intent(support_slots=6))
        with self.assertRaisesRegex(ValueError, "unknown BuildIntent fields"):
            BuildIntent.from_dict(self.intent(fortnite_fact_from_model=1))
        with self.assertRaisesRegex(ValueError, "weapon perk"):
            BuildIntent.from_dict(self.intent(mode="analyze", current_loadout={
                "weapon": "WID_Test_SR_Ore_T05", "weapon_perks": [{"slot": "one", "perk": "x"}]
            }))

    def test_natural_language_is_grounded_before_interpretation(self) -> None:
        text = "Build around Rescue Trooper Ramirez with the Test Rifle for 160s and burst damage"
        grounding = self.tools.grounded_mentions(text)
        raw = DeterministicReasoningProvider().interpret(text, grounding)
        intent = BuildIntent.from_dict(raw, text)
        self.assertEqual("Test Rifle", intent.weapon)
        self.assertEqual("Rescue Trooper Ramirez", intent.locked_commander)
        self.assertEqual(160, intent.power_level)
        self.assertIn(("burst_damage", 1.0), intent.objective_weights)
        enemy_grounding = self.tools.grounded_mentions("Delete Smashers")
        enemy_rows = [item for item in enemy_grounding if item["kind"] == "enemy"]
        self.assertEqual(1, len(enemy_rows))
        enemy_intent = BuildIntent.from_dict(
            DeterministicReasoningProvider().interpret("Delete Smashers", enemy_grounding)
        )
        self.assertEqual(enemy_rows[0]["entity_key"], enemy_intent.target_enemy)

    def test_realistic_language_evaluation_matrix(self) -> None:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "ai-evaluation-cases.json"
        cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
        provider = DeterministicReasoningProvider()
        self.assertGreaterEqual(len(cases), 12)
        for case in cases:
            with self.subTest(case=case["id"]):
                intent = BuildIntent.from_dict(
                    provider.interpret(case["request"], case["grounding"]),
                    case["request"],
                )
                expected = case["expect"]
                if "mode" in expected: self.assertEqual(expected["mode"], intent.mode)
                if "weapon" in expected: self.assertEqual(expected["weapon"], intent.weapon)
                if "enemy" in expected: self.assertEqual(expected["enemy"], intent.target_enemy)
                if "power_level" in expected: self.assertEqual(expected["power_level"], intent.power_level)
                if "locked_commander" in expected: self.assertEqual(expected["locked_commander"], intent.locked_commander)
                if "elemental_storm" in expected: self.assertEqual(expected["elemental_storm"], intent.elemental_storm)
                if "unavailable" in expected: self.assertIn(expected["unavailable"], intent.unavailable_heroes)
                if "avoid" in expected: self.assertIn(expected["avoid"], intent.avoid_conditions)
                if "objective" in expected:
                    self.assertIn(expected["objective"], dict(intent.objective_weights))
                if expected.get("requires_weapon_clarification"):
                    self.assertIsNone(intent.weapon)

    def test_catalog_tools_are_targeted_and_versioned(self) -> None:
        result = self.tools.search_catalog("hero", "Rescue Trooper")
        self.assertEqual(1, len(result))
        self.assertEqual("Rescue Trooper Ramirez", result[0]["display_name"])
        inspected = self.tools.inspect_entity("hero", "Rescue Trooper Ramirez")
        self.assertEqual("Rescue Trooper Ramirez", inspected["identity"]["display_name"])
        self.assertTrue(inspected["perk_roles"][0]["source"]["content_sha256"])
        schemas = self.tools.schemas()
        self.assertIn("loadout.optimize", schemas["tools"])
        self.assertNotIn("catalog.dump", schemas["tools"])

    def test_end_to_end_request_returns_optimizer_evidence_not_model_facts(self) -> None:
        result = AiOrchestrator(self.tools).run(
            "Build me a burst Test Rifle loadout for Ride the Lightning",
            self.intent(),
        )
        self.assertEqual("Rescue Trooper Ramirez", result["recommendation"]["commander"]["display_name"])
        self.assertTrue(result["explanation"]["evidence"])
        self.assertTrue(result["safeguards"]["evidence_ids_validated"])
        self.assertFalse(result["safeguards"]["unknown_mechanics_are_zero"])
        self.assertEqual("commander", result["synergy_chain"][0]["stage"])
        provenance = result["recommendation"]["provenance"]
        self.assertTrue(any(item.get("content_sha256") for item in provenance))

    def test_unconstrained_crowd_control_request_optimizes_weapon(self) -> None:
        result = AiOrchestrator(self.tools).run(
            "Generate the most effective overall loadout for crowd control",
            intent_patch={"support_slots": 0, "gadget_slots": 0, "beam_width": 8},
        )
        self.assertIn("recommendation", result)
        self.assertIn("crowd_control", result["intent"]["objective_weights"])
        self.assertIsNone(result["intent"]["weapon"])
        self.assertEqual("optimize", result["intent"]["dimension_states"]["weapon"])
        self.assertTrue(any("all legal catalog weapon" in item for item in result["assumptions"]))

    def test_whatever_weapon_is_best_and_choose_everything_do_not_clarify(self) -> None:
        for request in (
            "Whatever weapon is best for crowd control",
            "You choose everything for crowd control",
        ):
            with self.subTest(request=request):
                result = AiOrchestrator(self.tools).run(
                    request, intent_patch={"support_slots": 0, "gadget_slots": 0,
                                           "beam_width": 8}
                )
                self.assertIn("recommendation", result)
                self.assertIsNone(result["intent"]["weapon"])
                self.assertEqual(
                    "optimize", result["intent"]["dimension_states"]["weapon"]
                )
                if "everything" in request:
                    self.assertTrue(all(
                        state == "optimize"
                        for state in result["intent"]["dimension_states"].values()
                    ))
                    self.assertIsNone(result["intent"]["locked_commander"])
                    self.assertFalse(result["intent"]["locked_supports"])

    def test_explicit_weapon_lock_and_followup_delegation(self) -> None:
        grounded = [{"kind": "weapon", "entity_key": "nocturno",
                     "display_name": "Nocturno", "semantic_status": "partial"}]
        locked = BuildIntent.from_dict(
            DeterministicReasoningProvider().interpret(
                "Build the best Nocturno loadout", grounded
            )
        )
        self.assertEqual("Nocturno", locked.weapon)
        self.assertEqual("locked", locked.dimension_state("weapon"))

        first_payload = {
            **self.intent(),
            "dimension_states": {"weapon": "locked"},
            "explicit_dimensions": ["weapon"],
        }
        result = AiOrchestrator(self.tools).run(
            "Doesn't matter anymore, choose the weapon",
            previous_intent=first_payload,
            intent_patch={"support_slots": 0, "gadget_slots": 0, "beam_width": 8},
        )
        self.assertIn("recommendation", result)
        self.assertIsNone(result["intent"]["weapon"])
        self.assertEqual("optimize", result["intent"]["dimension_states"]["weapon"])

    def test_required_clarification_is_indispensable_and_never_loops(self) -> None:
        required = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "objective_weights": {"crowd_control": 1},
            "dimension_states": {"target_enemy": "required_clarification"},
            "explicit_dimensions": ["target_enemy"],
            "support_slots": 0, "gadget_slots": 0,
        }
        first = AiOrchestrator(self.tools).run("Optimize for this target", required)
        self.assertEqual("needs_clarification", first["status"])
        self.assertEqual(1, len(first["questions"]))
        second = AiOrchestrator(self.tools).run(
            "I still have not specified it", required,
            conversation=[{"role": "assistant", "content": "clarification",
                           "response": first}],
        )
        self.assertEqual("needs_clarification", second["status"])
        self.assertEqual([], second["questions"])
        self.assertTrue(second["blocking_reasons"])

    def test_provider_cannot_cite_manufactured_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown evidence IDs"):
            AiOrchestrator(self.tools, InvalidEvidenceProvider()).run(
                "Build a Test Rifle loadout", self.intent()
            )

    def test_inventory_and_locked_commander_constraints_change_legal_search(self) -> None:
        request = OptimizationRequest(
            "WID_Test_SR_Ore_T05", TargetContext("HuskGeneric"), MissionContext(),
            (("burst_damage", 1.0),), support_slots=0, gadget_slots=0,
            beam_width=8, max_results=1,
            constraints=OptimizationConstraints(
                locked_commander="Rescue Trooper Ramirez",
                owned_heroes=("Rescue Trooper Ramirez",),
            ),
        )
        result = optimize_loadouts(self.connection, request, self.snapshot)
        candidates = result["definitive_rankings"] + result["uncertainty_aware_recommendations"]
        self.assertEqual("Rescue Trooper Ramirez", candidates[0]["commander"]["display_name"])
        blocked = OptimizationRequest(
            "WID_Test_SR_Ore_T05", TargetContext("HuskGeneric"), MissionContext(),
            (("burst_damage", 1.0),), support_slots=0, gadget_slots=0,
            constraints=OptimizationConstraints(unavailable_heroes=("Rescue Trooper Ramirez",)),
        )
        with self.assertRaisesRegex(ValueError, "no legal commander"):
            optimize_loadouts(self.connection, blocked, self.snapshot)
        unavailable_weapon = BuildIntent.from_dict(self.intent(
            unavailable_weapons=["WID_Test_SR_Ore_T05"]
        ))
        with self.assertRaisesRegex(ValueError, "unavailable_weapons"):
            self.tools.optimize(unavailable_weapon)

    def test_locked_weapon_perk_is_enforced_by_real_slot_legality(self) -> None:
        row = self.connection.execute("""
            SELECT slot.slot_ordinal, alteration.alteration_key
            FROM catalog_weapon_variants variant
            JOIN catalog_weapon_slots slot ON slot.slot_loadout_id=variant.slot_loadout_id
            JOIN catalog_weapon_slot_options option ON option.weapon_slot_id=slot.id
            JOIN catalog_alterations alteration ON alteration.id=option.alteration_id
            WHERE variant.variant_key='WID_Test_SR_Ore_T05'
            ORDER BY slot.slot_ordinal, option.option_ordinal LIMIT 1
        """).fetchone()
        request = OptimizationRequest(
            "WID_Test_SR_Ore_T05", TargetContext("HuskGeneric"), MissionContext(),
            (("burst_damage", 1.0),), support_slots=0, gadget_slots=0,
            beam_width=16, max_results=1,
            constraints=OptimizationConstraints(
                locked_weapon_perks=((row["slot_ordinal"], row["alteration_key"]),)
            ),
        )
        result = optimize_loadouts(self.connection, request, self.snapshot)
        top = (result["definitive_rankings"] + result["uncertainty_aware_recommendations"])[0]
        selected = {(item["slot_ordinal"], item["alteration_key"])
                    for item in top["weapon"]["selected_perks"]}
        self.assertIn((row["slot_ordinal"], row["alteration_key"]), selected)

    def test_existing_loadout_analysis_reports_legality_and_runtime_limits(self) -> None:
        result = AiOrchestrator(self.tools).run(
            "What sucks about my current loadout?",
            self.intent(
                mode="analyze",
                current_loadout={"weapon": "WID_Test_SR_Ore_T05",
                                 "commander": "Rescue Trooper Ramirez"},
            ),
        )
        self.assertTrue(result["analysis"]["legality"]["legal"])
        self.assertIsNotNone(result["analysis"]["evaluation"])
        self.assertIn("weak_links", result["analysis"])
        self.assertTrue(result["analysis"]["replacement_candidates"])

    def test_build_comparison_refuses_definitive_winner_for_partial_results(self) -> None:
        loadout = SpecifiedLoadout("WID_Test_SR_Ore_T05", commander="Rescue Trooper Ramirez")
        comparison = self.tools.compare((loadout, loadout), CombatScenario())
        self.assertFalse(comparison["definitive"])
        self.assertIn("prevent", comparison["reason"])

    def test_comparison_orchestration_uses_one_identical_scenario(self) -> None:
        loadout = {"weapon": "WID_Test_SR_Ore_T05",
                   "commander": "Rescue Trooper Ramirez"}
        result = AiOrchestrator(self.tools).run(
            "Why is this build better than mine?",
            self.intent(mode="compare", comparison_loadouts=[loadout, loadout]),
        )
        self.assertIn("comparison", result)
        self.assertFalse(result["comparison"]["definitive"])
        self.assertTrue(result["safeguards"]["same_scenario_required"])

    def test_http_api_exposes_tool_schema_and_complete_flow(self) -> None:
        dashboard = Path(self.temporary.name) / "index.html"
        dashboard.write_text("ok", encoding="utf-8")
        application = ApiApplication(self.database, dashboard)
        status, _, body = application.dispatch("GET", "/api/ai/tools")
        self.assertEqual(200, status)
        self.assertIn("loadout.optimize", json.loads(body)["tools"])
        status, _, body = application.dispatch(
            "POST", "/api/ai/recommend",
            json.dumps({"request": "Build a Test Rifle loadout", "intent": self.intent()}).encode(),
        )
        self.assertEqual(200, status)
        self.assertIn("recommendation", json.loads(body))


if __name__ == "__main__":
    unittest.main()
