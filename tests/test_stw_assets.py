from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_assets import (  # noqa: E402
    asset_export_queue,
    catalog_coverage,
    full_roster_batch_plan,
    hero_provenance,
    ingest_asset_directory,
    perk_family_semantic_report,
    record_roster_export_receipt,
    roster_coverage_report,
    unresolved_reference_report,
    weapon_catalog_coverage,
    weapon_catalog_search,
    weapon_provenance,
)
from stw_pipeline import connect  # noqa: E402


HGD_PACKAGE = "/SaveTheWorld/Heroes/Commando/GameplayDefinition/HGD_Commando_GrenadeGun"
HID_PACKAGE = "/SaveTheWorld/Heroes/Commando/ItemDefinition/HID_Commando_GrenadeGun_SR_T05"
CURVE_PACKAGE = "/Game/Balance/DataTables/CombatEffects_HeroAbilities"


def _reference(package: str, object_name: str) -> dict[str, str]:
    return {"AssetPathName": f"{package}.{object_name}", "SubPathString": ""}


def _write_export(root: Path, relative: str, payload: list[dict]) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path


def write_golden_slice(root: Path) -> list[Path]:
    perk_root = "/SaveTheWorld/Abilities/Player/Perks/Hero/AssaultDamage"
    files = [
        _write_export(
            root,
            "Heroes/HID_Commando_GrenadeGun_SR_T05.json",
            [
                {
                    "Type": "FortHeroType",
                    "Name": "HID_Commando_GrenadeGun_SR_T05",
                    "Package": HID_PACKAGE,
                    "Properties": {
                        "HeroGameplayDefinition": {
                            "ObjectPath": f"{HGD_PACKAGE}.0",
                            "ObjectName": "FortHeroGameplayDefinition'HGD_Commando_GrenadeGun'",
                        },
                        "ItemName": {
                            "SourceString": "Rescue Trooper Ramirez",
                            "LocalizedString": "Rescue Trooper Ramirez",
                        },
                        "AttributeInitKey": {
                            "AttributeInitCategory": "Soldier",
                            "AttributeInitSubCategory": "Soldier_Balanced_SR_T5",
                        },
                        "DataList": [
                            {"Rarity": "EFortRarity::Legendary"},
                            {"Tier": "EFortItemTier::V"},
                        ],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Heroes/HGD_Commando_GrenadeGun.json",
            [
                {
                    "Type": "FortHeroGameplayDefinition",
                    "Name": "HGD_Commando_GrenadeGun",
                    "Package": HGD_PACKAGE,
                    "Properties": {
                        "HeroClassGameplayDefinition": {
                            "ObjectPath": "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/HCGD_Commando.0",
                            "ObjectName": "FortHeroClassGameplayDefinition'HCGD_Commando'",
                        },
                        "HeroBaseStatlineTags": ["Hero.StatLine.Balanced"],
                        "TierAbilityKits": [
                            {
                                "GrantedAbilityKit": _reference(
                                    "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/Kit_Commando_FragGrenade",
                                    "Kit_Commando_FragGrenade",
                                ),
                                "MinimumHeroRarity": "EFortRarity::Uncommon",
                            },
                            {
                                "GrantedAbilityKit": _reference(
                                    "/SaveTheWorld/Abilities/Player/Commando/Actives/Shockwave/Kit_Commando_Shockwave",
                                    "Kit_Commando_Shockwave",
                                ),
                                "MinimumHeroRarity": "EFortRarity::Uncommon",
                            },
                            {
                                "GrantedAbilityKit": _reference(
                                    "/SaveTheWorld/Abilities/Player/Commando/Actives/GoinCommando/Kit_Commando_GoinCommando",
                                    "Kit_Commando_GoinCommando",
                                ),
                                "MinimumHeroRarity": "EFortRarity::Uncommon",
                            },
                        ],
                        "HeroPerk": {
                            "GrantedAbilityKit": _reference(
                                f"{perk_root}/Kit_Perk_H_AssaultDamage_T01",
                                "Kit_Perk_H_AssaultDamage_T01",
                            )
                        },
                        "CommanderPerk": {
                            "GrantedAbilityKit": _reference(
                                f"{perk_root}/Kit_Perk_H_AssaultDamage_T02",
                                "Kit_Perk_H_AssaultDamage_T02",
                            )
                        },
                    },
                }
            ],
        ),
    ]
    for tier, value in (("T01", 1.17), ("T02", 1.33)):
        kit_name = f"Kit_Perk_H_AssaultDamage_{tier}"
        kit_package = f"{perk_root}/{kit_name}"
        effect_name = f"GE_Perk_H_AssaultDamage_DamageBuff_{tier}"
        effect_package = f"{perk_root}/{effect_name}"
        files.append(
            _write_export(
                root,
                f"Perks/{kit_name}.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": kit_name,
                        "Package": kit_package,
                        "Properties": {
                            "DisplayName": {"SourceString": "Assault Damage"},
                            "GrantedGameplayEffects": [
                                {
                                    "GameplayEffect": _reference(
                                        effect_package, f"{effect_name}_C"
                                    ),
                                    "Level": 0,
                                }
                            ]
                        },
                    }
                ],
            )
        )
        files.append(
            _write_export(
                root,
                f"Perks/{effect_name}.json",
                [
                    {
                        "Type": f"{effect_name}_C",
                        "Name": f"Default__{effect_name}_C",
                        "Package": effect_package,
                        "Template": {
                            "ObjectPath": "/SaveTheWorld/GameplayEffectTemplates/Hero/GET_DamageMultiplier_Ranged_Hero.2"
                        },
                        "Properties": {
                            "Modifiers": [
                                {
                                    "Attribute": {"AttributeName": "OutgoingAbilityDamage"},
                                    "ModifierOp": "EGameplayModOp::Multiplicitive",
                                    "ModifierMagnitude": {
                                        "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                                        "ScalableFloatMagnitude": {
                                            "Value": 1.0,
                                            "Curve": {
                                                "CurveTable": {
                                                    "ObjectPath": f"{CURVE_PACKAGE}.0"
                                                },
                                                "RowName": f"Perk.AssaultDamage.{tier}.DamageMult",
                                            },
                                        },
                                    },
                                    "SourceTags": {
                                        "RequireTags": ["Weapon.Ranged.Assault"],
                                        "IgnoreTags": [],
                                    },
                                    "TargetTags": {"RequireTags": [], "IgnoreTags": []},
                                }
                            ],
                            "StackingType": "EGameplayEffectStackingType::AggregateByTarget",
                            "StackLimitCount": 1,
                        },
                    }
                ],
            )
        )
    files.append(
        _write_export(
            root,
            "Balance/CombatEffects_HeroAbilities.json",
            [
                {
                    "Type": "CurveTable",
                    "Name": "CombatEffects_HeroAbilities",
                    "Package": CURVE_PACKAGE,
                    "Rows": {
                        "Perk.AssaultDamage.T01.DamageMult": {
                            "Keys": [{"Time": 0.0, "Value": 1.17}]
                        },
                        "Perk.AssaultDamage.T02.DamageMult": {
                            "Keys": [{"Time": 0.0, "Value": 1.33}]
                        },
                    },
                }
            ],
        )
    )
    return files


def write_weapon_slice(root: Path) -> list[Path]:
    weapon_package = "/SaveTheWorld/Items/Weapons/Ranged/Test/WID_Test_SR_Ore_T05"
    schematic_package = "/SaveTheWorld/Items/Schematics/Ranged/Test/SID_Test_SR_Ore_T05"
    recipe_package = "/SaveTheWorld/Items/Datatables/CraftingRecipes_New"
    stats_package = "/Game/Items/DataTables/RangedWeapons"
    loadouts_package = "/SaveTheWorld/Items/Alteration_v2/SlotLoadouts"
    definitions_package = "/SaveTheWorld/Items/Alteration_v2/SlotDefs"
    groups_package = "/SaveTheWorld/Items/Alteration_v2/AlterationGroups"
    alteration_package = "/SaveTheWorld/Items/Alteration_v2/AttributeAlterations/Damage/AID_Att_Damage_T05"
    ability_set_package = "/SaveTheWorld/Items/Alteration_v2/AttributeAlterations/Damage/AS_Att_Damage_T05"
    effect_package = "/SaveTheWorld/Items/Alteration_v2/AttributeAlterations/Damage/GE_Att_Damage"
    magnitude_package = "/Game/Balance/DataTables/CombatEffectMagnitude"
    return [
        _write_export(
            root,
            "Weapons/WID_Test_SR_Ore_T05.json",
            [
                {
                    "Type": "FortWeaponRangedItemDefinition",
                    "Name": "WID_Test_SR_Ore_T05",
                    "Package": weapon_package,
                    "Properties": {
                        "ActualAnalyticFNames": ["Tag:Weapon:wid_test_sr_ore_t05"],
                        "ItemName": {"SourceString": "Test Rifle"},
                        "ItemDescription": {"SourceString": "Fixture rifle"},
                        "WeaponStatHandle": {
                            "DataTable": {"ObjectPath": f"{stats_package}.0"},
                            "RowName": "Test_SR_Ore_T05",
                        },
                        "AlterationSlotsLoadoutRow": "SlotLoadout.Test",
                        "WeaponActorClass": _reference(
                            "/SaveTheWorld/Weapons/Test/B_Test", "B_Test_C"
                        ),
                        "BaseAlteration": _reference(
                            "/Game/Items/Alterations/BaseAlteration_BallisticDamage",
                            "BaseAlteration_BallisticDamage",
                        ),
                        "PrimaryFireAbility": _reference(
                            "/Game/Abilities/Weapons/Ranged/GA_Ranged_GenericDamage",
                            "GA_Ranged_GenericDamage_C",
                        ),
                        "AmmoData": _reference(
                            "/Game/Items/Ammo/AmmoDataBulletsLight",
                            "AmmoDataBulletsLight",
                        ),
                        "TriggerType": "EFortWeaponTriggerType::Automatic",
                        "DisplayTier": "EFortDisplayTier::Brightcore",
                        "DataList": [
                            {"Rarity": "EFortRarity::Legendary"},
                            {"Tier": "EFortItemTier::V", "MaxTier": "EFortItemTier::V"},
                            {
                                "RatingLookup": {
                                    "CurveTable": {
                                        "ObjectPath": "/Game/Balance/DataTables/Rating.0"
                                    },
                                    "RowName": "Default_SR_T05",
                                }
                            },
                            {"Tags": ["Weapon.Ranged.Assault"]},
                            {"Traits": ["Item.Trait.HasDurability"]},
                        ],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Schematics/SID_Test_SR_Ore_T05.json",
            [
                {
                    "Type": "FortSchematicItemDefinition",
                    "Name": "SID_Test_SR_Ore_T05",
                    "Package": schematic_package,
                    "Properties": {
                        "CraftingRecipe": {
                            "DataTable": {"ObjectPath": f"{recipe_package}.0"},
                            "RowName": "Ranged.Test.SR.Ore.T05",
                        },
                        "DataList": [
                            {"Rarity": "EFortRarity::Legendary"},
                            {"Tier": "EFortItemTier::V", "MaxTier": "EFortItemTier::V"},
                            {
                                "RatingLookup": {
                                    "CurveTable": {
                                        "ObjectPath": "/Game/Balance/DataTables/Rating.0"
                                    },
                                    "RowName": "Default_SR_T05",
                                }
                            },
                            {"Tags": ["Weapon.Ranged.Assault"]},
                        ],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/CraftingRecipes_New.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "CraftingRecipes_New",
                    "Package": recipe_package,
                    "Rows": {
                        "Ranged.Test.SR.Ore.T05": {
                            "RecipeResults": [
                                {
                                    "ItemPrimaryAssetId": {
                                        "PrimaryAssetType": {"Name": "Weapon"},
                                        "PrimaryAssetName": "wid_test_sr_ore_t05",
                                    },
                                    "Quantity": 1,
                                }
                            ],
                            "RecipeCosts": [
                                {
                                    "ItemPrimaryAssetId": {
                                        "PrimaryAssetType": {"Name": "Ingredient"},
                                        "PrimaryAssetName": "ingredient_test",
                                    },
                                    "Quantity": 11,
                                }
                            ],
                        }
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/RangedWeapons.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "RangedWeapons",
                    "Package": stats_package,
                    "Rows": {
                        "Test_SR_Ore_T05": {
                            "BaseLevel": 1,
                            "NamedWeightRow": "Assault_Test",
                            "DmgPB": 100.0,
                            "DmgMid": 75.0,
                            "DmgLong": 50.0,
                            "DmgMaxRange": 25.0,
                            "EnvDmgPB": 20.0,
                            "ImpactDmgPB": 30.0,
                            "DmgScale": 0.05,
                            "ImpactDmgScale": 0.05,
                            "DiceCritChance": 0.2,
                            "DiceCritDamageMultiplier": 0.75,
                            "DamageZone_Critical": 1.5,
                            "FiringRate": 8.0,
                            "ReloadTime": 2.5,
                            "ClipSize": 30,
                            "DurabilityPerUse": 0.1,
                            "RngPB": 1024.0,
                            "RngMid": 2048.0,
                            "RngLong": 4096.0,
                            "RngMax": 8192.0,
                        }
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/SlotLoadouts.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "SlotLoadouts",
                    "Package": loadouts_package,
                    "Rows": {
                        "SlotLoadout.Test": {
                            "AlterationSlots": [
                                {
                                    "UnlockLevel": 5,
                                    "UnlockRarity": "EFortRarity::Common",
                                    "SlotDefinitionRow": "Slot.Test.Damage",
                                    "bRespeccable": True,
                                    "SlotInitMin": "EFortRarity::Legendary",
                                    "SlotInitMax": "EFortRarity::Legendary",
                                }
                            ]
                        }
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/SlotDefs.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "SlotDefs",
                    "Package": definitions_package,
                    "Rows": {"Slot.Test.Damage": {"InitTierGroup": "AGRP.Test.Damage"}},
                }
            ],
        ),
        _write_export(
            root,
            "Tables/AlterationGroups.json",
            [
                {
                    "Type": "DataTable",
                    "Name": "AlterationGroups",
                    "Package": groups_package,
                    "Rows": {
                        "AGRP.Test.Damage": {
                            "RarityMapping": [
                                {
                                    "Key": "EFortRarity::Legendary",
                                    "Value": {
                                        "WeightData": [
                                            {
                                                "AID": "Alteration:aid_att_damage_t05",
                                                "InitialRollWeight": 10000,
                                                "ExclusionNames": ["Damage"],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Alterations/AID_Att_Damage_T05.json",
            [
                {
                    "Type": "FortAlterationItemDefinition",
                    "Name": "AID_Att_Damage_T05",
                    "Package": alteration_package,
                    "Properties": {
                        "AlterationAbilitySet": _reference(
                            ability_set_package, "AS_Att_Damage_T05"
                        ),
                        "ItemName": {"SourceString": "Damage"},
                        "ItemDescription": {"SourceString": "+30% Damage"},
                        "DataList": [{"Rarity": "EFortRarity::Legendary"}],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Alterations/AS_Att_Damage_T05.json",
            [
                {
                    "Type": "FortAbilitySet",
                    "Name": "AS_Att_Damage_T05",
                    "Package": ability_set_package,
                    "Properties": {
                        "GrantedGameplayEffects": [
                            {
                                "GameplayEffect": _reference(
                                    effect_package, "GE_Att_Damage_C"
                                ),
                                "Level": 12,
                            }
                        ]
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Alterations/GE_Att_Damage.json",
            [
                {
                    "Type": "GE_Att_Damage_C",
                    "Name": "Default__GE_Att_Damage_C",
                    "Package": effect_package,
                    "Properties": {
                        "Modifiers": [
                            {
                                "Attribute": {"AttributeName": "OutgoingAbilityDamage"},
                                "ModifierOp": "EGameplayModOp::Multiplicitive",
                                "ModifierMagnitude": {
                                    "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                                    "ScalableFloatMagnitude": {
                                        "Value": 1.0,
                                        "Curve": {
                                            "CurveTable": {
                                                "ObjectPath": f"{magnitude_package}.0"
                                            },
                                            "RowName": "Item.All.Damage.Normal",
                                        },
                                    },
                                },
                            }
                        ]
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/CombatEffectMagnitude.json",
            [
                {
                    "Type": "CurveTable",
                    "Name": "CombatEffectMagnitude",
                    "Package": magnitude_package,
                    "Rows": {
                        "Item.All.Damage.Normal": {
                            "InterpMode": "ERichCurveInterpMode::RCIM_Linear",
                            "Keys": [
                                {"Time": 0.0, "Value": 1.0},
                                {"Time": 10000.0, "Value": 251.0},
                            ],
                        }
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Tables/Rating.json",
            [
                {
                    "Type": "CurveTable",
                    "Name": "Rating",
                    "Package": "/Game/Balance/DataTables/Rating",
                    "Rows": {
                        "Default_SR_T05": {
                            "InterpMode": "ERichCurveInterpMode::RCIM_Linear",
                            "Keys": [
                                {"Time": 1.0, "Value": 106.0},
                                {"Time": 30.0, "Value": 130.0},
                                {"Time": 60.0, "Value": 144.0},
                            ],
                        },
                        "Item.All.CritRatingToCritChance": {
                            "InterpMode": "ERichCurveInterpMode::RCIM_Linear",
                            "Keys": [
                                {"Time": 0.0, "Value": 0.0},
                                {"Time": 30.0, "Value": 0.375},
                                {"Time": 10000.0, "Value": 0.745},
                            ],
                        }
                    },
                }
            ],
        ),
    ]


def extend_with_phase_two_semantics(root: Path) -> list[Path]:
    template_package = "/SaveTheWorld/GameplayEffectTemplates/Hero/GET_DamageMultiplier_Ranged_Hero"
    class_package = "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/HCGD_Commando"
    frag_kit_package = (
        "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/Kit_Commando_FragGrenade"
    )
    frag_ability_package = (
        "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/GA_Commando_FragGrenade"
    )
    files = [
        _write_export(
            root,
            "Templates/GET_DamageMultiplier_Ranged_Hero.json",
            [
                {
                    "Type": "GET_DamageMultiplier_Ranged_Hero_C",
                    "Name": "Default__GET_DamageMultiplier_Ranged_Hero_C",
                    "Package": template_package,
                    "Properties": {
                        "DurationPolicy": "EGameplayEffectDurationType::Infinite",
                        "StackingType": "EGameplayEffectStackingType::AggregateByTarget",
                        "StackLimitCount": 1,
                        "Modifiers": [],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Classes/HCGD_Commando.json",
            [
                {
                    "Type": "FortHeroClassGameplayDefinition",
                    "Name": "HCGD_Commando",
                    "Package": class_package,
                    "Properties": {
                        "DisplayName": {"SourceString": "Soldier"},
                        "ClassTags": ["Hero.Class.Commando"],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Abilities/Kit_Commando_FragGrenade.json",
            [
                {
                    "Type": "FortAbilityKit",
                    "Name": "Kit_Commando_FragGrenade",
                    "Package": frag_kit_package,
                    "Properties": {
                        "GrantedAbilities": [
                            _reference(frag_ability_package, "GA_Commando_FragGrenade_C")
                        ]
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Abilities/GA_Commando_FragGrenade.json",
            [
                {
                    "Type": "GA_Commando_FragGrenade_C",
                    "Name": "Default__GA_Commando_FragGrenade_C",
                    "Package": frag_ability_package,
                    "Properties": {
                        "DisplayName": {"SourceString": "Frag Grenade"},
                        "CooldownDuration": 8.0,
                        "AbilityTriggers": [
                            {"TriggerTag": {"TagName": "Event.Ability.FragGrenade"}}
                        ],
                        "ActivationRequiredTags": ["Hero.Class.Commando"],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Effects/GE_CustomProc.json",
            [
                {
                    "Type": "GE_CustomProc_C",
                    "Name": "Default__GE_CustomProc_C",
                    "Package": "/Test/Effects/GE_CustomProc",
                    "Properties": {
                        "DurationPolicy": "EGameplayEffectDurationType::HasDuration",
                        "DurationMagnitude": {"Value": 5.0, "Curve": {}},
                        "Period": 1.0,
                        "ChanceToApplyToTarget": 0.25,
                        "Modifiers": [
                            {
                                "Attribute": {"AttributeName": "OutgoingAbilityDamage"},
                                "ModifierOp": "EGameplayModOp::Additive",
                                "ModifierMagnitude": {
                                    "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::CustomCalculationClass",
                                    "CustomMagnitude": {
                                        "CalculationClassMagnitude": {
                                            "ObjectPath": "/Test/Calculations/MMC_CustomProc.0"
                                        }
                                    },
                                },
                                "SourceTags": {
                                    "RequireTags": ["Weapon.Ranged.Assault"],
                                    "IgnoreTags": [],
                                },
                                "TargetTags": {
                                    "RequireTags": ["Enemy.MistMonster"],
                                    "IgnoreTags": [],
                                },
                            }
                        ],
                        "Executions": [
                            {
                                "CalculationClass": {
                                    "ObjectPath": "/Test/Calculations/Exec_CustomProc.0"
                                },
                                "CalculationModifiers": [
                                    {
                                        "CapturedAttribute": {
                                            "AttributeToCapture": {
                                                "AttributeName": "HealingSource"
                                            }
                                        },
                                        "AggregatorType": (
                                            "EGameplayEffectScopedModifierAggregatorType::"
                                            "CapturedAttributeBacked"
                                        ),
                                        "ModifierOp": "EGameplayModOp::Additive",
                                        "TargetTags": {
                                            "RequireTags": [
                                                "Granted.Perk.Blueprint.T02"
                                            ],
                                            "IgnoreTags": [],
                                        },
                                        "ModifierMagnitude": {
                                            "MagnitudeCalculationType": (
                                                "EGameplayEffectMagnitudeCalculation::"
                                                "AttributeBased"
                                            ),
                                            "AttributeBasedMagnitude": {
                                                "Coefficient": {
                                                    "Value": 1.0,
                                                    "Curve": {
                                                        "CurveTable": {
                                                            "ObjectPath": (
                                                                "/Game/Balance/DataTables/"
                                                                "CombatEffects_HeroAbilities.0"
                                                            )
                                                        },
                                                        "RowName": (
                                                            "Perk.AssaultDamage.T01.DamageMult"
                                                        ),
                                                    },
                                                }
                                            },
                                        },
                                    },
                                    {
                                        "CapturedAttribute": {
                                            "AttributeToCapture": {
                                                "AttributeName": "OutgoingBaseDamage"
                                            }
                                        },
                                        "AggregatorType": (
                                            "EGameplayEffectScopedModifierAggregatorType::"
                                            "CapturedAttributeBacked"
                                        ),
                                        "ModifierOp": "EGameplayModOp::Additive",
                                        "ModifierMagnitude": {
                                            "MagnitudeCalculationType": (
                                                "EGameplayEffectMagnitudeCalculation::"
                                                "SetByCaller"
                                            ),
                                            "SetByCallerMagnitude": {
                                                "DataTag": {
                                                    "TagName": "SetByCaller.AbilityDamage"
                                                }
                                            },
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Effects/GE_Literal.json",
            [
                {
                    "Type": "GE_Literal_C",
                    "Name": "Default__GE_Literal_C",
                    "Package": "/Test/Effects/GE_Literal",
                    "Properties": {
                        "Modifiers": [
                            {
                                "Attribute": {"AttributeName": "Armor"},
                                "ModifierOp": "EGameplayModOp::Additive",
                                "ModifierMagnitude": {
                                    "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                                    "ScalableFloatMagnitude": {"Value": 0.2, "Curve": {}},
                                },
                                "SourceTags": {"RequireTags": [], "IgnoreTags": []},
                                "TargetTags": {"RequireTags": [], "IgnoreTags": []},
                            }
                        ]
                    },
                }
            ],
        ),
        _write_export(
            root,
            "Abilities/Kit_StructuralTest.json",
            [
                {
                    "Type": "FortAbilityKit",
                    "Name": "Kit_StructuralTest",
                    "Package": "/Test/Kits/Kit_StructuralTest",
                    "Properties": {
                        "References": [
                            _reference(
                                "/Test/Missing/GE_NameAloneIsNotEvidence",
                                "GE_NameAloneIsNotEvidence_C",
                            )
                        ]
                    },
                }
            ],
        ),
    ]
    return files


class AssetCatalogTests(unittest.TestCase):
    def test_weapon_slice_links_recipe_stats_slots_and_alteration_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_weapon_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="weapon-slice-test"
                )
                coverage = weapon_catalog_coverage(
                    connection, summary["snapshot_id"]
                )
                detail = weapon_provenance(
                    connection, "Test Rifle", summary["snapshot_id"]
                )
                search = weapon_catalog_search(
                    connection, "Rifle", summary["snapshot_id"]
                )
                grant = connection.execute(
                    "SELECT grant_level FROM catalog_ability_kit_grants"
                ).fetchone()
                interpolation = connection.execute(
                    "SELECT DISTINCT interpolation FROM catalog_curve_points"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(1, coverage["counts"]["weapon_identities"])
        self.assertEqual(1, coverage["counts"]["weapon_variants"])
        self.assertEqual(1, coverage["counts"]["resolved_weapon_schematics"])
        self.assertEqual(1, coverage["counts"]["resolved_slot_options"])
        self.assertEqual(1, coverage["counts"]["supported_alterations"])
        self.assertEqual(1.0, coverage["ratios"]["weapon_schematic_link_resolution"])
        self.assertEqual("Test Rifle", search["weapons"][0]["display_name"])
        variant = detail["matches"][0]["variants"][0]
        self.assertEqual("wid_test_sr_ore_t05", variant["primary_asset_name"])
        self.assertEqual(100.0, variant["damage_point_blank"])
        self.assertEqual(0.2, variant["crit_chance"])
        self.assertEqual(11, variant["crafting_costs"][0]["quantity"])
        option = detail["matches"][0]["slot_loadouts"][0]["slots"][0]["options"][0]
        self.assertEqual("+30% Damage", option["description"])
        self.assertEqual("supported", option["semantic_status"])
        self.assertEqual(12.0, grant["grant_level"])
        self.assertEqual(
            "ERichCurveInterpMode::RCIM_Linear", interpolation["interpolation"]
        )

    def test_weapon_schematic_stays_unresolved_without_explicit_asset_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_weapon_slice(root)
            weapon_file = root / "Weapons" / "WID_Test_SR_Ore_T05.json"
            payload = json.loads(weapon_file.read_text(encoding="utf-8"))
            payload[0]["Properties"].pop("ActualAnalyticFNames")
            weapon_file.write_text(json.dumps(payload), encoding="utf-8")
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="weapon-no-guess-test"
                )
                coverage = weapon_catalog_coverage(
                    connection, summary["snapshot_id"]
                )
            finally:
                connection.close()

        self.assertEqual(0, coverage["counts"]["weapon_variants"])
        self.assertEqual(1, coverage["counts"]["unresolved_weapon_schematics"])

    def test_weapon_catalog_can_be_rederived_without_stale_leaf_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_weapon_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                first = ingest_asset_directory(
                    connection, root, build_key="weapon-rederive-test"
                )
                before = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "catalog_schematic_costs",
                        "catalog_weapon_stats",
                        "catalog_weapon_slots",
                        "catalog_weapon_slot_options",
                    )
                }
                with connection:
                    connection.execute(
                        "DELETE FROM asset_normalization_runs WHERE snapshot_id=?",
                        (first["snapshot_id"],),
                    )
                second = ingest_asset_directory(
                    connection, root, build_key="weapon-rederive-test"
                )
                after = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                }
            finally:
                connection.close()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertFalse(second["idempotent"])
        self.assertEqual(before, after)

    def test_hero_class_kits_preserve_real_gameplay_ability_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            class_package = (
                "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/HCGD_Commando"
            )
            kit_package = (
                "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/StayFrosty/"
                "Kit_Perk_C_Commando_StayFrosty_T01"
            )
            ability_package = (
                "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/StayFrosty/"
                "GA_Perk_C_Commando_StayFrosty_T01"
            )
            effect_package = (
                "/SaveTheWorld/Abilities/Player/Perks/Class/Commando/StayFrosty/"
                "GE_Perk_C_Commando_StayFrosty_T01_Tag"
            )
            _write_export(
                root,
                "Classes/HCGD_Commando.json",
                [
                    {
                        "Type": "FortHeroClassGameplayDefinition",
                        "Name": "HCGD_Commando",
                        "Package": class_package,
                        "Properties": {
                            "ClassAbilityKits": [
                                {"ObjectPath": f"{kit_package}.Kit"}
                            ]
                        },
                    }
                ],
            )
            _write_export(
                root,
                "Classes/Kit_Perk_C_Commando_StayFrosty_T01.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit",
                        "Package": kit_package,
                        "Properties": {
                            "DisplayName": {"SourceString": "Stay Frosty"},
                            "GameplayAbilities": {
                                "ObjectPath": f"{ability_package}.0"
                            },
                            "GrantedGameplayEffects": {
                                "GameplayEffect": {
                                    "ObjectPath": f"{effect_package}.1"
                                }
                            },
                        },
                    }
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="hero-class-kit-test"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                coverage = catalog_coverage(connection, summary["snapshot_id"])
            finally:
                connection.close()

        assert hero is not None
        class_kit = hero["hero"]["class_kits"][0]
        self.assertEqual("Stay Frosty", class_kit["display_name"])
        self.assertEqual("partial_missing_grants", class_kit["status"])
        self.assertEqual(
            sorted([f"{ability_package}.0", f"{effect_package}.1"]),
            class_kit["unresolved_grants"],
        )
        self.assertEqual(1, coverage["counts"]["hero_class_kits"])
        self.assertEqual(1, coverage["counts"]["resolved_hero_class_kit_files"])

    def test_linked_gameplay_ability_and_referenced_data_row_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            kit = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "Kit_Commando_FragGrenade"
            )
            gadget = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "G_Commando_FragGrenade"
            )
            gameplay_ability = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "GA_Commando_FragGrenade_WithTrajectory"
            )
            table = "/Game/Balance/DataTables/GadgetScaling"
            _write_export(
                root,
                "Abilities/Kit_Commando_FragGrenade.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit_Commando_FragGrenade",
                        "Package": kit,
                        "Properties": {
                            "Gadgets": [{"ObjectPath": f"{gadget}.Gadget"}]
                        },
                    }
                ],
            )
            _write_export(
                root,
                "Abilities/G_Commando_FragGrenade.json",
                [
                    {
                        "Type": "FortGadgetItemDefinition",
                        "Name": "Gadget",
                        "Package": gadget,
                        "Properties": {
                            "GameplayAbility": {
                                "ObjectPath": f"{gameplay_ability}.0"
                            }
                        },
                    }
                ],
            )
            _write_export(
                root,
                "Abilities/GA_Commando_FragGrenade_WithTrajectory.json",
                [
                    {
                        "Type": "BlueprintGeneratedClass",
                        "Name": "GA_Commando_FragGrenade_WithTrajectory_C",
                        "Package": gameplay_ability,
                        "Properties": {},
                    },
                    {
                        "Type": "GA_Commando_FragGrenade_WithTrajectory_C",
                        "Name": "Default__GA_Commando_FragGrenade_WithTrajectory_C",
                        "Package": gameplay_ability,
                        "Properties": {
                            "AbilityDuration": 8.0,
                            "Costs": {"CostValue": {"Value": 30.0, "Curve": {}}},
                            "DamageStatHandle": {
                                "DataTable": {"ObjectPath": f"{table}.0"},
                                "RowName": "Commando_FragGrenade",
                            },
                        },
                    },
                ],
            )
            _write_export(
                root,
                "Balance/GadgetScaling.json",
                [
                    {
                        "Type": "DataTable",
                        "Name": "GadgetScaling",
                        "Package": table,
                        "Properties": {},
                        "Rows": {
                            "Commando_FragGrenade": {"DmgPB": 153.0},
                            "Unreferenced": {"DmgPB": 999.0},
                        },
                    }
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="linked-ability-test"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                coverage = catalog_coverage(connection, summary["snapshot_id"])
                data_rows = connection.execute(
                    "SELECT row_name, row_json FROM catalog_data_rows"
                ).fetchall()
            finally:
                connection.close()

        assert hero is not None
        frag = hero["abilities"][0]
        self.assertEqual("resolved", frag["status"])
        self.assertEqual(1, len(frag["implementations"]))
        self.assertEqual(
            gameplay_ability, frag["implementations"][0]["package_path"]
        )
        self.assertEqual(
            {"duration", "cost", "damage_stat_row"},
            {row["type"] for row in frag["implementations"][0]["mechanics"]},
        )
        duration = next(
            row
            for row in frag["implementations"][0]["mechanics"]
            if row["type"] == "duration"
        )
        self.assertEqual(8.0, duration["magnitude"]["literal_value"])
        cost = next(
            row
            for row in frag["implementations"][0]["mechanics"]
            if row["type"] == "cost"
        )
        self.assertEqual(30.0, cost["magnitude"]["literal_value"])
        self.assertEqual(1, coverage["counts"]["ability_links"])
        self.assertEqual(1, coverage["counts"]["referenced_data_rows"])
        self.assertEqual(
            [("Commando_FragGrenade", '{"DmgPB":153.0}')],
            [tuple(row) for row in data_rows],
        )

    def test_real_gadget_exposes_exact_gameplay_ability_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            kit = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "Kit_Commando_FragGrenade"
            )
            gadget = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "G_Commando_FragGrenade"
            )
            gameplay_ability = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "GA_Commando_FragGrenade_WithTrajectory"
            )
            _write_export(
                root,
                "Abilities/Kit_Commando_FragGrenade.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit_Commando_FragGrenade",
                        "Package": kit,
                        "Properties": {
                            "DisplayName": {"SourceString": "Frag Grenade"},
                            "Gadgets": [
                                {"ObjectPath": f"{gadget}.G_Commando_FragGrenade"}
                            ],
                        },
                    }
                ],
            )
            _write_export(
                root,
                "Abilities/G_Commando_FragGrenade.json",
                [
                    {
                        "Type": "FortGadgetItemDefinition",
                        "Name": "G_Commando_FragGrenade",
                        "Package": gadget,
                        "Properties": {
                            "GameplayAbility": {
                                "AssetPathName": (
                                    f"{gameplay_ability}."
                                    "GA_Commando_FragGrenade_WithTrajectory_C"
                                )
                            }
                        },
                    }
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="real-gadget-test"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                queue = asset_export_queue(
                    connection,
                    summary["snapshot_id"],
                    hero_name="Rescue Trooper Ramirez",
                )
            finally:
                connection.close()

        assert hero is not None
        frag = hero["abilities"][0]
        self.assertEqual("partial_missing_grants", frag["status"])
        self.assertEqual(
            [f"{gameplay_ability}.GA_Commando_FragGrenade_WithTrajectory_C"],
            frag["unresolved_grants"],
        )
        queued = next(
            asset
            for asset in queue["assets"]
            if asset["package_path"] == gameplay_ability
        )
        self.assertEqual(0, queued["priority"])
        self.assertIn("active_ability_logic", queued["categories"])

    def test_grant_resolves_one_semantic_effect_from_multi_export_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            support_kit = next(
                path
                for path in source_files
                if path.name == "Kit_Perk_H_AssaultDamage_T01.json"
            )
            payload = json.loads(support_kit.read_text(encoding="utf-8"))
            payload[0]["Properties"]["GrantedGameplayEffects"].append(
                {
                    "GameplayEffect": {"ObjectPath": "/Test/Effects/GE_Multi.1"},
                    "Level": 0,
                }
            )
            support_kit.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
            _write_export(
                root,
                "Effects/GE_Multi.json",
                [
                    {
                        "Type": "AssetTagsGameplayEffectComponent",
                        "Name": "AssetTagsGameplayEffectComponent_0",
                        "Package": "/Test/Effects/GE_Multi",
                        "Properties": {
                            "InheritableAssetTags": {
                                "Added": ["Asset.Test.MultiExport"]
                            }
                        },
                    },
                    {
                        "Type": "BlueprintGeneratedClass",
                        "Name": "GE_Multi_C",
                        "Package": "/Test/Effects/GE_Multi",
                        "Properties": {},
                    },
                    {
                        "Type": "GE_Multi_C",
                        "Name": "Default__GE_Multi_C",
                        "Package": "/Test/Effects/GE_Multi",
                        "Properties": {
                            "GEComponents": [],
                            "DurationMagnitude": {
                                "ScalableFloatMagnitude": {"Value": 7.0, "Curve": {}}
                            },
                        },
                    },
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                ingest_asset_directory(connection, root, build_key="multi-export-test")
                rows = connection.execute(
                    """
                    SELECT effect.effect_name, object.export_index
                    FROM catalog_ability_kit_grants grant_row
                    JOIN catalog_gameplay_effects effect
                      ON effect.id=grant_row.gameplay_effect_id
                    JOIN asset_objects object ON object.id=effect.source_object_id
                    WHERE grant_row.target_path='/Test/Effects/GE_Multi.1'
                    """
                ).fetchall()
                duration = connection.execute(
                    """
                    SELECT literal_value, calculation_type, interpretation_status
                    FROM catalog_magnitudes
                    WHERE purpose='effect_duration'
                    """
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(
            [("Default__GE_Multi_C", 2)], [tuple(row) for row in rows]
        )
        self.assertEqual((7.0, "ScalableFloat", "supported"), tuple(duration))

    def test_real_fmodel_kit_shapes_remain_partial_until_grants_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            support_kit = next(
                path
                for path in source_files
                if path.name == "Kit_Perk_H_AssaultDamage_T01.json"
            )
            payload = json.loads(support_kit.read_text(encoding="utf-8"))
            payload[0]["Properties"]["GrantedGameplayEffects"].append(
                {
                    "GameplayEffect": {
                        "ObjectPath": "/Test/Missing/GE_AssaultTag.1"
                    },
                    "Level": 0,
                }
            )
            support_kit.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )

            frag_kit = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "Kit_Commando_FragGrenade"
            )
            gadget = (
                "/SaveTheWorld/Abilities/Player/Commando/Actives/FragGrenade/"
                "G_Commando_FragGrenade"
            )
            _write_export(
                root,
                "Abilities/Kit_Commando_FragGrenade.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit_Commando_FragGrenade",
                        "Package": frag_kit,
                        "Properties": {
                            "DisplayName": {"SourceString": "Frag Grenade"},
                            "Gadgets": [
                                {"ObjectPath": f"{gadget}.G_Commando_FragGrenade"}
                            ],
                            "GrantedGameplayEffects": [
                                {
                                    "GameplayEffect": {
                                        "ObjectPath": "/Test/Missing/GE_FragTag.1"
                                    },
                                    "Level": 0,
                                }
                            ],
                        },
                    }
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="real-kit-shape-test"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                queue = asset_export_queue(
                    connection,
                    summary["snapshot_id"],
                    hero_name="Rescue Trooper Ramirez",
                )
                grant = connection.execute(
                    """
                    SELECT grant_row.grant_kind
                    FROM catalog_ability_kit_grants grant_row
                    JOIN catalog_ability_kits kit ON kit.id=grant_row.ability_kit_id
                    WHERE kit.kit_name='Kit_Commando_FragGrenade'
                      AND grant_row.target_path LIKE '%G_Commando_FragGrenade%'
                    """
                ).fetchone()
            finally:
                connection.close()

        assert hero is not None
        self.assertEqual("Frag Grenade", hero["abilities"][0]["display_name"])
        self.assertEqual("partial_missing_grants", hero["abilities"][0]["status"])
        self.assertEqual("ability", grant["grant_kind"])
        support = next(perk for perk in hero["perks"] if perk["mode"] == "support")
        self.assertEqual("partial_missing_grants", support["status"])
        self.assertEqual(["/Test/Missing/GE_AssaultTag.1"], support["unresolved_grants"])
        gadget_asset = next(
            asset for asset in queue["assets"] if asset["package_path"] == gadget
        )
        self.assertEqual(0, gadget_asset["priority"])
        self.assertIn("active_ability_implementation", gadget_asset["categories"])

    def test_phase_two_normalizes_structural_semantics_and_opacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            extend_with_phase_two_semantics(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="phase-two-semantics"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                coverage = catalog_coverage(connection, summary["snapshot_id"])
                tags = {
                    (row["tag_name"], row["semantic_role"])
                    for row in connection.execute(
                        """
                        SELECT gt.tag_name, occurrence.semantic_role
                        FROM catalog_gameplay_tags gt
                        JOIN catalog_gameplay_tag_occurrences occurrence
                          ON occurrence.tag_id=gt.id
                        """
                    )
                }
                inheritance = connection.execute(
                    """
                    SELECT relation, resolution_status
                    FROM catalog_inheritance_edges
                    WHERE target_path LIKE '%GET_DamageMultiplier_Ranged_Hero%'
                    """
                ).fetchall()
                mechanics = {
                    row["mechanic_type"]
                    for row in connection.execute(
                        "SELECT mechanic_type FROM catalog_mechanics"
                    )
                }
                opaque = {
                    row["mechanic_kind"]
                    for row in connection.execute(
                        "SELECT mechanic_kind FROM catalog_opaque_mechanics"
                    )
                }
                literal = connection.execute(
                    """
                    SELECT m.literal_value, m.interpretation_status
                    FROM catalog_magnitudes m
                    JOIN asset_objects ao ON ao.id=m.source_object_id
                    WHERE ao.package_path='/Test/Effects/GE_Literal'
                    """
                ).fetchone()
                execution_modifier = connection.execute(
                    """
                    SELECT magnitude.curve_row_name, magnitude.coefficient,
                           magnitude.interpretation_status,
                           mechanic.conditions_json
                    FROM catalog_mechanics mechanic
                    JOIN catalog_magnitudes magnitude
                      ON magnitude.id=mechanic.magnitude_id
                    WHERE mechanic.mechanic_type='execution_modifier'
                      AND mechanic.value_json LIKE '%HealingSource%'
                    """
                ).fetchone()
                set_by_caller_modifier = connection.execute(
                    """
                    SELECT magnitude.calculation_type,
                           magnitude.set_by_caller_tag,
                           magnitude.interpretation_status,
                           mechanic.value_json
                    FROM catalog_mechanics mechanic
                    JOIN catalog_magnitudes magnitude
                      ON magnitude.id=mechanic.magnitude_id
                    WHERE mechanic.mechanic_type='execution_modifier'
                      AND mechanic.value_json LIKE '%OutgoingBaseDamage%'
                    """
                ).fetchone()
                name_only_grant = connection.execute(
                    """
                    SELECT grant_kind, gameplay_effect_id, ability_id
                    FROM catalog_ability_kit_grants grant_row
                    JOIN catalog_ability_kits kit ON kit.id=grant_row.ability_kit_id
                    WHERE kit.kit_name='Kit_StructuralTest'
                    """
                ).fetchone()
            finally:
                connection.close()

        assert hero is not None
        self.assertEqual("resolved", hero["hero"]["class_status"])
        self.assertEqual("resolved", hero["abilities"][0]["status"])
        self.assertGreaterEqual(coverage["counts"]["hero_classes"], 1)
        self.assertGreaterEqual(coverage["counts"]["abilities"], 1)
        self.assertEqual(1, coverage["counts"]["fully_resolved_active_kits"])
        self.assertGreaterEqual(coverage["counts"]["gameplay_tags"], 4)
        self.assertIn(("Weapon.Ranged.Assault", "source_required"), tags)
        self.assertIn(("Enemy.MistMonster", "target_required"), tags)
        self.assertTrue(any(row["resolution_status"] == "resolved" for row in inheritance))
        self.assertTrue(
            {
                "duration",
                "period",
                "application_chance",
                "execution",
                "execution_modifier",
                "cooldown",
                "trigger",
                "stacking",
            }
            <= mechanics
        )
        self.assertIn("custom_magnitude", opaque)
        self.assertIn("execution_calculation", opaque)
        self.assertEqual(0.2, literal["literal_value"])
        self.assertEqual("supported", literal["interpretation_status"])
        self.assertEqual(
            ("Perk.AssaultDamage.T01.DamageMult", 1.0, "partial"),
            tuple(execution_modifier)[:3],
        )
        self.assertIn(
            "Granted.Perk.Blueprint.T02", execution_modifier["conditions_json"]
        )
        self.assertEqual(
            (
                "EGameplayEffectMagnitudeCalculation::SetByCaller",
                "SetByCaller.AbilityDamage",
                "partial",
            ),
            tuple(set_by_caller_modifier)[:3],
        )
        self.assertIn("OutgoingBaseDamage", set_by_caller_modifier["value_json"])
        self.assertEqual("reference", name_only_grant["grant_kind"])
        self.assertIsNone(name_only_grant["gameplay_effect_id"])
        self.assertIsNone(name_only_grant["ability_id"])

    def test_export_queue_is_exact_deduplicated_and_does_not_invent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            for path in source_files:
                if path.name.startswith("Kit_Perk_H_AssaultDamage_"):
                    path.unlink()
                if path.name == "GE_Perk_H_AssaultDamage_DamageBuff_T02.json":
                    path.unlink()
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="queue-test"
                )
                queue = asset_export_queue(connection, summary["snapshot_id"])
            finally:
                connection.close()

        packages = [asset["package_path"] for asset in queue["assets"]]
        self.assertEqual(len(packages), len(set(packages)))
        self.assertIn(
            "/SaveTheWorld/Abilities/Player/Perks/Hero/AssaultDamage/Kit_Perk_H_AssaultDamage_T01",
            packages,
        )
        self.assertIn(
            "/SaveTheWorld/Abilities/Player/Perks/Hero/AssaultDamage/Kit_Perk_H_AssaultDamage_T02",
            packages,
        )
        self.assertIn(
            "/SaveTheWorld/GameplayEffectTemplates/Hero/GET_DamageMultiplier_Ranged_Hero",
            packages,
        )
        self.assertFalse(any("DamageBuff_T02" in package for package in packages))
        self.assertTrue(all(asset["priority"] <= 2 for asset in queue["assets"]))
        self.assertIn("never synthesized", queue["selection_rule"])

    def test_blueprint_granted_perk_preserves_parameters_effects_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            hgd_file = next(
                path for path in source_files if path.name == "HGD_Commando_GrenadeGun.json"
            )
            hgd_payload = json.loads(hgd_file.read_text(encoding="utf-8"))
            hgd_properties = hgd_payload[0]["Properties"]
            support_kit = "/Test/Perks/Blueprint/Kit_Perk_H_Blueprint_T01"
            commander_kit = "/Test/Perks/Blueprint/Kit_Perk_H_Blueprint_T02"
            ability = "/Test/Perks/Blueprint/GA_Perk_H_Blueprint_T01"
            effect = "/Test/Perks/Blueprint/GE_Perk_H_Blueprint_Result"
            hgd_properties["HeroPerk"]["GrantedAbilityKit"]["AssetPathName"] = (
                f"{support_kit}.Kit_Perk_H_Blueprint_T01"
            )
            hgd_properties["CommanderPerk"]["GrantedAbilityKit"]["AssetPathName"] = (
                f"{commander_kit}.Kit_Perk_H_Blueprint_T02"
            )
            hgd_file.write_text(
                json.dumps(hgd_payload, separators=(",", ":")), encoding="utf-8"
            )
            _write_export(
                root,
                "Blueprint/Kit_Perk_H_Blueprint_T01.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit_Perk_H_Blueprint_T01",
                        "Package": support_kit,
                        "Properties": {
                            "GrantedAbilities": [
                                {"GameplayAbility": {"ObjectPath": f"{ability}.1"}}
                            ]
                        },
                    }
                ],
            )
            _write_export(
                root,
                "Blueprint/Kit_Perk_H_Blueprint_T02.json",
                [
                    {
                        "Type": "FortAbilityKit",
                        "Name": "Kit_Perk_H_Blueprint_T02",
                        "Package": commander_kit,
                        "Properties": {},
                    }
                ],
            )
            _write_export(
                root,
                "Blueprint/GA_Perk_H_Blueprint_T01.json",
                [
                    {
                        "Type": "BlueprintGeneratedClass",
                        "Name": "GA_Perk_H_Blueprint_T01_C",
                        "Package": ability,
                        "Properties": {},
                    },
                    {
                        "Type": "GA_Perk_H_Blueprint_T01_C",
                        "Name": "Default__GA_Perk_H_Blueprint_T01_C",
                        "Package": ability,
                        "Properties": {
                            "ChanceToProc": {
                                "Value": 1.0,
                                "Curve": {
                                    "CurveTable": {
                                        "ObjectPath": (
                                            "/Game/Balance/DataTables/"
                                            "CombatEffects_HeroAbilities.0"
                                        )
                                    },
                                    "RowName": "Perk.Blueprint.T01.Chance",
                                },
                            },
                            "GE_Result": {
                                "ObjectName": (
                                    "BlueprintGeneratedClass'"
                                    "GE_Perk_H_Blueprint_Result_C'"
                                ),
                                "ObjectPath": f"{effect}.1",
                            },
                            "AbilityTriggers": [
                                {
                                    "TriggerTag": {"TagName": "Event.Damage.Killed"},
                                    "TriggerSource": (
                                        "EGameplayAbilityTriggerSource::GameplayEvent"
                                    ),
                                }
                            ],
                        },
                    },
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="blueprint-perk-test"
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                queue = asset_export_queue(
                    connection,
                    summary["snapshot_id"],
                    hero_name="Rescue Trooper Ramirez",
                )
            finally:
                connection.close()

        assert hero is not None
        by_mode = {perk["mode"]: perk for perk in hero["perks"]}
        self.assertEqual(
            "structured_blueprint_behavior", by_mode["support"]["status"]
        )
        self.assertEqual(
            "structured_blueprint_behavior", by_mode["commander"]["status"]
        )
        implementation = by_mode["support"]["family_ability_implementations"][0]
        self.assertEqual("T01", implementation["granting_tier"])
        self.assertEqual(
            {"parameter", "referenced_effect", "trigger"},
            {mechanic["type"] for mechanic in implementation["mechanics"]},
        )
        parameter = next(
            mechanic
            for mechanic in implementation["mechanics"]
            if mechanic["type"] == "parameter"
        )
        self.assertEqual(
            "Perk.Blueprint.T01.Chance",
            parameter["magnitude"]["curve_row_name"],
        )
        queued = next(
            asset for asset in queue["assets"] if asset["package_path"] == effect
        )
        self.assertEqual(0, queued["priority"])
        self.assertIn("referenced_gameplay_effect", queued["categories"])

    def test_rescue_trooper_chain_is_normalized_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            before = {path: path.read_bytes() for path in source_files}
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection,
                    root,
                    build_key="++Fortnite+Release-37.00-CL-test",
                    game_version="37.00-test",
                    changelist="test",
                    exporter_version="test-fixture",
                )
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                stored_sources = {
                    row["source_path"]
                    for row in connection.execute("SELECT source_path FROM asset_files")
                }
                after = {path: path.read_bytes() for path in source_files}
            finally:
                connection.close()

        self.assertEqual("ready", summary["status"])
        self.assertFalse(summary["idempotent"])
        self.assertIsNotNone(hero)
        assert hero is not None
        self.assertEqual("Commando", hero["hero"]["class"])
        self.assertEqual("HID_Commando_GrenadeGun_SR_T05", hero["variants"][0]["key"])
        self.assertTrue(hero["variants"][0]["source"]["source_sha256"])
        self.assertEqual(3, len(hero["abilities"]))
        self.assertEqual(["unresolved"] * 3, [item["status"] for item in hero["abilities"]])
        by_mode = {perk["mode"]: perk for perk in hero["perks"]}
        self.assertEqual("T01", by_mode["support"]["tier"])
        self.assertEqual("T02", by_mode["commander"]["tier"])
        self.assertEqual("AssaultDamage", by_mode["support"]["family"])
        self.assertEqual("AssaultDamage", by_mode["commander"]["family"])
        self.assertEqual(17.0, by_mode["support"]["effects"][0]["percent_bonus"])
        self.assertEqual(33.0, by_mode["commander"]["effects"][0]["percent_bonus"])
        self.assertTrue(by_mode["support"]["ability_kit_source"]["source_sha256"])
        self.assertEqual(
            "Perk.AssaultDamage.T02.DamageMult",
            by_mode["commander"]["effects"][0]["curve_row"],
        )
        self.assertTrue(by_mode["support"]["effects"][0]["source"]["effect_sha256"])
        self.assertTrue(by_mode["support"]["effects"][0]["source"]["curve_sha256"])
        self.assertEqual({str(path.resolve()) for path in source_files}, stored_sources)
        self.assertEqual(before, after)

    def test_snapshot_ingestion_is_idempotent_and_preserves_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                first = ingest_asset_directory(
                    connection,
                    root,
                    build_key="build-37-test",
                    game_version="37.00",
                    changelist="123456",
                )
                before = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "game_builds",
                        "asset_snapshots",
                        "asset_files",
                        "asset_objects",
                        "asset_references",
                        "asset_normalization_runs",
                        "catalog_heroes",
                        "catalog_perks",
                        "catalog_effect_modifiers",
                    )
                }
                second = ingest_asset_directory(
                    connection,
                    root,
                    build_key="build-37-test",
                    game_version="37.00",
                    changelist="123456",
                )
                after = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                }
                build = connection.execute("SELECT * FROM game_builds").fetchone()
            finally:
                connection.close()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(before, after)
        self.assertEqual("37.00", build["game_version"])
        self.assertEqual("123456", build["changelist"])
        self.assertEqual("interaction-v4", second["normalization"]["normalizer_version"])

    def test_ingestion_preserves_duplicate_object_names_within_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            duplicate_package = "/Test/Particles/P_DuplicateNames"
            _write_export(
                root,
                "Particles/P_DuplicateNames.json",
                [
                    {
                        "Type": "DistributionFloatConstant",
                        "Name": "RequiredDistributionSpawnRate",
                        "Package": duplicate_package,
                        "Properties": {"Constant": 1.0},
                    },
                    {
                        "Type": "DistributionFloatConstant",
                        "Name": "RequiredDistributionSpawnRate",
                        "Package": duplicate_package,
                        "Properties": {"Constant": 2.0},
                    },
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="duplicate-object-name-test"
                )
                rows = connection.execute(
                    """
                    SELECT object_name, object_key FROM asset_objects
                    WHERE snapshot_id=? AND package_path=? ORDER BY export_index
                    """,
                    (summary["snapshot_id"], duplicate_package),
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(2, len(rows))
        self.assertEqual(rows[0]["object_name"], rows[1]["object_name"])
        self.assertNotEqual(rows[0]["object_key"], rows[1]["object_key"])

    def test_existing_raw_snapshot_is_rederived_for_a_new_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                first = ingest_asset_directory(
                    connection, root, build_key="normalizer-upgrade-test"
                )
                with connection:
                    connection.execute(
                        "DELETE FROM asset_normalization_runs WHERE snapshot_id=?",
                        (first["snapshot_id"],),
                    )
                    connection.execute(
                        "DELETE FROM catalog_gameplay_tags WHERE snapshot_id=?",
                        (first["snapshot_id"],),
                    )
                second = ingest_asset_directory(
                    connection, root, build_key="normalizer-upgrade-test"
                )
                tag_count = connection.execute(
                    "SELECT COUNT(*) FROM catalog_gameplay_tags WHERE snapshot_id=?",
                    (first["snapshot_id"],),
                ).fetchone()[0]
                runs = connection.execute(
                    """
                    SELECT normalizer_version, status FROM asset_normalization_runs
                    WHERE snapshot_id=?
                    """,
                    (first["snapshot_id"],),
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertFalse(second["idempotent"])
        self.assertGreater(tag_count, 0)
        self.assertEqual([("interaction-v4", "ready")], [tuple(row) for row in runs])

    def test_unresolved_references_are_reported_without_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="unresolved-test"
                )
                report = unresolved_reference_report(connection, summary["snapshot_id"])
            finally:
                connection.close()

        targets = {row["target_path"] for row in report["references"]}
        self.assertGreater(report["counts"]["unresolved"], 0)
        self.assertTrue(any("Kit_Commando_FragGrenade" in path for path in targets))
        self.assertTrue(any("GET_DamageMultiplier_Ranged_Hero" in path for path in targets))
        self.assertFalse(any("Kit_Perk_H_AssaultDamage_T01" in path for path in targets))
        self.assertFalse(any("Kit_Perk_H_AssaultDamage_T02" in path for path in targets))

    def test_missing_perk_assets_leave_balance_values_uninterpreted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            for path in source_files:
                if path.name.startswith("Kit_Perk_H_AssaultDamage_"):
                    path.unlink()
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                ingest_asset_directory(connection, root, build_key="incomplete-test")
                hero = hero_provenance(connection, "Rescue Trooper Ramirez")
                raw_rows = {
                    row["row_name"]: row["output_value"]
                    for row in connection.execute(
                        """
                        SELECT cr.row_name, cp.output_value
                        FROM catalog_curve_rows cr
                        JOIN catalog_curve_points cp ON cp.curve_row_id=cr.id
                        """
                    )
                }
            finally:
                connection.close()

        assert hero is not None
        by_mode = {perk["mode"]: perk for perk in hero["perks"]}
        self.assertEqual("unresolved_ability_kit", by_mode["support"]["status"])
        self.assertEqual("unresolved_ability_kit", by_mode["commander"]["status"])
        self.assertEqual([], by_mode["support"]["effects"])
        self.assertEqual([], by_mode["commander"]["effects"])
        self.assertNotIn("unlinked_balance_evidence", by_mode["support"])
        self.assertNotIn("unlinked_balance_evidence", by_mode["commander"])
        self.assertEqual(1.17, raw_rows["Perk.AssaultDamage.T01.DamageMult"])
        self.assertEqual(1.33, raw_rows["Perk.AssaultDamage.T02.DamageMult"])

    def test_roster_collapses_hid_variants_into_one_hgd_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            original = next(path for path in source_files if path.name.startswith("HID_"))
            payload = json.loads(original.read_text(encoding="utf-8"))
            payload[0]["Name"] = "HID_Commando_GrenadeGun_VR_T03"
            payload[0]["Package"] = (
                "/SaveTheWorld/Heroes/Commando/ItemDefinition/"
                "HID_Commando_GrenadeGun_VR_T03"
            )
            payload[0]["Properties"]["DataList"] = [
                {"Rarity": "EFortRarity::Epic"},
                {"Tier": "EFortItemTier::III"},
            ]
            _write_export(root, "Heroes/HID_Commando_GrenadeGun_VR_T03.json", payload)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="canonical-roster-test"
                )
                roster = roster_coverage_report(connection, summary["snapshot_id"])
            finally:
                connection.close()

        self.assertEqual(1, roster["summary"]["unique_hero_gameplay_identities"])
        self.assertEqual(1, roster["summary"]["hero_identities_with_hid_variants"])
        self.assertEqual(0, roster["summary"]["hero_identities_without_hid_variants"])
        self.assertEqual(2, roster["summary"]["raw_hid_objects"])
        self.assertEqual(2, roster["summary"]["mapped_hid_variants"])
        self.assertEqual(0, roster["summary"]["unmapped_hid_variants"])
        self.assertEqual(2, roster["heroes"][0]["variant_count"])
        self.assertEqual(2, roster["summary"]["hero_perk_assignments"])
        self.assertEqual(0, roster["summary"]["heroes_missing_support_or_commander"])
        self.assertEqual(
            {"support", "commander"},
            {perk["perk_mode"] for perk in roster["heroes"][0]["perks"]},
        )

    def test_perk_closure_deduplicates_shared_missing_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="perk-closure-test"
                )
                report = perk_family_semantic_report(
                    connection, "AssaultDamage", summary["snapshot_id"]
                )
            finally:
                connection.close()

        self.assertEqual("partial", report["status"])
        self.assertFalse(report["optimization_ready"])
        self.assertEqual(1, len(report["unresolved_dependencies"]))
        dependency = report["unresolved_dependencies"][0]
        self.assertEqual(
            "/SaveTheWorld/GameplayEffectTemplates/Hero/"
            "GET_DamageMultiplier_Ranged_Hero",
            dependency["package_path"],
        )
        self.assertEqual(2, dependency["reference_count"])

    def test_roster_marks_unsupported_blueprint_only_perk_as_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            source_files = write_golden_slice(root)
            hgd_file = next(
                path for path in source_files if path.name == "HGD_Commando_GrenadeGun.json"
            )
            hgd = json.loads(hgd_file.read_text(encoding="utf-8"))
            properties = hgd[0]["Properties"]
            perk_root = "/Test/Perks/Opaque"
            for property_name, tier in (("HeroPerk", "T01"), ("CommanderPerk", "T02")):
                properties[property_name]["GrantedAbilityKit"] = _reference(
                    f"{perk_root}/Kit_Perk_H_Opaque_{tier}",
                    f"Kit_Perk_H_Opaque_{tier}",
                )
            hgd_file.write_text(json.dumps(hgd, separators=(",", ":")), encoding="utf-8")
            ability_package = f"{perk_root}/GA_Perk_H_Opaque"
            for tier in ("T01", "T02"):
                kit_name = f"Kit_Perk_H_Opaque_{tier}"
                _write_export(
                    root,
                    f"Opaque/{kit_name}.json",
                    [
                        {
                            "Type": "FortAbilityKit",
                            "Name": kit_name,
                            "Package": f"{perk_root}/{kit_name}",
                            "Properties": {
                                "GameplayAbilities": [
                                    {"ObjectPath": f"{ability_package}.0"}
                                ]
                            },
                        }
                    ],
                )
            _write_export(
                root,
                "Opaque/GA_Perk_H_Opaque.json",
                [
                    {
                        "Type": "GA_Perk_H_Opaque_C",
                        "Name": "Default__GA_Perk_H_Opaque_C",
                        "Package": ability_package,
                        "Properties": {},
                    }
                ],
            )
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="opaque-roster-test"
                )
                report = perk_family_semantic_report(
                    connection, "Opaque", summary["snapshot_id"]
                )
            finally:
                connection.close()

        self.assertEqual("opaque", report["status"])
        self.assertFalse(report["optimization_ready"])
        reason_codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("blueprint_behavior_not_executed", reason_codes)
        self.assertIn("no_supported_semantic_facts", reason_codes)

    def test_complete_roster_batch_plan_is_small_relevant_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            extend_with_phase_two_semantics(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="batch-plan-test"
                )
                plan = full_roster_batch_plan(connection, summary["snapshot_id"])
                roster = roster_coverage_report(connection, summary["snapshot_id"])
            finally:
                connection.close()

        self.assertEqual(
            [
                "roster-gameplay-identities",
                "roster-hid-variants",
                "hero-perk-implementations",
                "shared-perk-semantic-bases",
                "hero-perk-balance-table",
            ],
            [batch["batch_id"] for batch in plan["batches"]],
        )
        self.assertEqual([0, 1, 2, 3, 4], [batch["priority"] for batch in plan["batches"]])
        self.assertEqual(
            4, plan["batches"][0]["deduplicated_export_scope_count"]
        )
        self.assertIsNone(plan["batches"][0]["deduplicated_dependency_count"])
        all_paths = json.dumps(plan["batches"])
        self.assertNotIn("UI/Foundation", all_paths)
        self.assertNotIn("Cosmetic", all_paths)
        self.assertEqual("resolved", roster["perk_families"][0]["status"])
        self.assertEqual(1.0, roster["summary"]["optimization_ready_percentage"])
        self.assertFalse(roster["catalog_awareness"]["complete_roster_claimed"])

    def test_roster_receipt_refuses_incomplete_folder_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "exports"
            write_golden_slice(root)
            connection = connect(Path(directory) / "catalog.sqlite3")
            try:
                summary = ingest_asset_directory(
                    connection, root, build_key="roster-receipt-test"
                )
                with self.assertRaisesRegex(ValueError, "missing scopes"):
                    record_roster_export_receipt(
                        connection,
                        summary["snapshot_id"],
                        confirm_complete_recursive_export=True,
                    )
                receipt_count = connection.execute(
                    "SELECT COUNT(*) FROM asset_roster_export_receipts"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(0, receipt_count)


if __name__ == "__main__":
    unittest.main()
