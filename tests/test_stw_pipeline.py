from __future__ import annotations

import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stw_history import get_attempt, list_attempts, theater_name  # noqa: E402
from stw_pipeline import activity_report, connect, ingest_logs, store_metrics  # noqa: E402


CAPTURES = Path(__file__).resolve().parents[1] / "logs" / "manual-telemetry-captures"


class StwPipelineTests(unittest.TestCase):
    def test_replays_migration_after_legacy_index_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "prototype.sqlite3"
            prototype = sqlite3.connect(database)
            prototype.execute(
                """
                CREATE TABLE capture_files(
                    source_file TEXT PRIMARY KEY, size_bytes INTEGER,
                    modified_ns INTEGER, attempt_count INTEGER, ingested_at TEXT
                )
                """
            )
            prototype.execute(
                """
                CREATE TABLE matchmaking_attempts(
                    source_file TEXT, attempt_index INTEGER, mission_id TEXT,
                    theater_id TEXT, internal_difficulty REAL, fill_mode TEXT,
                    party_size INTEGER, region TEXT
                )
                """
            )
            prototype.execute(
                """
                CREATE INDEX attempts_cohort_idx ON matchmaking_attempts(
                    mission_id, theater_id, internal_difficulty,
                    fill_mode, party_size, region
                )
                """
            )
            prototype.execute(
                "INSERT INTO capture_files VALUES ('preserved.log', 10, 20, 1, 'now')"
            )
            prototype.commit()
            prototype.close()

            connection = connect(database)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                index_table = connection.execute(
                    """
                    SELECT tbl_name FROM sqlite_master
                    WHERE type='index' AND name='attempts_cohort_idx'
                    """
                ).fetchone()[0]
                preserved = connection.execute(
                    "SELECT source_file FROM legacy_capture_files_v0"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(7, version)
        self.assertEqual("mission_attempts", index_table)
        self.assertEqual("preserved.log", preserved)

    def test_migrates_a_legacy_database_without_discarding_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(database)
            legacy.execute("CREATE TABLE capture_files(path TEXT PRIMARY KEY, size_bytes INTEGER)")
            legacy.execute("INSERT INTO capture_files VALUES ('old.log', 123)")
            legacy.commit()
            legacy.close()

            connection = connect(database)
            try:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                old_row = connection.execute(
                    "SELECT path, size_bytes FROM legacy_capture_files_v0"
                ).fetchone()
            finally:
                connection.close()

        self.assertIn("mission_attempts", tables)
        self.assertIn("schema_migrations", tables)
        self.assertEqual(("old.log", 123), tuple(old_row))

    def test_ingests_attempt_and_keeps_global_metrics_separate(self) -> None:
        lines = [
            "[2026.08.07-05.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] PartyMemberAccountIds=one PlayerAttributes="
            "{/Fortnite.com/Matchmaking:Region:NAE, "
            "/Fortnite.com/Matchmaking:MatchFill:Public, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:mission, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:theater, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
            "[2026.08.07-05.00.02:000][2]LogMatchmaking: "
            "[FMatchmakingClient::OnClientMatchAssigned] "
            "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:VA, "
            "sessionId:0123456789abcdef0123456789abcdef}",
            "[2026.08.07-05.00.03:000][3]LogParty: "
            "Id [MCP:member] team member data updated, "
            "team [HumanCampaign] at index [1]",
            "[2026.08.07-05.00.04:000][4]LogHealthSnapshot: "
            "Snapshot: Waiting to Start (FortGameStatePvE, Difficulty 52.00)",
            "[2026.08.07-05.01.03:000][5]LogTest: end",
        ]
        metrics = {
            "peakCCU": [{"timestamp": "2026-08-07T05:00:00.000Z", "value": 7000}],
            "uniquePlayers": [
                {"timestamp": "2026-08-07T05:00:00.000Z", "value": 15000}
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.log"
            capture.write_text("\n".join(lines), encoding="utf-8")
            connection = connect(root / "observations.sqlite3")
            try:
                counts = ingest_logs(connection, [capture])
                self.assertEqual(1, counts["files"])
                self.assertEqual(1, counts["mission_nodes"])
                self.assertEqual(1, counts["attempts"])
                self.assertEqual(1, counts["assignments"])
                self.assertEqual(1, counts["lobby_sessions"])
                self.assertEqual(1, counts["membership_events"])
                self.assertTrue(all(value == 0 for value in ingest_logs(connection, [capture]).values()))
                self.assertEqual({"samples": 2}, store_metrics(connection, "hour", metrics))
                report = activity_report(connection, 60, min_regions=1)
                attempt = list_attempts(connection, 1)[0]
                self.assertEqual(attempt["id"], get_attempt(connection, attempt["id"])["id"])
            finally:
                connection.close()

        self.assertEqual(7000, report["global_stw"]["hour"]["metrics"]["peakCCU"])
        cohort = report["regional_activity"]["cohorts"][0]
        region = cohort["regions"][0]
        self.assertEqual("NAE", region["region"])
        self.assertEqual(1.0, region["teammate_seen_rate"])
        self.assertEqual(0.3333, region["observed_team_high_water_index"])
        self.assertIn("not regional population", report["regional_activity"]["definition"])

    def test_membership_events_are_pseudonymous_and_idempotent(self) -> None:
        raw_member = "raw-member-should-never-be-stored"
        replacement = "replacement-should-never-be-stored"
        lines = [
            "[2026.08.07-05.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] AccountId=private-owner "
            "PartyMemberAccountIds=private-owner PlayerAttributes="
            "{/Fortnite.com/Matchmaking:Region:NAE, "
            "/Fortnite.com/Matchmaking:MatchFill:Public, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:mission, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:theater, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
            "[2026.08.07-05.00.02:000][2]LogMatchmaking: "
            "[FMatchmakingClient::OnClientMatchAssigned] "
            "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:VA, "
            "sessionId:0123456789abcdef0123456789abcdef}",
            f"[2026.08.07-05.00.03:000][3]LogParty: Id [MCP:{raw_member}] "
            "added to team [HumanCampaign] at index [1]",
            f"[2026.08.07-05.00.04:000][4]LogLobbyBeacon: ClientPlayerLeft MCP:{raw_member}",
            f"[2026.08.07-05.00.05:000][5]LogParty: Id [MCP:{replacement}] "
            "added to team [HumanCampaign] at index [1]",
            "[2026.08.07-05.00.06:000][6]LogLoad: LoadMap: "
            "host/STW_Zones/Maps/Zones/Zone_Test?EncryptionToken=private-token",
            "[2026.08.07-05.00.07:000][7]LogHealthSnapshot: "
            "Snapshot: Waiting to Start (FortGameStatePvE, Difficulty 52.00)",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "twine-ride-the-lightning-160.log"
            capture.write_text("\n".join(lines), encoding="utf-8")
            database = root / "history.sqlite3"
            connection = connect(database)
            try:
                first = ingest_logs(connection, [capture])
                second = ingest_logs(connection, [capture])
                events = connection.execute(
                    "SELECT event_type, participant_hash FROM membership_events ORDER BY source_line"
                ).fetchall()
                dump = "\n".join(connection.iterdump())
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(3, first["membership_events"])
        self.assertTrue(all(value == 0 for value in second.values()))
        self.assertEqual(["joined", "left", "joined"], [row["event_type"] for row in events])
        self.assertTrue(all(len(row["participant_hash"]) == 64 for row in events))
        self.assertNotIn(raw_member, dump)
        self.assertNotIn(replacement, dump)
        self.assertNotIn("private-owner", dump)
        self.assertNotIn("private-token", dump)
        self.assertEqual(7, version)

    def test_daily_reset_creates_distinct_nodes_sessions_and_maps(self) -> None:
        captures = [
            CAPTURES / "twine-ride-the-lightning-160-today.log",
            CAPTURES / "twine-ride-the-lightning-160-tomorrow.log",
        ]
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "reset.sqlite3")
            try:
                ingest_logs(connection, captures)
                second = ingest_logs(connection, captures)
                rows = connection.execute(
                    """
                    SELECT ma.id, ma.objective_hint, ma.internal_difficulty,
                           mn.theater_uuid, mn.mission_uuid,
                           ls.session_identifier, am.map_path
                    FROM mission_attempts AS ma
                    JOIN mission_nodes AS mn ON mn.id=ma.mission_node_id
                    JOIN assignments AS a ON a.attempt_id=ma.id
                    JOIN lobby_sessions AS ls ON ls.id=a.lobby_session_id
                    JOIN attempt_maps AS am ON am.attempt_id=ma.id
                    WHERE ma.outcome='joined' AND ma.objective_hint='Ride the Lightning'
                      AND am.map_path <> '/Game/Maps/Frontend'
                    ORDER BY ma.started_at
                    """
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(2, len(rows))
        self.assertTrue(all(value == 0 for value in second.values()))
        self.assertEqual(rows[0]["theater_uuid"], rows[1]["theater_uuid"])
        self.assertEqual(52.0, rows[0]["internal_difficulty"])
        self.assertEqual(rows[0]["internal_difficulty"], rows[1]["internal_difficulty"])
        self.assertNotEqual(rows[0]["mission_uuid"], rows[1]["mission_uuid"])
        self.assertNotEqual(rows[0]["session_identifier"], rows[1]["session_identifier"])
        self.assertNotEqual(rows[0]["map_path"], rows[1]["map_path"])
        self.assertEqual("Twine Peaks", theater_name(rows[0]["theater_uuid"]))

    def test_representative_capture_matrix_and_double_import(self) -> None:
        captures = [
            CAPTURES / "twine-repair-shelter-160-same-mission-different-lobby.log",
            CAPTURES / "twine-resupply-140-na-east-to-na-west.log",
            CAPTURES / "twine-140-rescue-survivors-fill-to-no-fill.log",
            CAPTURES / "140-repair-shelter-twine-to-same-mission-new-node-to-100-twine-repair-shelter.log",
            CAPTURES / "160-twine-retrieve-data-east-central-west-europe-oceania.log",
        ]
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "matrix.sqlite3")
            try:
                first = ingest_logs(connection, captures)
                before = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "capture_files", "mission_nodes", "mission_attempts", "assignments",
                        "lobby_sessions", "attempt_maps", "membership_events"
                    )
                }
                second = ingest_logs(connection, captures)
                after = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                }
                regions = {
                    row[0] for row in connection.execute(
                        "SELECT DISTINCT r.code FROM mission_attempts ma JOIN regions r ON r.id=ma.requested_region_id"
                    )
                }
                fills = {row[0] for row in connection.execute("SELECT DISTINCT fill_mode FROM mission_attempts")}
                event_types = {row[0] for row in connection.execute("SELECT DISTINCT event_type FROM membership_events")}
                multi_region = connection.execute(
                    """
                    SELECT COUNT(DISTINCT r.code)
                    FROM mission_attempts ma
                    JOIN mission_nodes mn ON mn.id=ma.mission_node_id
                    JOIN regions r ON r.id=ma.requested_region_id
                    WHERE ma.outcome='joined' AND ma.objective_hint='Retrieve the Data'
                      AND ma.internal_difficulty=52
                    """
                ).fetchone()[0]
                same_node_lobbies = connection.execute(
                    """
                    SELECT COUNT(DISTINCT ls.session_identifier)
                    FROM mission_attempts ma
                    JOIN mission_nodes mn ON mn.id=ma.mission_node_id
                    JOIN assignments a ON a.attempt_id=ma.id
                    JOIN lobby_sessions ls ON ls.id=a.lobby_session_id
                    WHERE ma.outcome='joined' AND ma.objective_hint='Repair the Shelter'
                      AND ma.internal_difficulty=52 AND mn.mission_uuid='5d96f04d-fcdb-4e05-8057-0bfb244e2e15'
                    """
                ).fetchone()[0]
                resupply_regions = connection.execute(
                    """
                    SELECT COUNT(DISTINCT r.code)
                    FROM mission_attempts ma
                    JOIN mission_nodes mn ON mn.id=ma.mission_node_id
                    JOIN regions r ON r.id=ma.requested_region_id
                    WHERE ma.outcome='joined' AND ma.objective_hint='Resupply'
                      AND mn.mission_uuid='2c4dd5b2-85ea-48dc-9f47-58fccfa8ae8c'
                    """
                ).fetchone()[0]
                repair_nodes = connection.execute(
                    """
                    SELECT COUNT(DISTINCT mission_node_id)
                    FROM mission_attempts
                    WHERE outcome='joined' AND objective_hint='Repair the Shelter'
                    """
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertGreater(first["attempts"], 0)
        self.assertTrue(all(value == 0 for value in second.values()))
        self.assertEqual(before, after)
        self.assertTrue({"NAE", "NAC", "NAW", "EU", "OCE"}.issubset(regions))
        self.assertTrue({"Public", "Private"}.issubset(fills))
        self.assertTrue({"left", "joined"}.issubset(event_types))
        self.assertEqual(5, multi_region)
        self.assertEqual(2, same_node_lobbies)
        self.assertEqual(2, resupply_regions)
        self.assertGreaterEqual(repair_nodes, 3)


if __name__ == "__main__":
    unittest.main()
