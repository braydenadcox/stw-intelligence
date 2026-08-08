#!/usr/bin/env python3
"""Inspect the evidence-backed local STW mission history."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from stw_pipeline import connect


THEATER_NAMES = {
    "33A2311D4AE64B361CCE27BC9F313C8B": "Stonewood",
    "D477605B4FA48648107B649CE97FCF27": "Plankerton",
    "E6ECBD064B153234656CB4BDE6743870": "Canny Valley",
    "D9A801C5444D1C74D1B7DAB5C7C12C5B": "Twine Peaks",
}


ATTEMPT_QUERY = """
SELECT ma.*, cf.source_path, mn.mission_uuid, mn.theater_uuid,
       mn.rotation_context, r.code AS requested_region,
       dc.code AS datacenter, a.assigned_at, a.assignment_latency_seconds,
       a.match_identifier, ls.session_identifier
FROM mission_attempts AS ma
JOIN capture_files AS cf ON cf.id = ma.capture_id
LEFT JOIN mission_nodes AS mn ON mn.id = ma.mission_node_id
LEFT JOIN regions AS r ON r.id = ma.requested_region_id
LEFT JOIN assignments AS a ON a.attempt_id = ma.id
LEFT JOIN datacenters AS dc ON dc.id = a.datacenter_id
LEFT JOIN lobby_sessions AS ls ON ls.id = a.lobby_session_id
"""


def list_attempts(connection: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        ATTEMPT_QUERY
        + " WHERE ma.stw_type='Mission' ORDER BY ma.started_at DESC, ma.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_attempt(connection: sqlite3.Connection, attempt_id: int) -> sqlite3.Row | None:
    return connection.execute(
        ATTEMPT_QUERY + " WHERE ma.id = ?", (attempt_id,)
    ).fetchone()


def theater_name(uuid: str | None) -> str:
    if not uuid:
        return "unknown theater"
    return THEATER_NAMES.get(uuid.upper(), f"unknown ({uuid[:8]})")


def _mission_label(row: sqlite3.Row) -> str:
    power = f"PL{row['power_level']}" if row["power_level"] else "PL?"
    objective = row["objective_hint"] or "Unknown objective"
    return f"{power} {objective}"


def print_attempts(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No mission attempts stored. Run stw_pipeline.py ingest first.")
        return
    print("ID   Started                  Theater       Mission                    Route       Latency  Session")
    for row in rows:
        route = f"{row['requested_region'] or '?'}->{row['datacenter'] or '?'}"
        latency = (
            f"{row['assignment_latency_seconds']:.2f}s"
            if row["assignment_latency_seconds"] is not None
            else "n/a"
        )
        session = row["session_identifier"] or "n/a"
        print(
            f"{row['id']:<4} {(row['started_at'] or 'unknown'):<24} "
            f"{theater_name(row['theater_uuid']):<13} {_mission_label(row):<26} "
            f"{route:<11} {latency:<8} {session}"
        )


def _participant(value: str | None) -> str:
    return f"teammate-{value[:10]}" if value else "unknown teammate"


def print_attempt(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    print(f"Attempt {row['id']}: {theater_name(row['theater_uuid'])} / {_mission_label(row)}")
    print(f"  Started: {row['started_at'] or 'unknown'}; outcome: {row['outcome']}")
    print(
        f"  Matchmaking: {row['requested_region'] or '?'} -> {row['datacenter'] or '?'}; "
        f"Fill={row['fill_mode'] or '?'}; party={row['party_size'] or '?'}"
    )
    print(f"  Mission node: {row['mission_uuid'] or 'unknown'}")
    print(f"  Theater UUID: {row['theater_uuid'] or 'unknown'}")
    print(f"  Rotation context: {row['rotation_context'] or 'unknown'}")
    print(f"  Lobby session: {row['session_identifier'] or 'not assigned'}")
    print(f"  Match identifier: {row['match_identifier'] or 'not observed'}")
    print(f"  Evidence: {row['source_path']}:{row['source_line_start']}")

    maps = connection.execute(
        "SELECT observed_at, map_path FROM attempt_maps WHERE attempt_id=? ORDER BY source_line",
        (row["id"],),
    ).fetchall()
    print("  Maps:")
    for event in maps:
        print(f"    {event['observed_at'] or 'unknown'}  {event['map_path']}")
    if not maps:
        print("    none observed")

    events = connection.execute(
        """
        SELECT occurred_at, phase, event_type, participant_hash,
               replaced_participant_hash, slot, team_size_after
        FROM membership_events WHERE attempt_id=? ORDER BY source_line, id
        """,
        (row["id"],),
    ).fetchall()
    print("  Membership timeline (pseudonymous):")
    for event in events:
        detail = _participant(event["participant_hash"])
        if event["event_type"] == "slot_reused":
            detail += f" replaced {_participant(event['replaced_participant_hash'])}"
        print(
            f"    {event['occurred_at'] or 'unknown'}  {event['phase']:<5} "
            f"slot={event['slot']} {event['event_type']:<11} {detail}; "
            f"team={event['team_size_after']}"
        )
    if not events:
        print("    none observed")
    else:
        print(f"  Final observed team: {events[-1]['team_size_after']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    attempts = commands.add_parser("attempts", help="list recent mission attempts")
    attempts.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="show one attempt with provenance")
    show.add_argument("attempt_id", type=int)
    args = parser.parse_args()

    connection = connect(args.db)
    try:
        if args.command == "attempts":
            print_attempts(list_attempts(connection, max(1, args.limit)))
            return 0
        row = get_attempt(connection, args.attempt_id)
        if row is None:
            parser.error(f"attempt {args.attempt_id} does not exist")
        print_attempt(connection, row)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
