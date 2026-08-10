from __future__ import annotations

import sys
import tempfile
import time
import unittest
import json
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_combat import (  # noqa: E402
    CombatScenario,
    LoadoutContext,
    WeaponConfiguration,
    WeaponPerkSelection,
    evaluate_combat,
)
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice, write_weapon_slice  # noqa: E402


class CombatEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_weapon_slice(exports)
        write_golden_slice(exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.summary = ingest_asset_directory(
            self.connection,
            exports,
            build_key="combat-golden",
            game_version="test",
            exporter_version="test",
        )
        self.configuration = WeaponConfiguration(
            "WID_Test_SR_Ore_T05",
            (WeaponPerkSelection(0, "aid_att_damage_t05"),),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _insert_modifier(
        self,
        attribute: str,
        operation: str,
        magnitude: float,
        *,
        source_required: tuple[str, ...] = (),
        target_required: tuple[str, ...] = (),
    ) -> None:
        effect_id = self.connection.execute(
            """
            SELECT id FROM catalog_gameplay_effects
            WHERE package_path LIKE '%/GE_Att_Damage'
            """
        ).fetchone()[0]
        ordinal = self.connection.execute(
            """
            SELECT COALESCE(MAX(modifier_ordinal), -1) + 1
            FROM catalog_effect_modifiers WHERE gameplay_effect_id=?
            """,
            (effect_id,),
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO catalog_effect_modifiers(
                    gameplay_effect_id, modifier_ordinal, attribute_name,
                    modifier_operation, magnitude_kind, literal_value,
                    source_required_tags_json, source_ignored_tags_json,
                    target_required_tags_json, target_ignored_tags_json,
                    interpretation_status
                ) VALUES (?, ?, ?, ?, 'fixture', ?, ?, '[]', ?, '[]', 'supported')
                """,
                (
                    effect_id,
                    ordinal,
                    attribute,
                    operation,
                    magnitude,
                    json.dumps(source_required),
                    json.dumps(target_required),
                ),
            )

    def test_curve_backed_weapon_perk_and_combat_metrics_are_deterministic(self) -> None:
        result = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(range_band="point_blank", window_mode="sustained"),
        )

        self.assertEqual("partial", result.status)
        self.assertAlmostEqual(1.3, result.attributes["OutgoingAbilityDamage"]["value"])
        self.assertAlmostEqual(130.0, result.metrics["configured_damage_per_shot"])
        self.assertAlmostEqual(227.5, result.metrics["damage_profiles"]["body_critical"])
        self.assertAlmostEqual(
            195.0, result.metrics["damage_profiles"]["headshot_noncritical"]
        )
        self.assertAlmostEqual(
            341.25, result.metrics["damage_profiles"]["headshot_critical"]
        )
        self.assertAlmostEqual(1040.0, result.metrics["burst_dps"])
        self.assertAlmostEqual(624.0, result.metrics["sustained_dps"])
        contribution = next(
            row for row in result.contributions if row["attribute"] == "OutgoingAbilityDamage"
        )
        self.assertEqual(12.0, contribution["grant_level"])
        interpolation = self.connection.execute(
            """
            SELECT point.interpolation
            FROM catalog_curve_points point
            JOIN catalog_curve_rows curve_row ON curve_row.id=point.curve_row_id
            WHERE curve_row.row_name='Item.All.Damage.Normal' LIMIT 1
            """
        ).fetchone()[0]
        self.assertEqual("ERichCurveInterpMode::RCIM_Linear", interpolation)
        self.assertIn(
            "live_damage_scaling_not_evaluated", {issue.code for issue in result.issues}
        )

    def test_gas_bias_aggregation_combines_weapon_and_hero_perks(self) -> None:
        support = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(support_heroes=("Rescue Trooper Ramirez",)),
            CombatScenario(window_mode="burst"),
        )
        commander = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(commander="Rescue Trooper Ramirez"),
            CombatScenario(window_mode="burst"),
        )

        self.assertAlmostEqual(1.47, support.attributes["OutgoingAbilityDamage"]["value"])
        self.assertAlmostEqual(147.0, support.metrics["configured_damage_per_shot"])
        self.assertAlmostEqual(1.63, commander.attributes["OutgoingAbilityDamage"]["value"])
        self.assertAlmostEqual(163.0, commander.metrics["configured_damage_per_shot"])
        self.assertEqual(
            "T02", commander.loadout["commander"]["perk_tier"]
        )
        self.assertTrue(
            any(
                row["origin_kind"] == "hero_perk"
                and row["active"]
                and row["conditions"]["source_required"] == ["Weapon.Ranged.Assault"]
                for row in commander.contributions
            )
        )

    def test_condition_tags_gate_modifiers_without_guessing(self) -> None:
        self._insert_modifier(
            "OutgoingAbilityDamage",
            "EGameplayModOp::Multiplicitive",
            1.5,
            target_required=("Gameplay.Status.Afflicted",),
        )
        inactive = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(target_afflicted=False),
        )
        active = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(target_afflicted=True),
        )
        self.assertAlmostEqual(1.3, inactive.attributes["OutgoingAbilityDamage"]["value"])
        self.assertAlmostEqual(1.8, active.attributes["OutgoingAbilityDamage"]["value"])
        gated = [
            row
            for row in inactive.contributions
            if row["conditions"]["target_required"] == ["Gameplay.Status.Afflicted"]
        ]
        self.assertEqual([False], [row["active"] for row in gated])

    def test_fire_rate_magazine_reload_and_explicit_runtime_overrides(self) -> None:
        self._insert_modifier(
            "WeaponRateOfFire", "EGameplayModOp::Multiplicitive", 1.25,
            source_required=("Weapon.Ranged",),
        )
        self._insert_modifier(
            "WeaponAmmoClipSize", "EGameplayModOp::Multiplicitive", 1.5
        )
        self._insert_modifier(
            "WeaponReloadSpeed", "EGameplayModOp::Multiplicitive", 1.5
        )
        unresolved = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(),
        )
        resolved = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(effective_reload_seconds=1.5),
        )
        self.assertAlmostEqual(10.0, resolved.metrics["fire_rate_per_second"])
        self.assertEqual(45, resolved.metrics["effective_magazine_rounds"])
        self.assertIsNone(unresolved.metrics["effective_reload_seconds"])
        self.assertIsNone(unresolved.metrics["sustained_dps"])
        self.assertAlmostEqual(975.0, resolved.metrics["sustained_dps"])

    def test_crit_rating_is_exposed_but_probability_requires_evidence(self) -> None:
        self._insert_modifier("CritRating", "EGameplayModOp::Additive", 30.0)
        self._insert_modifier("DiceCritMultiplier", "EGameplayModOp::Additive", 0.5)
        self._insert_modifier(
            "ZoneCritMultiplier", "EGameplayModOp::Multiplicitive", 1.2
        )
        unknown = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(range_band="mid"),
        )
        explicit = evaluate_combat(
            self.connection,
            self.configuration,
            LoadoutContext(),
            CombatScenario(range_band="mid", crit_probability=0.4, headshot=True),
        )
        self.assertEqual(30.0, unknown.metrics["crit_rating"])
        self.assertIsNone(unknown.metrics["effective_crit_probability"])
        self.assertIn(
            "crit_rating_conversion_unavailable", {issue.code for issue in unknown.issues}
        )
        self.assertAlmostEqual(1.8, explicit.attributes["ZoneCritMultiplier"]["value"])
        self.assertAlmostEqual(1.25, explicit.attributes["DiceCritMultiplier"]["value"])
        self.assertAlmostEqual(263.25, explicit.metrics["configured_damage_per_shot"])

    def test_invalid_perk_slot_is_rejected_instead_of_guessed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not one exact option"):
            evaluate_combat(
                self.connection,
                WeaponConfiguration(
                    "WID_Test_SR_Ore_T05",
                    (WeaponPerkSelection(5, "aid_att_damage_t05"),),
                ),
                LoadoutContext(),
                CombatScenario(),
            )

    def test_result_preserves_asset_and_curve_provenance(self) -> None:
        result = evaluate_combat(
            self.connection, self.configuration, LoadoutContext(), CombatScenario()
        )
        kinds = {row["kind"] for row in result.provenance}
        self.assertTrue(
            {
                "weapon_variant",
                "weapon_schematic",
                "weapon_stat_table",
                "weapon_alteration",
                "gameplay_effect",
                "curve_table",
            }
            <= kinds
        )
        for row in result.provenance:
            if "relative_path" in row:
                self.assertTrue(row["content_sha256"])

    def test_evaluator_is_fast_enough_for_future_search(self) -> None:
        started = time.perf_counter()
        for _ in range(30):
            evaluate_combat(
                self.connection, self.configuration, LoadoutContext(), CombatScenario()
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.assertLess(elapsed_ms / 30.0, 50.0)


if __name__ == "__main__":
    unittest.main()
