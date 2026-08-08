from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from stw_app import ApiApplication, ProviderRefreshLoop  # noqa: E402
from stw_live import LogWatcher  # noqa: E402
from stw_pipeline import connect  # noqa: E402
from stw_providers import (  # noqa: E402
    FixtureProvider,
    ProviderHealth,
    ingest_provider_rotation,
)
from stw_queries import recent_attempts  # noqa: E402


FIXTURE = ROOT / "fixtures" / "current-mission-rotation.json"
MISSION_UUID = "e699dd8d-25e6-4c0c-8c68-894fce98c657"
THEATER_UUID = "D9A801C5444D1C74D1B7DAB5C7C12C5B"


def registration(second: int = 0, mission: str = MISSION_UUID) -> str:
    return (
        f"[2026.08.08-01.00.{second:02d}:000][1]LogMatchmaking: "
        "[FMatchmakingClient::Register] PartyMemberAccountIds=local PlayerAttributes="
        "{/Fortnite.com/Matchmaking:Region:OCE, "
        "/Fortnite.com/Matchmaking:MatchFill:Public, "
        f"/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:{mission}, "
        f"/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:{THEATER_UUID}, "
        "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}"
    )


SEARCHING = (
    "[2026.08.08-01.00.01:000][2]MatchmakingLog: "
    "Matchmaking Service State Changed From Registering to Registered"
)
ASSIGNED = (
    "[2026.08.08-01.00.02:500][3]LogMatchmaking: "
    "[FMatchmakingClient::OnClientMatchAssigned] MatchId=live-match "
    "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:SYD, "
    "sessionId:22222222222222222222222222222222}"
)
TEAMMATE = (
    "[2026.08.08-01.00.03:000][4]LogParty: Id [MCP:private-live-member] "
    "added to team [HumanCampaign] at index [1]"
)
MAP = (
    "[2026.08.08-01.00.04:000][5]LogLoad: LoadMap: "
    "host/STW_Zones/Maps/Zones/Zone_Arid_WildWest_02"
)
MISSION_READY = (
    "[2026.08.08-01.00.05:000][6]LogHealthSnapshot: "
    "Snapshot: Waiting to Start (FortGameStatePvE, Difficulty 52.00)"
)
LEAVING = (
    "[2026.08.08-01.01.00:000][7]LogOnlineGame: "
    "FortPC::ReturnToMainMenu(), Reason=[]"
)
FRONTEND = (
    "[2026.08.08-01.01.01:000][8]LogLoad: "
    "LoadMap: /Game/Maps/Frontend?closed"
)


def append(path: Path, text: str) -> None:
    with path.open("ab") as output:
        output.write(text.encode("utf-8"))


class LiveWatcherTests(unittest.TestCase):
    def test_provider_refresh_failure_keeps_last_good_rotation(self) -> None:
        class ToggleProvider(FixtureProvider):
            fail = False

            def fetch_rotation(self, now=None, previous_snapshot=None):
                if self.fail:
                    raise RuntimeError("simulated provider outage")
                return super().fetch_rotation(now, previous_snapshot)

            def health(self, now=None):
                if self.fail:
                    return ProviderHealth(
                        "unhealthy", "unknown", None, "simulated provider outage"
                    )
                return super().health(now)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "provider-refresh.sqlite3"
            provider = ToggleProvider(FIXTURE)
            refresh = ProviderRefreshLoop(database, provider, refresh_seconds=5)
            first = refresh.refresh_once()
            next_wait = refresh._next_wait_seconds(
                datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            )
            provider.fail = True
            failed = refresh.refresh_once()
            connection = connect(database)
            try:
                mission_count = connection.execute(
                    "SELECT COUNT(*) FROM external_missions"
                ).fetchone()[0]
            finally:
                connection.close()
            status, _, body = ApiApplication(
                database, provider_status=refresh.status
            ).dispatch("GET", "/api/health")
            health = json.loads(body)

        self.assertEqual("healthy", first["status"])
        self.assertEqual(43230.0, next_wait)
        self.assertEqual("unhealthy", failed["status"])
        self.assertEqual(2, mission_count)
        self.assertEqual(200, status)
        self.assertEqual("unhealthy", health["provider_runtime"]["status"])

    def test_append_buffer_restart_state_persistence_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "FortniteGame.log"
            source.touch()
            database = root / "stw.sqlite3"
            state_dir = root / "live"
            connection = connect(database)
            try:
                ingest_provider_rotation(connection, FixtureProvider(FIXTURE))
            finally:
                connection.close()
            watcher = LogWatcher(
                database, source, state_dir, start_at_end=False, poll_interval=0.01
            )

            raw_registration = registration()
            split = len(raw_registration) // 2
            append(source, raw_registration[:split])
            first = watcher.poll_once()
            self.assertEqual(0, first["lines"])
            connection = connect(database)
            try:
                self.assertEqual(
                    0, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
                checkpoint = connection.execute("SELECT * FROM log_watchers").fetchone()
                self.assertGreater(len(checkpoint["partial_bytes"]), 0)
            finally:
                connection.close()

            append(source, raw_registration[split:] + "\n" + SEARCHING + "\n")
            second = watcher.poll_once()
            self.assertEqual(2, second["lines"])
            connection = connect(database)
            try:
                self.assertEqual("Searching", connection.execute("SELECT state FROM live_states").fetchone()[0])
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
                offset = connection.execute("SELECT byte_offset FROM log_watchers").fetchone()[0]
            finally:
                connection.close()

            restarted = LogWatcher(
                database, source, state_dir, start_at_end=False, poll_interval=0.01
            )
            self.assertEqual(watcher.watcher_id, restarted.watcher_id)
            self.assertEqual(0, restarted.poll_once()["bytes"])
            connection = connect(database)
            try:
                self.assertEqual(offset, connection.execute("SELECT byte_offset FROM log_watchers").fetchone()[0])
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
            finally:
                connection.close()

            append(source, "\n".join((ASSIGNED, TEAMMATE, MAP, MISSION_READY)) + "\n")
            live_result = restarted.poll_once()
            self.assertEqual(4, live_result["lines"])
            self.assertEqual(0, restarted.poll_once()["lines"])
            connection = connect(database)
            try:
                state = connection.execute("SELECT state, attempt_id FROM live_states").fetchone()
                attempt = connection.execute(
                    """
                    SELECT ma.*, r.code AS region, dc.code AS datacenter,
                           a.assignment_latency_seconds
                    FROM mission_attempts ma
                    JOIN regions r ON r.id=ma.requested_region_id
                    JOIN assignments a ON a.attempt_id=ma.id
                    JOIN datacenters dc ON dc.id=a.datacenter_id
                    """
                ).fetchone()
                self.assertEqual("In Mission", state["state"])
                self.assertEqual(attempt["id"], state["attempt_id"])
                self.assertEqual("OCE", attempt["region"])
                self.assertEqual("SYD", attempt["datacenter"])
                self.assertEqual(2.5, attempt["assignment_latency_seconds"])
                self.assertEqual(160, attempt["power_level"])
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM membership_events").fetchone()[0]
                )
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM mission_matches WHERE status='accepted'").fetchone()[0]
                )
                dump = "\n".join(connection.iterdump())
                self.assertNotIn("private-live-member", dump)
            finally:
                connection.close()

            api = ApiApplication(database)
            page_status, page_type, page_body = api.dispatch("GET", "/")
            self.assertEqual(200, page_status)
            self.assertIn("text/html", page_type)
            self.assertIn(b"STW Intelligence", page_body)
            status, content_type, body = api.dispatch("GET", "/api/current")
            current = json.loads(body)
            self.assertEqual(200, status)
            self.assertIn("application/json", content_type)
            self.assertEqual("In Mission", current["state"])
            self.assertEqual("Ride the Lightning", current["attempt"]["objective"])
            self.assertEqual(2, current["attempt"]["current_team_size"])
            for endpoint in (
                "/api/attempts",
                f"/api/attempts/{attempt['id']}",
                "/api/missions/current",
                "/api/correlation/current",
                "/api/health",
            ):
                response_status, _, response_body = api.dispatch("GET", endpoint)
                self.assertEqual(200, response_status, endpoint)
                self.assertIsNotNone(json.loads(response_body))

            append(source, LEAVING + "\n" + FRONTEND + "\n")
            restarted.poll_once()
            connection = connect(database)
            try:
                self.assertEqual("Idle", connection.execute("SELECT state FROM live_states").fetchone()[0])
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
                )
                transitions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT state FROM live_state_events ORDER BY source_line, id"
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(
                [
                    "Registering", "Searching", "Assigned", "In Lobby",
                    "Joining", "In Mission", "Leaving", "Idle",
                ],
                transitions,
            )

            homebase = registration().replace(
                "01.00.00:000", "01.01.02:000"
            ).replace("Type:Mission", "Type:StormShield")
            append(source, homebase + "\n")
            restarted.poll_once()
            connection = connect(database)
            try:
                self.assertEqual(
                    "Idle", connection.execute("SELECT state FROM live_states").fetchone()[0]
                )
                self.assertEqual(
                    2, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
                self.assertEqual(1, len(recent_attempts(connection)))
            finally:
                connection.close()

    def test_truncation_and_rotation_start_new_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "FortniteGame.log"
            source.write_text(registration() + "\n", encoding="utf-8")
            database = root / "stw.sqlite3"
            watcher = LogWatcher(
                database, source, root / "live", start_at_end=False, poll_interval=0.01
            )
            watcher.poll_once()

            rewritten = registration(10, "rewritten-mission") + "\n" + SEARCHING + "\n"
            source.write_text(rewritten, encoding="utf-8")
            watcher.poll_once()
            connection = connect(database)
            try:
                self.assertEqual(1, connection.execute("SELECT generation FROM log_watchers").fetchone()[0])
                self.assertEqual(
                    2, connection.execute("SELECT COUNT(*) FROM live_watch_generations").fetchone()[0]
                )
            finally:
                connection.close()

            source.rename(root / "FortniteGame-backup.log")
            source.write_text(registration(20, "rotated-mission") + "\n", encoding="utf-8")
            watcher.poll_once()
            connection = connect(database)
            try:
                self.assertEqual(2, connection.execute("SELECT generation FROM log_watchers").fetchone()[0])
                self.assertEqual(
                    3, connection.execute("SELECT COUNT(*) FROM live_watch_generations").fetchone()[0]
                )
                self.assertEqual(
                    3, connection.execute("SELECT COUNT(*) FROM capture_files WHERE capture_kind='live'").fetchone()[0]
                )
                self.assertEqual(
                    3, connection.execute("SELECT COUNT(*) FROM mission_attempts").fetchone()[0]
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
