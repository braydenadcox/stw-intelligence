#!/usr/bin/env python3
"""Persist STW observations and report measured global and regional activity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from analyze_telemetry import analyze


API_ROOT = "https://api.fortnite.com/ecosystem/v1/islands/campaign/metrics"
INTERVAL_PATHS = {"day": "", "hour": "/hour", "minute": "/minute"}
TEAM_CAPACITY = 4


MIGRATIONS = [
    """
    CREATE TABLE capture_files (
        id INTEGER PRIMARY KEY,
        content_sha256 TEXT NOT NULL UNIQUE,
        source_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        modified_ns INTEGER NOT NULL,
        attempt_count INTEGER NOT NULL,
        first_ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE capture_sources (
        capture_id INTEGER NOT NULL REFERENCES capture_files(id),
        source_path TEXT NOT NULL,
        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (capture_id, source_path)
    );
    CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE regions (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        display_name TEXT
    );
    CREATE TABLE datacenters (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        display_name TEXT
    );
    CREATE TABLE datacenter_region_observations (
        datacenter_id INTEGER NOT NULL REFERENCES datacenters(id),
        region_id INTEGER NOT NULL REFERENCES regions(id),
        capture_id INTEGER NOT NULL REFERENCES capture_files(id),
        source_line INTEGER NOT NULL,
        observed_at TEXT,
        evidence_type TEXT NOT NULL,
        PRIMARY KEY (datacenter_id, region_id, capture_id, source_line, evidence_type)
    );
    CREATE TABLE mission_nodes (
        id INTEGER PRIMARY KEY,
        theater_uuid TEXT NOT NULL,
        mission_uuid TEXT NOT NULL,
        rotation_context TEXT NOT NULL,
        rotation_context_evidence TEXT NOT NULL,
        observed_rotation_start TEXT,
        observed_rotation_end TEXT,
        first_seen_at TEXT,
        last_seen_at TEXT,
        UNIQUE (theater_uuid, mission_uuid, rotation_context)
    );
    CREATE TABLE lobby_sessions (
        id INTEGER PRIMARY KEY,
        session_identifier TEXT NOT NULL UNIQUE,
        first_seen_at TEXT,
        last_seen_at TEXT
    );
    CREATE TABLE mission_attempts (
        id INTEGER PRIMARY KEY,
        capture_id INTEGER NOT NULL REFERENCES capture_files(id),
        source_attempt_index INTEGER NOT NULL,
        source_line_start INTEGER NOT NULL,
        source_line_end INTEGER,
        mission_node_id INTEGER REFERENCES mission_nodes(id),
        requested_region_id INTEGER REFERENCES regions(id),
        stw_type TEXT,
        fill_mode TEXT,
        party_size INTEGER,
        started_at TEXT,
        ended_at TEXT,
        outcome TEXT NOT NULL,
        observation_seconds REAL,
        build_id TEXT,
        link_code TEXT,
        platform TEXT,
        input_type TEXT,
        objective_hint TEXT,
        objective_evidence TEXT,
        internal_difficulty REAL,
        power_level INTEGER,
        team_size_at_start INTEGER,
        team_size_15s INTEGER,
        team_size_30s INTEGER,
        team_size_60s INTEGER,
        first_teammate_seconds REAL,
        full_team_seconds REAL,
        UNIQUE (capture_id, source_attempt_index),
        UNIQUE (capture_id, source_line_start)
    );
    CREATE TABLE assignments (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL UNIQUE REFERENCES mission_attempts(id),
        source_line INTEGER NOT NULL,
        assigned_at TEXT,
        assignment_latency_seconds REAL,
        datacenter_id INTEGER REFERENCES datacenters(id),
        lobby_session_id INTEGER REFERENCES lobby_sessions(id),
        match_identifier TEXT
    );
    CREATE TABLE attempt_maps (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL REFERENCES mission_attempts(id),
        source_line INTEGER NOT NULL,
        observed_at TEXT,
        map_path TEXT NOT NULL,
        UNIQUE (attempt_id, source_line, map_path)
    );
    CREATE TABLE membership_events (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL REFERENCES mission_attempts(id),
        lobby_session_id INTEGER REFERENCES lobby_sessions(id),
        source_line INTEGER NOT NULL,
        occurred_at TEXT,
        phase TEXT NOT NULL CHECK (phase IN ('lobby', 'zone')),
        event_type TEXT NOT NULL CHECK (event_type IN ('joined', 'present', 'left', 'slot_reused')),
        participant_hash TEXT,
        replaced_participant_hash TEXT,
        slot INTEGER,
        team_size_after INTEGER,
        UNIQUE (attempt_id, source_line, event_type, slot)
    );
    CREATE TABLE IF NOT EXISTS global_metrics (
        interval TEXT NOT NULL,
        metric TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        value REAL,
        fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (interval, metric, timestamp)
    );
    CREATE INDEX mission_nodes_lookup_idx ON mission_nodes (
        theater_uuid, mission_uuid, observed_rotation_start
    );
    CREATE INDEX attempts_history_idx ON mission_attempts (started_at DESC);
    CREATE INDEX attempts_cohort_idx ON mission_attempts (
        mission_node_id, internal_difficulty, fill_mode, party_size, requested_region_id
    );
    CREATE INDEX membership_timeline_idx ON membership_events (attempt_id, occurred_at, source_line);
    """,
    """
    CREATE TABLE providers (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        terms_url TEXT,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
    );
    CREATE TABLE provider_snapshots (
        id INTEGER PRIMARY KEY,
        provider_id INTEGER NOT NULL REFERENCES providers(id),
        fetched_at TEXT NOT NULL,
        source_timestamp TEXT,
        etag TEXT,
        last_modified TEXT,
        payload_sha256 TEXT NOT NULL,
        raw_payload TEXT NOT NULL,
        parse_status TEXT NOT NULL CHECK (parse_status IN ('parsed', 'failed')),
        parse_error TEXT,
        UNIQUE (provider_id, payload_sha256)
    );
    CREATE TABLE mission_rotations (
        id INTEGER PRIMARY KEY,
        provider_id INTEGER NOT NULL REFERENCES providers(id),
        provider_rotation_key TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT NOT NULL,
        snapshot_id INTEGER NOT NULL UNIQUE REFERENCES provider_snapshots(id),
        status TEXT NOT NULL CHECK (status IN ('current', 'expired', 'future')),
        CHECK (valid_from < valid_until)
    );
    CREATE TABLE objectives (
        id INTEGER PRIMARY KEY,
        canonical_code TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL
    );
    CREATE TABLE external_missions (
        id INTEGER PRIMARY KEY,
        rotation_id INTEGER NOT NULL REFERENCES mission_rotations(id),
        provider_mission_key TEXT,
        theater_code TEXT NOT NULL,
        theater_name TEXT,
        provider_theater_id TEXT,
        objective_id INTEGER NOT NULL REFERENCES objectives(id),
        power_level INTEGER NOT NULL,
        husk_power_level INTEGER,
        biome_code TEXT,
        biome_name TEXT,
        is_four_player INTEGER NOT NULL CHECK (is_four_player IN (0, 1)),
        alert_type TEXT,
        map_coordinate_system TEXT,
        map_position_json TEXT,
        source_ordinal INTEGER NOT NULL,
        raw_record_reference TEXT,
        UNIQUE (rotation_id, source_ordinal),
        UNIQUE (rotation_id, provider_mission_key)
    );
    CREATE TABLE external_mission_rewards (
        id INTEGER PRIMARY KEY,
        external_mission_id INTEGER NOT NULL REFERENCES external_missions(id),
        kind TEXT NOT NULL CHECK (kind IN ('alert', 'repeatable', 'base', 'other')),
        item_code TEXT,
        display_name TEXT NOT NULL,
        rarity TEXT,
        quantity REAL,
        multiplier REAL,
        image_url TEXT,
        source_ordinal INTEGER NOT NULL,
        UNIQUE (external_mission_id, kind, source_ordinal)
    );
    CREATE TABLE external_mission_modifiers (
        id INTEGER PRIMARY KEY,
        external_mission_id INTEGER NOT NULL REFERENCES external_missions(id),
        modifier_code TEXT,
        display_name TEXT NOT NULL,
        element TEXT,
        image_url TEXT,
        source_ordinal INTEGER NOT NULL,
        UNIQUE (external_mission_id, source_ordinal)
    );
    CREATE TABLE mission_matches (
        id INTEGER PRIMARY KEY,
        mission_node_id INTEGER NOT NULL REFERENCES mission_nodes(id),
        rotation_id INTEGER NOT NULL REFERENCES mission_rotations(id),
        external_mission_id INTEGER REFERENCES external_missions(id),
        method TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'none')),
        status TEXT NOT NULL CHECK (status IN ('accepted', 'ambiguous', 'unmatched')),
        evidence_json TEXT NOT NULL,
        matched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        matcher_version TEXT NOT NULL,
        UNIQUE (mission_node_id, rotation_id, external_mission_id, status)
    );
    CREATE UNIQUE INDEX mission_matches_one_accepted_idx
        ON mission_matches(mission_node_id, rotation_id) WHERE status = 'accepted';
    CREATE UNIQUE INDEX mission_matches_one_unmatched_idx
        ON mission_matches(mission_node_id, rotation_id) WHERE status = 'unmatched';
    CREATE INDEX external_missions_filter_idx ON external_missions (
        rotation_id, theater_code, power_level, objective_id, biome_code
    );
    CREATE INDEX mission_matches_status_idx ON mission_matches (rotation_id, status);
    """,
    """
    CREATE TABLE log_watchers (
        id INTEGER PRIMARY KEY,
        source_path TEXT NOT NULL UNIQUE,
        file_identity TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        byte_offset INTEGER NOT NULL DEFAULT 0,
        partial_bytes BLOB NOT NULL DEFAULT X'',
        tail_bytes BLOB NOT NULL DEFAULT X'',
        spool_path TEXT NOT NULL,
        spool_size INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'starting'
            CHECK (status IN ('starting', 'watching', 'missing', 'error', 'stopped')),
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        checkpoint_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_event_at TEXT,
        last_error TEXT
    );
    CREATE TABLE live_watch_generations (
        watcher_id INTEGER NOT NULL REFERENCES log_watchers(id),
        generation INTEGER NOT NULL,
        capture_id INTEGER REFERENCES capture_files(id),
        file_identity TEXT,
        spool_path TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT,
        PRIMARY KEY (watcher_id, generation),
        UNIQUE (capture_id)
    );
    CREATE TABLE live_states (
        watcher_id INTEGER PRIMARY KEY REFERENCES log_watchers(id),
        generation INTEGER NOT NULL,
        state TEXT NOT NULL,
        attempt_id INTEGER REFERENCES mission_attempts(id),
        occurred_at TEXT,
        source_line INTEGER,
        reason TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE live_state_events (
        id INTEGER PRIMARY KEY,
        watcher_id INTEGER NOT NULL REFERENCES log_watchers(id),
        generation INTEGER NOT NULL,
        attempt_id INTEGER REFERENCES mission_attempts(id),
        occurred_at TEXT,
        source_line INTEGER NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL,
        UNIQUE (watcher_id, generation, source_line, state, reason)
    );
    CREATE INDEX live_state_events_timeline_idx
        ON live_state_events(watcher_id, generation, source_line, id);
    """,
    """
    CREATE TABLE attempt_activity_scores (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL REFERENCES mission_attempts(id),
        score_version TEXT NOT NULL,
        window_seconds INTEGER NOT NULL,
        score REAL NOT NULL,
        arrival_score REAL NOT NULL,
        concurrency_score REAL NOT NULL,
        breadth_score REAL NOT NULL,
        retention_score REAL NOT NULL,
        assignment_score REAL NOT NULL,
        max_concurrent_remote INTEGER NOT NULL,
        unique_remote INTEGER NOT NULL,
        retained_teammate_seconds REAL NOT NULL,
        possible_teammate_seconds REAL NOT NULL,
        evidence_json TEXT NOT NULL,
        calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (attempt_id, score_version, window_seconds)
    );
    CREATE TABLE regional_activity (
        id INTEGER PRIMARY KEY,
        mission_node_id INTEGER NOT NULL REFERENCES mission_nodes(id),
        external_mission_id INTEGER REFERENCES external_missions(id),
        region_id INTEGER NOT NULL REFERENCES regions(id),
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        score_version TEXT NOT NULL,
        window_seconds INTEGER NOT NULL,
        score REAL NOT NULL,
        arrival_score REAL NOT NULL,
        concurrency_score REAL NOT NULL,
        breadth_score REAL NOT NULL,
        retention_score REAL NOT NULL,
        assignment_score REAL NOT NULL,
        sample_count INTEGER NOT NULL,
        effective_sample_size REAL NOT NULL,
        latest_sample_at TEXT NOT NULL,
        coverage REAL NOT NULL,
        confidence_band TEXT NOT NULL
            CHECK (confidence_band IN ('insufficient', 'low', 'moderate', 'higher')),
        median_assignment_latency_seconds REAL,
        assignment_join_completion_rate REAL NOT NULL,
        evidence_json TEXT NOT NULL,
        calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (mission_node_id, region_id, score_version, window_seconds)
    );
    CREATE INDEX regional_activity_lookup_idx
        ON regional_activity(mission_node_id, score_version, score DESC);
    """,
    """
    ALTER TABLE regional_activity ADD COLUMN coverage_15 REAL NOT NULL DEFAULT 0;
    ALTER TABLE regional_activity ADD COLUMN coverage_30 REAL NOT NULL DEFAULT 0;
    ALTER TABLE regional_activity ADD COLUMN coverage_60 REAL NOT NULL DEFAULT 0;
    """,
    """
    CREATE TABLE mission_cohorts (
        id INTEGER PRIMARY KEY,
        cohort_version TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        theater_code TEXT NOT NULL,
        objective_code TEXT NOT NULL,
        power_level INTEGER NOT NULL,
        is_four_player INTEGER NOT NULL CHECK (is_four_player IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (cohort_version, identity_key),
        UNIQUE (
            cohort_version, theater_code, objective_code,
            power_level, is_four_player
        )
    );
    CREATE TABLE mission_cohort_memberships (
        id INTEGER PRIMARY KEY,
        mission_node_id INTEGER NOT NULL REFERENCES mission_nodes(id),
        cohort_id INTEGER REFERENCES mission_cohorts(id),
        cohort_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('included', 'excluded', 'conflict')),
        mission_match_id INTEGER REFERENCES mission_matches(id) ON DELETE SET NULL,
        external_mission_id INTEGER REFERENCES external_missions(id),
        evidence_json TEXT NOT NULL,
        evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (mission_node_id, cohort_version),
        CHECK (
            (status = 'included' AND cohort_id IS NOT NULL)
            OR (status <> 'included' AND cohort_id IS NULL)
        )
    );
    CREATE INDEX mission_cohort_memberships_lookup_idx
        ON mission_cohort_memberships(cohort_id, status, mission_node_id);
    """,
    """
    CREATE TABLE game_builds (
        id INTEGER PRIMARY KEY,
        build_key TEXT NOT NULL UNIQUE,
        game_version TEXT,
        changelist TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE asset_snapshots (
        id INTEGER PRIMARY KEY,
        game_build_id INTEGER NOT NULL REFERENCES game_builds(id),
        source_root TEXT NOT NULL,
        exporter_name TEXT NOT NULL,
        exporter_version TEXT,
        manifest_sha256 TEXT NOT NULL,
        file_count INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ingesting', 'ready', 'failed')),
        error_text TEXT,
        ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (game_build_id, manifest_sha256)
    );
    CREATE TABLE asset_files (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        relative_path TEXT NOT NULL,
        source_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        UNIQUE (snapshot_id, relative_path)
    );
    CREATE TABLE asset_objects (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        asset_file_id INTEGER NOT NULL REFERENCES asset_files(id) ON DELETE CASCADE,
        export_index INTEGER NOT NULL,
        package_path TEXT,
        object_name TEXT NOT NULL,
        object_type TEXT NOT NULL,
        class_path TEXT,
        object_key TEXT NOT NULL,
        UNIQUE (asset_file_id, export_index),
        UNIQUE (snapshot_id, object_key)
    );
    CREATE TABLE asset_references (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        property_path TEXT NOT NULL,
        target_path TEXT NOT NULL,
        target_package_path TEXT NOT NULL,
        target_object_id INTEGER REFERENCES asset_objects(id),
        resolution_status TEXT NOT NULL
            CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous')),
        UNIQUE (source_object_id, property_path, target_path)
    );
    CREATE INDEX asset_references_target_idx
        ON asset_references(snapshot_id, target_package_path, resolution_status);

    CREATE TABLE catalog_curve_tables (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        package_path TEXT NOT NULL,
        table_name TEXT NOT NULL
    );
    CREATE TABLE catalog_curve_rows (
        id INTEGER PRIMARY KEY,
        curve_table_id INTEGER NOT NULL REFERENCES catalog_curve_tables(id) ON DELETE CASCADE,
        row_name TEXT NOT NULL,
        UNIQUE (curve_table_id, row_name)
    );
    CREATE TABLE catalog_curve_points (
        id INTEGER PRIMARY KEY,
        curve_row_id INTEGER NOT NULL REFERENCES catalog_curve_rows(id) ON DELETE CASCADE,
        point_ordinal INTEGER NOT NULL,
        time_value REAL NOT NULL,
        output_value REAL NOT NULL,
        interpolation TEXT,
        UNIQUE (curve_row_id, point_ordinal)
    );
    CREATE TABLE catalog_gameplay_effects (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        package_path TEXT NOT NULL,
        effect_name TEXT NOT NULL,
        template_path TEXT,
        stacking_type TEXT,
        stack_limit INTEGER
    );
    CREATE TABLE catalog_effect_modifiers (
        id INTEGER PRIMARY KEY,
        gameplay_effect_id INTEGER NOT NULL
            REFERENCES catalog_gameplay_effects(id) ON DELETE CASCADE,
        modifier_ordinal INTEGER NOT NULL,
        attribute_name TEXT,
        modifier_operation TEXT,
        magnitude_kind TEXT,
        literal_value REAL,
        curve_table_path TEXT,
        curve_row_name TEXT,
        curve_row_id INTEGER REFERENCES catalog_curve_rows(id),
        source_required_tags_json TEXT NOT NULL DEFAULT '[]',
        source_ignored_tags_json TEXT NOT NULL DEFAULT '[]',
        target_required_tags_json TEXT NOT NULL DEFAULT '[]',
        target_ignored_tags_json TEXT NOT NULL DEFAULT '[]',
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial', 'unsupported')),
        UNIQUE (gameplay_effect_id, modifier_ordinal)
    );
    CREATE TABLE catalog_ability_kits (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        package_path TEXT NOT NULL,
        kit_name TEXT NOT NULL
    );
    CREATE TABLE catalog_ability_kit_grants (
        id INTEGER PRIMARY KEY,
        ability_kit_id INTEGER NOT NULL REFERENCES catalog_ability_kits(id) ON DELETE CASCADE,
        source_reference_id INTEGER NOT NULL UNIQUE REFERENCES asset_references(id),
        grant_kind TEXT NOT NULL CHECK (grant_kind IN ('gameplay_effect', 'ability', 'reference')),
        target_path TEXT NOT NULL,
        gameplay_effect_id INTEGER REFERENCES catalog_gameplay_effects(id)
    );
    CREATE TABLE catalog_heroes (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        hero_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        hero_class TEXT,
        statline_tags_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (snapshot_id, hero_key)
    );
    CREATE TABLE catalog_hero_variants (
        id INTEGER PRIMARY KEY,
        hero_id INTEGER NOT NULL REFERENCES catalog_heroes(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        variant_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        rarity TEXT,
        tier TEXT,
        attribute_init_key TEXT,
        UNIQUE (hero_id, variant_key)
    );
    CREATE TABLE catalog_hero_abilities (
        id INTEGER PRIMARY KEY,
        hero_id INTEGER NOT NULL REFERENCES catalog_heroes(id) ON DELETE CASCADE,
        ability_ordinal INTEGER NOT NULL,
        ability_kit_path TEXT NOT NULL,
        ability_kit_id INTEGER REFERENCES catalog_ability_kits(id),
        minimum_rarity TEXT,
        UNIQUE (hero_id, ability_ordinal)
    );
    CREATE TABLE catalog_perks (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        perk_family TEXT NOT NULL,
        perk_tier TEXT NOT NULL,
        ability_kit_path TEXT NOT NULL,
        ability_kit_id INTEGER REFERENCES catalog_ability_kits(id),
        UNIQUE (snapshot_id, perk_family, perk_tier)
    );
    CREATE TABLE catalog_hero_perks (
        id INTEGER PRIMARY KEY,
        hero_id INTEGER NOT NULL REFERENCES catalog_heroes(id) ON DELETE CASCADE,
        perk_id INTEGER NOT NULL REFERENCES catalog_perks(id) ON DELETE CASCADE,
        perk_mode TEXT NOT NULL CHECK (perk_mode IN ('support', 'commander')),
        UNIQUE (hero_id, perk_mode)
    );
    CREATE INDEX catalog_hero_name_idx ON catalog_heroes(snapshot_id, display_name);
    """,
    """
    CREATE TABLE catalog_hero_classes (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        class_key TEXT NOT NULL,
        display_name TEXT,
        package_path TEXT NOT NULL,
        UNIQUE (snapshot_id, class_key)
    );
    CREATE TABLE asset_normalization_runs (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        normalizer_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'ready', 'failed')),
        error_text TEXT,
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        UNIQUE (snapshot_id, normalizer_version)
    );
    ALTER TABLE catalog_heroes ADD COLUMN hero_class_path TEXT;
    ALTER TABLE catalog_heroes ADD COLUMN hero_class_id INTEGER
        REFERENCES catalog_hero_classes(id);

    CREATE TABLE catalog_abilities (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        ability_key TEXT NOT NULL,
        display_name TEXT,
        package_path TEXT NOT NULL,
        semantic_status TEXT NOT NULL DEFAULT 'partial'
            CHECK (semantic_status IN ('supported', 'partial', 'opaque')),
        UNIQUE (snapshot_id, ability_key)
    );
    ALTER TABLE catalog_ability_kit_grants ADD COLUMN ability_id INTEGER
        REFERENCES catalog_abilities(id);
    ALTER TABLE catalog_perks ADD COLUMN perk_key TEXT;
    ALTER TABLE catalog_perks ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'structured_identifier'
        CHECK (identity_status IN ('structured_identifier', 'explicit_unparsed'));
    ALTER TABLE catalog_perks ADD COLUMN source_reference_id INTEGER
        REFERENCES asset_references(id);

    CREATE TABLE catalog_inheritance_edges (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        source_reference_id INTEGER NOT NULL UNIQUE REFERENCES asset_references(id),
        relation TEXT NOT NULL CHECK (relation IN ('template', 'super', 'archetype')),
        target_path TEXT NOT NULL,
        target_object_id INTEGER REFERENCES asset_objects(id),
        resolution_status TEXT NOT NULL
            CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous'))
    );

    CREATE TABLE catalog_gameplay_tags (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        tag_name TEXT NOT NULL,
        UNIQUE (snapshot_id, tag_name)
    );
    CREATE TABLE catalog_gameplay_tag_occurrences (
        id INTEGER PRIMARY KEY,
        tag_id INTEGER NOT NULL REFERENCES catalog_gameplay_tags(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        property_path TEXT NOT NULL,
        semantic_role TEXT NOT NULL,
        UNIQUE (tag_id, source_object_id, property_path, semantic_role)
    );
    CREATE INDEX catalog_gameplay_tag_lookup_idx
        ON catalog_gameplay_tags(snapshot_id, tag_name);

    CREATE TABLE catalog_magnitudes (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        property_path TEXT NOT NULL,
        purpose TEXT NOT NULL,
        calculation_type TEXT,
        literal_value REAL,
        coefficient REAL,
        pre_additive REAL,
        post_additive REAL,
        curve_table_path TEXT,
        curve_row_name TEXT,
        curve_row_id INTEGER REFERENCES catalog_curve_rows(id),
        custom_calculation_path TEXT,
        set_by_caller_tag TEXT,
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial', 'opaque')),
        shape_json TEXT NOT NULL,
        UNIQUE (source_object_id, property_path, purpose)
    );
    ALTER TABLE catalog_effect_modifiers ADD COLUMN magnitude_id INTEGER
        REFERENCES catalog_magnitudes(id);
    ALTER TABLE catalog_effect_modifiers ADD COLUMN evaluation_channel TEXT;

    CREATE TABLE catalog_mechanics (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        owner_domain TEXT NOT NULL
            CHECK (owner_domain IN ('gameplay_effect', 'ability', 'ability_kit', 'hero_class', 'other')),
        owner_id INTEGER,
        mechanic_type TEXT NOT NULL,
        property_path TEXT NOT NULL,
        magnitude_id INTEGER REFERENCES catalog_magnitudes(id),
        conditions_json TEXT NOT NULL DEFAULT '{}',
        value_json TEXT NOT NULL DEFAULT '{}',
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial', 'opaque')),
        UNIQUE (source_object_id, property_path, mechanic_type)
    );
    CREATE TABLE catalog_opaque_mechanics (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL REFERENCES asset_objects(id) ON DELETE CASCADE,
        property_path TEXT NOT NULL,
        mechanic_kind TEXT NOT NULL,
        referenced_path TEXT,
        reason TEXT NOT NULL,
        UNIQUE (source_object_id, property_path, mechanic_kind)
    );
    CREATE INDEX catalog_opaque_mechanics_snapshot_idx
        ON catalog_opaque_mechanics(snapshot_id, mechanic_kind);
    """,
    """
    ALTER TABLE catalog_ability_kits ADD COLUMN display_name TEXT;
    """,
    """
    CREATE TABLE catalog_ability_links (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_ability_id INTEGER NOT NULL REFERENCES catalog_abilities(id) ON DELETE CASCADE,
        source_reference_id INTEGER NOT NULL UNIQUE REFERENCES asset_references(id),
        relation TEXT NOT NULL CHECK (relation IN ('gameplay_ability')),
        target_path TEXT NOT NULL,
        target_ability_id INTEGER REFERENCES catalog_abilities(id),
        resolution_status TEXT NOT NULL
            CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous'))
    );
    CREATE INDEX catalog_ability_links_source_idx
        ON catalog_ability_links(source_ability_id);

    CREATE TABLE catalog_data_tables (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        package_path TEXT NOT NULL,
        table_name TEXT NOT NULL
    );
    CREATE TABLE catalog_data_rows (
        id INTEGER PRIMARY KEY,
        data_table_id INTEGER NOT NULL REFERENCES catalog_data_tables(id) ON DELETE CASCADE,
        row_name TEXT NOT NULL,
        row_json TEXT NOT NULL,
        UNIQUE (data_table_id, row_name)
    );
    """,
    """
    CREATE TABLE catalog_hero_class_kits (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        hero_class_id INTEGER NOT NULL REFERENCES catalog_hero_classes(id) ON DELETE CASCADE,
        kit_ordinal INTEGER NOT NULL,
        source_reference_id INTEGER NOT NULL UNIQUE REFERENCES asset_references(id),
        ability_kit_path TEXT NOT NULL,
        ability_kit_id INTEGER REFERENCES catalog_ability_kits(id),
        UNIQUE (hero_class_id, kit_ordinal)
    );
    """,
    """
    CREATE TABLE asset_roster_export_receipts (
        snapshot_id INTEGER PRIMARY KEY
            REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        plan_version TEXT NOT NULL,
        scopes_json TEXT NOT NULL,
        attestation_text TEXT NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE catalog_weapon_identities (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        identity_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT,
        weapon_kind TEXT NOT NULL CHECK (weapon_kind IN ('ranged', 'melee')),
        identity_evidence TEXT NOT NULL
            CHECK (identity_evidence IN ('localized_display_and_kind', 'variant_only')),
        UNIQUE (snapshot_id, identity_key)
    );
    CREATE TABLE catalog_weapon_slot_loadouts (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        loadout_row_name TEXT NOT NULL,
        source_data_row_id INTEGER REFERENCES catalog_data_rows(id),
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial')),
        UNIQUE (snapshot_id, loadout_row_name)
    );
    CREATE TABLE catalog_weapon_variants (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        identity_id INTEGER NOT NULL
            REFERENCES catalog_weapon_identities(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        variant_key TEXT NOT NULL,
        primary_asset_name TEXT NOT NULL,
        package_path TEXT NOT NULL,
        object_type TEXT NOT NULL,
        rarity TEXT,
        tier TEXT,
        max_tier TEXT,
        display_tier TEXT,
        trigger_type TEXT,
        weapon_actor_path TEXT,
        stat_table_path TEXT,
        stat_row_name TEXT,
        stat_data_row_id INTEGER REFERENCES catalog_data_rows(id),
        slot_loadout_row TEXT,
        slot_loadout_id INTEGER REFERENCES catalog_weapon_slot_loadouts(id),
        baseline_slot_loadout_row TEXT,
        baseline_slot_loadout_id INTEGER REFERENCES catalog_weapon_slot_loadouts(id),
        base_alteration_path TEXT,
        primary_fire_ability_path TEXT,
        ammo_data_path TEXT,
        tags_json TEXT NOT NULL DEFAULT '[]',
        traits_json TEXT NOT NULL DEFAULT '[]',
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial')),
        UNIQUE (snapshot_id, variant_key),
        UNIQUE (snapshot_id, primary_asset_name)
    );
    CREATE INDEX catalog_weapon_variants_identity_idx
        ON catalog_weapon_variants(identity_id, rarity, tier);
    CREATE TABLE catalog_weapon_stats (
        id INTEGER PRIMARY KEY,
        weapon_variant_id INTEGER NOT NULL UNIQUE
            REFERENCES catalog_weapon_variants(id) ON DELETE CASCADE,
        source_data_row_id INTEGER NOT NULL REFERENCES catalog_data_rows(id),
        base_level INTEGER,
        named_weight_row TEXT,
        damage_point_blank REAL,
        damage_mid REAL,
        damage_long REAL,
        damage_max_range REAL,
        environmental_damage REAL,
        impact_damage REAL,
        damage_scale REAL,
        impact_scale REAL,
        crit_chance REAL,
        crit_damage_bonus REAL,
        fire_rate REAL,
        reload_time REAL,
        magazine_size INTEGER,
        durability_per_use REAL,
        range_point_blank REAL,
        range_mid REAL,
        range_long REAL,
        range_max REAL,
        raw_stats_json TEXT NOT NULL
    );
    CREATE TABLE catalog_schematics (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        schematic_key TEXT NOT NULL,
        package_path TEXT NOT NULL,
        crafting_table_path TEXT,
        crafting_row_name TEXT,
        crafting_data_row_id INTEGER REFERENCES catalog_data_rows(id),
        result_asset_type TEXT,
        result_primary_asset_name TEXT,
        result_quantity INTEGER,
        weapon_variant_id INTEGER REFERENCES catalog_weapon_variants(id) ON DELETE SET NULL,
        link_status TEXT NOT NULL
            CHECK (link_status IN ('resolved', 'unresolved', 'ambiguous', 'not_applicable')),
        rarity TEXT,
        tier TEXT,
        max_tier TEXT,
        rating_curve_path TEXT,
        rating_row_name TEXT,
        tags_json TEXT NOT NULL DEFAULT '[]',
        traits_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (snapshot_id, schematic_key)
    );
    CREATE INDEX catalog_schematics_result_idx
        ON catalog_schematics(snapshot_id, result_asset_type, result_primary_asset_name);
    CREATE TABLE catalog_schematic_costs (
        id INTEGER PRIMARY KEY,
        schematic_id INTEGER NOT NULL REFERENCES catalog_schematics(id) ON DELETE CASCADE,
        cost_ordinal INTEGER NOT NULL,
        item_asset_type TEXT NOT NULL,
        item_primary_asset_name TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        UNIQUE (schematic_id, cost_ordinal)
    );
    CREATE TABLE catalog_alterations (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
        source_object_id INTEGER NOT NULL UNIQUE REFERENCES asset_objects(id),
        alteration_key TEXT NOT NULL,
        package_path TEXT NOT NULL,
        display_name TEXT,
        description TEXT,
        rarity TEXT,
        ability_set_path TEXT,
        ability_kit_id INTEGER REFERENCES catalog_ability_kits(id),
        tags_json TEXT NOT NULL DEFAULT '[]',
        semantic_status TEXT NOT NULL
            CHECK (semantic_status IN ('supported', 'partial', 'opaque')),
        UNIQUE (snapshot_id, alteration_key)
    );
    CREATE TABLE catalog_weapon_slots (
        id INTEGER PRIMARY KEY,
        slot_loadout_id INTEGER NOT NULL
            REFERENCES catalog_weapon_slot_loadouts(id) ON DELETE CASCADE,
        slot_ordinal INTEGER NOT NULL,
        unlock_level INTEGER,
        unlock_rarity TEXT,
        slot_definition_row TEXT,
        alteration_group_row TEXT,
        respeccable INTEGER,
        initial_rarity_min TEXT,
        initial_rarity_max TEXT,
        interpretation_status TEXT NOT NULL
            CHECK (interpretation_status IN ('supported', 'partial')),
        UNIQUE (slot_loadout_id, slot_ordinal)
    );
    CREATE TABLE catalog_weapon_slot_options (
        id INTEGER PRIMARY KEY,
        weapon_slot_id INTEGER NOT NULL REFERENCES catalog_weapon_slots(id) ON DELETE CASCADE,
        option_ordinal INTEGER NOT NULL,
        perk_rarity TEXT NOT NULL,
        alteration_primary_asset_name TEXT NOT NULL,
        alteration_id INTEGER REFERENCES catalog_alterations(id) ON DELETE SET NULL,
        initial_roll_weight INTEGER,
        exclusion_names_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (weapon_slot_id, perk_rarity, option_ordinal)
    );
    """,
    """
    CREATE INDEX catalog_ability_kit_grants_kit_idx
        ON catalog_ability_kit_grants(ability_kit_id);
    CREATE INDEX catalog_ability_kit_grants_ability_idx
        ON catalog_ability_kit_grants(ability_id);
    CREATE INDEX catalog_ability_kit_grants_effect_idx
        ON catalog_ability_kit_grants(gameplay_effect_id);
    CREATE INDEX catalog_ability_kits_snapshot_idx
        ON catalog_ability_kits(snapshot_id);
    CREATE INDEX catalog_ability_links_target_idx
        ON catalog_ability_links(target_ability_id);
    CREATE INDEX catalog_alterations_ability_kit_idx
        ON catalog_alterations(ability_kit_id);
    CREATE INDEX catalog_effect_modifiers_magnitude_idx
        ON catalog_effect_modifiers(magnitude_id);
    CREATE INDEX catalog_effect_modifiers_curve_idx
        ON catalog_effect_modifiers(curve_row_id);
    CREATE INDEX catalog_gameplay_effects_snapshot_idx
        ON catalog_gameplay_effects(snapshot_id);
    CREATE INDEX catalog_gameplay_tag_occurrences_object_idx
        ON catalog_gameplay_tag_occurrences(source_object_id);
    CREATE INDEX catalog_hero_abilities_ability_kit_idx
        ON catalog_hero_abilities(ability_kit_id);
    CREATE INDEX catalog_hero_class_kits_ability_kit_idx
        ON catalog_hero_class_kits(ability_kit_id);
    CREATE INDEX catalog_hero_perks_perk_idx
        ON catalog_hero_perks(perk_id);
    CREATE INDEX catalog_heroes_class_idx
        ON catalog_heroes(hero_class_id);
    CREATE INDEX catalog_inheritance_edges_source_object_idx
        ON catalog_inheritance_edges(source_object_id);
    CREATE INDEX catalog_inheritance_edges_target_object_idx
        ON catalog_inheritance_edges(target_object_id);
    CREATE INDEX catalog_magnitudes_curve_idx
        ON catalog_magnitudes(curve_row_id);
    CREATE INDEX catalog_mechanics_magnitude_idx
        ON catalog_mechanics(magnitude_id);
    CREATE INDEX catalog_perks_ability_kit_idx
        ON catalog_perks(ability_kit_id);
    CREATE INDEX catalog_schematics_data_row_idx
        ON catalog_schematics(crafting_data_row_id);
    CREATE INDEX catalog_schematics_weapon_variant_idx
        ON catalog_schematics(weapon_variant_id);
    CREATE INDEX catalog_weapon_slot_loadouts_data_row_idx
        ON catalog_weapon_slot_loadouts(source_data_row_id);
    CREATE INDEX catalog_weapon_slot_options_alteration_idx
        ON catalog_weapon_slot_options(alteration_id);
    CREATE INDEX catalog_weapon_stats_data_row_idx
        ON catalog_weapon_stats(source_data_row_id);
    CREATE INDEX catalog_weapon_variants_stat_data_row_idx
        ON catalog_weapon_variants(stat_data_row_id);
    CREATE INDEX catalog_weapon_variants_slot_loadout_idx
        ON catalog_weapon_variants(slot_loadout_id);
    CREATE INDEX catalog_weapon_variants_baseline_loadout_idx
        ON catalog_weapon_variants(baseline_slot_loadout_id);
    """
]

DIFFICULTY_TO_POWER_LEVEL = {7.0: 15, 20.0: 40, 30.0: 70, 50.0: 140, 52.0: 160}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    _prepare_legacy_schema(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }
    for version, migration in enumerate(MIGRATIONS, 1):
        if version in applied:
            continue
        if version == 1:
            _remove_legacy_index_conflicts(connection)
        if version == 3:
            _ensure_live_capture_columns(connection)
        with connection:
            connection.executescript(_replay_safe_migration(migration))
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
            connection.execute(f"PRAGMA user_version = {version}")
    return connection


def _replay_safe_migration(migration: str) -> str:
    migration = re.sub(
        r"CREATE TABLE (?!IF NOT EXISTS )",
        "CREATE TABLE IF NOT EXISTS ",
        migration,
    )
    return re.sub(
        r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS )",
        lambda match: f"CREATE {match.group(1) or ''}INDEX IF NOT EXISTS ",
        migration,
    )


def _remove_legacy_index_conflicts(connection: sqlite3.Connection) -> None:
    expected_tables = {
        "mission_nodes_lookup_idx": "mission_nodes",
        "attempts_history_idx": "mission_attempts",
        "attempts_cohort_idx": "mission_attempts",
        "membership_timeline_idx": "membership_events",
    }
    with connection:
        for name, expected_table in expected_tables.items():
            row = connection.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            if row is not None and row["tbl_name"] != expected_table:
                connection.execute(f'DROP INDEX "{name}"')


def _ensure_live_capture_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(capture_files)")
    }
    with connection:
        if "capture_kind" not in columns:
            connection.execute(
                """
                ALTER TABLE capture_files ADD COLUMN capture_kind TEXT NOT NULL DEFAULT 'batch'
                    CHECK (capture_kind IN ('batch', 'live'))
                """
            )
        if "latest_content_sha256" not in columns:
            connection.execute(
                "ALTER TABLE capture_files ADD COLUMN latest_content_sha256 TEXT"
            )


def _prepare_legacy_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_migrations" in tables or "capture_files" not in tables:
        return
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(capture_files)")
    }
    if "id" in columns:
        return
    with connection:
        connection.execute("ALTER TABLE capture_files RENAME TO legacy_capture_files_v0")
        if "matchmaking_attempts" in tables:
            connection.execute(
                "ALTER TABLE matchmaking_attempts RENAME TO legacy_matchmaking_attempts_v0"
            )


def find_logs(paths: Iterable[Path]) -> list[Path]:
    logs: set[Path] = set()
    for path in paths:
        if path.is_dir():
            logs.update(candidate.resolve() for candidate in path.rglob("*.log"))
        elif path.is_file():
            logs.add(path.resolve())
        else:
            raise FileNotFoundError(path)
    return sorted(logs)


def ingest_logs(connection: sqlite3.Connection, paths: Iterable[Path]) -> dict[str, int]:
    files = find_logs(paths)
    counters = {
        "files": 0,
        "mission_nodes": 0,
        "attempts": 0,
        "assignments": 0,
        "lobby_sessions": 0,
        "maps": 0,
        "membership_events": 0,
    }
    privacy_salt = _privacy_salt(connection)
    for path in files:
        content_hash = _file_sha256(path)
        result = analyze(path, privacy_salt=privacy_salt)
        attempts = result["attempts"]
        stat = path.stat()
        source = str(path)
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO capture_files(
                    content_sha256, source_path, size_bytes, modified_ns, attempt_count
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_sha256) DO NOTHING
                """,
                (content_hash, source, stat.st_size, stat.st_mtime_ns, len(attempts)),
            )
            counters["files"] += cursor.rowcount
            capture_id = connection.execute(
                "SELECT id FROM capture_files WHERE content_sha256 = ?", (content_hash,)
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO capture_sources(capture_id, source_path) VALUES (?, ?)
                ON CONFLICT(capture_id, source_path) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP
                """,
                (capture_id, source),
            )
            for index, attempt in enumerate(attempts):
                region_id = _lookup_id(connection, "regions", attempt.get("region"))
                node_id, node_inserted = _mission_node(
                    connection, content_hash, attempt
                )
                counters["mission_nodes"] += node_inserted
                objective_hint = _objective_hint(path)
                cursor = connection.execute(
                    """
                    INSERT INTO mission_attempts(
                        capture_id, source_attempt_index, source_line_start, source_line_end,
                        mission_node_id, requested_region_id, stw_type, fill_mode, party_size,
                        started_at, ended_at, outcome, observation_seconds, build_id, link_code,
                        platform, input_type, objective_hint, objective_evidence,
                        internal_difficulty, power_level, team_size_at_start, team_size_15s,
                        team_size_30s, team_size_60s, first_teammate_seconds, full_team_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(capture_id, source_attempt_index) DO NOTHING
                    """,
                    (
                        capture_id,
                        index,
                        attempt.get("line"),
                        attempt.get("end_line"),
                        node_id,
                        region_id,
                        attempt.get("stw_type"),
                        attempt.get("fill"),
                        attempt.get("party_size"),
                        attempt.get("timestamp"),
                        attempt.get("end_timestamp"),
                        attempt.get("outcome"),
                        attempt.get("post_assignment_observation_seconds"),
                        attempt.get("build_id"),
                        attempt.get("link_code"),
                        attempt.get("platform"),
                        attempt.get("input_type"),
                        objective_hint,
                        "capture_filename" if objective_hint else None,
                        attempt.get("internal_difficulty"),
                        _power_level(attempt.get("internal_difficulty")),
                        attempt.get("observed_team_size_at_match_start"),
                        attempt.get("largest_team_size_within_15_seconds"),
                        attempt.get("largest_team_size_within_30_seconds"),
                        attempt.get("largest_team_size_within_60_seconds"),
                        attempt.get("time_to_first_teammate_seconds"),
                        attempt.get("time_to_full_team_seconds"),
                    ),
                )
                counters["attempts"] += cursor.rowcount
                attempt_id = connection.execute(
                    "SELECT id FROM mission_attempts WHERE capture_id=? AND source_attempt_index=?",
                    (capture_id, index),
                ).fetchone()["id"]
                lobby_id, lobby_inserted = _lobby_session(connection, attempt)
                counters["lobby_sessions"] += lobby_inserted
                datacenter_id = _lookup_id(
                    connection, "datacenters", attempt.get("assigned_subregion")
                )
                if attempt.get("assigned_line") is not None:
                    cursor = connection.execute(
                        """
                        INSERT INTO assignments(
                            attempt_id, source_line, assigned_at, assignment_latency_seconds,
                            datacenter_id, lobby_session_id, match_identifier
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(attempt_id) DO NOTHING
                        """,
                        (
                            attempt_id,
                            attempt.get("assigned_line"),
                            attempt.get("assigned_timestamp"),
                            attempt.get("assignment_latency_seconds"),
                            datacenter_id,
                            lobby_id,
                            attempt.get("assigned_match_id"),
                        ),
                    )
                    counters["assignments"] += cursor.rowcount
                if datacenter_id and region_id and attempt.get("assigned_line"):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO datacenter_region_observations(
                            datacenter_id, region_id, capture_id, source_line,
                            observed_at, evidence_type
                        ) VALUES (?, ?, ?, ?, ?, 'assignment')
                        """,
                        (
                            datacenter_id,
                            region_id,
                            capture_id,
                            attempt.get("assigned_line"),
                            attempt.get("assigned_timestamp"),
                        ),
                    )
                for event in attempt.get("map_events", []):
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO attempt_maps(
                            attempt_id, source_line, observed_at, map_path
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (attempt_id, event["line"], event["timestamp"], event["map"]),
                    )
                    counters["maps"] += cursor.rowcount
                for event in attempt.get("membership_events", []):
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO membership_events(
                            attempt_id, lobby_session_id, source_line, occurred_at, phase,
                            event_type, participant_hash, replaced_participant_hash, slot,
                            team_size_after
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            lobby_id,
                            event["line"],
                            event["timestamp"],
                            event["phase"],
                            event["event_type"],
                            event["participant_hash"],
                            event["replaced_participant_hash"],
                            event["slot"],
                            event["team_size_after"],
                        ),
                    )
                    counters["membership_events"] += cursor.rowcount
            for observation in result["qos"]["datacenter_results"]:
                dc_id = _lookup_id(connection, "datacenters", observation["subregion"])
                qos_region_id = _lookup_id(connection, "regions", observation["region"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO datacenter_region_observations(
                        datacenter_id, region_id, capture_id, source_line,
                        observed_at, evidence_type
                    ) VALUES (?, ?, ?, ?, ?, 'qos')
                    """,
                    (
                        dc_id,
                        qos_region_id,
                        capture_id,
                        observation["line"],
                        observation["timestamp"],
                    ),
                )
    return counters


def _privacy_salt(connection: sqlite3.Connection) -> bytes:
    row = connection.execute(
        "SELECT value FROM app_metadata WHERE key='privacy_salt'"
    ).fetchone()
    if row is None:
        value = secrets.token_hex(32)
        with connection:
            connection.execute(
                "INSERT INTO app_metadata(key, value) VALUES ('privacy_salt', ?)", (value,)
            )
        return bytes.fromhex(value)
    return bytes.fromhex(row["value"])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lookup_id(
    connection: sqlite3.Connection, table: str, code: object
) -> int | None:
    if not isinstance(code, str) or not code:
        return None
    if table not in {"regions", "datacenters"}:
        raise ValueError(f"unsupported lookup table: {table}")
    connection.execute(f"INSERT OR IGNORE INTO {table}(code) VALUES (?)", (code,))
    return connection.execute(
        f"SELECT id FROM {table} WHERE code = ?", (code,)
    ).fetchone()["id"]


def _rotation_window(timestamp: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(timestamp, str):
        return None, None, None
    parsed = datetime.strptime(timestamp, "%Y.%m.%d-%H.%M.%S:%f").replace(
        tzinfo=timezone.utc
    )
    start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        timestamp,
        timestamp,
        f"utc-day:{start.date().isoformat()}",
    )


def _mission_node(
    connection: sqlite3.Connection,
    content_hash: str,
    attempt: dict[str, Any],
) -> tuple[int | None, int]:
    theater = attempt.get("theater_id")
    mission = attempt.get("mission_id")
    if not isinstance(theater, str) or not isinstance(mission, str):
        return None, 0
    rotation_start, rotation_end, rotation_context = _rotation_window(
        attempt.get("timestamp")
    )
    rotation_context = rotation_context or f"unknown:{content_hash}"
    existing = connection.execute(
        "SELECT id FROM mission_nodes WHERE theater_uuid=? AND mission_uuid=? AND rotation_context=?",
        (theater, mission, rotation_context),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO mission_nodes(
            theater_uuid, mission_uuid, rotation_context, rotation_context_evidence,
            observed_rotation_start, observed_rotation_end, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, 'inferred_from_utc_capture_day', ?, ?, ?, ?)
        ON CONFLICT(theater_uuid, mission_uuid, rotation_context) DO UPDATE SET
            observed_rotation_start=CASE
                WHEN mission_nodes.observed_rotation_start IS NULL THEN excluded.observed_rotation_start
                WHEN excluded.observed_rotation_start IS NULL THEN mission_nodes.observed_rotation_start
                ELSE MIN(mission_nodes.observed_rotation_start, excluded.observed_rotation_start)
            END,
            observed_rotation_end=CASE
                WHEN mission_nodes.observed_rotation_end IS NULL THEN excluded.observed_rotation_end
                WHEN excluded.observed_rotation_end IS NULL THEN mission_nodes.observed_rotation_end
                ELSE MAX(mission_nodes.observed_rotation_end, excluded.observed_rotation_end)
            END,
            first_seen_at=CASE
                WHEN mission_nodes.first_seen_at IS NULL THEN excluded.first_seen_at
                WHEN excluded.first_seen_at IS NULL THEN mission_nodes.first_seen_at
                ELSE MIN(mission_nodes.first_seen_at, excluded.first_seen_at)
            END,
            last_seen_at=CASE
                WHEN mission_nodes.last_seen_at IS NULL THEN excluded.last_seen_at
                WHEN excluded.last_seen_at IS NULL THEN mission_nodes.last_seen_at
                ELSE MAX(mission_nodes.last_seen_at, excluded.last_seen_at)
            END
        """,
        (
            theater,
            mission,
            rotation_context,
            rotation_start,
            attempt.get("end_timestamp") or rotation_end,
            attempt.get("timestamp"),
            attempt.get("end_timestamp") or attempt.get("timestamp"),
        ),
    )
    row = connection.execute(
        "SELECT id FROM mission_nodes WHERE theater_uuid=? AND mission_uuid=? AND rotation_context=?",
        (theater, mission, rotation_context),
    ).fetchone()
    return row["id"], int(existing is None)


def _lobby_session(
    connection: sqlite3.Connection, attempt: dict[str, Any]
) -> tuple[int | None, int]:
    session_id = attempt.get("assigned_session_id")
    if not isinstance(session_id, str):
        return None, 0
    existing = connection.execute(
        "SELECT id FROM lobby_sessions WHERE session_identifier=?", (session_id,)
    ).fetchone()
    connection.execute(
        """
        INSERT INTO lobby_sessions(session_identifier, first_seen_at, last_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(session_identifier) DO UPDATE SET
            first_seen_at=CASE
                WHEN lobby_sessions.first_seen_at IS NULL THEN excluded.first_seen_at
                WHEN excluded.first_seen_at IS NULL THEN lobby_sessions.first_seen_at
                ELSE MIN(lobby_sessions.first_seen_at, excluded.first_seen_at)
            END,
            last_seen_at=CASE
                WHEN lobby_sessions.last_seen_at IS NULL THEN excluded.last_seen_at
                WHEN excluded.last_seen_at IS NULL THEN lobby_sessions.last_seen_at
                ELSE MAX(lobby_sessions.last_seen_at, excluded.last_seen_at)
            END
        """,
        (
            session_id,
            attempt.get("assigned_timestamp"),
            attempt.get("end_timestamp") or attempt.get("assigned_timestamp"),
        ),
    )
    row = connection.execute(
        "SELECT id FROM lobby_sessions WHERE session_identifier=?", (session_id,)
    ).fetchone()
    return row["id"], int(existing is None)


def _power_level(value: object) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return DIFFICULTY_TO_POWER_LEVEL.get(float(value))


def _objective_hint(path: Path) -> str | None:
    name = path.stem.lower().replace("_", "-")
    aliases = (
        ("ride-the-lightning", "Ride the Lightning"),
        ("ride-lightning", "Ride the Lightning"),
        ("retrieve-data", "Retrieve the Data"),
        ("repair-shelter", "Repair the Shelter"),
        ("rescue-survivors", "Rescue the Survivors"),
        ("build-radar-grid", "Build the Radar Grid"),
        ("fight-the-storm", "Fight the Storm"),
        ("resupply", "Resupply"),
    )
    return next((label for token, label in aliases if token in name), None)


def ensure_live_capture(
    connection: sqlite3.Connection,
    watcher_id: int,
    generation: int,
    source_path: Path,
    spool_path: Path,
    file_identity: str | None,
) -> int:
    row = connection.execute(
        """
        SELECT capture_id FROM live_watch_generations
        WHERE watcher_id=? AND generation=?
        """,
        (watcher_id, generation),
    ).fetchone()
    if row is not None and row["capture_id"] is not None:
        return row["capture_id"]
    identity_hash = hashlib.sha256(
        f"live\0{watcher_id}\0{generation}\0{source_path.resolve()}".encode("utf-8")
    ).hexdigest()
    latest_hash = _file_sha256(spool_path)
    stat = spool_path.stat()
    with connection:
        connection.execute(
            """
            INSERT INTO capture_files(
                content_sha256, source_path, size_bytes, modified_ns, attempt_count,
                capture_kind, latest_content_sha256
            ) VALUES (?, ?, ?, ?, 0, 'live', ?)
            ON CONFLICT(content_sha256) DO UPDATE SET
                source_path=excluded.source_path,
                size_bytes=excluded.size_bytes,
                modified_ns=excluded.modified_ns,
                latest_content_sha256=excluded.latest_content_sha256,
                last_ingested_at=CURRENT_TIMESTAMP
            """,
            (
                identity_hash,
                str(source_path.resolve()),
                stat.st_size,
                stat.st_mtime_ns,
                latest_hash,
            ),
        )
        capture_id = connection.execute(
            "SELECT id FROM capture_files WHERE content_sha256=?", (identity_hash,)
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO capture_sources(capture_id, source_path) VALUES (?, ?)
            ON CONFLICT(capture_id, source_path) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP
            """,
            (capture_id, str(source_path.resolve())),
        )
        connection.execute(
            """
            INSERT INTO live_watch_generations(
                watcher_id, generation, capture_id, file_identity, spool_path
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(watcher_id, generation) DO UPDATE SET
                capture_id=excluded.capture_id,
                file_identity=excluded.file_identity,
                spool_path=excluded.spool_path
            """,
            (watcher_id, generation, capture_id, file_identity, str(spool_path)),
        )
    return capture_id


def persist_live_analysis(
    connection: sqlite3.Connection,
    capture_id: int,
    source_path: Path,
    spool_path: Path,
    result: dict[str, Any],
) -> dict[str, int]:
    """Upsert one growing live-log generation using Phase 1 normalized tables."""
    counters = {
        "attempts_created": 0,
        "attempts_updated": 0,
        "assignments": 0,
        "maps": 0,
        "membership_events": 0,
    }
    objective_hint = _objective_hint(source_path)
    attempts = result.get("attempts", [])
    with connection:
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            region_id = _lookup_id(connection, "regions", attempt.get("region"))
            node_id, _ = _mission_node(
                connection,
                connection.execute(
                    "SELECT content_sha256 FROM capture_files WHERE id=?", (capture_id,)
                ).fetchone()["content_sha256"],
                attempt,
            )
            existing = connection.execute(
                """
                SELECT id FROM mission_attempts
                WHERE capture_id=? AND source_attempt_index=?
                """,
                (capture_id, index),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO mission_attempts(
                    capture_id, source_attempt_index, source_line_start, source_line_end,
                    mission_node_id, requested_region_id, stw_type, fill_mode, party_size,
                    started_at, ended_at, outcome, observation_seconds, build_id, link_code,
                    platform, input_type, objective_hint, objective_evidence,
                    internal_difficulty, power_level, team_size_at_start, team_size_15s,
                    team_size_30s, team_size_60s, first_teammate_seconds, full_team_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_id, source_attempt_index) DO UPDATE SET
                    source_line_end=excluded.source_line_end,
                    mission_node_id=COALESCE(excluded.mission_node_id, mission_attempts.mission_node_id),
                    requested_region_id=COALESCE(excluded.requested_region_id, mission_attempts.requested_region_id),
                    stw_type=COALESCE(excluded.stw_type, mission_attempts.stw_type),
                    fill_mode=COALESCE(excluded.fill_mode, mission_attempts.fill_mode),
                    party_size=COALESCE(excluded.party_size, mission_attempts.party_size),
                    ended_at=excluded.ended_at,
                    outcome=excluded.outcome,
                    observation_seconds=excluded.observation_seconds,
                    build_id=COALESCE(excluded.build_id, mission_attempts.build_id),
                    link_code=COALESCE(excluded.link_code, mission_attempts.link_code),
                    platform=COALESCE(excluded.platform, mission_attempts.platform),
                    input_type=COALESCE(excluded.input_type, mission_attempts.input_type),
                    objective_hint=COALESCE(excluded.objective_hint, mission_attempts.objective_hint),
                    objective_evidence=COALESCE(excluded.objective_evidence, mission_attempts.objective_evidence),
                    internal_difficulty=COALESCE(excluded.internal_difficulty, mission_attempts.internal_difficulty),
                    power_level=COALESCE(excluded.power_level, mission_attempts.power_level),
                    team_size_at_start=COALESCE(excluded.team_size_at_start, mission_attempts.team_size_at_start),
                    team_size_15s=COALESCE(excluded.team_size_15s, mission_attempts.team_size_15s),
                    team_size_30s=COALESCE(excluded.team_size_30s, mission_attempts.team_size_30s),
                    team_size_60s=COALESCE(excluded.team_size_60s, mission_attempts.team_size_60s),
                    first_teammate_seconds=COALESCE(excluded.first_teammate_seconds, mission_attempts.first_teammate_seconds),
                    full_team_seconds=COALESCE(excluded.full_team_seconds, mission_attempts.full_team_seconds)
                """,
                (
                    capture_id,
                    index,
                    attempt.get("line"),
                    attempt.get("end_line"),
                    node_id,
                    region_id,
                    attempt.get("stw_type"),
                    attempt.get("fill"),
                    attempt.get("party_size"),
                    attempt.get("timestamp"),
                    attempt.get("end_timestamp"),
                    attempt.get("outcome"),
                    attempt.get("post_assignment_observation_seconds"),
                    attempt.get("build_id"),
                    attempt.get("link_code"),
                    attempt.get("platform"),
                    attempt.get("input_type"),
                    objective_hint,
                    "capture_filename" if objective_hint else None,
                    attempt.get("internal_difficulty"),
                    _power_level(attempt.get("internal_difficulty")),
                    attempt.get("observed_team_size_at_match_start"),
                    attempt.get("largest_team_size_within_15_seconds"),
                    attempt.get("largest_team_size_within_30_seconds"),
                    attempt.get("largest_team_size_within_60_seconds"),
                    attempt.get("time_to_first_teammate_seconds"),
                    attempt.get("time_to_full_team_seconds"),
                ),
            )
            counters["attempts_created" if existing is None else "attempts_updated"] += 1
            attempt_id = connection.execute(
                """
                SELECT id FROM mission_attempts
                WHERE capture_id=? AND source_attempt_index=?
                """,
                (capture_id, index),
            ).fetchone()["id"]
            # A growing live spool is re-analyzed from the beginning. Replace its
            # derived roster timeline so parser improvements and late lines cannot
            # leave obsolete inferred events behind.
            connection.execute(
                "DELETE FROM membership_events WHERE attempt_id=?", (attempt_id,)
            )
            lobby_id, _ = _lobby_session(connection, attempt)
            datacenter_id = _lookup_id(
                connection, "datacenters", attempt.get("assigned_subregion")
            )
            if attempt.get("assigned_line") is not None:
                assignment_existing = connection.execute(
                    "SELECT id FROM assignments WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO assignments(
                        attempt_id, source_line, assigned_at, assignment_latency_seconds,
                        datacenter_id, lobby_session_id, match_identifier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_id) DO UPDATE SET
                        source_line=excluded.source_line,
                        assigned_at=excluded.assigned_at,
                        assignment_latency_seconds=excluded.assignment_latency_seconds,
                        datacenter_id=COALESCE(excluded.datacenter_id, assignments.datacenter_id),
                        lobby_session_id=COALESCE(excluded.lobby_session_id, assignments.lobby_session_id),
                        match_identifier=COALESCE(excluded.match_identifier, assignments.match_identifier)
                    """,
                    (
                        attempt_id,
                        attempt.get("assigned_line"),
                        attempt.get("assigned_timestamp"),
                        attempt.get("assignment_latency_seconds"),
                        datacenter_id,
                        lobby_id,
                        attempt.get("assigned_match_id"),
                    ),
                )
                counters["assignments"] += int(assignment_existing is None)
            if datacenter_id and region_id and attempt.get("assigned_line"):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO datacenter_region_observations(
                        datacenter_id, region_id, capture_id, source_line,
                        observed_at, evidence_type
                    ) VALUES (?, ?, ?, ?, ?, 'assignment')
                    """,
                    (
                        datacenter_id,
                        region_id,
                        capture_id,
                        attempt.get("assigned_line"),
                        attempt.get("assigned_timestamp"),
                    ),
                )
            for event in attempt.get("map_events", []):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO attempt_maps(
                        attempt_id, source_line, observed_at, map_path
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (attempt_id, event["line"], event["timestamp"], event["map"]),
                )
                counters["maps"] += cursor.rowcount
            for event in attempt.get("membership_events", []):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO membership_events(
                        attempt_id, lobby_session_id, source_line, occurred_at, phase,
                        event_type, participant_hash, replaced_participant_hash, slot,
                        team_size_after
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        lobby_id,
                        event["line"],
                        event["timestamp"],
                        event["phase"],
                        event["event_type"],
                        event["participant_hash"],
                        event["replaced_participant_hash"],
                        event["slot"],
                        event["team_size_after"],
                    ),
                )
                counters["membership_events"] += cursor.rowcount
        for observation in result.get("qos", {}).get("datacenter_results", []):
            dc_id = _lookup_id(connection, "datacenters", observation["subregion"])
            region_id = _lookup_id(connection, "regions", observation["region"])
            connection.execute(
                """
                INSERT OR IGNORE INTO datacenter_region_observations(
                    datacenter_id, region_id, capture_id, source_line,
                    observed_at, evidence_type
                ) VALUES (?, ?, ?, ?, ?, 'qos')
                """,
                (
                    dc_id,
                    region_id,
                    capture_id,
                    observation["line"],
                    observation["timestamp"],
                ),
            )
        stat = spool_path.stat()
        connection.execute(
            """
            UPDATE capture_files SET
                size_bytes=?, modified_ns=?, attempt_count=?,
                latest_content_sha256=?, last_ingested_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                stat.st_size,
                stat.st_mtime_ns,
                len(attempts),
                _file_sha256(spool_path),
                capture_id,
            ),
        )
    return counters


def persist_live_state_events(
    connection: sqlite3.Connection,
    watcher_id: int,
    generation: int,
    capture_id: int,
    result: dict[str, Any],
) -> int:
    created = 0
    events = result.get("state_events", [])
    with connection:
        connection.execute(
            "DELETE FROM live_state_events WHERE watcher_id=? AND generation=?",
            (watcher_id, generation),
        )
        for event in events:
            attempt_id = None
            if event.get("attempt_line") is not None:
                row = connection.execute(
                    """
                    SELECT id FROM mission_attempts
                    WHERE capture_id=? AND source_line_start=?
                    """,
                    (capture_id, event["attempt_line"]),
                ).fetchone()
                attempt_id = row["id"] if row else None
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO live_state_events(
                    watcher_id, generation, attempt_id, occurred_at,
                    source_line, state, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    watcher_id,
                    generation,
                    attempt_id,
                    event.get("timestamp"),
                    event["line"],
                    event["state"],
                    event["reason"],
                ),
            )
            created += cursor.rowcount
        if events:
            event = events[-1]
            row = connection.execute(
                """
                SELECT attempt_id FROM live_state_events
                WHERE watcher_id=? AND generation=? AND source_line=?
                  AND state=? AND reason=?
                """,
                (
                    watcher_id,
                    generation,
                    event["line"],
                    event["state"],
                    event["reason"],
                ),
            ).fetchone()
            attempt_id = None if event["state"] == "Idle" else row["attempt_id"]
            connection.execute(
                """
                INSERT INTO live_states(
                    watcher_id, generation, state, attempt_id, occurred_at,
                    source_line, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(watcher_id) DO UPDATE SET
                    generation=excluded.generation,
                    state=excluded.state,
                    attempt_id=excluded.attempt_id,
                    occurred_at=excluded.occurred_at,
                    source_line=excluded.source_line,
                    reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    watcher_id,
                    generation,
                    event["state"],
                    attempt_id,
                    event.get("timestamp"),
                    event["line"],
                    event["reason"],
                ),
            )
    return created


def fetch_metrics(interval: str) -> dict[str, Any]:
    request = urllib.request.Request(
        API_ROOT + INTERVAL_PATHS[interval],
        headers={"Accept": "application/json", "User-Agent": "stw-intelligence/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Epic Data API request failed: {error}") from error


def store_metrics(
    connection: sqlite3.Connection, interval: str, payload: dict[str, Any]
) -> dict[str, int]:
    rows = 0
    with connection:
        for metric, samples in payload.items():
            if not isinstance(samples, list):
                continue
            for sample in samples:
                if not isinstance(sample, dict) or "timestamp" not in sample:
                    continue
                value = sample.get("value")
                if value is not None and not isinstance(value, (int, float)):
                    continue
                connection.execute(
                    """
                    INSERT INTO global_metrics(interval, metric, timestamp, value)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(interval, metric, timestamp) DO UPDATE SET
                        value=excluded.value,
                        fetched_at=CURRENT_TIMESTAMP
                    """,
                    (interval, metric, sample["timestamp"], value),
                )
                rows += 1
    return {"samples": rows}


def latest_global_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for interval in INTERVAL_PATHS:
        rows = connection.execute(
            """
            SELECT metric, timestamp, value
            FROM global_metrics
            WHERE interval = ? AND timestamp = (
                SELECT MAX(timestamp) FROM global_metrics WHERE interval = ? AND value IS NOT NULL
            )
            ORDER BY metric
            """,
            (interval, interval),
        ).fetchall()
        if rows:
            result[interval] = {
                "timestamp": rows[0]["timestamp"],
                "metrics": {row["metric"]: row["value"] for row in rows},
            }
    return result


def activity_report(
    connection: sqlite3.Connection, horizon: int, min_regions: int = 2
) -> dict[str, Any]:
    size_column = f"team_size_{horizon}s"
    rows = connection.execute(
        f"""
        SELECT
            ma.*,
            ma.started_at AS registered_at,
            mn.mission_uuid AS mission_id,
            mn.theater_uuid AS theater_id,
            r.code AS region,
            a.assignment_latency_seconds
        FROM mission_attempts AS ma
        JOIN mission_nodes AS mn ON mn.id = ma.mission_node_id
        JOIN regions AS r ON r.id = ma.requested_region_id
        LEFT JOIN assignments AS a ON a.attempt_id = ma.id
        WHERE ma.outcome = 'joined'
          AND ma.fill_mode = 'Public'
          AND ma.party_size = 1
          AND ma.observation_seconds >= ?
          AND ma.{size_column} IS NOT NULL
        ORDER BY mn.mission_uuid, mn.theater_uuid, ma.internal_difficulty,
                 r.code, ma.started_at
        """,
        (horizon,),
    ).fetchall()

    cohorts: dict[tuple[object, ...], dict[str, list[sqlite3.Row]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        key = (row["mission_id"], row["theater_id"], row["internal_difficulty"])
        cohorts[key][row["region"]].append(row)

    cohort_reports = []
    for (mission, theater, difficulty), regions in cohorts.items():
        if len(regions) < min_regions:
            continue
        region_reports = []
        for region, attempts in sorted(regions.items()):
            sizes = [int(row[size_column]) for row in attempts]
            latencies = [
                float(row["assignment_latency_seconds"])
                for row in attempts
                if row["assignment_latency_seconds"] is not None
            ]
            teammate_arrivals = sum(
                row["first_teammate_seconds"] is not None
                and row["first_teammate_seconds"] <= horizon
                for row in attempts
            )
            slot_fill_scores = [
                max(0, min(TEAM_CAPACITY - 1, size - 1)) / (TEAM_CAPACITY - 1)
                for size in sizes
            ]
            region_reports.append(
                {
                    "region": region,
                    "attempts": len(attempts),
                    "teammate_seen_rate": round(teammate_arrivals / len(attempts), 4),
                    "mean_team_high_water": round(fmean(sizes), 4),
                    "observed_team_high_water_index": round(fmean(slot_fill_scores), 4),
                    "mean_assignment_latency_seconds": round(fmean(latencies), 4)
                    if latencies
                    else None,
                }
            )
        cohort_reports.append(
            {
                "mission_id": mission,
                "theater_id": theater,
                "internal_difficulty": difficulty,
                "regions_observed": len(regions),
                "regions": region_reports,
            }
        )

    return {
        "global_stw": latest_global_metrics(connection),
        "regional_activity": {
            "horizon_seconds": horizon,
            "definition": (
                "Single-client, match-visible teammate activity for fixed mission cohorts; "
                "this is not regional population or a regional share of global CCU."
            ),
            "eligibility": "joined + Public Fill + solo party + full observation window",
            "minimum_regions_per_cohort": min_regions,
            "cohorts": cohort_reports,
        },
    }


def print_human_report(report: dict[str, Any]) -> None:
    print("Global STW metrics (official Epic aggregate)")
    if not report["global_stw"]:
        print("  No metrics stored. Run sync-global first.")
    for interval, record in report["global_stw"].items():
        metrics = record["metrics"]
        print(
            f"  {interval:6} {record['timestamp']} "
            f"peakCCU={_display(metrics.get('peakCCU'))} "
            f"uniquePlayers={_display(metrics.get('uniquePlayers'))} "
            f"plays={_display(metrics.get('plays'))}"
        )

    activity = report["regional_activity"]
    print(f"\nRegional matchmaking activity ({activity['horizon_seconds']}s window)")
    print("  Activity index only; it is not a regional player count or CCU share.")
    cohorts = activity["cohorts"]
    if not cohorts:
        print("  No eligible observations stored.")
        return
    for cohort in cohorts:
        print(
            f"  mission={cohort['mission_id']} theater={cohort['theater_id']} "
            f"difficulty={cohort['internal_difficulty']}"
        )
        for region in cohort["regions"]:
            print(
                f"    {region['region']:4} n={region['attempts']:<3} "
                f"teammate_seen={region['teammate_seen_rate']:.1%} "
                f"team_high_water_index={region['observed_team_high_water_index']:.3f} "
                f"mean_assignment={_display(region['mean_assignment_latency_seconds'])}s"
            )


def _display(value: object) -> str:
    return "n/a" if value is None else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Extract and store log captures")
    ingest_parser.add_argument("paths", nargs="+", type=Path)

    sync_parser = subparsers.add_parser(
        "sync-global", help="Store official global STW engagement metrics"
    )
    sync_parser.add_argument("--interval", choices=INTERVAL_PATHS, default="minute")
    sync_parser.add_argument("--from-file", type=Path)

    report_parser = subparsers.add_parser("report", help="Report measured activity")
    report_parser.add_argument("--horizon", type=int, choices=(15, 30, 60), default=60)
    report_parser.add_argument(
        "--all-cohorts",
        action="store_true",
        help="include single-region cohorts that cannot support regional comparison",
    )
    report_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    connection = connect(args.db)
    try:
        if args.command == "ingest":
            print(json.dumps(ingest_logs(connection, args.paths), indent=2))
        elif args.command == "sync-global":
            payload = (
                json.loads(args.from_file.read_text(encoding="utf-8"))
                if args.from_file
                else fetch_metrics(args.interval)
            )
            print(json.dumps(store_metrics(connection, args.interval, payload), indent=2))
        else:
            report = activity_report(
                connection, args.horizon, min_regions=1 if args.all_cohorts else 2
            )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print_human_report(report)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
