# Element and status interaction semantics

The element/status phase builds canonical identities over the existing asset graph; it
does not introduce a second mechanics interpreter. The same GameplayEffect modifiers,
magnitudes, curves, conditions, executions, tags, stacking facts, provenance records,
and opaque boundaries used by heroes, team perks, abilities, gadgets, alterations, and
signature weapons remain authoritative.

## Inspecting the catalog

```powershell
python tools/stw_elements.py --db data/phase2-real-validation.sqlite3 coverage
python tools/stw_elements.py --db data/phase2-real-validation.sqlite3 element Water
python tools/stw_elements.py --db data/phase2-real-validation.sqlite3 status Afflicted
python tools/stw_elements.py --db data/phase2-real-validation.sqlite3 status Frozen
```

The real build currently establishes five damage identities. Localized alteration data
proves the player-facing names while exact gameplay tags preserve Fortnite's internal
vocabulary:

| Player-facing identity | Internal damage tag |
| --- | --- |
| Fire | `Gameplay.Damage.Elemental.Fire` |
| Water | `Gameplay.Damage.Elemental.Ice` |
| Nature | `Gameplay.Damage.Elemental.Lightning` |
| Energy | `Gameplay.Damage.Physical.Energy` |
| Physical | `Gameplay.Damage.Physical` |

Water/Ice and Nature/Lightning are therefore aliases supported by real alteration
display data and structural tags, not filename conventions.

## Real coverage

Snapshot 33 (`interaction-v7`) contains 5 elemental identities and all 36 observed
`Gameplay.Status.*` identities. These include six affliction identities, snare, three
slow identities, two stun identities, knockback, freeze, vulnerability, five immunity
identities, and four regeneration-block identities. Exact status tags remain distinct;
parent relationships (for example, elemental affliction under affliction) are explicit.
Across element/status-bearing assets the shared interpreter reports 495 supported, 797
partial, and 313 opaque interaction facts. Identity/lifecycle classification remains
conservative: 1 status is fully supported and 35 are partial because at least one real
application or downstream behavior is not statically closed.

The report connects tag occurrences back through the structural owner graph for weapons,
weapon perks, signatures, hero perks, team perks, active abilities, and gadgets. The
focused enemy elemental asset family adds real Fire, Water, and Nature enemy grants,
elemental pawn DoT/snare effects, and resistance/vulnerability effects. Physical remains
the absence/default damage type in relevant enemy rules rather than a fabricated enemy
element grant.

Nocturno exposes four selectable elemental perk identities. Physical is not reported as
a fifth selectable alteration because no explicit Physical element alteration is present
in its slot graph.

## Matchup facts and runtime boundary

`CombatEffects_NPC` proves constant additive `DamageResistance` facts:

- default: `0.50`
- matching-element adjustment: `-0.17` (total `0.33`)
- Energy adjustment: `-0.25` (total `0.25`)
- weak-element adjustment: `+0.25` (total `0.75`)
- strong-element adjustment: `-0.50` (total `0.00`)

The three enemy element effects structurally associate those rows with exact attacker
tags, establishing all 15 default/matching/Energy/weak/strong conditional rules. The same
table proves freeze-duration multipliers of `1.25` (weak), `1.00` (neutral), and `0.75`
(strong).

These are resistance and duration-aggregation facts. The final conversion from aggregated
`DamageResistance` to live damage remains inside native
`FortDamageFormulaExecutionCalculation`; the catalog deliberately does not turn these
values into community-known damage multipliers.

## Acquisition and boundaries

[`asset-acquisition-element-status.json`](../fixtures/asset-acquisition-element-status.json)
is the reproducible automatic acquisition manifest. It exported 30 packages with zero
failures: the 24-package NPC elemental family plus six graph-proven balance and resistance
dependencies. Visual particle/cue inheritance was excluded because it does not improve
semantic interpretation. Raw FModel output stays outside Git.

Status application, duration, period, chance, stacking, refresh-policy, conditions,
modifiers, and executions are retained when present. A status remains partial when its
application or outcome crosses an execution, Blueprint, or native-code boundary; missing
behavior is never treated as zero.

This interaction layer is mature enough to proceed to enemy archetypes and mission
modifiers. Those phases should attach target identities and scenario context to these
shared tags/rules rather than redefine element or status semantics.

On the current real catalog, the complete coverage report runs in about 1.7 seconds. The
final snapshot ingested 15,724 files and 40,197 objects; the full rebuild took about 272
seconds, dominated by pre-existing weapon and magnitude normalization rather than the
new element/status step (about 0.02 seconds).
