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
    hero_provenance,
    ingest_asset_directory,
    unresolved_reference_report,
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
                            "GrantedGameplayEffects": [
                                _reference(effect_package, f"{effect_name}_C")
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
                                }
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
        self.assertEqual("phase2-v1", second["normalization"]["normalizer_version"])

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
        self.assertEqual([("phase2-v1", "ready")], [tuple(row) for row in runs])

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


if __name__ == "__main__":
    unittest.main()
