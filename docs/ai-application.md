# STW AI application

The local application now combines two isolated data planes:

- `data/stw-intelligence.sqlite3` stores matchmaking history, application settings,
  AI conversations, and the user's inventory.
- `data/phase2-real-validation.sqlite3` is the read-mostly normalized Fortnite asset
  catalog used by the loadout tools, evaluator, and optimizer.

The browser never reads either database directly. It uses the existing local HTTP
application, which delegates all loadout facts and calculations to the structured AI
orchestration layer.

## Launch

For the deterministic offline provider, double-click `start-stw.cmd` or run:

```powershell
python tools/stw_app.py
```

The application opens at `http://127.0.0.1:8765`. The default asset catalog path is
`data/phase2-real-validation.sqlite3`; override it with `--asset-db` when needed.

## OpenAI provider

The real adapter uses the Responses API with strict Structured Outputs. Configuration
is external and API keys are read only from environment variables:

```powershell
$env:OPENAI_API_KEY = "your-key"
$env:STW_AI_PROVIDER = "openai"
$env:STW_AI_MODEL = "gpt-5.6-terra"
$env:STW_AI_REASONING_EFFORT = "low"
./start-stw.cmd
```

Optional settings are `OPENAI_BASE_URL` and `STW_AI_TIMEOUT_SECONDS`. The key is never
written to the database, returned by the API, or displayed by the UI. The provider
uses `store: false`, retries transient 408/409/429/5xx and network failures, records
request/token/latency/retry metrics, and validates model output through the same local
`BuildIntent` and evidence-ID checks used by the offline provider.

The deterministic provider remains the default so normal development and tests never
require network access or paid inference.

When the OpenAI provider is enabled, the current request, a compact recent conversation
window, and at most 30 catalog-grounded entity summaries are sent to the configured
HTTPS API. Raw Fortnite assets, telemetry logs, databases, and full optimizer results
are not uploaded for intent interpretation. Evidence selection receives only the small
structured evidence bundle for the selected recommendation.

## Product workflows

### AI Chat

Chat requests run as background jobs. The interface polls public progress stages:

1. Understanding request
2. Resolving constraints
3. Generating legal builds
4. Evaluating candidates
5. Analyzing uncertainty
6. Preparing recommendation

These are workflow states, not internal chain-of-thought. Conversations and their last
validated intent are persisted locally. A follow-up such as “make it more survivable”
inherits the previous weapon/mission context and changes only the newly expressed goal.

### Build Viewer

Recommendations show commander, five supports, team perk, exact weapon variant, legal
weapon perks, gadgets, supported score components, deterministic contribution chain,
scenario assumptions, and supported/partial/opaque evidence. Evidence is concise by
default and expandable.

### Build Analyzer

Users can enter an exact weapon variant plus any commander, support team, team perk,
gadgets, and target. The result reports legality, active synergies, conflicting
conditions, repeated perk families, weak links, runtime uncertainty, and constrained
replacement candidates.

### Inventory

Catalog search adds canonical heroes, weapon families, team perks, and gadgets to the
local owned collection. Enabling “Only use my inventory” turns non-empty inventory
categories into hard optimizer constraints. An incomplete hero inventory that cannot
fill the requested support slots returns a clear search error rather than silently
using unowned heroes.

### Comparison

Recommendations can be pinned and compared side by side. The backend reconstructs
specified loadouts and evaluates both under one identical scenario. A winner is not
declared definitive if either build is illegal, partial, or opaque.

### Matchmaking

Live, History, Activity, Missions, and Settings remain available from the same
navigation and continue using the original telemetry/provider paths.

## API

- `POST /api/ai/jobs` creates a background chat/analyze/compare job.
- `GET /api/ai/jobs/{id}` returns progress, errors, or the final result.
- `GET /api/ai/conversations` and `GET /api/ai/conversations/{id}` restore chat.
- `GET|POST /api/ai/inventory` reads or updates owned content.
- `GET /api/ai/catalog?kind=hero&query=...` performs targeted catalog search.
- `GET /api/ai/config` reports provider/catalog readiness and safe instrumentation.
- `GET /api/ai/tools` returns the versioned structured tool contract.

## Validation

The normal suite uses the deterministic provider and synthetic asset slices. Product
tests cover build generation, follow-up modification, inventory restrictions,
analysis, comparison, clarification, every progress stage, missing-catalog errors,
persistence, HTTP endpoints, strict OpenAI request schemas, retries, metrics, and
hallucinated evidence rejection.

Real-provider testing is opt-in:

```powershell
$env:STW_RUN_REAL_PROVIDER_TESTS = "1"
python -m unittest tests.test_stw_ai_product.OpenAIProviderTests.test_optional_real_provider_schema
```

## Remaining public-product work

The local first version is usable, but a public release still needs guided onboarding,
inventory import, richer catalog autocomplete, cancellation/queue limits, conversation
deletion/export, accessibility and browser coverage, encrypted secret management or a
hosted proxy, packaging/updating, telemetry consent, and broader real-provider evals.
