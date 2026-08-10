# stw-intelligence
Platform and intelligence project for Fortnite Save The World metrics

- [Telemetry capture findings](docs/telemetry-capture-findings.md)
- [Regional population signal investigation](docs/regional-population-signal-investigation.md)
- Extract captures with `python tools/analyze_telemetry.py <log> [<log> ...]`

## Local intelligence pipeline

The pipeline keeps authoritative global STW engagement separate from sampled regional
matchmaking activity. It does not convert the regional activity index into player counts.

```powershell
# Import all local captures. Content hashes make repeat imports idempotent.
python tools/stw_pipeline.py ingest logs/telemetry-captures

# Browse recent attempts, then inspect one timeline and its source provenance.
python tools/stw_history.py attempts --limit 20
python tools/stw_history.py show 1

# Ingest the permission-safe provider fixture and correlate local nodes.
python tools/stw_missions.py ingest-fixture fixtures/current-mission-rotation.json
python tools/stw_missions.py rotation
python tools/stw_missions.py missions
python tools/stw_missions.py mission 1
python tools/stw_missions.py matches --evidence

# Fetch Epic's public Save the World (`campaign`) metrics.
python tools/stw_pipeline.py sync-global --interval minute
python tools/stw_pipeline.py sync-global --interval hour
python tools/stw_pipeline.py sync-global --interval day

# Compare only mission/theater/difficulty cohorts observed in multiple regions.
python tools/stw_pipeline.py report --horizon 60

# Machine-readable output for a future dashboard or API.
python tools/stw_pipeline.py report --horizon 60 --json
```

The default database is `data/stw-intelligence.sqlite3`. Regional observations qualify
only when they are joined, Public Fill, solo-party attempts with a complete 15, 30, or
60-second observation window. Comparisons remain separated by mission, theater, and
internal difficulty. Use `--all-cohorts` to inspect non-comparable single-region history.

`observed_team_high_water_index` is the mean observed team-index high-water above the
solo player, normalized across the three remaining team slots. It measures the experience
of this one client for that exact mission cohort. It is not regional CCU, population share,
current occupancy, or session supply.

Run the test suite with:

```powershell
python -m unittest discover -s tests -v
```

## Local Fortnite asset catalog

Phase 1 can inventory a read-only FModel export directory and build a versioned local
hero/perk reference graph. Raw Fortnite JSON is not copied into the repository or the
database. The catalog stores source paths, file hashes, build metadata, normalized facts,
and explicit resolved/unresolved reference edges.

Supply the actual Fortnite build identifier rather than guessing it:

```powershell
python tools/stw_assets.py ingest `
  "C:\path\to\FModel\Output\Exports" `
  --build-id "your-fortnite-build-id" `
  --game-version "your-game-version" `
  --exporter-version "your-fmodel-version"

python tools/stw_assets.py hero "Rescue Trooper Ramirez"
python tools/stw_assets.py unresolved
python tools/stw_assets.py coverage
python tools/stw_assets.py queue
python tools/stw_assets.py queue --max-priority 0 --paths-only
```

The checked-in Rescue Trooper golden manifest contains hashes and required follow-up
asset paths only; it contains no Fortnite asset payloads. The focused unit-test fixture
is synthetic and validates `AssaultDamage T01/T02` as `+17% / +33%` only when an
AbilityKit, GameplayEffect modifier, and curve row resolve into a complete evidence chain.

The Phase 2 queue is reference-driven: it deduplicates unresolved package/object paths
that already occur in exported data and prioritizes hero perks, active abilities,
GameplayEffects, inherited templates, curves, and custom calculations. It never creates
a path from a filename convention. Export the listed assets beneath the same FModel
export root and ingest that root again; the changed manifest creates a new immutable
snapshot and the next queue follows the newly exposed references recursively.

Gameplay tags, literal and curve-backed magnitudes, duration, period, application chance,
stacking, cooldown/trigger facts, and inheritance are normalized for downstream rules.
Custom magnitude and execution calculations remain explicitly opaque until a dedicated
interpreter exists.

## Live local application

On Windows, the default command watches Fortnite's standard log location, loads the
permission-safe mission fixture, and serves the dashboard and API on localhost:

```powershell
python tools/stw_app.py
```

For the easiest Windows launch, double-click `start-stw.cmd`. It starts the app and opens
the dashboard automatically. Backup, recovery, diagnostics, privacy, and retention
instructions are in [the operations guide](docs/operations.md).

To have a lightweight monitor start STW Intelligence only while Fortnite is running,
double-click `enable-stw-auto-start.cmd` once. It installs a per-user Windows Startup
shortcut and starts the monitor immediately. The full watcher/dashboard starts when
`FortniteClient-Win64-Shipping.exe` appears and shuts down cleanly when that process
exits. Double-click `disable-stw-auto-start.cmd` to disable it. Windows exposes one
Fortnite client process, so this activation covers the full Fortnite session; the
telemetry parser still records only recognized STW activity.

Open <http://127.0.0.1:8765>. Start the application before queueing a mission; a new
watch starts at the current end of the log so historical Fortnite output is not replayed.
The byte checkpoint, incomplete-line buffer, and live parsing spool are persisted beneath
`data/`, so restarting the application continues from the last committed position.

Use explicit paths when Fortnite or the database is elsewhere:

```powershell
python tools/stw_app.py --log "C:\path\to\FortniteGame.log" --db data/stw-intelligence.sqlite3
```

For a deliberate one-time replay of an existing log, add `--from-start`. The local API
provides `/api/current`, `/api/attempts`, `/api/attempts/<id>`,
`/api/missions/current`, `/api/correlation/current`, `/api/activity/current`,
`/api/recommendation/current`, `/api/cohorts/current`, `/api/settings`,
`/api/diagnostics`, and `/api/health`.

The Activity view keeps each observed node rotation-scoped, then reuses evidence across
daily resets only through a versioned, provider-backed comparable-mission cohort. A
cohort requires a consistent theater, objective, power level, and four-player status
from accepted mission matches. Missing or conflicting identities remain excluded. The
view discloses node and rotation counts alongside the versioned 0-100 activity score.
This remains local evidence—not player population, queue depth, or CCU.

Recommendations compare observations in the same three-hour local-time band. The
default timezone is `America/Los_Angeles` and can be changed in Settings. A
time-specific result requires three complete samples in each of at least two regions;
otherwise the app visibly falls back to the overall recent ranking.

```powershell
python tools/stw_activity.py refresh
python tools/stw_activity.py cohorts
python tools/stw_activity.py report
python tools/stw_activity.py recommend
```

## Approved live mission feed

The bundled FortniteDB v1 API specification does not expose full current mission rows,
so the application does not scrape FortniteDB or pretend its summary endpoint is a mission
rotation. An approved HTTPS JSON feed can be connected without changing the database or
matcher. Its payload must follow [the normalized feed contract](docs/provider-feed-contract.md).

Keep credentials out of command history by storing the key in an environment variable:

```powershell
$env:STW_PROVIDER_API_KEY = "your-provider-key"
python tools/stw_app.py `
  --provider-url "https://provider.example/stw/rotation" `
  --provider-code "approved_feed" `
  --provider-name "Approved STW mission feed" `
  --provider-api-key-env "STW_PROVIDER_API_KEY" `
  --provider-api-key-header "API-Key"
```

The app caches a healthy feed through its `valid_until` time, refreshes just after expiry,
uses `ETag`/`Last-Modified` validators when supplied, and keeps the last good rotation if
a refresh fails. Failed refreshes retry every five minutes by default. Runtime fetch status
is available from `/api/health`. To ingest once:

```powershell
python tools/stw_missions.py ingest-url "https://provider.example/stw/rotation" `
  --provider-code "approved_feed" `
  --provider-name "Approved STW mission feed" `
  --api-key-env "STW_PROVIDER_API_KEY"
```
