# AgentCore UI Content Wiring — Reflections + Semantic

**Date:** 2026-07-25
**Status:** approved design (converged with the user 2026-07-25, empirically
verified against the live AgentCore instance eu-west-2 acct 708306701628),
pending implementation.
**Depends on:** the **backend-wiring area** (CORE-INFRA) — `create_app` builds a
`StorageBackend` via `storage/factory.build_backend`, stores it at
`app.extensions["backend"]`, keeps the local `sqlite3.Connection` at
`app.extensions["db_connection"]` for OPERATIONAL-STATE only, and threads the
backend's capability flags into every template (a `capabilities` context
processor / `caps` object). This area consumes that seam and extends it.
**Predecessors:** #87 agentcore learning-loop parity
(`2026-07-24-agentcore-parity-design.md`); the two reference inventories
`docs/agentcore-ui/data-sources.md` and `docs/agentcore-ui/agentcore-mapping.md`.

## Planning guardrails (surfaced from planning + implementation memory)

These are high-confidence reflections that govern this plan; they sit at the top
so the executing agent cannot miss them.

- **[[keep-docs-in-sync]]** (conf 0.95, evidence 7) — every code change that
  touches tools/config/architecture must sync `website/*` and `README.md` in the
  SAME PR. This area changes UI data-flow and adds capability flags → the
  agentcore capability table in `website/agentcore-setup.md` and any
  UI/architecture prose that says "UI opens local sqlite" must be updated. A
  dedicated docs task (Task 8) exists; note "docs unaffected" explicitly for any
  file genuinely untouched.
- **[[bootstrap-brutalist-classes]]** (conf 0.75) — Bootstrap utility classes
  (`badge`, `bg-success`, …) are NOT defined in the UI `app.css`. Any new gated
  markup reuses the project-native classes (`.polarity-badge`, `.phase-badge`,
  `.rating-badge`, shared partials) — verify against `app.css` before use.
- **[[playwright-textContent]]** (conf 0.8) — Playwright `has_text=`/`text=`
  match DOM textContent, not CSS-rendered case. Capability-gated template edits
  that only hide/show blocks are safe; label-case restyles are not part of this
  area.
- **[[server-boot-dead-host]]** (conf 0.65) — "constructs without throwing" is a
  weak test. Backend-routing tests must drive an actual backend call (stubbed
  boto for agentcore) and assert the local sqlite path is NOT hit for content
  reads, not merely that `create_app` builds.
- **[[verify-internal-patterns]]** (standards/ralph-runtime, conf 0.9) — when a
  task says "follows existing pattern X", read X's source at spec-write time.
  Every backend method / service signature this plan cites was read at
  HEAD 76442eb (see the verified-facts table).

Dismissed as not-applicable: TypeScript/htmx/tempfile/PRAGMA reflections (no TS,
no new htmx mutation-error surfaces, no temp-file or PK-ordinal code here).

## Goal

In **agentcore mode**, the two VISIBLE content surfaces — the Reflections tab
(list, detail without provenance, promote, retire) and the Semantic tab (full
CRUD: create / update-text / set-scope / delete / list / detail) — read and
write through `StorageBackend` methods instead of the local `sqlite3.Connection`.
In **sqlite mode** every one of these surfaces behaves byte-identically to today.
The split is driven by backend **capability flags**, so the sqlite path is never
branched on backend identity — only on declared capability, which sqlite reports
exactly as it behaves now.

## Grounding (verified against source at HEAD 76442eb — do not re-derive)

| Fact | Where |
|---|---|
| UI `create_app` opens local `memory.db` unconditionally; services (`ReflectionService`, `SemanticMemoryService`, `EpisodeService`) get the raw conn, never a `StorageBackend`. This is the dependency's job to fix; this area assumes `app.extensions["backend"]` exists post-dependency. | `ui/app.py:83-97`; data-sources.md:9-13 |
| `SqliteBackend.promote_reflection` / `retire_reflection` wrap `ReflectionService.promote_to_general` / `retire` **verbatim** — same ValueError/idempotency semantics as the UI's current direct service calls. Rerouting the UI promote/retire routes through the backend is behaviour-identical for sqlite. | `storage/sqlite.py:345-349` |
| `SqliteBackend.semantic_{observe,list,update_text,set_scope,delete}` wrap `SemanticMemoryService.{create,list_for_project,update_text,set_scope,delete}` verbatim, threading the same `sync_embedder`. Rerouting UI semantic CRUD through the backend is behaviour-identical for sqlite. | `storage/sqlite.py:169-204` |
| `SqliteBackend.semantic_list(track_exposure=True)` → `list_for_project(track_exposure=True)` — same Wilson ranking + local `session_memory_exposure` side-effect the UI relies on today (UI currently calls `list_for_project` with the default `track_exposure=True`). | `storage/sqlite.py:182-195`; app.py:507; data-sources.md:140 |
| `AgentCoreBackend` already implements `promote_reflection` (→ `general/reflections`, status=promoted), `retire_reflection` (→ `{actor}/retired`, status=retired), and full semantic CRUD (`semantic_observe`/`semantic_list`/`semantic_update_text`/`semantic_set_scope`/`semantic_delete`). No new WRITE methods needed on agentcore for those. | `agentcore.py:1578-1591, 1199-1476` |
| `AgentCoreBackend.semantic_list` maps summaries → the same `SemanticMemory` dataclass the sqlite read model returns (attribute access), with counters from metadata. | `agentcore.py:1246-1341` |
| `AgentCoreBackend._get_semantic_record(record_id)` (GetMemoryRecord) exists but is **private and returns the raw record**, not a model. Detail-drawer wiring needs a public `semantic_get` returning a `SemanticMemory`. | `agentcore.py:1193-1197` |
| `AgentCoreBackend._parse_reflection_record(rec)` maps a record → the sqlite-shaped reflection dict (body-first / metadata-fallback, incl. `status`, `polarity`, `confidence`, counters, `updated_at`). `_fetch_reflection_buckets` fans out `projects/{actor}/reflections/` + `general/reflections/` via `list_memory_records`, client-side status/polarity filtering, Wilson-ranked. No FLAT list method and no single-record reflection accessor is exposed. | `agentcore.py:317-408, 732-885` |
| `AgentCoreBackend._get_record(id)` + `_record_body(record)` exist (used by `_mutate_namespace_and_status`). A public `reflection_get` = `_get_record` + `_parse_reflection_record`. | `agentcore.py:1523-1525` |
| Reflection status model, agentcore: **active** (born here, == sqlite `confirmed`) / **promoted** / **retired**. NO `pending_review` gate; NO `confirmed`/`superseded`. Records with no status metadata parse as `active`. | agentcore-mapping.md:74; agentcore.py:838-842 |
| `queries.reflection_list_for_ui` — six filters (project/tech/phase/polarity/status/min_confidence) + `useful_only`; default `status IN ('pending_review','confirmed')`; order confidence DESC, updated_at DESC, rowid DESC. | `ui/queries.py:221`; data-sources.md:110 |
| `queries.reflection_detail` — two SELECTs: (1) the row incl. rating counters + `last_*_at`; (2) provenance `reflection_sources → observations → episodes`. Returns None → 404. | `ui/queries.py:385`; data-sources.md:111 |
| `queries.fetch_rating_evidence(conn, kind, id)` reads local `session_memory_exposure` (OPERATIONAL — stays on `db_conn` in both modes). | `ui/queries.py:800`; data-sources.md:112 |
| UI reflection routes: `reflections_panel` (queries.reflection_list_for_ui), `reflections_drawer` (queries.reflection_detail + fetch_rating_evidence), `reflection_confirm`/`retire`/`edit_save`/`promote` (ReflectionService on `db_conn`). Semantic routes: `semantic_panel` (SemanticMemoryService.list_for_project), `semantic_create`/`scope`/`delete`/`update` (SemanticMemoryService), `semantic_drawer` (inline raw SELECT + fetch_rating_evidence). | `ui/app.py:290-604` |
| `supports_episodes` is the ONLY capability flag; agentcore reports False. No `supports_provenance` / review / text-edit flag exists yet. | `protocol.py:57-64`; agentcore.py:105-107 |
| `services/scoring.wilson_lower_bound(pos, n)` is the shared ranker used by both backends' reflection retrieval. | `2026-07-24-agentcore-parity-design.md` §3 |

## Design

### 1. Three new READ methods on `StorageBackend` (content accessors)

The write side already exists on both backends (promote/retire + semantic CRUD).
The gap is three READs the visible surfaces need. Each is added to
`storage/protocol.py` and implemented on BOTH backends; sqlite implementations
are thin, behaviour-preserving wrappers over existing `queries.py` /
`services/*` code so the sqlite path stays byte-identical.

**1a. `reflection_list` — flat list for the panel.**

```python
def reflection_list(
    self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None,
    status: str | None = None, min_confidence: float = 0.0,
    useful_only: bool = False, limit: int = 200,
) -> list[dict[str, Any]]:
    """Flat reflection list for the management panel (NOT bucketed retrieval).
    Returns row dicts with the keys reflection_row.html renders."""
```

- **Sqlite:** delegates to `queries.reflection_list_for_ui(self._conn, ...)`
  unchanged — same filters, same default `status IN ('pending_review',
  'confirmed')`, same ordering. Byte-identical to today.
- **Agentcore:** `list_memory_records` over the concrete namespaces —
  `projects/{actor}/reflections/`, `general/reflections/` (promoted), and
  `{actor}/retired/` (only when the caller's status filter admits retired) —
  parse each via `_parse_reflection_record`, apply the client-side
  filters (tech/phase/polarity/min_confidence/useful_only) the parser/bucket
  code already models, and order **flat** by
  `wilson_lower_bound(useful+overlooked, useful+overlooked+ignored)` desc,
  confidence desc, `updated_at` desc. Default status set =
  `{active, promoted}` (retired excluded unless explicitly requested), the
  active/promoted/retired analogue of sqlite's `pending_review`/`confirmed`
  default. Returns dicts with the SAME keys the row template reads.

**1b. `reflection_get` — single reflection row (NO provenance).**

```python
def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
    """The reflection ROW (counters + last_*_at where available). Provenance
    (source-observation / episode joins) is a SEPARATE, capability-gated
    concern — never returned here. None when absent."""
```

- **Sqlite:** returns exactly the row-portion fields of
  `queries.reflection_detail` (a `queries.reflection_row(conn, id)` extraction —
  the identical SELECT the current detail query runs for the row, no sources).
- **Agentcore:** `_get_record(reflection_id)` + `_parse_reflection_record`;
  `last_*_at` fields resolve to None (agentcore records carry no per-rating
  timestamps — the drawer template already tolerates None). None when
  GetMemoryRecord 404s.

**1c. `semantic_get` — single semantic memory.**

```python
def semantic_get(self, *, id: str) -> Any | None:
    """A single SemanticMemory read model (same shape semantic_list yields).
    None when absent."""
```

- **Sqlite:** wraps the inline SELECT currently in `semantic_drawer`
  (extracted to `queries.semantic_detail` / a `SemanticMemoryService.get`),
  returning the same field set the drawer template reads.
- **Agentcore:** `_get_semantic_record` + `_semantic_summary_to_model`.

### 2. Capability flags (extend the dependency's capability seam)

The backend-wiring dependency threads the backend's capability flags into
templates. This area adds three flags to `StorageBackend` (protocol + both
impls) and references them in the reflection templates. Each is a `@property`,
sqlite reporting the value that reproduces today's behaviour.

| Flag | sqlite | agentcore | Gates |
|---|---|---|---|
| `supports_reflection_review` | `True` | `False` | The **Confirm** action (born-active model has no `pending_review→confirmed` gate) AND the status-filter `<select>` option set (sqlite: pending_review/confirmed/retired/superseded; agentcore: active/promoted/retired). |
| `supports_provenance` | `True` | `False` | The reflection-detail **provenance section** (source-observation / episode joins). Hidden in agentcore — there is no `reflection_sources` linkage server-side. |
| `supports_reflection_text_edit` | `True` | `False` | The inline **Edit** action (AI-managed reflection text). **DEFAULT off** in agentcore — see Open Decisions. |

`supports_episodes` (already present) continues to hide the Episodes tab; the
Observations tab and retention-runs panel are hidden by OTHER areas' flags and
are out of scope here.

### 3. Route rewiring (Reflections tab)

All routes read the backend from `app.extensions["backend"]`; rating evidence
and the 404-existence checks that back the drawer stay on `db_conn` /
backend as noted.

- **`reflections_panel`** → `backend.reflection_list(...)` (replaces
  `queries.reflection_list_for_ui`). Row template unchanged.
- **`reflections_drawer`** → `backend.reflection_get(id)` for the row (404 when
  None); `queries.fetch_rating_evidence(db_conn, "reflection", id)` for the
  evidence receipts (OPERATIONAL, stays local); provenance fetched via
  `queries.reflection_provenance(db_conn, id)` **only when
  `caps.supports_provenance`**. The drawer template wraps the provenance block
  in `{% if supports_provenance %}` and the Confirm/Edit buttons in their
  respective flags.
- **`reflection_promote`** → `backend.promote_reflection(reflection_id=id)`.
- **`reflection_retire`** → `backend.retire_reflection(reflection_id=id)`.
  Existence pre-check and drawer re-render go through `backend.reflection_get`.
- **`reflection_confirm`** and **`reflection_edit_form`/`reflection_edit_save`**
  remain **sqlite-only** (they use `ReflectionService` on `db_conn`; the
  protocol has no confirm/text-edit method and the born-active model has no
  such actions). Their buttons are gated OFF in agentcore
  (`supports_reflection_review` / `supports_reflection_text_edit`), so the
  routes are never surfaced there. Left registered and untouched — sqlite
  behaviour is literally the same code.

### 4. Route rewiring (Semantic tab)

- **`semantic_panel`** → `backend.semantic_list(project=…, scope_filter=…,
  search=…)` (replaces the per-route `SemanticMemoryService.list_for_project`).
  The exposure/Wilson side-effect is preserved for sqlite (same service
  underneath); agentcore lists server-side and writes no local content.
- **`semantic_create`** → `backend.semantic_observe(content=…, project=…,
  scope=…)`. ValueError → 400 card unchanged.
- **`semantic_update`** → `backend.semantic_update_text(id=…, content=…)`.
- **`semantic_scope`** → `backend.semantic_set_scope(id=…, scope=…)`.
- **`semantic_delete`** → `backend.semantic_delete(id=…)` (idempotent).
- **`semantic_drawer`** → `backend.semantic_get(id=…)` for the row (404 when
  None) + `queries.fetch_rating_evidence(db_conn, "semantic", id)` (local).

The semantic tab has NO capability-gated hiding — full CRUD is servable on both
backends (agentcore-mapping.md classes every semantic item EASY). The `HX-Trigger`
contracts (`semantic-changed`, `reflection-changed`) are unchanged.

### 5. Why sqlite is provably unchanged

Every sqlite code path is either (a) the exact same `queries.py`/service call it
makes today, now reached through a thin backend wrapper that forwards verbatim,
or (b) literally untouched (confirm/edit routes). The capability flags sqlite
reports (`supports_reflection_review=True`, `supports_provenance=True`,
`supports_reflection_text_edit=True`) make every gated template block render
exactly as now. The existing UI test suite pins the rendered HTML; a
sqlite-mode regression fails those tests.

## Non-goals

- **Provenance in agentcore** — no `reflection_sources` linkage exists
  server-side; the section is hidden, not reconstructed.
- **Observations tab, Episodes tab, Diagnostics retention-runs panel,
  distinct-project dropdown rework** — owned by the backend-wiring / other
  areas. This area touches only Reflections + Semantic content surfaces.
- **Reflection text-edit ON agentcore** — default off (Open Decision below);
  no AWS text-update method is wired.
- **Local vec0 re-embedding from agentcore mode** — AWS-managed; a non-goal
  inherited from the parity design.
- **Cross-store title joins / overlooked aggregates** (Diagnostics) — HARD,
  out of scope.

## Error handling

- Backend content reads that 404 (GetMemoryRecord / absent row) → the same
  `abort(404)` the routes raise today.
- Write ValueErrors (invalid scope, lifecycle block) → the same 400/409 cards.
- AgentCore AWS calls remain best-effort with the existing
  `_retry_on_transient_404` / reserved-metadata-strip helpers; a list failure
  degrades to an empty panel rather than a 500 (mirrors the empty-list render).
- OPERATIONAL reads (`fetch_rating_evidence`, hook errors, rating counters)
  never move — they stay on `db_conn` in both modes and cannot regress.

## Validation

- **Unit (backend):** `reflection_list` flat-order parity (a Wilson fixture
  ranks identically to `test_wilson_ranking.py`); default status set excludes
  retired; `reflection_get` returns None on 404 and the row dict on hit;
  `semantic_get` returns the model / None. Sqlite wrappers pinned against the
  existing `queries` output on a seeded tmp db; agentcore via stubbed boto per
  `tests/storage/test_agentcore_unit.py`.
- **Capability flags:** sqlite reports all three True, agentcore all three
  False; a template-render test asserts the provenance/confirm/edit blocks are
  present under sqlite caps and absent under agentcore caps.
- **Route (UI):** with a stubbed agentcore backend injected at
  `app.extensions["backend"]`, the reflection panel/drawer/promote/retire and
  the full semantic CRUD render/act against the backend and NOT the local
  content tables (dead-content-table trick: seed the local `reflections` /
  `semantic_memories` tables with a sentinel row and assert it never appears —
  proving the read went to the backend). Sqlite-mode UI suite unchanged and
  green.
- **Full suite + pyright 0 errors.** No live AWS in CI; a manual live-smoke
  checklist (one real agentcore session: list reflections, open a drawer,
  promote + retire one, create/edit/scope/delete a semantic) goes in the PR body.

## Open decisions (surface to the user before/at execution)

**OD-1 — inline reflection text-edit on agentcore.** The converged design
defaults this OFF (`supports_reflection_text_edit=False`) because agentcore
reflection text is AI-managed by the extraction pipeline and no reflection
text-update backend method exists. This is a genuine open decision, not a
settled fact:

- **A (default, this design):** hide the Edit action in agentcore. Lowest risk;
  no new AWS write path; matches "AI-managed" framing.
- **B:** wire a `reflection_update_text` backend method (body RMW for migrated
  records via `batch_update_memory_records`; AWS-extracted records' editability
  of `use_cases`/`hints` unresolved) and surface Edit. Larger surface; touches
  the extraction-managed body.
- **C:** surface Edit only for MIGRATED reflections (source_backend==sqlite),
  hide for AWS-extracted. Half-measure; needs the parser to expose origin.

Recommend A for this area; B/C are a follow-up PR if the user wants live
editing of migrated reflection text.
