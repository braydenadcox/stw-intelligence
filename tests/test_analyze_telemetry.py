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
            "PlayerAttributes={/Fortnite.com/Matchmaking:Region:NAW, "
            "/Fortnite.com/Matchmaking:MatchFill:Public, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission:mission-uuid, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater:theater-uuid, "
            "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type:Mission}",
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
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            result = analyze(path)

        attempt = result["attempts"][0]
        self.assertEqual("NAW", attempt["region"])
        self.assertEqual(2.5, attempt["assignment_latency_seconds"])
        self.assertEqual(4, attempt["observed_team_size"])
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
