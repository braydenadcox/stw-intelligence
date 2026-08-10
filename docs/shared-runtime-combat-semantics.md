# Shared runtime combat semantics

This phase audits the boundary between cooked Fortnite asset facts and behavior compiled
into native `/Script/FortniteGame` code. Run the machine-readable report with:

```powershell
python tools/stw_runtime.py --db data/phase2-real-validation.sqlite3 report
```

## Proven lookups

- `Item.All.CritRatingToCritChance` is an asset-backed curve. The evaluator now resolves
  the exact curve value for total `CritRating`, with source hash and row provenance.
- A schematic's explicit rating row resolves item level to displayed item rating. This
  does not prove how `DmgScale` changes weapon damage.
- `HomebaseRatingDifficultyMapping` resolves a Homebase rating to a difficulty bucket.
- `GameDifficultyGrowthBounds` exposes exact mission difficulty, rating, loot, and stat
  clamp rows for each named zone definition.
- Default-channel GameplayEffect modifier aggregation remains supported before the
  native damage execution boundary.

These are lookup rules, not permission to infer the native code that consumes them.

## Native boundaries

Cooked assets explicitly point final damage to
`FortDamageFormulaExecutionCalculation`, offense mapping to
`FromOffenseModMagnitudeCalculation`, and weapon stat initialization to native Fortnite
systems. CUE4Parse can export the class references and configured coefficients, but the
native implementations are not Unreal assets and cannot be exported by FModel.

Consequently, the following remain partial or opaque:

- `BaseLevel` and `DmgScale` application to damage;
- the inner offense calculation (the outer mapping proves
  `1 + 0.01 * FromOffense(...)`);
- application of mission difficulty to enemy attributes and final damage;
- elemental attack/target matchup multipliers;
- combining the CritRating curve result with a weapon's `DiceCritChance`;
- converting `WeaponReloadSpeed` into animation/runtime seconds;
- fractional magazine rounding;
- the final calculation order inside `FortDamageFormulaExecutionCalculation`.

The evaluator exposes proven intermediate values and keeps final live damage null where
these boundaries matter. Scenario overrides remain labeled assumptions.

## Nocturno signature

Paint the Field is classified as **partially statically modelable**. Real assets prove:

- triggers on damage dealt and reload;
- a constant 0.65 stored-damage factor per mark;
- a 256 Unreal-unit sphere radius;
- infinite marks aggregated by source with a 500-stack cap;
- physical/fire/water/nature/energy SetByCaller damage effects selected by weapon element;
- target filters and exclusion of alteration AOE damage from recursively marking.

The cooked Blueprint export does not contain executable graph bytecode. The event control
flow that records hit damage, sequences explosions, handles target death, and clears marks
therefore remains opaque. Final explosion damage also enters the native damage execution.
The evaluator must not add signature damage to DPS until those runtime inputs are supplied
or independently validated.

## Readiness

Absolute live-game DPS is not defensible from cooked assets alone. The evaluator is ready
for combinatorial search only when the optimizer restricts candidates to supported
mechanics, preserves a common scenario, and separates partial/opaque builds rather than
ranking their missing damage as zero.
