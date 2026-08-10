from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_loadouts import (  # noqa: E402
    assemble_loadout,
    recommend_loadout,
    search_perks,
    semantic_vocabulary,
)
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import extend_with_phase_two_semantics, write_golden_slice  # noqa: E402


class LoadoutReasoningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_golden_slice(exports)
        extend_with_phase_two_semantics(exports)
        self.connection = connect(root / "catalog.sqlite3")
        ingest_asset_directory(
            self.connection,
            exports,
            build_key="loadout-golden",
            game_version="test",
            exporter_version="test",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_exact_attribute_and_tag_search_preserves_numeric_provenance(self) -> None:
        result = search_perks(
            self.connection,
            attributes=["OutgoingAbilityDamage"],
            tags=["Weapon.Ranged.Assault"],
        )
        self.assertEqual(result["counts"]["optimization_ready_matches"], 1)
        match = result["results"][0]
        self.assertEqual(match["perk_family"], "AssaultDamage")
        modifiers = [
            fact for fact in match["matched_evidence"] if fact["fact_type"] == "modifier"
        ]
        values = {
            point["output_value"]
            for fact in modifiers
            for point in fact["magnitude"]["curve_points"]
        }
        self.assertEqual(values, {1.17, 1.33})
        self.assertTrue(
            all(len(fact["source"]["content_sha256"]) == 64 for fact in modifiers)
        )

    def test_compound_search_does_not_join_unrelated_tag_evidence(self) -> None:
        result = search_perks(
            self.connection,
            attributes=["OutgoingAbilityDamage"],
            tags=["Weapon.Ranged.Shotgun"],
        )
        self.assertEqual(result["results"], [])

    def test_partial_semantics_are_excluded_instead_of_recommended(self) -> None:
        snapshot_id, source_object_id = self.connection.execute(
            """
            SELECT kit.snapshot_id, kit.source_object_id
            FROM catalog_ability_kits kit
            JOIN catalog_perks perk ON perk.ability_kit_id=kit.id
            WHERE perk.perk_family='AssaultDamage' LIMIT 1
            """
        ).fetchone()
        self.connection.execute(
            """
            INSERT INTO catalog_opaque_mechanics(
              snapshot_id, source_object_id, property_path, mechanic_kind, reason
            ) VALUES (?, ?, '$.OpaqueTest', 'test', 'not statically interpreted')
            """,
            (snapshot_id, source_object_id),
        )
        self.connection.commit()
        result = search_perks(
            self.connection,
            attributes=["OutgoingAbilityDamage"],
            tags=["Weapon.Ranged.Assault"],
        )
        self.assertEqual(result["results"], [])
        self.assertEqual(len(result["excluded"]), 1)
        self.assertEqual(result["excluded"][0]["semantic_status"], "partial")

    def test_zero_support_loadout_uses_proven_commander_assignment(self) -> None:
        result = recommend_loadout(
            self.connection,
            attributes=["OutgoingAbilityDamage"],
            tags=["Weapon.Ranged.Assault"],
            support_slots=0,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["commander"]["display_name"], "Rescue Trooper Ramirez")
        self.assertEqual(result["commander"]["perk_tier"], "T02")
        commander_values = {
            point["output_value"]
            for fact in result["commander"]["evidence"]
            if fact["fact_type"] == "modifier"
            for point in fact["magnitude"]["curve_points"]
        }
        self.assertEqual(commander_values, {1.33})

    def test_assembly_never_reuses_a_family_or_hero(self) -> None:
        def assignment(hero: str, mode: str) -> dict:
            return {
                "hero_key": hero,
                "display_name": hero,
                "hero_class": "Commando",
                "perk_tier": "T02" if mode == "commander" else "T01",
            }

        search = {
            "snapshot_id": 1,
            "criteria": {},
            "excluded": [],
            "results": [
                {
                    "perk_family": "Alpha",
                    "match_score": 2,
                    "matched_evidence": [],
                    "assignments": {
                        "commander": [assignment("HeroA", "commander")],
                        "support": [assignment("HeroA", "support")],
                    },
                },
                {
                    "perk_family": "Beta",
                    "match_score": 1,
                    "matched_evidence": [],
                    "assignments": {"support": [assignment("HeroB", "support")]},
                },
                {
                    "perk_family": "Gamma",
                    "match_score": 1,
                    "matched_evidence": [],
                    "assignments": {"support": [assignment("HeroC", "support")]},
                },
            ],
        }
        result = assemble_loadout(search, support_slots=2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["commander"]["hero_key"], "HeroA")
        self.assertEqual(
            [support["perk_family"] for support in result["supports"]],
            ["Beta", "Gamma"],
        )

    def test_vocabulary_only_uses_optimization_ready_families(self) -> None:
        vocabulary = semantic_vocabulary(self.connection)
        self.assertIn("OutgoingAbilityDamage", vocabulary["attributes"])
        self.assertIn("Weapon.Ranged.Assault", vocabulary["tags"])


if __name__ == "__main__":
    unittest.main()
