# Weapon and schematic catalog

The local asset pipeline catalogs craftable ranged and melee weapons without copying raw
Fortnite exports into Git or treating filenames as semantic evidence. It keeps five
separate layers:

1. a friendly weapon identity for browsing;
2. every structurally identified weapon-definition variant;
3. its referenced stat-table row and perk-slot loadout;
4. schematics linked through explicit recipe-result primary asset IDs;
5. alteration definitions linked to their ability/effect semantics where exported.

Use the real validation database explicitly:

```powershell
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 weapon-coverage
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 weapons --query "Nocturno"
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 weapon "Nocturno"
```

`weapon-coverage` reports structural resolution separately from semantic coverage.
`weapon_schematic_link_resolution` measures whether weapon-producing recipes identify
exactly one weapon variant. `weapon_variant_stat_and_slot_resolution` requires both the
referenced stat row and slot loadout. `alteration_static_semantic_coverage` includes only
alterations whose exported ability/effect chain has a supported static interpretation;
partial and opaque Blueprint behavior is never promoted to a guessed value.

Friendly identities group localized display name plus weapon kind for useful queries.
They are not authoritative gameplay equivalence classes. The report counts identity
groups containing multiple actor classes so downstream optimization can retain variant
boundaries whenever implementations differ.

Raw exports stay read-only in FModel's output directory. SQLite snapshots preserve their
paths, hashes, build metadata, source objects, source table rows, and unresolved reference
queue. Re-ingesting an unchanged snapshot is idempotent; a normalizer upgrade re-derives
only catalog rows and preserves the immutable raw reference graph.
