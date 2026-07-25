# AgentCore-Mode UI — READ SPEC

The authoritative per-read contract for the better-memory management UI when
`config.storage_backend == 'agentcore'`. For **every** read the UI performs it
gives three things:

1. **sqlite source** — how the read works today (raw SQL through `queries.py` or
   inline in `app.py`, on the shared local `sqlite3.Connection`).
2. **agentcore source** — one of: an exact `StorageBackend`/`AgentCoreBackend`
   method + the AWS API it calls + the namespace, or **stays local memory.db**
   (operational state), or **HIDDEN in agentcore mode** (tab/section gated off).
3. **shape / field differences** — columns or joins that vanish, statuses that
   remap, counters sourced differently.

## Governing principles (settled 2026-07-25; do not re-litigate)

- **Content vs operational split.** Durable *content* (reflections, semantic
  records) lives AWS-side under `AgentCoreBackend`; *operational state*
  (`session_memory_exposure`, `rating_diagnostics`, `hook_errors`, the
  `agentcore_migration` ledger, `audit_log`) always lives in the local
  `memory.db` in both modes. Operational reads keep their raw-SQL local path
  unchanged; content reads must route through backend methods.
- **Core-infra prerequisite.** `create_app()` must build a `StorageBackend` via
  `better_memory.storage.factory.build_backend` and stash it in
  `app.extensions['backend']`. Content reads call backend methods; operational
  reads keep using `app.extensions['db_connection']` (the local
  `memory.db`). In sqlite mode the backend *is* `SqliteBackend`, so routing all
  content reads through it leaves sqlite behaviour byte-for-byte identical.
- **Capability gating.** New backend capability flags (siblings of the existing
  `supports_episodes`) drive what the UI renders:
  `supports_observations=False`, `supports_provenance=False`,
  `supports_retention_runs=False`, `supports_reflection_text_edit=False`
  (default) on `AgentCoreBackend`; all `True` on `SqliteBackend`. Every
  "HIDDEN" row below is a template `{% if caps.* %}` guard, not a code deletion.
- **Namespaces** are built by `storage/session.py:resolve_namespace(actor_id,
  kind)` where `actor_id = resolve_actor_id(project)` (project name, or
  `"general"`). Leaf namespaces are exact and slash-tolerant:
  - reflections → `projects/{actor}/reflections/` and `general/reflections/`
  - retired → `projects/{actor}/retired/`
  - semantic → `projects/{actor}/semantic/` and `general/semantic/`
- **Reflection status model** in agentcore: `active` (== sqlite `confirmed`) /
  `promoted` (scope project→general) / `retired`. There is **no
  `pending_review` / `confirmed` gate** — records are born `active`. Any UI
  filter or default that references `pending_review`/`confirmed` remaps to
  `active`.

---

## Reflections tab

### R1 — List panel (`GET /reflections/panel`)

- **sqlite source:** `queries.reflection_list_for_ui` — raw `SELECT` over
  `reflections` with six filters (project / tech / phase / polarity / status /
  min_confidence) + `useful_only`; default `status IN ('pending_review',
  'confirmed')`; ordered confidence DESC, updated_at DESC, rowid DESC.
- **agentcore source:** a **new flat backend method**
  `AgentCoreBackend.list_reflections(*, project, status_filter, tech, phase,
  polarity, min_confidence, useful_only, limit)`, built on
  `self._data.list_memory_records(memoryId=episodic.memory_id, namespace=…,
  maxResults≤100, nextToken=…)` fanned out over **two** namespaces
  (`projects/{actor}/reflections/` + `general/reflections/`), plus
  `projects/{actor}/retired/` **only when** the status filter selects retired.
  Each summary is parsed by the existing `_parse_reflection_record`. Dedup by
  record id across namespaces (project wins). Ranking reuses the shared Wilson
  order (`services/scoring.wilson_lower_bound`), matching `_fetch_reflection_buckets`.
- **shape / field differences:**
  - **Status remap:** the default `('pending_review','confirmed')` filter
    becomes `active` (+ `promoted` in the general namespace). `pending_review`
    is not selectable — the Confirm workflow is gone.
  - **Filters are client-side.** `metadataFilters` cannot express polarity
    (not an indexed key) and a server `status` filter would hide AWS-extracted
    records that carry no status metadata; so tech / phase / polarity / status /
    min_confidence / useful_only are all applied in Python over parsed records.
  - **No `rowid` tiebreak** — records have UUID ids, not rowids; tiebreak is
    Wilson confidence DESC then recency DESC.

### R2 — Detail drawer, record row (`GET /reflections/<id>/drawer`)

- **sqlite source:** `queries.reflection_detail` — SELECT #1 the full
  `reflections` row (incl. `useful_count` / `times_misled` / `times_overlooked`
  / `last_*_at`); SELECT #2 the provenance join `reflection_sources →
  observations → episodes`.
- **agentcore source:** the row leg only, via a **new thin accessor**
  `AgentCoreBackend.get_reflection(*, reflection_id)` wrapping
  `self._data.get_memory_record(memoryId=episodic.memory_id,
  memoryRecordId=id)['memoryRecord']` (falling back to `list_memory_records`
  with `metadataFilters` for BASE records, as `_get_record` already does),
  parsed by `_parse_reflection_record`. No namespace arg — `get_memory_record`
  keys on memoryId + recordId.
- **shape / field differences:**
  - **Provenance section HIDDEN** (`supports_provenance=False`). There is no
    `reflection_sources` link table, observations are immutable events, and
    episodes are unsupported — the "evidence observations / owning episode"
    block is gated off entirely, not rendered empty.
  - Counters (`useful_count`, `times_misled`, `times_overlooked`,
    `times_ignored`) come body-first (migrated records) / metadata
    `numberValue` fallback (AWS-extracted), per `_parse_reflection_record`.
    `last_*_at` collapse to the single `last_credited_at` stringValue.

### R3 — Rating evidence receipts (drawer, re-rendered after every write)

- **sqlite source:** `queries.fetch_rating_evidence` — `SELECT classification,
  evidence, rated_at FROM session_memory_exposure WHERE
  memory_kind='reflection' AND memory_id=? AND evidence IS NOT NULL` newest
  first, LIMIT 10.
- **agentcore source:** **stays local memory.db.** `session_memory_exposure`
  is session-operational state written by the local ledger
  (`services/exposure_log`) in both modes. Unchanged raw SQL on the local conn.
- **shape / field differences:** none.

### R4 — Edit form pre-populate (`GET /reflections/<id>/edit`)

- **sqlite source:** reuses `queries.reflection_detail` to load current
  `use_cases` / `hints` into the edit form.
- **agentcore source:** **HIDDEN in agentcore mode by default**
  (`supports_reflection_text_edit=False`). Reflection text is AI-managed. When
  the flag is enabled (open decision — see Gaps), it reads only `use_cases` /
  `hints` from the R2 accessor (`get_reflection` + `_parse_reflection_record`),
  decoupled from the join-heavy detail query.
- **shape / field differences:** the provenance columns the sqlite form ignores
  are simply never fetched.

### R5 — Project dropdown population (`GET /reflections`)

- **sqlite source:** `queries.reflection_distinct_projects` — `SELECT DISTINCT
  project FROM reflections`, then unioned with `project_name()`.
- **agentcore source:** a **new backend method**
  `AgentCoreBackend.list_projects()` returning the UNION of:
  (a) `self._data.list_actors(memoryId=episodic.memory_id)` → `actorSummaries[].actorId`
  (event-derived actors — every project that has ≥1 `observe()`); and
  (b) the project set parsed from the local `agentcore_migration` ledger's
  `namespace` column (regex `projects/([^/]+)/`, `general` for the general
  root). The route still unions `project_name()` so the current project is
  always selectable and self-populates on first `observe()`.
- **shape / field differences:** there is **no list-namespaces / list-distinct
  API**; parent namespaces do not roll up. The dropdown is therefore a UNION of
  the event-actor plane and the local migration ledger, not a single
  `SELECT DISTINCT`. Projects that have only AWS-extracted reflection records
  and never an `observe()` nor a migration row will not appear until one exists.

---

## Semantic tab

### S1 — Tab shell (`GET /semantic`)

- **sqlite source:** static render, no DB access.
- **agentcore source:** unchanged — pure shell, backend-agnostic.
- **shape / field differences:** none.

### S2 — List / filter / search panel (`GET /semantic/panel`)

- **sqlite source:** `SemanticMemoryService.list_for_project` — raw SQL over
  `semantic_memories`, Wilson-ranked in Python; **read has a local write
  side-effect** (inserts `source='retrieve'` exposure rows; bumps
  `rating_diagnostics.session_id_missing`).
- **agentcore source:** `backend.semantic_list(project=…, scope_filter=…,
  search=…, track_exposure=…)`. Search present → `retrieve_memory_records`
  (`searchCriteria.searchQuery`, `topK=50`); absent →
  `list_memory_records(maxResults=100)`. Each summary mapped by
  `_semantic_summary_to_model` into the same `SemanticMemory` dataclass with
  all counters. Wilson ranking stays Python over those counters. The exposure
  side-effect **stays local** (`services/exposure_log` on the local conn).
- **shape / field differences:**
  - **Scope-filter default gap (must fix):** the UI's default `scope_filter=None`
    means "project rows + ALL general rows", but `semantic_list` today resolves
    a single namespace and maps `None`→project only, dropping general. The
    agentcore path must fan out to **both** `projects/{actor}/semantic/` and
    `general/semantic/` when `scope_filter is None`, dedup by id, and merge
    before Wilson ranking. `scope_filter='project'` / `'general'` stay
    single-namespace.
  - Counters (`useful_count`, `times_misled`, `times_overlooked` ←
    `overlooked_count`, `times_ignored` ← `ignored_count`) come from declared
    `numberValue` metadata; absent → zeroed, never None. Scope is derived from
    the (slash-normalized) namespace membership, not a column.
  - `search` server-side semantic (cosine) ranking replaces the sqlite `LIKE`
    substring filter — results are relevance-ordered, not lexical.

### S3 — Detail drawer, record row (`GET /semantic/<id>/drawer`)

- **sqlite source:** inline raw `SELECT` of the `semantic_memories` row
  (content, scope, timestamps, counters); `abort(404)` if absent. Evidence leg
  via `queries.fetch_rating_evidence`.
- **agentcore source:** a **new public accessor**
  `AgentCoreBackend.get_semantic(*, id)` wrapping the existing private
  `_get_semantic_record` (=`get_memory_record(memoryId=semantic.memory_id,
  memoryRecordId=id)`), mapped by `_semantic_summary_to_model`. Evidence leg
  **stays local memory.db** (`fetch_rating_evidence`, `memory_kind='semantic'`).
- **shape / field differences:** counters/scope sourced as in S2. 404 maps from
  a boto `ResourceNotFoundException` rather than an empty row.

---

## Diagnostics tab

### D1 — Recent ratings table, last 20 rated exposures (`GET /diagnostics`)

- **sqlite source:** inline route SQL — `session_memory_exposure e LEFT JOIN
  reflections r … LEFT JOIN semantic_memories s … WHERE rated_at IS NOT NULL
  ORDER BY rated_at DESC LIMIT 20`, resolving a display title.
- **agentcore source:** the exposure rows **stay local memory.db** (unchanged).
  The **title-resolution join is the hard leg**: `reflections` /
  `semantic_memories` are empty locally, so titles resolve per-id via
  `backend.get_reflection(id)` (episodic) / `backend.get_semantic(id)`
  (semantic) — up to 20 point `get_memory_record` fetches.
- **shape / field differences:** a title may be unresolvable (record retired
  from a namespace, deleted, or expired) → render the memory id / a
  `(unavailable)` placeholder instead of a title. Consider capping/omitting the
  N fetches under cost pressure (open decision — see Gaps).

### D2 — Rating diagnostics counters (`GET /diagnostics`)

- **sqlite source:** inline `SELECT metric, value FROM rating_diagnostics`.
- **agentcore source:** **stays local memory.db.** `rating_diagnostics` is
  telemetry; the real rating sweep on agentcore writes it to the local db.
- **shape / field differences:** none.

### D3 — Overlooked total, `SUM(times_overlooked)` (`GET /diagnostics`)

- **sqlite source:** inline `SELECT (SUM(times_overlooked) FROM reflections) +
  (SUM(times_overlooked) FROM semantic_memories)`.
- **agentcore source:** **no aggregate/SUM API exists.** A faithful total needs
  a client-side enumeration — `list_memory_records` paginated across the four
  namespaces (`projects/{actor}/reflections/`, `general/reflections/`,
  `projects/{actor}/semantic/`, `general/semantic/`) summing the
  `overlooked_count` counter per record — via a **new backend method**
  `sum_overlooked(project)`. This is an unbounded paginated scan.
- **shape / field differences:** cost/design decision (see Gaps). Interim
  option: display "—" / omit the tile in agentcore mode, or read a locally
  mirrored aggregate. Do NOT fall back to the local `reflections` /
  `semantic_memories` tables — empty in agentcore, would render a false 0.

### D4 — Hook errors panel (`GET /diagnostics/panel/hook-errors`)

- **sqlite source:** `queries.hook_errors_list_for_ui` (local `hook_errors`).
- **agentcore source:** **stays local memory.db.** Hook errors are recorded by
  `record_hook_error` on its own local connection in both modes.
- **shape / field differences:** none.

### D5 — Hook error detail drawer (`GET /diagnostics/hook-errors/<id>/drawer`)

- **sqlite source:** `queries.hook_error_detail` (local `hook_errors`).
- **agentcore source:** **stays local memory.db.**
- **shape / field differences:** none.

### D6 — Retention runs panel (`GET /diagnostics/panel/retention-runs`)

- **sqlite source:** `queries.retention_runs_list_for_ui` (local
  `retention_runs`).
- **agentcore source:** **HIDDEN in agentcore mode**
  (`supports_retention_runs=False`). In agentcore, pruning is AWS-side **event
  expiry** (`eventExpiryDuration`, min 3 / max 365 days, live-updatable via
  `UpdateMemory`); records themselves are durable (no TTL). There is no
  local archive/prune run to display.
- **shape / field differences:** whole panel gated off; the retention producer
  path (`RetentionScheduler`) does not run in agentcore mode.

---

## Cross-cutting: project resolution & connection acquisition

### X1 — Project scoping / resolution (`project_name()`)

- **sqlite source:** `better_memory.config.project_name()` — cwd/env/filesystem
  (`BETTER_MEMORY_PROJECT` env > `.better-memory` override > git-root walk >
  `'general'`). No table.
- **agentcore source:** **unchanged and backend-independent.** The resolved
  project value *is* AgentCore's `actorId` (via `resolve_actor_id`) and the
  namespace root.
- **shape / field differences:** none.

### X2 — UI connection / backend acquisition (`create_app()`)

- **sqlite source:** opens the local `memory.db` as a `sqlite3.Connection` and
  stashes it in `app.extensions['db_connection']`; never touches
  `storage/factory`.
- **agentcore source:** additionally build a `StorageBackend` via
  `factory.build_backend` into `app.extensions['backend']` and read its
  capability flags into template context. Content reads (R1, R2, R4-if-enabled,
  R5, S2, S3, D1-title-leg, D3) route through the backend; operational reads
  (R3, D2, D4, D5, and D1's exposure rows) keep the local conn. The local
  `memory.db` is still opened in agentcore mode — it holds the operational
  tables and the migration ledger.
- **shape / field differences:** N/A (infra).

### X3 — Left-rail tab navigation (capability gating)

- **sqlite source:** static Jinja, 5 hardcoded tabs; no gating.
- **agentcore source:** template reads capability flags from
  `app.extensions['backend']`. In agentcore mode the **Observations** tab
  (`supports_observations=False`) and **Episodes** tab
  (`supports_episodes=False`) are hidden; visible tabs are Reflections,
  Semantic, Diagnostics.
- **shape / field differences:** N/A (navigation).

---

## HIDDEN in agentcore mode — reads NOT performed

These sqlite reads are gated off by capability flags and issue **no** agentcore
call:

| Read | sqlite source | Why hidden |
|---|---|---|
| Observations dropdown / list panel / detail drawer | `queries.observation_*` | No observation concept in UI; events handled transparently. `supports_observations=False`. |
| Reflection detail **provenance** join | `reflection_sources → observations → episodes` in `reflection_detail` | No link table; observations are events; episodes unsupported. `supports_provenance=False`. |
| Reflection **edit form** (default) | `reflection_detail` reuse | Text is AI-managed; `supports_reflection_text_edit=False` (open decision). |
| Episodes timeline / banner / detail / close pre-check | `queries.episode_*` | `supports_episodes=False`; AgentCore groups by `sessionId`, no episode entity. |
| Retention runs panel | `queries.retention_runs_list_for_ui` | Pruning = AWS event expiry; `supports_retention_runs=False`. |

---

## Open decisions (flagged, not settled here)

1. **Reflection inline text-edit** (`supports_reflection_text_edit`): default
   OFF (AI-managed). If enabled, R4's read + the write need a new reflection
   body-RMW method and a decision on AWS-extracted-record editability.
2. **D1 title resolution cost:** up to 20 point `get_memory_record` calls per
   diagnostics load — acceptable, cap, or drop the title column.
3. **D3 overlooked total:** unbounded 4-namespace scan vs. a local mirrored
   aggregate vs. hiding the tile.
