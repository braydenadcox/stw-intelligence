from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from stw_assets import ingest_asset_directory  # noqa: E402
from stw_context import (  # noqa: E402
    MissionContext,
    TargetContext,
    context_coverage,
    enemy_report,
    mission_report,
    modifier_report,
    scenario_report,
)
from stw_pipeline import connect  # noqa: E402


def _write(path: Path, exports: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(exports), encoding="utf-8")


def write_context_slice(root: Path) -> None:
    stats = "/SaveTheWorld/Characters/Enemies/DataTables/CharacterAttributesAI"
    _write(root / "CharacterAttributesAI.json", [{
        "Type": "DataTable", "Name": "CharacterAttributesAI", "Package": stats,
        "Rows": {"HuskGeneric": {"Health": 100.0}, "SmasherGeneric": {"Health": 1000.0}},
    }])
    for name, row, tag, attack in (
        ("HuskPawn", "HuskGeneric", "NPC.CharacterType.Husk.Basic", "NPC.Ability.Attack.Melee.Primary"),
        ("SmasherPawn", "SmasherGeneric", "NPC.CharacterType.Smasher.Basic", "NPC.Ability.Attack.Melee.Charge"),
    ):
        package = f"/SaveTheWorld/Characters/Enemies/{name}"
        _write(root / f"{name}.json", [{
            "Type": f"{name}_C", "Name": f"Default__{name}_C", "Package": package,
            "Properties": {
                "PawnStatHandle": {"DataTable": {"ObjectPath": f"{stats}.0"}, "RowName": row},
                "GameplayTags": [{"TagName": tag}, {"TagName": attack}],
                "DefaultGameplayAbilitySets": [{"ObjectPath": "/SaveTheWorld/Abilities/NPC/Test/GAS_Test.0"}],
                "DamageZones[2]": {"Bones": ["head"], "DamageMultiplier": 2.0},
            },
        }])
    _write(root / "GAS_Test.json", [{
        "Type": "FortAbilitySet", "Name": "GAS_Test",
        "Package": "/SaveTheWorld/Abilities/NPC/Test/GAS_Test", "Properties": {},
    }])
    mission_info = "/SaveTheWorld/Missions/Primary/MissionInfo_RideTheLightning"
    _write(root / "MissionInfo.json", [{"Type": "FortMissionInfo", "Name": "MissionInfo_RideTheLightning",
        "Package": mission_info, "Properties": {}}])
    _write(root / "MissionGen.json", [{
        "Type": "FortMissionGenerator", "Name": "MissionGen_RideTheLightning",
        "Package": "/SaveTheWorld/World/MissionGens/RideTheLightning/MissionGen_RideTheLightning",
        "Properties": {"MissionName": {"LocalizedString": "Ride the Lightning"},
                       "MissionDescription": {"LocalizedString": "Defend Lars' van."},
                       "PrimaryMissionInfo": {"ObjectPath": f"{mission_info}.0"}},
    }])
    effect = "/SaveTheWorld/Abilities/GameplayModifiers/Mutations/Misc/GE_GM_MaxHealthIncrease"
    _write(root / "HealthEffect.json", [{
        "Type": "GE_GM_MaxHealthIncrease_C", "Name": "Default__GE_GM_MaxHealthIncrease_C",
        "Package": effect, "Properties": {"Modifiers": [{"Attribute": {"AttributeName": "MaxHealth"},
            "ModifierOp": "EGameplayModOp::Multiplicitive", "ModifierMagnitude": {"ScalableFloatMagnitude": {"Value": 2.0}}}]},
    }])
    _write(root / "Modifier.json", [{
        "Type": "FortGameplayModifierItemDefinition", "Name": "GM_HuskHealth",
        "Package": "/SaveTheWorld/Items/GameplayModifiers/Enemy/GM_HuskHealth",
        "Properties": {"ItemName": {"LocalizedString": "Husk Heartiness"},
            "ItemDescription": {"LocalizedString": "Husks have increased health."},
            "PersistentGameplayEffects": [{"DeliveryRequirements": {
                "bApplyToAIPawns": True,
                "IgnoredTargetTags": [{"TagName": "NPC.CharacterType.Smasher.Basic"}]},
                "GameplayEffects": [{"GameplayEffect": {"ObjectPath": f"{effect}.0"}}]}]},
    }])
    encounter_table = "/Game/AIDirector/DataTables/AIDirEncounterOptions"
    _write(root / "EncounterOptions.json", [{"Type": "DataTable", "Name": "AIDirEncounterOptions",
        "Package": encounter_table, "Rows": {"EDO_Elemental_Fire": {"Cost": 1.0}}}])
    _write(root / "EDO_Elemental_Fire.json", [{
        "Type": "EDO_Elemental_Fire_C", "Name": "Default__EDO_Elemental_Fire_C",
        "Package": "/SaveTheWorld/AIDirector/Encounters/DifficultyOptions/Modifiers/Elemental/EDO_Elemental_Fire",
        "Properties": {"ModifierTags": ["NPC.Elemental.Fire"],
            "CostAndAvailability": {"DataTable": {"ObjectPath": f"{encounter_table}.0"},
                                    "RowName": "EDO_Elemental_Fire"}},
    }])


class ContextSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_context_slice(self.root)
        self.connection = connect(self.root / "catalog.sqlite3")
        self.snapshot = ingest_asset_directory(
            self.connection, self.root, build_key="test-build"
        )["snapshot_id"]

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_enemy_identity_stats_abilities_and_weak_point_are_provenanced(self) -> None:
        report = enemy_report(self.connection, self.snapshot, "HuskGeneric")
        self.assertEqual("pawn_stat_handle", report["identity"]["identity_evidence"])
        self.assertEqual(100.0, report["stat_row"]["Health"])
        self.assertEqual("resolved", report["ability_sets"][0]["resolution_status"])
        self.assertEqual(["head"], report["damage_zones"][0]["bones"])
        self.assertTrue(report["provenance"]["content_sha256"])

    def test_mission_variants_group_by_primary_mission_reference(self) -> None:
        report = mission_report(self.connection, self.snapshot, "Ride the Lightning")
        self.assertEqual(1, len(report["variants"]))
        self.assertEqual("supported", report["semantic_status"])

    def test_modifier_preserves_effect_and_delivery_conditions(self) -> None:
        report = modifier_report(self.connection, self.snapshot, "Husk Heartiness")
        self.assertEqual("enemy", report["target_scope"])
        self.assertEqual("resolved", report["grants"][0]["resolution_status"])
        self.assertEqual("Default__GE_GM_MaxHealthIncrease_C", report["grants"][0]["effect_name"])

    def test_same_modifier_changes_applicability_by_enemy_context(self) -> None:
        mission = MissionContext(objective="Ride the Lightning", four_player=True,
                                 modifier_keys=("Husk Heartiness",))
        husk = scenario_report(self.connection, self.snapshot, TargetContext("HuskGeneric"), mission)
        smasher = scenario_report(self.connection, self.snapshot, TargetContext("SmasherGeneric"), mission)
        self.assertEqual("applies", husk["modifier_evaluations"][0]["grants"][0]["applicability"])
        self.assertEqual("excluded", smasher["modifier_evaluations"][0]["grants"][0]["applicability"])
        self.assertEqual("partial", husk["four_player_scaling"]["status"])

    def test_elemental_storm_resolves_exact_encounter_modifier(self) -> None:
        report = scenario_report(
            self.connection, self.snapshot, TargetContext("HuskGeneric"),
            MissionContext(objective="Ride the Lightning", elemental_storm="Fire"),
        )
        self.assertIn("NPC.Elemental.Fire", report["effective_target_tags"])
        self.assertEqual("supported", report["encounter_context"][0]["semantic_status"])

    def test_ingestion_and_normalization_are_idempotent(self) -> None:
        second = ingest_asset_directory(self.connection, self.root, build_key="test-build")
        self.assertEqual(self.snapshot, second["snapshot_id"])
        coverage = context_coverage(self.connection, self.snapshot)
        self.assertEqual(2, coverage["enemies"]["identities"])
        self.assertEqual(1, coverage["missions"]["objectives"])
        self.assertEqual(1, coverage["context_modifiers"]["identities"])


if __name__ == "__main__":
    unittest.main()
