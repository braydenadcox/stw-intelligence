from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_interactions import (  # noqa: E402
    active_ability_coverage,
    active_ability_report,
    gadget_coverage,
    gadget_report,
    team_perk_coverage,
    team_perk_report,
)
from stw_pipeline import connect  # noqa: E402


ROOT = "/SaveTheWorld/Abilities/Player/Perks/Leader/TestInteraction"
ACTIVE_ROOT = "/SaveTheWorld/Abilities/Player/Commando/Actives/TestBlast"
GADGET_ROOT = "/SaveTheWorld/Abilities/Player/Generic/Gadgets/TestDeployable"


def _ref(package: str, name: str) -> dict[str, str]:
    return {"AssetPathName": f"{package}.{name}", "SubPathString": ""}


def _write(root: Path, name: str, payload: list[dict]) -> None:
    path = root / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_team_perk_slice(root: Path) -> None:
    kit = f"{ROOT}/Kit_Perk_L_TestInteraction_T01"
    ability = f"{ROOT}/GA_Perk_L_TestInteraction_T01"
    effect = f"{ROOT}/GE_Perk_L_TestInteraction_Buff"
    effect_tooltip = f"{ROOT}/TT_Perk_L_TestInteraction_T01"
    requirement_tooltip = f"{ROOT}/TT_Perk_L_TestInteraction_Req"
    _write(
        root,
        "TPID_TestInteraction",
        [
            {
                "Type": "FortTeamPerkItemDefinition",
                "Name": "TPID_TestInteraction",
                "Package": f"{ROOT}/TPID_TestInteraction",
                "Properties": {
                    "GrantedAbilityKit": _ref(kit, "Kit_Perk_L_TestInteraction_T01"),
                    "TeamPerkLoadoutConditions": [
                        {
                            "NumTimesSatisfiable": 2,
                            "RequiredTagQuery": {
                                "TokenStreamVersion": 0,
                                "TagDictionary": [{"TagName": "Keyword.Test"}],
                                "QueryTokenStream": [0, 1, 2, 1, 0],
                                "AutoDescription": " ALL( Keyword.Test )",
                            },
                            "bConsiderMinimumTier": True,
                            "bConsiderMaximumTier": False,
                            "bConsiderMinimumLevel": False,
                            "bConsiderMaximumLevel": False,
                            "bConsiderMinimumRarity": False,
                            "bConsiderMaximumRarity": False,
                            "MinimumHeroTier": "EFortItemTier::III",
                            "MaximumHeroTier": "EFortItemTier::V",
                            "MinimumHeroLevel": 1,
                            "MaximumHeroLevel": 50,
                            "MinimumHeroRarity": "EFortRarity::Common",
                            "MaximumHeroRarity": "EFortRarity::Legendary",
                        }
                    ],
                    "ItemName": {"LocalizedString": "Test Interaction"},
                    "DataList": [
                        {"TooltipClass": _ref(requirement_tooltip, "TT_Perk_L_TestInteraction_Req_C")},
                        {"Traits": ["Item.Trait.SingleStack"]},
                    ],
                },
            }
        ],
    )
    _write(
        root,
        "Kit_Perk_L_TestInteraction_T01",
        [
            {
                "Type": "FortAbilityKit",
                "Name": "Kit_Perk_L_TestInteraction_T01",
                "Package": kit,
                "Properties": {
                    "DisplayName": {"LocalizedString": "Test Interaction"},
                    "Tooltip": {"ObjectPath": f"{effect_tooltip}.0"},
                    "GameplayAbilities": [{"ObjectPath": f"{ability}.0"}],
                    "GrantedGameplayEffects": [
                        {"GameplayEffect": {"ObjectPath": f"{effect}.0"}, "Level": 1.0}
                    ],
                    "RemovedGameplayEffects": [
                        {"GameplayEffect": {"ObjectPath": f"{effect}.0"}}
                    ],
                },
            }
        ],
    )
    _write(
        root,
        "GA_Perk_L_TestInteraction_T01",
        [
            {
                "Type": "GA_Perk_L_TestInteraction_T01_C",
                "Name": "Default__GA_Perk_L_TestInteraction_T01_C",
                "Package": ability,
                "Properties": {
                    "CooldownDuration": {"Value": 8.0, "Curve": {"CurveTable": None, "RowName": "None"}},
                    "AbilityTriggers": [{"TriggerTag": {"TagName": "Event.Husk.Eliminated"}}],
                },
            }
        ],
    )
    _write(
        root,
        "GE_Perk_L_TestInteraction_Buff",
        [
            {
                "Type": "GE_Perk_L_TestInteraction_Buff_C",
                "Name": "Default__GE_Perk_L_TestInteraction_Buff_C",
                "Package": effect,
                "Properties": {
                    "DurationPolicy": "EGameplayEffectDurationType::HasDuration",
                    "DurationMagnitude": {"Value": 6.0, "Curve": {"CurveTable": None, "RowName": "None"}},
                    "Period": {"Value": 1.0, "Curve": {"CurveTable": None, "RowName": "None"}},
                    "ChanceToApplyToTarget": {"Value": 0.25, "Curve": {"CurveTable": None, "RowName": "None"}},
                    "StackingType": "EGameplayEffectStackingType::AggregateBySource",
                    "StackLimitCount": 3,
                    "StackDurationRefreshPolicy": "EGameplayEffectStackingDurationPolicy::NeverRefresh",
                    "StackPeriodResetPolicy": "EGameplayEffectStackingPeriodPolicy::NeverReset",
                    "Modifiers": [
                        {
                            "Attribute": {"AttributeName": "OutgoingAbilityDamage"},
                            "ModifierOp": "EGameplayModOp::Multiplicitive",
                            "ModifierMagnitude": {
                                "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                                "ScalableFloatMagnitude": {
                                    "Value": 1.2,
                                    "Curve": {"CurveTable": None, "RowName": "None"},
                                },
                            },
                        }
                    ],
                },
            }
        ],
    )
    for name, package, description in (
        ("TT_Perk_L_TestInteraction_T01", effect_tooltip, "Triggers a periodic stacking buff."),
        ("TT_Perk_L_TestInteraction_Req", requirement_tooltip, "REQUIRES: 2 Test heroes"),
    ):
        _write(
            root,
            name,
            [
                {
                    "Type": f"{name}_C",
                    "Name": f"Default__{name}_C",
                    "Package": package,
                    "Properties": {"Description": {"LocalizedString": description}},
                }
            ],
        )


def write_active_ability_slice(root: Path) -> None:
    hgd = "/SaveTheWorld/Heroes/Test/HGD_TestHero"
    hcgd = "/SaveTheWorld/Heroes/Classes/HCGD_TestCommando"
    kit = f"{ACTIVE_ROOT}/Kit_Commando_TestBlast"
    gadget = f"{ACTIVE_ROOT}/G_Commando_TestBlast"
    ability = f"{ACTIVE_ROOT}/GA_Commando_TestBlast"
    cooldown = f"{ACTIVE_ROOT}/GE_Commando_TestBlastCooldown"
    damage = f"{ACTIVE_ROOT}/GE_Commando_TestBlastDamage"
    stats = "/Game/Balance/DataTables/GadgetScaling"
    _write(
        root,
        "HCGD_TestCommando",
        [{
            "Type": "FortHeroClassGameplayDefinition",
            "Name": "HCGD_TestCommando",
            "Package": hcgd,
            "Properties": {
                "DisplayName": {"LocalizedString": "Test Commando"},
                "ClassAbilityKits": [_ref(kit, "Kit_Commando_TestBlast")],
            },
        }],
    )
    _write(
        root,
        "HGD_TestHero",
        [{
            "Type": "FortHeroGameplayDefinition",
            "Name": "HGD_TestHero",
            "Package": hgd,
            "Properties": {
                "HeroClassGameplayDefinition": _ref(hcgd, "HCGD_TestCommando"),
                "TierAbilityKits": [{
                    "GrantedAbilityKit": _ref(kit, "Kit_Commando_TestBlast"),
                    "MinimumHeroRarity": "EFortRarity::Rare",
                }],
            },
        }],
    )
    _write(
        root,
        "Kit_Commando_TestBlast",
        [{
            "Type": "FortAbilityKit",
            "Name": "Kit_Commando_TestBlast",
            "Package": kit,
            "Properties": {
                "DisplayName": {"LocalizedString": "Test Blast"},
                "Gadgets": [_ref(gadget, "G_Commando_TestBlast")],
                "GrantedGameplayEffects": [
                    {"GameplayEffect": _ref(damage, "GE_Commando_TestBlastDamage_C")}
                ],
            },
        }],
    )
    _write(
        root,
        "G_Commando_TestBlast",
        [{
            "Type": "FortGadgetItemDefinition",
            "Name": "G_Commando_TestBlast",
            "Package": gadget,
            "Properties": {
                "ItemName": {"LocalizedString": "Test Blast"},
                "GameplayAbility": _ref(ability, "GA_Commando_TestBlast_C"),
                "DamageStatHandle": {
                    "DataTable": _ref(stats, "GadgetScaling"),
                    "RowName": "Commando_TestBlast",
                },
            },
        }],
    )
    _write(
        root,
        "GA_Commando_TestBlast",
        [{
            "Type": "GA_Commando_TestBlast_C",
            "Name": "Default__GA_Commando_TestBlast_C",
            "Package": ability,
            "Class": "BlueprintGeneratedClass",
            "Properties": {
                "AbilityDuration": 5.0,
                "AbilityTags": ["Ability.Commando.TestBlast"],
                "ActivationBlockedTags": ["Granted.Status.AbilityBlock"],
                "CooldownGameplayEffectClass": _ref(
                    cooldown, "GE_Commando_TestBlastCooldown_C"
                ),
                "Costs": [{
                    "CostValue": {
                        "Value": 20.0,
                        "Curve": {"CurveTable": None, "RowName": "None"},
                    }
                }],
                "DamageStatHandle": {
                    "DataTable": _ref(stats, "GadgetScaling"),
                    "RowName": "Commando_TestBlast",
                },
                "EffectContainers": {
                    "ApplicationTag": {"TagName": "Ability.TestBlast.Damage"},
                    "TargetGameplayEffectClasses": [
                        _ref(damage, "GE_Commando_TestBlastDamage_C")
                    ],
                },
                "ExplosionRadiusDefault": 512.0,
            },
        }],
    )
    _write(
        root,
        "GE_Commando_TestBlastCooldown",
        [{
            "Type": "GE_Commando_TestBlastCooldown_C",
            "Name": "Default__GE_Commando_TestBlastCooldown_C",
            "Package": cooldown,
            "Properties": {
                "DurationPolicy": "EGameplayEffectDurationType::HasDuration",
                "DurationMagnitude": {
                    "Value": 12.0,
                    "Curve": {"CurveTable": None, "RowName": "None"},
                },
            },
        }],
    )
    _write(
        root,
        "GE_Commando_TestBlastDamage",
        [{
            "Type": "GE_Commando_TestBlastDamage_C",
            "Name": "Default__GE_Commando_TestBlastDamage_C",
            "Package": damage,
            "Properties": {
                "Modifiers": [{
                    "Attribute": {"AttributeName": "Health"},
                    "ModifierOp": "EGameplayModOp::Additive",
                    "ModifierMagnitude": {
                        "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                        "ScalableFloatMagnitude": {
                            "Value": -2.5,
                            "Curve": {"CurveTable": None, "RowName": "None"},
                        },
                    },
                }]
            },
        }],
    )
    _write(
        root,
        "GadgetScaling",
        [{
            "Type": "DataTable",
            "Name": "GadgetScaling",
            "Package": stats,
            "Properties": {"RowStruct": {"ObjectPath": "/Script/FortniteGame.FortBaseWeaponStats"}},
            "Rows": {
                "Commando_TestBlast": {
                    "DmgPB": 42.0,
                    "DmgScale": 1.5,
                    "EnvDmgPB": 7.0,
                }
            },
        }],
    )


def write_gadget_slice(root: Path) -> None:
    node = "/SaveTheWorld/Items/HomebaseNodes/SkillTree_TestDeployable"
    kit = f"{GADGET_ROOT}/Kit_Generic_TestDeployable"
    ability = f"{GADGET_ROOT}/GA_Generic_TestDeployable"
    effect = f"{GADGET_ROOT}/GE_Generic_TestDeployableDamage"
    actor = f"{GADGET_ROOT}/B_TestDeployable"
    _write(
        root,
        "SkillTree_TestDeployable",
        [{
            "Type": "FortHomebaseNodeItemDefinition",
            "Name": "SkillTree_TestDeployable",
            "Package": node,
            "Properties": {
                "ItemName": {"LocalizedString": "Test Deployable"},
                "LevelData": [
                    {
                        "DisplayDataId": "G_TestDeployable_0",
                        "MinCommanderLevel": 1,
                        "Cost": [],
                        "GameplayEffectRowNames": [],
                        "AbilityKit": _ref(kit, "Kit_Generic_TestDeployable"),
                        "UnlockedSquadSlots": [],
                    },
                    {
                        "DisplayDataId": "G_TestDeployable_1",
                        "MinCommanderLevel": 40,
                        "Cost": [],
                        "GameplayEffectRowNames": ["GE_GadgetUpgrade_TestDeployable_T1"],
                        "AbilityKit": {"AssetPathName": "", "SubPathString": ""},
                        "UnlockedSquadSlots": [],
                    },
                ],
            },
        }],
    )
    _write(
        root,
        "Kit_Generic_TestDeployable",
        [{
            "Type": "FortAbilityKit",
            "Name": "Kit_Generic_TestDeployable",
            "Package": kit,
            "Properties": {
                "DisplayName": {"LocalizedString": "Test Deployable"},
                "GameplayAbilities": [{"ObjectPath": f"{ability}.0"}],
                "GrantedGameplayEffects": [
                    {"GameplayEffect": {"ObjectPath": f"{effect}.0"}, "Level": 1.0}
                ],
            },
        }],
    )
    _write(
        root,
        "Kit_InternalDebugGadget",
        [{
            "Type": "FortAbilityKit",
            "Name": "Kit_InternalDebugGadget",
            "Package": f"{GADGET_ROOT}/Kit_InternalDebugGadget",
            "Properties": {"DisplayName": {"LocalizedString": "Not Selectable"}},
        }],
    )
    _write(
        root,
        "GA_Generic_TestDeployable",
        [{
            "Type": "GA_Generic_TestDeployable_C",
            "Name": "Default__GA_Generic_TestDeployable_C",
            "Package": ability,
            "Class": "BlueprintGeneratedClass'GA_Generic_TestDeployable_C'",
            "Properties": {
                "CooldownDuration": {"Value": 30.0, "Curve": {"CurveTable": None, "RowName": "None"}},
                "MaxCharges": 2,
                "DamageRadius": 512.0,
                "SpawnedActorClass": _ref(actor, "B_TestDeployable_C"),
                "ActivationRequiredTags": [{"TagName": "State.InMission"}],
            },
        }],
    )
    _write(
        root,
        "GE_Generic_TestDeployableDamage",
        [{
            "Type": "GE_Generic_TestDeployableDamage_C",
            "Name": "Default__GE_Generic_TestDeployableDamage_C",
            "Package": effect,
            "Properties": {
                "DurationPolicy": "EGameplayEffectDurationType::Instant",
                "Modifiers": [{
                    "Attribute": {"AttributeName": "Health"},
                    "ModifierOp": "EGameplayModOp::Additive",
                    "ModifierMagnitude": {
                        "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                        "ScalableFloatMagnitude": {"Value": -100.0, "Curve": {"CurveTable": None, "RowName": "None"}},
                    },
                }],
            },
        }],
    )
    _write(
        root,
        "B_TestDeployable",
        [{
            "Type": "B_TestDeployable_C",
            "Name": "Default__B_TestDeployable_C",
            "Package": actor,
            "Class": "BlueprintGeneratedClass'B_TestDeployable_C'",
            "Properties": {},
        }],
    )


class TeamPerkInteractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_team_perk_slice(exports)
        write_active_ability_slice(exports)
        write_gadget_slice(exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.first = ingest_asset_directory(
            self.connection, exports, build_key="team-perk-test", exporter_version="test"
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_team_perk_identity_eligibility_and_interactions_are_normalized(self) -> None:
        report = team_perk_report(self.connection, "Test Interaction")

        self.assertEqual("Test Interaction", report["identity"]["display_name"])
        self.assertEqual("Triggers a periodic stacking buff.", report["identity"]["effect_description"])
        self.assertEqual("REQUIRES: 2 Test heroes", report["identity"]["requirement_description"])
        self.assertEqual("supported", report["eligibility"]["status"])
        self.assertEqual(2, report["eligibility"]["required_support_slots"])
        self.assertEqual(["Keyword.Test"], report["eligibility"]["rules"][0]["required_tags"])
        self.assertEqual("EFortItemTier::III", report["eligibility"]["rules"][0]["tier"]["minimum"])
        self.assertEqual({"ability", "gameplay_effect"}, {grant["kind"] for grant in report["semantics"]["grants"]})
        self.assertEqual(
            {"granted", "removed"},
            {grant["operation"] for grant in report["semantics"]["grants"]},
        )
        mechanic_types = {row["mechanic_type"] for row in report["semantics"]["mechanics"]}
        self.assertTrue({"cooldown", "trigger", "duration", "period", "application_chance", "stacking"} <= mechanic_types)
        self.assertEqual("partial", report["semantics"]["status"])
        self.assertIn(
            "blueprint_execution",
            {row["mechanic_kind"] for row in report["semantics"]["opaque_boundaries"]},
        )
        stacking = next(
            row for row in report["semantics"]["mechanics"]
            if row["mechanic_type"] == "stacking"
        )
        self.assertEqual(
            "EGameplayEffectStackingDurationPolicy::NeverRefresh",
            stacking["value"]["StackDurationRefreshPolicy"],
        )
        self.assertTrue(report["identity"]["source"]["content_sha256"])

    def test_team_perk_ingestion_is_idempotent_and_coverage_is_measured(self) -> None:
        root = Path(self.temporary.name) / "exports"
        second = ingest_asset_directory(
            self.connection, root, build_key="team-perk-test", exporter_version="test"
        )
        coverage = team_perk_coverage(self.connection)

        self.assertTrue(second["idempotent"])
        self.assertEqual(self.first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(1, coverage["counts"]["team_perks"])
        self.assertEqual(1.0, coverage["coverage"]["identity"])
        self.assertEqual(1.0, coverage["coverage"]["eligibility_supported"])

    def test_active_ability_catalog_preserves_grants_runtime_facts_and_opacity(self) -> None:
        report = active_ability_report(self.connection, "Test Blast")

        self.assertEqual("Kit_Commando_TestBlast", report["identity"]["active_ability_key"])
        self.assertEqual(
            {"hero_loadout", "hero_class"},
            {grant["domain"] for grant in report["grantees"]},
        )
        hero = next(g for g in report["grantees"] if g["domain"] == "hero_loadout")
        self.assertEqual("EFortRarity::Rare", hero["minimum_rarity"])
        self.assertEqual("resolved", hero["evidence"]["resolution_status"])
        self.assertTrue(all(grant["resolved"] for grant in report["semantics"]["grants"]))
        self.assertEqual(
            "Default__GA_Commando_TestBlast_C",
            report["semantics"]["gameplay_ability_links"][0]["target_key"],
        )
        mechanic_types = {m["mechanic_type"] for m in report["semantics"]["mechanics"]}
        self.assertTrue(
            {"cooldown_effect", "cost", "duration", "damage_stat_row", "effect_container", "parameter"}
            <= mechanic_types
        )
        stat = next(
            m for m in report["semantics"]["mechanics"]
            if m["mechanic_type"] == "damage_stat_row" and m.get("resolved_data_row")
        )
        self.assertEqual("resolved", stat["resolved_data_row"]["status"])
        self.assertEqual(42.0, stat["resolved_data_row"]["row"]["DmgPB"])
        self.assertIn(
            "Ability.Commando.TestBlast",
            {tag["tag_name"] for tag in report["semantics"]["gameplay_tags"]},
        )
        self.assertIn(
            "blueprint_execution",
            {boundary["mechanic_kind"] for boundary in report["semantics"]["opaque_boundaries"]},
        )
        self.assertEqual("partial", report["semantics"]["status"])
        self.assertTrue(report["identity"]["source"]["content_sha256"])

    def test_active_ability_coverage_is_idempotent_and_auditable(self) -> None:
        coverage = active_ability_coverage(self.connection)

        self.assertEqual(1, coverage["counts"]["active_ability_identities"])
        self.assertEqual(1, coverage["counts"]["hero_loadout_ability_identities"])
        self.assertEqual(1, coverage["counts"]["class_granted_kit_identities"])
        self.assertEqual(2, coverage["counts"]["structural_grants"])
        self.assertEqual(1.0, coverage["coverage"]["structural_grants_resolved"])
        self.assertEqual(1.0, coverage["coverage"]["identity"])
        self.assertEqual(1.0, coverage["coverage"]["semantic_grants_resolved"])
        self.assertEqual(1.0, coverage["coverage"]["damage_stat_rows_resolved"])
        self.assertEqual(0, coverage["counts"]["deduplicated_missing_dependencies"])

    def test_selectable_gadget_identity_levels_and_shared_semantics(self) -> None:
        report = gadget_report(self.connection, "Test Deployable")

        self.assertEqual("SkillTree_TestDeployable", report["identity"]["gadget_key"])
        self.assertEqual(2, len(report["levels"]))
        self.assertEqual(40, report["levels"][1]["minimum_commander_level"])
        self.assertEqual(
            ["GE_GadgetUpgrade_TestDeployable_T1"],
            report["levels"][1]["gameplay_effect_rows"],
        )
        mechanic_types = {item["mechanic_type"] for item in report["semantics"]["mechanics"]}
        self.assertTrue({"cooldown", "activation_condition", "parameter", "spawned_entity"} <= mechanic_types)
        self.assertEqual("partial", report["semantics"]["status"])
        self.assertTrue(report["identity"]["source"]["content_sha256"])

        coverage = gadget_coverage(self.connection)
        self.assertEqual(1, coverage["counts"]["gadget_identities"])
        self.assertEqual(2, coverage["counts"]["levels"])
        self.assertEqual(1.0, coverage["ratios"]["structural_coverage"])
        names = [row[0] for row in self.connection.execute(
            "SELECT display_name FROM catalog_gadgets ORDER BY display_name"
        )]
        self.assertEqual(["Test Deployable"], names)


if __name__ == "__main__":
    unittest.main()
