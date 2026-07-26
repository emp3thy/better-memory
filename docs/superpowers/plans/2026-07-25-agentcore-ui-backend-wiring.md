# Core-infra: thread StorageBackend into the UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `create_app` builds a `StorageBackend` via `factory.build_backend` and routes the CONTENT operations that map 1:1 to an existing protocol method (semantic CRUD, reflection promote/retire) through it; operational-state reads/writes stay on the local `memory.db` connection; capability flags are added as infra for the two dependent tasks, with the existing `supports_episodes` gate implemented as the reference. Foundation for the two dependent agentcore-UI tasks.

**Architecture:** Keep `app.extensions['db_connection']` (now operational-state only) AND add `app.extensions['backend']` built from the same conn + sync_embedder. `SqliteBackend` delegates to the exact services/queries the UI already calls, so sqlite mode is byte-identical. HARD content operations with no protocol method stay on their raw path as named seams for the dependent tasks. Capability flags gain observations/provenance/retention/reflection-mutation properties (sqlite=True, agentcore=False), exposed to templates via a context processor; the Episodes nav gate is the reference consumer.

**Tech Stack:** Python 3.12, Flask, sqlite, boto3 (stubbed via MagicMock in tests — established `tests/storage/test_agentcore_unit.py` pattern), pytest, pyright.

**Spec:** `docs/superpowers/specs/2026-07-25-agentcore-ui-backend-wiring-design.md`

## GUARDRAILS (surfaced from planning + implementation memory — read first)

High-confidence reflections that govern this work. Task references use `[[slug]]`.

- **[[keep-docs-in-sync]]** (implementation, confidence 0.95, evidence 7, useful 19) — *Keep website and README in sync with every code change.* This task edits `better_memory/ui/app.py`, `storage/protocol.py`, and adds capability flags. Docs affected: `website/architecture.md` (storage/UI prose — the UI now reaches content through `StorageBackend`) and the protocol docstrings for the new flags. README config/mcp-tools tables and `website/configuration.md` are **unaffected** (no new env var, no MCP tool change) — state "docs unaffected" for those explicitly in the PR (Task 6). When rewriting a doc line, verify every factual token against source; do not carry stale tokens forward.
- **[[surface-planning-memory]]** (planning, confidence 0.9) — planning memory + knowledge standards must be surfaced at the TOP of the plan (done here). Applies to the plan author, satisfied.
- **[[server-boot-real-call]]** (implementation, confidence 0.65) — *A "constructs without throwing" test is insufficient to prove wiring.* For Task 1's agentcore-mode test, drive an actual content route through the stubbed backend and assert the stub is called — do not merely assert `create_app` returns an app. (The dead-host variant is overkill here since the backend is stubbed, but the "exercise a real call" principle holds.)
- **[[brutalist-css-classes]]** (implementation, confidence 0.75) — Bootstrap utility classes are unavailable in this UI; use project-native classes (`app.css`). Relevant to Task 5's Episodes-gate template edit — the gate wraps an existing `.rail-link`; do not introduce new utility classes.
- **[[playwright-domtext]]** (implementation, confidence 0.8) — Playwright/`has_text` matches DOM textContent, not CSS-rendered text. Relevant only if a browser test asserts the hidden/shown Episodes tab; assert on presence/absence of the nav element, not CSS visibility.

Considered and dismissed: `[[sqlite-isolation-rollback]]`, `[[sqlite-datetime-compare]]`, `[[frozen-dataclass-nullable]]`, `[[tempfile-fd-leak]]` — this task adds no new DML, no timestamp comparisons, no new read-model dataclasses, no temp-file handling. `[[transcribe-spec-test-cases]]` — honoured: every enumerated test below is written as actual test code, not paraphrased.

## Global Constraints

- Branch `feat/agentcore-ui-backend-wiring` off `main` at task start.
- Test command `./.venv/Scripts/python.exe -m pytest <path> -v`; pyright 0 errors; NO live AWS (MagicMock clients / stubbed `build_backend` only).
- **Sqlite behaviour byte-identical throughout.** Every routed operation delegates to the same service the UI calls today; the existing `tests/ui/*` suites are the pins and must stay green with zero edits to their assertions (fixture/import edits only if unavoidable).
- Operational-state routes (Diagnostics hook-errors/retention, exposure/rating evidence, `audit_log`) keep using `app.extensions['db_connection']` verbatim — do not route them through the backend.
- Content operations with no protocol method (reflection confirm/edit-text, promote-observation-to-semantic, semantic drawer row, reflection list/detail, observation/episode reads, Diagnostics content joins) stay on their current path — they are the dependent tasks' seams. Do NOT invent backend methods for them here.
- ASCII only; ruff line length 100; stage exact paths; one commit per task with footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- No new env keys.

## Verified-against-source facts (do not re-derive)

| Fact | Where |
|---|---|
| `create_app` opens `connect(resolve_home()/'memory.db')`, stores at `app.extensions['db_connection']`, builds `resolved_sync_embedder`, and never imports the factory/backend | `ui/app.py:80-98` |
| `build_backend(*, config, memory_conn, embedder=None, sync_embedder=None, session_id, project)` → Sqlite/AgentCore; agentcore threads `local_conn=memory_conn`, requires boto3 + `agentcore.json` | `storage/factory.py:32-111` |
| `SqliteBackend.semantic_list` default `track_exposure=True` → `SemanticMemoryService.list_for_project` (same default, `services/semantic.py:220-226`); `semantic_observe/update_text/set_scope/delete`, `promote_reflection`, `retire_reflection` are one-line service delegates | `storage/sqlite.py:169-204,345-349` |
| UI semantic write/panel routes build a per-request `SemanticMemoryService(conn, sync_embedder=app.extensions['sync_embedder'])`; reflection promote/retire call `app.extensions['reflection_service']` | `ui/app.py:462-485,495-604,651-675` |
| Capability flags `supports_synthesis`/`supports_episodes` exist on protocol; `AgentCoreBackend.supports_episodes=False`; NO UI code references any flag (Episodes gate unimplemented) | `storage/protocol.py:52-64`; `data-sources.md` UI-infra row |
| UI test harness: `create_app(start_watchdog=False, db_path=tmp_db)`, `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite` pin, `project_name` monkeypatched per test | `tests/ui/conftest.py`; `tests/ui/test_semantic.py` |
| Operational-state routes already correct on raw conn (hook-error delete/purge, diagnostics counters, rating evidence) | `ui/app.py:677-764` |
| Content ops with NO protocol method (HARD): reflection list/detail, confirm, edit-text, `create_from_observation`, single-semantic get, observation/episode reads, diagnostics content joins | `agentcore-mapping.md` HARD table |

---

### Task 1: Build the backend in `create_app`; keep the raw conn for operational state

**Files:**
- Modify: `better_memory/ui/app.py` (imports; `create_app` body ~:80-98)
- Test: `tests/ui/test_app.py` (extend)

**Interfaces:**
- Produces: after existing `db_conn` / `resolved_sync_embedder` setup, `create_app` calls
  ```python
  from better_memory.storage.factory import build_backend
  backend = build_backend(
      config=get_config(),
      memory_conn=db_conn,
      sync_embedder=resolved_sync_embedder,
      session_id=None,
      project=project_name(),
  )
  app.extensions["backend"] = backend
  ```
  `app.extensions['db_connection']` and the existing `episode_service`/`reflection_service`/`sync_embedder`/`db_path` extensions are UNCHANGED. No route bodies change in this task.
- Consumed by Tasks 2–5.

- [ ] **Step 1: Write failing tests** — (a) `test_create_app_builds_backend`: build via the `client` fixture's app (`create_app(start_watchdog=False, db_path=tmp_db)` with `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite`), assert `app.extensions['backend']` is present and `isinstance(..., SqliteBackend)`, and `app.extensions['db_connection']` is still the open conn. (b) `test_backend_shares_the_ui_connection`: assert the backend's `_conn` is the same object as `app.extensions['db_connection']` (sqlite path shares the conn — no second content store). (c) `test_agentcore_mode_builds_stubbed_backend`: monkeypatch `better_memory.ui.app.build_backend` to a stub returning a MagicMock backend; build the app with a config whose `storage_backend='agentcore'` (monkeypatch `get_config`); assert `build_backend` was called once with `memory_conn=<the ui conn>`, `session_id=None`, `sync_embedder=<resolved>`, and `project=<project_name()>`, and that `app.extensions['backend']` is the stub. Per `[[server-boot-real-call]]` the deeper assertion (a real route reaching the stub) lands in Tasks 2–5; here assert the constructor wiring.
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_app.py -v` — FAIL (no `backend` extension / `build_backend` not imported).
- [ ] **Step 3:** Implement per Interfaces. Import `build_backend` at module top (alongside the existing `get_config`, `project_name`, `resolve_home` imports). Do not remove any existing extension.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — all pass (every existing route test still green; backend built but unused so far).
- [ ] **Step 5:** Commit `feat(ui): build StorageBackend in create_app (operational conn retained)`.

---

### Task 2: Route the semantic list panel through `backend.semantic_list`

**Files:**
- Modify: `better_memory/ui/app.py` (`semantic_panel` ~:495-512)
- Test: `tests/ui/test_semantic.py` (extend)

**Interfaces:**
- Produces: `semantic_panel` resolves `project`/`scope_filter`/`search` exactly as today, then calls `app.extensions['backend'].semantic_list(project=project, scope_filter=scope_filter, search=search)` (default `track_exposure=True`) instead of constructing a `SemanticMemoryService`. Template render unchanged (`rows=rows, project=project`). The per-request service construction and its import are removed from this route.
- Preservation: `backend.semantic_list` → `SemanticMemoryService.list_for_project` with the identical default `track_exposure=True`, so ranking, scope semantics, search escaping, AND the exposure/`rating_diagnostics` side-effects are byte-identical (verified `sqlite.py:182-195`, `semantic.py:220-226`).

- [ ] **Step 1: Write failing test** — `test_panel_routes_through_backend`: monkeypatch `app.extensions['backend'].semantic_list` (or spy via `unittest.mock.patch.object` on the built backend) to assert it is called once with the resolved kwargs; keep the existing `test_renders_seeded_rows_newest_first` / `test_includes_general_from_other_projects` as the behaviour pins (they must still pass through the backend path).
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py -v` — the new spy test FAILs (still calls the raw service).
- [ ] **Step 3:** Implement; remove the now-unused local `SemanticMemoryService` import in this route.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py -q` — all pass (pins prove identical rows + ordering).
- [ ] **Step 5:** Commit `refactor(ui): semantic panel reads through StorageBackend`.

---

### Task 3: Route semantic writes through `backend.semantic_*`

**Files:**
- Modify: `better_memory/ui/app.py` (`semantic_create` ~:514, `semantic_scope` ~:533, `semantic_delete` ~:550, `semantic_update` ~:589)
- Test: `tests/ui/test_semantic.py` (extend)

**Interfaces:**
- Produces:
  - `semantic_create` → `backend.semantic_observe(content=content, project=project_name(), scope=scope)`; discard the returned id as today.
  - `semantic_update` → `backend.semantic_update_text(id=id, content=content)`.
  - `semantic_scope` → `backend.semantic_set_scope(id=id, scope=scope)`.
  - `semantic_delete` → `backend.semantic_delete(id=id)` (idempotent).
  All keep their `ValueError`→400 error-card contract and `HX-Trigger: semantic-changed`. The per-request `SemanticMemoryService` construction + imports are removed from all four routes.
- Preservation: each backend method is a one-line delegate to the same service method the routes call today, over the same conn + sync_embedder the backend was built with (`app.extensions['sync_embedder']`), so validation, embedding side-effects, and error text are identical.
- **Out of scope:** `observation_promote_to_semantic` (`create_from_observation`) — no protocol method; stays on `SemanticMemoryService` (dependent-task seam). Do not touch it.

- [ ] **Step 1: Write failing tests** — extend with per-route spy assertions (create/update/scope/delete each call the matching backend method with the resolved kwargs), plus keep the existing success + `ValueError`→400 pins. Include `test_create_invalid_scope_returns_400` and `test_delete_idempotent` if not already present (transcribe, do not paraphrase).
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py -v` — new spy tests FAIL.
- [ ] **Step 3:** Implement all four routes; delete the now-unused per-route `SemanticMemoryService` imports.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_semantic.py -q` — all pass.
- [ ] **Step 5:** Commit `refactor(ui): semantic writes go through StorageBackend`.

---

### Task 4: Route reflection promote/retire through the backend

**Files:**
- Modify: `better_memory/ui/app.py` (`reflection_promote` ~:462, `reflection_retire` ~:391)
- Test: `tests/ui/test_reflections.py` (extend)

**Interfaces:**
- Produces: `reflection_promote` → `backend.promote_reflection(reflection_id=id)`; `reflection_retire` → `backend.retire_reflection(reflection_id=id)`. Both keep the existing existence-precheck (`queries.reflection_detail`, still on the raw conn — a content READ with no backend method yet, dependent-task seam), the `ValueError`→409 card, the drawer re-render, and `HX-Trigger: reflection-changed`.
- Preservation: `promote_reflection`/`retire_reflection` delegate to `ReflectionService.promote_to_general`/`retire` — the same methods `app.extensions['reflection_service']` calls today, raising the same `ValueError` on lifecycle blocks.
- **Out of scope:** `reflection_confirm` and `reflection_edit_save` (`update_text`) — no protocol method; `confirm`'s gate is absent from the agentcore status model. They stay on `app.extensions['reflection_service']` (dependent-task seam). Do not touch them.

- [ ] **Step 1: Write failing tests** — `test_promote_routes_through_backend` / `test_retire_routes_through_backend` (spy the backend method); keep the existing promote/retire success (200 + `HX-Trigger`) and lifecycle-block (409) pins.
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -v` — spy tests FAIL.
- [ ] **Step 3:** Implement the two routes only. Leave `confirm`/`edit_save` untouched.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_reflections.py -q` — all pass.
- [ ] **Step 5:** Commit `refactor(ui): reflection promote/retire go through StorageBackend`.

---

### Task 5: Capability-flag mechanism + Episodes reference gate

**Files:**
- Modify: `better_memory/storage/protocol.py` (new flag properties + docstrings), `better_memory/storage/sqlite.py` (flags = True), `better_memory/storage/agentcore.py` (flags per agentcore shape), `better_memory/ui/app.py` (context processor), `better_memory/ui/templates/base.html` (Episodes nav gate)
- Test: `tests/storage/test_sqlite_backend.py` + `tests/storage/test_agentcore_unit.py` (flag values), `tests/ui/test_app.py` (context processor + Episodes gate)

**Interfaces:**
- Produces (protocol, exact — read-only properties mirroring `supports_episodes`):
  ```python
  @property
  def supports_observations(self) -> bool: ...
  @property
  def supports_provenance(self) -> bool: ...
  @property
  def supports_retention(self) -> bool: ...
  @property
  def supports_reflection_mutation(self) -> bool: ...
  ```
  `SqliteBackend` returns `True` for all four. `AgentCoreBackend` returns `False` for all four (no observation concept; no provenance joins; pruning = event expiry; reflections born active with AI-managed text).
- Context processor in `create_app`:
  ```python
  @app.context_processor
  def _inject_capabilities() -> dict[str, object]:
      b = app.extensions["backend"]
      return {"caps": {
          "episodes": b.supports_episodes,
          "observations": b.supports_observations,
          "provenance": b.supports_provenance,
          "retention": b.supports_retention,
          "reflection_mutation": b.supports_reflection_mutation,
      }}
  ```
- `base.html`: wrap the Episodes `.rail-link` in `{% if caps.episodes %}...{% endif %}` (project-native class per `[[brutalist-css-classes]]`; no new utility classes). Missing key ⇒ treat as shown (default-visible) so sqlite can never accidentally hide a tab.
- **Scope boundary:** this task ships ONLY the flags, the context processor, and the Episodes gate. Hiding the Observations tab, provenance sections, retention panel, and reflection mutation actions is the dependent tasks' work using these flags. Do not gate those here.
- Preservation: `SqliteBackend` returns `True` for every flag ⇒ `caps.episodes` is True in sqlite mode ⇒ the Episodes tab renders exactly as today; no other template gate is added, so sqlite HTML is unchanged.

- [ ] **Step 1: Write failing tests** — (a) `SqliteBackend` returns True for all four new flags; (b) `AgentCoreBackend` (existing stubbed fixture) returns False for all four; (c) UI: `caps` is injected and the Episodes nav link is present in sqlite mode; (d) UI: with a stub backend reporting `supports_episodes=False` (patch `app.extensions['backend']`), `GET /episodes`-nav rendering omits the Episodes `.rail-link` — assert on element presence/absence, not CSS (`[[playwright-domtext]]`).
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py tests/ui/test_app.py -v` — FAIL (properties/`caps` missing).
- [ ] **Step 3:** Implement protocol properties (with docstrings stating sqlite=True/agentcore=False rationale), both backend implementations, the context processor, and the `base.html` Episodes gate.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui -q` — all pass.
- [ ] **Step 5:** Commit `feat(storage,ui): capability flags + Episodes-tab gate mechanism`.

---

### Task 6: Docs, pyright, full suite

**Files:**
- Modify: `website/architecture.md` (storage/UI prose), protocol docstrings (done in Task 5), possibly `docs/agentcore-ui/data-sources.md` note that content ops now route through the backend post-wiring.

- [ ] **Step 1:** Update `website/architecture.md` storage/UI paragraph to state the management UI now reaches memory CONTENT through `StorageBackend` (semantic CRUD + reflection promote/retire), while session-operational state stays on the local `memory.db` — per `[[keep-docs-in-sync]]`, verify every factual token against the code. State explicitly in the PR body that `website/configuration.md`, `website/mcp-tools.md`, and `README.md` tool/env tables are **unaffected** (no new env var, no MCP tool change). Grep synonyms to catch stale prose: `backend-unaware`, `opens the local`, `bypasses StorageBackend`.
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q` → full green. Fix stragglers minimally.
- [ ] **Step 4:** Commit `docs: UI now reaches content through StorageBackend`.

---

### Task 7: PR, babysit, merge

- [ ] Push `feat/agentcore-ui-backend-wiring`; `gh pr create` (body: spec link; the wiring summary — backend built in `create_app`, semantic CRUD + reflection promote/retire routed, operational-state retained on the local conn, capability flags + Episodes gate; the named seams left for the two dependent tasks; sqlite byte-identical proof = existing `tests/ui/*` green; "docs unaffected" note for config/mcp-tools/README tables; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`).
- [ ] Babysit bots to green + zero unresolved threads → squash-merge, checkout main, pull.
- [ ] Memory sweep before finishing (CLAUDE.md phase trigger): record any non-obvious fix/gap from review as a `failure` observation; record the content-vs-operational split decision as a `success`/`decision` observation if not already captured.

---

## Self-review notes

- Design §1→T1, §2→T2/T3/T4, §4→T5, non-goals honoured (no HARD content op served, no dropdown rework, no extra tab gating, no new env/tool).
- Sqlite-preservation named in every routing task: each backend method is a one-line delegate to the service the UI already calls (`sqlite.py` line refs in the facts table); existing `tests/ui/*` suites are the pins and their assertions are not edited.
- Every content op with no protocol method is explicitly listed as out-of-scope in the task that would otherwise touch it (T3 promote-observation, T4 confirm/edit-text), preventing accidental scope creep into the dependent tasks.
- Capability flags shipped as infra (T5) with only the pre-existing `supports_episodes` gate implemented, proving the mechanism without pre-empting dependent-task UI decisions (e.g. the genuinely-open reflection edit-text disable).
- `build_backend` kwargs in T1 match the factory signature exactly (`config`, `memory_conn`, `sync_embedder`, `session_id`, `project`); `embedder` omitted (UI has no ObservationService embedder — sqlite FTS via triggers).
- Doc-sync (T6) scoped precisely per `[[keep-docs-in-sync]]`: architecture prose + protocol docstrings changed; config/mcp-tools/README tables explicitly declared unaffected.
