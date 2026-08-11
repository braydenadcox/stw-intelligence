# Full deterministic loadout optimization

`tools/stw_optimizer.py` is the deterministic search/reasoning engine for complete STW
builds. It combines the existing combat evaluator with normalized commander/support
perks, team-perk eligibility, active abilities, weapon variants and legal slot options,
gadgets, signatures, elements/statuses, and enemy/mission context.

It is not the natural-language AI layer. It accepts one explicit scenario and returns
machine-readable recommendations that a future AI can explain conversationally.

## Example

```powershell
python tools/stw_optimizer.py `
  --db data/phase2-real-validation.sqlite3 `
  --weapon Nocturno `
  --enemy default__huskpawn_c `
  --mission /savetheworld/missions/primary/launchtheballoon/ridethestorm `
  --four-player `
  --elemental-storm Fire `
  --mission-modifier gm_enemy_maxhealthincrease_major `
  --objective burst_damage=2,crowd_clear=2,weapon_uptime=1 `
  --beam-width 128 `
  --max-results 10
```

Supported objectives are `burst_damage`, `sustained_damage`, `crowd_clear`,
`mist_monster_boss`, `survivability`, `healing_sustain`, `crowd_control`,
`ability_uptime`, `weapon_uptime`, and `condition_reliability`. Weights are normalized
inside one request; scores from different scenarios are never compared.

## Legality

- A commander cannot occupy a support slot, and all five supports are unique canonical
  hero identities.
- Team perks are admitted only when every structurally interpreted support-tag,
  class/keyword, count, tier, level, and rarity requirement is satisfied. The request
  makes hero progression explicit (max-tier Legendary by default).
- Weapon configurations select exactly one real alteration option per real slot and
  honor bidirectional alteration exclusions. Only maximum-tier variants are searched
  for a display-name preference unless an exact variant key is requested.
- Gadgets are unique and limited to two.

No filename convention creates legality.

## Search and scoring

The raw space is too large to enumerate. On snapshot 35, hero identity uniqueness alone
produces an upper bound of 789,082,947,288 commander/five-support combinations. Nocturno
has a 6,250,000 raw slot Cartesian upper bound, the Potshot 25,000,000, and Primal
Shotgun 18,750,000 across its maximum-tier variants. With 27 team perks and 28 gadget
pairs, Nocturno's complete unpruned upper bound exceeds 3.7e21 candidates.

The engine uses:

1. exact weapon-family/variant and slot legality filtering;
2. cached direct AbilityKit/GameplayEffect hero, team-perk, and gadget profiles;
3. objective/weapon/target semantic compatibility ordering;
4. commander-diverse beam search through five support slots;
5. team-perk requirement pruning;
6. bounded team-perk, gadget, and weapon expansion;
7. candidate deduplication and memoized combat evaluations;
8. Pareto dominance marking before weighted ranking.

The beam only decides which legal candidates reach the expensive evaluator. Every final
supported numeric combat component comes from `stw_combat.py`.

Numeric objective components are normalized across candidates in that one run and then
weighted. Non-DPS mechanics use supported semantic evidence units rather than invented
cross-mechanic damage values. Partial and opaque facts are retained as unquantified
components and are never inserted as zero. Definitive rankings and uncertainty-aware
recommendations are separate lists; an incomplete build cannot outrank a fully modeled
build by silently omitting its unknown mechanics.

## Real validation

A 24-candidate beam on snapshot 35 produced these sanity results:

| Scenario | Top uncertainty-aware result | Runtime | Result |
| --- | --- | ---: | --- |
| Nocturno crowd clear/weapon damage | Rescue Trooper Ramirez; T5 crystal Nocturno | 5.14 s | Rediscovered the proven assault-damage commander; Paint the Field remains opaque |
| Potshot vs Smasher | T5 ore Potshot | 7.27 s | Weapon resolved; material explosive/hover/custom hero interactions prevent a definitive known-build ranking |
| Primal Shotgun | T5 crystal Primal Shotgun | 5.15 s | Legal weapon resolved; signature and shotgun-specialist hero semantics remain materially opaque |
| Ability focus | Berserker Renegade / Cool Customer | 5.74 s | Supported evidence ranked, but active-ability runtime chains keep the recommendation non-definitive |
| Survivability/sustain | Gia / Bio-Energy Source | 5.62 s | No supported comparable numeric score; returned only as an uncertainty-aware semantic candidate |
| Crowd control | Primal Shotgun / Slow Your Roll / Slow Field | 7.17 s | Relevant control systems rediscovered; opaque/partial execution is explicit |

These are sanity checks, not hardcoded builds. The known Potshot, Primal Shotgun,
ability, sustain, and crowd-control archetypes remain non-definitive precisely where
their material hero, team-perk, gadget, signature, or native execution is incomplete.

## Output and boundaries

Every recommendation includes commander, five supports, team perk, weapon variant and
selected slot perks, two gadgets, granted commander abilities, scenario, normalized
weights, supported score components, unquantified partial/opaque components, confidence,
active combat contributions, limiting conditions, complete evaluator output, and source
file/hash provenance.

Absolute live-game DPS is still not claimed. Native item/F.O.R.T./mission/enemy scaling,
final elemental resistance conversion, and Blueprint-controlled signature sequencing
remain explicit limitations. The optimizer is trustworthy for directly comparable
catalog-stat results and for uncertainty-aware semantic recommendations under one fixed
scenario.
