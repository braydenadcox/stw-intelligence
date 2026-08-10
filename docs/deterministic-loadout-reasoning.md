# Deterministic loadout reasoning

This is the first consumer of the normalized hero/perk catalog. It searches only facts
grounded in the selected asset snapshot and only recommends perk families classified as
`optimization_ready`. Partial and opaque matches are returned separately with their exact
blockers; they are never silently treated as understood.

It is a semantic relevance engine, not a DPS simulator. It does not yet model weapons,
enemies, team perks, mission scaling, hero ownership, or custom Blueprint behavior.
Consequently, an honest result may contain fewer than five support heroes.

Start by listing the exact vocabulary available in the current resolved catalog:

```powershell
python tools/stw_loadouts.py `
  --db data/phase2-real-validation.sqlite3 `
  vocabulary
```

Search for perk families whose normalized modifier proves both the attribute and its
condition on the same fact:

```powershell
python tools/stw_loadouts.py `
  --db data/phase2-real-validation.sqlite3 `
  search `
  --attribute OutgoingAbilityDamage `
  --tag Weapon.Ranged.Assault
```

That query currently reproduces Rescue Trooper Ramirez's `AssaultDamage` chain and emits
the T01/T02 curve points (`1.17` and `1.33`) with package paths and source hashes.

Assemble a commander/support result from exact matches:

```powershell
python tools/stw_loadouts.py `
  --db data/phase2-real-validation.sqlite3 `
  recommend `
  --attribute OutgoingAbilityDamage
```

Flags may be repeated. Required attributes, tags, and mechanics use AND semantics.
Preferred terms order otherwise eligible matches by the number of exact matches; they
never turn magnitudes from unlike mechanics into a fabricated power score. With no
required terms, at least one preferred term must match.

Recommendation evidence is deliberately narrower than the catalog's transitive semantic
closure. Only the perk kit and assets it directly grants establish perk ownership. This
prevents the effects of an active ability referenced by a cooldown perk from being
misreported as effects granted by that perk.
