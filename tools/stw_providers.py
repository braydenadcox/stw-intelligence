#!/usr/bin/env python3
"""Provider-neutral STW mission ingestion and conservative mission correlation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MATCHER_VERSION = "rotation-fields-v1"

THEATER_UUID_CODES = {
    "33A2311D4AE64B361CCE27BC9F313C8B": "stonewood",
    "D477605B4FA48648107B649CE97FCF27": "plankerton",
    "E6ECBD064B153234656CB4BDE6743870": "canny_valley",
    "D9A801C5444D1C74D1B7DAB5C7C12C5B": "twine_peaks",
}

OBJECTIVE_ALIASES = {
    "ride_the_lightning": "ride_the_lightning",
    "retrieve_the_data": "retrieve_the_data",
    "retrieve_data": "retrieve_the_data",
    "repair_the_shelter": "repair_the_shelter",
    "repair_shelter": "repair_the_shelter",
    "rescue_the_survivors": "rescue_the_survivors",
    "rescue_survivors": "rescue_the_survivors",
    "build_the_radar_grid": "build_the_radar_grid",
    "build_radar_grid": "build_the_radar_grid",
    "fight_the_storm": "fight_the_storm",
    "resupply": "resupply",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def canonical_objective(value: str | None) -> str | None:
    if not value:
        return None
    slug = _slug(value)
    return OBJECTIVE_ALIASES.get(slug, slug)


def canonical_theater(value: str | None) -> str | None:
    if not value:
        return None
    uuid_code = THEATER_UUID_CODES.get(value.upper())
    if uuid_code:
        return uuid_code
    aliases = {
        "canny": "canny_valley",
        "twine": "twine_peaks",
        "twine_peaks": "twine_peaks",
    }
    slug = _slug(value)
    return aliases.get(slug, slug)


def canonical_local_biome(map_path: str) -> str | None:
    lower = map_path.lower()
    mappings = (
        ("wildwest", "arid_wild_west"),
        ("thunderroute99", "arid_thunder_route_99"),
        ("ad_lake", "autumn_lake"),
        ("ad_grassland", "autumn_grassland"),
        ("ad_industrial", "autumn_industrial"),
        ("temperate_industrial", "temperate_industrial"),
        ("temperate_suburban", "temperate_suburban"),
        ("tp_island", "twine_island"),
    )
    return next((code for token, code in mappings if token in lower), None)


def _parse_time(value: str) -> datetime:
    if re.match(r"^\d{4}\.\d{2}\.\d{2}-", value):
        return datetime.strptime(value, "%Y.%m.%d-%H.%M.%S:%f").replace(
            tzinfo=timezone.utc
        )
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: str) -> str:
    return _parse_time(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProviderCapabilities:
    code: str
    display_name: str
    adapter_version: str
    terms_url: str | None
    fields: tuple[str, ...]
    map_coordinate_system: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    status: str
    freshness: str
    valid_until: str | None
    detail: str


@dataclass(frozen=True)
class RawProviderSnapshot:
    raw_payload: str
    fetched_at: str
    source_timestamp: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class NormalizedReward:
    kind: str
    display_name: str
    item_code: str | None = None
    rarity: str | None = None
    quantity: float | None = None
    multiplier: float | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class NormalizedModifier:
    display_name: str
    modifier_code: str | None = None
    element: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class NormalizedMission:
    provider_mission_key: str | None
    theater_code: str
    theater_name: str | None
    provider_theater_id: str | None
    objective_code: str
    objective_name: str
    power_level: int
    husk_power_level: int | None
    biome_code: str | None
    biome_name: str | None
    is_four_player: bool
    alert_type: str | None
    map_coordinate_system: str | None
    map_position: dict[str, Any] | None
    source_ordinal: int
    raw_record_reference: str | None
    rewards: tuple[NormalizedReward, ...] = field(default_factory=tuple)
    modifiers: tuple[NormalizedModifier, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NormalizedRotation:
    provider_rotation_key: str
    valid_from: str
    valid_until: str
    source_timestamp: str | None
    missions: tuple[NormalizedMission, ...]


class MissionProvider(ABC):
    @abstractmethod
    def describe(self) -> ProviderCapabilities:
        """Describe stable adapter capabilities without fetching remote data."""

    @abstractmethod
    def fetch_rotation(
        self, now: datetime | None = None, previous_snapshot: RawProviderSnapshot | None = None
    ) -> RawProviderSnapshot:
        """Fetch one immutable raw rotation snapshot."""

    @abstractmethod
    def normalize(self, snapshot: RawProviderSnapshot) -> NormalizedRotation:
        """Translate a raw snapshot into provider-neutral mission records."""

    @abstractmethod
    def health(self, now: datetime | None = None) -> ProviderHealth:
        """Report adapter readability and fixture freshness."""


class FixtureProvider(MissionProvider):
    """Permission-safe provider backed entirely by a local JSON fixture."""

    def __init__(self, path: Path):
        self.path = path

    def _document(self) -> dict[str, Any]:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("fixture root must be an object")
        return value

    def describe(self) -> ProviderCapabilities:
        document = self._document()
        provider = document.get("provider", {})
        return ProviderCapabilities(
            code=str(provider.get("code", "fixture")),
            display_name=str(provider.get("display_name", "Local mission fixture")),
            adapter_version=str(provider.get("adapter_version", "1")),
            terms_url=provider.get("terms_url"),
            fields=(
                "theater", "objective", "power_level", "husk_power_level", "biome",
                "is_four_player", "alert_type", "rewards", "modifiers", "map_position",
            ),
            map_coordinate_system=document.get("map_coordinate_system"),
        )

    def fetch_rotation(
        self, now: datetime | None = None, previous_snapshot: RawProviderSnapshot | None = None
    ) -> RawProviderSnapshot:
        del previous_snapshot
        raw = self.path.read_text(encoding="utf-8")
        document = json.loads(raw)
        rotation = document.get("rotation", {})
        return RawProviderSnapshot(
            raw_payload=raw,
            fetched_at=_now_iso(now),
            source_timestamp=rotation.get("source_timestamp"),
        )

    def normalize(self, snapshot: RawProviderSnapshot) -> NormalizedRotation:
        document = json.loads(snapshot.raw_payload)
        rotation = document.get("rotation")
        missions = document.get("missions")
        if not isinstance(rotation, dict) or not isinstance(missions, list):
            raise ValueError("fixture requires rotation object and missions array")
        valid_from = _iso(str(rotation["valid_from"]))
        valid_until = _iso(str(rotation["valid_until"]))
        if _parse_time(valid_from) >= _parse_time(valid_until):
            raise ValueError("rotation valid_from must precede valid_until")
        normalized: list[NormalizedMission] = []
        for ordinal, record in enumerate(missions):
            if not isinstance(record, dict):
                raise ValueError(f"mission {ordinal} must be an object")
            theater = record.get("theater", {})
            objective = record.get("objective", {})
            if not isinstance(theater, dict) or not isinstance(objective, dict):
                raise ValueError(f"mission {ordinal} theater/objective must be objects")
            objective_name = str(objective.get("name") or objective.get("code") or "unknown")
            objective_code = canonical_objective(str(objective.get("code") or objective_name))
            theater_code = canonical_theater(str(theater.get("code") or theater.get("name") or ""))
            if not objective_code or not theater_code:
                raise ValueError(f"mission {ordinal} requires theater and objective")
            rewards = tuple(
                NormalizedReward(
                    kind=_reward_kind(str(item.get("kind", "other"))),
                    item_code=item.get("item_code"),
                    display_name=str(item["display_name"]),
                    rarity=item.get("rarity"),
                    quantity=_optional_float(item.get("quantity")),
                    multiplier=_optional_float(item.get("multiplier")),
                    image_url=item.get("image_url"),
                )
                for item in record.get("rewards", [])
            )
            modifiers = tuple(
                NormalizedModifier(
                    modifier_code=item.get("modifier_code"),
                    display_name=str(item["display_name"]),
                    element=item.get("element"),
                    image_url=item.get("image_url"),
                )
                for item in record.get("modifiers", [])
            )
            biome = record.get("biome") or {}
            if isinstance(biome, str):
                biome = {"code": biome, "name": biome}
            coordinate_system = record.get("map_coordinate_system") or document.get(
                "map_coordinate_system"
            )
            normalized.append(
                NormalizedMission(
                    provider_mission_key=_optional_text(record.get("provider_mission_key")),
                    theater_code=theater_code,
                    theater_name=_optional_text(theater.get("name")),
                    provider_theater_id=_optional_text(theater.get("provider_id")),
                    objective_code=objective_code,
                    objective_name=objective_name,
                    power_level=int(record["power_level"]),
                    husk_power_level=_optional_int(record.get("husk_power_level")),
                    biome_code=_slug(str(biome["code"])) if biome.get("code") else None,
                    biome_name=_optional_text(biome.get("name")),
                    is_four_player=bool(record.get("is_four_player", False)),
                    alert_type=_optional_text(record.get("alert_type")),
                    map_coordinate_system=_optional_text(coordinate_system),
                    map_position=record.get("map_position"),
                    source_ordinal=ordinal,
                    raw_record_reference=f"$.missions[{ordinal}]",
                    rewards=rewards,
                    modifiers=modifiers,
                )
            )
        return NormalizedRotation(
            provider_rotation_key=str(rotation["key"]),
            valid_from=valid_from,
            valid_until=valid_until,
            source_timestamp=_optional_text(rotation.get("source_timestamp")),
            missions=tuple(normalized),
        )

    def health(self, now: datetime | None = None) -> ProviderHealth:
        try:
            rotation = self.normalize(self.fetch_rotation(now))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            return ProviderHealth("unhealthy", "unknown", None, str(error))
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        freshness = (
            "future" if instant < _parse_time(rotation.valid_from)
            else "current" if instant < _parse_time(rotation.valid_until)
            else "stale"
        )
        return ProviderHealth("healthy", freshness, rotation.valid_until, str(self.path))


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    return float(value) if value is not None else None


def _reward_kind(value: str) -> str:
    kind = _slug(value)
    return kind if kind in {"alert", "repeatable", "base"} else "other"


def ingest_provider_rotation(
    connection: sqlite3.Connection,
    provider: MissionProvider,
    now: datetime | None = None,
) -> dict[str, int]:
    capabilities = provider.describe()
    snapshot = provider.fetch_rotation(now)
    rotation = provider.normalize(snapshot)
    payload_hash = hashlib.sha256(snapshot.raw_payload.encode("utf-8")).hexdigest()
    counters = {
        "providers": 0,
        "snapshots": 0,
        "rotations": 0,
        "missions": 0,
        "rewards": 0,
        "modifiers": 0,
        "rotation_id": 0,
    }
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    status = (
        "future" if instant < _parse_time(rotation.valid_from)
        else "current" if instant < _parse_time(rotation.valid_until)
        else "expired"
    )
    with connection:
        existing_provider = connection.execute(
            "SELECT id FROM providers WHERE code=?", (capabilities.code,)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO providers(code, display_name, adapter_version, terms_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                display_name=excluded.display_name,
                adapter_version=excluded.adapter_version,
                terms_url=excluded.terms_url
            """,
            (
                capabilities.code,
                capabilities.display_name,
                capabilities.adapter_version,
                capabilities.terms_url,
            ),
        )
        provider_id = connection.execute(
            "SELECT id FROM providers WHERE code=?", (capabilities.code,)
        ).fetchone()["id"]
        counters["providers"] = int(existing_provider is None)
        cursor = connection.execute(
            """
            INSERT INTO provider_snapshots(
                provider_id, fetched_at, source_timestamp, etag, last_modified,
                payload_sha256, raw_payload, parse_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed')
            ON CONFLICT(provider_id, payload_sha256) DO NOTHING
            """,
            (
                provider_id,
                snapshot.fetched_at,
                snapshot.source_timestamp or rotation.source_timestamp,
                snapshot.etag,
                snapshot.last_modified,
                payload_hash,
                snapshot.raw_payload,
            ),
        )
        counters["snapshots"] = cursor.rowcount
        snapshot_id = connection.execute(
            "SELECT id FROM provider_snapshots WHERE provider_id=? AND payload_sha256=?",
            (provider_id, payload_hash),
        ).fetchone()["id"]
        existing_rotation = connection.execute(
            "SELECT id FROM mission_rotations WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if existing_rotation is not None:
            counters["rotation_id"] = existing_rotation["id"]
            return counters
        cursor = connection.execute(
            """
            INSERT INTO mission_rotations(
                provider_id, provider_rotation_key, valid_from, valid_until, snapshot_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                rotation.provider_rotation_key,
                rotation.valid_from,
                rotation.valid_until,
                snapshot_id,
                status,
            ),
        )
        rotation_id = cursor.lastrowid
        counters["rotations"] = 1
        counters["rotation_id"] = rotation_id
        for mission in rotation.missions:
            connection.execute(
                """
                INSERT INTO objectives(canonical_code, display_name) VALUES (?, ?)
                ON CONFLICT(canonical_code) DO UPDATE SET display_name=excluded.display_name
                """,
                (mission.objective_code, mission.objective_name),
            )
            objective_id = connection.execute(
                "SELECT id FROM objectives WHERE canonical_code=?", (mission.objective_code,)
            ).fetchone()["id"]
            mission_cursor = connection.execute(
                """
                INSERT INTO external_missions(
                    rotation_id, provider_mission_key, theater_code, theater_name,
                    provider_theater_id, objective_id, power_level, husk_power_level,
                    biome_code, biome_name, is_four_player, alert_type,
                    map_coordinate_system, map_position_json, source_ordinal,
                    raw_record_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rotation_id,
                    mission.provider_mission_key,
                    mission.theater_code,
                    mission.theater_name,
                    mission.provider_theater_id,
                    objective_id,
                    mission.power_level,
                    mission.husk_power_level,
                    mission.biome_code,
                    mission.biome_name,
                    int(mission.is_four_player),
                    mission.alert_type,
                    mission.map_coordinate_system,
                    json.dumps(mission.map_position, sort_keys=True)
                    if mission.map_position is not None
                    else None,
                    mission.source_ordinal,
                    mission.raw_record_reference,
                ),
            )
            external_id = mission_cursor.lastrowid
            counters["missions"] += 1
            for ordinal, reward in enumerate(mission.rewards):
                connection.execute(
                    """
                    INSERT INTO external_mission_rewards(
                        external_mission_id, kind, item_code, display_name, rarity,
                        quantity, multiplier, image_url, source_ordinal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        external_id, reward.kind, reward.item_code, reward.display_name,
                        reward.rarity, reward.quantity, reward.multiplier, reward.image_url,
                        ordinal,
                    ),
                )
                counters["rewards"] += 1
            for ordinal, modifier in enumerate(mission.modifiers):
                connection.execute(
                    """
                    INSERT INTO external_mission_modifiers(
                        external_mission_id, modifier_code, display_name, element,
                        image_url, source_ordinal
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        external_id, modifier.modifier_code, modifier.display_name,
                        modifier.element, modifier.image_url, ordinal,
                    ),
                )
                counters["modifiers"] += 1
    return counters


def match_rotation(connection: sqlite3.Connection, rotation_id: int) -> dict[str, int]:
    rotation = connection.execute(
        "SELECT * FROM mission_rotations WHERE id=?", (rotation_id,)
    ).fetchone()
    if rotation is None:
        raise ValueError(f"rotation {rotation_id} does not exist")
    external = connection.execute(
        """
        SELECT em.*, o.canonical_code AS objective_code
        FROM external_missions em JOIN objectives o ON o.id=em.objective_id
        WHERE em.rotation_id=? ORDER BY em.id
        """,
        (rotation_id,),
    ).fetchall()
    node_ids = {
        row["mission_node_id"]
        for row in connection.execute(
            "SELECT mission_node_id, started_at FROM mission_attempts WHERE mission_node_id IS NOT NULL"
        )
        if row["started_at"]
        and _parse_time(rotation["valid_from"])
        <= _parse_time(row["started_at"])
        < _parse_time(rotation["valid_until"])
    }
    counts = {
        "nodes": len(node_ids),
        "accepted": 0,
        "ambiguous_nodes": 0,
        "ambiguous_candidates": 0,
        "unmatched": 0,
        "changed": 0,
    }
    for node_id in sorted(node_ids):
        node = connection.execute(
            "SELECT * FROM mission_nodes WHERE id=?", (node_id,)
        ).fetchone()
        attempts = [
            row
            for row in connection.execute(
                """
                SELECT ma.id, ma.started_at, ma.power_level, ma.objective_hint,
                       ma.objective_evidence
                FROM mission_attempts ma WHERE ma.mission_node_id=?
                """,
                (node_id,),
            )
            if row["started_at"]
            and _parse_time(rotation["valid_from"])
            <= _parse_time(row["started_at"])
            < _parse_time(rotation["valid_until"])
        ]
        powers = {row["power_level"] for row in attempts if row["power_level"] is not None}
        objectives = {
            canonical_objective(row["objective_hint"])
            for row in attempts
            if row["objective_hint"]
        }
        objective_evidence = {
            row["objective_evidence"] for row in attempts if row["objective_evidence"]
        }
        map_rows = connection.execute(
            """
            SELECT DISTINCT am.map_path FROM attempt_maps am
            JOIN mission_attempts ma ON ma.id=am.attempt_id
            WHERE ma.mission_node_id=? AND am.map_path <> '/Game/Maps/Frontend'
            """,
            (node_id,),
        ).fetchall()
        biomes = {
            biome for row in map_rows if (biome := canonical_local_biome(row["map_path"]))
        }
        theater = canonical_theater(node["theater_uuid"])
        conflicts = []
        if len(powers) > 1:
            conflicts.append("multiple_power_levels")
        if len(objectives) > 1:
            conflicts.append("multiple_objectives")
        if len(biomes) > 1:
            conflicts.append("multiple_biomes")
        power = next(iter(powers), None) if len(powers) == 1 else None
        objective = next(iter(objectives), None) if len(objectives) == 1 else None
        biome = next(iter(biomes), None) if len(biomes) == 1 else None
        stage = list(external)
        stage_counts = {"same_rotation": len(stage)}
        if conflicts or power is None or theater is None:
            stage = []
            stage_counts["usable_local_facts"] = 0
        else:
            stage = [row for row in stage if row["theater_code"] == theater]
            stage_counts["same_theater"] = len(stage)
            stage = [row for row in stage if row["power_level"] == power]
            stage_counts["same_power_level"] = len(stage)
            if objective is not None:
                stage = [row for row in stage if row["objective_code"] == objective]
            stage_counts["same_objective_when_known"] = len(stage)
            if biome is not None:
                stage = [
                    row
                    for row in stage
                    if row["biome_code"] is None or row["biome_code"] == biome
                ]
            stage_counts["compatible_biome_when_available"] = len(stage)
        candidates = stage
        base_evidence = {
            "rotation": {
                "provider_rotation_key": rotation["provider_rotation_key"],
                "valid_from": rotation["valid_from"],
                "valid_until": rotation["valid_until"],
                "local_attempts_in_window": [row["id"] for row in attempts],
            },
            "theater": theater,
            "power_level": power,
            "objective": objective,
            "objective_provenance": sorted(objective_evidence),
            "biome": biome,
            "map_position": "not_used_no_shared_local_coordinate_system",
            "conflicts": conflicts,
            "candidate_count": len(candidates),
            "filter_counts": stage_counts,
        }
        desired: list[tuple[int | None, str, str, str]] = []
        if len(candidates) == 1:
            confidence = "medium" if "capture_filename" in objective_evidence else "high"
            evidence = dict(base_evidence)
            evidence["candidate"] = _candidate_evidence(candidates[0])
            desired.append(
                (
                    candidates[0]["id"],
                    "accepted",
                    confidence,
                    json.dumps(evidence, sort_keys=True),
                )
            )
            counts["accepted"] += 1
        elif candidates:
            counts["ambiguous_nodes"] += 1
            counts["ambiguous_candidates"] += len(candidates)
            for candidate in candidates:
                evidence = dict(base_evidence)
                evidence["candidate"] = _candidate_evidence(candidate)
                desired.append(
                    (candidate["id"], "ambiguous", "low", json.dumps(evidence, sort_keys=True))
                )
        else:
            counts["unmatched"] += 1
            desired.append((None, "unmatched", "none", json.dumps(base_evidence, sort_keys=True)))
        counts["changed"] += _store_match_set(connection, node_id, rotation_id, desired)
    return counts


def _candidate_evidence(candidate: sqlite3.Row) -> dict[str, object]:
    return {
        "external_mission_id": candidate["id"],
        "provider_mission_key": candidate["provider_mission_key"],
        "theater": candidate["theater_code"],
        "power_level": candidate["power_level"],
        "objective": candidate["objective_code"],
        "biome": candidate["biome_code"],
    }


def _store_match_set(
    connection: sqlite3.Connection,
    node_id: int,
    rotation_id: int,
    desired: list[tuple[int | None, str, str, str]],
) -> int:
    existing = [
        (row["external_mission_id"], row["status"], row["confidence"], row["evidence_json"])
        for row in connection.execute(
            """
            SELECT external_mission_id, status, confidence, evidence_json
            FROM mission_matches WHERE mission_node_id=? AND rotation_id=? ORDER BY id
            """,
            (node_id, rotation_id),
        )
    ]
    if sorted(existing, key=str) == sorted(desired, key=str):
        return 0
    with connection:
        connection.execute(
            "DELETE FROM mission_matches WHERE mission_node_id=? AND rotation_id=?",
            (node_id, rotation_id),
        )
        connection.executemany(
            """
            INSERT INTO mission_matches(
                mission_node_id, rotation_id, external_mission_id, method,
                confidence, status, evidence_json, matcher_version
            ) VALUES (?, ?, ?, 'inferred_rotation_fields', ?, ?, ?, ?)
            """,
            [
                (node_id, rotation_id, external_id, confidence, status, evidence, MATCHER_VERSION)
                for external_id, status, confidence, evidence in desired
            ],
        )
    return 1


def latest_rotation_id(connection: sqlite3.Connection) -> int | None:
    now = _now_iso()
    row = connection.execute(
        """
        SELECT id FROM mission_rotations
        ORDER BY CASE WHEN valid_from <= ? AND valid_until > ? THEN 0 ELSE 1 END,
                 valid_from DESC, id DESC
        LIMIT 1
        """,
        (now, now),
    ).fetchone()
    return row["id"] if row else None
