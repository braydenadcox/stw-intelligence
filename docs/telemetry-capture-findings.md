# STW Telemetry Capture Findings

## Result

The August 6 captures establish that a local, read-only Fortnite log parser can reliably observe the local player's matchmaking region, fill setting, selected theater, selected mission UUID, assigned game session, loaded biome/map, post-join mission difficulty, and the members of the matched STW team.

This is enough for a useful personal matchmaking timeline and for sampled lobby-occupancy measurements. It is not a global population feed: the client only reveals a lobby after the local player queues into it, and an empty/new lobby is not evidence that nobody else is queuing for the same mission.

## Confirmed fields

| Signal | Log field | Confidence | Timing |
| --- | --- | --- | --- |
| Region | `Matchmaking:Region` (`NAE`, `NAW`) | Direct | At queue registration |
| Fill | `Matchmaking:MatchFill` (`Public`, `Private`) | Direct | At queue registration |
| Zone/theater | STW `Theater` UUID | Direct ID; label from controlled test | At queue registration |
| Selected mission | STW `Mission` UUID | Direct | At queue registration |
| Mission power bracket | `FortGameStatePvE, Difficulty N` | Direct internal rating; PL mapping empirically calibrated | After joining/loading |
| Mission biome/map | `LogLoad: LoadMap` path | Direct | After joining/loading |
| Game session | 32-character `Session Id` | Direct but ephemeral | On assignment/join |
| Visible team size | `HumanCampaign` team-member indices | Direct snapshot | During lobby/world replication |

The registration examples expose the values in one structured line. The Twine PL160 capture records NAE, Public fill, Twine theater, and mission UUID `b2252cdd-...` at [line 30862](../logs/telemetry-captures/twine-ride-the-lightning-160.log#L30862). The region-switch capture records the same mission UUID first on NAE at [line 31922](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L31922) and then on NAW at [line 58500](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L58500).

## Controlled-test conclusions

### Mission UUID identifies the selected mission, not the lobby

- The two Repair the Shelter PL160 joins use the same mission UUID, `5d96f04d-...`, at [line 31344](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L31344) and [line 61541](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L61541).
- Those joins receive different game-session IDs (`b588b852...` and `580c3641...`) and load through different servers at [line 32766](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L32766) and [line 62777](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L62777).
- The first lobby visibly fills through team index 3 at [line 37192](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L37192); the second independently reaches index 3 at [line 62297](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L62297).

Therefore the mission UUID is suitable as the join key for repeated samples of one current mission. Session ID is the join key for one particular lobby instance.

### Region and fill are explicit and independent of mission identity

- The Resupply PL140 test keeps mission UUID `2c4dd5b2-...` while `Region` changes from NAE to NAW.
- The Rescue the Survivors PL140 test keeps mission UUID `91d8f1fb-...` while `MatchFill` changes from `Public` at [line 29995](../logs/telemetry-captures/twine-140-rescue-survivors-fill-to-no-fill.log#L29995) to `Private` at [line 55972](../logs/telemetry-captures/twine-140-rescue-survivors-fill-to-no-fill.log#L55972).
- Private/no-fill also explicitly adds `MatchType:NewMatch`. Public matchmaking omits that attribute in these captures, so omission must not be parsed as a literal match type.

### Power level has a stable post-join internal rating

| Labeled mission PL | Logged PvE difficulty | Evidence |
| ---: | ---: | --- |
| 15 | 7 | [stonewood line 34161](../logs/telemetry-captures/stonewood-fight-the-storm-15.log#L34161) |
| 40 | 20 | [plankerton line 36521](../logs/telemetry-captures/plankerton-repair-shelter-40.log#L36521) |
| 70 | 30 | [canny line 37169](../logs/telemetry-captures/canny-ride-lightning-70.log#L37169) |
| 140 | 50 | [Twine line 64000](../logs/telemetry-captures/160-140p4-ride-lightning-twine-same-mission-type-diff-pl.log#L64000) |
| 160 | 52 | [Twine line 34886](../logs/telemetry-captures/160-140p4-ride-lightning-twine-same-mission-type-diff-pl.log#L34886) |

The PL160 and PL140 Ride the Lightning selections have different mission UUIDs (`b2252cdd-...` and `e16ba007-...`). That proves the UUID distinguishes the selected mission occurrences, but it does not prove that power level can be decoded from the UUID itself. The numeric difficulty appears only after world load in this build. Pre-join PL requires enrichment from a current mission catalog or a learned UUID-to-mission record.

### Theater UUID calibration

| Theater | UUID |
| --- | --- |
| Stonewood | `33A2311D4AE64B361CCE27BC9F313C8B` |
| Plankerton | `D477605B4FA48648107B649CE97FCF27` |
| Canny Valley | `E6ECBD064B153234656CB4BDE6743870` |
| Twine Peaks | `D9A801C5444D1C74D1B7DAB5C7C12C5B` |
| STW staging/Homebase | `C4C2925C466832846A042E97F00FE5CF` |

## Population implications

The logs expose the local matched squad. `HumanCampaign` indices are a cleaner occupancy signal than counting display names: index 0 is the local player and indices 1–3 are matched teammates. This supports measurements such as “this sampled lobby reached 4/4,” along with queue-to-assignment latency and whether matchmaking created or backfilled a session when explicitly logged.

It does **not** support multiplying one lobby result into a regional population count. A defensible analytics layer should retain each observation as a sample keyed by timestamp, region, theater UUID, mission UUID, fill mode, and session ID. Aggregate only rates such as successful match incidence, observed team size, and assignment latency, with sample counts and confidence intervals.

## Extractor

Run the dependency-free extractor against one or more captures:

```powershell
python tools/analyze_telemetry.py logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log
```

It emits JSON and deliberately excludes display names, server hosts, local account IDs, and encryption tokens. `distinct_observed_team_members` spans the entire file, while `largest_observed_team_size` is the largest replicated team index plus one; neither should be interpreted as a concurrent global count.

The `attempts` collection correlates each registration with its assignment timestamp, assignment latency, newly observed session ID, map loads, team-size high-water mark, and internal difficulty. Outcomes are conservative: `joined` requires a PvE difficulty snapshot, `assigned_not_joined` means the service assigned a session but no world-load confirmation followed, and `registered_not_assigned` means no assignment was observed before the next attempt or end of file.

This distinction recovers the action sequence in the annotated PL comparison without relying on the note: the second PL160 attempt was assigned after 10.601 seconds but not joined; the first PL140 attempt was assigned after 11.181 seconds but not joined; and the following PL140 attempt joined with internal difficulty 50. The two same-mission Repair the Shelter samples both joined full four-player lobbies, with different session IDs and assignment latencies of 2.666 and 2.457 seconds.

## Next highest-value tests

1. Queue the same mission/region with fill several times, canceling after assignment, and record assignment latency plus whether the assigned session ID repeats. This tests how often samples hit an existing lobby.
2. Run simultaneous controlled clients into the same mission and region. Verify whether both see the same session ID and team indices, which would validate cross-client deduplication.
3. Queue the same mission with a known party of two, then three. This separates pre-made party size from strangers added by matchmaking.
4. Capture a failed/no-session timeout. A successful empty lobby and a matchmaking failure represent different population evidence.
5. Capture the same mission UUID before and after the daily mission rotation. This determines UUID lifetime and confirms whether a catalog snapshot must be versioned by rotation time.

Avoid putting free-form notes at line 1 of raw logs in future captures if byte-for-byte provenance matters. Prefer a sidecar `.md` or `.json` manifest with expected region, mission label, PL, fill, action sequence, and wall-clock markers.
