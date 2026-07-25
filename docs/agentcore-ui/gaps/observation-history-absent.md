# GAP / DECISION — Observation history is absent in agentcore mode

**Status:** decided (2026-07-25). Converged with the user, verified against the live
AgentCore instance (eu-west-2, acct 708306701628). This doc records what is lost and
why the reconstruction is not worth building. It does not re-open the decision.

**Decision in one line:** the Observations tab is **hidden** in agentcore mode. Raw
observation browsing, the observation audit trail, and reinforcement inspection have no
faithful server-side source, and the only reconstruction path
(`ListActors` -> `ListSessions` -> `ListEvents`) is N+1+M, lossy, and current-metadata-only.
Do not build it.

---

## 1. What the data is / what the UI does today (sqlite mode)

In sqlite mode the Observations tab is a first-class browser over the `observations`
content table. It exposes three reads and one write:

- **Project filter dropdown** — `queries.observation_distinct_projects`:
  `SELECT DISTINCT project FROM observations`. Enumerates every project that has ever
  called `observe()`.
- **List panel** (filtered, day-grouped, 30s auto-refresh) —
  `queries.observation_list_for_ui`: `SELECT ... FROM observations` with optional
  `WHERE` on `project`/`status`/`outcome`/`component`, `ORDER BY created_at DESC`,
  `LIMIT`. Renders, per row, the **mutable** columns `status`
  (`active` / `consumed_without_reflection` / `archived` / …) and
  `reinforcement_score`, plus `episode_id`.
- **Detail drawer** — `queries.observation_detail`: three reads on one connection —
  (1) the full observation row; (2) the **audit trail**
  `SELECT ... FROM audit_log WHERE entity_type='observation' AND entity_id=?`;
  (3) **linked reflections** via `reflections JOIN reflection_sources` (which
  reflections this observation seeded).
- **Promote to semantic** (write, shown only when `status='active'`) —
  `SemanticMemoryService.create_from_observation`, inside `SAVEPOINT promote_observation`:
  insert a `semantic_memories` row **and** `UPDATE observations SET
  status='consumed_without_reflection'` in one atomic step.

The value the tab delivers in sqlite mode is therefore three things that are all
**observation-centric and mutable/relational**: (a) raw observation browsing project-wide
across all sessions, (b) an audit trail of state transitions, and (c) reinforcement
inspection (`reinforcement_score`, use/misuse counters) plus provenance
(observation -> reflection).

## 2. Where it comes from, and why agentcore cannot serve it the same way

Every read above hits the local `observations` table directly (raw SQL through
`queries.py`, bypassing `StorageBackend` — see `data-sources.md`). In agentcore mode that
content moves AWS-side, and the mapping breaks on four independent live-API facts.

**a. Observations are immutable events, not records.** `observe()` maps to a
`CreateEvent` keyed by `actorId` + `sessionId`. An event carries only creation-time
metadata (`component`, `theme`, `outcome`) and **cannot hold** a mutable `status`,
`reinforcement_score`, or `episode_id`. The backend already concedes this:
`AgentCoreBackend.list_observations` (agentcore.py:887) returns `reinforcement_score: None`
as a stable placeholder because the event plane has no such field. So even where a row can
be fetched, the two columns the list panel and drawer render (`status`,
`reinforcement_score`) are structurally absent. Reinforcement inspection is simply not
representable.

**b. `ListEvents` requires `sessionId`; there is no project-wide event read.**
`list_events(memoryId, actorId, sessionId, …)` needs **both** an actor and a concrete
session. `list_observations` documents this directly: it returns only the **current
session's** events, and "cross-session enumeration is deferred" (agentcore.py:898-899).
The UI's list panel is inherently **project-wide across all sessions** — there is no single
call that returns it.

**c. No list-distinct-projects API.** The dropdown's `SELECT DISTINCT project` has no
event-plane or record-plane equivalent. `ListActors(memoryId)` enumerates
**event-derived** actors (projects with >=1 `observe()`), but there is no
list-namespaces API and parent namespaces do not roll up. The backend is bound to one
`actorId` at a time.

**d. No provenance link table.** The drawer's observation -> reflection join walks
`reflection_sources`. Records carry no `episode_id` and there is no `reflection_sources`
analogue AWS-side, so the "linked reflections" and audit-linked provenance legs have
nothing to walk. (The `audit_log` leg itself is operational state and stays local — but an
audit trail of an entity the tab no longer shows is orphaned.)

**The only reconstruction path** for project-wide raw browsing is a three-level fan-out:

```
ListActors(memoryId)                         -> every project (actor) with observations
  for each actor: ListSessions(actorId)      -> every session under that project      [N]
    for each session: ListEvents(actor, sid) -> the events in that session            [N x M]
```

This is **N+1+M** AWS round-trips (1 + N actors + N x M sessions) to reassemble what
`SELECT ... FROM observations` does in one local query, and it is **lossy**: the reassembled
rows still have `reinforcement_score = None`, no `status`, no `episode_id`, and no
provenance edges. It reconstructs the *text* of observations but none of the fields the tab
exists to show.

## 3. Options going forward

### Option A — Hide the Observations tab in agentcore mode. **[RECOMMENDED — converged design]**

Gate the tab off via a backend capability flag (add an `supports_observations`-style flag
alongside the existing `supports_episodes=False`), the same mechanism that hides Episodes.
Events are handled transparently by the write plane; there is no user-facing "observation"
concept in agentcore mode. The audit-trail, provenance, and reinforcement sections
disappear with the tab.

- **Pros:** Honest — it hides exactly the capabilities that cannot be served
  (reinforcement inspection is impossible, provenance has no backing, project-wide browse
  needs N+1+M). Zero AWS cost. Consistent with the Episodes precedent and with
  capability-flag-driven gating, so **sqlite mode is completely unchanged**. Nothing is
  half-shown or silently wrong.
- **Cons:** Users lose raw observation browsing and the audit trail entirely in agentcore
  mode. The state transitions still happen server-side (event expiry, extraction) but are
  not inspectable through this UI.
- **Net:** The lost surface is genuinely not reconstructable at fidelity. Hiding beats
  showing a lie.

### Option B — Build the full N+1+M reconstruction (ListActors -> ListSessions -> ListEvents).

Add `list_actors` / `list_sessions` backend methods and drive the dropdown, list panel,
and drawer body off the fan-out.

- **Pros:** Restores raw observation *text* browsing, project-wide, without a data model
  change.
- **Cons:** N+1+M AWS calls per page load (unbounded in N sessions), no pagination story
  that maps cleanly onto three nested cursors, and **the result is still lossy**:
  `reinforcement_score`, `status`, `episode_id`, and provenance are all `None`/absent. It
  spends real latency and cost to render a degraded panel whose two headline columns are
  blank. This is the **"not worth it / we are screwed"** option — the reconstruction is
  expensive *and* it does not actually reconstruct the thing that made the tab useful.
- **Net:** Rejected. Cost is high and the payload is still missing the mutable fields.

### Option C — Mirror observation state locally (keep a local `observations` shadow in agentcore mode).

Keep writing observation rows (with `status` / `reinforcement_score` / `episode_id`) to the
local `memory.db` even in agentcore mode, and serve the tab from that.

- **Pros:** Full-fidelity tab, one local query, no fan-out.
- **Cons:** Reintroduces exactly the dual-write / divergence problem the parity design
  exists to eliminate — the local shadow and the AWS event plane drift, cross-machine
  sessions never appear in the local mirror, and "project-wide across all sessions" is
  still wrong because only *this machine's* observations are mirrored. It also contradicts
  the settled local-vs-content split (content is AWS-side; only operational state stays
  local).
- **Net:** Rejected. It rebuilds a divergent local content store under a new name.

### Option D — Serve a current-session-only observation list (no dropdown, no cross-session).

Wire the panel to `AgentCoreBackend.list_observations` as-is (current session only), drop
the project dropdown and the audit/provenance/reinforcement sections.

- **Pros:** Cheap (one `ListEvents` call), uses a method that already exists.
- **Cons:** Silently redefines the tab — a user expecting project-wide history sees only
  the session that happens to be current, with blank `status`/`reinforcement_score`. A
  misleading partial view is worse than an absent one; it looks like data loss.
- **Net:** Rejected in favor of A. If a session-scoped event view is ever wanted, it
  belongs in Diagnostics as an explicitly session-scoped panel, not masquerading as the
  Observations tab.

---

## Summary

1. In sqlite mode the Observations tab does three things — project-wide raw browsing, an
   audit trail, and reinforcement/provenance inspection — all off the mutable local
   `observations` table.
2. In agentcore mode observations are immutable `CreateEvent`s with only creation-time
   metadata: no `status`, no `reinforcement_score` (backend returns `None`), no
   `episode_id`, and no `reflection_sources` provenance link.
3. `ListEvents` needs both `actorId` and `sessionId`, so there is no project-wide read;
   the only path is a lossy N+1+M `ListActors` -> `ListSessions` -> `ListEvents` fan-out.
4. That reconstruction recovers observation *text* but still drops every mutable field the
   tab exists to show, at high AWS cost — the "not worth it" option.
5. **Recommended (converged): hide the Observations tab** via a backend capability flag,
   mirroring the `supports_episodes=False` precedent; sqlite mode is unchanged.
6. Local-mirror (C) and current-session-only (D) are both rejected as divergent or
   misleading; a session-scoped event view, if ever wanted, belongs in Diagnostics.
