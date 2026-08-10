# Gadget interaction semantics

The gadget catalog treats `FortHomebaseNodeItemDefinition.LevelData[].AbilityKit` as the
structural proof that a gadget is player-selectable. It does not infer identities from the
large generic gadget folder, which also contains debug, onboarding, vehicle, and internal
systems. The granted AbilityKit then enters the same reference graph and normalized
GameplayAbility, GameplayEffect, magnitude, curve, tag, execution, condition, and opacity
model used by hero abilities and team perks.

## Commands

```powershell
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadgets
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadget "Adrenaline Rush"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadget "Air Strike"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadget "Banner"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadget "Hover Turret"
python tools/stw_interactions.py --db data/phase2-real-validation.sqlite3 gadget "Supply Drop"
```

The controlled acquisition manifest is
[`asset-acquisition-gadgets.json`](../fixtures/asset-acquisition-gadgets.json). It covers the
smallest practical 215-package runtime family, the eight exact unlock nodes, and the exact
legacy/runtime dependencies discovered by recursive semantic closure. Raw exports remain
read-only and outside Git.

## Real-build coverage

Snapshot 31 (`interaction-v5`) proves:

- 8/8 selectable gadget identities: Adrenaline Rush, Air Strike, Banner, Hover Turret,
  Proximity Mine, Slow Field, Supply Drop, and Teleporter;
- 48/48 upgrade levels structurally cataloged, with six levels and exact minimum commander
  levels per gadget;
- 8/8 base levels structurally interpreted;
- 40 upgrade rows retained as explicit partial references because the node stores only row
  names and does not identify the owning table;
- 77 supported, 219 partial, and 5 opaque normalized interaction facts;
- 25.6% of normalized interaction facts fully interpretable and 100% of gadget identities
  known at least partially;
- zero unresolved priority 0-2 dependencies in the transitive gadget closures;
- 8 partial, 0 fully supported, and 0 wholly opaque gadget identities.

Structural identity coverage is 100%. Fully supported identity coverage is intentionally
0%: every gadget crosses cooked Blueprint/native behavior, and all upgraded levels depend on
unowned GameplayEffect row names. This is an honest limitation, not missing catalog data.

## Representative validation

| Gadget | Structurally proven examples | Remaining boundary |
| --- | --- | --- |
| Air Strike | cooldown effect, exact damage-stat handle, targeting/effect containers, shared direct-ballistic damage template | strike scheduling, target selection, and impact execution |
| Adrenaline Rush | cooldown effect, heal-related stat handle, periodic/execution facts, attribute references, radii and spawned entity references | recipient selection and Blueprint orchestration |
| Banner | deployable actor, building buffs, duration/stacking, respawn effect, modifiers and execution | placement, building attachment, respawn orchestration |
| Hover Turret | charge/ammo cost definition, cooldown, damage stat handle, duration and firing parameters | spawned turret targeting/firing loop |
| Slow Field | cooldown, duration, area/effect containers, blocking/cancel tags | placement and per-target runtime application |
| Supply Drop | activation condition, cooldown, duration, stacking and team-utility modifier | spawned loot contents and delivery orchestration |

Proximity Mine and Teleporter are also cataloged through closed graphs, with their
deployable/charge, linking, and runtime boundaries retained as partial or opaque facts.

## Readiness

The shared interaction model is mature enough to proceed to signature weapon effects and
sixth perks. Gadget selection and known interactions can become first-class future loadout
inputs, while the eventual evaluator must preserve partial/opaque gadget contributions and
must not treat them as zero.
