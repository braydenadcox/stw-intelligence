# Team-perk interaction semantics

The interaction catalog treats `FortTeamPerkItemDefinition` as the structural team-perk
identity. It links each identity to its explicitly granted `FortAbilityKit`, preserves the
engine loadout-condition records, and reuses the existing AbilityKit, GameplayAbility,
GameplayEffect, magnitude, curve, tag, inheritance, mechanic, opaque-boundary, and source
provenance tables.

The installed Fortnite 41.30 package index contains 27 team-perk identities. The controlled
acquisition scope is `/SaveTheWorld/Abilities/Player/Perks/Leader`, containing 257 packages.
Raw exports remain in FModel's external export directory and are ignored by Git.

## Reports

```powershell
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 team-perks
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 team-perk "Blast from the Past"
```

The detail report includes identity text, requirement text, exact eligibility rules, support
slot count, tier/level/rarity gates, granted or removed abilities/effects, normalized
mechanics and modifiers, provenance hashes, opaque Blueprint boundaries, and an exact
deduplicated dependency queue.

## Evidence boundary

- `supported` eligibility means the exported tag query and every active tier/level/rarity
  constraint are structurally represented. The original query payload is retained.
- `partial` semantics means useful static facts exist, but activation, value assignment, or
  effect application crosses an unresolved or Blueprint-controlled boundary.
- `opaque` means the structural identity and grants are known but no complete static effect
  interpretation is available. Opaque mechanics remain first-class; they are never valued
  as zero.
- A team perk is fully supported only when its transitive semantic graph contains supported
  facts and no partial, opaque, or unresolved semantic boundary.

Gameplay-event registrations, source/target tags, durations, periods, application chances,
stack type/cap/reset policies, cooldown inputs, set-by-caller tags, execution definitions,
and effect modifiers are preserved through the shared interaction tables. Compiled Blueprint
execution is explicitly recorded as `blueprint_execution`; the catalog does not pretend to
execute Blueprint bytecode.

## Current real-build measurement

Snapshot 26 (`interaction-v3`) measures:

- 27/27 identities and AbilityKits cataloged;
- 27/27 eligibility compositions supported;
- 55/55 direct semantic grants resolved;
- 175 supported, 247 partial, and 56 opaque interaction-fact occurrences across the
  per-perk transitive graphs;
- all 27 perks partially interpretable, with none falsely labeled fully supported;
- three deduplicated priority-zero shared GameplayEffect templates still queued.

The mechanically diverse real checks include Blast from the Past (health/shield and
Blueprint application), Bio-Energy Source (set-by-caller shield healing), Happy Holidays
(cooldown modifiers), Totally Rockin' Out (conditional proc behavior), Hot Swap
(four-class support composition), Phase Blaster (gameplay events/periodic damage), and
Trick and Treat (proc/life-leech behavior).
