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

## Live local application

On Windows, the default command watches Fortnite's standard log location, loads the
permission-safe mission fixture, and serves the dashboard and API on localhost:

```powershell
python tools/stw_app.py
```

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
`/api/recommendation/current`, and `/api/health`.

The Activity view compares complete, solo, Public Fill observations for the same
rotation-scoped mission. It reports a versioned 0-100 observed matchmaking activity
score, component evidence, sample count, effective sample size, coverage, and confidence.
The score is local evidence only: it is not a player population, queue depth, or CCU
estimate. Recompute or inspect it from the command line with:

```powershell
python tools/stw_activity.py refresh
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
