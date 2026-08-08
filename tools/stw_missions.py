#!/usr/bin/env python3
"""Ingest and inspect provider missions and local mission correlations."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from stw_pipeline import connect
from stw_providers import (
    FixtureProvider,
    ingest_provider_rotation,
    latest_rotation_id,
    match_rotation,
)


def _rotation_id(connection: sqlite3.Connection, requested: int | None) -> int:
    value = requested if requested is not None else latest_rotation_id(connection)
    if value is None:
        raise ValueError("no provider rotation is stored")
    return value


def print_rotation(connection: sqlite3.Connection, rotation_id: int) -> None:
    row = connection.execute(
        """
        SELECT mr.*, p.code AS provider_code, p.display_name,
               ps.fetched_at, ps.source_timestamp, ps.payload_sha256,
               COUNT(em.id) AS mission_count
        FROM mission_rotations mr
        JOIN providers p ON p.id=mr.provider_id
        JOIN provider_snapshots ps ON ps.id=mr.snapshot_id
        LEFT JOIN external_missions em ON em.rotation_id=mr.id
        WHERE mr.id=? GROUP BY mr.id
        """,
        (rotation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"rotation {rotation_id} does not exist")
    print(f"Rotation {row['id']}: {row['provider_rotation_key']} ({row['status']})")
    print(f"  Provider: {row['display_name']} [{row['provider_code']}]")
    print(f"  Valid: {row['valid_from']} -> {row['valid_until']}")
    print(f"  Source timestamp: {row['source_timestamp'] or 'not supplied'}")
    print(f"  Fetched: {row['fetched_at']}; missions: {row['mission_count']}")
    print(f"  Snapshot SHA-256: {row['payload_sha256']}")


def print_missions(connection: sqlite3.Connection, rotation_id: int) -> None:
    rows = connection.execute(
        """
        SELECT em.id, em.provider_mission_key, em.theater_name, em.theater_code,
               o.display_name AS objective, em.power_level, em.husk_power_level,
               em.biome_name, em.biome_code, em.is_four_player, em.alert_type,
               (SELECT COUNT(*) FROM external_mission_rewards r
                WHERE r.external_mission_id=em.id) AS reward_count,
               (SELECT COUNT(*) FROM external_mission_modifiers m
                WHERE m.external_mission_id=em.id) AS modifier_count
        FROM external_missions em JOIN objectives o ON o.id=em.objective_id
        WHERE em.rotation_id=? ORDER BY em.theater_code, em.power_level, o.display_name, em.id
        """,
        (rotation_id,),
    ).fetchall()
    if not rows:
        print("No external missions stored for this rotation.")
        return
    print("ID   Theater       Mission                         Biome                 4P  Alert       R/M  Provider key")
    for row in rows:
        theater = row["theater_name"] or row["theater_code"]
        mission = f"PL{row['power_level']} {row['objective']}"
        biome = row["biome_name"] or row["biome_code"] or "unknown"
        print(
            f"{row['id']:<4} {theater:<13} {mission:<31} {biome:<21} "
            f"{'yes' if row['is_four_player'] else 'no ':<3} {row['alert_type'] or 'none':<11} "
            f"{row['reward_count']}/{row['modifier_count']}  "
            f"{row['provider_mission_key'] or '(ordinal only)'}"
        )


def print_mission(connection: sqlite3.Connection, mission_id: int) -> None:
    row = connection.execute(
        """
        SELECT em.*, o.canonical_code AS objective_code, o.display_name AS objective_name,
               mr.valid_from, mr.valid_until
        FROM external_missions em
        JOIN objectives o ON o.id=em.objective_id
        JOIN mission_rotations mr ON mr.id=em.rotation_id
        WHERE em.id=?
        """,
        (mission_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"external mission {mission_id} does not exist")
    print(f"External mission {row['id']}: PL{row['power_level']} {row['objective_name']}")
    print(f"  Theater: {row['theater_name'] or row['theater_code']}")
    print(f"  Provider key: {row['provider_mission_key'] or 'not supplied'}")
    print(f"  Valid: {row['valid_from']} -> {row['valid_until']}")
    print(f"  Husk PL: {row['husk_power_level'] or 'not supplied'}")
    print(f"  Biome: {row['biome_name'] or row['biome_code'] or 'not supplied'}")
    print(f"  Four-player: {'yes' if row['is_four_player'] else 'no'}")
    print(f"  Alert: {row['alert_type'] or 'none'}")
    if row["map_position_json"]:
        print(f"  Map position ({row['map_coordinate_system']}): {row['map_position_json']}")
    rewards = connection.execute(
        """
        SELECT * FROM external_mission_rewards
        WHERE external_mission_id=? ORDER BY kind, source_ordinal
        """,
        (mission_id,),
    ).fetchall()
    print("  Rewards:")
    for reward in rewards:
        amount = reward["quantity"] if reward["quantity"] is not None else "?"
        print(
            f"    [{reward['kind']}] {amount} x {reward['display_name']}"
            f" ({reward['rarity'] or reward['item_code'] or 'unclassified'})"
        )
    if not rewards:
        print("    none supplied")
    modifiers = connection.execute(
        """
        SELECT * FROM external_mission_modifiers
        WHERE external_mission_id=? ORDER BY source_ordinal
        """,
        (mission_id,),
    ).fetchall()
    print("  Modifiers:")
    for modifier in modifiers:
        element = f" [{modifier['element']}]" if modifier["element"] else ""
        print(f"    {modifier['display_name']}{element}")
    if not modifiers:
        print("    none supplied")


def print_matches(
    connection: sqlite3.Connection,
    rotation_id: int,
    status: str | None,
    show_evidence: bool = False,
) -> None:
    rows = connection.execute(
        """
        SELECT mm.status, mm.confidence, mm.method, mn.id AS node_id,
               mn.mission_uuid, mn.theater_uuid, em.id AS external_id,
               em.power_level, o.display_name AS objective, em.biome_name,
               em.provider_mission_key, mm.evidence_json
        FROM mission_matches mm
        JOIN mission_nodes mn ON mn.id=mm.mission_node_id
        LEFT JOIN external_missions em ON em.id=mm.external_mission_id
        LEFT JOIN objectives o ON o.id=em.objective_id
        WHERE mm.rotation_id=? AND (? IS NULL OR mm.status=?)
        ORDER BY mm.status, mn.id, em.id
        """,
        (rotation_id, status, status),
    ).fetchall()
    if not rows:
        print("No mission correlations stored for this selection.")
        return
    print("Status      Confidence Node  External  Friendly mission                  Mission UUID")
    for row in rows:
        friendly = (
            f"PL{row['power_level']} {row['objective']}"
            if row["external_id"] is not None
            else "unresolved"
        )
        print(
            f"{row['status']:<11} {row['confidence']:<10} {row['node_id']:<5} "
            f"{str(row['external_id'] or '-'):<9} {friendly:<33} {row['mission_uuid']}"
        )
        if show_evidence:
            print("  " + json.dumps(json.loads(row["evidence_json"]), sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest-fixture", help="ingest and correlate a local fixture")
    ingest.add_argument("fixture", type=Path)
    rotation = commands.add_parser("rotation", help="show provider rotation provenance")
    rotation.add_argument("--rotation", type=int)
    missions = commands.add_parser("missions", help="list normalized external missions")
    missions.add_argument("--rotation", type=int)
    mission = commands.add_parser("mission", help="show rewards and modifiers for one mission")
    mission.add_argument("mission_id", type=int)
    match = commands.add_parser("match", help="run conservative correlation")
    match.add_argument("--rotation", type=int)
    matches = commands.add_parser("matches", help="list correlation outcomes")
    matches.add_argument("--rotation", type=int)
    matches.add_argument("--status", choices=("accepted", "ambiguous", "unmatched"))
    matches.add_argument("--evidence", action="store_true")
    health = commands.add_parser("health", help="inspect a local fixture's freshness")
    health.add_argument("fixture", type=Path)
    args = parser.parse_args()

    if args.command == "health":
        print(json.dumps(FixtureProvider(args.fixture).health().__dict__, indent=2))
        return 0
    connection = connect(args.db)
    try:
        if args.command == "ingest-fixture":
            ingestion = ingest_provider_rotation(connection, FixtureProvider(args.fixture))
            matching = match_rotation(connection, ingestion["rotation_id"])
            print(json.dumps({"ingestion": ingestion, "matching": matching}, indent=2))
        elif args.command == "rotation":
            print_rotation(connection, _rotation_id(connection, args.rotation))
        elif args.command == "missions":
            print_missions(connection, _rotation_id(connection, args.rotation))
        elif args.command == "mission":
            print_mission(connection, args.mission_id)
        elif args.command == "match":
            print(json.dumps(match_rotation(connection, _rotation_id(connection, args.rotation)), indent=2))
        else:
            print_matches(
                connection,
                _rotation_id(connection, args.rotation),
                args.status,
                args.evidence,
            )
        return 0
    except ValueError as error:
        parser.error(str(error))
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
