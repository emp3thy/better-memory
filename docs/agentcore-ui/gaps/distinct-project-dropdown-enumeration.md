# GAP / DECISION — Distinct-project dropdown enumeration in agentcore mode

**Status:** design converged 2026-07-25 (verified against the live AgentCore
instance, eu-west-2, acct 708306701628). This document records the decision and
its residual caveats for a maintainer deciding what to build. Do not re-litigate
the chosen source — build on it.

**Scope:** the project-selector dropdown that appears on the Observations tab and
the Reflections tab. Both are populated today by a raw `SELECT DISTINCT project`
against local sqlite and have no direct AgentCore equivalent.

---

## 1. What the data is / what the UI does today (sqlite mode)

The management UI renders a **project filter dropdown** at the top of two tabs so
the operator can scope the list panels to one project. It is a flat list of the
distinct project names that have any content of the relevant kind.

- **Observations tab** — dropdown built from every distinct `project` value in the
  `observations` table.
- **Reflections tab** — dropdown built from every distinct `project` value in the
  `reflections` table, then **unioned** with `project_name()` (the current cwd's
  project) so the active project is always selectable even before it has any rows.

The list is purely for UI navigation: pick a project, the panel re-queries scoped
to it. In sqlite mode this is cheap and exact because all content lives in one
local database and `SELECT DISTINCT` sees every project ever written.

---

## 2. Where it comes from, and why agentcore cannot serve it the same way

### Exact source today

| Tab | Route / call site | Query function | SQL |
|---|---|---|---|
| Observations | `app.py:612` `observation_promote_to_semantic` render path / `GET /observations` | `queries.observation_distinct_projects` (`queries.py:546`) | `SELECT DISTINCT project FROM observations` |
| Reflections | `app.py:294` `reflections()` | `queries.reflection_distinct_projects` (`queries.py:561`) | `SELECT DISTINCT project FROM reflections`, then `set`-unioned with `project_name()` |

Both run raw parameterized SQL on the shared `sqlite3.Connection` in
`app.extensions['db_connection']`. Neither goes through `StorageBackend`. Both
read **content tables** (`observations`, `reflections`), which in agentcore mode
live AWS-side — the local read would return nothing.

### Why AgentCore has no equivalent (live-API facts)

The `SELECT DISTINCT project` pattern depends on being able to enumerate the set
of projects that hold content. AgentCore cannot answer that question directly:

- **Content lives in memory *records*, not events.** Reflections are records under
  `projects/{project}/reflections/`; semantic records under
  `projects/{project}/semantic/`. `actorId == project`. These records have **no
  TTL** (durable).
- **There is no list-namespaces / list-actors-with-records API.** You cannot ask
  AgentCore "which project namespaces contain records." `ListMemoryRecords`
  requires a concrete leaf namespace (slash-tolerant but exact; parent namespaces
  do not roll up). The backend is bound to one `actorId` per call
  (`resolve_actor_id`, `resolve_namespace` in `better_memory/storage/session.py`).
- **`ListActors(memoryId)` enumerates EVENT-derived actors, not record
  namespaces.** It returns every actor with ≥1 `observe()` (`CreateEvent`) —
  i.e. projects that have emitted at least one observation event. It does **not**
  see projects that only have records (e.g. reflections/semantic migrated in bulk
  but never yet observed against).
- **`ListEvents` needs `actorId` AND `sessionId`** — it cannot enumerate across
  sessions, and `ListSessions(actorId)` still presupposes you already know the
  actor. So there is no bottom-up "walk all events to discover all projects" path
  either.

**The concrete failure mode.** A project migrated into AgentCore (its reflections
and semantic records exist under `projects/{project}/...`) but never subsequently
`observe()`d is **invisible to `ListActors`**. Its records are durable and fully
queryable *if you already know the project name* — but nothing enumerates the name
for the dropdown. Such a project is "migrated-dormant": present in content, absent
from every enumeration API.

---

## 3. Options going forward

### Option A — `ListActors(memoryId)` alone

Use only the event-derived actor list.

- **Pros:** single AWS call; no local state; always reflects live event activity;
  self-populates a project the instant it emits its first `observe()`.
- **Cons:** **misses every migrated-dormant project** (records but no events) — the
  exact population that motivated this gap. Also subject to the event-expiry decay
  caveat below: a project whose events have all aged out silently drops off the
  list even though its durable records remain. Incomplete on its own.

### Option B — `ListActors(memoryId)` UNION the local `agentcore_migration` ledger *(RECOMMENDED — matches the converged design)*

Union the event-derived actors with the project set recorded in the local
`agentcore_migration` ledger.

- **Source of the ledger project set:** `agentcore_migration`
  (`better_memory/storage/agentcore_migrate.py:52`) has a `namespace` column on
  every migrated row (`projects/{project}/reflections/`, `.../semantic/`, or
  `general/...`). Parse the `{project}` segment out of each distinct `namespace`
  (map `general/...` → the general bucket) to recover the set of projects that were
  ever migrated into this instance. This is **local operational state** — it stays
  in `memory.db` in both modes, consistent with the settled local-vs-content split.
- **Semantics of the union:** `ListActors` contributes **recently/currently active**
  projects (anything that has observed, including brand-new projects the ledger has
  never heard of); the ledger contributes **everything ever migrated** (including
  migrated-dormant projects `ListActors` cannot see). The union is the closest
  achievable approximation of "all projects with content." It **self-populates on
  first `observe()`** for genuinely new projects (via the `ListActors` leg) and
  covers historical migrations (via the ledger leg).
- **Pros:** covers the migrated-dormant population that Option A drops; the ledger
  leg is decay-proof (a local durable table, unaffected by event expiry); no new
  AWS API required beyond `ListActors`; degrades gracefully (ledger-only if
  `ListActors` errors, `ListActors`-only on a fresh instance with no migration).
- **Cons / honest limits:**
  - **Not perfectly complete.** A project that was *never migrated through this
    instance's ledger* AND has *no live events* (e.g. records created directly
    server-side, or a ledger reset/rebuilt on another machine) is still invisible.
    This residual is accepted: it is strictly smaller than Option A's blind spot.
  - **365-day event-expiry decay caveat.** Records never expire, but **events do**
    — `eventExpiryDuration` is min 3 / max 365 days, live-updatable via
    `UpdateMemory` (episodic was just bumped 90→365). A project that is active in
    records but has emitted no `observe()` within the expiry window will fall out of
    the `ListActors` leg. Option B masks this **only for projects also in the
    ledger**; an active-by-records, non-migrated, event-expired project would still
    disappear. Setting `eventExpiryDuration` to the 365-day max minimizes but does
    not eliminate this window.
  - Ledger and live event set can drift (ledger reflects a point-in-time migration,
    not ongoing deletes) — the dropdown may list a project whose records were later
    deleted. A stale-but-harmless entry (selecting it yields an empty panel) is the
    accepted trade vs. missing a live project.

### Option C — Seed a synthetic observation per project (the "make it enumerable" option)

At migration time, emit one throwaway `CreateEvent` per migrated project so every
project becomes event-active and therefore visible to `ListActors` — collapsing the
problem back to Option A.

- **Pros:** the dropdown reduces to a single `ListActors` call with no local ledger
  dependency; conceptually uniform (every project is event-active).
- **Cons:** **pollutes the immutable event log** with synthetic non-observations
  that carry no real content and can never be cleaned up individually (observations
  are immutable `CreateEvents` keyed by `actorId`+`sessionId`, carrying only
  creation-time metadata — they cannot hold a "synthetic" flag or mutable status).
  Worse, **it does not survive event expiry**: the synthetic marker ages out after
  ≤365 days and the migrated-dormant project vanishes again unless re-seeded on a
  schedule — reintroducing exactly the decay problem Option B's local ledger avoids.
  It also corrupts any event-derived analytics/session counts. Rejected.

### Option D — "Not worth it": accept `ListActors`-only and document the blind spot

Ship Option A, tell operators that migrated-dormant projects won't appear in the
dropdown until first `observe()`, and provide a free-text project box as an escape
hatch.

- **Pros:** zero new local state; simplest possible implementation.
- **Cons:** silently hides real, populated projects from the operator — a
  correctness/discoverability regression versus sqlite mode, where the dropdown is
  exhaustive. The free-text escape hatch pushes the burden onto the operator to
  already know the project name. Only defensible if the migrated-dormant population
  is provably empty, which it is not. Not recommended.

---

## Recommendation

Build **Option B**: replace both raw `SELECT DISTINCT project` calls
(`queries.observation_distinct_projects`, `queries.reflection_distinct_projects`)
with, in agentcore mode, `ListActors(memoryId)` UNION the distinct project set
parsed from the `agentcore_migration` ledger's `namespace` column — gated behind a
backend capability flag so sqlite mode continues to call the existing
`SELECT DISTINCT` unchanged. Keep `eventExpiryDuration` at the 365-day maximum to
shrink the residual decay window, and document the two accepted residuals
(non-migrated + event-expired projects can still be missed; deleted-record projects
can still be listed) rather than chasing perfect completeness.
