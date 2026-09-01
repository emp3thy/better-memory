# AgentCore UI Feature Mapping

This document consolidates the per-domain analysis of every management-UI data
operation into a single two-bucket split: which UI features can be served in
**agentcore mode** today (or with only mechanical wiring), and which face a real
data-model gap or design decision.

Each UI feature was classified against the AgentCore StorageBackend
(`better_memory/storage/agentcore.py`), the storage protocol
(`better_memory/storage/protocol.py`), and the parity design spec
(`docs/superpowers/specs/2026-07-24-agentcore-parity-design.md`).

- **EASY** — a working AgentCore method already maps to the feature (needs only
  wiring), or the data is session-operational state that the settled
  local-vs-content split keeps in the local `memory.db` in both modes, or the
  feature has no data source at all (static shell / filesystem / config).
- **HARD** — no AgentCore path exists, the load-bearing content requires a
  cross-table/cross-plane join AgentCore does not model, the read needs
  cross-session/cross-actor enumeration the event/record planes do not support,
  or a status/lifecycle field has no server-side equivalent. These require a
  genuine design decision, not a wiring swap.

Items that appeared in more than one domain have been deduplicated (see
_Promote observation to semantic memory_, which surfaced under both the
Observations and Semantic tabs — kept once, HARD).

---

## EASY — servable today (wiring or local-split only)

| Feature | Domain | Op | AgentCore path / candidate source | Rationale | Refs |
|---|---|---|---|---|---|
| Reflection drawer — rating evidence receipts | Reflections | read | local `session_memory_exposure` (services/exposure_log) | Session-operational state kept in local memory.db in both modes; backend already reads/writes this exact table via the local conn. | agentcore.py:88-97; queries.py:800; design §1 |
| Reflection edit form (pre-populate use_cases/hints) | Reflections | read | `get_memory_record` + `_parse_reflection_record` | Form needs only use_cases + hints from the single row, both parsed body-first off a get_memory_record fetch. Only work is decoupling from the join-heavy `reflection_detail` query. | agentcore.py:732-885, 972-983 |
| Retire reflection | Reflections | write | `retire_reflection` | Direct existing map: sets status='retired', moves record to retired namespace, handles AWS-extracted and migrated records; declared on protocol. Only route the UI write through the backend. | agentcore.py:1585-1591; protocol.py:245-247 |
| Promote reflection to general scope | Reflections | write | `promote_reflection` | Direct existing map: sets status='promoted', moves to general/reflections namespace; promotion fully modelled and admitted by the buckets fetch. Only route the UI write through the backend. | agentcore.py:1578-1583; protocol.py:241-243 |
| Episodes tab shell (page load) | Episodes | read | `supports_episodes` capability flag (False) | Shell route issues no query; the tab is hidden by the existing capability flag. Data-less shell gated by a working backend property. | app.py:205-207; agentcore.py:106-107; protocol.py:58-64 |
| Semantic tab shell (filter form + panel mount) | Semantic | read | none (static render) | Pure static shell, zero DB access, hardcoded initial filters; all data arrives via the /semantic/panel sub-request. Backend-agnostic. | app.py:487; templates/semantic.html |
| List / filter / search semantic memories (panel) | Semantic | read | `semantic_list` (ListMemoryRecords / RetrieveMemoryRecords) | Working method fans out to list/retrieve and maps into the same SemanticMemory dataclass with every counter; Wilson ranking is Python over those counters. Exposure side-effect stays local. Only wiring: route through backend.semantic_list instead of raw SQL. | agentcore.py:1246, 1285, 1691; services/semantic.py:220 |
| Semantic memory detail drawer | Semantic | read | `_get_semantic_record` (GetMemoryRecord) + local exposure ledger | Single-record read maps to GetMemoryRecord (needs a thin public get-one accessor); evidence leg reads the local session_memory_exposure ledger. Both serviceable. | agentcore.py:1193, 1285; queries.py:800 |
| Create semantic memory | Semantic | write | `semantic_observe` (BatchCreateMemoryRecords) | Direct one-to-one: inserts content as a BASE record under userPreference strategy, namespace from scope, content-hash dedup. (Sqlite mode has no local embedding write either any more — embeddings were removed repo-wide in remove-ollama-embeddings.) | agentcore.py:1199; _agentcore_strategies.py:60 |
| Update semantic memory text | Semantic | write | `semantic_update_text` (BatchUpdateMemoryRecords) | Direct one-to-one: get + batch_update with full metadata snapshot (reserved keys stripped), transient-404 retry. Re-embedding is AWS-managed. | agentcore.py:1383, 1012 |
| Set semantic memory scope (project<->general) | Semantic | write | `semantic_set_scope` (BatchUpdateMemoryRecords namespaces move) | Direct one-to-one: validates scope, moves record between project/general semantic namespaces. Scope is namespace membership rather than a column. | agentcore.py:1410 |
| Delete semantic memory | Semantic | write | `semantic_delete` (BatchDeleteMemoryRecords) | Direct one-to-one: removes record, AWS reclaims embedding. Minor: raises on not-found vs sqlite idempotency (trivial wrapper). | agentcore.py:1444 |
| Rating diagnostics counters (session_id_missing etc.) | Diagnostics | read | local `rating_diagnostics` | Operational/telemetry state; stays local. The rating sweep that writes these becomes real on agentcore and runs against local memory.db. | design §1, §2; protocol.py:281-288 |
| Hook errors panel (grouped-by-day list) | Diagnostics | read | local `hook_errors` | Session-operational state, local in both modes; queries read local sqlite directly. Backend-independent. | design §1 (line 54) |
| Hook error detail drawer | Diagnostics | read | local `hook_errors` | Single hook_errors row from local sqlite; local-only operational state. | design §1 (line 54) |
| Retention runs panel (grouped-by-day audit list) | Diagnostics | read | local `retention_runs` | Operational audit ledger, always local; queries read local sqlite. Backend-independent. | design Non-goals (line 140) |
| Delete a single hook error | Diagnostics | write | local `hook_errors` | DELETE against local-only operational state; correctly bypasses StorageBackend. | design §1 (line 54) |
| Purge all hook errors (Clear all) | Diagnostics | write | local `hook_errors` | Unfiltered DELETE against local operational state; no backend involvement is correct. | design §1 (line 54) |
| Hook error recording (producer — INSERT) | Diagnostics | write | local `hook_errors` | record_hook_error INSERTs on its own local connection, no StorageBackend abstraction (hooks must never fail). | design §1 (lines 46-55) |
| Retention run recording (producer — INSERT) | Diagnostics | write | local `retention_runs` | _record_run INSERTs into the local ledger via raw sqlite. UI only reads it. | design Non-goals (line 140) |
| Project scoping / resolution (project_name()) | UI infra | read | `project_name()` backend-independent; maps to actorId/namespace | Pure cwd/env/filesystem resolution, identical in both modes; project value is already AgentCore's actorId namespace. (Caveat: reflections route unions raw-SQL distinct projects — see HARD dropdown items.) | config.py; _agentcore_strategies.py:99,119; storage/session.py |
| Session identity resolution (absent in UI) | UI infra | read | none (local exposure ledger) | UI is session-agnostic; browses by project. The only cross-session read (exposure ledger) stays local. Nothing for agentcore to serve. | design §1; protocol.py:262-306 |
| Left-rail tab navigation (Episodes capability gating) | UI infra | read | `supports_episodes` (protocol.py:57-64, False) | Authoritative capability data already exists; implementing the gate is threading one boolean into create_app/base.html. No AWS call. | protocol.py:57-64; agentcore.py:106; base.html |
| UI URL discovery file (ui.url) write | UI infra | write | none (filesystem) | Atomic filesystem write of the loopback URL via os.replace; no DB/backend. Identical in both modes. | ui/__main__.py |
| UI subprocess spawn + liveness detection | UI infra | write | none (process + filesystem) | Process spawn + filesystem + loopback /healthz probe; backend-agnostic launcher. | services/ui_launcher.py |
| Inactivity watchdog + explicit shutdown | UI infra | write | none (process lifecycle) | Filesystem + process lifecycle + CSRF guard only; touches no memory content or backend. | app.py (/shutdown, watchdog, /healthz) |

---

## HARD — real data-model gap or design decision

| Feature | Domain | Op | AgentCore path / candidate source | Rationale | Refs |
|---|---|---|---|---|---|
| Observations page load — Project filter dropdown | Observations | read | local sqlite (migration ledger) / undecided | SELECT DISTINCT project has no equivalent: observations are CreateEvents keyed by actorId, and ListEvents requires actorId AND sessionId. No list-distinct-actors API; cross-session enumeration is deferred. | agentcore.py:169,177,203,898; queries.py |
| Observations list panel (filtered, day-grouped) | Observations | read | agentcore `list_events` (cross-session enum needed) | list_observations exists but only returns the current session; UI needs project-wide history across all sessions. Filters post-filtered client-side; reinforcement_score/status/episode_id columns have no event-plane equivalent. | agentcore.py:887,898,949,961; queries.py |
| Observation detail drawer (record + audit + reflections) | Observations | read | undecided (audit local; body cross-session; reflection join none) | Audit leg is clean local. But single-event body fetch needs its sessionId (cross-session gap), and there is no reflection_sources linkage to walk observation->reflection. | agentcore.py:102,898; protocol.py:270; design §1 |
| Promote observation to semantic memory | Observations / Semantic | write | agentcore `semantic_observe` (create half); undecided (consume half) | Create half maps to semantic_observe. But the source observation is an immutable event with no status lifecycle — no way to mark it consumed — and the SAVEPOINT crosses event and record planes with no distributed transaction. Cross-plane/lifecycle gap. | agentcore.py:156,887,1199; services/semantic.py:148; protocol.py |
| Reflections page load — Project dropdown | Reflections | read | local sqlite (migration ledger) / undecided | SELECT DISTINCT project across all reflections; list_memory_records requires a concrete namespace and there is no list-namespaces/list-actors API. Backend is bound to one actorId. | queries.py:561; agentcore.py:392-403; design (no enum provision) |
| Reflections list panel (filtered rows) | Reflections | read | agentcore `list_memory_records` (new flat method + status decision) | Record fetch/parse exists, but status model mismatches (pending_review/confirmed don't exist server-side; only active/promoted/retired), retrieve() returns polarity-bucketed Wilson-ranked results not the flat list, and the six-filter surface is only partial. | queries.py:221; agentcore.py:317-408,838-842; design §3 |
| Reflection detail drawer (row + provenance) | Reflections | read | undecided (get_memory_record for row; provenance join none) | The row is fetchable, but the load-bearing provenance join reflection_sources->observations->episodes has no backing: no link table, observations are events, episodes unsupported. | queries.py:385; agentcore.py:106-107,1496-1503 |
| Confirm reflection (pending_review -> confirmed) | Reflections | write | agentcore `_mutate_namespace_and_status` (no 'confirmed' in model) | Mechanics trivial, but there is no confirm method on the protocol and the pending_review->confirmed gate is not part of agentcore's status model — 'confirmed' would be neither produced nor read back meaningfully. Needs a data-model decision. | services/reflection.py:1639; agentcore.py:1507-1576,378-381; protocol.py:241-247 |
| Edit reflection text (use_cases + hints) in place | Reflections | write | agentcore `batch_update_memory_records` (no wired reflection text-update method) | No reflection text-update method exists (only semantic_update_text). A new body RMW is needed; AWS-extracted records' editability of those fields is unresolved. (The sqlite-mode edit no longer re-embeds either — that vec0 write was removed repo-wide in remove-ollama-embeddings.) | services/reflection.py:1683; agentcore.py:1055-1111; design Non-goals |
| Episode timeline (list grouped by day) | Episodes | read | undecided (episode concept absent) | AgentCore groups by sessionId, has no episode record; the panel needs an episode entity plus the reflection->source-observation->episode cross-join. list_episodes returns []. Episode parity is a design non-goal. Moot (tab hidden). | queries.py:32-94; agentcore.py:1496-1503; design line 140 |
| Unclosed-episode banner | Episodes | read | undecided (no open/closed lifecycle) | COUNT looks trivial but depends on an episodes table with ended_at lifecycle AgentCore does not model (supports_episodes=False; close_* are no-ops). Moot (tab hidden). | queries.py:696-711; agentcore.py:1476-1494; design line 140 |
| Episode detail drawer (open row) | Episodes | read | undecided (episode entity + join absent) | Three raw-SQL reads including the reflection->source-observation->episode cross-join; records carry no episode_id and there is no episode record. Moot (tab hidden). | queries.py:124-194; agentcore.py:106-107; design line 140 |
| Episode close existence pre-check | Episodes | read | undecided (no episodes to confirm) | Reuses episode_detail as an existence guard; inherits the same blocker — no agentcore read can confirm an episode exists because agentcore has no episodes. Moot (tab hidden). | app.py:265-266; queries.py:124-194; agentcore.py:106-107 |
| Close episode (set outcome/close_reason) | Episodes | write | undecided (episode lifecycle absent) | UPDATE across episodes + episode_sessions modelling the lifecycle AgentCore lacks; close_episode_by_id is a no-op returning ''. Closing is meaningless without an episode concept. Moot (tab hidden). | app.py:255-288; services/episode.py:272; agentcore.py:1486-1494 |
| Recent ratings table (last 20 rated exposures) | Diagnostics | read | agentcore `GetMemoryRecord` (title resolution) + local `session_memory_exposure` | Left side resolves locally, but the LEFT JOIN to reflections/semantic_memories for the display title is an un-wired cross-store join needing N per-id AWS fetches, with no guarantee archived records still resolve. | design §1,§2; agentcore.py:864,980,1194; protocol.py:262-306 |
| Overlooked total (SUM times_overlooked) | Diagnostics | read | agentcore `ListMemoryRecords` (client-side sum) | The counter is read per record, but there is no aggregate/SUM API; the total needs an unbounded paginated enumeration across four namespaces summed client-side — a real cost/design decision. | design lines 16-21; agentcore.py:856,875,392-408,1272-1276 |
| Retention archive/prune of observations | Diagnostics | write | undecided (content-mutation engine) | Rules A/B/C read reflection_sources->reflections->episodes and UPDATE observations SET status='archived', then DELETE observations (the `observation_embeddings` table this also used to prune was dropped in migration 0018) — all content tables AWS-side, joined across a graph AgentCore does not model. Retention parity is a non-goal. | design line 140; protocol.py:58-64 |
| UI sqlite connection acquisition (shared conn) | UI infra | read | agentcore `RetrieveMemoryRecords`/`ListMemoryRecords` (new read layer) | app.py unconditionally opens local memory.db and never touches the factory; the entire read layer is raw SQL through queries.py which no backend method serves. Needs a backend threaded into create_app AND a from-scratch read layer. | app.py:83-92; factory.py:32-111; db/connection.py |
| Write-path service instantiation (Episode/Reflection/Semantic services) | UI infra | write | agentcore StorageBackend methods (rewire every write route) | UI hands services a raw sqlite3.Connection, so even writes bypass the backend and land locally. Semantic CRUD + reflection promote/retire exist but nothing is wired; EpisodeService writes have no analogue. Named core infrastructure gap. | app.py:93-96; protocol.py:152-247; agentcore.py:9-15,106 |

---

## How the line was drawn

The classification hinges on the **settled local-vs-content split** in the parity
design (§1): session-operational state (hook errors, retention runs, the exposure
ledger, rating diagnostics, migration ledger, audit log) always lives in the local
`memory.db` in both modes, while durable content (observations, reflections,
semantic memories, episodes) moves to AWS-side event and record planes in agentcore
mode.

A feature is **EASY** when at least one of these holds:

1. **Local by the settled split** — the data is operational state that never leaves
   local sqlite (most of the Diagnostics tab, exposure/rating evidence, UI
   lifecycle plumbing). Served as-is, backend-independent.
2. **A working backend method already maps** — the entire semantic-memory CRUD
   surface, plus reflection promote/retire, have one-to-one AgentCoreBackend
   methods; only UI wiring (route the call through the backend instead of raw SQL)
   is missing.
3. **No data source at all** — static shells, config reads, filesystem writes, and
   capability-flag gating (`supports_episodes`).

A feature is **HARD** when it hits one of these structural gaps:

- **Cross-session / cross-actor enumeration** the event and record planes do not
  support — every "distinct projects" dropdown and every project-wide list panel,
  because ListEvents needs a sessionId and there is no list-namespaces/list-actors
  API.
- **A cross-table / cross-plane join AgentCore does not model** — the
  reflection_sources -> observations -> episodes provenance graph behind every
  detail drawer, the ratings-table title join, and the observation->semantic
  promotion that spans the event and record planes atomically.
- **A status / lifecycle field with no server-side equivalent** — the
  pending_review->confirmed reflection gate, marking an immutable event
  "consumed", the whole episode open/closed lifecycle, and retention's
  status='archived' mutation.
- **A named infrastructure gap** — the UI's read and write layers both bypass the
  StorageBackend entirely (raw sqlite conn + queries.py), so serving them in
  agentcore mode is a from-scratch read layer plus rewiring every write route, not
  a swap.

Note that most Episode-tab items are HARD *and* moot: `supports_episodes=False`
hides the tab, so those gaps never surface at runtime — but they remain genuine
data-model gaps rather than served features.
