from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stw_ai import BuildIntent, INTENT_SCHEMA_VERSION  # noqa: E402
from stw_ai_app import AiJobManager, SEARCH_STAGES  # noqa: E402
from stw_ai_openai import OpenAIReasoningProvider, OpenAIProviderError  # noqa: E402
from stw_app import ApiApplication  # noqa: E402
from stw_assets import ingest_asset_directory  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from test_stw_assets import write_golden_slice, write_weapon_slice  # noqa: E402
from test_stw_context import write_context_slice  # noqa: E402


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self): return json.dumps(self.value).encode()


def provider_intent(**changes):
    weights = {name: 0.0 for name in (
        "burst_damage", "sustained_damage", "crowd_clear", "mist_monster_boss",
        "survivability", "healing_sustain", "crowd_control", "ability_uptime",
        "weapon_uptime", "condition_reliability",
    )}
    weights["burst_damage"] = 1.0
    value = {
        "schema_version": INTENT_SCHEMA_VERSION, "mode": "recommend",
        "weapon": "Test Rifle", "target_enemy": "HuskGeneric",
        "target_element": None, "mission": None, "power_level": None,
        "four_player": None, "elemental_storm": None,
        "objective_weights": weights, "unavailable_heroes": [],
        "unavailable_weapons": [], "locked_commander": None,
        "avoid_conditions": [], "allow_partial": True, "allow_opaque": True,
        "requested_alternatives": 3,
    }
    value.update(changes)
    return value


def response_with(value, input_tokens=10, output_tokens=5):
    return {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value)}
    ]}], "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


class AiProductTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        exports = root / "exports"
        write_weapon_slice(exports); write_golden_slice(exports); write_context_slice(exports)
        self.asset_db = root / "assets.sqlite3"
        connection = connect(self.asset_db)
        ingest_asset_directory(connection, exports, build_key="ai-product", exporter_version="test")
        connection.close()
        self.app_db = root / "application.sqlite3"
        connect(self.app_db).close()
        self.manager = AiJobManager(self.app_db, self.asset_db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def intent(**changes):
        value = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "weapon": "WID_Test_SR_Ore_T05", "target_enemy": "HuskGeneric",
            "objective_weights": {"burst_damage": 1}, "support_slots": 0,
            "gadget_slots": 0, "beam_width": 8, "requested_alternatives": 2,
        }
        value.update(changes)
        return value

    def test_background_job_exposes_safe_progress_and_persists_conversation(self) -> None:
        submitted = self.manager.submit({"request": "Build a Test Rifle loadout",
                                         "intent": self.intent()})
        job = self.manager.wait(submitted["id"])
        self.assertEqual("completed", job["status"])
        observed = [item["stage"] for item in job["progress"]]
        self.assertEqual(list(SEARCH_STAGES), observed)
        conversation = self.manager.conversation(job["conversation_id"])
        self.assertEqual(["user", "assistant"], [item["role"] for item in conversation["messages"]])
        self.assertIsNotNone(conversation["last_intent"])
        self.assertNotIn("chain", " ".join(observed).casefold())

    def test_followup_inherits_weapon_and_changes_objective(self) -> None:
        first = self.manager.wait(self.manager.submit({
            "request": "Build a Test Rifle loadout", "intent": self.intent()
        })["id"])
        followup = self.manager.wait(self.manager.submit({
            "request": "Make it more survivable",
            "conversation_id": first["conversation_id"],
            "intent_patch": {"support_slots": 0, "gadget_slots": 0, "beam_width": 8},
        })["id"])
        self.assertEqual("completed", followup["status"])
        intent = followup["result"]["intent"]
        self.assertEqual("WID_Test_SR_Ore_T05", intent["weapon"])
        self.assertIn("survivability", intent["objective_weights"])

    def test_inventory_restriction_reaches_optimizer(self) -> None:
        self.manager.set_inventory({"entity_kind": "hero", "entity_key": "ramirez",
                                    "display_name": "Rescue Trooper Ramirez", "owned": True})
        self.manager.set_inventory({"entity_kind": "weapon", "entity_key": "test-rifle",
                                    "display_name": "Test Rifle", "owned": True})
        job = self.manager.wait(self.manager.submit({
            "request": "Use what I own", "restrict_to_owned": True,
            "intent": self.intent(weapon="Test Rifle"),
        })["id"])
        self.assertEqual("completed", job["status"])
        self.assertEqual("Rescue Trooper Ramirez",
                         job["result"]["recommendation"]["commander"]["display_name"])

    def test_friendly_mission_control_target_resolves_structurally(self) -> None:
        job = self.manager.wait(self.manager.submit({
            "request": "Adapt this for Smashers", "intent": self.intent(),
            "intent_patch": {"target_enemy": "Smashers"},
        })["id"])
        self.assertEqual("completed", job["status"])
        self.assertIn("smasher", job["result"]["intent"]["target_enemy"].casefold())

    def test_analysis_comparison_clarification_and_error_states(self) -> None:
        analysis = self.manager.wait(self.manager.submit({
            "request": "Analyze my current loadout",
            "intent": self.intent(mode="analyze", current_loadout={
                "weapon": "WID_Test_SR_Ore_T05", "commander": "Rescue Trooper Ramirez"
            }),
        })["id"])
        self.assertTrue(analysis["result"]["analysis"]["legality"]["legal"])
        loadout = {"weapon": "WID_Test_SR_Ore_T05", "commander": "Rescue Trooper Ramirez"}
        comparison = self.manager.wait(self.manager.submit({
            "request": "Compare these", "intent": self.intent(
                mode="compare", comparison_loadouts=[loadout, loadout]
            ),
        })["id"])
        self.assertIn("comparison", comparison["result"])
        clarification = self.manager.wait(self.manager.submit({
            "request": "Make me something fun",
            "intent": {"schema_version": INTENT_SCHEMA_VERSION,
                       "objective_weights": {"sustained_damage": 1},
                       "dimension_states": {
                           "target_enemy": "required_clarification"
                       },
                       "explicit_dimensions": ["target_enemy"],
                       "support_slots": 0, "gadget_slots": 0},
        })["id"])
        self.assertEqual("needs_clarification", clarification["result"]["status"])
        broken = AiJobManager(self.app_db, Path(self.temporary.name) / "missing.sqlite3")
        failed = broken.wait(broken.submit({"request": "build something"})["id"])
        self.assertEqual("failed", failed["status"])
        self.assertIn("asset catalog not found", failed["error"])

    def test_product_api_exposes_jobs_inventory_catalog_and_history(self) -> None:
        dashboard = Path(self.temporary.name) / "index.html"
        dashboard.write_text("product", encoding="utf-8")
        api = ApiApplication(self.app_db, dashboard, ai_jobs=self.manager)
        status, _, body = api.dispatch("POST", "/api/ai/jobs", json.dumps({
            "request": "Build it", "intent": self.intent()
        }).encode())
        self.assertEqual(202, status)
        job_id = json.loads(body)["id"]
        self.manager.wait(job_id)
        compatibility = api.dispatch("POST", "/api/ai/recommend", json.dumps({
            "request": "Build it", "intent": self.intent()
        }).encode())
        self.assertEqual(202, compatibility[0])
        self.manager.wait(json.loads(compatibility[2])["id"])
        self.assertEqual(200, api.dispatch("GET", f"/api/ai/jobs/{job_id}")[0])
        self.assertEqual(200, api.dispatch("GET", "/api/ai/conversations")[0])
        self.assertEqual(200, api.dispatch("GET", "/api/ai/catalog?kind=hero&query=Rescue")[0])
        saved = api.dispatch("POST", "/api/ai/inventory", json.dumps({
            "entity_kind": "hero", "entity_key": "ramirez",
            "display_name": "Rescue Trooper Ramirez", "owned": True,
        }).encode())
        self.assertEqual(200, saved[0])
        self.assertEqual(1, len(json.loads(api.dispatch("GET", "/api/ai/inventory")[2])["items"]))

    def test_product_shell_contains_complete_ai_workflows_without_secrets(self) -> None:
        page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        for label in ("AI Chat", "Build Analyzer", "Inventory", "Compare Builds",
                      "understanding_request", "Only use my inventory",
                      "Evidence and uncertainty"):
            self.assertIn(label, page)
        self.assertNotIn("sk-", page)
        self.assertNotIn("OPENAI_API_KEY =", page)


class OpenAIProviderTests(unittest.TestCase):
    def test_provider_requires_key_and_https(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                OpenAIReasoningProvider(api_key="")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            OpenAIReasoningProvider(api_key="key", base_url="http://example.com/v1")

    def test_structured_intent_and_usage_instrumentation(self) -> None:
        captured = []
        def opener(request, timeout):
            captured.append((request, timeout))
            return FakeResponse(response_with(provider_intent()))
        provider = OpenAIReasoningProvider(api_key="test-key", model="test-model", opener=opener)
        result = provider.interpret("Build it", [])
        self.assertEqual("Test Rifle", result["weapon"])
        sent = json.loads(captured[0][0].data)
        self.assertEqual("json_schema", sent["text"]["format"]["type"])
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertFalse(sent["store"])
        status = provider.status()
        self.assertEqual(10, status["input_tokens"])
        self.assertNotIn("test-key", json.dumps(status))

    def test_retry_and_hallucinated_evidence_rejection(self) -> None:
        calls = []
        def retrying(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "rate", {}, io.BytesIO())
            return FakeResponse(response_with(provider_intent()))
        provider = OpenAIReasoningProvider(api_key="key", opener=retrying, max_retries=1)
        provider.interpret("Build it", [])
        self.assertEqual(1, provider.status()["retries"])
        invalid = OpenAIReasoningProvider(
            api_key="key", opener=lambda *_args, **_kwargs: FakeResponse(
                response_with({"selected_ids": ["made-up"]})
            )
        )
        intent = BuildIntent.from_dict({"schema_version": INTENT_SCHEMA_VERSION,
                                        "weapon": "Test Rifle", "target_enemy": "HuskGeneric",
                                        "objective_weights": {"burst_damage": 1}})
        with self.assertRaisesRegex(OpenAIProviderError, "not supplied"):
            invalid.select_evidence(intent, [{"id": "e1", "text": "real"}])

    @unittest.skipUnless(os.environ.get("STW_RUN_REAL_PROVIDER_TESTS") == "1",
                         "real provider test is opt-in")
    def test_optional_real_provider_schema(self) -> None:
        provider = OpenAIReasoningProvider()
        result = provider.interpret("Build a Nocturno loadout", [
            {"kind": "weapon", "entity_key": "ranged:nocturno",
             "display_name": "Nocturno", "semantic_status": "partial"}
        ])
        self.assertEqual(INTENT_SCHEMA_VERSION, result["schema_version"])


if __name__ == "__main__":
    unittest.main()
