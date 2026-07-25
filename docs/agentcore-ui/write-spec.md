# AgentCore-Mode UI — WRITE SPEC

The management UI (`better_memory/ui/app.py`) opens the **local `memory.db`** as a raw
`sqlite3.Connection` at `create_app()` and routes every content write through a service
bound to that raw connection (`ReflectionService`, `SemanticMemoryService`,
`EpisodeService`) or via inline `conn.execute` in the route. It never touches
`storage/factory.build_backend`. This spec defines, for **agentcore mode**, the exact write
path each UI mutation must take instead, naming real `StorageBackend` / `AgentCoreBackend`
methods and the AWS API each maps to.

This is a **write-only** spec (reads are covered by `data-sources.md` /
`agentcore-mapping.md`). "Write" here includes the two reflection lifecycle mutations
(promote/retire), all semantic CRUD, the observation→semantic promotion, and the hook-error
delete/purge — plus the actions that must be **hidden/disabled** in agentcore mode.

---

## Core-infra prerequisite (everything below sits on this)

`create_app()` MUST build a backend via
`better_memory.storage.factory.build_backend(config=…, memory_conn=<local memory.db conn>,
sync_embedder=…, session_id=None, project=project_name())` and store it in
`app.extensions['backend']`, then route all **content** writes through backend methods.

- **Two connections coexist.** The local `sqlite3.Connection` is STILL opened and kept in
  `app.extensions['db_connection']` — it is the `local_conn` passed to `build_backend` and it
  remains the sole source for **operational-state** reads/writes
  (`hook_errors`, `session_memory_exposure`, `rating_diagnostics`, `retention_runs`,
  `audit_log`). In sqlite mode `build_backend` returns a `SqliteBackend` wrapping that same
  connection, so behavior is byte-for-byte unchanged.
- **Content writes go through the backend.** Every write route stops instantiating a service
  on the raw conn and instead calls `app.extensions['backend'].<method>(…)`. In sqlite mode
  the backend delegates to the identical service; in agentcore mode it hits AWS.
- **Capability gating drives what is even wired.** `supports_episodes` already exists. Add
  analogous capability flags on the protocol so the UI can hide/disable actions without
  sniffing `config.storage_backend`:
  - `supports_episodes` (exists; False on agentcore) → hides Episodes tab and all its writes.
  - `supports_observations` (new; False on agentcore) → hides Observations tab, the
    observation→semantic promote action, and all provenance/linked-reflection sections.
  - `supports_reflection_text_edit` (new; False on agentcore by default — see §Open decision)
    → hides the inline reflection use_cases/hints edit form.
  - `supports_reflection_confirm` (new; False on agentcore) → hides the Confirm action
    (reflections are born `active`; there is no `pending_review` gate).
  sqlite mode returns True for all of these, so nothing about sqlite mode changes.

---

## Write-by-write specification

Legend for **destination**: **AWS** = durable content, goes to AgentCore memory records;
**LOCAL** = operational state, stays in `memory.db` in both modes.

### Reflections

| # | UI write | Current sqlite path | AgentCore-mode path (backend method → AWS API) | Dest |
|---|----------|---------------------|-----------------------------------------------|------|
| R1 | **Promote reflection to general scope** (`POST /reflections/<id>/promote`) | `ReflectionService.promote_to_general` → `UPDATE reflections SET scope='general', updated_at` on raw conn | `backend.promote_reflection(reflection_id=id)` → `AgentCoreBackend.promote_reflection` (agentcore.py:1578) → `_mutate_namespace_and_status(new_namespaces=[resolve_namespace("general","reflections")], new_status="promoted")` → **UpdateMemoryRecord / BatchUpdateMemoryRecords** (moves record project→general reflections namespace, status→`promoted`). Declared on `protocol.py:241`. | AWS |
| R2 | **Retire reflection** (`POST /reflections/<id>/retire`) | `ReflectionService.retire` → `UPDATE reflections SET status='retired', updated_at` on raw conn | `backend.retire_reflection(reflection_id=id)` → `AgentCoreBackend.retire_reflection` (agentcore.py:1585) → `_mutate_namespace_and_status(new_namespaces=[resolve_namespace(actor_id,"retired")], new_status="retired")` → **UpdateMemoryRecord / BatchUpdateMemoryRecords** (moves to `projects/{project}/retired/`, status→`retired`; handles AWS-extracted and migrated records). Declared on `protocol.py:245`. | AWS |
| R3 | **Confirm reflection** (`POST /reflections/<id>/confirm`) | `ReflectionService.confirm` → `UPDATE reflections SET status='confirmed'` on raw conn | **HIDDEN/DISABLED** in agentcore mode via `supports_reflection_confirm=False`. Reason: agentcore's reflection status model is `active`/`promoted`/`retired` with **no `pending_review` gate** — reflections are born `active` (== sqlite `confirmed`). There is no confirm method on the protocol and `confirmed` would be neither produced nor read back. Route returns 404/hidden button; never wired. | — |
| R4 | **Edit reflection text** (use_cases + hints) (`POST /reflections/<id>/edit`) | `ReflectionService.update_text` → `UPDATE reflections SET use_cases, hints, updated_at` + local vec0 re-embed on raw conn | **HIDDEN/DISABLED by default** in agentcore mode via `supports_reflection_text_edit=False`. Reason: reflection text is AI-managed by the AgentCore extraction pipeline; there is **no wired reflection text-update backend method** (only `semantic_update_text` exists), AWS-extracted records' field editability is unresolved, and the vec0 re-embed has no agentcore counterpart. This is a genuine open decision (see below); DEFAULT is disable. If later enabled, it maps to a new `backend.reflection_update_text(id, use_cases, hints)` → `batch_update_memory_records` body RMW (agentcore.py:1055) → **BatchUpdateMemoryRecords**. | AWS (if enabled) |
| R5 | Rating evidence receipts re-render after any reflection write | `queries.fetch_rating_evidence` on `session_memory_exposure` | **UNCHANGED — LOCAL.** Reads `session_memory_exposure` on `app.extensions['db_connection']` in both modes (session-operational state; `protocol.py:262` list_session_exposures reads the same local table). | LOCAL |

### Semantic memories (full CRUD, all AWS in agentcore mode)

| # | UI write | Current sqlite path | AgentCore-mode path (backend method → AWS API) | Dest |
|---|----------|---------------------|-----------------------------------------------|------|
| S1 | **Create semantic memory** (`POST /semantic`) | `SemanticMemoryService.create` → `INSERT semantic_memories` + local vec0 embed | `backend.semantic_observe(content=…, project=…, scope=…)` → `AgentCoreBackend.semantic_observe` (agentcore.py:1199) → inserts a BASE record under the userPreference strategy, namespace from scope (`resolve_namespace(actor,"semantic")`), content-hash dedup → **BatchCreateMemoryRecords**. Returns record id. Embedding is AWS-managed (local vec0 write is a non-goal). Declared `protocol.py:152`. | AWS |
| S2 | **Update semantic memory text** (`POST /semantic/<id>/update`) | `SemanticMemoryService.update_text` → `UPDATE semantic_memories SET content` + local vec0 re-embed | `backend.semantic_update_text(id=id, content=…)` → `AgentCoreBackend.semantic_update_text` (agentcore.py:1383) → get + batch_update with full metadata snapshot (reserved keys stripped), transient-404 retry → **BatchUpdateMemoryRecords**. Re-embedding AWS-managed. Declared `protocol.py:173`. | AWS |
| S3 | **Set scope (project ↔ general)** (`POST /semantic/<id>/scope`) | `SemanticMemoryService.set_scope` → `UPDATE semantic_memories SET scope` | `backend.semantic_set_scope(id=id, scope=…)` → `AgentCoreBackend.semantic_set_scope` (agentcore.py:1410) → validates scope, moves the record between `projects/{project}/semantic/` and `general/semantic/` namespaces (scope is namespace membership, not a column) → **BatchUpdateMemoryRecords** (namespace move). Declared `protocol.py:177`. | AWS |
| S4 | **Delete semantic memory** (`POST /semantic/<id>/delete`) | `SemanticMemoryService.delete` → `DELETE FROM semantic_memories` (idempotent) | `backend.semantic_delete(id=id)` → `AgentCoreBackend.semantic_delete` (agentcore.py:1444) → removes the record; AWS reclaims the embedding → **BatchDeleteMemoryRecords**. Minor semantic difference: agentcore raises on not-found vs sqlite idempotency — wrap so the route stays idempotent (treat not-found as success). Declared `protocol.py:181`. | AWS |

### Observation → semantic promotion

| # | UI write | Current sqlite path | AgentCore-mode path | Dest |
|---|----------|---------------------|---------------------|------|
| O1 | **Promote observation → semantic** (`POST /observations/<id>/promote-to-semantic`) | `SemanticMemoryService.create_from_observation` → `SAVEPOINT`: SELECT observation, INSERT `semantic_memories`, UPDATE `observations SET status='consumed_without_reflection'`, vec0 embed | **HIDDEN/DISABLED** in agentcore mode. Reason: the whole **Observations tab is hidden** (`supports_observations=False`) — observations are immutable `CreateEvents` with no mutable status, so the "mark consumed" half has no equivalent, and the `SAVEPOINT` crosses the event and record planes with no distributed transaction. The create half alone would be `semantic_observe`, but the action is not offered because its consume half cannot be honored. Never wired. | — |

### Diagnostics — hook errors (operational; stays LOCAL in both modes)

| # | UI write | Current sqlite path | AgentCore-mode path | Dest |
|---|----------|---------------------|---------------------|------|
| D1 | **Delete one hook error** (`POST /diagnostics/hook-errors/<id>/delete`) | inline `conn.execute("DELETE FROM hook_errors WHERE id=?")` + `conn.commit()` in route | **UNCHANGED — LOCAL.** Same inline `DELETE` on `app.extensions['db_connection']`. `hook_errors` is local-only operational state with no backend method; correctly bypasses `StorageBackend` in both modes. | LOCAL |
| D2 | **Purge all hook errors** (`POST /diagnostics/hook-errors/purge`) | inline `conn.execute("DELETE FROM hook_errors")` + `conn.commit()` in route | **UNCHANGED — LOCAL.** Same unfiltered inline `DELETE` on the local conn. Local operational state, no backend involvement is correct. | LOCAL |

### UI infra — project dropdown population (read that feeds every write's project scope)

| # | UI write/read | Current sqlite path | AgentCore-mode path | Dest |
|---|----------|---------------------|---------------------|------|
| P1 | **Distinct-project dropdown** (`queries.reflection_distinct_projects` / `observation_distinct_projects`, `SELECT DISTINCT project`) | raw `SELECT DISTINCT project` on the content table | Replace the raw SELECT DISTINCT with a new `backend.list_projects()`: in agentcore mode = **ListActors(memoryId)** (enumerates event-derived actors — projects with ≥1 `observe()`) UNION the local `agentcore_migration` ledger's project set (parse the actorId out of the ledger `namespace` column, `projects/{actor}/…`, via `resolve_namespace` inverse). Self-populates on first `observe()`. A new `AgentCoreBackend.list_projects()` wrapping the ListActors control-plane call must be added (none exists today — grep confirms no `ListActors`/`list_actors`). In sqlite mode `list_projects()` = the existing `SELECT DISTINCT project`, unchanged. | AWS + LOCAL |

---

## Actions HIDDEN / DISABLED in agentcore mode (summary + reason)

| Action | Gate flag | Reason |
|--------|-----------|--------|
| Confirm reflection (R3) | `supports_reflection_confirm=False` | No `pending_review` gate; reflections born `active`. `confirmed` has no server-side meaning. |
| Inline reflection text-edit (R4) | `supports_reflection_text_edit=False` (DEFAULT; open decision) | Reflection text is AI/extraction-pipeline managed; no wired text-update method; AWS-extracted editability unresolved; vec0 re-embed has no counterpart. |
| Promote observation→semantic (O1) | `supports_observations=False` | Observations are immutable events with no status lifecycle — cannot mark consumed; SAVEPOINT crosses event+record planes. |
| Whole Observations tab | `supports_observations=False` | No observation concept in agentcore mode (events handled transparently). Also removes all provenance/linked-reflection sections. |
| Whole Episodes tab + Close episode | `supports_episodes=False` (exists) | AgentCore groups events by `sessionId`; no episode entity; `close_*` are no-ops. |
| Retention-runs write/prune | n/a (never UI-triggered) | Pruning = event expiry (`eventExpiryDuration`, live via UpdateMemory); no UI content-mutation engine. |

## Open decision (do not resolve here)

**Inline reflection text-edit (R4)** — DEFAULT is disable (`supports_reflection_text_edit=False`)
because reflection bodies are AI-managed. Whether to expose an editor for the migrated-reflection
JSON body (whose text lives in the content body, unlike AWS-extracted reflections managed by the
extraction pipeline) is a genuine open question flagged for the user, not settled by this spec.
