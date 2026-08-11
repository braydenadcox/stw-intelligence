# Fortnite Save the World Golden QA Benchmark — 2026 Evidence Edition

Version: 2.0  
Evidence cutoff: August 2, 2026  
Primary expert baseline: MistaBeastxGames  
Companion files: `stw_golden_qa_sources_2026.yaml`, `stw_golden_qa_cases_2026.yaml`

## Outcome

This benchmark turns 26 video transcripts into a versioned QA layer for an STW optimizer. It is designed to catch broken mechanics, invalid loadouts, missing constraints, and implausible ranking changes without baking a YouTube tier list into the product.

The suite contains 85 cases across 18 domains:

| Enforcement | Cases | Release behavior |
|---|---:|---|
| Hard invariant | 49 | Block after the referenced asset/runtime fact has been verified |
| Ranking expectation | 19 | Flag for review; do not auto-block solely on creator opinion |
| Contextual | 11 | Enforce only when the named event, mission, objective, or constraint is active |
| Quarantine | 6 | Never use as current truth until a fresh runtime or asset check passes |

Case status is separate from enforcement: 78 are active, 5 are quarantined, and 2 are informational historical cases.

## What this benchmark is — and is not

It is a regression oracle for questions such as:

- Did the optimizer remember elemental counters and wall-material weaknesses?
- Did it distinguish a weapon's raw performance from its performance with a full hero loadout?
- Did it account for reload, magazine, cooldown, elemental lock, range, geometry, and activation requirements?
- Did it accidentally apply an event modifier in a normal mission?
- Did it promote a suspected bug or a showcase damage number into a permanent formula?

It is not a replacement for game assets, runtime tests, or patch-aware data ingestion. Exact perk values, cooldowns, damage numbers, and undocumented interactions should come from the current game build whenever possible. Expert videos provide hypotheses, comparisons, failure modes, and sanity checks.

## Evidence and currentness policy

The benchmark uses four independent questions for every claim:

1. **When was the evidence published?** A 2026 source is eligible for current QA. A 2025 source is historical until revalidated.
2. **What kind of claim is it?** Asset value, observed runtime behavior, build recommendation, or subjective ranking are not interchangeable.
3. **How should failure behave?** Hard block, review warning, scenario-only check, or quarantine.
4. **What would verify it?** Current asset inspection, controlled runtime test, model-behavior review, or simple mathematics.

A publication date does not by itself prove every number. A 2026 video can still describe an undocumented or bugged interaction; the Black Drum is the clearest example.

## Source ledger

| ID | Date | Video | Evidence role | Currentness |
|---|---|---|---|---|
| S01 | 2026-03-13 | [2026: Ranking EVERY ASSAULT RIFLE](https://www.youtube.com/watch?v=kfZLGNeHZrU) | Comparative tier test; raw vs loadout | Current 2026 |
| S02 | 2026-04-10 | [Ranking EVERY SHOTGUN](https://www.youtube.com/watch?v=yvZpBSw73mk) | Comparative tier test; burst vs sustained | Current 2026 |
| S03 | 2026-04-29 | [Ranking EVERY CONSTRUCTOR](https://www.youtube.com/watch?v=2q4UMrSpbAo) | Commander/support and scenario ranking | Current 2026 |
| S04 | 2026-05-05 | [Ranking EVERY GADGET](https://www.youtube.com/watch?v=KZ_vlEbluGE) | Gadget utility and cooldown comparison | Current 2026 |
| S05 | 2026-08-02 | [Chaos Exploder with Crackshot is FANTASTIC!](https://www.youtube.com/watch?v=BdAiaRD5fww) | Follow-up practical build test | Current 2026 |
| S06 | 2026-07-31 | [The BEST PERKS for the Chaos Exploder](https://www.youtube.com/watch?v=shGtk1lsPDI) | Controlled perk comparison | Current 2026 |
| S07 | 2026-07-30 | [FIRST LOOK at the NEW Chaos Exploder AR](https://www.youtube.com/watch?v=RvcQ9zjKBJc) | First-look runtime evidence | Current 2026 |
| S08 | 2026-06-02 | [Fortnite Save the World Elements Explained](https://www.youtube.com/watch?v=wb6NY92WxJI) | Epic-sponsored instruction | Current 2026 |
| S09 | 2026-06-04 | [Fortnite Save the World Hero Loadouts Explained](https://www.youtube.com/watch?v=MTjYLElrsSM) | Epic-sponsored instruction | Current 2026 |
| S10 | 2026-06-09 | [The BEST WEAPONS to use with Defenders](https://www.youtube.com/watch?v=yIwxzggZvss) | Defender interaction test | Current 2026 |
| S11 | 2026-06-12 | [Ranking EVERY DEFENDER](https://www.youtube.com/watch?v=MoFJudPsGTg) | Defender archetype ranking | Current 2026 |
| S12 | 2026-03-21 | [The Supply Drop Got a MASSIVE Buff!](https://www.youtube.com/watch?v=gDnNZ5j2lDk) | Patch behavior and farming test | Current 2026 |
| S13 | 2026-04-04 | [Flash A.C. with the Black Drum is INSANE!](https://www.youtube.com/watch?v=a07wDLHYPcg) | Mature build test | Current, version-sensitive |
| S14 | 2026-03-24 | [The Black Drum is INCREDIBLE!](https://www.youtube.com/watch?v=zNlEdpqgxyg) | Suspected bug investigation | Current, version-sensitive |
| S15 | 2026-04-18 | [The NEW Horde Melee META with ED-EE!](https://www.youtube.com/watch?v=9f3v9yQ68iM) | Horde scenario build | Current 2026 |
| S16 | 2026-05-20 | [The BEST Shotgun Build for Power Hour!](https://www.youtube.com/watch?v=QiuQkbNz1so) | Limited-event modifier test | Current, scenario-only |
| S17 | 2026-05-18 | [The BEST Rocket Spam Build for Power Hour!](https://www.youtube.com/watch?v=UvwTQkt6hpQ) | Limited-event modifier test | Current, scenario-only |
| S18 | 2026-05-16 | [The BEST Lefty and Righty Build for Power Hour!](https://www.youtube.com/watch?v=_N_n5rU00cA) | Limited-event modifier test | Current, scenario-only |
| S19 | 2026-07-11 | [Stun EVERYTHING with the Tree of Light!](https://www.youtube.com/watch?v=ivlJblOQFIE) | Themed melee/control test | Current 2026 |
| S20 | 2026-04-23 | [The BEST PERKS for the Broadside](https://www.youtube.com/watch?v=CKUtJ83nAu8) | Controlled trap geometry/perk test | Current 2026 |
| S21 | 2026-04-08 | [The BEST PERKS for the Ceiling Drop Trap](https://www.youtube.com/watch?v=rHxiJx9k3k8) | Controlled trap geometry/perk test | Current 2026 |
| S22 | 2026-05-01 | [The ULTIMATE Popcorn Trap Build!](https://www.youtube.com/watch?v=JDhStXAOYYI) | Novelty build and failure mode | Current, not a recommendation |
| S23 | 2026-03-29 | [FREE ROCKETS with This INSANE Build!](https://www.youtube.com/watch?v=QjSVM7KY_7E) | Non-event launcher build test | Current 2026 |
| S24 | 2026-03-20 | [Mermonster Ken with the Husk Grinder is NUTS!](https://www.youtube.com/watch?v=cWjIoHdOkSo) | Conditional melee build test | Current 2026 |
| S25 | 2026-03-17 | [The Room Sweeper is a MONSTER!](https://www.youtube.com/watch?v=k_xUxgRlzzQ) | Themed shotgun build test | Current 2026 |
| S26 | 2025-12-09 | [Stoneheart Farrah and the Vacuum Tube Bow are INSANE!](https://www.youtube.com/watch?v=QgYEhXQbMIY) | Historical interaction test | Historical; 2026 revalidation required |

The YAML source ledger is the canonical machine-readable version of this table.

## High-confidence 2026 baselines

### Elements and materials

- Water is the counter-element for fire enemies.
- Fire is the counter-element for nature enemies.
- Nature is the counter-element for water enemies.
- Energy is a generalist against elemental targets; physical is most appropriate against normal physical enemies and is penalized against elementals.
- Nature enemies strongly punish metal walls, so brick is preferred.
- Water enemies punish brick walls, so metal is preferred.

The numerical multipliers still belong in the asset/runtime data layer. QA checks the counter relationships and verifies the current multipliers before hard enforcement.

### Loadout construction

- Commander and support versions of a hero perk must not be treated as equal.
- Team perks must satisfy their activation requirements; Totally Rocking Out needs compatible support heroes and a usable trigger loop.
- Every recommendation should state the objective and the loop: damage type, activation, sustain, reload/ammo, mobility, and relevant mission constraints.
- An accessible starter build is evidence of a coherent build, not proof of the global meta.

### Weapon-only versus full-loadout ranking

The assault-rifle and shotgun tier lists explicitly use two views. The optimizer should do the same:

| View | Includes | Excludes |
|---|---|---|
| Weapon-only | Base stats, available perks, innate mechanics, element restrictions | Commander/support effects, team-perk uptime, ability reloads |
| Full loadout | All weapon factors plus commander, supports, team perk, activation uptime, reload/ammo loop | Unavailable heroes/perks and unstated event modifiers |

This distinction explains legitimate ranking flips. A long-reload weapon can move sharply upward with Chaos Agent. A crit-scaled weapon can move upward with Totally Rocking Out. A theoretical sustained-DPS leader can lose in practical burst scenarios.

## Reconciled findings

### Chaos Exploder: use the evidence sequence, not the first impression

The three videos form a useful update chain:

1. **First look (S07):** identifies the impact explosion, automatic ammo return, and unusual reload interaction.
2. **Perk test (S06):** compares fire rate, magazine, crit, reload, and element constraints.
3. **Crackshot follow-up (S05):** shows that automatic ammo return can preserve Crackshot's weapon-continuity stacks, while also exposing the awkward wait/pacing cost.

QA should therefore require the optimizer to model both burst and long-run behavior. “Never reload” is not automatically optimal, and “Crackshot lead is always best” is too strong. The current expert conclusion is a conditional tradeoff among stack ceiling, wait time, manual reload, and Sledgehammer-style crit scaling.

### Shotguns: ranking flips are expected

- Popshot is a strong mathematical sustained-DPS candidate when the native reload loop matters.
- Ground Pounder rises with an ability-triggered reload solution because its long reload stops suppressing practical burst.
- Huskbuster/Stampede remain efficient general performers.
- Room Sweeper gains substantially from Totally Rocking Out but remains ammo intensive.
- Vacuum Tube Shotgun is powerful in favorable elemental contexts and carries an elemental-lock penalty elsewhere.
- Primal Shotgun behavior was inconsistent in the source and is quarantined from stable numeric ranking.

### Constructors: role and mission matter more than one global tier

- BASE Kyle, Power BASE, Ice King, and Mega BASE form the core high-value defense set for different purposes.
- Supercharged Traps must account for BASE-affecting support count.
- Warden Kyle is valuable support utility; that does not make commander placement mandatory.
- Machinist Harper can be essential when durability binds and nearly wasted when it does not.
- Electro-Pulse Penny and Thunder Thora are useful low-level passive-damage options but should not inherit the same scaling assumption in endgame content.
- DECOY value falls when the target class does not respond to it.
- ED-EE is a coherent energy-melee/shield build, not a universal constructor recommendation.

### Gadgets: the 2026 Supply Drop update changes farming evaluation

The current evidence describes a much shorter Supply Drop cooldown, substantial materials and crafting items, traps, and per-player instanced loot. Gadgeteer/Undercover Buzz can cut the cooldown further. A coordinated team can scale yield dramatically.

Do not encode the video's rough “16x” comparison as a constant. Model the actual party size, cooldown, pickup/instancing rules, mission duration, and opportunity cost.

### Defender scope is separate

The defender update changes costs and logistics: defenders can use equipment without the old pad/ammo/durability burdens and can share a schematic. That is important QA for a defender module.

It should not silently modify a player-only hero optimizer. Defender targeting, charge behavior, engagement range, and signature-perk cooldown behavior require their own runtime rules. Suspected cooldown bypasses are quarantined.

### Black Drum is current evidence with quarantined mechanics

The build loop is reproducible enough to test: shoot, phase shift, Chaos Agent reload, missing-health scaling, and survivability management. The exact explosion formula is not safe to canonize. The sources suspect per-pellet behavior and describe a sudden, possibly unintended damage spike.

Acceptable QA behavior:

- Recognize the loadout loop and its requirements.
- Flag the weapon as version-sensitive.
- Require a controlled runtime test for pellet count, radius, damage formula, and reload trigger.
- Never make exact showcase damage a release-blocking constant.

### Power Hour cannot leak into normal missions

The Super Soldier modifier reduces an active cooldown when qualifying ranged eliminations occur. The three Power Hour builds exploit that modifier for War Cry, launchers, Ground Pounder reloads, and Raven abilities.

The strongest hard negative test is simple: remove the modifier and confirm the optimizer removes the cooldown-reduction loop. Trap eliminations should not be assumed to qualify without testing.

### Traps require geometry, direction, and durability context

- Broadside cannonballs need an opposing wall, normally one to three tiles away, to realize repeated bounce damage; one tile is the preferred compact geometry.
- A universal Broadside sixth perk is invalid. Durability, attached-structure healing, and structure health answer different failure modes.
- Ceiling Drop Traps are directional. Placement orientation determines the tire roll direction and should coordinate with Floor Freeze Traps.
- Three tiles is the standard strong Ceiling Drop Trap height; four does not normally trigger, while the six-tile “popcorn” setup depends on launchers and is a novelty build.
- Extreme tire spam can create performance problems and should not be recommended as a default defense.

### Non-event rocket conservation is probabilistic

Star-Spangled Headhunter plus 8-Bit Demo can create a 70% ammo-nonconsumption window during War Cry. The correct expectation is geometric: if each shot has a 30% chance to consume a round, one consumed round funds about $1 / 0.30 = 3.33$ expected shots. It is not merely a 70% magazine increase.

Assault Ammo Recovery does not apply to launchers. Team War Cry staggering is a separate positioning and uptime problem.

### Historical Stoneheart Farrah evidence stays visible but inactive

The December 2025 source describes Vacuum Tube Bow chain lightning branching through Stoneheart Farrah for exceptional crowd clear. It also emphasizes cooldown/ready state, elemental match, and weak single-target relevance compared with dedicated boss weapons.

Because it is not 2026-approved evidence, the interaction is informational until the current assets and runtime are checked. The suite deliberately tests that historical evidence cannot override newer contradictory data.

## What is deliberately not hard-coded

- Creator tier letters.
- Exact showcase damage numbers.
- Lobby-specific trap durability totals.
- “Best weapon in the game” statements.
- Rough multiplicative farming slogans.
- Suspected Black Drum pellet/explosion behavior.
- Defender signature-perk cooldown bypasses.
- Limited-event cooldown rules outside the event.
- The 2025 Farrah/Vacuum Tube interaction before revalidation.

These remain evidence notes or directional tests so a patch can change the product without forcing developers to edit a false constant.

## Recommended repo layout

```text
qa/
  golden/
    stw/
      STW_Golden_QA_Benchmark_2026.md
      stw_golden_qa_sources_2026.yaml
      stw_golden_qa_cases_2026.yaml
```

Keep game assets and runtime snapshots elsewhere, for example `data/stw/<build-id>/`. The golden cases should refer to normalized entity IDs in your adapter rather than creator spelling.

## Runner contract

For each case, the adapter should:

1. Load the fixture and explicitly set the mission/event context.
2. Query the optimizer for its recommendation plus structured reasons.
3. Evaluate the oracle type.
4. Attach the game build ID, data snapshot ID, and optimizer commit.
5. Apply enforcement only if the case's verification requirement is satisfied.
6. Store a compact diff: changed rank, missing factor, forbidden conclusion, or runtime mismatch.

Suggested result shape:

```json
{
  "case_id": "SG-002",
  "game_build": "<build-id>",
  "optimizer_commit": "<sha>",
  "outcome": "pass | fail | review | skipped",
  "observed": {},
  "missing_factors": [],
  "evidence_sources": ["S02", "S16"],
  "verification": "asset | runtime | model-only",
  "notes": ""
}
```

## CI profiles

| Profile | Included cases | Intended use |
|---|---|---|
| `smoke` | Active hard invariants with asset/model verification | Every pull request |
| `runtime` | Mechanics requiring controlled in-game observation | On game update or scheduled validation |
| `ranking-review` | Directional expert expectations | Before recommendation-model releases |
| `scenario` | Event, endurance, Horde, novelty, and party-context cases | When that feature is enabled |
| `quarantine` | Suspected bugs, inconsistent behavior, historical evidence | Never release-blocking; run for change detection |

## Release gates

A release should fail when a verified active hard invariant fails. A release should enter manual review when a ranking expectation changes materially. Contextual cases should be skipped unless their complete fixture is active. Quarantined cases should report only.

Recommended additional checks:

- Every case ID is unique.
- Every source reference exists in the source ledger.
- Every hard invariant names a verification method.
- No historical-only source is the sole basis of an active hard invariant.
- No quarantine case is configured as release-blocking.
- Every event mechanic includes a negative non-event case.
- Every recommended loadout has activation and sustain/reload checks.

## Maintenance workflow

When the game updates:

1. Ingest current asset/runtime facts.
2. Run active hard invariants.
3. Re-run version-sensitive and quarantined tests.
4. Review changed rankings instead of automatically restoring the old order.
5. Mark contradicted claims `superseded`; do not delete their provenance.
6. Add the new build ID and validation date to the test results.

When a new MistaBeastxGames transcript arrives, add it to the source ledger first. Extract atomic claims, decide currentness and enforcement, then add only cases that test a distinct mechanic, constraint, reversal, or failure mode. Repeated hype or duplicate demonstrations should strengthen provenance, not multiply identical tests.

## Version note

This 2026 evidence edition supersedes the older undated transcript baseline for current-release decisions. The older benchmark remains useful as a historical claim inventory, but its mechanics and rankings require confirmation before they can block a 2026 release.
