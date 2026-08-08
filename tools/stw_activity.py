#!/usr/bin/env python3
"""Compute versioned observed matchmaking activity from local mission attempts."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


SCORE_VERSION = "observed-activity-v1"
WINDOW_SECONDS = 60
RECENCY_HALF_LIFE_HOURS = 6.0


def _parse_time(value: str) -> datetime:
    if "." in value[:10] and "-" in value:
        return datetime.strptime(value, "%Y.%m.%d-%H.%M.%S:%f").replace(
            tzinfo=timezone.utc
        )
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now(now: datetime | None = None) -> datetime:
    instant = now or datetime.now(timezone.utc)
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed(start: str, end: str) -> float:
    return (_parse_time(end) - _parse_time(start)).total_seconds()


def score_attempt(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row,
    window_seconds: int = WINDOW_SECONDS,
) -> dict[str, Any] | None:
    """Score one complete, comparable attempt from its pseudonymous event timeline."""
    if (
        attempt["outcome"] != "joined"
        or attempt["fill_mode"] != "Public"
        or attempt["party_size"] != 1
        or attempt["mission_node_id"] is None
        or attempt["requested_region_id"] is None
        or attempt["assigned_at"] is None
        or attempt["observation_seconds"] is None
        or attempt["observation_seconds"] < window_seconds
        or attempt[f"team_size_{window_seconds}s"] is None
    ):
        return None

    events = connection.execute(
        """
        SELECT occurred_at, event_type, participant_hash, replaced_participant_hash,
               source_line, id
        FROM membership_events
        WHERE attempt_id=? AND occurred_at IS NOT NULL
        ORDER BY occurred_at, source_line, id
        """,
        (attempt["id"],),
    ).fetchall()
    present_since: dict[str, float] = {}
    first_arrival: dict[str, float] = {}
    retained: dict[str, float] = defaultdict(float)
    max_concurrent = 0
    for event in events:
        elapsed = _elapsed(attempt["assigned_at"], event["occurred_at"])
        if elapsed < 0:
            continue
        if elapsed > window_seconds:
            continue
        participant = event["participant_hash"]
        replaced = event["replaced_participant_hash"]
        if event["event_type"] == "slot_reused" and replaced in present_since:
            retained[replaced] += elapsed - present_since.pop(replaced)
        if event["event_type"] == "left":
            if participant in present_since:
                retained[participant] += elapsed - present_since.pop(participant)
            continue
        if not participant:
            continue
        first_arrival.setdefault(participant, elapsed)
        present_since.setdefault(participant, elapsed)
        max_concurrent = max(max_concurrent, len(present_since))

    for participant, started in present_since.items():
        retained[participant] += window_seconds - started

    if attempt[f"team_size_{window_seconds}s"] > 1 and not first_arrival:
        return None
    arrivals = sorted(first_arrival.values())[:3]
    arrival_score = sum(
        15.0 * max(0.0, 1.0 - arrival / window_seconds) for arrival in arrivals
    )
    concurrency_score = 20.0 * min(max_concurrent, 3) / 3.0
    unique_remote = len(first_arrival)
    breadth_score = 10.0 * min(unique_remote, 3) / 3.0
    possible_seconds = sum(
        max(0.0, window_seconds - arrival) for arrival in first_arrival.values()
    )
    retained_seconds = min(sum(retained.values()), possible_seconds)
    retention_score = (
        15.0 * retained_seconds / possible_seconds if possible_seconds > 0 else 0.0
    )
    latency = attempt["assignment_latency_seconds"]
    assignment_score = 10.0 * math.exp(-float(latency) / 30.0) if latency is not None else 0.0
    total = min(
        100.0,
        arrival_score
        + concurrency_score
        + breadth_score
        + retention_score
        + assignment_score,
    )
    evidence = {
        "window_seconds": window_seconds,
        "arrival_seconds": [round(value, 3) for value in arrivals],
        "max_concurrent_remote": max_concurrent,
        "unique_remote": unique_remote,
        "retained_teammate_seconds": round(retained_seconds, 3),
        "possible_teammate_seconds": round(possible_seconds, 3),
        "assignment_latency_seconds": latency,
        "components": {
            "arrival": round(arrival_score, 6),
            "concurrency": round(concurrency_score, 6),
            "breadth": round(breadth_score, 6),
            "retention": round(retention_score, 6),
            "assignment": round(assignment_score, 6),
        },
    }
    return {
        "attempt_id": attempt["id"],
        "mission_node_id": attempt["mission_node_id"],
        "region_id": attempt["requested_region_id"],
        "sample_at": attempt["ended_at"] or attempt["started_at"],
        "assignment_latency_seconds": latency,
        "score": total,
        "arrival_score": arrival_score,
        "concurrency_score": concurrency_score,
        "breadth_score": breadth_score,
        "retention_score": retention_score,
        "assignment_score": assignment_score,
        "max_concurrent_remote": max_concurrent,
        "unique_remote": unique_remote,
        "retained_teammate_seconds": retained_seconds,
        "possible_teammate_seconds": possible_seconds,
        "evidence_json": json.dumps(evidence, sort_keys=True),
    }


def _confidence(sample_count: int, effective_sample_size: float) -> str:
    if sample_count < 3:
        return "insufficient"
    if sample_count >= 20 and effective_sample_size >= 10:
        return "higher"
    if sample_count >= 8 and effective_sample_size >= 5:
        return "moderate"
    return "low"


def refresh_activity(
    connection: sqlite3.Connection,
    now: datetime | None = None,
    window_seconds: int = WINDOW_SECONDS,
) -> dict[str, int]:
    instant = _now(now)
    if window_seconds not in (15, 30, 60):
        raise ValueError("activity window must be 15, 30, or 60 seconds")
    all_attempts = connection.execute(
        f"""
        SELECT ma.*, a.assigned_at, a.assignment_latency_seconds
        FROM mission_attempts ma
        LEFT JOIN assignments a ON a.attempt_id=ma.id
        WHERE ma.stw_type='Mission' AND ma.fill_mode='Public' AND ma.party_size=1
          AND ma.mission_node_id IS NOT NULL AND ma.requested_region_id IS NOT NULL
        ORDER BY ma.started_at, ma.id
        """
    ).fetchall()
    latest_build_by_node: dict[int, str | None] = {}
    for attempt in all_attempts:
        latest_build_by_node[attempt["mission_node_id"]] = attempt["build_id"]
    attempts = [
        attempt
        for attempt in all_attempts
        if attempt["build_id"] == latest_build_by_node[attempt["mission_node_id"]]
    ]
    scored = [
        score
        for attempt in attempts
        if (score := score_attempt(connection, attempt, window_seconds)) is not None
    ]
    with connection:
        connection.execute(
            "DELETE FROM attempt_activity_scores WHERE score_version=? AND window_seconds=?",
            (SCORE_VERSION, window_seconds),
        )
        connection.executemany(
            """
            INSERT INTO attempt_activity_scores(
                attempt_id, score_version, window_seconds, score, arrival_score,
                concurrency_score, breadth_score, retention_score, assignment_score,
                max_concurrent_remote, unique_remote, retained_teammate_seconds,
                possible_teammate_seconds, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    score["attempt_id"], SCORE_VERSION, window_seconds, score["score"],
                    score["arrival_score"], score["concurrency_score"],
                    score["breadth_score"], score["retention_score"],
                    score["assignment_score"], score["max_concurrent_remote"],
                    score["unique_remote"], score["retained_teammate_seconds"],
                    score["possible_teammate_seconds"], score["evidence_json"],
                )
                for score in scored
            ],
        )
        connection.execute(
            "DELETE FROM regional_activity WHERE score_version=? AND window_seconds=?",
            (SCORE_VERSION, window_seconds),
        )

        grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for score in scored:
            grouped[(score["mission_node_id"], score["region_id"])].append(score)
        for (node_id, region_id), samples in grouped.items():
            weights = []
            for sample in samples:
                age_hours = max(
                    0.0,
                    (instant - _parse_time(sample["sample_at"])).total_seconds() / 3600.0,
                )
                weights.append(2.0 ** (-age_hours / RECENCY_HALF_LIFE_HOURS))
            weight_sum = sum(weights)
            effective_size = weight_sum * weight_sum / sum(w * w for w in weights)

            def weighted(field: str) -> float:
                return sum(w * float(s[field]) for w, s in zip(weights, samples)) / weight_sum

            cohort_attempts = [
                attempt
                for attempt in attempts
                if attempt["mission_node_id"] == node_id
                and attempt["requested_region_id"] == region_id
            ]
            joined_count = sum(attempt["outcome"] == "joined" for attempt in cohort_attempts)
            coverage = {
                seconds: (
                    sum(
                        attempt["outcome"] == "joined"
                        and attempt["observation_seconds"] is not None
                        and attempt["observation_seconds"] >= seconds
                        and attempt[f"team_size_{seconds}s"] is not None
                        for attempt in cohort_attempts
                    )
                    / joined_count
                    if joined_count
                    else 0.0
                )
                for seconds in (15, 30, 60)
            }
            latencies = [
                float(attempt["assignment_latency_seconds"])
                for attempt in cohort_attempts
                if attempt["assignment_latency_seconds"] is not None
            ]
            external = connection.execute(
                """
                SELECT external_mission_id FROM mission_matches
                WHERE mission_node_id=? AND status='accepted'
                ORDER BY matched_at DESC, id DESC LIMIT 1
                """,
                (node_id,),
            ).fetchone()
            sample_ids = [sample["attempt_id"] for sample in samples]
            evidence = {
                "attempt_ids": sample_ids,
                "weights": [round(weight, 8) for weight in weights],
                "half_life_hours": RECENCY_HALF_LIFE_HOURS,
                "eligibility": "joined_public_fill_solo_complete_window",
                "score_version": SCORE_VERSION,
                "build_id": latest_build_by_node[node_id],
                "party_size": 1,
                "fill_mode": "Public",
            }
            connection.execute(
                """
                INSERT INTO regional_activity(
                    mission_node_id, external_mission_id, region_id, window_start,
                    window_end, score_version, window_seconds, score, arrival_score,
                    concurrency_score, breadth_score, retention_score, assignment_score,
                    sample_count, effective_sample_size, latest_sample_at, coverage,
                    confidence_band, median_assignment_latency_seconds,
                    assignment_join_completion_rate, evidence_json,
                    coverage_15, coverage_30, coverage_60
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    external["external_mission_id"] if external else None,
                    region_id,
                    min(sample["sample_at"] for sample in samples),
                    _iso(instant),
                    SCORE_VERSION,
                    window_seconds,
                    weighted("score"),
                    weighted("arrival_score"),
                    weighted("concurrency_score"),
                    weighted("breadth_score"),
                    weighted("retention_score"),
                    weighted("assignment_score"),
                    len(samples),
                    effective_size,
                    max(sample["sample_at"] for sample in samples),
                    coverage[60],
                    _confidence(len(samples), effective_size),
                    median(latencies) if latencies else None,
                    joined_count / len(cohort_attempts) if cohort_attempts else 0.0,
                    json.dumps(evidence, sort_keys=True),
                    coverage[15],
                    coverage[30],
                    coverage[60],
                ),
            )
    return {"attempts_scored": len(scored), "regions_scored": len(grouped)}


def activity_overview(
    connection: sqlite3.Connection, mission_node_id: int | None = None
) -> dict[str, Any]:
    if mission_node_id is None:
        row = connection.execute(
            """
            SELECT mission_node_id FROM regional_activity
            WHERE score_version=? AND window_seconds=?
            ORDER BY latest_sample_at DESC, mission_node_id DESC LIMIT 1
            """,
            (SCORE_VERSION, WINDOW_SECONDS),
        ).fetchone()
        mission_node_id = row["mission_node_id"] if row else None
    if mission_node_id is None:
        return {
            "definition": "Observed activity from this client; not population or CCU.",
            "score_version": SCORE_VERSION,
            "window_seconds": WINDOW_SECONDS,
            "mission": None,
            "regions": [],
            "leader": None,
        }
    rows = connection.execute(
        """
        SELECT ra.*, r.code AS region, mn.mission_uuid, mn.theater_uuid,
               COALESCE(
                   em.power_level,
                   (SELECT ma.power_level FROM mission_attempts ma
                    WHERE ma.mission_node_id=ra.mission_node_id
                      AND ma.power_level IS NOT NULL
                    ORDER BY ma.started_at DESC, ma.id DESC LIMIT 1)
               ) AS power_level,
               em.theater_name, em.theater_code,
               COALESCE(
                   o.display_name,
                   (SELECT ma.objective_hint FROM mission_attempts ma
                    WHERE ma.mission_node_id=ra.mission_node_id
                      AND ma.objective_hint IS NOT NULL
                    ORDER BY ma.started_at DESC, ma.id DESC LIMIT 1)
               ) AS objective
        FROM regional_activity ra
        JOIN regions r ON r.id=ra.region_id
        JOIN mission_nodes mn ON mn.id=ra.mission_node_id
        LEFT JOIN external_missions em ON em.id=ra.external_mission_id
        LEFT JOIN objectives o ON o.id=em.objective_id
        WHERE ra.mission_node_id=? AND ra.score_version=? AND ra.window_seconds=?
        ORDER BY ra.score DESC, ra.sample_count DESC, r.code
        """,
        (mission_node_id, SCORE_VERSION, WINDOW_SECONDS),
    ).fetchall()
    regions = [
        {
            "region": row["region"],
            "score": round(row["score"], 2),
            "components": {
                "arrival": round(row["arrival_score"], 2),
                "concurrency": round(row["concurrency_score"], 2),
                "breadth": round(row["breadth_score"], 2),
                "retention": round(row["retention_score"], 2),
                "assignment": round(row["assignment_score"], 2),
            },
            "sample_count": row["sample_count"],
            "effective_sample_size": round(row["effective_sample_size"], 2),
            "latest_sample_at": row["latest_sample_at"],
            "latest_sample_age_seconds": round(
                max(
                    0.0,
                    (_now() - _parse_time(row["latest_sample_at"])).total_seconds(),
                ),
                1,
            ),
            "coverage": round(row["coverage"], 4),
            "coverage_15": round(row["coverage_15"], 4),
            "coverage_30": round(row["coverage_30"], 4),
            "coverage_60": round(row["coverage_60"], 4),
            "confidence": row["confidence_band"],
            "median_assignment_latency_seconds": row[
                "median_assignment_latency_seconds"
            ],
            "assignment_join_completion_rate": round(
                row["assignment_join_completion_rate"], 4
            ),
        }
        for row in rows
    ]
    first = rows[0] if rows else None
    mission = (
        {
            "mission_node_id": mission_node_id,
            "mission_uuid": first["mission_uuid"],
            "theater_uuid": first["theater_uuid"],
            "theater": first["theater_name"] or first["theater_code"],
            "power_level": first["power_level"],
            "objective": first["objective"],
            "window_start": min(row["window_start"] for row in rows),
            "window_end": max(row["window_end"] for row in rows),
        }
        if first
        else None
    )
    leader = regions[0] if regions and regions[0]["confidence"] != "insufficient" else None
    return {
        "definition": (
            "Observed matchmaking activity from this client for one exact mission and "
            "rotation; this is not regional population, queue depth, or CCU."
        ),
        "score_version": SCORE_VERSION,
        "window_seconds": WINDOW_SECONDS,
        "half_life_hours": RECENCY_HALF_LIFE_HOURS,
        "mission": mission,
        "regions": regions,
        "leader": leader,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="recompute attempt and regional activity scores")
    report = commands.add_parser("report", help="print the latest regional comparison")
    report.add_argument("--mission-node", type=int)
    args = parser.parse_args()

    from stw_pipeline import connect

    connection = connect(args.db)
    try:
        if args.command == "refresh":
            print(json.dumps(refresh_activity(connection), indent=2))
        else:
            print(json.dumps(activity_overview(connection, args.mission_node), indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
