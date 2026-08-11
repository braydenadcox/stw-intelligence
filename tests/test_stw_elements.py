from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_elements import (  # noqa: E402
    element_report,
    element_status_coverage,
    elemental_matchup_report,
    status_report,
)
from stw_pipeline import connect  # noqa: E402
from tests.test_stw_signatures import _write, write_signature_slice  # noqa: E402


def write_element_status_slice(root: Path) -> None:
    write_signature_slice(root)
    base = "/SaveTheWorld/Items/Alteration_v2/AttributeAlterations/Elemental"
    for friendly, internal in (
        ("Fire", "Gameplay.Damage.Elemental.Fire"),
        ("Water", "Gameplay.Damage.Elemental.Ice"),
        ("Nature", "Gameplay.Damage.Elemental.Lightning"),
        ("Energy", "Gameplay.Damage.Physical.Energy"),
    ):
        package = f"{base}/{friendly}/AID_Ele_{friendly}_T01"
        _write(
            root / "Elements" / f"{friendly}.json",
            [{
                "Type": "FortAlterationItemDefinition",
                "Name": f"AID_Ele_{friendly}_T01",
                "Package": package,
                "Properties": {
                    "ItemName": {"LocalizedString": f"{friendly} Rounds"},
                    "ItemDescription": {"LocalizedString": f"Element: {friendly}"},
                    "GameplayTags": [{"TagName": internal}],
                    "DataList": [{"Rarity": "EFortRarity::Legendary"}],
                },
            }],
        )
    _write(
        root / "Elements" / "Physical.json",
        [{
            "Type": "FortWeaponRangedItemDefinition",
            "Name": "WID_TestPhysical",
            "Package": "/SaveTheWorld/Items/Weapons/Test/WID_TestPhysical",
            "Properties": {"DamageTypeTags": [{"TagName": "Gameplay.Damage.Physical"}]},
        }],
    )
    _write(
        root / "Elements" / "EnemyElement.json",
        [{
            "Type": "FortAbilitySet",
            "Name": "GAS_NPC_Elemental_Fire",
            "Package": "/SaveTheWorld/Abilities/NPC/Elemental/GAS_NPC_Elemental_Fire",
            "Properties": {"GrantedTags": [{"TagName": "Gameplay.Damage.Elemental.Fire"}]},
        }],
    )
    curve_package = "/SaveTheWorld/Balance/DataTables/CombatEffects_NPC"
    curve_rows = {}
    for name, value in (
        ("Default", 0.5), ("VsMatchingElement", -0.17),
        ("VsWeakElement", 0.25), ("VsStrongElement", -0.5),
        ("VsEnergyElement", -0.25),
    ):
        curve_rows[f"Elemental.DamageResist.{name}"] = {
            "InterpMode": "ERichCurveInterpMode::RCIM_Linear",
            "Keys": [{"Time": 0.0, "Value": value}, {"Time": 10000.0, "Value": value}],
        }
    _write(
        root / "Elements" / "CombatEffects_NPC.json",
        [{"Type": "CurveTable", "Name": "CombatEffects_NPC", "Package": curve_package,
          "Rows": curve_rows}],
    )
    def modifier(row_name: str, required_tag: str | None = None) -> dict:
        value = {
            "Attribute": {"AttributeName": "DamageResistance"},
            "ModifierOp": "EGameplayModOp::Additive",
            "ModifierMagnitude": {
                "MagnitudeCalculationType": "EGameplayEffectMagnitudeCalculation::ScalableFloat",
                "ScalableFloatMagnitude": {
                    "Value": 1.0,
                    "Curve": {"CurveTable": {"ObjectPath": f"{curve_package}.0"}, "RowName": row_name},
                },
            },
        }
        if required_tag:
            value["SourceTags"] = {"RequireTags": [required_tag]}
        return value
    _write(
        root / "Elements" / "GE_NPC_GenericFireDamage.json",
        [{
            "Type": "GE_NPC_GenericFireDamage_C",
            "Name": "Default__GE_NPC_GenericFireDamage_C",
            "Package": "/SaveTheWorld/Abilities/NPC/Elemental/GE_NPC_GenericFireDamage",
            "Properties": {
                "Modifiers": [
                    modifier("Elemental.DamageResist.Default"),
                    modifier("Elemental.DamageResist.VsMatchingElement", "Gameplay.Damage.Elemental.Fire"),
                    modifier("Elemental.DamageResist.VsWeakElement", "Gameplay.Damage.Elemental.Lightning"),
                ],
                "InheritableOwnedTagsContainer": {
                    "Added": [{"TagName": "Gameplay.Damage.Elemental.Fire"}]
                },
            },
        }],
    )
    statuses = "/SaveTheWorld/GameplayEffectTemplates/StatusValidation"
    exports = []
    cases = (
        ("Afflicted", "Gameplay.Status.Afflicted"),
        ("Snare", "Gameplay.Status.Snare"),
        ("Frozen", "Gameplay.Status.Frozen"),
        ("Stun", "Gameplay.Status.Stun"),
        ("Vulnerable", "Gameplay.Status.Vulnerable"),
        ("KnockbackImmunity", "Gameplay.Status.KnockbackImmunity"),
    )
    for name, tag in cases:
        exports.append({
            "Type": f"GET_{name}_C",
            "Name": f"Default__GET_{name}_C",
            "Package": statuses,
            "Properties": {
                "DurationPolicy": "EGameplayEffectDurationType::HasDuration",
                "DurationMagnitude": {"Value": 6.0},
                "Period": {"Value": 1.0},
                "StackingType": "EGameplayEffectStackingType::AggregateByTarget",
                "StackLimitCount": 1,
                "GrantedTags": [{"TagName": tag}],
            },
        })
    exports[0]["Properties"]["GrantedTags"].extend([
        {"TagName": "Gameplay.Status.Afflicted.Elemental.Fire"}
    ])
    exports[2]["Properties"]["EffectTags"] = [{"TagName": "Gameplay.Effect.Frozen"}]
    _write(root / "Statuses" / "Statuses.json", exports)


class ElementStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.exports = root / "exports"
        write_element_status_slice(self.exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.first = ingest_asset_directory(
            self.connection, self.exports, build_key="element-status-test", exporter_version="test"
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_player_names_map_to_internal_damage_tags_with_provenance(self) -> None:
        water = element_report(self.connection, "Water")
        self.assertEqual("Gameplay.Damage.Elemental.Ice", water["identity"]["internal_damage_tag"])
        self.assertEqual("localized_alteration_and_tag", water["identity"]["identity_evidence"])
        self.assertTrue(water["tags"][0]["evidence"]["content_sha256"])
        fire = element_report(self.connection, "Fire")
        self.assertIn("enemies", fire["occurrences"]["domains"])
        self.assertEqual("partial", fire["matchup_rule"]["status"])

    def test_matchup_resistance_facts_do_not_invent_final_damage_multiplier(self) -> None:
        report = elemental_matchup_report(self.connection, self.first["snapshot_id"])
        rule = next(
            item for item in report["rules"]
            if item["defender_element"] == "Fire" and item["attacker_element"] == "Nature"
        )
        self.assertEqual("VsWeakElement", rule["relationship"])
        self.assertAlmostEqual(0.25, rule["resistance_adjustment"])
        self.assertAlmostEqual(0.75, rule["total_damage_resistance"])
        self.assertIn("no final damage multiplier is inferred", report["remaining_boundary"])

    def test_status_lifecycle_and_aliases_are_normalized(self) -> None:
        affliction = status_report(self.connection, "Afflicted")
        self.assertEqual("affliction", affliction["identity"]["status_family"])
        self.assertEqual("supported", affliction["identity"]["semantic_status"])
        self.assertEqual(1, affliction["mechanics"]["mechanic_types"]["period"])
        frozen = status_report(self.connection, "Frozen")
        self.assertEqual(
            {"Gameplay.Effect.Frozen", "Gameplay.Status.Frozen"},
            {tag["tag_name"] for tag in frozen["tags"]},
        )

    def test_coverage_and_idempotency(self) -> None:
        coverage = element_status_coverage(self.connection)
        self.assertEqual(5, coverage["counts"]["element_identities"])
        self.assertGreaterEqual(coverage["counts"]["status_identities"], 7)
        self.assertGreaterEqual(coverage["counts"]["enemy_element_identities"], 1)
        self.assertGreater(coverage["counts"]["supported_interaction_facts"], 0)
        self.assertGreaterEqual(coverage["counts"]["partial_interaction_facts"], 0)
        second = ingest_asset_directory(
            self.connection, self.exports, build_key="element-status-test", exporter_version="test"
        )
        self.assertTrue(second["idempotent"])
        self.assertEqual(self.first["snapshot_id"], second["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
