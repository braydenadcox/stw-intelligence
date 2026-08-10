from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_interactions import team_perk_coverage, team_perk_report  # noqa: E402
from stw_pipeline import connect  # noqa: E402


ROOT = "/SaveTheWorld/Abilities/Player/Perks/Leader/TestInteraction"


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


class TeamPerkInteractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_team_perk_slice(exports)
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


if __name__ == "__main__":
    unittest.main()
