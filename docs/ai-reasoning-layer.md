# Evidence-constrained AI reasoning layer

`tools/stw_ai.py` is the backend orchestration layer between natural-language requests
and the deterministic STW catalog, evaluator, context resolver, and optimizer. It does
not let a language model supply Fortnite facts.

## Architecture

```text
user text
  -> versioned BuildIntent
  -> provider-neutral orchestration
  -> targeted catalog / legality / evaluator / optimizer tools
  -> structured result and evidence IDs
  -> validated evidence selection
  -> rendered explanation
```

`ReasoningProvider` has two narrow responsibilities: interpret text into a validated
intent and select evidence IDs for an explanation. The included
`DeterministicReasoningProvider` is offline and makes the test suite and local demo
independent of a paid/network model. A future model adapter must implement the same
interface.

The provider cannot add free-form game claims to the result. Unknown evidence IDs are
rejected. Explanatory sentences are rendered from deterministic result facts, semantic
status, and source provenance.

## BuildIntent v1

The explicit `stw.build-intent.v1` schema carries:

- recommendation, analysis, or comparison mode;
- weapon, enemy, element/status, enemy modifiers, mission, PL, four-player, storm,
  and mission-modifier context;
- weighted damage, control, sustain, uptime, and reliability objectives;
- owned/unavailable heroes and weapons, plus owned team perks and gadgets;
- locked commander, supports, team perk, and gadgets;
- avoided mechanics such as elimination-trigger dependence;
- whether partial or opaque candidates are allowed;
- requested alternatives and a complete/partial current loadout.

Invalid objectives, malformed weapon perks, out-of-range search controls, and unknown
schema versions fail validation. Missing material choices produce clarification instead
of guessed mechanics. If no enemy is supplied and the real catalog contains the exact
`default__huskpawn_c` identity, the response records use of that catalog baseline as an
explicit assumption.

## Structured tools

The versioned `stw.ai-tools.v1` boundary exposes:

- targeted catalog search and entity inspection;
- loadout legality validation;
- deterministic evaluation of a specified loadout;
- constrained full-loadout optimization;
- same-scenario build comparison;
- provenance retrieval for material recommendation claims.

Catalog search is capped at 25 rows and uses a whitelist of entity tables. There is no
whole-catalog dump tool.

## Existing-loadout analysis

The analyzer returns legality errors, active supported synergies, inactive/conflicting
conditions, repeated normalized perk families, evaluator limitations, mission/target
suitability, partial/opaque mechanics, and constrained replacement candidates. A
replacement is not described as definitively superior when material mechanics remain
unquantified.

## CLI

```powershell
python tools/stw_ai.py `
  "Build me the strongest Nocturno loadout for 160s." `
  --db data/phase2-real-validation.sqlite3
```

For exact ownership, locks, current loadouts, or comparisons, provide a JSON
`BuildIntent` override:

```powershell
python tools/stw_ai.py "Build around Paleo Luna" `
  --db data/phase2-real-validation.sqlite3 `
  --intent-json my-intent.json
```

## Local HTTP API

`GET /api/ai/tools` returns the tool contract. `POST /api/ai/recommend` accepts:

```json
{
  "request": "Make me a Potshot build for Smashers",
  "intent": {
    "schema_version": "stw.build-intent.v1",
    "weapon": "The Potshot",
    "target_enemy": "Smasher",
    "objective_weights": {"burst_damage": 2, "mist_monster_boss": 2}
  }
}
```

The explicit `intent` is optional. Supplying it is useful for application forms and
inventory-aware clients; otherwise the local provider interprets the request.

## Example flows

For “Build me the strongest Nocturno loadout for 160s,” the real-catalog demonstration
grounded Nocturno, parsed PL160 and sustained damage, recorded its use of the exact
default Husk identity, ran the constrained optimizer, and returned Rescue Trooper
Ramirez. The explanation also said the recommendation was non-definitive because Paint
the Field and other material mechanics remained opaque.

For “I don't own Lynx; make me a Potshot build for deleting Smashers,” the language
layer emits Lynx in `unavailable_heroes`, Potshot as the weapon, Smasher as the target,
and burst/boss objective weights. Lynx is removed before hero search. The response can
only recommend a legal candidate from the remaining inventory.

For “What sucks about my current loadout?”, a structured `current_loadout` is validated
first. The analyzer reports proven active contributions, inactive conditions, repeated
perk families, unresolved mechanics, and replacement candidates. Numerical deltas are
marked definitive only when both sides are fully supported.

## Validation and performance

The evaluation fixture contains 12 realistic requests spanning weapon builds, build-
around, ownership, mission adaptation, reliability, vague requests, current-loadout
criticism, ability uptime, sustain, and scenarios requiring clarification. Tests also
cover hostile fabricated evidence IDs, exact inventory constraints, legality,
same-scenario comparison, API behavior, and end-to-end provenance.

On real snapshot 35, the default 64-wide Nocturno request evaluated 33 candidates in
about 7.9 seconds on the development machine. Intent parsing and evidence rendering are
small compared with optimizer evaluation. The result correctly remained
uncertainty-aware because all 33 candidates contained material opaque mechanics.

## Boundaries

This backend is ready for a user-facing application and a real LLM adapter. The UI
still needs inventory entry, structured scenario controls, streamed progress, and
human-friendly comparison displays. A model adapter must retain schema validation,
evidence-ID validation, and the rule that partial/opaque contributions are never zero.
