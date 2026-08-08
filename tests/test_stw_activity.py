from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from stw_activity import activity_overview, recommend_region, refresh_activity  # noqa: E402
from stw_app import ApiApplication  # noqa: E402
from stw_pipeline import connect  # noqa: E402


class ActivityScoreTests(unittest.TestCase):
    def _seed(self, connection) -> None:
        connection.execute(
            """
            INSERT INTO capture_files(
                content_sha256, source_path, size_bytes, modified_ns, attempt_count
            ) VALUES ('capture', 'controlled.log', 1, 1, 7)
            """
        )
        capture_id = connection.execute("SELECT id FROM capture_files").fetchone()[0]
        connection.execute(
            """
            INSERT INTO mission_nodes(
                theater_uuid, mission_uuid, rotation_context,
                rotation_context_evidence, observed_rotation_start,
                observed_rotation_end, first_seen_at, last_seen_at
            ) VALUES ('theater', 'mission', '2026-08-08', 'test',
                      '2026-08-08T00:00:00Z', '2026-08-09T00:00:00Z',
                      '2026-08-08T17:00:00Z', '2026-08-08T19:00:00Z')
            """
        )
        node_id = connection.execute("SELECT id FROM mission_nodes").fetchone()[0]
        for region in ("NAE", "OCE"):
            connection.execute("INSERT INTO regions(code) VALUES (?)", (region,))

        nae_id = connection.execute(
            "SELECT id FROM regions WHERE code='NAE'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO mission_attempts(
                capture_id, source_attempt_index, source_line_start, mission_node_id,
                requested_region_id, stw_type, fill_mode, party_size, started_at,
                ended_at, outcome, observation_seconds, build_id, power_level,
                team_size_at_start, team_size_15s, team_size_30s, team_size_60s
            ) VALUES (?, 0, 1, ?, ?, 'Mission', 'Public', 1,
                      '2026-08-08T17:00:00Z', '2026-08-08T17:01:01Z', 'joined',
                      61, 'old-build', 160, 1, 1, 1, 1)
            """,
            (capture_id, node_id, nae_id),
        )

        index = 0
        for region, populated in (("NAE", True), ("OCE", False)):
            region_id = connection.execute(
                "SELECT id FROM regions WHERE code=?", (region,)
            ).fetchone()[0]
            for sample in range(3):
                index += 1
                minute = index
                started = f"2026-08-08T18:{minute:02d}:00Z"
                assigned = started
                ended = f"2026-08-08T18:{minute + 1:02d}:01Z"
                connection.execute(
                    """
                    INSERT INTO mission_attempts(
                        capture_id, source_attempt_index, source_line_start,
                        mission_node_id, requested_region_id, stw_type, fill_mode,
                        party_size, started_at, ended_at, outcome, observation_seconds,
                        build_id, power_level, team_size_at_start, team_size_15s,
                        team_size_30s, team_size_60s
                    ) VALUES (?, ?, ?, ?, ?, 'Mission', 'Public', 1, ?, ?, 'joined',
                              61, 'current-build', 160, ?, ?, ?, ?)
                    """,
                    (
                        capture_id,
                        index,
                        index * 10,
                        node_id,
                        region_id,
                        started,
                        ended,
                        4 if populated else 1,
                        4 if populated else 1,
                        4 if populated else 1,
                        4 if populated else 1,
                    ),
                )
                attempt_id = connection.execute(
                    "SELECT id FROM mission_attempts WHERE source_attempt_index=?",
                    (index,),
                ).fetchone()[0]
                latency = 0.0 if populated else 30.0
                connection.execute(
                    """
                    INSERT INTO assignments(
                        attempt_id, source_line, assigned_at, assignment_latency_seconds
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (attempt_id, index * 10 + 1, assigned, latency),
                )
                if populated:
                    for teammate in range(3):
                        connection.execute(
                            """
                            INSERT INTO membership_events(
                                attempt_id, source_line, occurred_at, phase, event_type,
                                participant_hash, slot, team_size_after
                            ) VALUES (?, ?, ?, 'lobby', 'joined', ?, ?, ?)
                            """,
                            (
                                attempt_id,
                                index * 10 + 2 + teammate,
                                assigned,
                                f"attempt-{index}-teammate-{teammate}",
                                teammate + 1,
                                teammate + 2,
                            ),
                        )
        connection.commit()

    def test_scores_components_aggregates_and_refreshes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            try:
                self._seed(connection)
                now = datetime(2026, 8, 8, 20, tzinfo=timezone.utc)
                first = refresh_activity(connection, now)
                second = refresh_activity(connection, now)
                overview = activity_overview(connection)
                counts = (
                    connection.execute(
                        "SELECT COUNT(*) FROM attempt_activity_scores"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM regional_activity"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

        self.assertEqual({"attempts_scored": 6, "regions_scored": 2}, first)
        self.assertEqual(first, second)
        self.assertEqual((6, 2), counts)
        self.assertEqual("NAE", overview["leader"]["region"])
        self.assertEqual("recommended", overview["recommendation"]["status"])
        self.assertEqual("NAE", overview["recommendation"]["region"])
        self.assertFalse(
            overview["recommendation"]["basis"]["network_ping_used"]
        )
        self.assertEqual(100.0, overview["leader"]["score"])
        self.assertEqual("low", overview["leader"]["confidence"])
        self.assertEqual(3, overview["leader"]["sample_count"])
        oce = next(row for row in overview["regions"] if row["region"] == "OCE")
        self.assertAlmostEqual(10.0 * math.exp(-1), oce["score"], places=2)
        self.assertEqual(1.0, oce["coverage"])
        self.assertEqual(1.0, oce["coverage_15"])
        self.assertEqual(1.0, oce["coverage_30"])
        self.assertEqual(1.0, oce["coverage_60"])
        self.assertIn("not regional population", overview["definition"])

    def test_activity_api_and_invalid_mission_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            try:
                self._seed(connection)
                refresh_activity(
                    connection, datetime(2026, 8, 8, 20, tzinfo=timezone.utc)
                )
            finally:
                connection.close()

            api = ApiApplication(database)
            status, _, body = api.dispatch("GET", "/api/activity/current")
            recommendation_status, _, recommendation_body = api.dispatch(
                "GET", "/api/recommendation/current"
            )
            invalid, _, _ = api.dispatch(
                "GET", "/api/activity/current?mission_node=not-a-number"
            )
            payload = json.loads(body)
            recommendation = json.loads(recommendation_body)

        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["regions"]))
        self.assertEqual(200, recommendation_status)
        self.assertEqual(
            "NAE", recommendation["recommendation"]["region"]
        )
        self.assertEqual(400, invalid)

    def test_recommendation_abstains_for_insufficient_evidence_and_ties(self) -> None:
        def region(code: str, score: float, confidence: str, samples: int) -> dict:
            return {
                "region": code,
                "score": score,
                "confidence": confidence,
                "sample_count": samples,
                "effective_sample_size": float(samples),
                "components": {
                    "arrival": 20.0,
                    "concurrency": 10.0,
                    "breadth": 5.0,
                    "retention": 10.0,
                    "assignment": 5.0,
                },
            }

        insufficient = recommend_region(
            [region("NAE", 80.0, "insufficient", 2)]
        )
        tied = recommend_region(
            [
                region("NAE", 70.0, "low", 3),
                region("EU", 70.0, "low", 3),
            ]
        )

        self.assertEqual("insufficient_data", insufficient["status"])
        self.assertIsNone(insufficient["region"])
        self.assertEqual("no_clear_leader", tied["status"])
        self.assertIsNone(tied["region"])
        self.assertIn("row order", tied["reasons"][1])

    def test_empty_database_returns_an_insufficient_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "empty.sqlite3"
            connection = connect(database)
            connection.close()
            status, _, body = ApiApplication(database).dispatch(
                "GET", "/api/recommendation/current"
            )
            payload = json.loads(body)

        self.assertEqual(200, status)
        self.assertEqual("insufficient_data", payload["recommendation"]["status"])


if __name__ == "__main__":
    unittest.main()
