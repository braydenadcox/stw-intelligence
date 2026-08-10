from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from stw_signatures import signature_coverage, signature_report  # noqa: E402
from tests.test_stw_assets import write_weapon_slice  # noqa: E402


ROOT = "/SaveTheWorld/Items/Alteration_v2/GameplayAlterations/TestSignature"


def _write(path: Path, payload: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ref(package: str, name: str) -> dict[str, str]:
    return {"AssetPathName": f"{package}.{name}", "SubPathString": ""}


def write_signature_slice(root: Path) -> None:
    write_weapon_slice(root)
    loadouts = root / "Tables" / "SlotLoadouts.json"
    payload = json.loads(loadouts.read_text(encoding="utf-8"))
    slots = payload[0]["Rows"]["SlotLoadout.Test"]["AlterationSlots"]
    slots[0]["bRespeccable"] = False
    for ordinal in range(1, 5):
        slots.append(
            {
                "UnlockLevel": ordinal + 5,
                "UnlockRarity": "EFortRarity::Rare",
                "SlotDefinitionRow": "Slot.Test.Damage",
                "bRespeccable": True,
                "SlotInitMin": "EFortRarity::Legendary",
                "SlotInitMax": "EFortRarity::Legendary",
            }
        )
    slots.append(
        {
            "UnlockLevel": 30,
            "UnlockRarity": "EFortRarity::Legendary",
            "SlotDefinitionRow": "Slot.Test.Signature",
            "bRespeccable": True,
            "SlotInitMin": "EFortRarity::Legendary",
            "SlotInitMax": "EFortRarity::Legendary",
        }
    )
    loadouts.write_text(json.dumps(payload), encoding="utf-8")

    definitions = root / "Tables" / "SlotDefs.json"
    payload = json.loads(definitions.read_text(encoding="utf-8"))
    payload[0]["Rows"]["Slot.Test.Signature"] = {
        "InitTierGroup": "AGRP.Test.Signature"
    }
    definitions.write_text(json.dumps(payload), encoding="utf-8")

    groups = root / "Tables" / "AlterationGroups.json"
    payload = json.loads(groups.read_text(encoding="utf-8"))
    payload[0]["Rows"]["AGRP.Test.Damage"]["RarityMapping"][0]["Value"]["WeightData"].append(
        {
            "AID": "Alteration:aid_g_testintrinsic",
            "InitialRollWeight": 1,
            "ExclusionNames": [],
        }
    )
    payload[0]["Rows"]["AGRP.Test.Signature"] = {
        "RarityMapping": [
            {
                "Key": "EFortRarity::Legendary",
                "Value": {
                    "WeightData": [
                        {
                            "AID": "Alteration:aid_g_testsignature",
                            "InitialRollWeight": 10000,
                            "ExclusionNames": [],
                        }
                    ]
                },
            }
        ]
    }
    groups.write_text(json.dumps(payload), encoding="utf-8")

    alteration = f"{ROOT}/AID_G_TestSignature"
    ability_set = f"{ROOT}/AS_G_TestSignature"
    ability = f"{ROOT}/GA_G_TestSignature"
    effect = f"{ROOT}/GE_G_TestSignatureMark"
    intrinsic_alteration = f"{ROOT}/AID_G_TestIntrinsic"
    intrinsic_set = f"{ROOT}/AS_G_TestIntrinsic"
    _write(
        root / "Signatures" / "AID_G_TestSignature.json",
        [{
            "Type": "FortAlterationItemDefinition",
            "Name": "AID_G_TestSignature",
            "Package": alteration,
            "Properties": {
                "AlterationAbilitySet": _ref(ability_set, "AS_G_TestSignature"),
                "ItemName": {"LocalizedString": "Test Signature"},
                "ItemDescription": {"LocalizedString": "Hits mark; reload detonates marks."},
                "DataList": [{"Rarity": "EFortRarity::Legendary"}],
            },
        }],
    )
    _write(
        root / "Signatures" / "AS_G_TestSignature.json",
        [{
            "Type": "FortAbilitySet",
            "Name": "AS_G_TestSignature",
            "Package": ability_set,
            "Properties": {
                "GameplayAbilities": [{"ObjectPath": f"{ability}.0"}],
                "GrantedGameplayEffects": [
                    {"GameplayEffect": {"ObjectPath": f"{effect}.0"}, "Level": 1.0}
                ],
            },
        }],
    )
    _write(
        root / "Signatures" / "AID_G_TestIntrinsic.json",
        [{
            "Type": "FortAlterationItemDefinition",
            "Name": "AID_G_TestIntrinsic",
            "Package": intrinsic_alteration,
            "Properties": {
                "AlterationAbilitySet": _ref(intrinsic_set, "AS_G_TestIntrinsic"),
                "ItemName": {"LocalizedString": "Test Intrinsic"},
                "ItemDescription": {"LocalizedString": "Built-in marked damage behavior."},
                "DataList": [{"Rarity": "EFortRarity::Legendary"}],
            },
        }],
    )
    _write(
        root / "Signatures" / "AS_G_TestIntrinsic.json",
        [{
            "Type": "FortAbilitySet",
            "Name": "AS_G_TestIntrinsic",
            "Package": intrinsic_set,
            "Properties": {"GameplayAbilities": [{"ObjectPath": f"{ability}.0"}]},
        }],
    )
    _write(
        root / "Signatures" / "GA_G_TestSignature.json",
        [{
            "Type": "GA_G_TestSignature_C",
            "Name": "Default__GA_G_TestSignature_C",
            "Package": ability,
            "Class": "BlueprintGeneratedClass'GA_G_TestSignature_C'",
            "Properties": {
                "AbilityTriggers": [
                    {"TriggerTag": {"TagName": "Event.Weapon.Hit"}},
                    {"TriggerTag": {"TagName": "Event.Weapon.Reload"}},
                ],
                "ExplosionRadius": 256.0,
                "DamageCoefficient": 0.65,
                "MarkEffect": _ref(effect, "GE_G_TestSignatureMark_C"),
            },
        }],
    )
    _write(
        root / "Signatures" / "GE_G_TestSignatureMark.json",
        [{
            "Type": "GE_G_TestSignatureMark_C",
            "Name": "Default__GE_G_TestSignatureMark_C",
            "Package": effect,
            "Properties": {
                "DurationPolicy": "EGameplayEffectDurationType::Infinite",
                "StackingType": "EGameplayEffectStackingType::AggregateBySource",
                "StackLimitCount": 500,
                "GrantedTags": [{"TagName": "Status.Weapon.Marked"}],
            },
        }],
    )


class SignatureInteractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.exports = root / "exports"
        write_signature_slice(self.exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.first = ingest_asset_directory(
            self.connection,
            self.exports,
            build_key="signature-test",
            exporter_version="test",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_sixth_slot_ownership_and_shared_semantics_are_auditable(self) -> None:
        report = signature_report(self.connection, "Test Signature")

        self.assertEqual("sixth_perk", report["identity"]["signature_kind"])
        self.assertEqual(1, report["ownership"]["weapon_families"])
        self.assertEqual(1, report["ownership"]["eligible_variants"])
        self.assertEqual(1, report["ownership"]["linked_schematics"])
        mechanic_types = {m["mechanic_type"] for m in report["semantics"]["mechanics"]}
        self.assertTrue({"trigger", "parameter", "referenced_effect", "stacking"} <= mechanic_types)
        self.assertEqual(
            ["Event.Weapon.Hit", "Event.Weapon.Reload"],
            report["semantics"]["event_tags"],
        )
        self.assertIn("Status.Weapon.Marked", report["semantics"]["interaction_tags"])
        self.assertIn(
            "blueprint_execution",
            {item["mechanic_kind"] for item in report["semantics"]["opaque_boundaries"]},
        )
        self.assertEqual("partial", report["semantics"]["status"])
        self.assertTrue(report["identity"]["source"]["content_sha256"])
        intrinsic = signature_report(self.connection, "Test Intrinsic")
        self.assertEqual("intrinsic_signature", intrinsic["identity"]["signature_kind"])

    def test_static_non_signature_perk_is_excluded_and_ingestion_is_idempotent(self) -> None:
        second = ingest_asset_directory(
            self.connection,
            self.exports,
            build_key="signature-test",
            exporter_version="test",
        )
        coverage = signature_coverage(self.connection)

        self.assertTrue(second["idempotent"])
        self.assertEqual(self.first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(2, coverage["counts"]["signature_identities"])
        self.assertEqual(1, coverage["counts"]["weapon_families"])
        self.assertEqual(1.0, coverage["ratios"]["weapon_family_coverage"])
        keys = [row[0] for row in self.connection.execute(
            "SELECT signature_key FROM catalog_signature_effects"
        )]
        self.assertEqual(
            ["aid_g_testintrinsic", "aid_g_testsignature"], sorted(keys)
        )


if __name__ == "__main__":
    unittest.main()
