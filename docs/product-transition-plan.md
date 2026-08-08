# STW Intelligence: Product Transition Plan

This document closes the open-ended research phase and defines the first local-first product. It is based on the established telemetry findings, the August 7/8 daily-reset captures, the exported FortniteDB v1 OpenAPI document inspected on 2026-08-07, and public provider behavior inspected on 2026-08-07.

## What Changed After Daily Reset

### Result

`STW:Mission` changed across the daily rotation even though the user deliberately selected the same recognizable configuration: Twine Peaks, Ride the Lightning, PL 160.

| Field | Before reset | After reset | Meaning |
| --- | --- | --- | --- |
| Capture time | 2026-08-07 04:27:58 UTC | 2026-08-08 00:50:11 UTC | Different daily rotation windows |
| `STW:Theater` | `D9A801C5444D1C74D1B7DAB5C7C12C5B` | same | Twine theater persisted |
| `STW:Mission` | `b2252cdd-d1d2-40b2-abd0-cdccc0d5440f` | `e699dd8d-25e6-4c0c-8c68-894fce98c657` | Selected-node UUID did not persist |
| Internal difficulty | `52.0` | `52.0` | Calibrated PL 160 persisted |
| Objective | Ride the Lightning | Ride the Lightning | Recognizable mission type persisted |
| Generated map | `Zone_TP_Island01` | `Zone_Arid_WildWest_02` | Map/biome path did not persist |
| Requested region | NAE | OCE | User choice, not mission identity |
| Assigned datacenter | VA | SYD | Assignment result, not mission identity |
| Lobby session | `648b46dc...` | `8d1c6af2...` | Lobby identity changed as expected |

The exact registration evidence is in the [pre-reset capture](../logs/telemetry-captures/twine-ride-the-lightning-160-today.log#L30862) and [post-reset capture](../logs/telemetry-captures/twine-ride-the-lightning-160-tomorrow.log#L30241). The assignment rows independently show different lobby sessions in the [pre-reset capture](../logs/telemetry-captures/twine-ride-the-lightning-160-today.log#L30905) and [post-reset capture](../logs/telemetry-captures/twine-ride-the-lightning-160-tomorrow.log#L30250).

### What this proves

- A mission UUID is stable for one selected map node within an observed rotation, but it is not a durable identity for a human concept such as “Twine PL160 Ride the Lightning.”
- The UUID's demonstrated lifetime is **rotation-scoped**. These two captures do not prove that every node UUID is regenerated at exactly 00:00 UTC, nor do they establish whether UUID values can ever be reused later.
- Theater is the durable campaign key among the observed identifiers. Objective and PL are durable descriptive attributes, not unique keys.
- Generated map/biome is not a durable identity component: the two equivalent configurations loaded different map paths.
- Session ID remains the lobby-instance key and must never be used as mission identity.

FortniteDB advertises a daily mission expiry at `00:00:00`, consistent with the observed boundary, but the database should store provider-supplied validity timestamps rather than hard-code an assumed reset schedule. [FortniteDB current missions](https://fortnitedb.com/)

### Database consequences

1. Do not make `mission_uuid` a global primary key.
2. Store the raw UUID on an observed `MissionNode` with `first_seen_at`, `last_seen_at`, and the observed rotation window.
3. Use an internal surrogate ID for every table.
4. Treat the human mission concept and the rotating mission instance as different objects.
5. Preserve external and observed records independently and connect them through an auditable match table.
6. Never carry a mission-to-provider match across a reset merely because theater, objective, and PL repeat.

## Final Product Definition

STW Intelligence is a single-user, local-first mission browser and matchmaking journal that answers:

> For the mission I want to play now, which region has recently given this client the best matchmaking experience?

It combines two explicitly separated kinds of truth:

- **External truth:** the current rotation, objectives, PL, rewards, alerts, modifiers, and other mission metadata supplied by a replaceable provider.
- **Observed truth:** what this Fortnite client actually requested, where it was assigned, how long assignment took, which lobby it joined, and how teammates arrived or departed.

The product reports observed activity and sample quality. It does not report or imply regional player counts, queue depth, session supply, or global concurrency.

### Normalized data model

All timestamps are UTC. UUID-looking source values remain text so normalization never changes their representation. JSON is retained only for immutable raw provider snapshots and unrecognized parser fields; queryable domain fields are normalized.

#### External truth

| Entity | Important fields | Relationships and rules |
| --- | --- | --- |
| `Provider` | `id`, `code`, `display_name`, `adapter_version`, `terms_url`, `enabled` | One row per upstream source. |
| `ProviderSnapshot` | `id`, `provider_id`, `fetched_at`, `source_timestamp`, `etag`, `last_modified`, `payload_sha256`, `raw_payload`, `parse_status` | Immutable provenance and replay fixture. Deduplicate by provider and payload hash. |
| `MissionRotation` | `id`, `provider_id`, `provider_rotation_key`, `valid_from`, `valid_until`, `snapshot_id`, `status` | Provider-scoped validity window. Do not infer identity from the calendar date alone. |
| `Objective` | `id`, `canonical_code`, `display_name` | Canonical local vocabulary such as `ride_the_lightning`; provider aliases live in a mapping table. |
| `ExternalMission` | `id`, `rotation_id`, `provider_mission_key`, `theater_code`, `objective_id`, `power_level`, `husk_power_level`, `biome_code`, `is_four_player`, `map_position`, `alert_type`, `source_ordinal` | One provider mission row in one rotation. Unique provider key when supplied; otherwise no false natural key. |
| `ExternalMissionReward` | `external_mission_id`, `kind`, `item_code`, `display_name`, `rarity`, `quantity`, `multiplier`, `image_url` | `kind` distinguishes alert/one-time and repeatable/base rewards. |
| `ExternalMissionModifier` | `external_mission_id`, `modifier_code`, `display_name`, `element`, `image_url` | Many-to-many normalized modifiers. |

`ExternalMission` is deliberately not the same table as `MissionNode`. A provider may omit Epic's selected-node UUID or use an unrelated key.

#### Observed truth

| Entity | Important fields | Relationships and rules |
| --- | --- | --- |
| `MissionNode` | `id`, `theater_uuid`, `mission_uuid`, `observed_rotation_start`, `observed_rotation_end`, `first_seen_at`, `last_seen_at` | A locally observed selectable node. Surrogate key; index raw UUID and time range. |
| `MissionAttempt` | `id`, `mission_node_id`, `started_at`, `ended_at`, `fill_mode`, `party_size`, `build_id`, `platform`, `input_type`, `outcome`, `observation_seconds`, `source_file_id`, `source_line` | One matchmaking registration lifecycle. Null `mission_node_id` is valid for Storm Shield or an incomplete parse. |
| `Assignment` | `id`, `attempt_id`, `assigned_at`, `latency_ms`, `datacenter_id`, `lobby_session_id`, `match_id` | At most one accepted assignment per attempt in v1; preserve later reassignment events if discovered. |
| `Region` | `id`, `code`, `display_name` | Requested logical matchmaking region such as NAE or OCE. `MissionAttempt.region_id` points here. |
| `Datacenter` | `id`, `code`, `region_id`, `display_name`, `mapping_valid_from`, `mapping_valid_until` | Physical subregion such as VA or SYD. Mapping is time-versioned because infrastructure can change. |
| `LobbySession` | `id`, `session_id`, `first_seen_at`, `last_seen_at` | One temporary lobby instance. Session ID is unique only within the evidence available; retain timestamps defensively. |
| `MembershipEvent` | `id`, `lobby_session_id`, `attempt_id`, `occurred_at`, `phase`, `event_type`, `participant_hash`, `slot`, `team_size_after`, `source_line` | `phase` is lobby or zone; events include join, leave, local-login, and slot reuse. Participant identifiers are salted/local or redacted. |
| `HistoricalObservation` | `id`, `attempt_id`, `observed_at`, `metric_code`, `numeric_value`, `text_value`, `window_seconds`, `is_complete` | Immutable derived facts such as team size at 15/30/60 seconds. Recomputable via metric version. |

The requested region belongs to `MissionAttempt`; the assigned datacenter belongs to `Assignment`. This prevents the common mistake of treating NAE and VA as the same dimension.

#### Derived, auditable data

| Entity | Important fields | Relationships and rules |
| --- | --- | --- |
| `MissionMatch` | `id`, `mission_node_id`, `external_mission_id`, `method`, `confidence`, `status`, `evidence_json`, `matched_at`, `matcher_version` | Many candidates are allowed. Only one row may be accepted for a node at a time. Never copy provider fields into observed rows. |
| `RegionalActivity` | `id`, `external_mission_id` or `mission_node_id`, `region_id`, `window_start`, `window_end`, `score_version`, `score`, `sample_count`, `effective_sample_size`, `latest_sample_at`, `coverage`, `confidence_band` | A versioned aggregate, not a player-count estimate. Prefer a current external mission; fall back to a rotation-scoped node when unmatched. |

### External data layer

The telemetry parser depends only on local observed-domain types. It never imports a provider adapter.

```text
MissionProvider
  describe() -> ProviderCapabilities
  fetch_rotation(now, previous_snapshot?) -> RawProviderSnapshot
  normalize(snapshot) -> NormalizedRotation
  health() -> ProviderHealth

FortniteDBProvider
FixtureProvider
FutureProvider
```

`NormalizedRotation` contains provider identity, validity, and a list of normalized missions. Each normalized mission supports:

- provider mission key, if one exists;
- theater code/name and optional provider theater ID;
- objective code/name;
- mission PL and optional husk PL;
- biome and optional map position;
- four-player flag and alert type;
- alert and repeatable rewards;
- modifiers;
- source ordinal and raw-record reference;
- validity timestamps inherited from its rotation.

Provider-specific names are translated through alias tables. Unknown values remain representable; ingestion must not discard a whole rotation because one new modifier or reward appeared.

### FortniteDB investigation

The supplied v1 OpenAPI export documents five API operations, all requiring an `API-Key` header:

| Endpoint | Useful fields | Product use |
| --- | --- | --- |
| `GET /missions/summary` | `Vbucks`; `Notable[]` with level, image, name, rarity; zone-keyed `AlertSummary` items with image and sum | Dashboard summary only. It does **not** contain mission rows, objective, biome, modifiers, or a mission key. |
| `GET /profile/data/{name}` | refresh time, profile metadata, PL/FORT stats, inventory, ventures, loadout, schematics | Optional future personalization; not needed for mission intelligence. |
| `GET /profile/msk/{name}` | `HasQuest`, `PowerLevel`, `Tips`, `Regicide` | Optional MSK readiness feature; outside MVP. |
| `GET /leaderboards` | resource, progression, collection, quest, and endurance boards plus snapshot dates | Not useful for the product question. Must not be repurposed as population evidence. |
| `POST /schematics/crafting` | required-material image/value pairs; query by tier, rarity, material, name | Optional future schematic detail; not needed for current mission ingestion. |

The public site contains more data than that API contract:

- Zone pages are server-rendered and expose current mission rows with mission PL, objective, biome, four-player status, alert grouping, alert rewards, repeatable rewards, and husk PL. The current Twine page demonstrates this structure directly. [FortniteDB Twine missions](https://fortnitedb.com/zone/TwinePeaks)
- The Mission Finder initial HTML contains filter controls but no result rows. Results are populated after a search, and permanent filter URLs are supported. [Mission Finder](https://fortnitedb.com/all_missions), [Mission Finder guide](https://fortnitedb.com/mission-finder/guide)
- No supported Mission Finder JSON endpoint was identified from public documentation or indexed material. Direct unattended inspection received a Cloudflare challenge. That is evidence of an access boundary, not permission to bypass it.
- The likely upstream is Epic's authenticated STW world data: community source catalogs show a `GET /fortnite/api/game/v2/world/info` operation. This is an inference about the ecosystem, not proof of FortniteDB's implementation, and it is not a documented public Epic developer API. [Endpoint catalog](https://gist.github.com/NotOfficer/3160972bc78717014f39e73c316cbcf2)
- FortniteDB visibly rotates mission data at the daily expiry. No finer refresh guarantee is documented.
- No public FortniteDB API rate limit, cache contract, data license, or redistribution grant was found. The site identifies itself as unaffiliated with Epic and links a privacy policy, which is not a data-use license.

No superior **public, documented, full-current-mission API** was identified. Fortnite-API's published features cover cosmetics, shop, stats, news, playlists, map, banners, and keys—not current STW mission rows. Epic's public Fortnite Data API covers discoverable-island performance metrics, not STW rotation metadata. [Fortnite-API scope](https://fortnite-api.com/), [Epic Fortnite Data API scope](https://dev.epicgames.com/documentation/en-us/fortnite/using-fortnite-data-api-in-fortnite)

#### Recommended ingestion strategy

1. Ask FortniteDB for a supported full-mission feed, rate limits, caching rules, attribution, storage, and redistribution terms. This is the preferred production source because its normalized content already matches the product.
2. Build `MissionProvider` and a `FixtureProvider` first, using captured, permission-safe fixtures. This lets product work proceed without coupling the schema to a negotiation outcome.
3. If FortniteDB approves API access, implement `FortniteDBProvider` against the supported feed. Fetch once shortly after reset, retry with bounded exponential backoff, retain immutable raw snapshots, and honor `ETag`/`Last-Modified` if supplied.
4. Do not poll per user interaction. Cache through `valid_until`, mark stale data visibly, and keep the last good snapshot if refresh fails.
5. Do not make HTML scraping the silent production dependency. A personal experimental adapter may parse server-rendered zone pages at most once per rotation only with permission, must honor 403/429 and robots/access controls, and must never attempt to bypass Cloudflare.
6. Do not make users hand over Epic credentials to solve mission metadata. An authenticated Epic provider is a separate, opt-in research path with a larger security and maintenance burden.

### Mission matching

Matching runs only between records whose timestamps fall in the same rotation window.

#### Deterministic

- Exact equality of telemetry `STW:Mission` and a provider field explicitly documented to be the same Epic selected-node UUID, plus theater and rotation agreement.
- A previously accepted match within the same rotation for the exact same `(theater_uuid, mission_uuid)`.

No currently documented FortniteDB field satisfies the first condition.

#### Inferred

Candidate filtering, in order:

1. observation timestamp inside provider rotation validity;
2. canonical theater equality;
3. calibrated PL equality;
4. objective equality when the parser has an objective hint;
5. biome compatibility when a generated map path is available;
6. map position equality only if both sides later expose the same coordinate system.

If exactly one candidate survives, record a `high`-confidence inferred match with every predicate in `evidence_json`. If multiple candidates survive, keep them as ambiguous candidates and ask the UI to show “unmatched/ambiguous”; do not choose by row order. If objective is known only from a filename or user capture label, record that provenance and lower confidence. Rewards cannot disambiguate because local telemetry does not expose them.

The reset result forbids reusing yesterday's match. Even when theater, objective, and PL repeat, the new mission UUID starts a new matching problem.

### First regional activity model

Call the metric **Observed Matchmaking Activity**, not population, occupancy, or CCU. Compute it only for comparable public-Fill attempts and separate cohorts by rotation-scoped mission, party size, build compatibility, and requested region. No-Fill attempts remain in history but do not measure teammate activity.

For a joined attempt with a complete 60-second membership window, calculate a 0–100 score:

| Component | Points | Definition |
| --- | ---: | --- |
| Teammate arrival speed | 0–45 | Each of the three open team slots is worth `15 × max(0, 1 - arrival_seconds/60)`. A remote player already present when the local player logs in has `arrival_seconds = 0`. |
| Maximum concurrent teammates | 0–20 | `20 × min(max_concurrent_remote_players, 3) / 3`. |
| Unique teammate breadth | 0–10 | `10 × min(unique_remote_players, 3) / 3`. Slot reuse can raise breadth but not concurrency. |
| Retention | 0–15 | `15 × retained_teammate_seconds / possible_teammate_seconds` for teammates observed during the window. Departures reduce this value. |
| Assignment speed | 0–10 | `10 × exp(-assignment_latency_seconds / 30)`. Its weight is deliberately small because latency alone is not a population measure. |

The 15/30/60-second team sizes remain first-class explanatory metrics even though the score uses exact arrival times. They let a user read “two teammates by 15 seconds, full by 30 seconds” without interpreting a formula.

If no teammate appears in a complete window, arrival, concurrency, breadth, and retention are all zero. Incomplete windows are censored, not treated as empty lobbies. Calculate provisional 15- or 30-second submetrics for the live UI, but exclude them from the comparable 60-second aggregate. Cancellations, assignment failures, and assigned-but-not-joined outcomes appear as a separate completion-rate/reliability metric; they do not fabricate zero membership observations.

For a region and exact mission cohort, aggregate attempt scores with a six-hour recency half-life:

```text
weight_i = 2 ^ (-age_hours_i / 6)
regional_score = sum(weight_i * attempt_score_i) / sum(weight_i)
```

Always display sample count, effective sample size, latest sample age, median assignment latency, assignment/join completion rate, and 15/30/60 coverage next to the score. Use these initial confidence labels:

- **Insufficient:** fewer than 3 complete attempts.
- **Low:** 3–7 complete attempts or effective sample size below 3.
- **Moderate:** 8–19 complete attempts and effective sample size at least 5.
- **Higher:** at least 20 complete attempts and effective sample size at least 10.

These labels describe evidence quantity, not statistical certainty about the region. The plain-English interpretation is: “How quickly and reliably this client recently encountered teammates for this exact mission in this region.”

A future **region recommendation** may combine observed activity with client ping, but it must show the factors separately. Start with activity as the ranking key and reject recommendations with insufficient evidence; do not bury an arbitrary ping/activity tradeoff in v1.

## MVP

| Feature | User value | Required data | Available now? | Difficulty | Uncertainty |
| --- | --- | --- | --- | --- | --- |
| Current mission browser | Find a desirable mission by zone, objective, PL, reward, alert, biome, or modifier | Normalized provider rotation | Partly; visible on FortniteDB, no approved full feed | Medium | Provider contract is the main blocker |
| Mission detail | See all external metadata plus local regional observations without conflating them | External mission, matches, activity aggregates | Telemetry side yes; external side pending | Medium | Matching can be ambiguous |
| Live matchmaking status | See requested region/fill, assignment progress, datacenter, latency, lobby size, joins/departures | Live watcher and existing parser signals | Yes | Medium | Log format drift and partial lines |
| Attempt history | Audit previous attempts and outcomes | SQLite persistence of normalized observed events | Parser facts exist; persistence prototype exists | Low–medium | Source-log rotation and deduplication |
| Regional comparison | Compare observed activity for the same mission across tested regions | Multiple comparable Fill samples, scoring v1 | Signals yes; coverage sparse | Medium | Recommendations often remain “insufficient data” |
| Evidence-based recommendation | Answer which tested region has the best recent experience | Same-cohort activity, recency, sample quality, ping | Partly | Medium | Must abstain when evidence is weak |
| Provider/settings page | Configure source, refresh, log location, privacy, retention, region labels | Configuration and provider health | No product UI yet | Low | Provider terms and Windows paths |

The smallest useful release is the live matchmaking panel + persisted history + mission-level regional comparison, with current mission enrichment enabled when an approved provider is configured. Mission browsing without live telemetry is not differentiated; telemetry without history cannot support a defensible recommendation. The vertical slice needs both.

## Architecture

Use one Python application process and one SQLite database. Do not add a message broker, hosted database, accounts, containers, or cloud services.

```text
Fortnite log -> tail watcher -> line parser -> normalized events -> SQLite (WAL)
                                                        |             |
Provider -> raw snapshot -> provider normalizer --------+             |
                                                                      v
                                                     matcher -> analytics -> local HTTP/UI
```

### Components

- **Live log watcher:** tail the configured Windows log, track file identity and byte offset, handle truncation/rotation, buffer incomplete lines, and checkpoint only after transaction commit.
- **Telemetry parser:** keep a pure, testable state machine. Emit versioned normalized events rather than writing SQL from regex handlers. Unknown lines are ignored; malformed recognized lines emit diagnostics.
- **Normalized event model:** use small typed dataclasses for registration, assignment, map load, membership, cancellation, and completion. Include event time and source location on every event.
- **Persistence:** SQLite in WAL mode, foreign keys enabled, explicit schema migrations, batched transactions, and unique provenance keys for idempotent re-ingestion. The existing local pipeline is the seed, not a reason to preserve a premature schema.
- **Mission ingestion:** scheduled provider refresh after `valid_until`, immutable raw snapshots, normalization in a transaction, last-good snapshot fallback, visible stale/provider health state.
- **Mission enrichment:** a separate matcher writes `MissionMatch`; it never mutates observed facts.
- **Analytics:** SQL views or Python queries calculate versioned attempt metrics and cached `RegionalActivity`. Recompute when the algorithm version changes.
- **Backend:** a small local HTTP API in the same process is justified once the dashboard exists. FastAPI is reasonable for typed JSON and streaming live updates, but can wait until the persistence slice is stable.
- **Frontend:** a lightweight local web UI served by that process. Start with server-rendered pages plus small progressive enhancement or a minimal TypeScript client; a large SPA framework is unnecessary for v1.
- **Privacy:** never persist auth tokens or raw account IDs in analytics tables. Store only local salted participant hashes when uniqueness is required. Default to local storage and explicit export.

### Dashboard information architecture

#### Mission Browser

- provider freshness and rotation expiry at the top;
- filters for theater, objective, PL, rewards, alerts, modifiers, biome, and four-player missions;
- each row shows mission essentials, local sample count, latest observation age, and recommendation state;
- “insufficient data” is a valid result, not an error.

#### Mission Detail

- external truth card: objective, PL, biome, alert, modifiers, rewards, expiry, provider;
- observed truth card: mission UUID, match method/confidence, first/last seen;
- region comparison table: activity score, confidence, samples, assignment latency, teammate-at-15/30/60 rates, max team, unique teammates, departures, latest sample;
- attempt timeline and an explicit explanation of why the leading region is recommended.

#### Live Matchmaking

- selected mission and match confidence;
- requested region, Fill/No Fill, party size;
- registering/assigned/joining/in lobby/in zone/cancelled state;
- assignment timer, assigned datacenter, session fingerprint;
- live membership timeline with lobby/zone phase separation.

#### History

- filterable attempt table by date, rotation, mission, region, datacenter, fill, party size, and outcome;
- expandable event timeline;
- raw source location for audit, without exposing account identifiers;
- CSV/JSON export later, not in the first slice.

#### Regional Analytics

- only comparable cohorts by default;
- activity score and its components, sample/confidence indicators, recency, ping and assignment reliability shown separately;
- trends by rotation and time of day only after enough samples;
- permanent disclaimer: observed client experience, not regional population.

#### Settings

- Fortnite log path and watcher health;
- database path, retention, and privacy controls;
- provider selection, credentials, last refresh, terms/attribution, stale state;
- region/datacenter aliases;
- analytics version and reset/recompute action.

## First Engineering Task

Build the first persistence vertical slice and lock in the reset finding:

1. Add a migration for `MissionNode`, `MissionAttempt`, `Assignment`, `Region`, `Datacenter`, `LobbySession`, and `MembershipEvent` with source provenance and idempotency constraints.
2. Add a regression test that ingests the two Ride the Lightning captures and asserts: theater equal; internal difficulty equal; mission UUID different; lobby session different; map path different.
3. Feed the existing parser's normalized events into the schema.
4. Add one CLI query that prints the two attempts and their membership timelines from SQLite.

The milestone is working when a fresh database can ingest both files twice without duplicates and reproduce the daily-reset table above entirely from stored normalized facts. This prevents the most damaging identity-model mistake before UI or provider work begins.

## Remaining Research

### Must solve before implementation

- Obtain a supported full-current-mission data contract or explicit permission from FortniteDB, including rate limits, caching, attribution, storage, and redistribution terms. Provider-independent implementation can proceed; shipping FortniteDB ingestion cannot.
- Capture or obtain a permission-safe full-mission fixture that includes stable source keys, rotation validity, objectives, PL, biome, modifiers, rewards, and map position if available.
- Define and test parser deduplication across live tailing, application restart, file truncation, and archived-log re-import.
- Verify the precise log event that marks local login so “remote players before login” is computed consistently.

### Can solve during implementation

- Build and maintain objective, theater, biome, reward, and modifier alias tables from real provider fixtures.
- Calibrate membership retention and departure handling against more complete 60-second captures.
- Decide whether a small FastAPI dependency is worth adding once query endpoints are known.
- Test datacenter-to-region mapping drift and version the mapping.
- Tune recency half-life and confidence thresholds transparently after enough local samples; preserve score versions.
- Determine whether FortniteDB or an approved feed exposes the same Epic node UUID, which would upgrade matching from inferred to deterministic.

### Interesting but non-blocking

- Exact reset propagation delay among the game client and third-party providers.
- Whether mission UUIDs are ever reused after many rotations.
- Whether generated map paths improve biome matching across every objective.
- Time-of-day effects, only after the data set supports stratification.
- Opt-in import/export for aggregating observations across trusted users in a much later product.

Questions already closed—mission UUID behavior within a rotation, session identity, region versus datacenter, membership reconstruction, and the non-identifiability of exact population—must not be reopened without contradictory evidence.

## Long-Term Roadmap

### Phase 1: Evidence-safe local history

Deliver the first engineering task, a repeatable SQLite import, an attempt-history CLI, and reset regression tests. Working milestone: any archived capture can be imported idempotently and queried without raw-log scanning.

### Phase 2: Live vertical slice

Add the tail watcher and a minimal local page showing one attempt from registration through assignment and membership changes. Working milestone: launch the app before matchmaking and watch the attempt persist live.

### Phase 3: Provider contract and mission browser

Implement `FixtureProvider`, then an approved production provider. Add snapshot provenance, current mission browser, freshness, and stale fallback. Working milestone: browse one complete rotation offline after a single refresh.

### Phase 4: Auditable mission enrichment

Implement rotation-aware matching, ambiguity handling, and mission detail. Working milestone: a telemetry attempt links to one external mission only when evidence supports it and displays the match method.

### Phase 5: Activity v1 and recommendation

Implement the versioned score, comparable-cohort rules, confidence labels, regional detail, and abstention. Working milestone: compare regions for an exact mission and explain every point and exclusion.

### Phase 6: Product hardening

Add migration backups, diagnostics, provider health, log-format fixtures, privacy controls, export, packaging, and recovery documentation. Working milestone: a non-developer can install, configure, update, and troubleshoot the local app.

### Explicitly deferred

- exact regional player or session counts;
- queue depth, candidate-session lists, or matchmaking denominators;
- global or regional population estimates derived from assignment latency;
- “live population radar” language or visualizations;
- ML, forecasting, or prediction before a large, representative data set exists;
- cross-user telemetry collection, accounts, central ingestion, or cloud infrastructure;
- automatic region switching or automated matchmaking actions;
- unsupported Epic authentication and storage of user credentials;
- HTML scraping that bypasses provider controls;
- social/profile, MSK, schematic crafting, leaderboards, and collection-book features;
- native desktop packaging until the local web product proves useful.

The long-term product remains an evidence-backed decision aid: rich current mission intelligence plus an honest record of this client's matchmaking experience. Its credibility depends more on preserving those boundaries than on producing a recommendation for every mission.
