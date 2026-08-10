# Hero active-ability semantics

The active-ability catalog uses explicit `TierAbilityKits` references from
`FortHeroGameplayDefinition` and `ClassAbilityKits` references from
`FortHeroClassGameplayDefinition`. It does not infer an ability identity or classify a
class kit from its filename. Reports keep `hero_loadout` and `hero_class` grants separate.

Each canonical AbilityKit identity is linked through the shared graph to granted gadgets,
GameplayAbilities, GameplayEffects, inheritance, curves, data-table rows, tags, effect
containers, triggers, costs, cooldown effects, durations, periods, stacking, executions,
set-by-caller magnitudes, conditions, status tags, and source hashes. Numeric Blueprint
defaults are retained as partial structural parameters; the catalog never pretends their
runtime use is proven merely because a variable name and value are visible.

## Commands

```powershell
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 abilities
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 ability "Goin' Commando!!!"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 ability "Frag Grenade"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 ability "B.A.S.E."
```

The controlled seed acquisition manifest covers the four STW active-ability trees and
the class-kit tree:

```powershell
python tools/stw_asset_acquisition.py export fixtures/asset-acquisition-active-abilities.json
python tools/stw_asset_acquisition.py export fixtures/asset-acquisition-active-abilities.json --confirm-export
```

The first command is preview-only. On Fortnite 41.30 the five scopes match 513 packages.
After ingestion, the transitive report queue contains only exact unresolved priority 0–2
references. Raw exports remain outside Git and are never modified by normalization.

## Real-build coverage

Snapshot 26 (`interaction-v4`) contains:

- 29 canonical identities proven by gameplay-definition grants;
- 20 selectable hero-loadout ability identities across 216 canonical heroes;
- 9 class-granted kit identities, including B.A.S.E.;
- 657/657 structural grants resolved (648 hero-loadout grants and 9 class grants);
- 71/71 direct AbilityKit semantic grants resolved;
- 35/35 damage-stat handles resolved to their exact data-table rows;
- 780 supported, 1,441 partial, and 215 opaque interaction-fact occurrences;
- 0 fully supported, 29 partial, and 0 wholly opaque identities;
- zero deduplicated missing priority 0–2 dependencies in the active-ability closures.

All identities remain partial because the cooked Blueprint/native execution boundary is
real. Static facts are useful and fully proven where labeled supported, but activation
flow, target selection execution, spawned actors, projectile behavior, and some coefficient
application order cannot be reconstructed by executing cooked Blueprint graphs.

## Diverse validation cases

| Ability | Structurally proven examples | Remaining boundary |
| --- | --- | --- |
| Goin' Commando | 10-second default duration, cost/cooldown curve rows, exact ranged-weapon stat row, tags and effect containers | weapon spawning/firing and end-flow Blueprint execution |
| Frag Grenade | two costs, cooldown curve, exact gadget stat row, 256/512 radius defaults, targeting containers and periodic effects | projectile/cluster/impact execution |
| Shockwave | cost/cooldown curves, exact gadget stat row, 512/768 radius defaults, tags, effects and executions | movement/landing and hit application flow |
| Dragon Slash | cost/cooldown curves, exact gadget stat row, targeting containers, DOT periods and range/radius curves | movement path and target-hit Blueprint flow |
| T.E.D.D.Y. | fragment cost, cooldown curve, exact bear-tower stat row, 15-second lifespan default, range/fire-rate/damage inputs | spawn, targeting, firing loop and perk orchestration |
| B.A.S.E. | class grant, exact containment-field stat row, external perk ability, effects, attributes, periods and executions | placement, building attachment and external Blueprint coordination |

This is mature enough to reuse for gadgets: the shared representation now covers identity,
grants, stat handles, costs, cooldowns, conditions, effects, periodic behavior, execution
definitions, provenance, and honest opacity. Gadget work should add gadget-specific identity
and ownership/catalog coverage, not another semantic engine.
