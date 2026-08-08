#!/usr/bin/env python3
"""Extract privacy-conscious STW matchmaking facts from Fortnite client logs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
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
TEAM_ADDED_RE = re.compile(
    r"Id \[(?:MCP|EOS):([^]]+)] added to team \[HumanCampaign] "
    r"at index \[(\d+)]"
)
CLIENT_LEFT_RE = re.compile(r"LogLobbyBeacon: ClientPlayerLeft (?:MCP|EOS):([^\s]+)")
TEAM_REMOVED_RE = re.compile(
    r"Removing \[[^]]*] Id \[(?:MCP|EOS):([^]]+)] from \[[^]]*]'s team"
)
SESSION_RE = re.compile(r"Session Id \[([0-9a-fA-F]{32})]")
ASSIGNED_MARKER = "Matchmaking Service State Changed From Registered to Assigned"
ASSIGNMENT_RE = re.compile(
    r"\[FMatchmakingClient::OnClientMatchAssigned].*?"
    r"ServerAttributes=\{[^}]*?Matchmaking:SubRegion:([^,}]+),\s*"
    r"sessionId:([0-9a-fA-F]{32})"
)
MATCH_ID_RE = re.compile(r"\bMatchId=([^\s]+)")
PARTY_MEMBERS_RE = re.compile(r"PartyMemberAccountIds=(.*?)\s+PlayerAttributes=")
SUBREGION_PINGS_RE = re.compile(r"Matchmaking:SubRegionPings:\{([^}]*)}")
QOS_DATACENTER_RE = re.compile(
    r"\s([A-Z0-9_]+) \(([A-Z0-9_]+)\): (\d+)/(\d+) queries succeeded, "
    r"average ping: (\d+)ms \(adj: (\d+)ms\)"
)
QOS_AVAILABLE_RE = re.compile(r"AutoRegion ([A-Z]+): (\d+) datacenters available")
QOS_RECOMMENDATION_RE = re.compile(
    r"Best region is '([^']+)', recommended subregion is '([^']+)'"
)
LEGACY_STATE_RE = re.compile(r"Matchmaking state change (.+?) -> (.+)$")
SERVICE_STATE_RE = re.compile(
    r"Matchmaking Service State Changed From ([A-Za-z]+) to ([A-Za-z]+)"
)

ATTRIBUTES = {
    "region": "/Fortnite.com/Matchmaking:Region",
    "fill": "/Fortnite.com/Matchmaking:MatchFill",
    "match_type": "/Fortnite.com/Matchmaking:MatchType",
    "build_id": "/Fortnite.com/Matchmaking:BuildId",
    "link_code": "/Fortnite.com/Matchmaking:LinkCode",
    "preferred_link_code_version": "/Fortnite.com/Matchmaking:PreferredLinkCodeVersion",
    "playlist_version": "/Fortnite.com/Matchmaking:PlaylistVersion",
    "platform": "/Fortnite.com/Matchmaking:Platform",
    "input_type": "/Fortnite.com/Matchmaking:InputType",
    "platform_group": "/Fortnite.com/Matchmaking:PlatformGroup",
    "language": "/Fortnite.com/Matchmaking:Language",
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


def _party_size(line: str) -> int | None:
    match = PARTY_MEMBERS_RE.search(line)
    if not match:
        return None
    members = [value.strip(" []") for value in match.group(1).split(",")]
    return len([value for value in members if value])


def _subregion_pings(line: str) -> dict[str, int]:
    match = SUBREGION_PINGS_RE.search(line)
    if not match:
        return {}
    result: dict[str, int] = {}
    for item in match.group(1).split(","):
        key, separator, raw_value = item.partition(":")
        if separator and raw_value.strip().isdigit():
            result[key.strip()] = int(raw_value.strip())
    return result


def _participant_hash(value: str, privacy_salt: bytes | None) -> str | None:
    if privacy_salt is None:
        return None
    return hmac.new(privacy_salt, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _append_state_event(
    events: list[dict[str, object]],
    line: int,
    timestamp: str | None,
    state: str,
    reason: str,
    attempt: dict[str, object] | None,
) -> None:
    if attempt is not None and attempt.get("stw_type") not in (None, "Mission"):
        if reason != "matchmaking_registration":
            return
        state = "Idle"
        reason = "non_mission_session"
        attempt = None
    if events and events[-1]["state"] == state:
        return
    events.append(
        {
            "line": line,
            "timestamp": timestamp,
            "state": state,
            "reason": reason,
            "attempt_line": attempt.get("line") if attempt else None,
        }
    )


def analyze(path: Path, privacy_salt: bytes | None = None) -> dict[str, object]:
    registrations: list[dict[str, object]] = []
    difficulties: list[dict[str, object]] = []
    map_loads: list[dict[str, object]] = []
    session_ids: list[str] = []
    seen_session_ids: set[str] = set()
    assigned_session_ids: set[str] = set()
    team_member_ids: set[str] = set()
    max_team_index: int | None = None
    qos_datacenters: list[dict[str, object]] = []
    qos_available_datacenters: dict[str, int] = {}
    qos_recommendations: list[dict[str, object]] = []
    legacy_session_searches: list[dict[str, object]] = []
    state_events: list[dict[str, object]] = []
    current_legacy_search: dict[str, object] | None = None
    note: str | None = None
    current_attempt: dict[str, object] | None = None
    current_phase = "lobby"
    membership_slots: dict[str, dict[int, str]] = {"lobby": {}, "zone": {}}
    membership_members: dict[str, set[str]] = {"lobby": set(), "zone": set()}
    last_timestamp: str | None = None
    last_line_number = 0

    with path.open("r", encoding="utf-8", errors="replace") as log:
        for line_number, raw_line in enumerate(log, 1):
            last_line_number = line_number
            line = raw_line.rstrip("\r\n")
            if _timestamp(line) is not None:
                last_timestamp = _timestamp(line)
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
                event["party_size"] = _party_size(line)
                event["subregion_pings_ms"] = _subregion_pings(line)
                registrations.append(event)
                current_attempt = event
                event.update(
                    {
                        "assigned_line": None,
                        "assigned_timestamp": None,
                        "assignment_latency_seconds": None,
                        "assigned_subregion": None,
                        "assigned_match_id": None,
                        "assigned_session_id": None,
                        "assigned_session_reused_in_file": None,
                        "session_ids": [],
                        "maps": [],
                        "map_events": [],
                        "internal_difficulty": None,
                        "observed_team_size": None,
                        "team_size_events": [],
                        "membership_events": [],
                        "time_to_first_teammate_seconds": None,
                        "time_to_full_team_seconds": None,
                        "observed_team_size_at_match_start": None,
                        "post_assignment_observation_seconds": None,
                        "largest_team_size_within_seconds": {},
                        "largest_team_size_within_15_seconds": None,
                        "largest_team_size_within_30_seconds": None,
                        "largest_team_size_within_60_seconds": None,
                    }
                )
                current_phase = "lobby"
                membership_slots = {"lobby": {}, "zone": {}}
                membership_members = {"lobby": set(), "zone": set()}
                _append_state_event(
                    state_events,
                    line_number,
                    _timestamp(line),
                    "Registering",
                    "matchmaking_registration",
                    current_attempt,
                )

            service_state_match = SERVICE_STATE_RE.search(line)
            if service_state_match and _timestamp(line) is not None:
                service_state = {
                    "Registering": "Registering",
                    "Registered": "Searching",
                    "Assigned": "Assigned",
                    "Cancelled": "Cancelled",
                    "Canceled": "Cancelled",
                    "Failed": "Failed",
                    "Failure": "Failed",
                }.get(service_state_match.group(2))
                if service_state:
                    _append_state_event(
                        state_events,
                        line_number,
                        _timestamp(line),
                        service_state,
                        f"service_{service_state_match.group(2).lower()}",
                        current_attempt,
                    )

            assignment_match = ASSIGNMENT_RE.search(line)
            if current_attempt is not None and assignment_match:
                assigned_timestamp = _timestamp(line)
                session_id = assignment_match.group(2).lower()
                current_attempt["assigned_line"] = line_number
                current_attempt["assigned_timestamp"] = assigned_timestamp
                current_attempt["assignment_latency_seconds"] = _elapsed_seconds(
                    current_attempt["timestamp"], assigned_timestamp  # type: ignore[arg-type]
                )
                current_attempt["assigned_subregion"] = assignment_match.group(1).strip()
                current_attempt["assigned_session_id"] = session_id
                match_id = MATCH_ID_RE.search(line)
                current_attempt["assigned_match_id"] = (
                    match_id.group(1) if match_id else None
                )
                current_attempt["assigned_session_reused_in_file"] = (
                    session_id in assigned_session_ids
                )
                assigned_session_ids.add(session_id)
                _append_state_event(
                    state_events,
                    line_number,
                    assigned_timestamp,
                    "Assigned",
                    "match_assigned",
                    current_attempt,
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
                    current_attempt["observed_team_size_at_match_start"] = max(
                        current_attempt["party_size"] or 1,  # type: ignore[operator]
                        current_attempt["observed_team_size"] or 1,  # type: ignore[operator]
                    )
                    _append_state_event(
                        state_events,
                        line_number,
                        _timestamp(line),
                        "In Mission",
                        "pve_world_ready",
                        current_attempt,
                    )

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
                    current_attempt["map_events"].append(event)  # type: ignore[union-attr]
                    if map_path != "/Game/Maps/Frontend":
                        current_phase = "zone"
                        _append_state_event(
                            state_events,
                            line_number,
                            _timestamp(line),
                            "Joining",
                            "mission_map_load",
                            current_attempt,
                        )
                    elif current_attempt.get("internal_difficulty") is not None:
                        _append_state_event(
                            state_events,
                            line_number,
                            _timestamp(line),
                            "Idle",
                            "frontend_map_load",
                            current_attempt,
                        )

            team_added_match = TEAM_ADDED_RE.search(line)
            team_match = team_added_match or TEAM_MEMBER_RE.search(line)
            if team_match:
                participant_id = team_match.group(1)
                team_member_ids.add(participant_id)
                index = int(team_match.group(2))
                max_team_index = index if max_team_index is None else max(max_team_index, index)
                if (
                    current_attempt is not None
                    and current_attempt["assigned_timestamp"] is not None
                ):
                    if current_phase == "lobby":
                        _append_state_event(
                            state_events,
                            line_number,
                            _timestamp(line),
                            "In Lobby",
                            "human_campaign_presence",
                            current_attempt,
                        )
                    size = index + 1
                    previous_size = current_attempt["observed_team_size"]
                    if previous_size is None or size > previous_size:  # type: ignore[operator]
                        current_attempt["observed_team_size"] = size
                        timestamp = _timestamp(line)
                        elapsed = _elapsed_seconds(
                            current_attempt["assigned_timestamp"], timestamp  # type: ignore[arg-type]
                        )
                        current_attempt["team_size_events"].append(  # type: ignore[union-attr]
                            {
                                "line": line_number,
                                "timestamp": timestamp,
                                "team_size": size,
                                "seconds_since_assignment": elapsed,
                            }
                        )
                        if (
                            size >= 2
                            and current_attempt["time_to_first_teammate_seconds"] is None
                        ):
                            current_attempt["time_to_first_teammate_seconds"] = elapsed
                        if (
                            size >= 4
                            and current_attempt["time_to_full_team_seconds"] is None
                        ):
                            current_attempt["time_to_full_team_seconds"] = elapsed

                    if index > 0:
                        slots = membership_slots[current_phase]
                        members = membership_members[current_phase]
                        existing_slot = next(
                            (slot for slot, member in slots.items() if member == participant_id),
                            None,
                        )
                        if existing_slot is not None and existing_slot != index:
                            del slots[existing_slot]
                        previous_participant = slots.get(index)
                        is_new_participant = participant_id not in members
                        replaced_participant = None
                        if (
                            is_new_participant
                            and team_added_match
                            and previous_participant is not None
                            and previous_participant in members
                            and len(members) >= 3
                        ):
                            replaced_participant = previous_participant
                            members.remove(previous_participant)
                        slots[index] = participant_id
                        if is_new_participant:
                            members.add(participant_id)
                            current_attempt["membership_events"].append(  # type: ignore[union-attr]
                                {
                                    "line": line_number,
                                    "timestamp": _timestamp(line),
                                    "phase": current_phase,
                                    "event_type": "slot_reused"
                                    if replaced_participant is not None
                                    else ("joined" if team_added_match else "present"),
                                    "participant_hash": _participant_hash(
                                        participant_id, privacy_salt
                                    ),
                                    "replaced_participant_hash": _participant_hash(
                                        replaced_participant, privacy_salt
                                    )
                                    if replaced_participant is not None
                                    else None,
                                    "slot": index,
                                    "team_size_after": 1 + len(members),
                                }
                            )

            left_match = CLIENT_LEFT_RE.search(line) or TEAM_REMOVED_RE.search(line)
            if left_match and current_attempt is not None:
                participant_id = left_match.group(1)
                for phase in dict.fromkeys((current_phase, "lobby", "zone")):
                    slots = membership_slots[phase]
                    members = membership_members[phase]
                    matching_slot = next(
                        (slot for slot, member in slots.items() if member == participant_id),
                        None,
                    )
                    if participant_id not in members:
                        continue
                    members.remove(participant_id)
                    if matching_slot is not None:
                        del slots[matching_slot]
                    current_attempt["membership_events"].append(  # type: ignore[union-attr]
                        {
                            "line": line_number,
                            "timestamp": _timestamp(line),
                            "phase": phase,
                            "event_type": "left",
                            "participant_hash": _participant_hash(
                                participant_id, privacy_salt
                            ),
                            "replaced_participant_hash": None,
                            "slot": matching_slot,
                            "team_size_after": 1 + len(members),
                        }
                    )
                    break

            if (
                current_attempt is not None
                and (
                    "FortPC::ReturnToMainMenu()" in line
                    or "ClientReturnToMainMenuWithTextReason" in line
                )
            ):
                _append_state_event(
                    state_events,
                    line_number,
                    _timestamp(line),
                    "Leaving",
                    "return_to_main_menu",
                    current_attempt,
                )

            qos_match = QOS_DATACENTER_RE.search(line)
            if qos_match:
                qos_datacenters.append(
                    {
                        "line": line_number,
                        "timestamp": _timestamp(line),
                        "subregion": qos_match.group(1),
                        "region": qos_match.group(2),
                        "queries_succeeded": int(qos_match.group(3)),
                        "queries_total": int(qos_match.group(4)),
                        "average_ping_ms": int(qos_match.group(5)),
                        "adjusted_ping_ms": int(qos_match.group(6)),
                    }
                )

            qos_available_match = QOS_AVAILABLE_RE.search(line)
            if qos_available_match:
                qos_available_datacenters[qos_available_match.group(1)] = int(
                    qos_available_match.group(2)
                )

            qos_recommendation_match = QOS_RECOMMENDATION_RE.search(line)
            if qos_recommendation_match:
                qos_recommendations.append(
                    {
                        "line": line_number,
                        "timestamp": _timestamp(line),
                        "best_region": qos_recommendation_match.group(1),
                        "recommended_subregion": qos_recommendation_match.group(2),
                    }
                )

            legacy_state_match = LEGACY_STATE_RE.search(line)
            if legacy_state_match:
                previous_state = legacy_state_match.group(1)
                new_state = legacy_state_match.group(2)
                if new_state == "Finding Existing Session":
                    current_legacy_search = {
                        "line": line_number,
                        "timestamp": _timestamp(line),
                        "outcome": None,
                        "outcome_line": None,
                        "outcome_timestamp": None,
                        "search_latency_seconds": None,
                    }
                    legacy_session_searches.append(current_legacy_search)
                elif (
                    current_legacy_search is not None
                    and previous_state == "Testing Existing Sessions"
                    and new_state
                    in {"No matches available", "Joining Existing Session"}
                ):
                    outcome_timestamp = _timestamp(line)
                    current_legacy_search["outcome"] = (
                        "existing_session_found"
                        if new_state == "Joining Existing Session"
                        else "no_existing_session_found"
                    )
                    current_legacy_search["outcome_line"] = line_number
                    current_legacy_search["outcome_timestamp"] = outcome_timestamp
                    current_legacy_search["search_latency_seconds"] = _elapsed_seconds(
                        current_legacy_search["timestamp"], outcome_timestamp  # type: ignore[arg-type]
                    )
                    current_legacy_search = None

            for session_match in SESSION_RE.finditer(line):
                session_id = session_match.group(1).lower()
                if session_id not in session_ids:
                    session_ids.append(session_id)
                if session_id not in seen_session_ids:
                    seen_session_ids.add(session_id)
                    if current_attempt is not None:
                        current_attempt["session_ids"].append(session_id)  # type: ignore[union-attr]

    for index, attempt in enumerate(registrations):
        observation_end = (
            registrations[index + 1]["timestamp"]
            if index + 1 < len(registrations)
            else last_timestamp
        )
        attempt["post_assignment_observation_seconds"] = _elapsed_seconds(
            attempt["assigned_timestamp"], observation_end  # type: ignore[arg-type]
        )
        attempt["end_timestamp"] = observation_end
        attempt["end_line"] = (
            registrations[index + 1]["line"] - 1
            if index + 1 < len(registrations)
            else last_line_number
        )
        for horizon in (15, 30, 60):
            if (
                attempt["post_assignment_observation_seconds"] is None
                or attempt["post_assignment_observation_seconds"] < horizon  # type: ignore[operator]
            ):
                continue
            sizes = [
                event["team_size"]
                for event in attempt["team_size_events"]  # type: ignore[union-attr]
                if event["seconds_since_assignment"] is not None
                and event["seconds_since_assignment"] <= horizon
            ]
            size = max([attempt["party_size"] or 1, *sizes])  # type: ignore[list-item]
            attempt["largest_team_size_within_seconds"][str(horizon)] = size  # type: ignore[index]
            attempt[f"largest_team_size_within_{horizon}_seconds"] = size

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
        "qos": {
            "available_datacenters_by_region": qos_available_datacenters,
            "datacenter_results": qos_datacenters,
            "recommendations": qos_recommendations,
        },
        "legacy_session_searches": legacy_session_searches,
        "state_events": state_events,
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
