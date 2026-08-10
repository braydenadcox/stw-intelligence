#!/usr/bin/env python3
"""Compute versioned observed matchmaking activity from local mission attempts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCORE_VERSION = "observed-activity-v1"
WINDOW_SECONDS = 60
RECENCY_HALF_LIFE_HOURS = 6.0
RECOMMENDATION_VERSION = "activity-ranking-v1"
TIME_CONTEXT_VERSION = "local-time-band-v1"
TIME_BAND_HOURS = 3
COHORT_VERSION = "mission-signature-v1"
SCORE_TIE_EPSILON = 0.01


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


def recommend_region(
    regions: list[dict[str, Any]], mission_scope: str = "this exact mission"
) -> dict[str, Any]:
    """Turn comparable regional activity into an explainable, abstaining decision."""
    eligible = [row for row in regions if row["confidence"] != "insufficient"]
    basis = {
        "version": RECOMMENDATION_VERSION,
        "ranking_key": "observed_matchmaking_activity_score",
        "minimum_complete_samples": 3,
        "network_ping_used": False,
        "network_ping_status": "unknown",
        "assignment_latency_note": (
            "Assignment speed contributes at most 10 points to activity; median "
            "assignment latency is also shown separately and is not network ping."
        ),
    }


    if not eligible:
        return {
            "status": "insufficient_data",
            "region": None,
            "headline": "Not enough evidence yet",
            "summary": "No region has three complete comparable attempts.",
            "confidence": "insufficient",
            "score": None,
            "runner_up": None,
            "alternatives": [],
            "reasons": [
                "Complete 60-second Public Fill observations are required.",
                "The system abstains instead of guessing from incomplete regions.",
            ],
            "basis": basis,
        }

    ranked = sorted(
        eligible,
        key=lambda row: (-float(row["score"]), -int(row["sample_count"]), row["region"]),
    )
    winner = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    margin = float(winner["score"]) - float(runner["score"]) if runner else None
    alternatives = [
        {
            "region": row["region"],
            "score": row["score"],
            "confidence": row["confidence"],
        }
        for row in ranked[1:4]
    ]
    if runner is not None and abs(margin or 0.0) <= SCORE_TIE_EPSILON:
        return {
            "status": "no_clear_leader",
            "region": None,
            "headline": "No clear regional leader",
            "summary": (
                f"{winner['region']} and {runner['region']} have effectively tied "
                "observed activity scores."
            ),
            "confidence": "insufficient",
            "score": winner["score"],
            "runner_up": {
                "region": runner["region"],
                "score": runner["score"],
                "margin": round(margin or 0.0, 2),
            },
            "alternatives": alternatives,
            "reasons": [
                "The leading scores are equal within 0.01 points.",
                "The system abstains rather than breaking a tie by row order.",
            ],
            "basis": basis,
        }

    membership_points = sum(
        float(winner["components"][name])
        for name in ("arrival", "concurrency", "breadth", "retention")
    )
    if runner:
        lead_reason = (
            f"Its activity score leads {runner['region']} by {margin:.2f} points."
        )
        runner_up = {
            "region": runner["region"],
            "score": runner["score"],
            "margin": round(margin, 2),
        }
    else:
        lead_reason = "It is the only region with enough comparable evidence."
        runner_up = None
    return {
        "status": "recommended",
        "region": winner["region"],
        "headline": f"Try {winner['region']}",
        "summary": (
            f"{winner['region']} has the strongest observed matchmaking activity "
            f"for {mission_scope}, with {winner['confidence']} evidence confidence."
        ),
        "confidence": winner["confidence"],
        "score": winner["score"],
        "runner_up": runner_up,
        "alternatives": alternatives,
        "reasons": [
            lead_reason,
            (
                f"Teammate arrival, concurrency, breadth, and retention contributed "
                f"{membership_points:.2f} of 90 possible points."
            ),
            (
                f"Assignment speed contributed "
                f"{float(winner['components']['assignment']):.2f} of 10 possible points."
            ),
            (
                f"Evidence: {winner['sample_count']} complete attempts, effective "
                f"sample size {winner['effective_sample_size']:.2f}, "
                f"{winner['confidence']} confidence."
            ),
            (
                "Network ping was not observed and was not used in this recommendation."
            ),
        ],
        "basis": basis,
    }


def refresh_mission_cohorts(connection: sqlite3.Connection) -> dict[str, int]:
    """Link rotation-scoped nodes only when provider-backed identity is complete."""
    nodes = connection.execute("SELECT id FROM mission_nodes ORDER BY id").fetchall()
    matches = connection.execute(
        """
        SELECT mm.id AS match_id, mm.mission_node_id, mm.confidence,
               em.id AS external_mission_id, em.theater_code,
               o.canonical_code AS objective_code, em.power_level,
               em.is_four_player, mr.provider_rotation_key,
               mr.valid_from, mr.valid_until
        FROM mission_matches mm
        JOIN external_missions em ON em.id=mm.external_mission_id
        JOIN objectives o ON o.id=em.objective_id
        JOIN mission_rotations mr ON mr.id=mm.rotation_id
        WHERE mm.status='accepted'
        ORDER BY mm.mission_node_id, mm.matched_at, mm.id
        """
    ).fetchall()
    by_node: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for match in matches:
        by_node[match["mission_node_id"]].append(match)
    counts = {"cohorts": 0, "included": 0, "excluded": 0, "conflicts": 0}
    with connection:
        connection.execute(
            "DELETE FROM mission_cohort_memberships WHERE cohort_version=?",
            (COHORT_VERSION,),
        )
        for node in nodes:
            node_id = node["id"]
            accepted = [
                row for row in by_node.get(node_id, [])
                if row["confidence"] in ("high", "medium")
            ]
            signatures = {
                (
                    row["theater_code"], row["objective_code"],
                    row["power_level"], int(row["is_four_player"]),
                )
                for row in accepted
            }
            evidence: dict[str, Any] = {
                "cohort_version": COHORT_VERSION,
                "mission_node_id": node_id,
                "accepted_match_ids": [row["match_id"] for row in accepted],
                "rule": (
                    "accepted_provider_match_with_consistent_theater_objective_"
                    "power_level_and_four_player_status"
                ),
            }
            if not accepted or len(signatures) != 1:
                status = "excluded" if not accepted else "conflict"
                evidence["reason"] = (
                    "no_high_or_medium_confidence_accepted_match"
                    if not accepted else "conflicting_accepted_mission_identities"
                )
                if signatures:
                    evidence["signatures"] = [list(value) for value in sorted(signatures)]
                connection.execute(
                    """
                    INSERT INTO mission_cohort_memberships(
                        mission_node_id, cohort_version, status, evidence_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (node_id, COHORT_VERSION, status, json.dumps(evidence, sort_keys=True)),
                )
                counts["excluded" if status == "excluded" else "conflicts"] += 1
                continue
            theater, objective, power_level, is_four_player = next(iter(signatures))
            identity = {
                "theater_code": theater,
                "objective_code": objective,
                "power_level": power_level,
                "is_four_player": bool(is_four_player),
            }
            identity_key = hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO mission_cohorts(
                    cohort_version, identity_key, theater_code, objective_code,
                    power_level, is_four_player
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cohort_version, identity_key) DO NOTHING
                """,
                (
                    COHORT_VERSION, identity_key, theater, objective,
                    power_level, is_four_player,
                ),
            )
            cohort_id = connection.execute(
                "SELECT id FROM mission_cohorts WHERE cohort_version=? AND identity_key=?",
                (COHORT_VERSION, identity_key),
            ).fetchone()[0]
            chosen = accepted[-1]
            evidence["identity"] = identity
            evidence["source_rotation"] = {
                "provider_rotation_key": chosen["provider_rotation_key"],
                "valid_from": chosen["valid_from"],
                "valid_until": chosen["valid_until"],
            }
            connection.execute(
                """
                INSERT INTO mission_cohort_memberships(
                    mission_node_id, cohort_id, cohort_version, status,
                    mission_match_id, external_mission_id, evidence_json
                ) VALUES (?, ?, ?, 'included', ?, ?, ?)
                """,
                (
                    node_id, cohort_id, COHORT_VERSION, chosen["match_id"],
                    chosen["external_mission_id"], json.dumps(evidence, sort_keys=True),
                ),
            )
            counts["included"] += 1
        counts["cohorts"] = connection.execute(
            """
            SELECT COUNT(DISTINCT cohort_id) FROM mission_cohort_memberships
            WHERE cohort_version=? AND status='included'
            """,
            (COHORT_VERSION,),
        ).fetchone()[0]
    return counts


def refresh_activity(
    connection: sqlite3.Connection,
    now: datetime | None = None,
    window_seconds: int = WINDOW_SECONDS,
) -> dict[str, int]:
    instant = _now(now)
    cohort_counts = refresh_mission_cohorts(connection)
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
    latest_build_attempts = [
        attempt
        for attempt in all_attempts
        if attempt["build_id"] == latest_build_by_node[attempt["mission_node_id"]]
    ]
    # A growing Fortnite log can be imported as more than one capture. The same
    # attempt then has a different capture_id but identical observational identity.
    # Count it once, preferring the most complete copy, so evidence is not inflated.
    deduplicated: dict[tuple[Any, ...], sqlite3.Row] = {}
    for attempt in latest_build_attempts:
        identity = (
            attempt["mission_node_id"],
            attempt["requested_region_id"],
            attempt["started_at"],
            attempt["build_id"],
        )
        current = deduplicated.get(identity)
        quality = (
            attempt["observation_seconds"] or -1,
            attempt["source_line_end"] or -1,
            attempt["id"],
        )
        if current is None or quality > (
            current["observation_seconds"] or -1,
            current["source_line_end"] or -1,
            current["id"],
        ):
            deduplicated[identity] = attempt
    attempts = sorted(
        deduplicated.values(), key=lambda attempt: (attempt["started_at"], attempt["id"])
    )
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
    return {
        "attempts_scored": len(scored),
        "regions_scored": len(grouped),
        **{f"cohort_{key}": value for key, value in cohort_counts.items()},
    }


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
        recommendation = recommend_region([])
        return {
            "definition": "Observed activity from this client; not population or CCU.",
            "score_version": SCORE_VERSION,
            "window_seconds": WINDOW_SECONDS,
            "mission": None,
            "regions": [],
            "leader": None,
            "recommendation": recommendation,
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
    recommendation = recommend_region(regions)
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
        "recommendation": recommendation,
    }


def _cohort_scope(
    connection: sqlite3.Connection, mission_node_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT mcm.status, mcm.cohort_id, mcm.evidence_json,
               mc.theater_code, mc.objective_code, mc.power_level,
               mc.is_four_player
        FROM mission_cohort_memberships mcm
        LEFT JOIN mission_cohorts mc ON mc.id=mcm.cohort_id
        WHERE mcm.mission_node_id=? AND mcm.cohort_version=?
        """,
        (mission_node_id, COHORT_VERSION),
    ).fetchone()
    if row is None or row["status"] != "included":
        evidence = json.loads(row["evidence_json"]) if row else {}
        return {
            "type": "exact_rotation",
            "status": row["status"] if row else "not_evaluated",
            "cohort_version": COHORT_VERSION,
            "cohort_id": None,
            "node_ids": [mission_node_id],
            "node_count": 1,
            "rotation_count": 1,
            "identity": None,
            "reason": evidence.get("reason", "no_safe_cross_rotation_identity"),
        }
    members = connection.execute(
        """
        SELECT mcm.mission_node_id, mn.rotation_context
        FROM mission_cohort_memberships mcm
        JOIN mission_nodes mn ON mn.id=mcm.mission_node_id
        WHERE mcm.cohort_id=? AND mcm.cohort_version=? AND mcm.status='included'
        ORDER BY mn.first_seen_at, mcm.mission_node_id
        """,
        (row["cohort_id"], COHORT_VERSION),
    ).fetchall()
    rotations = {member["rotation_context"] for member in members}
    return {
        "type": "cross_rotation_cohort",
        "status": "cross_rotation" if len(rotations) > 1 else "single_rotation",
        "cohort_version": COHORT_VERSION,
        "cohort_id": row["cohort_id"],
        "node_ids": [member["mission_node_id"] for member in members],
        "node_count": len(members),
        "rotation_count": len(rotations),
        "identity": {
            "theater_code": row["theater_code"],
            "objective_code": row["objective_code"],
            "power_level": row["power_level"],
            "is_four_player": bool(row["is_four_player"]),
        },
        "reason": None,
    }


def _aggregate_sample_regions(
    connection: sqlite3.Connection,
    node_ids: list[int],
    instant: datetime,
    local_zone: ZoneInfo | None = None,
    band_start: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    placeholders = ",".join("?" for _ in node_ids)
    rows = connection.execute(
        f"""
        SELECT r.code AS region, ma.ended_at, ma.started_at, ma.build_id,
               aas.score, aas.arrival_score, aas.concurrency_score,
               aas.breadth_score, aas.retention_score, aas.assignment_score,
               a.assignment_latency_seconds
        FROM attempt_activity_scores aas
        JOIN mission_attempts ma ON ma.id=aas.attempt_id
        JOIN regions r ON r.id=ma.requested_region_id
        LEFT JOIN assignments a ON a.attempt_id=ma.id
        WHERE ma.mission_node_id IN ({placeholders}) AND aas.score_version=?
          AND aas.window_seconds=?
        ORDER BY COALESCE(ma.ended_at, ma.started_at), ma.id
        """,
        (*node_ids, SCORE_VERSION, WINDOW_SECONDS),
    ).fetchall()
    eligible: list[tuple[sqlite3.Row, datetime]] = []
    for row in rows:
        sample_at = _parse_time(row["ended_at"] or row["started_at"])
        if sample_at <= instant:
            eligible.append((row, sample_at))
    latest_build = eligible[-1][0]["build_id"] if eligible else None
    eligible = [pair for pair in eligible if pair[0]["build_id"] == latest_build]
    if local_zone is not None and band_start is not None:
        eligible = [
            pair for pair in eligible
            if pair[1].astimezone(local_zone).hour // TIME_BAND_HOURS
            * TIME_BAND_HOURS == band_start
        ]
    grouped: dict[str, list[tuple[sqlite3.Row, datetime]]] = defaultdict(list)
    for row, sample_at in eligible:
        grouped[row["region"]].append((row, sample_at))
    regions: list[dict[str, Any]] = []
    for region, samples in grouped.items():
        weights = [
            2.0 ** (
                -max(0.0, (instant - sample_at).total_seconds() / 3600.0)
                / RECENCY_HALF_LIFE_HOURS
            )
            for _, sample_at in samples
        ]
        weight_sum = sum(weights)
        effective_size = weight_sum * weight_sum / sum(weight * weight for weight in weights)

        def weighted(field: str) -> float:
            return sum(
                weight * float(row[field])
                for weight, (row, _) in zip(weights, samples)
            ) / weight_sum

        latencies = [
            float(row["assignment_latency_seconds"])
            for row, _ in samples if row["assignment_latency_seconds"] is not None
        ]
        regions.append(
            {
                "region": region,
                "score": round(weighted("score"), 2),
                "components": {
                    name: round(weighted(f"{name}_score"), 2)
                    for name in ("arrival", "concurrency", "breadth", "retention", "assignment")
                },
                "sample_count": len(samples),
                "effective_sample_size": round(effective_size, 2),
                "confidence": _confidence(len(samples), effective_size),
                "median_assignment_latency_seconds": median(latencies) if latencies else None,
            }
        )
    regions.sort(key=lambda region: (-region["score"], region["region"]))
    return regions, {
        "sample_count": len(eligible),
        "build_id": latest_build,
        "build_rule": "latest_observed_build_only",
    }


def recommendation_overview(
    connection: sqlite3.Connection,
    mission_node_id: int | None = None,
    at: datetime | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    overview = activity_overview(connection, mission_node_id)
    instant = _now(at)
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {timezone_name}") from error
    local_at = instant.astimezone(local_zone)
    band_start = local_at.hour // TIME_BAND_HOURS * TIME_BAND_HOURS
    band_end = band_start + TIME_BAND_HOURS - 1
    node_id = overview["mission"]["mission_node_id"] if overview["mission"] else None
    scope = (
        _cohort_scope(connection, node_id)
        if node_id is not None else {
            "type": "exact_rotation", "status": "not_evaluated",
            "cohort_version": COHORT_VERSION, "cohort_id": None,
            "node_ids": [], "node_count": 0, "rotation_count": 0,
            "identity": None, "reason": "no_mission",
        }
    )
    node_ids = scope["node_ids"]
    if node_ids:
        cohort_regions, cohort_evidence = _aggregate_sample_regions(
            connection, node_ids, instant
        )
        time_regions, time_evidence = _aggregate_sample_regions(
            connection, node_ids, instant, local_zone, band_start
        )
    else:
        cohort_regions, time_regions = [], []
        cohort_evidence = time_evidence = {
            "sample_count": 0, "build_id": None,
            "build_rule": "latest_observed_build_only",
        }
    base_recommendation = (
        recommend_region(cohort_regions, "this comparable mission cohort")
        if scope["type"] == "cross_rotation_cohort"
        else overview["recommendation"]
    )
    if scope["type"] == "cross_rotation_cohort" and scope["rotation_count"] > 1:
        base_recommendation = dict(base_recommendation)
        base_recommendation["summary"] += (
            f" Evidence spans {scope['node_count']} mission nodes across "
            f"{scope['rotation_count']} daily rotations."
        )
    sufficient_regions = [
        region for region in time_regions if region["confidence"] != "insufficient"
    ]
    context = {
        "version": TIME_CONTEXT_VERSION,
        "timezone": timezone_name,
        "target_at": _iso(instant),
        "local_at": local_at.isoformat(),
        "local_day": local_at.strftime("%A"),
        "band_start_hour": band_start,
        "band_end_hour": band_end,
        "band_label": f"{band_start:02d}:00-{band_end:02d}:59",
        "minimum_samples_per_region": 3,
        "samples_in_band": time_evidence["sample_count"],
        "regions_with_sufficient_data": len(sufficient_regions),
        "status": "fallback",
        "fallback_reason": None,
        "build_id": time_evidence["build_id"],
    }
    if len(sufficient_regions) >= 2:
        context["status"] = "time_specific"
        recommendation = recommend_region(
            time_regions,
            "this comparable mission cohort"
            if scope["type"] == "cross_rotation_cohort" else "this exact mission",
        )
        recommendation["summary"] += (
            f" This uses observations from the local {context['band_label']} time band."
        )
        if scope["type"] == "cross_rotation_cohort" and scope["rotation_count"] > 1:
            recommendation["summary"] += (
                f" Comparable evidence spans {scope['rotation_count']} rotations."
            )
    else:
        context["fallback_reason"] = (
            "Fewer than two regions have three complete samples in this local time band."
        )
        recommendation = dict(base_recommendation)
        recommendation["summary"] += (
            " Time-specific evidence is not sufficient, so this is the overall recent ranking."
        )
    return {
        "definition": (
            "Observed matchmaking activity from this client across only confidently "
            "matched comparable missions; this is not population, queue depth, or CCU."
            if scope["type"] == "cross_rotation_cohort" else overview["definition"]
        ),
        "mission": overview["mission"],
        "scope": {key: value for key, value in scope.items() if key != "node_ids"},
        "recommendation": recommendation,
        "base_recommendation": base_recommendation,
        "time_context": context,
        "time_regions": time_regions,
        "cohort_regions": cohort_regions,
        "cohort_evidence": cohort_evidence,
    }


def cohort_catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    cohorts = [
        {
            "cohort_id": row["id"],
            "cohort_version": row["cohort_version"],
            "theater_code": row["theater_code"],
            "objective_code": row["objective_code"],
            "power_level": row["power_level"],
            "is_four_player": bool(row["is_four_player"]),
            "node_count": row["node_count"],
            "rotation_count": row["rotation_count"],
            "mission_node_ids": json.loads(row["node_ids_json"]),
        }
        for row in connection.execute(
            """
            SELECT mc.*,
                   COUNT(mcm.mission_node_id) AS node_count,
                   COUNT(DISTINCT mn.rotation_context) AS rotation_count,
                   json_group_array(mcm.mission_node_id) AS node_ids_json
            FROM mission_cohorts mc
            JOIN mission_cohort_memberships mcm
              ON mcm.cohort_id=mc.id AND mcm.status='included'
            JOIN mission_nodes mn ON mn.id=mcm.mission_node_id
            WHERE mc.cohort_version=?
            GROUP BY mc.id ORDER BY rotation_count DESC, node_count DESC, mc.id
            """,
            (COHORT_VERSION,),
        )
    ]
    excluded = [
        {
            "mission_node_id": row["mission_node_id"],
            "status": row["status"],
            "reason": json.loads(row["evidence_json"]).get("reason"),
        }
        for row in connection.execute(
            """
            SELECT mission_node_id, status, evidence_json
            FROM mission_cohort_memberships
            WHERE cohort_version=? AND status<>'included'
            ORDER BY mission_node_id
            """,
            (COHORT_VERSION,),
        )
    ]
    return {
        "cohort_version": COHORT_VERSION,
        "definition": (
            "Provider-backed comparable mission identities; observed nodes remain "
            "rotation-scoped and are never merged."
        ),
        "cohorts": cohorts,
        "excluded": excluded,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="recompute attempt and regional activity scores")
    commands.add_parser("cohorts", help="inspect cross-rotation mission cohorts")
    report = commands.add_parser("report", help="print the latest regional comparison")
    report.add_argument("--mission-node", type=int)
    recommend = commands.add_parser(
        "recommend", help="print the current evidence-based region recommendation"
    )
    recommend.add_argument("--mission-node", type=int)
    recommend.add_argument("--at", help="recommendation time (ISO-8601, defaults to now)")
    recommend.add_argument("--timezone", default="America/Los_Angeles")
    args = parser.parse_args()

    from stw_pipeline import connect

    connection = connect(args.db)
    try:
        if args.command == "refresh":
            print(json.dumps(refresh_activity(connection), indent=2))
        elif args.command == "cohorts":
            print(json.dumps(cohort_catalog(connection), indent=2))
        elif args.command == "report":
            print(json.dumps(activity_overview(connection, args.mission_node), indent=2))
        else:
            print(
                json.dumps(
                    recommendation_overview(
                        connection,
                        args.mission_node,
                        _parse_time(args.at) if args.at else None,
                        args.timezone,
                    ),
                    indent=2,
                )
            )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
