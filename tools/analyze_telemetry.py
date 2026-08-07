#!/usr/bin/env python3
"""Extract privacy-conscious STW matchmaking facts from Fortnite client logs."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


TIMESTAMP_RE = re.compile(r"^\[([^]]+)]")
REGISTER_MARKER = "[FMatchmakingClient::Register]"
DIFFICULTY_RE = re.compile(
    r"Snapshot: (?:Waiting to Start|Start of Match) "
    r"\(FortGameStatePvE, Difficulty ([0-9.]+)\)"
)
LOAD_MAP_RE = re.compile(r"LogLoad: LoadMap: (?:[^/\s]+/)?([^?\s]+)")
TEAM_MEMBER_RE = re.compile(
    r"Id \[(?:MCP|EOS):([^]]+)] team member data updated, "
    r"team \[HumanCampaign] at index \[(\d+)]"
)
SESSION_RE = re.compile(r"Session Id \[([0-9a-fA-F]{32})]")
ASSIGNED_MARKER = "Matchmaking Service State Changed From Registered to Assigned"

ATTRIBUTES = {
    "region": "/Fortnite.com/Matchmaking:Region",
    "fill": "/Fortnite.com/Matchmaking:MatchFill",
    "match_type": "/Fortnite.com/Matchmaking:MatchType",
    "mission_id": "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Mission",
    "theater_id": "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Theater",
    "stw_type": "/Epic@Fortnite.com/SaveTheWorld/Matchmaking:Type",
}


def _timestamp(line: str) -> str | None:
    match = TIMESTAMP_RE.match(line)
    return match.group(1) if match else None


def _attribute(line: str, key: str) -> str | None:
    match = re.search(re.escape(key) + r":([^,}]+)", line)
    return match.group(1).strip() if match else None


def _elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    pattern = "%Y.%m.%d-%H.%M.%S:%f"
    return round(
        (datetime.strptime(end, pattern) - datetime.strptime(start, pattern)).total_seconds(),
        3,
    )


def analyze(path: Path) -> dict[str, object]:
    registrations: list[dict[str, object]] = []
    difficulties: list[dict[str, object]] = []
    map_loads: list[dict[str, object]] = []
    session_ids: list[str] = []
    seen_session_ids: set[str] = set()
    team_member_ids: set[str] = set()
    max_team_index: int | None = None
    note: str | None = None
    current_attempt: dict[str, object] | None = None

    with path.open("r", encoding="utf-8", errors="replace") as log:
        for line_number, raw_line in enumerate(log, 1):
            line = raw_line.rstrip("\r\n")
            if line_number == 1 and line.lower().startswith("note"):
                note = line

            if REGISTER_MARKER in line:
                event: dict[str, object] = {
                    "line": line_number,
                    "timestamp": _timestamp(line),
                }
                event.update(
                    {name: _attribute(line, key) for name, key in ATTRIBUTES.items()}
                )
                registrations.append(event)
                current_attempt = event
                event.update(
                    {
                        "assigned_line": None,
                        "assigned_timestamp": None,
                        "assignment_latency_seconds": None,
                        "session_ids": [],
                        "maps": [],
                        "internal_difficulty": None,
                        "observed_team_size": None,
                    }
                )

            if (
                current_attempt is not None
                and ASSIGNED_MARKER in line
                and _timestamp(line) is not None
                and current_attempt["assigned_line"] is None
            ):
                assigned_timestamp = _timestamp(line)
                current_attempt["assigned_line"] = line_number
                current_attempt["assigned_timestamp"] = assigned_timestamp
                current_attempt["assignment_latency_seconds"] = _elapsed_seconds(
                    current_attempt["timestamp"], assigned_timestamp  # type: ignore[arg-type]
                )

            difficulty_match = DIFFICULTY_RE.search(line)
            if difficulty_match and "Waiting to Start" in line:
                difficulty = float(difficulty_match.group(1))
                event = {
                    "line": line_number,
                    "timestamp": _timestamp(line),
                    "internal_difficulty": difficulty,
                }
                difficulties.append(event)
                if current_attempt is not None:
                    current_attempt["internal_difficulty"] = difficulty

            map_match = LOAD_MAP_RE.search(line)
            if map_match:
                map_path = map_match.group(1)
                event = {
                    "line": line_number,
                    "timestamp": _timestamp(line),
                    "map": map_path,
                }
                map_loads.append(event)
                if current_attempt is not None:
                    current_attempt["maps"].append(map_path)  # type: ignore[union-attr]

            team_match = TEAM_MEMBER_RE.search(line)
            if team_match:
                team_member_ids.add(team_match.group(1))
                index = int(team_match.group(2))
                max_team_index = index if max_team_index is None else max(max_team_index, index)
                if current_attempt is not None:
                    size = index + 1
                    previous_size = current_attempt["observed_team_size"]
                    if previous_size is None or size > previous_size:  # type: ignore[operator]
                        current_attempt["observed_team_size"] = size

            for session_match in SESSION_RE.finditer(line):
                session_id = session_match.group(1).lower()
                if session_id not in session_ids:
                    session_ids.append(session_id)
                if session_id not in seen_session_ids:
                    seen_session_ids.add(session_id)
                    if current_attempt is not None:
                        current_attempt["session_ids"].append(session_id)  # type: ignore[union-attr]

    for attempt in registrations:
        if attempt["internal_difficulty"] is not None:
            attempt["outcome"] = "joined"
        elif attempt["assigned_line"] is not None:
            attempt["outcome"] = "assigned_not_joined"
        else:
            attempt["outcome"] = "registered_not_assigned"

    result: dict[str, object] = {
        "file": str(path),
        "attempts": registrations,
        "registrations": registrations,
        "mission_difficulties": difficulties,
        "map_loads": map_loads,
        "distinct_observed_team_members": len(team_member_ids),
        "largest_observed_team_size": None
        if max_team_index is None
        else max_team_index + 1,
        "session_ids": session_ids,
    }
    if note:
        result["note"] = note
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="Fortnite log file(s)")
    args = parser.parse_args()
    print(json.dumps([analyze(path) for path in args.logs], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
