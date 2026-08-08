"""JSON-ready read models for the local STW application."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from stw_live import watcher_health
from stw_providers import latest_rotation_id


ATTEMPT_SELECT = """
SELECT ma.*, mn.theater_uuid, mn.mission_uuid, mn.rotation_context,
       r.code AS requested_region, dc.code AS datacenter,
       a.assigned_at, a.assignment_latency_seconds, a.match_identifier,
       ls.session_identifier, em.id AS external_mission_id,
       em.theater_name AS friendly_theater, em.theater_code,
       em.power_level AS friendly_power_level, em.biome_name, em.biome_code,
       o.display_name AS friendly_objective,
       mm.confidence AS match_confidence
FROM mission_attempts ma
LEFT JOIN mission_nodes mn ON mn.id=ma.mission_node_id
LEFT JOIN regions r ON r.id=ma.requested_region_id
LEFT JOIN assignments a ON a.attempt_id=ma.id
LEFT JOIN datacenters dc ON dc.id=a.datacenter_id
LEFT JOIN lobby_sessions ls ON ls.id=a.lobby_session_id
LEFT JOIN mission_matches mm ON mm.id=(
    SELECT mm2.id FROM mission_matches mm2
    WHERE mm2.mission_node_id=ma.mission_node_id AND mm2.status='accepted'
    ORDER BY mm2.matched_at DESC, mm2.id DESC LIMIT 1
)
LEFT JOIN external_missions em ON em.id=mm.external_mission_id
LEFT JOIN objectives o ON o.id=em.objective_id
"""


def _team_size(connection: sqlite3.Connection, attempt_id: int, row: sqlite3.Row) -> int | None:
    event = connection.execute(
        """
        SELECT team_size_after FROM membership_events
        WHERE attempt_id=? ORDER BY source_line DESC, id DESC LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    if event:
        return event["team_size_after"]
    return row["team_size_at_start"] or row["party_size"]


def _attempt_summary(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    objective = row["friendly_objective"] or row["objective_hint"]
    power = row["friendly_power_level"] or row["power_level"]
    session = row["session_identifier"]
    elapsed = row["assignment_latency_seconds"]
    if elapsed is None and row["started_at"]:
        try:
            started = datetime.strptime(
                row["started_at"], "%Y.%m.%d-%H.%M.%S:%f"
            ).replace(tzinfo=timezone.utc)
            elapsed = max(0.0, round((datetime.now(timezone.utc) - started).total_seconds(), 3))
        except ValueError:
            elapsed = None
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "outcome": row["outcome"],
        "mission_node_id": row["mission_node_id"],
        "mission_uuid": row["mission_uuid"],
        "theater_uuid": row["theater_uuid"],
        "theater": row["friendly_theater"] or row["theater_code"],
        "objective": objective,
        "power_level": power,
        "biome": row["biome_name"] or row["biome_code"],
        "match_confidence": row["match_confidence"],
        "requested_region": row["requested_region"],
        "fill_mode": row["fill_mode"],
        "party_size": row["party_size"],
        "datacenter": row["datacenter"],
        "assignment_latency_seconds": row["assignment_latency_seconds"],
        "assignment_elapsed_seconds": elapsed,
        "session_fingerprint": session[:12] if session else None,
        "current_team_size": _team_size(connection, row["id"], row),
    }


def recent_attempts(connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = connection.execute(
        ATTEMPT_SELECT + " ORDER BY ma.started_at DESC, ma.id DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [_attempt_summary(connection, row) for row in rows]


def attempt_detail(connection: sqlite3.Connection, attempt_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        ATTEMPT_SELECT + " WHERE ma.id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None
    result = _attempt_summary(connection, row)
    result["maps"] = [
        dict(event)
        for event in connection.execute(
            """
            SELECT observed_at, map_path, source_line FROM attempt_maps
            WHERE attempt_id=? ORDER BY source_line
            """,
            (attempt_id,),
        )
    ]
    result["membership_events"] = [
        {
            "occurred_at": event["occurred_at"],
            "phase": event["phase"],
            "event_type": event["event_type"],
            "participant": f"teammate-{event['participant_hash'][:10]}"
            if event["participant_hash"]
            else None,
            "replaced_participant": f"teammate-{event['replaced_participant_hash'][:10]}"
            if event["replaced_participant_hash"]
            else None,
            "slot": event["slot"],
            "team_size_after": event["team_size_after"],
            "source_line": event["source_line"],
        }
        for event in connection.execute(
            """
            SELECT * FROM membership_events
            WHERE attempt_id=? ORDER BY source_line, id
            """,
            (attempt_id,),
        )
    ]
    result["state_events"] = [
        dict(event)
        for event in connection.execute(
            """
            SELECT occurred_at, state, reason, source_line FROM live_state_events
            WHERE attempt_id=? ORDER BY source_line, id
            """,
            (attempt_id,),
        )
    ]
    return result


def current_state(connection: sqlite3.Connection) -> dict[str, Any]:
    state = connection.execute(
        """
        SELECT ls.*, lw.status AS watcher_status, lw.source_path, lw.last_error
        FROM live_states ls JOIN log_watchers lw ON lw.id=ls.watcher_id
        ORDER BY ls.updated_at DESC, ls.watcher_id DESC LIMIT 1
        """
    ).fetchone()
    if state is None:
        return {
            "state": "Idle",
            "attempt": None,
            "watcher_status": "not_configured",
            "updated_at": None,
        }
    attempt = attempt_detail(connection, state["attempt_id"]) if state["attempt_id"] else None
    return {
        "state": state["state"],
        "reason": state["reason"],
        "occurred_at": state["occurred_at"],
        "updated_at": state["updated_at"],
        "watcher_status": state["watcher_status"],
        "watcher_error": state["last_error"],
        "attempt": attempt,
    }


def current_missions(connection: sqlite3.Connection) -> dict[str, Any]:
    rotation_id = latest_rotation_id(connection)
    if rotation_id is None:
        return {"rotation": None, "missions": []}
    rotation = connection.execute(
        """
        SELECT mr.*, p.code AS provider_code, p.display_name AS provider_name,
               ps.fetched_at, ps.source_timestamp
        FROM mission_rotations mr
        JOIN providers p ON p.id=mr.provider_id
        JOIN provider_snapshots ps ON ps.id=mr.snapshot_id
        WHERE mr.id=?
        """,
        (rotation_id,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    valid_from = datetime.fromisoformat(rotation["valid_from"].replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(rotation["valid_until"].replace("Z", "+00:00"))
    freshness = "future" if now < valid_from else "current" if now < valid_until else "stale"
    missions = []
    for row in connection.execute(
        """
        SELECT em.*, o.display_name AS objective
        FROM external_missions em JOIN objectives o ON o.id=em.objective_id
        WHERE em.rotation_id=? ORDER BY em.theater_code, em.power_level, o.display_name, em.id
        """,
        (rotation_id,),
    ):
        mission = dict(row)
        mission["rewards"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT kind, item_code, display_name, rarity, quantity, multiplier
                FROM external_mission_rewards WHERE external_mission_id=?
                ORDER BY kind, source_ordinal
                """,
                (row["id"],),
            )
        ]
        mission["modifiers"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT modifier_code, display_name, element
                FROM external_mission_modifiers WHERE external_mission_id=?
                ORDER BY source_ordinal
                """,
                (row["id"],),
            )
        ]
        missions.append(mission)
    return {
        "rotation": {
            "id": rotation["id"],
            "provider": rotation["provider_name"],
            "provider_code": rotation["provider_code"],
            "key": rotation["provider_rotation_key"],
            "valid_from": rotation["valid_from"],
            "valid_until": rotation["valid_until"],
            "fetched_at": rotation["fetched_at"],
            "source_timestamp": rotation["source_timestamp"],
            "freshness": freshness,
            "is_fixture": rotation["provider_code"] == "local_fixture",
        },
        "missions": missions,
    }


def current_correlations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rotation_id = latest_rotation_id(connection)
    if rotation_id is None:
        return []
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT mm.status, mm.confidence, mm.method, mm.evidence_json,
                   mn.id AS mission_node_id, mn.mission_uuid,
                   em.id AS external_mission_id, em.provider_mission_key,
                   em.power_level, o.display_name AS objective
            FROM mission_matches mm
            JOIN mission_nodes mn ON mn.id=mm.mission_node_id
            LEFT JOIN external_missions em ON em.id=mm.external_mission_id
            LEFT JOIN objectives o ON o.id=em.objective_id
            WHERE mm.rotation_id=? ORDER BY mn.id, mm.status, em.id
            """,
            (rotation_id,),
        )
    ]


def application_health(connection: sqlite3.Connection) -> dict[str, Any]:
    missions = current_missions(connection)
    return {
        "status": "ok",
        "watchers": watcher_health(connection),
        "provider": missions["rotation"],
    }
