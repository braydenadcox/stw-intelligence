# Technical Feasibility Report: Fortnite Save the World Population Intelligence

## Executive Summary

This project is technically feasible only as a limited, evidence-based intelligence layer rather than a full real-time population radar. The strongest path is to infer activity from local, read-only signals that a player’s own Fortnite installation and OS expose while the game is running. That includes local logs, client-side config/state files, launcher artifacts, and possibly public web or API endpoints that are already accessible without modifying the game or bypassing protections.

However, the project is not feasible as a fully accurate, universal “live map” of all STW players. The core limitation is that Epic does not publicly provide a sanctioned API for real-time Save the World player population by region, zone, or mission. The client may expose some useful metadata, but much of the user-visible population intelligence would be inferred indirectly and would likely be noisy, incomplete, and difficult to validate at scale.

The most realistic outcome is a personal analytics tool that estimates:
- when the player is likely in a given region or zone,
- whether nearby matchmaking activity is occurring,
- whether a region appears more active over time,
- and how the player’s own session patterns correlate with population changes.

That is a meaningful product, but it should be treated as a personal observability platform rather than a definitive live population tracker.

---

## Existing Ecosystem

There is already a meaningful ecosystem around STW community tools, but it is largely built around:
- manual community reporting,
- Discord-based coordination,
- third-party websites that track player-facing data,
- and local tools that help players manage their own account, inventory, or mission progress.

The closest existing tools generally do one or more of the following:
- track player progress or collection data,
- provide mission or reward information,
- support STW account management,
- or help with squad coordination.

There is no widely documented, legitimate, public API that exposes a real-time census of active players by region/zone/mission for Save the World. This is the main reason the vision is ambitious and the technical risk is high.

---

## Available Data Sources

### 1. Publicly available information

The following are publicly visible or publicly documented categories of information:
- Fortnite/STW branding, game modes, maps, and known regions/zones from official content and community documentation.
- Public game metadata such as mission names, zone names, and content categories that appear in official websites, patch notes, and community wikis.
- Publicly documented Epic Online Services (EOS) capabilities, though these are platform services for developers and are not a sanctioned STW population API.
- Publicly accessible web endpoints that may expose account/session metadata, but these are typically user-authenticated or not intended for bulk population inference.

### 2. Epic APIs that exist

Epic provides developer-facing platform services, but the public documentation does not indicate a general-purpose API for:
- real-time STW population by region,
- live player counts per zone,
- or live mission occupancy.

What is publicly documented:
- Epic Online Services (EOS) for game backend integration.
- Account and auth services for players and developers.
- Store and distribution services.
- Platform tooling for Unreal Engine and game development.

What is not clearly available publicly:
- a user-facing or developer-facing STW-specific telemetry endpoint,
- a sanctioned live player census API,
- or a public “who is playing where” service.

### 3. STW-specific APIs

There is no strong evidence of a legitimate, public STW API that exposes:
- live population by region,
- live zone occupancy,
- mission-level player counts,
- or regional matchmaking activity.

Any such capability would likely be either:
- private/internal to Epic,
- gated behind authentication and role-based access,
- or unavailable to third parties entirely.

### 4. Local Fortnite logs

Fortnite clients commonly generate logs and diagnostic artifacts in local user folders. These may include:
- launcher logs,
- game client logs,
- crash dumps,
- config files,
- cached state files,
- and platform/authentication artifacts.

Typical log file patterns may include:
- FortniteLauncher.log
- FortniteGame.log
- UnrealEngine logs
- Windows Event Log references to the Fortnite process
- local appdata/cache paths for Fortnite/Epic services

The challenge is not whether logs exist; it is whether the logs contain enough useful STW-specific state to infer player location or population without relying on unsupported assumptions.

### 5. Files created while STW is running

While the game is running, the client may create or update files such as:
- save data or profile state,
- local config files,
- cached assets,
- temporary runtime files,
- session logs,
- and platform service cache files.

These files may reveal:
- that a game session started,
- which content or profile was loaded,
- which account/auth context was active,
- and possibly which region or service endpoint was being used for matchmaking.

But they are unlikely to expose a complete, trustworthy view of other players’ presence.

---

## Possible Detection Methods

### A. Local session fingerprinting

This is the most realistic method.

The tool could infer:
- when the player entered or exited STW,
- whether they were in a mission or lobby,
- when they launched the game,
- and which account/profile context was active.

This can support a personal “activity timeline” and basic occupancy estimation for the player’s own sessions.

### B. Local log parsing for matchmaking/session hints

If Fortnite logs contain references to:
- matchmaking request metadata,
- mission selection,
- region endpoint names,
- lobby/session identifiers,
- or content identifiers,

then a parser could extract useful hints. This would be a best-case scenario, but it is not guaranteed and may be brittle across patches.

### C. Public endpoint monitoring

A tool could monitor public, authenticated, or semi-public endpoints that are legitimately accessible to the player, such as:
- account/session endpoints,
- game-service status pages,
- or public web pages containing known metadata.

This might provide some indirect visibility into the player’s own service state, but not broad population data for others.

### D. Community-derived correlation

The project could correlate local observations with known public patterns:
- server status,
- periodic war/mission rotation events,
- known region availability,
- and player-reported activity windows.

That could produce a probabilistic model rather than a definitive truth.

---

## Reverse-Engineering Signal Inventory

The goal here is not to prove the idea is impossible. The goal is to map every plausible source of legitimate, observable information that could, in principle, reveal something about STW activity, state, or population.

### 1. Epic Games account and web endpoints

- What information could it expose?
  - Account identity, entitlement state, store ownership, platform session context, and possibly profile or service state that is visible to the authenticated user.
- Is it publicly documented?
  - Partially. Epic exposes account and platform documentation, but not a public STW population API.
- Is it legitimate to use?
  - Yes, if the access is from the player’s own account and within the documented terms.
- Has anyone successfully used it before?
  - Yes, in the sense that many projects use Epic account/session endpoints for account or entitlement checks, but no widely documented public project has used them to derive live STW population by region/zone/mission.
- Could it help estimate region / zone / mission / power level / population?
  - Region: weakly, possibly via service context or account-linked region data.
  - Zone / mission: unlikely directly.
  - Power level: possibly for the user’s own account only.
  - Population: not directly.
- Why would or wouldn’t it work?
  - It could reveal account or service state, but it is unlikely to expose the live occupancy of other players.

### 2. Epic Online Services (EOS) SDK services

- What information could it expose?
  - Presence, sessions, lobbies, matchmaking, user identity, platform services, and backend service routing information visible to a game client.
- Is it publicly documented?
  - Yes, at a platform level. The public docs describe the capabilities, but they do not document an STW-specific population feed.
- Is it legitimate to use?
  - Yes, for a game client or authorized integration.
- Has anyone successfully used it before?
  - Yes, many game projects use EOS for lobbies, presence, and sessions. However, no public STW population census service is known to exist through EOS.
- Could it help estimate region / zone / mission / power level / population?
  - Region: possibly, if the service or backend exposes routing or region metadata.
  - Zone / mission: possibly only if the game writes these into session or lobby data.
  - Power level: unlikely.
  - Population: only indirectly and weakly.
- Why would or wouldn’t it work?
  - EOS is powerful but does not automatically expose a global population map; the actual data would have to be present in the game’s own session metadata.

### 3. XMPP or other presence-style services

- What information could it expose?
  - Presence, party state, friend presence, chat, or account/session availability.
- Is it publicly documented?
  - Partially. Some presence-style infrastructure exists in the ecosystem, but public documentation for Fortnite’s specific usage is incomplete or absent.
- Is it legitimate to use?
  - Potentially, but only if used through documented, authorized channels. Accessing or abusing private chat/presence channels would be risky and likely against policy.
- Has anyone successfully used it before?
  - There are community examples of using presence systems in games, but not a widely documented STW population system.
- Could it help estimate region / zone / mission / power level / population?
  - Region: maybe if presence includes service context.
  - Zone / mission: unlikely.
  - Power level: unlikely.
  - Population: maybe at a very coarse social graph level, but not reliable.
- Why would or wouldn’t it work?
  - Presence data is often about availability of users, not occupancy of game-world locations.

### 4. Local Fortnite launcher and Epic Games Launcher artifacts

- What information could it expose?
  - Launch events, install paths, account context, game version, patch status, and service launch metadata.
- Is it publicly documented?
  - Yes, in the sense that these files and folders are part of the local install layout, but their exact contents are not formally documented.
- Is it legitimate to use?
  - Yes, read-only inspection of local files is legitimate.
- Has anyone successfully used it before?
  - Yes, many community tools inspect local game/launcher files for account or version diagnostics.
- Could it help estimate region / zone / mission / power level / population?
  - Region: possible if the launcher or client writes routing information.
  - Zone / mission: possible only if the runtime logs include them.
  - Power level: maybe for the local account if it is persisted.
  - Population: weakly, by tracking when the client is active.
- Why would or wouldn’t it work?
  - It can support a personal activity timeline, but it is unlikely to reveal other players’ live location without more structured telemetry.

### 5. Local log files and diagnostic logs

- What information could it expose?
  - Session starts, matchmaking attempts, backend hostnames, service endpoints, lobby creation, content loading, mission selection, error states, and sometimes user-facing state transitions.
- Is it publicly documented?
  - Not as a stable API; logs are implementation detail and can change across patches.
- Is it legitimate to use?
  - Yes, read-only local logging is legitimate.
- Has anyone successfully used it before?
  - Yes, many reverse-engineering and telemetry projects use logs to reconstruct gameplay or service behavior.
- Could it help estimate region / zone / mission / power level / population?
  - Region: possibly.
  - Zone: possibly if logged.
  - Mission: possibly if logged.
  - Power level: possibly if the client logs profile state.
  - Population: maybe if the logs include multiple session traces or backend counters, but not reliably.
- Why would or wouldn’t it work?
  - This is one of the best local-only avenues, but it depends entirely on whether the game writes the needed fields and whether the format remains stable enough to parse.

### 6. Local config, cache, and state files

- What information could it expose?
  - Stored preferences, recent sessions, local profile snapshots, cached backend responses, and short-lived state that can reveal what the client recently did.
- Is it publicly documented?
  - No, not in a stable or official sense.
- Is it legitimate to use?
  - Yes, if you inspect only files created for your own installation and account.
- Has anyone successfully used it before?
  - Yes, local-state analysis is common in community tooling and debugging.
- Could it help estimate region / zone / mission / power level / population?
  - Region: possible.
  - Zone / mission: possible only if persisted.
  - Power level: possible for local profile data.
  - Population: weakly, through session history and local activity clusters.
- Why would or wouldn’t it work?
  - It is useful for reconstructing your own recent activity, but it is not a direct source of other players’ population density.

### 7. Network metadata from the player’s own machine

- What information could it expose?
  - DNS lookups, IP endpoints, connection timing, service hostnames, CDN paths, connection counts, and coarse backend routing patterns.
- Is it publicly documented?
  - Partially. Endpoint names may be discoverable from the client and OS, but their semantics are not openly documented.
- Is it legitimate to use?
  - Yes, if it is limited to your own traffic and does not involve unauthorized interception or bypassing protections.
- Has anyone successfully used it before?
  - Yes, many telemetry and debugging projects analyze their own traffic patterns or service endpoints.
- Could it help estimate region / zone / mission / power level / population?
  - Region: yes, potentially.
  - Zone / mission: maybe, if the service endpoints or request patterns encode them.
  - Power level: unlikely.
  - Population: weakly, if backend traffic patterns correlate with activity peaks.
- Why would or wouldn’t it work?
  - Network metadata can provide hints about routing and service usage, but it usually does not carry the full semantic payload of in-game population data.

### 8. Public web APIs and status endpoints

- What information could it expose?
  - Service health, store state, account state, patch availability, content metadata, and occasionally public service status.
- Is it publicly documented?
  - Partially, and sometimes not clearly for Fortnite-specific backend features.
- Is it legitimate to use?
  - Yes, if it is public and authorized.
- Has anyone successfully used it before?
  - Yes, many tools rely on public web endpoints for game metadata and service health.
- Could it help estimate region / zone / mission / power level / population?
  - Region: weakly.
  - Zone / mission: not directly.
  - Power level: not directly.
  - Population: not directly.
- Why would or wouldn’t it work?
  - Useful for context, but not likely to expose real-time population without backend support.

### 9. Community APIs and fan-maintained services

- What information could it expose?
  - Publicly reported player stats, mission data, account-linked data, and community-aggregated activity patterns.
- Is it publicly documented?
  - Often not formally documented; they are community-maintained and may be unstable.
- Is it legitimate to use?
  - Usually yes, provided the service terms permit it.
- Has anyone successfully used it before?
  - Yes, many community tools rely on these services for metadata and convenience.
- Could it help estimate region / zone / mission / power level / population?
  - Region: weakly.
  - Zone / mission: possibly if the community source tracks it.
  - Power level: maybe for account-linked public data.
  - Population: only if the community source aggregates enough observed activity.
- Why would or wouldn’t it work?
  - These are promising for trend analysis, but they usually reflect user-submitted or limited observations rather than a true census.

### 10. Discord Rich Presence and related presence systems

- What information could it expose?
  - Whether the player is online, playing Fortnite, maybe in a mode or session, and sometimes a brief status string.
- Is it publicly documented?
  - Yes, at a platform level; the exact Fortnite integration may be client-driven and not officially standardized.
- Is it legitimate to use?
  - Yes, if the user has consented and the integration is standard and user-facing.
- Has anyone successfully used it before?
  - Yes, many projects use Discord presence as a coarse activity signal.
- Could it help estimate region / zone / mission / power level / population?
  - Region: very weakly.
  - Zone / mission: not directly.
  - Power level: no.
  - Population: only very coarse, indirect trends.
- Why would or wouldn’t it work?
  - Useful as an activity signal, but not a strong population estimator.

### 11. Existing open-source STW projects and community tooling

- What information could it expose?
  - Mission definitions, item data, progression data, account metadata, and sometimes local automation hooks.
- Is it publicly documented?
  - Often yes, but usually as community projects rather than official APIs.
- Is it legitimate to use?
  - Usually yes, if they are open-source and used within the game’s rules.
- Has anyone successfully used it before?
  - Yes, many community tools exist for STW utilities and account management.
- Could it help estimate region / zone / mission / power level / population?
  - Region: not really.
  - Zone / mission: possibly for content mapping.
  - Power level: sometimes for own-account progression.
  - Population: not directly.
- Why would or wouldn’t it work?
  - These projects can help with content models and local-state parsing, but they do not solve the core population problem by themselves.

### 12. Existing reverse-engineering efforts and telemetry projects

- What information could it expose?
  - Endpoint names, payload keys, service patterns, content IDs, and sometimes client-side state variables that are not documented publicly.
- Is it publicly documented?
  - Sometimes, but often in community forums or repositories rather than official docs.
- Is it legitimate to use?
  - It can be legitimate if it is based on public analysis of game behavior, not on prohibited access or tampering.
- Has anyone successfully used it before?
  - Yes, there is a long history of game reverse-engineering and telemetry research in the community.
- Could it help estimate region / zone / mission / power level / population?
  - Region: possibly.
  - Zone / mission: possibly if the client exposes them in readable form.
  - Power level: possibly for local account state.
  - Population: weakly at best.
- Why would or wouldn’t it work?
  - This is the most promising research avenue if one is willing to inspect client behavior carefully, but it is still not guaranteed to produce a trustworthy population signal.

---

## What Metadata Can Legitimately Be Observed?

The following are plausible and legitimate to observe from a player’s own machine:
- that the Fortnite client launched,
- that STW was active or loaded,
- that the player used a particular account or platform session,
- that a local save/profile state changed,
- that a mission/lobby session started or ended,
- that specific log lines or config files were written,
- and that the local client connected to known service endpoints.

The following are much less reliable:
- which other players are currently in a zone,
- which zone is most populated at this instant,
- the exact mission population for a given bracket,
- or a trustworthy estimate of real-time regional occupancy across the whole player base.

---

## Feasibility by Question

### 1. What information about STW is publicly available?

Publicly available information includes:
- game mode descriptions,
- known maps/zones,
- mission content names,
- patch notes,
- community wikis,
- and general game information.

What is not clearly public is real-time population telemetry.

### 2. What Epic APIs exist?

Epic exposes EOS and related platform services, but they do not appear to provide a public STW population API.

### 3. What STW APIs exist?

There is no strong public evidence of a legitimate STW-specific live population API for player census data.

### 4. What local Fortnite logs exist?

Likely yes: launcher/game logs, crash logs, and platform service logs. Their contents vary by client version and installation path.

### 5. What files are created while STW is running?

Likely yes: logs, cache files, temp files, saved configuration, and session state. Their exact structure is version-specific and not guaranteed to be stable.

### 6. What metadata can legitimately be observed?

Local account/session activity, client launch events, local profile state changes, and perhaps service endpoint references can be observed. Real-time crowd density data for other players cannot be reliably inferred from local files alone.

### 7. Can region be detected?

Yes for the local player's matchmaking attempt. Controlled August 2026 captures contain the explicit `Matchmaking:Region` attribute (including NAE and NAW) in the matchmaking registration line. This is a direct observation rather than a latency-based inference, although the format remains version-dependent.

### 8. Can zone be detected?

Yes for the locally selected/joined activity in the tested build. Matchmaking registration contains an STW theater UUID, and the subsequent map load exposes the world/biome path. Controlled captures calibrate theater UUIDs for Stonewood, Plankerton, Canny Valley, and Twine Peaks. Human-readable labeling still requires a maintained UUID mapping.

### 9. Can mission IDs be detected?

Yes for the local matchmaking attempt. Registration contains an STW mission UUID. Controlled tests show that it remains stable across different lobby instances, region changes, and fill changes for the same selected mission, while the assigned session ID changes per lobby. The UUID should be treated as a rotating mission-instance identifier and enriched with catalog data for a human-readable mission type and pre-join Power Level.

### 10. Can matchmaking events be detected?

Yes. The logs expose registration, assignment, join, map load, match start, and return-to-frontend transitions. They also expose the members and indices of the local `HumanCampaign` team, allowing an exact local-lobby occupancy snapshot. This still does not reveal players in lobbies the local client did not sample.

### 11. Can player Power Level be determined?

The selected mission's power bracket can be calibrated after joining. The tested `FortGameStatePvE` difficulty values map as follows: PL15=7, PL40=20, PL70=30, PL140=50, and PL160=52. The pre-join registration line does not contain this numeric value in the tested build, and these captures do not establish a reliable way to determine every teammate's personal Power Level.

### 12. What information is impossible to determine?

The following are likely impossible or impractical to determine from legitimate local-only inspection:
- live population counts of other players by region/zone/mission,
- true real-time occupancy for other players,
- participants outside the local player’s sampled lobby,
- or a reliable global STW population census.

### 13. What existing community tools already solve parts of this problem?

Existing tools mostly solve adjacent problems rather than the core one:
- STW account managers,
- mission/collection trackers,
- squad coordination tools,
- community Discord servers,
- and public wikis.

None are known to provide a legitimate, public, real-time population map of STW players by region/zone/mission.

### 14. What would an MVP realistically be?

A realistic MVP would be a personal analytics dashboard that:
- tracks the user’s own STW sessions,
- records when they queue, launch, and leave activities,
- logs local evidence of SP/mission/lobby events if available,
- estimates their likely region/zone based on local evidence,
- and surfaces patterns over time such as “you most often play in the evening” or “your sessions cluster around certain windows.”

That MVP is valuable for personal insight but should not promise live population estimates for others.

---

## Highest-Value Investigative Avenues

If the objective is to find any unexplored but legitimate technical avenue worth pursuing, the strongest candidates are:

1. Local log parsing
   - Best chance of exposing session, matchmaking, or content identifiers that could hint at region or mission.
   - Weakness: brittle and version-dependent.

2. Local state/cache inspection
   - Could reveal recent sessions, profile state, or content selection details.
   - Weakness: likely not enough to expose true live player counts.

3. Network metadata analysis of the player’s own traffic
   - Could reveal backend service routing and request patterns that correlate with gameplay state.
   - Weakness: usually too coarse to infer population directly.

4. Community-derived aggregation of public observations
   - Could produce trends, but likely not a true live census.
   - Weakness: depends heavily on the quality and scale of community data.

5. Reverse-engineering client-side state exposure
   - If the game writes region, zone, mission, or party state into logs, caches, or config files, this could become a very strong signal.
   - Weakness: requires careful empirical validation and may not exist.

## Technical Risks

1. Data scarcity
   - The local client exposes structured region, theater, mission, session, and local-team state in the tested build, but it does not expose unsampled lobbies or a global population count.

2. Version instability
   - Fortnite updates can change log formats, config locations, or service behavior, breaking parsers.

3. False confidence
   - A parser might infer a region or zone incorrectly and present it as fact when it is only a probabilistic guess.

4. Privacy and compliance
   - Even read-only local data collection should be handled carefully, especially if the tool stores account-related metadata.

5. Limited observability
   - The tool may be unable to measure anything beyond the user’s own activity without direct backend access.

---

## Unknowns Requiring Investigation

The August 2026 captures answer the first two empirical questions: Fortnite logs do contain structured STW matchmaking/session data, and they directly record region, theater UUID, mission UUID, fill state, assigned session, loaded map, and the local matched team. Remaining unknowns are:
- Whether launcher or game telemetry caches add useful history beyond the log.
- Whether a legitimate catalog source can enrich a mission UUID with type and Power Level before the local player joins it.
- How mission UUIDs behave across the daily rotation.
- How consistently repeated samples enter an existing lobby versus create a new one.
- Whether simultaneous consenting clients can deduplicate observations by assigned session ID.

These questions should be answered with a proof-of-concept investigation, not by assumption.

---

## Recommended Architecture

A conservative architecture would be:
- a local collector that inspects read-only files and logs,
- a parser layer that extracts structured events when present,
- a normalization layer that converts findings into a timeline of observed STW activity,
- and a dashboard that shows personal patterns and probabilities rather than authoritative player census data.

Recommended components:
- collector service for local files/logs,
- parser and schema layer for event extraction,
- state store for session timelines,
- analytics engine for trend detection,
- and a dashboard for personal insight.

The architecture should be designed around uncertainty:
- every inference should be labeled as “observed,” “inferred,” or “probable,”
- and the system should tolerate missing data gracefully.

---

## Recommended MVP

The most realistic MVP is:
- a desktop/local app that monitors the player’s own Fortnite/STW activity,
- records session start/end times,
- captures available local logs and artifacts,
- extracts any mission/session hints if present,
- and produces a personal dashboard showing:
  - session history,
  - activity trends by time of day,
  - likely queueing patterns,
  - and a confidence-based view of inferred state.

This MVP should explicitly avoid claiming:
- live population by region,
- real-time global occupancy,
- or guaranteed zone detection.

---

## Recommendation on Whether the Project Should Continue or Pivot

The project should continue, but only as a personal observability and analytics tool rather than a full real-time STW population intelligence platform.

Recommendation:
- Continue if the goal is to build a personal dashboard for the player’s own activity and inferred behavior patterns.
- Pivot if the goal is to deliver a reliable, real-time map of other players by region/zone/mission. That version is not currently supported by public, legitimate, documented data sources.

In short: the concept is feasible as a personal intelligence tool, but not as a definitive live population radar without access to private or protected backend data.

## Revised Conclusion for the Reverse-Engineering Objective

There is at least one plausible unexplored technical avenue worth investigating: local client telemetry exposure through logs, caches, state files, and network metadata. If Fortnite writes even a small amount of structured state about current session, matchmaking, backend routing, mission selection, or service context, that could become a legitimate signal for estimating active STW state.

The most important takeaway is this:
- the absence of a documented public API does not mean the client has no usable signal,
- and the absence of a known public STW population feed does not mean there is no local or indirect signal worth mining.

The project is therefore not dead. It is simply a research problem first and an engineering problem second.
