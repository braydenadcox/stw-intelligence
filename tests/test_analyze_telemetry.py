from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from analyze_telemetry import analyze  # noqa: E402
from sanitize_logs import sanitize_text  # noqa: E402


class AnalyzeTelemetryTests(unittest.TestCase):
    def test_correlates_attempt_and_omits_sensitive_fields(self) -> None:
        lines = [
            "[2026.08.07-05.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] AccountId=secret-account "
            "PartyMemberAccountIds=one,two PlayerAttributes="
            "{/Fortnite.com/Matchmaking:Region:NAW, "
            "/Fortnite.com/Matchmaking:BuildId:123, "
            "/Fortnite.com/Matchmaking:LinkCode:campaign, "
            "/Fortnite.com/Matchmaking:SubRegionPings:{OR:31, NCAL:45}, "
            "/Fortnite.com/Matchmaking:MatchFill:Public, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:mission-uuid, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:theater-uuid, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
            "[2026.08.07-05.00.02:500][2]LogMatchmaking: "
            "[FMatchmakingClient::OnClientMatchAssigned] "
            "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:OR, "
            "sessionId:0123456789abcdef0123456789abcdef}",
            "[2026.08.07-05.00.02:500][2]MatchmakingLog: "
            "Matchmaking Service State Changed From Registered to Assigned",
            "Session [Session Id [0123456789abcdef0123456789abcdef]]",
            "[2026.08.07-05.00.03:000][3]LogParty: Verbose: [private-name] "
            "Id [MCP:private-member-id] team member data updated, "
            "team [HumanCampaign] at index [3]",
            "[2026.08.07-05.00.04:000][4]LogLoad: LoadMap: "
            "1.2.3.4:9000/STW_Zones/Maps/Zones/Test?EncryptionToken=secret-token",
            "[2026.08.07-05.00.05:000][5]LogHealthSnapshot: "
            "======= Snapshot: Waiting to Start (FortGameStatePvE, Difficulty 50.00) =======",
            "[2026.08.07-05.00.33:000][6]LogTest: observation window complete",
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        attempt = result["attempts"][0]
        self.assertEqual("NAW", attempt["region"])
        self.assertEqual(2.5, attempt["assignment_latency_seconds"])
        self.assertEqual(2, attempt["party_size"])
        self.assertEqual("123", attempt["build_id"])
        self.assertEqual("campaign", attempt["link_code"])
        self.assertEqual({"OR": 31, "NCAL": 45}, attempt["subregion_pings_ms"])
        self.assertEqual("OR", attempt["assigned_subregion"])
        self.assertEqual(
            "0123456789abcdef0123456789abcdef", attempt["assigned_session_id"]
        )
        self.assertFalse(attempt["assigned_session_reused_in_file"])
        self.assertEqual(4, attempt["observed_team_size"])
        self.assertEqual(4, attempt["observed_team_size_at_match_start"])
        self.assertEqual(4, attempt["largest_team_size_within_15_seconds"])
        self.assertEqual(4, attempt["largest_team_size_within_30_seconds"])
        self.assertIsNone(attempt["largest_team_size_within_60_seconds"])
        self.assertEqual({"15": 4, "30": 4}, attempt["largest_team_size_within_seconds"])
        self.assertEqual(30.5, attempt["post_assignment_observation_seconds"])
        self.assertEqual(0.5, attempt["time_to_first_teammate_seconds"])
        self.assertEqual(0.5, attempt["time_to_full_team_seconds"])
        self.assertEqual(50.0, attempt["internal_difficulty"])
        self.assertEqual("joined", attempt["outcome"])
        self.assertEqual(["STW_Zones/Maps/Zones/Test"], attempt["maps"])

        serialized = json.dumps(result)
        self.assertNotIn("secret-account", serialized)
        self.assertNotIn("private-name", serialized)
        self.assertNotIn("private-member-id", serialized)
        self.assertNotIn("1.2.3.4", serialized)
        self.assertNotIn("secret-token", serialized)

    def test_assigned_without_world_load_is_not_reported_as_joined(self) -> None:
        lines = [
            "[2026.08.07-05.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] PlayerAttributes={"
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:first}",
            "[2026.08.07-05.00.01:000][2]MatchmakingLog: "
            "Matchmaking Service State Changed From Registered to Assigned",
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        self.assertEqual("assigned_not_joined", result["attempts"][0]["outcome"])

    def test_extracts_qos_session_reuse_and_legacy_search_outcomes(self) -> None:
        lines = [
            "[2026.08.07-05.00.00:000][1]LogQos: Display: AutoRegion NAW: 2 datacenters available",
            "[2026.08.07-05.00.00:100][2]LogQos: Verbose: OR (NAW): 4/4 queries succeeded, average ping: 29ms (adj: 31ms)",
            "[2026.08.07-05.00.00:200][3]LogQos: Best region is 'NAW', recommended subregion is 'OR'",
            "[2026.08.07-05.00.01:000][4]LogMatchmaking: [FMatchmakingClient::Register] PlayerAttributes={}",
            "[2026.08.07-05.00.02:000][5]LogMatchmaking: [FMatchmakingClient::OnClientMatchAssigned] ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:OR, sessionId:0123456789abcdef0123456789abcdef}",
            "[2026.08.07-05.00.03:000][6]LogMatchmaking: [FMatchmakingClient::Register] PlayerAttributes={}",
            "[2026.08.07-05.00.04:000][7]LogMatchmaking: [FMatchmakingClient::OnClientMatchAssigned] ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:OR, sessionId:0123456789abcdef0123456789abcdef}",
            "[2026.08.07-05.00.05:000][8]LogOnlineGame: Matchmaking state change Not Matchmaking -> Finding Existing Session",
            "[2026.08.07-05.00.06:250][9]LogOnlineGame: Matchmaking state change Testing Existing Sessions -> Joining Existing Session",
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        self.assertEqual({"NAW": 2}, result["qos"]["available_datacenters_by_region"])
        self.assertEqual(29, result["qos"]["datacenter_results"][0]["average_ping_ms"])
        self.assertEqual("OR", result["qos"]["recommendations"][0]["recommended_subregion"])
        self.assertFalse(result["attempts"][0]["assigned_session_reused_in_file"])
        self.assertTrue(result["attempts"][1]["assigned_session_reused_in_file"])
        self.assertEqual(
            "existing_session_found", result["legacy_session_searches"][0]["outcome"]
        )
        self.assertEqual(1.25, result["legacy_session_searches"][0]["search_latency_seconds"])

    def test_extracts_cancelled_and_failed_live_states(self) -> None:
        lines = [
            "[2026.08.08-01.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] PlayerAttributes={}",
            "[2026.08.08-01.00.01:000][2]MatchmakingLog: "
            "Matchmaking Service State Changed From Registered to Failed",
            "[2026.08.08-01.01.00:000][3]LogMatchmaking: "
            "[FMatchmakingClient::Register] PlayerAttributes={}",
            "[2026.08.08-01.01.01:000][4]MatchmakingLog: "
            "Matchmaking Service State Changed From Registered to Cancelled",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        self.assertEqual(
            ["Registering", "Failed", "Registering", "Cancelled"],
            [event["state"] for event in result["state_events"]],
        )

    def test_tracks_leave_replacement_across_roster_slot_reordering(self) -> None:
        lines = [
            "[2026.08.08-02.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] PlayerAttributes={"
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
            "[2026.08.08-02.00.01:000][2]LogMatchmaking: "
            "[FMatchmakingClient::OnClientMatchAssigned] "
            "ServerAttributes={/Fortnite.com/Matchmaking:SubRegion:OH, "
            "sessionId:0123456789abcdef0123456789abcdef}",
            "[2026.08.08-02.00.02:000][3]LogLoad: LoadMap: STW_Zones/Test",
            "[2026.08.08-02.00.03:000][4]LogParty: [A] Id [MCP:member-a] "
            "added to team [HumanCampaign] at index [1]",
            "[2026.08.08-02.00.03:100][5]LogParty: [B] Id [MCP:member-b] "
            "added to team [HumanCampaign] at index [2]",
            "[2026.08.08-02.00.03:200][6]LogParty: [C] Id [MCP:member-c] "
            "added to team [HumanCampaign] at index [3]",
            "[2026.08.08-02.05.00:000][7]LogParty: Removing [A] Id "
            "[MCP:member-a] from [local]'s team.",
            "[2026.08.08-02.05.00:100][8]LogParty: [B] Id [MCP:member-b] "
            "team member data updated, team [HumanCampaign] at index [1]",
            "[2026.08.08-02.05.00:200][9]LogParty: [C] Id [MCP:member-c] "
            "team member data updated, team [HumanCampaign] at index [2]",
            "[2026.08.08-02.06.00:000][10]LogParty: [D] Id [MCP:member-d] "
            "added to team [HumanCampaign] at index [3]",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            attempt = analyze(path, privacy_salt=b"test-salt")["attempts"][0]

        events = attempt["membership_events"]
        self.assertEqual(
            ["joined", "joined", "joined", "left", "joined"],
            [event["event_type"] for event in events],
        )
        self.assertEqual([2, 3, 4, 3, 4], [event["team_size_after"] for event in events])

    def test_treats_homebase_matchmaking_as_idle_live_state(self) -> None:
        lines = [
            "[2026.08.08-03.00.00:000][1]LogMatchmaking: "
            "[FMatchmakingClient::Register] PlayerAttributes={"
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:StormShield}",
            "[2026.08.08-03.00.01:000][2]MatchmakingLog: "
            "Matchmaking Service State Changed From Registering to Registered",
            "[2026.08.08-03.00.02:000][3]LogLoad: LoadMap: STW_HestiaBeauty/World/Zone",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        self.assertEqual(["Idle"], [event["state"] for event in result["state_events"]])
        self.assertEqual("non_mission_session", result["state_events"][0]["reason"])


class SanitizeLogsTests(unittest.TestCase):
    def test_removes_credentials_and_preserves_stable_relationships(self) -> None:
        raw = (
            "C:/Users/Example Person/AppData -AUTH_PASSWORD=exchange-code "
            "-caldera=header.payload.signature -epicusername=ExamplePlayer "
            "-epicuserid=abcdef123456 EncryptionToken=abcdef123456:session "
            "at 192.0.2.1 [ExamplePlayer] Id [MCP:abcdef123456]\n"
            "AccountId=abcdef123456 [ExamplePlayer] Id [MCP:abcdef123456]"
        )
        sanitized = sanitize_text(raw)

        for secret in (
            "Example Person",
            "exchange-code",
            "header.payload.signature",
            "ExamplePlayer",
            "abcdef123456",
            "192.0.2.1",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertEqual(4, sanitized.count("<ACCOUNT_"))
        self.assertEqual(3, sanitized.count("<PLAYER_"))
        self.assertIn("-AUTH_PASSWORD=<REDACTED>", sanitized)
        self.assertIn("EncryptionToken=<REDACTED>", sanitized)


if __name__ == "__main__":
    unittest.main()
