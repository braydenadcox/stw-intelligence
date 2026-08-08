# Regional STW Population Signal Investigation

## Verdict

No accessible artifact contains a direct STW player count by matchmaking region, a queue depth, an active-session count, or a candidate-session count. Exact regional population is therefore **currently unsupported**.

The project can nevertheless measure a useful, explicitly sampled **regional matchmaking activity index**. Its strongest inputs are assignment latency, assigned subregion, local-lobby team-size observations over fixed windows, time to the first other player, assignment success, and session reuse. These observations must remain stratified by mission UUID, theater, Fill, party size, build/bucket, region, and time.

The investigation covered the repository, eleven telemetry captures, repository backup logs, two older Fortnite crash logs, 48 replay files, configuration and save-state files, hotfix/EMS caches, CMS/cache metadata, `AnalyticsLocalDB.json`, and likely local/debug endpoint markers. Packaged game content and encrypted network traffic were not reverse engineered.

## Quantified Capture Result

Across the eleven controlled captures, the extractor found 25 records with `STW:Type=Mission`:

- 24/25 received an MMS assignment; 22 were joined, 2 were deliberately not joined, and 1 was abandoned before assignment while changing region.
- All 24 assigned session IDs were unique, including repeat queues of the same mission UUID.
- Physical assignments were BR to SAO (5), NAE to OH (5), NAE to VA (7), NAC to IA (1), NAW to OR (4), EU to FR (1), and OCE to SYD (1).
- Assignment latency ranged from 2.174 to 13.992 seconds, with a 9.520-second mean.
- In public joined samples, all 5 assignments under 5 seconds observed another player within 30 seconds. Two of 16 assignments at or above 5 seconds did so.

That last split remains an interesting association: fast assignment may indicate backfill into an already active lobby. The new five-region capture proves that the reverse inference is invalid, however: NAE took 13.992 seconds yet started with two observed players. A roughly 10-14 second assignment is therefore ambiguous, not evidence of a newly created empty lobby.

## Five-Region Same-Node Test

The Retrieve the Data capture holds the mission UUID, Twine theater, PL160/internal difficulty 52, Public Fill, solo party, build, and matchmaking bucket constant while changing only the requested region. An initial BR registration at [line 29866](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L29866) was abandoned before assignment while switching to NAE, so it is not part of the five joined samples.

| Requested region | Assigned site | Site QoS ping | Assignment | Observed at match start | Largest within 30s | Eventual high-water | Observation window |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NAE | VA | 87 ms | 13.992 s | 2 | 2 | 4 | 110.307 s |
| NAC | IA | 59 ms | 10.953 s | 1 | 1 | 2 | 133.122 s |
| NAW | OR | 26 ms | 13.882 s | 1 | 1 | 1 | 141.632 s |
| EU | FR | 165 ms | 12.002 s | 1 | 1 | 1 | 117.956 s |
| OCE | SYD | 159 ms | 13.680 s | 1 | 1 | 1 | 141.630 s |

Evidence lines for the five mission registrations and assignments are [NAE 31019/31050](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L31019), [NAC 58465/58497](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L58465), [NAW 88083/88117](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L88083), [EU 116297/116308](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L116297), and [OCE 144819/144844](../logs/telemetry-captures/160-twine-retrieve-data-east-central-west-europe-oceania.log#L144819).

This directly confirms the logical-to-physical mappings for all five requested regions. It also shows that assignment latency is not simply network ping: EU assigned faster than low-ping NAW, and NAE assigned VA despite OH having the lower measured ping. Backend site choice may incorporate existing-session location or allocation capacity, but one sample per region cannot distinguish those explanations.

At this moment and for this exact node, NAE had the strongest sampled team activity: another player was already observed at match start and the lobby reached four after 96.903 seconds. NAC gained a second player after 103.488 seconds. No teammate was observed in NAW, EU, or OCE during their roughly two-minute windows. These are five localized samples, not regional population rankings.

## Signal Inventory

Examples below omit account IDs, player names, host addresses, tokens, and full ephemeral identifiers.

| Signal or event | Example | Location and scope | Observed changes | Population value | Confidence |
| --- | --- | --- | --- | --- | --- |
| `Matchmaking:Region` | `NAE`, `NAW`, `BR` | MMS `FMatchmakingClient::Register`; per attempt. Same mission switches at [NAE line 31922](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L31922) and [NAW line 58500](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L58500). | Changes with region; independent of mission, lobby, Fill, and players. | Exact requested logical pool; essential grouping key, not a count. | Confirmed |
| Assigned `Matchmaking:SubRegion` | `OH`, `VA`, `OR`, `SAO` | `FMatchmakingClient::OnClientMatchAssigned`; per assignment. See [OH line 31947](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L31947) and [OR line 58537](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L58537). | Changes with region and between lobbies in one region; not driven by Fill or mission identity. | Exact physical allocation site. Allows regional results to be split by datacenter and prevents latency from being mistaken for population. | Confirmed |
| `SubRegionPings` | `OR:29`, `OH:83`, `VA:84` ms | Registration attributes; per attempt/client network snapshot. | Changes with network path and time, not lobby population. Mission/Fill changes usually retain the same snapshot in one run. | Control variable for network latency and reachability. | Confirmed |
| QoS datacenter result | `OR (NAW): 4/4 ... 29ms` | `LogQos` startup evaluation; per client evaluation. See [QoS block](../logs/telemetry-captures/twine-resupply-140-na-east-to-na-west.log#L7477). | Changes with client route/time; independent of mission/lobby/Fill/players. | Separates transport quality from matchmaking delay. Query success is probe reachability, not session supply. | Confirmed |
| `AutoRegion ... datacenters available` | `NAE:2`, `EU:3`, `OCE:1` | `LogQos`; per configured region. See [line 5379](../logs/telemetry-captures/twine-ride-the-lightning-160.log#L5379). | Stable across these captures. | Infrastructure/config count only. It does not mean that many live game servers or sessions exist. | Confirmed, not population |
| Assignment latency | `2.457s`, `11.040s` | Register timestamp to `OnClientMatchAssigned`; per attempt. | Changes across lobby, mission, region, and Fill. | Best current indirect signal when combined with early occupancy. The observed fast/full association needs controlled validation. | Strong hypothesis |
| `HumanCampaign` team index | indices `0` through `3` | `LogParty`; per visible local lobby/member update. A full observed index set reaches index 3 at [line 37192](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L37192). | Changes as players differ, arrive, or leave; Fill directly affects it. | Direct evidence of each observed member and a team-size high-water mark. It says nothing about unsampled lobbies. | Confirmed |
| Team timing fields | largest observed within 30 seconds `=4`, first teammate `5.760s` | Derived from timestamped `HumanCampaign` updates; per assignment. | Changes with lobby/player arrival and observation duration. | Strong activity/fill-rate metric if every sample uses the same time window. It is a high-water mark because member-removal events are not yet parsed. | Confirmed measurement; population relation is a hypothesis |
| `PartyMemberAccountIds` count | `party_size=1` | MMS registration; per attempt. Raw IDs are not retained. | Changes with premade party, not strangers found by matchmaking. | Required control so premade members are not counted as regional matchmaking supply. | Confirmed |
| Assigned `sessionId` | 32-character ephemeral ID | Assignment server attributes; per lobby. Two same-mission queues receive different IDs in the Repair the Shelter capture. | Changes with lobby; remains independent of mission UUID. May repeat on rejoin/reconnect. | Enables lobby deduplication, repeat-session rate, and multi-client joins to a known lobby. It is not a population count. | Confirmed |
| `MatchId` and `TicketId` | UUID / 32-character ID | MMS assignment and registration; per assignment/ticket. | Changes each attempt, including when mission is unchanged. | Correlation keys only. No count or ordering semantics were found. The extractor intentionally does not retain them. | Confirmed, low value |
| Matchmaking bucket attributes | `BuildId=55554820`, `LinkCode=campaign`, `PreferredLinkCodeVersion=16`, `PlaylistVersion=1`, platform/input/language | MMS registration; per attempt/client. | Stable in this capture set; can change with build, platform, or client settings. | Necessary stratification keys because incompatible buckets can split a region's available players. No bucket size is exposed. | Confirmed |
| Mission/theater/Fill/type | mission UUID, theater UUID, `Public`/`Private`, `Mission` | MMS registration; per attempt/selected node. | Controlled captures prove mission, theater, and Fill can change independently of session. | Essential conditioning variables. A regional aggregate that ignores mission choice would be badly biased. | Confirmed |
| Legacy existing-session result | `Testing Existing Sessions -> No matches available` or `-> Joining Existing Session` | Two local 2025 crash logs; per legacy search poll. One example is `...BA24.../FortniteGame.log` lines 21434-21500. | Changed across repeated polls while region remained NAW. Mission/lobby changes are not recoverable enough to compare cleanly. | Best explicit session-supply observation: whether one exact search found a joinable existing session. Current MMSv2 captures do not emit it. | Confirmed for legacy client; current availability unknown |
| `QueuedPlayers=0` | always `0` | MMS service-state lines; per local client operation. | Did not change across regions, missions, Fill, assignment latency, or full/solo lobbies. | Describes queued local client operations, not regional waiting players. | Probably irrelevant |
| `Going into new session provided by MMS` | identical wording | `ConnectToReservationBeacon`; per current join. See [line 31374](../logs/telemetry-captures/twine-repair-shelter-160-same-mission-different-lobby.log#L31374). | Appears for both visibly populated and solo sessions. | Means the session came from MMS and bypassed external reservation; it does not reliably prove a newly created empty lobby. | Probably irrelevant |
| Find-session `Ping:9999ms` | `9999ms` placeholder | `FindSession`; per assigned session lookup. | Repeats despite successful joins and normal QoS. | Placeholder, not measured server latency or population pressure. | Probably irrelevant |
| Join HTTP result | `204`, payload replaced by `<Payload>` | MCP `JoinInternetSession`; per join request. | Successful joins are consistent; no capacity metadata is logged. | Failure-rate telemetry could be useful during errors, but successful status is not a population signal. | Weak lead |
| EOS assignment-message `RawLatencyMs` | positive, duplicate, and sometimes negative values | `LogEOSMessaging`; per delivered message. | Changes even for duplicate assignment messages; negative samples indicate clock/timestamp artifacts. | Backend delivery/clock diagnostic, not reliable queue pressure. | Weak lead |
| Beacon/net-driver endpoint | redacted host plus game/beacon port | connection logs; per server connection. | Changes with lobby/session. | At most corroborates server change or coarse geography. Assigned subregion is cleaner; host cardinality cannot be converted to server capacity. | Confirmed, low value |
| Persisted selection state | last region, Fill, theater, mission | `GameUserSettings.ini`; latest/recent local selection. | Changes after player selection; session fields were empty. | Useful recovery if the log registration is missing, but stale and contains no other-player state. | Confirmed, low value |
| Crash context region | `RegionId=NAW`, `SubregionId=NCAL` | `CrashContext.runtime-xml`; per crash snapshot. | Both accessible crashes recorded the then-current routing context. | Fallback region evidence only; no count. | Confirmed, low value |

## Direct, Indirect, Infrastructure, and Supply Findings

### Direct population signals

None were found. Searches for population, player count, concurrency, occupancy, capacity, waiting players, candidate sessions, and related structures produced only unrelated engine/game-feature text. The current client does not log a regional denominator.

The closest direct observation is each `HumanCampaign` member update in the sampled lobby. The current extractor derives high-water marks; it does not yet reconstruct member departures.

### Indirect population signals

Ranked strongest to weakest:

1. Assignment latency combined with team-size high-water marks over fixed windows and time to first other player.
2. Assignment success/failure or timeout rate, once genuine failures are captured.
3. Existing-session encounter rate, if the legacy outcome or a current equivalent can be recovered.
4. Assigned-session reuse rate, especially across simultaneous consenting clients.
5. Join/reservation failure and retry rates, after separating service incidents from full-session races.
6. Raw EOS delivery latency and network timing, mainly as nuisance/control variables.

### Region infrastructure signals

Requested region and assigned subregion are direct. The hotfix cache also defines the region-to-QoS-site map: NAE has OH/VA, NAW has OR/NCAL, EU has DE/GB/FR, NAC has TX/IA/MX, with single primary sites for BR, OCE, ASIA, and ME in this snapshot. Raw probe hosts are intentionally not stored or reported. QoS pings and probe success provide reachability, not capacity.

### Session supply signals

The strongest signal is the old `No matches available` versus `Joining Existing Session` state. Across the two old crash logs the extractor found 35 search polls: 5 found an existing session, 29 reported none, and 1 was incomplete. These polls are not independent samples; several belong to repeated polling within one matchmaking episode.

Current MMSv2 instead returns one assignment containing a session ID and subregion. It exposes neither the number of candidates considered nor whether the assigned server was newly provisioned. The repeated phrase `new session provided by MMS` cannot fill that gap because it appears in known populated lobbies too.

## Other Artifacts Examined

- `AnalyticsLocalDB.json` contains only `LastVersionCompleted`; it is not an event store.
- The 48 replay files are compressed per-session recordings and contain none of the searched regional population/session-supply markers. They could reconstruct a recorded match, not live unsampled demand.
- CMS/download caches contain media and manifests. EMS hotfix files contain configuration such as datacenter definitions, not live utilization.
- `GameUserSettings.ini` retains selections, not assigned population or session capacity.
- Save files and packaged `.ucas`/`.pak` content showed no readable live-population table. Packaged content is static in any case.
- MMS/EOS assignment messages say a JWT was decoded, but the token/body is not logged. MCP request and response bodies are represented as `<Payload>`, so there is no local blob to decode.
- No usable localhost HTTP/WebSocket/debug population endpoint or API-capture artifact was found. An `OnlineAPICapture` plugin name and an internal asset-snapshot message endpoint appear in startup logs, but neither exposes a callable population service in this environment.

## Immediate Instrumentation

`tools/analyze_telemetry.py` now emits:

- assigned subregion and assigned session ID;
- whether that assigned session repeats within a file;
- party size without retaining member identities;
- timestamped team-size growth, time to first teammate, time to full team, match-start observed size, largest size within 30 seconds, and observation duration;
- registration `SubRegionPings` and matchmaking bucket attributes;
- QoS region/site counts, per-site ping/probe results, and recommended region/subregion;
- legacy existing-session search results and search latency.

The raw collector should also preserve explicit cancellation/failure/timeout lines when they occur. Do not treat an attempt as a failed assignment merely because a capture ended.

## Next Experiments

1. **Validate the latency/backfill hypothesis.** Solo queue one fixed public mission 20-30 times in one region. For every assignment stay exactly 60 seconds, then leave. Compare assignment latency with the largest observed team size by 15, 30, and 60 seconds. This is the highest-value experiment.
2. **Interleaved region comparison.** Repeat the same fixed node in NAE, NAW, and EU in randomized rotation, not one region at a time. Keep Fill, party size, hardware, and the 60-second observation window fixed. This limits time-of-day drift.
3. **Known existing-lobby validation.** Have a consenting second client wait alone in a known public mission, then queue the first client into the same node/region. A shared session ID proves an existing-session join and calibrates the latency/team signature.
4. **Known new-lobby baseline.** Use Private/No Fill on the same node as an allocation-control condition. It cannot measure population, but it tests whether the roughly 10-second path is simply server provisioning. Public and private results must never be pooled.
5. **True failure capture.** On a very low-demand node, let matchmaking run to its natural terminal state rather than canceling. Preserve timeout, retry, heartbeat, and state-transition lines. This tests whether current MMSv2 exposes an equivalent to the legacy no-match outcome.
6. **Party-size calibration.** Repeat with a known party of two so the parser can verify that party size is separable from strangers added by matchmaking. Skip this if the eventual collector will enforce solo-only sampling.

## What Can Be Estimated

- **Exact now:** requested region, assigned subregion, selected mission/theater/Fill/bucket, assignment outcome and latency, sampled session identity, and visible team-member update events.
- **Estimable after controlled sampling:** relative activity by region for a fixed mission/bucket/time window; probability of seeing another player within a fixed interval; assignment and existing-session encounter rates; lower bounds on distinct observed lobbies and players in a cooperating-client network.
- **Currently unsupported:** exact active players by region, exact joinable sessions by region, regional queue depth, or a defensible split of a global STW total. Queue samples overrepresent players currently matchmaking and popular missions, while missing players already in missions, private sessions, SSDs, and unobserved mission buckets.

## Final Ranked Leads

1. Assignment latency plus early team occupancy: strongest current indirect lead.
2. Assigned subregion: strongest physical region-identification lead.
3. `HumanCampaign` team timeline: direct sampled-member evidence and team-size high-water marks.
4. Legacy existing-session outcome: strongest explicit session-supply lead, but absent from current captures.
5. Session-ID reuse and cross-client deduplication.
6. Assignment success, failure, timeout, and retry rates.
7. Party size and matchmaking bucket fields as necessary controls.
8. QoS ping/probe data as latency controls.
9. Persisted/crash region state as fallback identification.
10. Join/reservation errors and EOS delivery timing as weak diagnostics.

The surprising result is the clean fast-assignment/early-teammate split in this small dataset. It gives the project a concrete next hypothesis to falsify. The equally important negative result is that neither current MMS assignment metadata nor the extensive local cache set contains a hidden regional denominator.

## Requested Bottom Line

1. **Best direct lead:** no regional count exists; the best direct observation is member updates from the one sampled `HumanCampaign` team.
2. **Best indirect lead:** assignment latency combined with the largest observed team size within 30 seconds and time to first other player.
3. **Best region-identification lead:** requested `Matchmaking:Region` plus assigned `Matchmaking:SubRegion`.
4. **Best session-supply lead:** legacy `No matches available` versus `Joining Existing Session`, if a current equivalent can be captured.
5. **Instrument now:** region/subregion, mission/theater/Fill/bucket, party size, assignment result/latency, session ID/reuse, fixed-window team high-water marks, teammate arrival, QoS, and failure/retry states.
6. **Next experiment:** first validate fast-assignment versus known existing/new lobbies, then run interleaved NAE/NAW/EU repeated samples with a fixed 60-second window.
7. **Feasibility:** exact counts are unsupported; relative regional matchmaking activity is estimable after controlled sampling.
8. **Ranked leads:** the ten-item ranking immediately above orders every promising lead found.
9. **Code changes:** the extractor additions listed under Immediate Instrumentation expose and store the useful signals without retaining identities or hosts.
10. **Surprise:** the under-5-second assignment group still aligned perfectly with seeing another player by 30 seconds, but the five-region test showed that slow assignment can also lead directly to an occupied lobby. The older client also logged explicit existing-session search failures that current MMSv2 hides.
