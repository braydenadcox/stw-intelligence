from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_pipeline import MIGRATIONS, connect, ingest_logs  # noqa: E402
from stw_providers import (  # noqa: E402
    FixtureProvider,
    HttpMissionProvider,
    ingest_provider_rotation,
    match_rotation,
)


ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "logs" / "manual-telemetry-captures"
FIXTURE = ROOT / "fixtures" / "current-mission-rotation.json"
NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


def write_unmatched_capture(path: Path) -> None:
    lines = [
        "[2026.08.08-01.00.00:000][1]LogMatchmaking: "
        "[FMatchmakingClient::Register] PartyMemberAccountIds=local PlayerAttributes="
        "{/Fortnite.com/Matchmaking:Region:NAE, "
        "/Fortnite.com/Matchmaking:MatchFill:Public, "
        "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:unmatched-resupply-node, "
        "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:D9A801C5444D1C74D1B7DAB5C7C12C5B, "
        "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
        "[2026.08.08-01.00.02:000][2]LogMatchmaking: "
        "[FMatchmakingClient::OnClientMatchAssigned] "
        "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:VA, "
        "sessionId:11111111111111111111111111111111}",
        "[2026.08.08-01.00.05:000][3]LogLoad: LoadMap: "
        "host/STW_Zones/Maps/Zones/Zone_Arid_WildWest_01",
        "[2026.08.08-01.00.06:000][4]LogHealthSnapshot: "
        "Snapshot: Waiting to Start (FortGameStatePvE, Difficulty 50.00)",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


class StwProviderTests(unittest.TestCase):
    def test_http_provider_uses_environment_key_and_conditional_requests(self) -> None:
        payload = FIXTURE.read_bytes()
        requests: list[dict[str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                requests.append(
                    {
                        "api_key": self.headers.get("X-Test-Key"),
                        "if_none_match": self.headers.get("If-None-Match"),
                    }
                )
                if self.headers.get("If-None-Match") == '"rotation-v1"':
                    self.send_response(304)
                    self.send_header("ETag", '"rotation-v1"')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", '"rotation-v1"')
                self.send_header("Last-Modified", "Sat, 08 Aug 2026 00:05:00 GMT")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous_key = os.environ.get("STW_TEST_PROVIDER_KEY")
        os.environ["STW_TEST_PROVIDER_KEY"] = "test-key-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                connection = connect(Path(directory) / "http-provider.sqlite3")
                try:
                    provider = HttpMissionProvider(
                        f"http://127.0.0.1:{server.server_port}/rotation",
                        code="test_live_feed",
                        display_name="Test live feed",
                        api_key_env="STW_TEST_PROVIDER_KEY",
                        api_key_header="X-Test-Key",
                        allow_insecure_http=True,
                    )
                    first = ingest_provider_rotation(connection, provider, NOW)
                    second = ingest_provider_rotation(connection, provider, NOW)
                    counts = {
                        table: connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in (
                            "provider_snapshots", "mission_rotations", "external_missions"
                        )
                    }
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if previous_key is None:
                os.environ.pop("STW_TEST_PROVIDER_KEY", None)
            else:
                os.environ["STW_TEST_PROVIDER_KEY"] = previous_key

        self.assertEqual(1, first["snapshots"])
        self.assertEqual(0, second["snapshots"])
        self.assertEqual(
            {"provider_snapshots": 1, "mission_rotations": 1, "external_missions": 2},
            counts,
        )
        self.assertEqual("test-key-value", requests[0]["api_key"])
        self.assertEqual('"rotation-v1"', requests[1]["if_none_match"])
        self.assertEqual("healthy", provider.health(NOW).status)
        self.assertEqual("current", provider.health(NOW).freshness)

    def test_http_provider_requires_https_and_configured_environment_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            HttpMissionProvider(
                "http://example.test/rotation",
                code="unsafe",
                display_name="Unsafe",
            )
        provider = HttpMissionProvider(
            "https://example.test/rotation",
            code="missing_key",
            display_name="Missing key",
            api_key_env="STW_TEST_MISSING_KEY",
        )
        os.environ.pop("STW_TEST_MISSING_KEY", None)
        with self.assertRaisesRegex(ValueError, "STW_TEST_MISSING_KEY"):
            provider.fetch_rotation(NOW)

    def test_upgrades_an_existing_phase_one_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "phase-one.sqlite3"
            phase_one = sqlite3.connect(database)
            phase_one.executescript(MIGRATIONS[0])
            phase_one.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            phase_one.execute("INSERT INTO schema_migrations VALUES (1, CURRENT_TIMESTAMP)")
            phase_one.execute("PRAGMA user_version = 1")
            phase_one.commit()
            phase_one.close()

            connection = connect(database)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()

        self.assertEqual(21, version)
        self.assertIn("mission_attempts", tables)
        self.assertIn("provider_snapshots", tables)
        self.assertIn("mission_matches", tables)
        self.assertIn("mission_cohorts", tables)
        self.assertIn("mission_cohort_memberships", tables)
        self.assertIn("asset_snapshots", tables)
        self.assertIn("asset_references", tables)
        self.assertIn("catalog_heroes", tables)

    def test_fixture_snapshot_is_idempotent_and_normalizes_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "provider.sqlite3")
            try:
                provider = FixtureProvider(FIXTURE)
                first = ingest_provider_rotation(connection, provider, NOW)
                before = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "providers", "provider_snapshots", "mission_rotations",
                        "external_missions", "external_mission_rewards",
                        "external_mission_modifiers",
                    )
                }
                second = ingest_provider_rotation(connection, provider, NOW)
                after = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                }
                rtl = connection.execute(
                    """
                    SELECT em.*, o.canonical_code AS objective_code
                    FROM external_missions em JOIN objectives o ON o.id=em.objective_id
                    WHERE em.provider_mission_key='fixture-twine-rtl-160'
                    """
                ).fetchone()
                rewards = connection.execute(
                    """
                    SELECT kind, item_code, quantity FROM external_mission_rewards
                    WHERE external_mission_id=? ORDER BY source_ordinal
                    """,
                    (rtl["id"],),
                ).fetchall()
                modifiers = connection.execute(
                    """
                    SELECT modifier_code, element FROM external_mission_modifiers
                    WHERE external_mission_id=? ORDER BY source_ordinal
                    """,
                    (rtl["id"],),
                ).fetchall()
                rotation = connection.execute(
                    "SELECT valid_from, valid_until, status FROM mission_rotations"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(1, first["snapshots"])
        self.assertEqual(1, first["rotations"])
        self.assertEqual(2, first["missions"])
        self.assertEqual(3, first["rewards"])
        self.assertEqual(2, first["modifiers"])
        self.assertTrue(
            all(value == 0 for key, value in second.items() if key != "rotation_id")
        )
        self.assertEqual(before, after)
        self.assertEqual("ride_the_lightning", rtl["objective_code"])
        self.assertEqual(160, rtl["power_level"])
        self.assertEqual(250, rtl["husk_power_level"])
        self.assertEqual("arid_wild_west", rtl["biome_code"])
        self.assertEqual(1, rtl["is_four_player"])
        self.assertEqual(
            [("alert", "legendary_survivor", 1.0), ("repeatable", "storm_shard", 4.0)],
            [tuple(row) for row in rewards],
        )
        self.assertEqual(
            [("water_storm", "water"), ("wall_weakening", None)],
            [tuple(row) for row in modifiers],
        )
        self.assertEqual("current", rotation["status"])
        self.assertLess(rotation["valid_from"], rotation["valid_until"])
        self.assertEqual("healthy", FixtureProvider(FIXTURE).health(NOW).status)

    def test_unique_ambiguous_and_unmatched_correlations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "matches.sqlite3")
            try:
                unmatched_capture = root / "twine-resupply-140.log"
                write_unmatched_capture(unmatched_capture)
                ingest_logs(
                    connection,
                    [
                        CAPTURES / "twine-ride-the-lightning-160-tomorrow.log",
                        unmatched_capture,
                    ],
                )
                unique_rotation = ingest_provider_rotation(
                    connection, FixtureProvider(FIXTURE), NOW
                )["rotation_id"]
                unique = match_rotation(connection, unique_rotation)
                unique_again = match_rotation(connection, unique_rotation)
                unique_rows = connection.execute(
                    """
                    SELECT mm.status, mm.confidence, o.canonical_code
                    FROM mission_matches mm
                    LEFT JOIN external_missions em ON em.id=mm.external_mission_id
                    LEFT JOIN objectives o ON o.id=em.objective_id
                    WHERE mm.rotation_id=? ORDER BY mm.status
                    """,
                    (unique_rotation,),
                ).fetchall()

                duplicate = json.loads(FIXTURE.read_text(encoding="utf-8"))
                duplicate["rotation"]["key"] = "fixture-ambiguous-2026-08-08"
                clone = copy.deepcopy(duplicate["missions"][0])
                clone["provider_mission_key"] = "fixture-twine-rtl-160-second-node"
                duplicate["missions"].append(clone)
                ambiguous_path = root / "ambiguous.json"
                ambiguous_path.write_text(json.dumps(duplicate), encoding="utf-8")
                ambiguous_rotation = ingest_provider_rotation(
                    connection, FixtureProvider(ambiguous_path), NOW
                )["rotation_id"]
                ambiguous = match_rotation(connection, ambiguous_rotation)
                rtl_statuses = connection.execute(
                    """
                    SELECT mm.status, COUNT(*) AS candidates
                    FROM mission_matches mm
                    JOIN mission_nodes mn ON mn.id=mm.mission_node_id
                    WHERE mm.rotation_id=? AND mn.mission_uuid=? GROUP BY mm.status
                    """,
                    (ambiguous_rotation, "e699dd8d-25e6-4c0c-8c68-894fce98c657"),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(1, unique["accepted"])
        self.assertEqual(1, unique["unmatched"])
        self.assertEqual(0, unique_again["changed"])
        self.assertEqual(
            [("accepted", "medium", "ride_the_lightning"), ("unmatched", "none", None)],
            [tuple(row) for row in unique_rows],
        )
        self.assertEqual(1, ambiguous["ambiguous_nodes"])
        self.assertEqual(2, ambiguous["ambiguous_candidates"])
        self.assertEqual(("ambiguous", 2), tuple(rtl_statuses))

    def test_matches_do_not_cross_daily_reset_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "reset-matches.sqlite3")
            try:
                ingest_logs(
                    connection,
                    [
                        CAPTURES / "twine-ride-the-lightning-160-today.log",
                        CAPTURES / "twine-ride-the-lightning-160-tomorrow.log",
                    ],
                )
                previous_day = json.loads(FIXTURE.read_text(encoding="utf-8"))
                previous_day["rotation"] = {
                    "key": "fixture-2026-08-07",
                    "valid_from": "2026-08-07T00:00:00Z",
                    "valid_until": "2026-08-08T00:00:00Z",
                    "source_timestamp": "2026-08-07T00:05:00Z",
                }
                previous_day["missions"] = [copy.deepcopy(previous_day["missions"][0])]
                previous_day["missions"][0]["provider_mission_key"] = "fixture-previous-rtl-160"
                previous_day["missions"][0]["biome"] = {
                    "code": "twine_island",
                    "name": "Twine Peaks island",
                }
                previous_path = root / "previous-day.json"
                previous_path.write_text(json.dumps(previous_day), encoding="utf-8")
                first_rotation = ingest_provider_rotation(
                    connection,
                    FixtureProvider(previous_path),
                    datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
                )["rotation_id"]
                first_match = match_rotation(connection, first_rotation)

                second_rotation = ingest_provider_rotation(
                    connection,
                    FixtureProvider(FIXTURE),
                    NOW,
                )["rotation_id"]
                second_match = match_rotation(connection, second_rotation)
                accepted = connection.execute(
                    """
                    SELECT mm.rotation_id, mm.mission_node_id, mn.mission_uuid,
                           em.provider_mission_key
                    FROM mission_matches mm
                    JOIN mission_nodes mn ON mn.id=mm.mission_node_id
                    JOIN external_missions em ON em.id=mm.external_mission_id
                    WHERE mm.status='accepted' ORDER BY mm.rotation_id
                    """
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(1, first_match["accepted"])
        self.assertEqual(1, second_match["accepted"])
        self.assertEqual(2, len(accepted))
        self.assertNotEqual(accepted[0]["mission_node_id"], accepted[1]["mission_node_id"])
        self.assertNotEqual(accepted[0]["mission_uuid"], accepted[1]["mission_uuid"])
        self.assertEqual("fixture-previous-rtl-160", accepted[0]["provider_mission_key"])
        self.assertEqual("fixture-twine-rtl-160", accepted[1]["provider_mission_key"])


if __name__ == "__main__":
    unittest.main()
