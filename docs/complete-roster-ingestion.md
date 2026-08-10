# Controlled complete-roster ingestion

This workflow expands the validated hero/perk catalog without tracing heroes one at a
time. Raw FModel exports remain read-only and outside Git. Folder selection controls the
export scope; only explicit references inside exported assets become semantic evidence.

## Commands

```powershell
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 batch-plan
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 roster
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 perk "AssaultDamage"
```

After each FModel batch, ingest the same export root again. Its changed manifest creates
a new immutable snapshot, and `batch-plan` recomputes deduplicated follow-up folders from
the new graph.

After all five batches have actually been exported recursively and ingested, record the
snapshot-scoped receipt:

```powershell
python tools/stw_assets.py --db data/phase2-real-validation.sqlite3 roster-receipt `
  --confirm-complete-recursive-export
```

The command refuses to record a receipt when any required scope is absent. The receipt
does not invent completeness: it preserves the operator confirmation alongside the exact
immutable snapshot whose folder presence was verified.

## Ordered FModel batches

### 1. Canonical gameplay identities

Export these four folders recursively:

```text
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Commando/GameplayDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Constructor/GameplayDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Ninja/GameplayDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Outlander/GameplayDefinition
```

Relevant type: `FortHeroGameplayDefinition`. This establishes one canonical identity per
HGD and discovers support/commander perks from the HGD's own references.

### 2. HID variants and friendly names

Export these four folders recursively:

```text
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Commando/ItemDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Constructor/ItemDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Ninja/ItemDefinition
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Heroes/Outlander/ItemDefinition
```

Relevant type: `FortHeroType`. Each HID is linked through its explicit
`HeroGameplayDefinition` reference. Rarity and evolution variants never become separate
heroes. Unmapped HIDs remain visible in coverage.

### 3. Hero perk implementations

Export these two folders recursively:

```text
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Abilities/Player/Perks/Hero
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/Abilities/Player/Perks/Leader
```

Relevant types: ability kits, GameplayAbility Blueprint defaults, and GameplayEffects.
These two families replace hundreds of individual hero-by-hero exports.

### 4. Shared semantic bases

Export these folders recursively:

```text
FortniteGame/Plugins/GameFeatures/SaveTheWorld/Content/GameplayEffectTemplates
FortniteGame/Content/GameplayEffectTemplates
FortniteGame/Content/Abilities/Player/Parents
```

These close inherited damage, healing, duration, cooldown, modifier, and generic
triggered-ability semantics shared by many perks.

### 5. Shared balance values

Export this asset:

```text
FortniteGame/Content/Balance/DataTables/CombatEffects_HeroAbilities.uasset
```

This resolves curve-backed magnitudes, chances, durations, and thresholds.

## Coverage rules

- `resolved`: supported structural facts exist and no known semantic dependency or
  opaque behavior blocks interpretation.
- `partial`: useful facts exist, but an exact dependency or Blueprint/custom behavior
  remains unresolved.
- `opaque`: the chain is structurally known, but no currently supported semantic facts
  explain its behavior.
- `optimization_ready` is true only for `resolved` perk families.

The catalog never claims complete-roster awareness merely because some heroes are
present. Completion requires all controlled export scopes to be observed in the snapshot,
all four hero classes, both perk modes for every HGD, and every exported HID mapped to an
HGD. Missing scopes and orphan HIDs are reported explicitly.

## Explicit exclusions

UI icons, portraits, cosmetics, feedback, frontend animations, XP/sacrifice tables, and
unrelated active-ability dependencies are not part of the hero-perk roster batches.
