from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stw_assets import ingest_asset_directory  # noqa: E402
from stw_golden_qa import (  # noqa: E402
    DEFAULT_BINDINGS,
    CaseBinding,
    ExactPredicate,
    GoldenExecutor,
    GoldenRunConfig,
    GoldenRunner,
    GoldenValidationError,
    ResultMetadata,
    StaticMetadataProvider,
    StaticSelector,
    classify_cases,
    load_benchmark,
    summarize,
    validate_bindings,
)
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "qa" / "golden" / "stw"


def metadata() -> StaticMetadataProvider:
    return StaticMetadataProvider(ResultMetadata(
        game_build="++Fortnite+Release-test", game_version="test", changelist="123",
        asset_snapshot_id=1, asset_manifest_sha256="manifest-sha",
        optimizer_commit="optimizer-commit", optimizer_dirty=True,
        optimizer_source_sha256="optimizer-source-sha",
    ))


class GoldenQaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.benchmark = load_benchmark(QA_ROOT)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_golden_slice(exports)
        self.connection = connect(root / "catalog.sqlite3")
        self.snapshot = ingest_asset_directory(
            self.connection, exports, build_key="golden-qa-test",
            game_version="test", changelist="123", exporter_version="test",
        )["snapshot_id"]

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_both_yaml_files_are_strictly_valid_and_cross_referenced(self) -> None:
        self.assertEqual(26, len(self.benchmark.sources))
        self.assertEqual(85, len(self.benchmark.cases))
        self.assertEqual(85, len({case.id for case in self.benchmark.cases}))
        self.assertTrue(all(set(case.sources) <= set(self.benchmark.sources)
                            for case in self.benchmark.cases))
        validate_bindings(self.benchmark)

    def test_loader_rejects_duplicate_cases_unknown_sources_and_bad_oracles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for source in QA_ROOT.iterdir():
                if source.suffix == ".yaml": shutil.copy2(source, target / source.name)
            cases_path = target / "stw_golden_qa_cases_2026.yaml"
            document = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
            document["cases"][1]["id"] = document["cases"][0]["id"]
            cases_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(GoldenValidationError, "duplicate case id"):
                load_benchmark(target)

            document = yaml.safe_load((QA_ROOT / cases_path.name).read_text(encoding="utf-8"))
            document["cases"][0]["sources"] = ["MISSING"]
            cases_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(GoldenValidationError, "missing sources"):
                load_benchmark(target)

            document = yaml.safe_load((QA_ROOT / cases_path.name).read_text(encoding="utf-8"))
            document["cases"][0]["oracle"] = {"type": "relative_rank", "candidate": "water"}
            cases_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(GoldenValidationError, "oracle is missing"):
                load_benchmark(target)

    def test_executor_and_verification_gate_classification_are_independent(self) -> None:
        classified = classify_cases(self.benchmark)
        self.assertEqual({"executor": "runtime", "verification_gates": ["context", "runtime"],
                          "bound": True}, classified["EVENT-001"])
        self.assertEqual({"executor": "catalog", "verification_gates": ["asset"],
                          "bound": True}, classified["LOAD-003"])
        self.assertEqual("unsupported", classified["ELEM-005"]["executor"])
        counts = {}
        for item in classified.values():
            counts[item["executor"]] = counts.get(item["executor"], 0) + 1
        self.assertEqual({"optimizer": 1, "catalog": 4, "runtime": 3,
                          "policy": 4, "unsupported": 73}, counts)

    def test_binding_type_must_match_declared_oracle_without_prose_interpretation(self) -> None:
        bad = dict(DEFAULT_BINDINGS)
        bad["LOAD-003"] = replace(
            bad["LOAD-003"], predicate=ExactPredicate(("support_bonus_percent",), 17)
        )
        # Exact remains valid even though its expected token/prose differs: bindings,
        # not oracle text interpretation, define execution semantics.
        validate_bindings(self.benchmark, bad)
        bad["LOAD-003"] = replace(
            bad["LOAD-003"], predicate=replace(bad["META-001"].predicate)
        )
        with self.assertRaisesRegex(GoldenValidationError, "does not match"):
            validate_bindings(self.benchmark, bad)

    def test_load_003_uses_current_asset_values_and_preserves_transcript_claims_only(self) -> None:
        runner = GoldenRunner(
            self.benchmark, metadata(), GoldenExecutor(self.connection, self.snapshot)
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            results = runner.run(GoldenRunConfig(frozenset({"asset"})))
        result = next(item for item in results if item.case_id == "LOAD-003")
        self.assertEqual("pass", result.outcome)
        self.assertEqual(33.0, result.observed["commander_bonus_percent"])
        self.assertEqual(17.0, result.observed["support_bonus_percent"])
        self.assertEqual(0.5, result.observed["transcript_claims"]["commander_bonus_claim"])
        self.assertEqual("normalized_game_assets", result.observed["execution_source"])

    def test_incomplete_optimizer_scenarios_remain_explicitly_unsupported(self) -> None:
        classifications = classify_cases(self.benchmark)
        self.assertEqual("unsupported", classifications["SG-001"]["executor"])
        self.assertEqual("unsupported", classifications["SG-002"]["executor"])
        results = GoldenRunner(self.benchmark, metadata()).run()
        by_id = {item.case_id: item for item in results}
        self.assertEqual("unsupported", by_id["SG-001"].outcome)
        self.assertEqual("unsupported", by_id["SG-002"].outcome)
        self.assertIn("typed executable binding", by_id["SG-001"].reason)

    def test_element_bindings_use_current_resistance_facts_and_preserve_claims(self) -> None:
        rules = []
        for defender in ("Fire", "Water", "Nature"):
            rules.extend([
                {"defender_element": defender, "relationship": "Default",
                 "attacker_element": None, "total_damage_resistance": 0.5,
                 "source": {"content_sha256": f"{defender}-default"}},
                {"defender_element": defender, "relationship": "VsEnergyElement",
                 "attacker_element": "Energy", "total_damage_resistance": 0.25,
                 "source": {"content_sha256": f"{defender}-energy"}},
            ])
        with mock.patch("stw_golden_qa.elemental_matchup_report", return_value={
            "rules": rules, "remaining_boundary": "native final damage conversion",
        }):
            executor = GoldenExecutor(self.connection, self.snapshot)
            elem_case = next(case for case in self.benchmark.cases if case.id == "ELEM-004")
            observed = executor.execute(elem_case, DEFAULT_BINDINGS["ELEM-004"])
            passed, _ = DEFAULT_BINDINGS["ELEM-004"].predicate.evaluate(observed)
            self.assertTrue(passed)
            self.assertEqual(0.75, observed["transcript_claims"]["energy_multiplier_claim"])
            self.assertEqual("normalized_game_assets", observed["execution_source"])
            ar_case = next(case for case in self.benchmark.cases if case.id == "AR-006")
            ar_observed = executor.execute(ar_case, DEFAULT_BINDINGS["AR-006"])
            self.assertFalse(ar_observed["physical_universally_best"])

    def test_weapon_element_availability_uses_structural_tag_linkage(self) -> None:
        connection = mock.Mock()
        def query_result(rows):
            result = mock.Mock()
            result.fetchall.return_value = rows
            return result
        connection.execute.side_effect = [
            query_result([{"internal_damage_tag": "Gameplay.Damage.Physical.Energy"}]),
            query_result([{"id": 41}]),
            query_result([{"variant_key": "WID_Chaos", "alteration_key": "AID_Energy",
                           "tag_name": "Gameplay.Damage.Physical.Energy",
                           "content_sha256": "sha"}]),
        ]
        executor = GoldenExecutor(connection, snapshot_id=9)
        case = next(case for case in self.benchmark.cases if case.id == "CHAOS-006")
        observed = executor.execute(case, DEFAULT_BINDINGS["CHAOS-006"])
        self.assertEqual("confirmed", observed["availability_basis"])
        self.assertEqual("WID_Chaos", observed["matching_options"][0]["variant_key"])
        passed, _ = DEFAULT_BINDINGS["CHAOS-006"].predicate.evaluate(observed)
        self.assertTrue(passed)

    def test_normal_mission_binding_proves_event_modifier_does_not_leak(self) -> None:
        requests = []
        def fake_optimizer(connection, request, snapshot_id):
            requests.append(request)
            return {
                "scenario_resolution": {"modifier_evaluations": []},
                "definitive_rankings": [{
                    "raw_supported_components": {"burst_damage": 1.0},
                    "limiting_conditions": [],
                }],
                "uncertainty_aware_recommendations": [],
            }
        executor = GoldenExecutor(self.connection, self.snapshot, optimizer=fake_optimizer)
        case = next(case for case in self.benchmark.cases if case.id == "EVENT-002")
        observed = executor.execute(case, DEFAULT_BINDINGS["EVENT-002"])
        self.assertFalse(observed["event_modifier_leaked"])
        self.assertEqual((), requests[0].mission.modifier_keys)
        passed, _ = DEFAULT_BINDINGS["EVENT-002"].predicate.evaluate(observed)
        self.assertTrue(passed)

    def test_policy_cases_execute_as_structured_predicates(self) -> None:
        results = GoldenRunner(self.benchmark, metadata()).run()
        by_id = {item.case_id: item for item in results}
        self.assertEqual(["pass", "pass", "pass"],
                         [by_id[key].outcome for key in ("META-001", "META-002", "META-003")])
        self.assertEqual("normalized_game_assets", by_id["META-001"].observed["canonical_fact_source"])

    def test_runtime_gate_requires_receipt_and_receipt_is_structurally_evaluated(self) -> None:
        runner = GoldenRunner(self.benchmark, metadata())
        missing = next(item for item in runner.run() if item.case_id == "CHAOS-002")
        self.assertEqual("awaiting_verification", missing.outcome)
        executor = GoldenExecutor(runtime_receipts={
            "CHAOS-002": {"automatic_ammo_return_counts_as_reload": False}
        })
        verified = next(item for item in GoldenRunner(self.benchmark, metadata(), executor).run(
            GoldenRunConfig(frozenset({"runtime"}))) if item.case_id == "CHAOS-002")
        self.assertEqual("pass", verified.outcome)

    def test_contextual_cases_run_only_with_complete_enabled_context(self) -> None:
        runner = GoldenRunner(self.benchmark, metadata())
        skipped = next(item for item in runner.run() if item.case_id == "CONS-005")
        self.assertEqual("skipped", skipped.outcome)
        enabled = next(item for item in runner.run(GoldenRunConfig(
            frozenset({"context"}), frozenset({"constructor:durability-not-binding"})
        )) if item.case_id == "CONS-005")
        self.assertEqual("pass", enabled.outcome)

        executor = GoldenExecutor(runtime_receipts={
            "EVENT-001": {"modifier_present": True, "cooldown_effect_active": True}
        })
        event = next(item for item in GoldenRunner(self.benchmark, metadata(), executor).run(
            GoldenRunConfig(frozenset({"runtime", "context"}),
                            frozenset({"Power Hour:Super Soldier"})))
                     if item.case_id == "EVENT-001")
        self.assertEqual("pass", event.outcome)

    def test_quarantine_historical_and_manual_runtime_are_nonblocking(self) -> None:
        executor = GoldenExecutor(runtime_receipts={"DEF-004": {"revalidated_for_build": False}})
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = next(item for item in GoldenRunner(
                self.benchmark, metadata(), executor
            ).run(GoldenRunConfig(frozenset({"runtime"}))) if item.case_id == "DEF-004")
        self.assertEqual("review", result.outcome)
        self.assertFalse(result.blocking)
        historical = next(case for case in self.benchmark.cases if case.id == "FAR-003")
        self.assertEqual("informational", historical.status)
        self.assertEqual("unsupported", next(item for item in GoldenRunner(
            self.benchmark, metadata()).run() if item.case_id == "FAR-003").outcome)

    def test_failed_active_hard_invariant_is_blocking(self) -> None:
        bindings = dict(DEFAULT_BINDINGS)
        bindings["META-001"] = replace(
            bindings["META-001"], selector=StaticSelector({"canonical_fact_source": "subjective_tier"})
        )
        result = next(item for item in GoldenRunner(
            self.benchmark, metadata(), bindings=bindings
        ).run() if item.case_id == "META-001")
        self.assertEqual("fail", result.outcome)
        self.assertTrue(result.blocking)

    def test_all_85_cases_are_reported_with_injected_metadata(self) -> None:
        def fake_optimizer(connection, request, snapshot_id):
            value = 90.0
            return {"scenario_resolution": {"modifier_evaluations": []},
                    "definitive_rankings": [{
                        "raw_supported_components": {
                            "sustained_damage": value, "burst_damage": value},
                        "limiting_conditions": [],
                    }], "uncertainty_aware_recommendations": []}

        class SmokeExecutor(GoldenExecutor):
            def execute(inner_self, case, binding):
                if case.id in {"ELEM-004", "AR-006"}:
                    return {
                        "factors": ["energy_is_generalist",
                                    "physical_is_penalized_against_elementals",
                                    "verify_current_multipliers"],
                        "physical_universally_best": False,
                    }
                if case.id == "CHAOS-006":
                    return {"availability_basis": "confirmed"}
                return super(SmokeExecutor, inner_self).execute(case, binding)

        executor = SmokeExecutor(self.connection, self.snapshot, optimizer=fake_optimizer)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            results = GoldenRunner(self.benchmark, metadata(), executor).run(
                GoldenRunConfig(frozenset({"asset", "context"}),
                                frozenset({"normal_mission"}), profile="smoke")
            )
        self.assertEqual(85, len(results))
        self.assertEqual({
            "pass": 8, "fail": 0, "review": 0, "skipped": 2,
            "unsupported": 73, "awaiting_verification": 2,
        }, summarize(results))
        self.assertTrue(all(item.metadata.optimizer_commit == "optimizer-commit" for item in results))
        self.assertTrue(all(item.metadata.game_build == "++Fortnite+Release-test" for item in results))
        self.assertTrue(all(item.profile == "smoke" for item in results))


if __name__ == "__main__":
    unittest.main()
