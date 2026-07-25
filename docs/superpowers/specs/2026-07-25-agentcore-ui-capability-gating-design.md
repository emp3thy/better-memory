# AgentCore UI: capability-gating + backend-routed content

**Date:** 2026-07-25
**Status:** approved design (converged with the user 2026-07-25, empirically verified against the live AgentCore instance eu-west-2 acct 708306701628), pending implementation
**Predecessors:** #87 (learning-loop parity), `docs/superpowers/specs/2026-07-24-agentcore-parity-design.md`
**Reference inventories:** `docs/agentcore-ui/data-sources.md` (every UI read/write + its data source), `docs/agentcore-ui/agentcore-mapping.md` (per-item EASY/HARD mapping)

## Goal

In `agentcore` mode the management UI must show only the surfaces AgentCore can
truthfully back, and every surface it does show must read/write the real backend
(AWS-side records) rather than the empty local content tables. Concretely: hide
the surfaces with no AgentCore data model, route the standing content surfaces
through `StorageBackend`, and replace the one cross-project enumeration
(`SELECT DISTINCT project`) with an AgentCore-native source. **sqlite mode stays
byte-for-byte unchanged** — every capability flag is `True` there, so no gate
fires and no query changes.

## Grounding — live-API facts (verified 2026-07-25; do not re-derive)

- Reflections are memory RECORDS under `projects/{project}/reflections/`
  (episodic); semantic under `projects/{project}/semantic/`; `actorId == project`;
  `general/` is the cross-project bucket. Records have **no TTL**; only EVENTS
  expire (`eventExpiryDuration` min3/max365d, live-updatable).
- Observations are immutable `CreateEvent`s keyed by `actorId`+`sessionId`; they
  carry only creation-time metadata — **no** mutable `status` /
  `reinforcement_score` / `episode_id`. `list_events` needs BOTH `actorId` and
  `sessionId`. There is **no list-namespaces API**; parents do not roll up.
- `ListActors(memoryId)` enumerates EVENT-derived actors (every project with
  `>=1` observe()), self-populating on first observe(). `ListSessions(actorId)`
  works. `ListActors` does NOT enumerate record namespaces, so a project that
  only has migrated records (no events yet) is invisible to it — hence the UNION
  with the local migration ledger.
- AgentCore reflection status model: `active` (born here == sqlite `confirmed`) /
  `promoted` (project->general) / `retired`. There is **no `pending_review`
  gate** and no `confirm` transition. Migrated reflection text lives in the JSON
  content body; AWS-extracted reflections are managed by the extraction pipeline.
- Settled local-vs-content split (`2026-07-24` parity design §1): durable CONTENT
  (observations, reflections, semantic, episodes) lives AWS-side in agentcore
  mode; session-operational state (`session_memory_exposure`, `hook_errors`,
  `retention_runs`, `rating_diagnostics`, `audit_log`, `agentcore_migration`)
  always lives in the local `memory.db` in BOTH modes.
- `create_app` today opens local `memory.db` directly and never imports the
  factory or any `StorageBackend`; ALL 47 UI reads/writes bypass the backend
  (`data-sources.md`). No capability gating exists in the UI — `supports_episodes`
  is referenced nowhere under `better_memory/ui` (grep-confirmed).

## Design

### 1. Capability flags (mirror `supports_episodes`)

Add five read-only `bool` properties to the `StorageBackend` Protocol alongside
the existing `supports_episodes` / `supports_synthesis`, each driving exactly one
UI surface:

| Flag | Surface it gates | sqlite | agentcore |
|---|---|---|---|
| `supports_episodes` *(exists)* | Episodes tab | `True` | `False` |
| `supports_observations` | Observations tab (nav + all `/observations*` routes) | `True` | `False` |
| `supports_provenance` | Reflection drawer source-obs/episode joins; observation drawer linked-reflections; and the expensive provenance FETCH itself | `True` | `False` |
| `supports_retention_runs` | Retention-runs panel on Diagnostics (+ its route) | `True` | `False` |
| `supports_reflection_confirm` | Confirm-reflection action (drawer button + `/confirm` route) | `True` | `False` |
| `supports_reflection_text_edit` | Inline reflection use_cases/hints edit (drawer button + `/edit` routes) | `True` | `False` |

`SqliteBackend` returns `True` for every new flag (nothing is hidden; behaviour
unchanged). `AgentCoreBackend` returns `False` for every new flag. The flags are
plain properties — no AWS call, no per-request cost.

Rationale per agentcore `False`:
- **observations** — no observation concept survives to the UI; events are handled
  transparently and `ListEvents` needs a sessionId the UI never has.
- **provenance** — the `reflection_sources -> observations -> episodes` graph has
  no AgentCore backing (no link table; observations are events; episodes absent).
- **retention_runs** — pruning in agentcore IS event expiry; no retention run ever
  writes the local `retention_runs` ledger, so the panel is permanently empty and
  misleading.
- **reflection_confirm** — reflections are born `active`; there is no
  `pending_review -> confirmed` gate to action.
- **reflection_text_edit** — reflection bodies are AI-managed by the extraction
  pipeline; see Open decisions.

### 2. `create_app` builds and routes a `StorageBackend` (the foundation)

`create_app` gains a backend built from config, and every CONTENT read/write on a
surface that stays visible in agentcore mode goes through it:

- Build via `storage/factory.build_backend(config=get_config(), memory_conn=db_conn,
  sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`.
  Store it in `app.extensions["backend"]`. The **same** `db_conn` is passed as
  `memory_conn` — in agentcore mode the factory threads it in as `local_conn`
  (operational-state ledger), and content lives AWS-side.
- **Operational-state reads/writes stay on the raw `db_conn`** in both modes:
  `hook_errors` (panel/drawer/delete/purge), `session_memory_exposure` (rating
  evidence, recent-ratings, exposure ledger), `rating_diagnostics` counters. These
  are correct locally by the settled split; they are NOT routed through the backend.
- A Jinja **context processor** injects a `caps` object (the six flags) into every
  template render so `base.html` and every fragment can gate without each route
  passing flags explicitly.
- **Content routing** (Tasks 6-7): semantic CRUD -> `backend.semantic_*`;
  reflection promote/retire -> `backend.promote_reflection` / `retire_reflection`;
  reflection list/detail -> backend read methods (§4). Each `SqliteBackend` method
  reproduces the current query/service behaviour verbatim, so the sqlite UI is
  unchanged and pinned by the existing UI suites; the agentcore method does the
  AWS version.

Backend construction must not break sqlite deployments that lack boto3 — the
factory already guards the agentcore import; sqlite mode never reaches it.

### 3. Gating mechanics

- **Nav** (`base.html` `.rail-nav`): wrap the Episodes and Observations links in
  `{% if caps.supports_episodes %}` / `{% if caps.supports_observations %}`. The
  remaining three links are always shown.
- **Routes**: every gated route (`/episodes*`, `/observations*`,
  `/diagnostics/panel/retention-runs`, `/reflections/<id>/confirm`,
  `/reflections/<id>/edit[/save]`) checks its flag first and `abort(404)` when
  `False`, so a hand-typed URL cannot reach a hidden surface in agentcore mode. In
  sqlite mode the flags are `True` and the guard is a no-op.
- **In-drawer sections/actions**: provenance blocks, the Confirm button, and the
  Edit button/form are wrapped in their `{% if caps.* %}`. Gated fragments must use
  **project-native brutalist CSS classes** (`.chip`, `.polarity-badge`,
  `.rating-badge`, shared `_rating_stat.html`), never Bootstrap utilities — those
  are undefined in `app.css` (see guardrail G4).
- **Provenance data-fetch**: `supports_provenance` gates the FETCH as well as the
  render. In sqlite the reflection drawer keeps calling `queries.reflection_detail`
  (provenance join intact). In agentcore the drawer fetches the row only (§4), so
  the join that has no backing is never issued.

### 4. Backend read surface for the standing Reflections tab

Two thin additions let the visible agentcore Reflections tab read real records
while keeping sqlite identical:

- `list_reflections_for_ui(*, project, tech, phase, polarity, status,
  min_confidence, useful_only) -> list[Row-like]` — SqliteBackend delegates to the
  existing `queries.reflection_list_for_ui` (identical rows/order/filters, sqlite
  unchanged). AgentCoreBackend flattens its existing bucketed `retrieve()`
  (already Wilson-ordered, per-namespace) and post-filters `min_confidence` /
  `useful_only`; `status` is inert (all records `active`), `polarity`/`tech`/`phase`
  pass through to `retrieve`.
- `get_reflection_for_ui(*, reflection_id) -> dict | None` — SqliteBackend delegates
  to `queries.reflection_detail` (row + provenance; sqlite drawer unchanged, still
  renders provenance because `supports_provenance=True`). AgentCoreBackend returns
  the parsed record row WITHOUT provenance (drawer hides it via the flag).

Semantic CRUD (`semantic_list`, `semantic_observe`, `semantic_update_text`,
`semantic_set_scope`, `semantic_delete`, `_get_semantic_record`) and reflection
`promote_reflection` / `retire_reflection` already exist on both backends
(`agentcore-mapping.md` EASY) — Task 6 is pure wiring.

### 5. Distinct-project dropdown -> `distinct_projects`

Replace the raw `SELECT DISTINCT project FROM reflections` behind the Reflections
project dropdown with a backend method `distinct_projects() -> list[str]`:

- SqliteBackend: `SELECT DISTINCT project FROM reflections` — **identical** to
  today's `queries.reflection_distinct_projects`, so the sqlite dropdown is
  unchanged.
- AgentCoreBackend: `ListActors(memoryId)` (event-derived project actors) UNION the
  project set parsed from the local `agentcore_migration` ledger's `namespace`
  column (`projects/{project}/...` -> `{project}`) — covers projects that have only
  migrated records and no events yet. Best-effort: an AWS error degrades to the
  ledger-only set; empty ledger + AWS error -> `[]` (the route still unions in
  `project_name()`, so the current project is always selectable).

The route keeps its existing `sorted({project_name(), *backend.distinct_projects()})`
union — only the data source changes. The Observations dropdown is untouched (that
tab is hidden in agentcore; sqlite unchanged).

## Open decisions (resolved for this autonomous pass)

- **Inline reflection text-edit in agentcore** — the converged design flagged this
  a genuine open decision. **Resolved: disable** (`supports_reflection_text_edit=False`
  on agentcore). Rationale: reflection bodies are AI-managed by the AWS extraction
  pipeline; a human free-text edit would either be clobbered by re-extraction or
  require a body-RMW path (`agentcore-mapping.md` HARD — AWS-extracted editability
  unresolved). Flipping later is a one-line flag change plus wiring
  `batch_update_memory_records`; recorded as revisitable, not closed.

## Scope / boundaries (deferred to companion parity work-areas)

- **Observation content reads** (list panel across sessions, detail body) — HARD
  (`ListEvents` needs a sessionId). Moot here: the whole tab is hidden.
- **Diagnostics content aggregates** — the recent-ratings title LEFT JOIN and the
  overlooked-total SUM read AWS-side records with no aggregate API. Not gated
  (Diagnostics stays visible for hook-errors + rating counters); they degrade
  gracefully in agentcore (title falls back to the id; SUM reads local-only) and
  full parity is a named follow-up, not this PR.
- **Promote-observation-to-semantic**, **episode lifecycle**, **retention
  archive/prune** — HARD/moot (hidden or event-plane); unchanged from prior designs.

## Non-goals

- Serving hidden surfaces in agentcore (they are hidden, not reimplemented).
- Local embedding writes from agentcore mode (server-side search; unchanged).
- Any change to sqlite-mode behaviour, ordering, filters, or rendered output.

## Error handling

- Backend build failure in sqlite mode is impossible (no AWS path); in agentcore
  mode a missing `agentcore.json` already raises a clear `FileNotFoundError` from
  the factory (unchanged).
- `distinct_projects` / agentcore read methods are best-effort: AWS errors degrade
  to the ledger-only / Wilson-only / empty result, never raise into a route.
- Gated routes `abort(404)` rather than 500 when reached with the flag off.

## Validation

- **Unit**: capability-flag values per backend; context processor injects `caps`;
  gated routes 404 in agentcore, 200 in sqlite; nav omits/keeps links per flag;
  `distinct_projects` sqlite-identity + agentcore ListActors-UNION-ledger (stubbed
  boto); `list_reflections_for_ui` / `get_reflection_for_ui` sqlite-identity vs the
  current queries.
- **Sqlite-preservation**: the entire existing `tests/ui/*` suite passes unchanged
  — the proof that every flag is `True` and no gate fires in sqlite mode.
- **Agentcore app-boot** (guardrail G7): build `create_app` with
  `storage_backend=agentcore` (stubbed clients) and drive a real gated request —
  point any leaked local-content read at an assertion so a bypass fails loudly,
  not silently.
- **Full suite + pyright**; no live AWS in CI; manual live-smoke checklist in the
  PR body (agentcore session: nav hides Episodes/Observations, Reflections/Semantic
  read real records, dropdown lists real projects).
