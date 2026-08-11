# Enemy and mission context semantics

This phase adds canonical `TargetContext` and `MissionContext` inputs over the shared
asset graph. It does not introduce a second combat interpreter and it does not invent
Fortnite's native health, damage, AI, or spawning formulas.

## Inspecting the catalog

```powershell
python tools/stw_context.py --db data/phase2-real-validation.sqlite3 coverage
python tools/stw_context.py --db data/phase2-real-validation.sqlite3 enemy default__huskpawn_c
python tools/stw_context.py --db data/phase2-real-validation.sqlite3 mission /savetheworld/missions/primary/launchtheballoon/ridethestorm
python tools/stw_context.py --db data/phase2-real-validation.sqlite3 modifier "Husk Heartiness (Major)"
python tools/stw_context.py --db data/phase2-real-validation.sqlite3 scenario default__huskpawn_c --objective /savetheworld/missions/primary/launchtheballoon/ridethestorm --four-player --modifier gm_enemy_maxhealthincrease_major
```

## Evidence model

Enemy identity is established by a pawn default's `PawnStatHandle` or explicit pawn
inheritance. Character, attack, element, immunity, and status tags remain exact. Stat
table rows, granted ability sets, movement facts, damage zones, and parent variants are
stored separately with source-object and file provenance. Common community groupings
such as “Mist Monster” are not assigned unless a structural Fortnite tag or relation
proves them.

Mission-generator variants are grouped by their `PrimaryMissionInfo` reference, not by
filename. Gameplay modifiers preserve their localized identity, delivery requirements,
target team/scope, granted AbilityKits, GameplayEffects, mutators, and resolution state.
Encounter option sets preserve exact generation facts, while encounter modifiers retain
their tags and difficulty-cost table rows.

`scenario` evaluates only structural applicability. A requirement that is satisfied is
reported as applying; an explicit ignore/exclusion tag is reported as excluded; missing
target facts remain unknown. Four-player, power-level, elemental-storm, and modifier
inputs are retained even where the final calculation crosses native code.

## Automatic acquisition

[`asset-acquisition-enemy-mission-context.json`](../fixtures/asset-acquisition-enemy-mission-context.json)
is the reproducible acquisition manifest. It targets enemy pawn gameplay families,
gameplay modifiers and mutations, encounter difficulty options, mission generators, and
the narrow STW balance-table family. Maps, meshes, textures, audio, dialogue, UI, and
general environment content are excluded. Raw FModel exports remain outside Git.

## Real catalog coverage

Real snapshot 35 (`interaction-v8`) contains 46 structurally distinct enemy pawn
archetypes, 100 primary mission identities represented by 126 generator variants, 188
gameplay modifiers, 177 encounter option sets, and 9 exact tagged encounter modifiers.
The enemy set covers normal/Husky/special Husks, Smasher, Taker, Flinger, Blaster,
Shielder, miniboss variants, Starlight bosses, and the four-player Storm King identity.
The common “Mist Monster” umbrella is not fabricated because the inspected pawn graph
does not grant one canonical grouping tag.

Of 106 enemy ability-set grants, 103 resolve (97.17%). The remaining three all point to
one Wargames-only Zapper package. Seventeen of 46 pawn identities resolve an exact row
from `CharacterAttributesAI`; other variant row names remain partial rather than being
silently mapped to a similar base row. Interaction classification totals 164 supported,
62 partial, and 25 opaque facts. Gameplay modifiers specifically are 157 supported, 6
partial, and 25 opaque.

The acquisition manifest matches 2,314 relevant packages and exported them with zero
failures. The final snapshot contains 18,002 files and 60,951 objects. Full catalog
normalization stages took about 365 seconds (about 402 seconds wall-clock including
ingestion); the new enemy/mission context stage itself took
1.27 seconds. The focused six-test scenario suite runs in about 0.8 seconds and the full
104-test project suite in about 20 seconds.

## Runtime boundaries

The catalog exposes proven inputs but deliberately leaves these boundaries partial or
opaque:

- final enemy health, shield, armor, and outgoing-damage scaling in native formulas;
- AI decision-making, encounter composition, and runtime spawn selection;
- Blueprint/native special attacks, bosses, and miniboss sequencing;
- mission runtime state and dynamically selected modifier combinations.

This is sufficient structured scenario context for a deterministic optimizer to filter,
compare, and explain supported interactions without treating unknown mechanics as zero.
