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
from stw_activity import (  # noqa: E402
    activity_overview,
    cohort_catalog,
    recommend_region,
    recommendation_overview,
    refresh_activity,
)
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

    def _seed_second_rotation(self, connection) -> int:
        connection.execute(
            """
            INSERT INTO mission_nodes(
                theater_uuid, mission_uuid, rotation_context,
                rotation_context_evidence, observed_rotation_start,
                observed_rotation_end, first_seen_at, last_seen_at
            ) VALUES ('theater', 'mission-next', '2026-08-09', 'test',
                      '2026-08-09T00:00:00Z', '2026-08-10T00:00:00Z',
                      '2026-08-09T18:00:00Z', '2026-08-09T19:00:00Z')
            """
        )
        next_node = connection.execute(
            "SELECT id FROM mission_nodes WHERE mission_uuid='mission-next'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO capture_files(
                content_sha256, source_path, size_bytes, modified_ns, attempt_count
            ) VALUES ('next-rotation', 'next.log', 2, 2, 6)
            """
        )
        capture_id = connection.execute(
            "SELECT id FROM capture_files WHERE content_sha256='next-rotation'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO mission_attempts(
                capture_id, source_attempt_index, source_line_start, source_line_end,
                mission_node_id, requested_region_id, stw_type, fill_mode, party_size,
                started_at, ended_at, outcome, observation_seconds, build_id,
                power_level, team_size_at_start, team_size_15s, team_size_30s,
                team_size_60s
            )
            SELECT ?, source_attempt_index+100, source_line_start+10000,
                   source_line_end+10000, ?, requested_region_id, stw_type, fill_mode,
                   party_size, replace(started_at, '2026-08-08', '2026-08-09'),
                   replace(ended_at, '2026-08-08', '2026-08-09'), outcome,
                   observation_seconds, build_id, power_level, team_size_at_start,
                   team_size_15s, team_size_30s, team_size_60s
            FROM mission_attempts WHERE build_id='current-build'
            """,
            (capture_id, next_node),
        )
        connection.execute(
            """
            INSERT INTO assignments(
                attempt_id, source_line, assigned_at, assignment_latency_seconds
            )
            SELECT copy.id, original_assignment.source_line+10000,
                   replace(original_assignment.assigned_at, '2026-08-08', '2026-08-09'),
                   original_assignment.assignment_latency_seconds
            FROM mission_attempts copy
            JOIN mission_attempts original
              ON copy.source_attempt_index=original.source_attempt_index+100
            JOIN assignments original_assignment
              ON original_assignment.attempt_id=original.id
            WHERE copy.capture_id=?
            """,
            (capture_id,),
        )
        connection.execute(
            """
            INSERT INTO membership_events(
                attempt_id, source_line, occurred_at, phase, event_type,
                participant_hash, replaced_participant_hash, slot, team_size_after
            )
            SELECT copy.id, event.source_line+10000,
                   replace(event.occurred_at, '2026-08-08', '2026-08-09'),
                   event.phase, event.event_type, event.participant_hash,
                   event.replaced_participant_hash, event.slot, event.team_size_after
            FROM mission_attempts copy
            JOIN mission_attempts original
              ON copy.source_attempt_index=original.source_attempt_index+100
            JOIN membership_events event ON event.attempt_id=original.id
            WHERE copy.capture_id=?
            """,
            (capture_id,),
        )
        connection.execute(
            """
            INSERT INTO providers(code, display_name, adapter_version)
            VALUES ('test', 'Test', '1')
            """
        )
        provider_id = connection.execute("SELECT id FROM providers").fetchone()[0]
        connection.execute(
            """
            INSERT INTO objectives(canonical_code, display_name)
            VALUES ('ride_the_lightning', 'Ride the Lightning')
            """
        )
        objective_id = connection.execute("SELECT id FROM objectives").fetchone()[0]
        node_ids = [
            connection.execute(
                "SELECT id FROM mission_nodes WHERE mission_uuid='mission'"
            ).fetchone()[0],
            next_node,
        ]
        for index, (node_id, day) in enumerate(zip(node_ids, (8, 9)), 1):
            payload_hash = f"snapshot-{index}"
            connection.execute(
                """
                INSERT INTO provider_snapshots(
                    provider_id, fetched_at, payload_sha256, raw_payload, parse_status
                ) VALUES (?, ?, ?, '{}', 'parsed')
                """,
                (provider_id, f"2026-08-{day:02d}T00:00:01Z", payload_hash),
            )
            snapshot_id = connection.execute(
                "SELECT id FROM provider_snapshots WHERE payload_sha256=?",
                (payload_hash,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO mission_rotations(
                    provider_id, provider_rotation_key, valid_from, valid_until,
                    snapshot_id, status
                ) VALUES (?, ?, ?, ?, ?, 'expired')
                """,
                (
                    provider_id, f"rotation-{index}",
                    f"2026-08-{day:02d}T00:00:00Z",
                    f"2026-08-{day + 1:02d}T00:00:00Z", snapshot_id,
                ),
            )
            rotation_id = connection.execute(
                "SELECT id FROM mission_rotations WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO external_missions(
                    rotation_id, provider_mission_key, theater_code, theater_name,
                    objective_id, power_level, is_four_player, source_ordinal
                ) VALUES (?, ?, 'twine_peaks', 'Twine Peaks', ?, 160, 1, 0)
                """,
                (rotation_id, f"mission-{index}", objective_id),
            )
            external_id = connection.execute(
                "SELECT id FROM external_missions WHERE rotation_id=?", (rotation_id,)
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO mission_matches(
                    mission_node_id, rotation_id, external_mission_id, method,
                    confidence, status, evidence_json, matcher_version
                ) VALUES (?, ?, ?, 'inferred', 'high', 'accepted', '{}', 'test')
                """,
                (node_id, rotation_id, external_id),
            )
        connection.commit()
        return next_node

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

        self.assertEqual(6, first["attempts_scored"])
        self.assertEqual(2, first["regions_scored"])
        self.assertEqual(1, first["cohort_excluded"])
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

    def test_refresh_deduplicates_attempts_reimported_from_a_growing_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            try:
                self._seed(connection)
                connection.execute(
                    """
                    INSERT INTO capture_files(
                        content_sha256, source_path, size_bytes, modified_ns, attempt_count
                    ) VALUES ('later-capture', 'controlled-grown.log', 2, 2, 3)
                    """
                )
                capture_id = connection.execute(
                    "SELECT id FROM capture_files WHERE content_sha256='later-capture'"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO mission_attempts(
                        capture_id, source_attempt_index, source_line_start,
                        source_line_end, mission_node_id, requested_region_id,
                        stw_type, fill_mode, party_size, started_at, ended_at,
                        outcome, observation_seconds, build_id, link_code, platform,
                        input_type, objective_hint, objective_evidence,
                        internal_difficulty, power_level, team_size_at_start,
                        team_size_15s, team_size_30s, team_size_60s,
                        first_teammate_seconds, full_team_seconds
                    )
                    SELECT ?, source_attempt_index, source_line_start, source_line_end,
                           mission_node_id, requested_region_id, stw_type, fill_mode,
                           party_size, started_at, ended_at, outcome, observation_seconds,
                           build_id, link_code, platform, input_type, objective_hint,
                           objective_evidence, internal_difficulty, power_level,
                           team_size_at_start, team_size_15s, team_size_30s,
                           team_size_60s, first_teammate_seconds, full_team_seconds
                    FROM mission_attempts
                    WHERE requested_region_id=(SELECT id FROM regions WHERE code='OCE')
                      AND build_id='current-build'
                    """,
                    (capture_id,),
                )
                connection.execute(
                    """
                    INSERT INTO assignments(
                        attempt_id, source_line, assigned_at,
                        assignment_latency_seconds, datacenter_id,
                        lobby_session_id, match_identifier
                    )
                    SELECT copy.id, original_assignment.source_line,
                           original_assignment.assigned_at,
                           original_assignment.assignment_latency_seconds,
                           original_assignment.datacenter_id,
                           original_assignment.lobby_session_id,
                           original_assignment.match_identifier
                    FROM mission_attempts copy
                    JOIN mission_attempts original
                      ON original.started_at=copy.started_at
                     AND original.capture_id<>copy.capture_id
                    JOIN assignments original_assignment
                      ON original_assignment.attempt_id=original.id
                    WHERE copy.capture_id=?
                    """,
                    (capture_id,),
                )
                connection.commit()
                result = refresh_activity(
                    connection, datetime(2026, 8, 8, 20, tzinfo=timezone.utc)
                )
                oce_samples = connection.execute(
                    """
                    SELECT sample_count FROM regional_activity ra
                    JOIN regions r ON r.id=ra.region_id WHERE r.code='OCE'
                    """
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(6, result["attempts_scored"])
        self.assertEqual(3, oce_samples)

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
                "GET",
                "/api/recommendation/current?at=2026-08-08T20:00:00Z&timezone=UTC",
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
        self.assertEqual("time_specific", recommendation["time_context"]["status"])
        self.assertEqual(6, recommendation["time_context"]["samples_in_band"])
        self.assertEqual(400, invalid)

    def test_time_aware_recommendation_uses_band_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            try:
                self._seed(connection)
                refresh_activity(
                    connection, datetime(2026, 8, 8, 20, tzinfo=timezone.utc)
                )
                matching = recommendation_overview(
                    connection,
                    at=datetime(2026, 8, 8, 20, tzinfo=timezone.utc),
                    timezone_name="UTC",
                )
                fallback = recommendation_overview(
                    connection,
                    at=datetime(2026, 8, 8, 21, 30, tzinfo=timezone.utc),
                    timezone_name="UTC",
                )
            finally:
                connection.close()

        self.assertEqual("18:00-20:59", matching["time_context"]["band_label"])
        self.assertEqual("time_specific", matching["time_context"]["status"])
        self.assertEqual("NAE", matching["recommendation"]["region"])
        self.assertEqual("fallback", fallback["time_context"]["status"])
        self.assertEqual(0, fallback["time_context"]["samples_in_band"])
        self.assertEqual("NAE", fallback["recommendation"]["region"])
        self.assertIn("overall recent ranking", fallback["recommendation"]["summary"])

    def test_cross_rotation_cohort_reuses_only_provider_backed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            try:
                self._seed(connection)
                next_node = self._seed_second_rotation(connection)
                result = refresh_activity(
                    connection, datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
                )
                recommendation = recommendation_overview(
                    connection,
                    mission_node_id=next_node,
                    at=datetime(2026, 8, 9, 20, tzinfo=timezone.utc),
                    timezone_name="UTC",
                )
                catalog = cohort_catalog(connection)
                api_status, _, api_body = ApiApplication(database).dispatch(
                    "GET", "/api/cohorts/current"
                )
                connection.execute(
                    """
                    UPDATE external_missions SET is_four_player=0
                    WHERE provider_mission_key='mission-2'
                    """
                )
                connection.commit()
                refresh_activity(
                    connection, datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
                )
                split = recommendation_overview(
                    connection,
                    mission_node_id=next_node,
                    at=datetime(2026, 8, 9, 20, tzinfo=timezone.utc),
                    timezone_name="UTC",
                )
            finally:
                connection.close()

        self.assertEqual(1, result["cohort_cohorts"])
        self.assertEqual(2, result["cohort_included"])
        self.assertEqual("cross_rotation_cohort", recommendation["scope"]["type"])
        self.assertEqual("cross_rotation", recommendation["scope"]["status"])
        self.assertEqual(2, recommendation["scope"]["rotation_count"])
        self.assertEqual(2, recommendation["scope"]["node_count"])
        self.assertEqual(12, recommendation["time_context"]["samples_in_band"])
        self.assertEqual("NAE", recommendation["recommendation"]["region"])
        self.assertEqual(6, recommendation["time_regions"][0]["sample_count"])
        self.assertEqual(1, len(catalog["cohorts"]))
        self.assertEqual(200, api_status)
        self.assertEqual(2, json.loads(api_body)["cohorts"][0]["rotation_count"])
        self.assertEqual("single_rotation", split["scope"]["status"])
        self.assertEqual(1, split["scope"]["node_count"])
        self.assertEqual(6, split["time_context"]["samples_in_band"])

    def test_recommendation_api_rejects_invalid_time_and_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "activity.sqlite3"
            connection = connect(database)
            connection.close()
            api = ApiApplication(database)
            bad_time, _, _ = api.dispatch(
                "GET", "/api/recommendation/current?at=yesterday"
            )
            bad_zone, _, _ = api.dispatch(
                "GET", "/api/recommendation/current?timezone=Not/AZone"
            )

        self.assertEqual(400, bad_time)
        self.assertEqual(400, bad_zone)

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
