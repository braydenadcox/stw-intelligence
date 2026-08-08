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
