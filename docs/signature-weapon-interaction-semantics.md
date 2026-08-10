# Signature weapon interaction semantics

Signature discovery is structural. Every alteration offered by slot ordinal five is a
sixth-perk identity. Outside that slot, an alteration is included only when a
non-respeccable weapon slot grants an exact AbilityKit with runtime ability behavior or an
explicit opaque boundary. Display names, descriptions, and package-name conventions are not
used to decide identity or ownership.

Each identity reuses the normalized alteration, AbilityKit, GameplayAbility,
GameplayEffect, execution, magnitude, curve, gameplay-tag, inheritance, provenance, and
opacity layers. The ownership table records every eligible weapon family, variant, slot,
rarity option, and linked schematic without copying semantic facts onto weapon rows.

## Commands

```powershell
python tools/stw_signatures.py --db data/phase2-real-validation.sqlite3 signatures
python tools/stw_signatures.py --db data/phase2-real-validation.sqlite3 signature "Paint the Field"
python tools/stw_signatures.py --db data/phase2-real-validation.sqlite3 signature "Super Shot"
python tools/stw_signatures.py --db data/phase2-real-validation.sqlite3 signature "aid_g_onkill_chainlightning"
```

The existing controlled weapon acquisition manifest is sufficient:

```powershell
python tools/stw_asset_acquisition.py export fixtures/asset-acquisition-weapon-semantics.json
```

It covers the weapon, alteration, data-table, and weapon-ability families. Signature-specific
semantic closure on snapshot 31 found zero missing priority 0-2 packages, so this phase
required no additional raw export. Raw Fortnite assets remain read-only and outside Git.

## Real-build coverage

Snapshot 31 (`interaction-v6`) contains:

- 155 canonical signature identities: 151 sixth-slot perks and 4 non-respeccable intrinsic
  signatures;
- ownership across 398 of 415 weapon families (95.9%) and 3,777 eligible variants;
- 8 fully supported, 144 partial, and 3 wholly opaque transitive interaction graphs;
- 943 supported, 645 partial, and 70 opaque normalized interaction facts;
- 109 explicit Blueprint/native boundaries;
- 56.9% fully interpretable interaction-fact coverage and 98.1% supported-or-partial
  identity coverage;
- zero unresolved signature dependencies.

The uncovered weapon families do not have a structurally qualifying sixth/intrinsic slot in
the current normalized build. They are not assigned a signature by name or reputation.

## Representative validation

| Weapon / mechanic | Structurally proven examples | Remaining boundary |
| --- | --- | --- |
| Nocturno — Paint the Field | damage/reload events, stored mark effect, 65% damage factor per mark, 500 source-aggregated stacks, 256-unit radius, element-matched SetByCaller effects | recording hit damage, detonation/removal sequencing, death handling, native final damage |
| Founder's Deconstructor — Super Shot | reload-linked damage stack, five-stack cap and normalized duration/stack policy | first-hit consumption and reload-count sequencing |
| Vacuum Tube Bow — chain lightning | damage trigger, stat handle, target/effect containers, executions, coefficients and periodic facts | chain target selection and projectile/runtime traversal |
| Founder's Revolt — Chain Bullet | canonical sixth-slot ownership and explicit Blueprint boundary | secondary-target selection and chained bullet execution |
| Primal Flame Bow — fire spread | damage trigger, fire/status effects, executions, periods, stack and target conditions | nearby-target selection and affliction propagation order |
| Candy Corn LMG — Candy! | elimination-oriented ability graph, cooldown/effect facts and spawned pickup entity | proc decision, pickup placement and consumption behavior |
| Blizzard Blitzer — hit-streak freeze | damage events, seven-hit stack mechanics, cooldown effect, frozen/status tags, boss/recent-freeze exclusions | hit counter and freeze application control flow |
| Storm King's Fury — Meteor Slam charge | non-respeccable intrinsic ownership, effect map, seven-stack policy and modifiers | meteor spawning, stack-to-meteor conversion and slam execution |
| The Instigator — Hunter's Mark | intrinsic ownership, trigger, 10-second effect, damage-taken modifier and boss condition | bow-charge threshold and debuff application flow |

## Nocturno improvement

Paint the Field is no longer only a standalone runtime special case. It is now a canonical
signature identity linked to the Nocturno weapon family, all seven craftable variants, all
seven corresponding schematics, 15 transitive GameplayEffects, 30 event-relevant mechanics,
30 interaction tags, and complete source hashes. Its specialized 0.65 factor, 256-unit
radius, 500-stack mark policy, event triggers, and element-matched damage paths remain
available, while the same report exposes seven honest Blueprint/native boundaries.

## Runtime limits and readiness

Cooked Blueprint event ordering, native damage execution, target selection, chained
projectile traversal, spawned-actor behavior, proc decisions, and some mark/stack cleanup
cannot be executed statically. These mechanics remain first-class partial or opaque facts;
they are never treated as zero.

The shared interaction model is mature enough to proceed to elements and status effects.
That phase should establish the common elemental matchup and status lifecycle semantics used
by these signature graphs rather than adding signature-specific guesses.
