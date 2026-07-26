# AgentCore UI Content Wiring (Reflections + Semantic) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the VISIBLE agentcore content surfaces — Reflections (list, detail without provenance, promote, retire) and Semantic (full CRUD) — through `StorageBackend` methods, driven by capability flags, with sqlite-mode behaviour byte-identical throughout.

**Architecture:** Three new READ accessors (`reflection_list`, `reflection_get`, `semantic_get`) join the already-existing write methods (promote/retire + semantic CRUD) on the protocol and both backends; sqlite impls are thin verbatim wrappers over today's `queries.py`/`services/*`. Three capability flags (`supports_reflection_review`, `supports_provenance`, `supports_reflection_text_edit`) drive template gating. The UI reflection + semantic routes stop calling `queries.py`/`SemanticMemoryService` on the raw conn and call `app.extensions["backend"]` instead; operational reads (rating evidence) stay on `db_conn`.

**Tech Stack:** Python 3.12, Flask + Jinja + htmx, sqlite, boto3 (stubbed via MagicMock in tests — `tests/storage/test_agentcore_unit.py` pattern), pytest, Playwright for UI render pins.

**Spec:** `docs/superpowers/specs/2026-07-25-agentcore-ui-content-wiring-design.md`

## Global Constraints

- **DEPENDENCY:** the backend-wiring area must have landed first — `create_app` builds `app.extensions["backend"]` (a `StorageBackend` from `factory.build_backend`, scoped to `project_name()`), keeps `app.extensions["db_connection"]` for operational-state only, and threads the backend's capability flags into templates (a `capabilities` context processor exposing `caps`). If that seam is absent, Task 0 (spike) stops and escalates rather than re-building it here.
- Branch `feat/agentcore-ui-content-wiring` created at task start (before changes) per standards/ralph-runtime.
- Test command `./.venv/Scripts/python.exe -m pytest <path> -v`; pyright 0 errors; NO live AWS (MagicMock clients only).
- **Sqlite behaviour byte-identical.** Every sqlite path is either a verbatim wrapper forwarding to today's `queries.py`/service call, or literally untouched (confirm/edit routes). The existing UI suite (`tests/ui/*`) pins rendered HTML — a sqlite regression fails it. Every task states its sqlite-preservation pin.
- AgentCore calls best-effort: reuse `_retry_on_transient_404` and the reserved-metadata-strip helper; a list failure renders an empty panel, never a 500.
- ASCII only; ruff line 100; stage exact paths; one commit per task; commit footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- No new env keys; no new MCP tools (this is UI-plane only).
- Capability flags are `@property` on the backend; sqlite reports the value that reproduces today's behaviour (all three True).

## Verified-against-source facts (HEAD 76442eb — do not re-derive)

| Fact | Where |
|---|---|
| `SqliteBackend.promote_reflection`/`retire_reflection` wrap `ReflectionService.promote_to_general`/`retire` verbatim | `storage/sqlite.py:345-349` |
| `SqliteBackend.semantic_{observe,list,update_text,set_scope,delete}` wrap `SemanticMemoryService.*` verbatim, same `sync_embedder` | `storage/sqlite.py:169-204` |
| `AgentCoreBackend.promote_reflection`/`retire_reflection` + full semantic CRUD already implemented | `agentcore.py:1199-1476, 1578-1591` |
| `AgentCoreBackend._parse_reflection_record` (body-first/metadata-fallback: status/polarity/confidence/counters/updated_at) | `agentcore.py:732-885` |
| `AgentCoreBackend._get_record(id)` + `_record_body` + `_get_semantic_record(id)` (private) | `agentcore.py:1193-1197, 1523-1525` |
| `_fetch_reflection_buckets` namespace fan-out + client-side filter/rank pattern to mirror for the flat list | `agentcore.py:317-408` |
| `queries.reflection_list_for_ui` (six filters + useful_only; default `status IN ('pending_review','confirmed')`; order confidence/updated_at/rowid DESC) | `ui/queries.py:221` |
| `queries.reflection_detail` (row SELECT + provenance SELECT); `queries.fetch_rating_evidence` (local exposure) | `ui/queries.py:385, 800` |
| Reflection routes + semantic routes current wiring | `ui/app.py:290-604` |
| `services/scoring.wilson_lower_bound(pos, n)` shared ranker | parity design §3 |
| Only `supports_episodes` exists today | `protocol.py:57-64`; `agentcore.py:105-107` |
| Reflection status model agentcore = active/promoted/retired; no pending_review/confirmed/superseded; no-status parses active | agentcore.py:838-842 |

---

### Task 0: Dependency spike — confirm the backend-wiring seam (confidence 92%)

**Files:** none (read-only verification).

- [ ] **Step 1:** Confirm on the current branch base that `create_app` builds `app.extensions["backend"]`, retains `app.extensions["db_connection"]`, and exposes capability flags to templates (grep `app.extensions["backend"]`, a `capabilities`/`caps` context processor). Confirm `supports_episodes` is already threaded into `base.html`.
- [ ] **Step 2:** If ANY of those are absent, STOP and escalate — this plan builds ON that seam and must not silently re-implement it. If present, record the exact `caps` accessor name the templates use (the plan below assumes `caps.<flag>`; adjust template references to the real name).

*Sqlite preservation:* read-only; no change.

---

### Task 1: Capability flags on the protocol + both backends (confidence 95%)

**Files:**
- Modify: `better_memory/storage/protocol.py` (three `@property` declarations), `better_memory/storage/sqlite.py` (return True×3), `better_memory/storage/agentcore.py` (return False×3)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces (exact):**
```python
@property
def supports_reflection_review(self) -> bool: ...   # sqlite True, agentcore False
@property
def supports_provenance(self) -> bool: ...          # sqlite True, agentcore False
@property
def supports_reflection_text_edit(self) -> bool: ...# sqlite True, agentcore False
```
Docstrings state what each gates (Confirm action + status vocabulary; provenance section; inline Edit — default off per OD-1).

- [ ] **Step 1:** Failing tests — assert each flag's value on both backends (agentcore via the existing `backend` fixture; sqlite via its fixture).
- [ ] **Step 2:** Run — FAIL (attributes missing).
- [ ] **Step 3:** Implement the three properties on both backends; add protocol declarations with docstrings.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage -q` — pass.
- [ ] **Step 5:** Commit `feat(storage): reflection-review/provenance/text-edit capability flags`.

*Sqlite preservation:* new properties only; sqlite reports True — no behavioural branch fires until templates read them (Task 6). Existing tests untouched.

---

### Task 2: `reflection_get` — single reflection row, both backends (confidence 92%)

**Files:**
- Modify: `better_memory/storage/protocol.py` (method), `better_memory/ui/queries.py` (extract `reflection_row(conn, id)` from `reflection_detail`'s row SELECT + `reflection_provenance(conn, id)` from its provenance SELECT), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`, `tests/ui/test_queries.py` (pin the extraction)

**Interfaces (exact):**
```python
def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None: ...
```
Returns the row dict (counters + `last_*_at` where available), NO provenance. None when absent.

- Sqlite impl → `queries.reflection_row(self._conn, reflection_id)`.
- Agentcore impl → `_get_record` + `_parse_reflection_record`; `last_*_at` → None; None on GetMemoryRecord 404 (catch the not-found via the existing helper).
- `queries.reflection_row` + `queries.reflection_provenance` together must reproduce `reflection_detail` exactly (pin: `reflection_detail`'s dict == `{**reflection_row, "sources": reflection_provenance}` on a seeded db).

- [ ] **Step 1:** Failing tests — sqlite `reflection_get` returns the same row fields as `reflection_detail` minus sources on a seeded reflection; None for a missing id. Agentcore: stubbed `get_memory_record` (body-shape + metadata-shape) → parsed dict; 404 → None. Extraction pin test in `test_queries.py`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement the extraction (behaviour-preserving; `reflection_detail` now composes the two helpers so its output is unchanged) + both backend methods + protocol decl.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui/test_queries.py -q` — pass.
- [ ] **Step 5:** Commit `feat(storage): reflection_get row accessor + queries row/provenance split`.

*Sqlite preservation:* `reflection_detail` recomposed from the two extracted helpers → identical output, pinned by the equality test; every existing drawer test still green.

---

### Task 3: `reflection_list` — flat list, both backends (confidence 90%)

**Files:**
- Modify: `better_memory/storage/protocol.py` (method), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl + retired-namespace fan-out)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces (exact):**
```python
def reflection_list(
    self, *, project: str | None = None, tech: str | None = None,
    phase: str | None = None, polarity: str | None = None,
    status: str | None = None, min_confidence: float = 0.0,
    useful_only: bool = False, limit: int = 200,
) -> list[dict[str, Any]]:
```
Returns row dicts with the keys `reflection_row.html` renders (id, title, phase, polarity, confidence, tech, status, useful_count, times_misled, times_overlooked, evidence_count, updated_at — read the template for the exact set BEFORE implementing).

- **Sqlite:** delegate to `queries.reflection_list_for_ui(self._conn, project=project or self._project, tech=…, …)` verbatim.
- **Agentcore:** fan out `list_memory_records` over `projects/{actor}/reflections/`, `general/reflections/`, and `{actor}/retired/` (retired ns included ONLY when the resolved status set admits retired), parse via `_parse_reflection_record`, dedup by id (project ns wins), apply tech/phase/polarity/min_confidence/useful_only client-side, default status set `{active, promoted}` when `status` is None (else the single requested status), order flat by `wilson_lower_bound(useful+overlooked, useful+overlooked+ignored)` desc, confidence desc, `updated_at` ts desc; truncate to `limit`. Reuse the `_fetch` pagination helper shape from `_fetch_reflection_buckets`.

- [ ] **Step 1:** Failing tests — sqlite: `reflection_list` == `queries.reflection_list_for_ui` on a seeded db across a couple of filter combos. Agentcore: three stubbed records (67/125, 3/1, 0/0 counters — same numbers as `test_wilson_ranking.py`) assert flat Wilson order; a retired record is excluded by default and included when `status='retired'`; polarity/tech filters drop non-matches; row-key completeness asserted against the template's field list.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement both; import `wilson_lower_bound` from `services/scoring`.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage -q` — pass.
- [ ] **Step 5:** Commit `feat(storage): flat reflection_list with Wilson ordering on both backends`.

*Sqlite preservation:* sqlite impl is a pure forward to the existing query — same rows, same order. Confidence 90 (not higher) because the agentcore row-key set must match the template exactly; Step 1 reads `reflection_row.html` first to pin it.

---

### Task 4: `semantic_get` — single semantic memory, both backends (confidence 93%)

**Files:**
- Modify: `better_memory/storage/protocol.py` (method), `better_memory/ui/queries.py` (extract `semantic_detail(conn, id)` from the inline `semantic_drawer` SELECT) OR `better_memory/services/semantic.py` (`SemanticMemoryService.get`), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces (exact):**
```python
def semantic_get(self, *, id: str) -> Any | None: ...  # a SemanticMemory | None
```
- Sqlite → the extracted single-row read returning the same field set the drawer template reads (as a `SemanticMemory` or the existing drawer dict shape — match what `semantic_drawer.html` consumes; read it first).
- Agentcore → `_get_semantic_record` + `_semantic_summary_to_model`; None on 404.

- [ ] **Step 1:** Failing tests — sqlite returns the drawer field set for a seeded memory, None for missing. Agentcore stubbed `get_memory_record` → model with counters; 404 → None.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement; keep the drawer's exact field set (read `semantic_drawer.html`).
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage -q` — pass.
- [ ] **Step 5:** Commit `feat(storage): semantic_get single-record accessor on both backends`.

*Sqlite preservation:* the drawer's inline SELECT becomes a named function returning the identical shape; Task 6 rewires the route to it. No behaviour change until then.

---

### Task 5: Rewire the Semantic routes through the backend (confidence 92%)

**Files:**
- Modify: `better_memory/ui/app.py` (`semantic_panel`, `semantic_create`, `semantic_scope`, `semantic_delete`, `semantic_drawer`, `semantic_update`)
- Test: `tests/ui/test_semantic_routes.py` (extend with an agentcore-backend-stub render/act test)

**Interfaces:** each route reads `backend = app.extensions["backend"]` and calls the corresponding backend method (`semantic_list`/`semantic_observe`/`semantic_set_scope`/`semantic_delete`/`semantic_get`/`semantic_update_text`). `fetch_rating_evidence` stays on `db_conn`. `HX-Trigger`/400-card contracts unchanged. Drop the per-route `SemanticMemoryService(...)` construction and the inline drawer SELECT.

- [ ] **Step 1:** Failing tests — inject a stubbed agentcore backend at `app.extensions["backend"]`; assert panel lists from the backend, create/update/scope/delete call the backend methods, drawer renders from `semantic_get`. Dead-content-table trick: seed the local `semantic_memories` table with a sentinel and assert it never renders (proves the read went to the backend, not local sqlite).
- [ ] **Step 2:** Run — FAIL (routes still hit the service/conn).
- [ ] **Step 3:** Rewire; keep validation/`HX-Trigger`/error-card behaviour identical.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass (sqlite-mode semantic tests unchanged and green).
- [ ] **Step 5:** Commit `feat(ui): route semantic CRUD through StorageBackend`.

*Sqlite preservation:* in sqlite mode `app.extensions["backend"]` is a `SqliteBackend` whose methods forward to the SAME `SemanticMemoryService` on the SAME conn (same `sync_embedder`) — identical writes, identical Wilson-ranked list with the exposure side-effect. Existing sqlite UI tests pin it.

---

### Task 6: Rewire the Reflections routes + capability-gate the templates (confidence 88%)

**Files:**
- Modify: `better_memory/ui/app.py` (`reflections_panel`, `reflections_drawer`, `reflection_promote`, `reflection_retire`; leave `reflection_confirm`/`edit_*` untouched), `better_memory/ui/templates/fragments/reflection_drawer.html` (gate provenance / confirm / edit), `better_memory/ui/templates/fragments/reflection_filter_form.html` (gate status `<select>` options), `better_memory/ui/templates/reflections.html` if the status default is inlined there
- Test: `tests/ui/test_reflection_routes.py`, `tests/ui/test_reflection_templates.py` (gating assertions)

**Interfaces:**
- `reflections_panel` → `backend.reflection_list(...)`.
- `reflections_drawer`/`promote`/`retire` → `backend.reflection_get(id)` for row + existence (404 when None); `queries.fetch_rating_evidence(db_conn, …)` for receipts; provenance via `queries.reflection_provenance(db_conn, id)` ONLY when `caps.supports_provenance`; promote/retire → `backend.promote_reflection`/`retire_reflection`.
- Templates: `{% if supports_provenance %}` around the provenance block; `{% if supports_reflection_review %}` around the Confirm button and to select the status `<select>` option set (sqlite: pending_review/confirmed/retired/superseded; agentcore: active/promoted/retired); `{% if supports_reflection_text_edit %}` around the Edit button. Use the real `caps` accessor confirmed in Task 0; reuse project-native CSS classes (guardrail [[bootstrap-brutalist-classes]]).

- [ ] **Step 1:** Failing tests — agentcore-backend-stub: panel lists via `reflection_list`; drawer renders WITHOUT the provenance section, WITHOUT Confirm/Edit; status filter shows active/promoted/retired; promote/retire call the backend; sentinel local `reflections` row never renders. Sqlite-caps template test: provenance + Confirm + Edit + the four sqlite statuses ALL present (pins no sqlite regression). Dead-content-table trick as in Task 5.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Rewire routes + gate templates.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass; sqlite UI suite green.
- [ ] **Step 5:** Commit `feat(ui): route reflections through StorageBackend + capability-gate provenance/confirm/edit`.

*Sqlite preservation:* sqlite backend methods forward to the same `queries`/`ReflectionService`; the gated blocks all render (flags True) → byte-identical HTML, pinned by the sqlite-caps template test. Confirm/edit routes are literally unchanged. Confidence 88 (lowest task) because template gating + the real `caps` accessor name is the integration-risk point; Step 1 asserts BOTH the sqlite-present and agentcore-absent renders, and Task 0 pinned the accessor.

---

### Task 7: Reflection detail 404 + error-card parity sweep (confidence 93%)

**Files:**
- Modify: `better_memory/ui/app.py` (confirm the `reflection_get`-based existence checks in promote/retire match the old `reflection_detail is None → 404`)
- Test: `tests/ui/test_reflection_routes.py` (404 + 409 paths on both backends)

- [ ] **Step 1:** Failing/again-green tests — promote/retire on a missing id → 404 (both modes); a lifecycle-blocked promote/retire (agentcore RuntimeError / sqlite ValueError) → the same error-card status the route returned before (map agentcore RuntimeError to the same 409 card shape as sqlite ValueError, or document the status intentionally).
- [ ] **Step 2:** Run — confirm behaviour.
- [ ] **Step 3:** Adjust the except-clauses so both backends' failure modes render the existing card contract.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass.
- [ ] **Step 5:** Commit `fix(ui): uniform 404/error-card handling for backend-routed reflection actions`.

*Sqlite preservation:* sqlite still raises ValueError → same 409 card; only the agentcore RuntimeError branch is added.

---

### Task 8: Docs, website, pyright, full suite (confidence 95%)

- [ ] **Step 1:** Update `website/agentcore-setup.md` capability table (Reflections: list/detail-without-provenance/promote/retire now backend-routed; provenance/confirm/inline-edit hidden in agentcore; Semantic: full CRUD backend-routed), `website/architecture.md` (drop/adjust any "UI opens local sqlite for content" prose — now backend-routed for these surfaces), `docs/agentcore-ui/agentcore-mapping.md` if any EASY item's "only wiring" note is now DONE. Grep synonyms: `raw SQL`, `bypasses StorageBackend`, `SemanticMemoryService`, `reflection_detail`. Note "docs unaffected" for `README.md`/`mcp-tools.md` (no MCP tool or env change) explicitly. (Guardrail [[keep-docs-in-sync]].)
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q` full run; fix stragglers minimally.
- [ ] **Step 4:** Commit `docs: agentcore UI content-wiring capability + data-flow updates`.

---

### Task 9: PR, babysit, merge (confidence 95%)

- [ ] Push; `gh pr create` — body: spec link; the two rewired surfaces; the three capability flags; OD-1 (text-edit default-off) called out for the user; sqlite-unchanged evidence; and a **manual live-smoke checklist** (one real `BETTER_MEMORY_STORAGE_BACKEND=agentcore` UI session: list reflections, open a drawer — no provenance/confirm/edit, promote + retire one, create/edit/scope/delete a semantic memory). Footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] Babysit to green + zero threads → squash-merge, checkout main, pull.
- [ ] Deploy note: no env changes; live smoke is manual (needs AWS creds) — present the checklist, don't run it unprompted.

---

## Self-review notes

- Spec §1 → T2/T3/T4 (the three READ accessors); §2 → T1 (flags); §3 → T6/T7; §4 → T5; §5 (sqlite-unchanged) pinned in every task's preservation note + the sqlite-caps template test in T6.
- Write methods (promote/retire, semantic CRUD) reused as-is — no new agentcore write code; verified wrappers at `sqlite.py:169-204,345-349` and `agentcore.py:1199-1476,1578-1591`.
- Every new method signature (T1–T4) is stated once and referenced identically in protocol/sqlite/agentcore/route consumers.
- Dependency risk isolated in Task 0 (spike-and-escalate, not silent rebuild).
- Lowest-confidence task (T6, 88%) is the template-gating integration; mitigated by Task 0 pinning the real `caps` accessor and T6 Step 1 asserting BOTH sqlite-present and agentcore-absent renders.
- Open decision OD-1 (agentcore text-edit) is defaulted OFF and surfaced in the PR body, not silently resolved.
- Doc-sync task (T8) covers `website/agentcore-setup.md` + `architecture.md`; README/mcp-tools explicitly noted unaffected (no MCP/env change).
