# Deterministic combat evaluation

`tools/stw_combat.py` is the first scoring-core vertical slice. It evaluates one exact
weapon variant, selected alteration in each specified slot, commander/support context,
and combat scenario from a single immutable asset snapshot. It never selects a build.

## Run the real Nocturno slice

```powershell
python tools/stw_combat.py --db data/phase2-real-validation.sqlite3 nocturno-demo
```

The demonstration evaluates three legitimate T05 ore Nocturno configurations: an
afflicted-target damage build, an explicit critical-hit build, and the afflicted build
with Rescue Trooper Ramirez as commander. To evaluate a specific configuration directly:

```powershell
python tools/stw_combat.py --db data/phase2-real-validation.sqlite3 evaluate `
  --variant WID_Assault_Auto_Founders_SR_Ore_T05 `
  --perk 0:aid_att_damage_t05 `
  --perk 1:aid_att_critdamage_t05 `
  --perk 2:aid_ele_energy_t05 `
  --perk 3:aid_att_firerate_ranged_t05 `
  --perk 4:aid_conditional_afflicted_dmgbonus_t05 `
  --perk 5:aid_g_weapon_onreload_explode `
  --commander "Rescue Trooper Ramirez" `
  --target-afflicted --window-mode burst --window-seconds 1
```

The JSON response contains normalized attributes, shot profiles, burst and sustained
metrics, every active/inactive contribution, source conditions, grant levels, curve
rows, source file hashes, assumptions, and explicit evaluation issues.

## Proven rules

The evaluator currently supports exact stat-table damage anchors (point blank, mid,
long, and max range), fire rate, magazine size, reload time, base critical chance,
critical-damage bonus, and headshot multiplier. It applies supported alteration and hero
GameplayEffects, hierarchical required/ignored source and target tags, literal and
curve-backed magnitudes, row-level linear interpolation, and Unreal Gameplay Ability
System default-channel additive/multiplicative aggregation. Burst DPS is the selected
shot profile times rounds per second. Sustained DPS is magazine damage divided by firing
time plus reload time.

These are catalog-stat damage units. They are useful for deterministic comparisons whose
unknown runtime factors are identical, but they are not a claim about final damage shown
against a live husk.

## Evidence boundary

Each result distinguishes facts from assumptions. Asset facts carry package/object or
data-row provenance and the source export hash. The versioned engine rule for default
GameplayEffect aggregation and the analytic fire-rate model are listed separately under
`assumptions`.

The shared-runtime audit now resolves exact CritRating and item-rating curve values, but
deliberately does not infer the native rule that combines or consumes them. See
[shared runtime combat semantics](shared-runtime-combat-semantics.md).

The evaluator deliberately does not infer:

- weapon item-level/DmgScale application, hero F.O.R.T. offense, mission scaling, enemy
  resistance, or elemental matchup multipliers;
- the native combination of the proven CritRating curve result with base crit chance;
- the conversion from modified `WeaponReloadSpeed` to runtime reload seconds;
- fractional magazine rounding;
- animation timing, projectile travel, spread, accuracy, or ammo economy;
- custom Blueprint/proc behavior such as Nocturno's reload explosion.

An explicit scenario value may supply observed effective crit probability, magazine
capacity, or reload seconds. Such a value is recorded as an assumption and never
presented as asset-derived. Partial and opaque mechanics remain issues and contribute no
invented damage.

## Scoring-core readiness

The evaluator is suitable as the deterministic kernel for builds composed only of its
supported mechanics and compared in an identical declared scenario. A future optimizer
must filter or clearly rank partial results separately. It is not yet suitable for a
global absolute-DPS optimizer until the runtime scaling, crit-rating, reload-speed, and
element rules are established and signature/custom mechanics receive dedicated models.
