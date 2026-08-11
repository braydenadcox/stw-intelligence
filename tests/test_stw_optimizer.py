from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stw_assets import ingest_asset_directory  # noqa: E402
from stw_combat import CombatScenario, LoadoutContext, WeaponConfiguration, evaluate_combat  # noqa: E402
from stw_context import MissionContext, TargetContext  # noqa: E402
from stw_optimizer import (  # noqa: E402
    HeroProgression,
    OptimizationRequest,
    SearchStats,
    _hero_beam,
    _team_perk_eligible,
    _weapon_configurations,
    _resolve_weapon_variants,
    optimize_loadouts,
)
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice, write_weapon_slice  # noqa: E402
from test_stw_context import write_context_slice  # noqa: E402
from test_stw_interactions import write_gadget_slice, write_team_perk_slice  # noqa: E402


class OptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.exports = root / "exports"
        write_weapon_slice(self.exports)
        write_golden_slice(self.exports)
        write_context_slice(self.exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.snapshot = ingest_asset_directory(
            self.connection, self.exports, build_key="optimizer-test", exporter_version="test"
        )["snapshot_id"]

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_objective_weights_are_explicit_and_normalized(self) -> None:
        request = OptimizationRequest(
            "WID_Test_SR_Ore_T05", TargetContext("HuskGeneric"),
            MissionContext("Ride the Lightning"),
            (("burst_damage", 2.0), ("survivability", 1.0)),
        )
        self.assertEqual({"burst_damage": 2 / 3, "survivability": 1 / 3}, request.weights())
        with self.assertRaisesRegex(ValueError, "unsupported optimization objectives"):
            OptimizationRequest("x", TargetContext("x"), MissionContext(), (("magic", 1),)).weights()

    def test_weapon_generation_uses_real_slots_and_prefers_highest_rarity_options(self) -> None:
        variants = _resolve_weapon_variants(
            self.connection, self.snapshot, "WID_Test_SR_Ore_T05"
        )
        configurations, theoretical, _ = _weapon_configurations(
            self.connection, variants, {"burst_damage": 1.0}, 16, None
        )
        self.assertGreater(theoretical, 0)
        selected = configurations[0]["configuration"].perks
        self.assertEqual(len(selected), len({perk.slot_ordinal for perk in selected}))
        self.assertEqual("aid_att_damage_t05", selected[0].alteration_key)
        for perk in selected:
            count = self.connection.execute("""
                SELECT COUNT(*) FROM catalog_weapon_slots slot
                JOIN catalog_weapon_slot_options option ON option.weapon_slot_id=slot.id
                JOIN catalog_alterations alteration ON alteration.id=option.alteration_id
                WHERE slot.slot_loadout_id=? AND slot.slot_ordinal=?
                  AND alteration.alteration_key=?
            """, (variants[0]["slot_loadout_id"], perk.slot_ordinal, perk.alteration_key)).fetchone()[0]
            self.assertEqual(1, count)

    def test_team_perk_requirements_use_support_tags_and_progression(self) -> None:
        report = {"eligibility": {"status": "supported", "rules": [{
            "status": "supported", "required_count": 2,
            "required_tags": ["Keyword.Dino"], "required_class_tags": [],
            "required_keyword_tags": ["Keyword.Dino"],
            "tier": {"minimum": "EFortItemTier::III", "maximum": None},
            "level": {"minimum": None, "maximum": None},
            "rarity": {"minimum": None, "maximum": None},
        }]}}
        legal, status, _ = _team_perk_eligible(
            report, [{"tags": ["Keyword.Dino"]}, {"tags": ["Keyword.Dino.Special"]}],
            HeroProgression(),
        )
        illegal, _, reasons = _team_perk_eligible(
            report, [{"tags": ["Keyword.Dino"]}, {"tags": []}], HeroProgression()
        )
        self.assertTrue(legal)
        self.assertEqual("supported", status)
        self.assertFalse(illegal)
        self.assertTrue(reasons)

    def test_hero_beam_never_reuses_commander_or_support_identity(self) -> None:
        def hero(index: int, role: str) -> dict:
            return {"hero_key": f"hero-{index}", "display_name": f"Hero {index}",
                    "perk_family": f"family-{index}", "objective_scores": {"survivability": 1},
                    "semantic_status": "supported", "role": role}
        beam = _hero_beam([hero(0, "commander"), hero(1, "commander")],
                          [hero(index, "support") for index in range(7)],
                          {"survivability": 1.0}, 5, 8, SearchStats())
        self.assertTrue(beam)
        for candidate in beam:
            identities = [candidate["commander"]["hero_key"],
                          *(item["hero_key"] for item in candidate["supports"])]
            self.assertEqual(6, len(set(identities)))

    def test_complete_search_is_scenario_bound_auditable_and_uncertainty_separated(self) -> None:
        request = OptimizationRequest(
            "WID_Test_SR_Ore_T05", TargetContext("HuskGeneric"),
            MissionContext("Ride the Lightning", four_player=True),
            (("burst_damage", 1.0),), combat_scenario=CombatScenario(window_mode="burst"),
            support_slots=0, gadget_slots=0, beam_width=8, max_results=3,
        )
        result = optimize_loadouts(self.connection, request, self.snapshot)
        self.assertGreater(result["search_space"]["weapon_configurations"], 0)
        self.assertGreater(result["counts"]["evaluated"], 0)
        recommendations = result["definitive_rankings"] + result["uncertainty_aware_recommendations"]
        top = recommendations[0]
        self.assertEqual("Rescue Trooper Ramirez", top["commander"]["display_name"])
        self.assertEqual("HuskGeneric", result["scenario_resolution"]["resolved_target"]["display_name"])
        self.assertEqual("supported", top["supported_score_components"]["burst_damage"]["status"])
        self.assertTrue(top["combat_evaluation"]["contributions"])
        self.assertTrue(all(item.get("content_sha256") for item in top["provenance"] if "relative_path" in item))

    def test_combat_kernel_applies_supported_team_and_gadget_subeffects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exports = root / "exports"
            write_weapon_slice(exports)
            write_golden_slice(exports)
            write_team_perk_slice(exports)
            write_gadget_slice(exports)
            connection = connect(root / "catalog.sqlite3")
            try:
                snapshot = ingest_asset_directory(
                    connection, exports, build_key="optimizer-interactions", exporter_version="test"
                )["snapshot_id"]
                result = evaluate_combat(
                    connection, WeaponConfiguration("WID_Test_SR_Ore_T05"),
                    LoadoutContext(team_perk="TPID_TestInteraction",
                                   gadgets=("SkillTree_TestDeployable",)),
                    CombatScenario(), snapshot,
                )
                kinds = {item["origin_kind"] for item in result.contributions}
                self.assertIn("team_perk", kinds)
                self.assertIn(
                    "gadget_runtime_targeting_not_evaluated",
                    {issue.code for issue in result.issues},
                )
                self.assertIn("gadget", {item["kind"] for item in result.provenance})
                self.assertEqual("partial", result.status)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
