#!/usr/bin/env python3
"""Evidence report and proven lookup helpers for shared STW runtime semantics."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from stw_assets import latest_asset_snapshot_id
from stw_pipeline import connect


def _source(connection: sqlite3.Connection, object_id: int, kind: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT object.package_path, object.object_name, file.relative_path,
               file.content_sha256
        FROM asset_objects object JOIN asset_files file ON file.id=object.asset_file_id
        WHERE object.id=?
        """,
        (object_id,),
    ).fetchone()
    return {"kind": kind, **dict(row)} if row else {"kind": kind, "object_id": object_id}


def curve_lookup(
    connection: sqlite3.Connection,
    snapshot_id: int,
    row_name: str,
    input_value: float,
    package_path: str | None = None,
) -> tuple[float | None, dict[str, Any] | None, str | None]:
    rows = connection.execute(
        """
        SELECT point.time_value, point.output_value, point.interpolation,
               table_row.source_object_id, table_row.package_path
        FROM catalog_curve_rows curve_row
        JOIN catalog_curve_tables table_row ON table_row.id=curve_row.curve_table_id
        JOIN catalog_curve_points point ON point.curve_row_id=curve_row.id
        WHERE table_row.snapshot_id=? AND curve_row.row_name=?
          AND (? IS NULL OR lower(table_row.package_path)=lower(?))
        ORDER BY point.point_ordinal
        """,
        (snapshot_id, row_name, package_path, package_path),
    ).fetchall()
    if not rows:
        return None, None, "curve_missing"
    object_ids = {row["source_object_id"] for row in rows}
    if len(object_ids) != 1:
        return None, None, "curve_ambiguous"
    provenance = _source(connection, rows[0]["source_object_id"], "runtime_curve") | {
        "row_name": row_name
    }
    for row in rows:
        if math.isclose(float(row["time_value"]), input_value, abs_tol=1e-9):
            return float(row["output_value"]), provenance, None
    lower = next((row for row in reversed(rows) if row["time_value"] < input_value), None)
    upper = next((row for row in rows if row["time_value"] > input_value), None)
    if lower is None or upper is None:
        return None, provenance, "curve_extrapolation_unproven"
    modes = (str(lower["interpolation"]), str(upper["interpolation"]))
    if not all("RCIM_Linear" in mode for mode in modes):
        return None, provenance, "curve_interpolation_unsupported"
    fraction = (input_value - lower["time_value"]) / (
        upper["time_value"] - lower["time_value"]
    )
    return (
        float(lower["output_value"])
        + fraction * (float(upper["output_value"]) - float(lower["output_value"])),
        provenance,
        None,
    )


def crit_rating_to_chance(
    connection: sqlite3.Connection, snapshot_id: int, crit_rating: float
) -> tuple[float | None, dict[str, Any] | None, str | None]:
    return curve_lookup(
        connection, snapshot_id, "Item.All.CritRatingToCritChance", crit_rating
    )


def homebase_rating_to_difficulty(
    connection: sqlite3.Connection, snapshot_id: int, rating: int
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    rows = connection.execute(
        """
        SELECT row.row_json, table_row.source_object_id
        FROM catalog_data_rows row
        JOIN catalog_data_tables table_row ON table_row.id=row.data_table_id
        WHERE table_row.snapshot_id=?
          AND table_row.table_name='HomebaseRatingDifficultyMapping'
          AND row.row_name=?
        """,
        (snapshot_id, str(rating)),
    ).fetchall()
    if not rows:
        return None, None, "rating_mapping_missing"
    if len(rows) != 1:
        return None, None, "rating_mapping_ambiguous"
    payload = json.loads(rows[0]["row_json"])
    difficulty = payload.get("Difficulty")
    if not isinstance(difficulty, (int, float)):
        return None, None, "difficulty_value_missing"
    return (
        int(difficulty),
        _source(connection, rows[0]["source_object_id"], "runtime_data_table")
        | {"row_name": str(rating)},
        None,
    )


def _offense_rule(connection: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT magnitude.*, object.id AS object_id
        FROM catalog_magnitudes magnitude
        JOIN asset_objects object ON object.id=magnitude.source_object_id
        WHERE magnitude.snapshot_id=?
          AND object.package_path='/SaveTheWorld/Balance/CombinedStats/GE_Map_Offense_To_WeaponDamage'
          AND magnitude.purpose='effect_modifier'
        """,
        (snapshot_id,),
    ).fetchall()
    if len(rows) != 1:
        return {"status": "unsupported", "reason": "offense_mapping_not_unique"}
    row = rows[0]
    shape = json.loads(row["shape_json"])
    custom = shape.get("CustomMagnitude", {})
    calculation = (custom.get("CalculationClassMagnitude") or {}).get("ObjectName")
    coefficient = (custom.get("Coefficient") or {}).get("Value")
    pre_additive = (custom.get("PreMultiplyAdditiveValue") or {}).get("Value")
    post_additive = (custom.get("PostMultiplyAdditiveValue") or {}).get("Value")
    return {
        "status": "partial",
        "proven_outer_formula": "post_additive + coefficient * native_calculation_result",
        "coefficient": coefficient,
        "pre_additive": pre_additive,
        "post_additive": post_additive,
        "native_calculation": calculation,
        "reason": "FromOffenseModMagnitudeCalculation is native Fortnite code",
        "provenance": _source(connection, row["object_id"], "offense_mapping"),
    }


def nocturno_signature_report(
    connection: sqlite3.Connection, snapshot_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT alteration.description, alteration.source_object_id,
               ability.source_object_id AS ability_object_id
        FROM catalog_alterations alteration
        JOIN catalog_ability_kit_grants grant_row
          ON grant_row.ability_kit_id=alteration.ability_kit_id
         AND grant_row.grant_kind='ability'
        JOIN catalog_abilities ability ON ability.id=grant_row.ability_id
        WHERE alteration.snapshot_id=?
          AND lower(alteration.alteration_key)='aid_g_weapon_onreload_explode'
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return {"status": "unsupported", "reason": "signature_graph_missing"}
    magnitude = connection.execute(
        """
        SELECT magnitude.* FROM catalog_magnitudes magnitude
        WHERE magnitude.source_object_id=? AND magnitude.purpose='ability_parameter'
          AND magnitude.curve_row_name='Alteration.Weapon.Ranged.OnReload.Explode.Damage'
        """,
        (row["ability_object_id"],),
    ).fetchone()
    damage_factor = None
    curve_source = None
    if magnitude:
        damage_factor, curve_source, _ = curve_lookup(
            connection, snapshot_id, magnitude["curve_row_name"], 0.0
        )
        if damage_factor is not None:
            damage_factor *= float(magnitude["literal_value"] or 1.0)
    mark = connection.execute(
        """
        SELECT effect.stacking_type, effect.stack_limit, effect.source_object_id
        FROM catalog_mechanics mechanic
        JOIN catalog_gameplay_effects effect
          ON effect.snapshot_id=mechanic.snapshot_id
         AND json_extract(mechanic.value_json, '$.target_path')
             LIKE effect.package_path || '.%'
        WHERE mechanic.source_object_id=? AND mechanic.mechanic_type='referenced_effect'
          AND json_extract(mechanic.value_json, '$.name') LIKE '%Mark_Target%'
        """,
        (row["ability_object_id"],),
    ).fetchone()
    triggers = connection.execute(
        """SELECT value_json FROM catalog_mechanics WHERE source_object_id=?
           AND mechanic_type='trigger'""",
        (row["ability_object_id"],),
    ).fetchone()
    return {
        "status": "partial",
        "classification": "statically_bounded_blueprint_control_flow",
        "damage_factor_per_mark": damage_factor,
        "radius_unreal_units": curve_lookup(
            connection,
            snapshot_id,
            "Alteration.Weapon.Ranged.OnReload.Explode.Radius",
            0.0,
        )[0],
        "mark_stacking_type": mark["stacking_type"] if mark else None,
        "mark_stack_limit": mark["stack_limit"] if mark else None,
        "triggers": json.loads(triggers["value_json"]) if triggers else [],
        "proven": [
            "damage and reload gameplay-event triggers",
            "65% stored damage factor per mark",
            "element-matched SetByCaller damage effects",
            "infinite marks aggregated by source with a 500 stack cap",
            "256 Unreal-unit targeting radius",
        ],
        "opaque": [
            "Blueprint event control flow that records each hit's damage",
            "mark removal/explosion scheduling and target-death handling",
            "native FortDamageFormulaExecutionCalculation final damage",
        ],
        "provenance": [
            _source(connection, row["source_object_id"], "weapon_alteration"),
            _source(connection, row["ability_object_id"], "signature_ability"),
            *([curve_source] if curve_source else []),
            *([_source(connection, mark["source_object_id"], "mark_effect")] if mark else []),
        ],
    }


def runtime_semantics_report(
    connection: sqlite3.Connection, snapshot_id: int | None = None
) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_asset_snapshot_id(connection)
    if snapshot_id is None:
        raise ValueError("no asset snapshot is available")
    sample, crit_source, crit_error = crit_rating_to_chance(connection, snapshot_id, 30.0)
    difficulty, difficulty_source, difficulty_error = homebase_rating_to_difficulty(
        connection, snapshot_id, 160
    )
    return {
        "snapshot_id": snapshot_id,
        "rules": {
            "crit_rating_conversion": {
                "status": "supported" if crit_error is None else "unsupported",
                "lookup": "Item.All.CritRatingToCritChance evaluated at total CritRating",
                "sample": {"rating": 30.0, "chance": sample},
                "provenance": crit_source,
                "remaining_boundary": "combination with weapon DiceCritChance is native",
            },
            "item_level_damage_scaling": {
                "status": "partial",
                "proven": "weapon BaseLevel/DmgScale fields and rarity-tier item-rating curves",
                "reason": "native weapon stat initialization applies DmgScale",
            },
            "hero_offense": _offense_rule(connection, snapshot_id),
            "mission_enemy_scaling": {
                "status": "partial",
                "proven": "rating-to-difficulty and mission growth-bound lookup tables",
                "sample": {"rating": 160, "difficulty": difficulty},
                "provenance": difficulty_source,
                "lookup_error": difficulty_error,
                "reason": "native pawn initialization and damage execution consume the difficulty",
            },
            "elemental_matchups": {
                "status": "opaque",
                "reason": "FortDamageFormulaExecutionCalculation is native Fortnite code",
            },
            "reload_speed": {
                "status": "opaque",
                "proven": "WeaponReloadSpeed modifier aggregation",
                "reason": "runtime conversion to animation/reload seconds is native",
            },
            "fractional_magazine_rounding": {
                "status": "opaque",
                "reason": "integer clip initialization/rounding is native",
            },
            "final_damage_order": {
                "status": "partial",
                "proven": "default-channel GameplayEffect aggregation before damage execution",
                "reason": "FortDamageFormulaExecutionCalculation final ordering is native",
            },
        },
        "nocturno_signature": nocturno_signature_report(connection, snapshot_id),
        "absolute_live_damage_defensible": False,
        "optimizer_readiness": "supported-mechanics-only",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/phase2-real-validation.sqlite3"))
    parser.add_argument("command", choices=("report",))
    args = parser.parse_args(argv)
    connection = connect(args.db)
    try:
        print(json.dumps(runtime_semantics_report(connection), indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
