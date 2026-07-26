# Core-infra: thread StorageBackend into the UI

**Date:** 2026-07-25
**Status:** approved design (converged with the user 2026-07-25; live-API facts
verified against the AgentCore instance eu-west-2 acct 708306701628), pending
implementation
**Predecessors:** #87 (agentcore learning-loop parity), the parity design
`docs/superpowers/specs/2026-07-24-agentcore-parity-design.md`, and the two
reference inventories `docs/agentcore-ui/data-sources.md` +
`docs/agentcore-ui/agentcore-mapping.md`.

## Goal

The management UI (`better_memory/ui/app.py`) is **backend-unaware**: `create_app`
opens the local `memory.db` directly and every content read/write bypasses the
`StorageBackend` abstraction (all 47 data ops in `data-sources.md`,
`goes_through_backend=false` for every one). This task makes `create_app` build a
`StorageBackend` via `factory.build_backend`, and routes the CONTENT operations
that already map 1:1 to an existing protocol method through the backend, while
**operational-state** reads/writes stay on the local `memory.db` connection. It is
the **foundation the other two agentcore-UI tasks sit on** (Reflections agentcore
mode; Semantic CRUD + Diagnostics + capability gating) — they cannot route their
harder operations through the backend until the backend is threaded and the
content-vs-operational split is drawn here.

Sqlite mode stays **byte-identical**: `SqliteBackend` delegates to the exact same
services/queries the UI calls today (`storage/sqlite.py`), so routing a content op
through it changes nothing observable in sqlite mode.

## Grounding (verified against source at HEAD 76442eb; do not re-derive)

- `create_app` opens `connect(resolve_home()/'memory.db')` unconditionally and
  stores it at `app.extensions['db_connection']`; it never imports `factory`,
  `build_backend`, or any `StorageBackend` (`app.py:83-98`;
  `data-sources.md` UI-infra row).
- `build_backend(*, config, memory_conn, embedder, sync_embedder, session_id,
  project)` returns `SqliteBackend` or `AgentCoreBackend`; the agentcore branch
  threads `local_conn=memory_conn` (session-operational state stays local in both
  modes) and requires boto3 + `agentcore.json` (`storage/factory.py:32-111`).
- `SqliteBackend` is a behaviour-preserving wrapper: `semantic_list` →
  `SemanticMemoryService.list_for_project` (same default `track_exposure=True`,
  confirmed `services/semantic.py:220-226`), `semantic_observe/update_text/
  set_scope/delete` → the same service methods, `promote_reflection` →
  `ReflectionService.promote_to_general`, `retire_reflection` →
  `ReflectionService.retire` (`storage/sqlite.py:169-204,345-349`). Every one is
  what the UI already invokes.
- The UI builds a shared `resolved_sync_embedder` and threads it into every
  write-path service; `build_backend` forwards `sync_embedder` to `SqliteBackend`
  which reuses it (`app.py:86-97`; `sqlite.py:75`). No embedder is constructed
  twice.
- Capability flags already exist on the protocol as `supports_synthesis` /
  `supports_episodes`; `AgentCoreBackend.supports_episodes=False`. **No UI code
  references any capability flag today** — `supports_episodes` is never read in
  `better_memory/ui` (grep-confirmed in `data-sources.md`), so even the intended
  Episodes-tab hide is unimplemented.
- Operational-state tables (`session_memory_exposure`, `rating_diagnostics`,
  `hook_errors`, `retention_runs`, `audit_log`) are local in both modes; the
  hook-error delete/purge routes correctly hit the raw conn already
  (`app.py:750-764`).
- **No backend method exists** for: reflection `confirm`, reflection
  `update_text`, `create_from_observation` (promote-observation-to-semantic),
  single-semantic get (drawer row), the flat 6-filter reflection list, any
  observation/episode read, or the Diagnostics content joins. These are the HARD
  items in `agentcore-mapping.md` and are **out of scope for this task**.

## Design

### 1. Build the backend in `create_app`; keep the raw conn for operational state

`create_app` gains a backend alongside the existing connection. Both live in
`app.extensions`:

- `app.extensions['db_connection']` — **unchanged**. Remains the shared local
  `sqlite3.Connection`. From now on its role is narrowed to **operational-state
  only**: `session_memory_exposure` (rating evidence receipts), `rating_diagnostics`,
  `hook_errors`, `retention_runs`, `audit_log`. Every route reading/writing those
  keeps using it verbatim.
- `app.extensions['backend']` — **new**. Built via:

  ```python
  backend = build_backend(
      config=get_config(),
      memory_conn=db_conn,
      sync_embedder=resolved_sync_embedder,
      session_id=None,          # the UI is session-agnostic
      project=project_name(),   # default only; routes still pass explicit project
  )
  ```

  `session_id=None` because the UI browses by project, never by session (verified:
  `get_session_id` is not called anywhere in `better_memory/ui`). `project` is the
  backend's *default*; every content route continues to pass an explicit
  `project=` (per-request `project_name()` or the filter value), so the default is
  never load-bearing — this is what keeps multi-project browsing intact.

  In **sqlite mode** `memory_conn` is the same conn the UI already opened, so
  `SqliteBackend` shares it — no second connection, no divergence. In **agentcore
  mode** the factory threads `local_conn=memory_conn`, so content routes to AWS
  while operational-state routes stay on the local conn — exactly the settled
  split from the parity design (§1).

The existing `EpisodeService` / `ReflectionService` app-extension instances stay
for now (they back the operations with no protocol method — see §3).

### 2. Route the 1:1-mapped CONTENT operations through the backend

These operations have an existing protocol method that `SqliteBackend` delegates
to the identical service call. Route each through `app.extensions['backend']`:

| UI operation | Route | Backend method |
|---|---|---|
| Semantic list/filter/search panel | `GET /semantic/panel` | `semantic_list(project, scope_filter, search)` |
| Create semantic memory | `POST /semantic` | `semantic_observe(content, project, scope)` |
| Update semantic text | `POST /semantic/<id>/update` | `semantic_update_text(id, content)` |
| Set semantic scope | `POST /semantic/<id>/scope` | `semantic_set_scope(id, scope)` |
| Delete semantic memory | `POST /semantic/<id>/delete` | `semantic_delete(id)` |
| Promote reflection to general | `POST /reflections/<id>/promote` | `promote_reflection(reflection_id)` |
| Retire reflection | `POST /reflections/<id>/retire` | `retire_reflection(reflection_id)` |

The per-request `SemanticMemoryService(conn, sync_embedder=...)` construction in
the four `/semantic*` write routes and the panel route is **removed** — the
backend already owns one built with the same conn + sync_embedder, so the observed
behaviour (validation, `ValueError`→400 card, `HX-Trigger`, embedding side-effect,
exposure side-effect) is identical. `semantic_observe` returns the new id, which
the create route discards exactly as today.

Byte-identical preservation is structural, not coincidental: each backend method
is a one-line delegate to the same service method with the same defaults
(`track_exposure=True` verified). The existing route tests
(`tests/ui/test_semantic.py`, `test_reflections.py`) are the pins.

### 3. What stays on the raw path (documented seams for the two dependent tasks)

These CONTENT operations have **no backend protocol method** today, so this task
does **not** move them — they keep their current raw-SQL / raw-service path,
sqlite-only, and each is an explicit seam the dependent tasks widen:

- **Reflection reads** — the flat 6-filter list (`reflection_list_for_ui`) and the
  provenance-joined detail (`reflection_detail`). Bucketed `retrieve` ≠ the flat
  filtered list; provenance has no agentcore backing. → *Reflections dependent
  task* (new flat-list backend method; detail without provenance).
- **Reflection `confirm` and inline `update_text`** — no protocol method;
  `confirm`'s `pending_review→confirmed` gate does not exist in the agentcore
  status model (born active). → *Reflections dependent task* (hide `confirm`;
  decide edit-text disable — a genuine open decision).
- **Promote-observation-to-semantic** (`create_from_observation`) — cross-plane
  (event→record), no protocol method. → *Observations/Semantic dependent task*.
- **Semantic detail drawer row** — inline single-row `SELECT`; needs a get-one
  accessor. → *Semantic dependent task*.
- **Observation and Episode reads/writes** — Observations tab is hidden in
  agentcore mode; Episodes already intended-hidden (`supports_episodes=False`).
  → dependent tasks.
- **Diagnostics content joins** — recent-ratings title `LEFT JOIN` and
  overlooked-total `SUM` over content tables. → *Diagnostics dependent task*.

Keeping these on the raw conn is safe in **sqlite** mode (content lives locally)
and knowingly incomplete in **agentcore** mode (they would read empty/partial),
which is why they are named seams, not silent gaps — the dependent tasks own them.

### 4. Capability-flag mechanism (infra for the dependent tasks)

Add the capability flags the dependent tasks will gate on, and implement the
existing-flag gate as the reference so the mechanism is proven and tested here:

- **Protocol** (`storage/protocol.py`): add read-only properties analogous to
  `supports_episodes` — `supports_observations`, `supports_provenance`,
  `supports_retention`, `supports_reflection_mutation` (covers the
  confirm/edit-text mutation actions that agentcore lacks). Each documented with
  the sqlite=True / agentcore=False rationale.
- **SqliteBackend**: every new flag returns `True` (sqlite exposes all of it), so
  sqlite renders exactly as today.
- **AgentCoreBackend**: `supports_observations=False`, `supports_provenance=False`,
  `supports_retention=False`, `supports_reflection_mutation=False`, matching the
  agentcore UI shape (no observation concept; no provenance joins; pruning = event
  expiry; reflections born active with AI-managed text).
- **Expose to templates**: a Flask context processor injects the backend's
  capability flags into every render, so `base.html` and fragment templates can
  gate on them.
- **Reference gate**: implement the Episodes-tab nav gate in `base.html` using
  `supports_episodes` — the one flag that already exists and is already meant to
  hide a tab. This proves the context-processor + template-gate mechanism
  end-to-end. Sqlite mode is unaffected (`supports_episodes=True` ⇒ tab shows).

The dependent tasks consume the other flags to hide the Observations tab,
provenance sections, retention panel, and mutation actions. This task ships only
the mechanism + the Episodes reference gate.

## Non-goals

- Serving any HARD content read/write in agentcore mode (§3 seams) — those are the
  two dependent tasks.
- Hiding the Observations tab, provenance sections, retention panel, or
  reflection mutation actions — dependent tasks (flags are provided here).
- The distinct-project dropdown rework (`ListActors` ∪ migration ledger) —
  dependent task; the dropdowns stay on raw `SELECT DISTINCT` for now.
- Any change to operational-state routes (Diagnostics hook-errors/retention,
  exposure/rating evidence) — they are already correctly local.
- New env vars or MCP tools.

## Error handling

- `build_backend` in agentcore mode raises if boto3 or `agentcore.json` is missing
  — the same hard failure the MCP server already surfaces; correct for an
  agentcore deployment. Sqlite mode has no new failure surface (same conn as
  today).
- Content-write routes keep their existing `ValueError`→400 (semantic) / 409
  (reflection lifecycle) card contract — the backend method raises the same
  `ValueError` the service raises (it is a one-line passthrough).
- The context processor degrades safely: if a flag is absent on a backend
  (shouldn't happen once the protocol is implemented) the template treats missing
  as "shown" so sqlite can never accidentally hide a tab.

## Validation

- **Unit / route (sqlite pins):** existing `tests/ui/test_semantic.py`,
  `test_reflections.py`, `test_app.py` pass unchanged — they are the byte-identical
  proof for every routed operation. Extend with: `create_app` builds a backend and
  keeps `db_connection` (both extensions present); semantic panel/write routes and
  reflection promote/retire produce identical bodies/status/`HX-Trigger` going
  through the backend; capability context processor injects flags; Episodes nav
  gate hidden when a stub backend reports `supports_episodes=False`, shown for
  sqlite.
- **Agentcore-mode wiring (stubbed):** `create_app` with
  `storage_backend=agentcore` and a stubbed `build_backend`/backend builds without
  opening a second content store, routes semantic/reflection ops to the backend
  (assert the stub is called, not raw SQL), and keeps operational-state routes on
  the local conn. No live AWS.
- **Full suite + pyright:** `./.venv/Scripts/python.exe -m pytest tests -q`;
  `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- **Doc-sync:** `website/architecture.md` storage/UI paragraph updated to state the
  UI now reaches content through `StorageBackend`; protocol docstrings for the new
  flags. README/website config + mcp-tools tables unaffected (no tool/env change) —
  note "docs unaffected" for those explicitly.
