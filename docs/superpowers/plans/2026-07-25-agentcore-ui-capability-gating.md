# AgentCore UI Capability-Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In agentcore mode the UI hides the surfaces AgentCore cannot back and routes the standing content surfaces through `StorageBackend`; the distinct-project dropdown becomes AgentCore-native. sqlite mode is byte-for-byte unchanged.

**Architecture:** Five new `bool` capability flags on `StorageBackend` (all `True` on sqlite, all `False` on agentcore) drive nav/route/section gates via a `caps` Jinja context processor. `create_app` builds a backend through `storage/factory.build_backend` (same `db_conn` -> `memory_conn`) and routes CONTENT through it (semantic CRUD, reflection promote/retire, reflection list/detail), while operational-state (`hook_errors`, `session_memory_exposure`, `rating_diagnostics`) stays on the raw local conn. A new `distinct_projects` backend method replaces `SELECT DISTINCT project`; agentcore sources it from `ListActors` UNION the `agentcore_migration` ledger namespaces.

**Tech Stack:** Python 3.12, Flask/Jinja, htmx, sqlite, boto3 (stubbed via MagicMock in tests — `tests/storage/test_agentcore_unit.py` pattern), pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-agentcore-ui-capability-gating-design.md`

---

## Guardrails (surfaced from planning memory — read before drafting code)

- **[[G1-planning-memory]]** *(conf 0.9, useful 14)* — this plan itself satisfies the "retrieve planning memory + knowledge_list, surface guardrails at TOP" convention. Standards knowledge scanned: only `standards/ralph-runtime.md` (executor-runtime rules; applies if run under Ralph — feature-branch-at-task-start, per-task confidence scoring).
- **[[G2-docs-in-sync]]** *(conf 0.95, evidence 7)* — **keep website + README in sync with every code change**. New capability flags and the changed dropdown source touch documented behaviour: sweep `website/agentcore-setup.md` (capability table), `website/architecture.md` (UI/storage prose), and `README.md`. Task 9 owns this; note "docs unaffected" explicitly for any file genuinely untouched.
- **[[G4-brutalist-css]]** *(conf 0.75)* — **Bootstrap utility classes are unavailable** in this UI; `app.css` is brutalist. Any markup added/moved in gated fragments (Tasks 3-5) must use project-native classes (`.chip`, `.polarity-badge`, `.rating-badge`, `_rating_stat.html`). Since this PR mostly WRAPS existing markup in `{% if %}`, the risk is low — but verify no new utility class sneaks in.
- **[[G5-playwright-textcontent]]** *(conf 0.8)* — Playwright/text locators match DOM textContent, not CSS-rendered text. Nav-gating tests should assert on link **presence/absence** (element count), not on transformed label case.
- **[[G7-agentcore-boot-test]]** *(conf 0.65)* — an agentcore app-boot test must **drive a real gated request** and make any leaked local-content read fail loudly; "app constructs without throwing" is insufficient (Task 2).
- Dismissed as not-applicable: queue-dispatch boundary (0.9 dont — no Ralph queue filing here), config-merger frozen-registry (no config-file merge), tempfile fd-leak / cp1252 CLI print / Partial<T> (no new CLI prints, temp files, or TS in this plan — Task 9 keeps any prose ASCII per house style regardless).

## Global Constraints

- Branch `feat/agentcore-ui-gating` (create at task start if run under Ralph).
- Test command `./.venv/Scripts/python.exe -m pytest <path> -v`; pyright 0 errors; NO live AWS in tests (MagicMock clients only).
- **sqlite behaviour byte-identical throughout** — every new flag is `True` on sqlite so no gate fires; every backend method wrapping a UI read reproduces the current query verbatim. The existing `tests/ui/*` suite is the preservation pin and must pass unchanged.
- All AWS reads best-effort: reuse the existing `_retry_on_transient_404` / reserved-metadata-strip helpers; agentcore read methods degrade (ledger-only / Wilson-only / empty), never raise into a route.
- ASCII only; ruff line 100; stage exact paths; commit per task with footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- No new env keys.

## Verified-against-source facts (do not re-derive)

| Fact | Where |
|---|---|
| `create_app` opens local `memory.db` via `connect(...)`, stores `db_conn` in `app.extensions["db_connection"]`; never imports factory/StorageBackend; builds `EpisodeService`/`ReflectionService`/per-route `SemanticMemoryService` on the raw conn | `ui/app.py:80-98,368-485,487-604,651-675` |
| Nav is 5 static hardcoded links; no `supports_*`/`storage_backend`/`agentcore` reference anywhere under `better_memory/ui` (grep: zero) | `ui/templates/base.html:90-111`; grep |
| `supports_episodes` / `supports_synthesis` are the only capability properties; UI hides Episodes tab "when False" is intent-only, unimplemented | `storage/protocol.py:52-64`; agentcore returns `supports_episodes=False` |
| `build_backend(*, config, memory_conn, embedder, sync_embedder, session_id, project)`; agentcore branch already threads `memory_conn -> local_conn`; guards boto import for sqlite-only installs | `storage/factory.py:32-111` |
| Semantic CRUD + `promote_reflection` + `retire_reflection` exist on both backends (1:1, EASY); `_get_semantic_record`, `semantic_list`, `retrieve` present on agentcore | `agentcore-mapping.md`; `storage/agentcore.py:221,1193,1246,1578-1591`; `storage/protocol.py:162-183,241-247` |
| Reflections dropdown = `queries.reflection_distinct_projects` unioned with `project_name()` and sorted casefold | `ui/app.py:290-315` |
| Reflection list = `queries.reflection_list_for_ui` (6 filters, order confidence DESC/updated_at DESC/rowid DESC, default status IN pending_review,confirmed); detail = `queries.reflection_detail` (row + provenance join) + `queries.fetch_rating_evidence` (local exposure) | `ui/app.py:317-366`; `data-sources.md` |
| Retention-runs panel route `/diagnostics/panel/retention-runs` reads local `retention_runs`; hook-errors + rating counters are operational/local | `ui/app.py:677-738`; `data-sources.md` |
| `agentcore_migration` ledger has a `namespace TEXT` column (`projects/{project}/...`), no direct `project` column | `storage/agentcore_migrate.py:51-63` |
| Live: `ListActors(memoryId)` enumerates event-derived project actors; `ListEvents` needs actorId+sessionId; no list-namespaces API; agentcore reflection statuses active/promoted/retired (no pending_review/confirm) | design Grounding; verified 2026-07-25 |

---

### Task 1: Capability flags on the Protocol + both backends

**Files:**
- Modify: `better_memory/storage/protocol.py` (5 new properties), `better_memory/storage/sqlite.py` (return `True`), `better_memory/storage/agentcore.py` (return `False`)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`

**Interfaces:**
- Produces on `StorageBackend`: `supports_observations`, `supports_provenance`, `supports_retention_runs`, `supports_reflection_confirm`, `supports_reflection_text_edit` — all `@property -> bool`, docstring naming the single UI surface each gates (mirror `supports_episodes`'s docstring style). SqliteBackend: all `True`. AgentCoreBackend: all `False`. No AWS call in any.

- [ ] **Step 1:** Failing tests — assert each new property is `True` on a SqliteBackend fixture and `False` on the stubbed AgentCoreBackend fixture; assert `supports_episodes` values are unchanged (regression pin).
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py -v` — FAIL (attributes missing).
- [ ] **Step 3:** Implement the properties.
- [ ] **Step 4:** Run the two files — pass.
- [ ] **Step 5:** Commit `feat(storage): capability flags for UI surface gating`.

**Sqlite preservation:** additive properties only; no existing method or query touched. sqlite returns `True` everywhere, so downstream gates never fire.

---

### Task 2: `create_app` builds a backend + injects `caps` (foundation)

**Files:**
- Modify: `better_memory/ui/app.py` (build backend via factory; `app.extensions["backend"]`; `caps` context processor)
- Test: `tests/ui/test_app_backend_wiring.py` (new)

**Interfaces:**
- Produces: `app.extensions["backend"]` = `build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`. A `@app.context_processor` returns `{"caps": <object exposing the six supports_* flags read off the backend>}` so every render sees `caps`. The raw `db_conn` remains in `app.extensions["db_connection"]` for operational-state reads/writes (unchanged). No route behaviour changes yet (content routing lands in Tasks 6-8); this task only makes the backend + caps available.
- Consumed by: Tasks 3-8.

- [ ] **Step 1:** Failing tests — (a) sqlite-mode app exposes `app.extensions["backend"]` and a rendered page's context has `caps.supports_observations is True`; (b) **agentcore app-boot (G7)**: monkeypatch `get_config().storage_backend='agentcore'` with stubbed boto clients + a real tmp `memory.db`, assert the app builds AND a GET `/reflections` renders with `caps.supports_observations is False` (drive a real request, not just construction).
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_app_backend_wiring.py -v` — FAIL.
- [ ] **Step 3:** Implement. Keep the existing `db_conn` open and stored; build the backend from the same conn; register the context processor. Reuse the factory's boto-import guard — sqlite mode must not require boto3.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — full UI suite passes (no route change yet).
- [ ] **Step 5:** Commit `feat(ui): build StorageBackend in create_app + caps context processor`.

**Sqlite preservation:** the raw conn and every existing route are untouched; `caps` is all-`True`; the existing UI suite passes unchanged.

---

### Task 3: Gate the nav + Episodes/Observations routes

**Files:**
- Modify: `better_memory/ui/templates/base.html` (wrap Episodes + Observations links), `better_memory/ui/app.py` (guard `/episodes*` and `/observations*` routes)
- Test: `tests/ui/test_nav_gating.py` (new)

**Interfaces:**
- Produces: `base.html` wraps the Episodes link in `{% if caps.supports_episodes %}` and the Observations link in `{% if caps.supports_observations %}`; the other three links always render. Each `/episodes...` route body starts with `if not caps... abort(404)` (read caps off `app.extensions["backend"]`); same for `/observations...`. sqlite: flags `True`, guards inert, all five links shown.

- [ ] **Step 1:** Failing tests — sqlite render of any page shows all 5 rail links (assert element **count/presence**, not label text — G5); agentcore-stubbed render shows 3 links (no Episodes, no Observations); GET `/observations` and `/episodes` return 404 in agentcore, 200 in sqlite.
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/ui/test_nav_gating.py -v` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass.
- [ ] **Step 5:** Commit `feat(ui): gate Episodes + Observations nav and routes by capability`.

**Sqlite preservation:** flags `True` -> every link and route reachable exactly as today.

---

### Task 4: Gate provenance sections + provenance data-fetch

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html` (wrap source-observation/episode provenance block), `better_memory/ui/templates/fragments/observation_drawer.html` (wrap linked-reflections block), `better_memory/ui/app.py` (reflection drawer routes fetch row-only when `not caps.supports_provenance`)
- Test: `tests/ui/test_provenance_gating.py` (new)

**Interfaces:**
- Produces: both drawers wrap their provenance sections in `{% if caps.supports_provenance %}`. The reflection drawer routes (`/reflections/<id>/drawer` and every route that re-renders it) fetch provenance-bearing detail via `queries.reflection_detail` only when `caps.supports_provenance`; otherwise via the row-only backend read (Task 7's `get_reflection_for_ui`). sqlite: flag `True`, provenance rendered and fetched exactly as today.

- [ ] **Step 1:** Failing tests — sqlite reflection drawer HTML contains the provenance section; agentcore-stubbed drawer omits it and issues no provenance join (assert the join query is not called / row-only path taken); observation drawer linked-reflections gated likewise (sqlite only, tab hidden in agentcore but assert the template conditional).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement the template conditionals; make the drawer fetch branch on the flag. (Depends on Task 7 for the agentcore row-only read; if Task 7 not yet merged, branch to a stub that Task 7 fills — sequence 7 before this if executing strictly, or land the template conditional here and the fetch-branch when 7 lands.)
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass.
- [ ] **Step 5:** Commit `feat(ui): gate reflection/observation provenance sections + fetch`.

**Sqlite preservation:** flag `True` keeps both the render and the `queries.reflection_detail` provenance fetch identical to today.

---

### Task 5: Gate retention-runs panel + Confirm action + inline text-edit

**Files:**
- Modify: `better_memory/ui/templates/diagnostics.html` (wrap retention-runs panel mount), `better_memory/ui/templates/fragments/reflection_drawer.html` (wrap Confirm button + Edit button), `better_memory/ui/app.py` (guard `/diagnostics/panel/retention-runs`, `/reflections/<id>/confirm`, `/reflections/<id>/edit`, `/reflections/<id>/edit` POST)
- Test: `tests/ui/test_diagnostics_reflection_gating.py` (new)

**Interfaces:**
- Produces: the retention-runs panel include + its `hx-get` mount are wrapped in `{% if caps.supports_retention_runs %}`, and the route `abort(404)`s when off. The Confirm button is wrapped in `{% if caps.supports_reflection_confirm %}` and `/confirm` guards. The inline Edit button/form are wrapped in `{% if caps.supports_reflection_text_edit %}` and both `/edit` routes guard. Hook-errors panel + rating-counter dl on Diagnostics remain ungated (operational/local, visible in both modes). sqlite: all flags `True`, everything visible and reachable as today.

- [ ] **Step 1:** Failing tests — agentcore Diagnostics HTML omits the retention-runs panel and GET `/diagnostics/panel/retention-runs` is 404, while hook-errors panel + rating counters still render; agentcore reflection drawer omits Confirm + Edit controls and POST `/reflections/<id>/confirm`, GET+POST `/reflections/<id>/edit` are 404; all present/200 in sqlite.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement (resolve the text-edit open decision as DISABLE on agentcore, per design §Open decisions — driven entirely by the flag).
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass.
- [ ] **Step 5:** Commit `feat(ui): gate retention-runs panel, confirm, inline text-edit by capability`.

**Sqlite preservation:** flags `True` -> retention panel, Confirm, and Edit all present and functional exactly as today.

---

### Task 6: Route semantic CRUD + reflection promote/retire through the backend

**Files:**
- Modify: `better_memory/ui/app.py` (`/semantic*` routes -> `backend.semantic_*`; `/reflections/<id>/promote` -> `backend.promote_reflection`; `/reflections/<id>/retire` -> `backend.retire_reflection`; semantic drawer row -> `backend`-appropriate get)
- Test: `tests/ui/test_semantic_reflection_backend_routing.py` (new); existing `tests/ui` semantic/reflection tests are the preservation pins

**Interfaces:**
- Produces: the visible content mutations/reads on the standing agentcore tabs go through `app.extensions["backend"]` instead of a raw-conn `SemanticMemoryService` / `ReflectionService`. SqliteBackend's `semantic_*` / `promote_reflection` / `retire_reflection` reproduce the current service behaviour (same validation, same `HX-Trigger`s, same error cards), so sqlite output is unchanged. Rating-evidence receipts on the drawers keep reading local `session_memory_exposure` via `queries.fetch_rating_evidence` (operational, unrouted).

- [ ] **Step 1:** Failing tests — assert the semantic create/update/scope/delete/list and reflection promote/retire routes call the backend (spy on `app.extensions["backend"]`) and still emit the same `HX-Trigger` and status codes; agentcore-stubbed variant asserts the backend method is invoked (not the raw service).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Preserve the exact route contracts (status codes, `HX-Trigger` strings, error-card HTML). Keep `queries.fetch_rating_evidence` on the raw conn.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/ui -q` — existing semantic/reflection UI tests pass unchanged.
- [ ] **Step 5:** Commit `feat(ui): route semantic CRUD + reflection promote/retire through StorageBackend`.

**Sqlite preservation:** SqliteBackend methods wrap the same service/SQL; existing UI suite pins identical behaviour, `HX-Trigger`s, and error handling.

---

### Task 7: Route Reflections list + detail through the backend

**Files:**
- Modify: `better_memory/storage/protocol.py` (`list_reflections_for_ui`, `get_reflection_for_ui`), `better_memory/storage/sqlite.py` (delegate to existing queries), `better_memory/storage/agentcore.py` (flatten `retrieve` / parse record), `better_memory/ui/app.py` (`/reflections/panel` + drawer routes call the backend)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`, `tests/ui/test_reflections_backend_routing.py` (new)

**Interfaces:**
- Produces (exact signatures):

```python
def list_reflections_for_ui(self, *, project: str, tech: str | None, phase: str | None,
                            polarity: str | None, status: str | None,
                            min_confidence: float, useful_only: bool) -> list[Any]
def get_reflection_for_ui(self, *, reflection_id: str) -> dict[str, Any] | None
```

SqliteBackend: `list_reflections_for_ui` delegates to `queries.reflection_list_for_ui` (identical rows/order/filters); `get_reflection_for_ui` delegates to `queries.reflection_detail` (row + provenance). AgentCoreBackend: `list_reflections_for_ui` flattens the existing bucketed `retrieve(project,tech,phase,polarity)` (already Wilson-ordered) and post-filters `min_confidence`/`useful_only` (`status` inert — all `active`); `get_reflection_for_ui` returns the parsed record row WITHOUT provenance. The panel + drawer routes call these methods; the drawer's provenance render/fetch is already gated by Task 4.

- [ ] **Step 1:** Failing tests — sqlite: `list_reflections_for_ui` returns the SAME rows/order as `queries.reflection_list_for_ui` for a seeded db across the filter matrix (identity pin); `get_reflection_for_ui` matches `queries.reflection_detail`. agentcore (stubbed): list flattens buckets and honours `min_confidence`/`useful_only`; detail returns row-only. UI: `/reflections/panel` and `/reflections/<id>/drawer` render via the backend.
- [ ] **Step 2:** Run `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui/test_reflections_backend_routing.py -v` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui -q` — pass.
- [ ] **Step 5:** Commit `feat(ui): route Reflections list + detail through StorageBackend`.

**Sqlite preservation:** SqliteBackend methods are thin pass-throughs to the exact existing queries; the identity tests + existing UI reflection tests pin unchanged output.

---

### Task 8: `distinct_projects` backend method + dropdown replacement

**Files:**
- Modify: `better_memory/storage/protocol.py` (`distinct_projects`), `better_memory/storage/sqlite.py` (SELECT DISTINCT), `better_memory/storage/agentcore.py` (`ListActors` UNION ledger namespaces), `better_memory/ui/app.py` (`/reflections` route uses `backend.distinct_projects()`)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`, `tests/ui/test_reflections_dropdown.py` (new)

**Interfaces:**
- Produces: `distinct_projects(self) -> list[str]`. SqliteBackend: `SELECT DISTINCT project FROM reflections` (identical result to today's `queries.reflection_distinct_projects`). AgentCoreBackend: `ListActors(memoryId)` actor ids UNION the project set parsed from the local `agentcore_migration.namespace` column (`projects/{p}/...` -> `{p}`); best-effort — AWS error degrades to ledger-only, empty+error -> `[]`. The `/reflections` route keeps `sorted({project_name(), *backend.distinct_projects()}, key=casefold)`; only the data source changes. Observations dropdown untouched.

- [ ] **Step 1:** Failing tests — sqlite: `distinct_projects` equals the current `reflection_distinct_projects` output for a seeded db (identity). agentcore (stubbed): `ListActors` returns `["a","b"]`, ledger has namespace `projects/c/reflections/` -> result superset `{a,b,c}`; `ListActors` raising -> ledger-only `{c}`; both empty -> `[]`. UI: `/reflections` dropdown includes `project_name()` + backend projects, sorted casefold.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement (parse the ledger namespace with the `projects/{p}/` prefix rule; guard the `ListActors` call best-effort).
- [ ] **Step 4:** Run `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui -q` — pass.
- [ ] **Step 5:** Commit `feat(ui): distinct_projects via ListActors UNION migration ledger`.

**Sqlite preservation:** SqliteBackend's method returns the identical DISTINCT set; the route's union+sort is unchanged, so the sqlite dropdown is byte-identical.

---

### Task 9: Docs, website, README, pyright, full suite

- [ ] **Step 1:** Update `website/agentcore-setup.md` capability table (UI surfaces hidden in agentcore: Episodes, Observations, provenance, retention-runs panel, Confirm, inline text-edit; dropdown now ListActors+ledger); `website/architecture.md` UI/storage prose (create_app now builds a backend; operational-state stays local); `README.md` if it enumerates UI tabs/capabilities. Grep synonyms: `Observations tab`, `retention runs`, `Confirm`, `SELECT DISTINCT`, `supports_episodes`. State "docs unaffected" for any file genuinely untouched (G2).
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` -> 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q` -> green; fix stragglers minimally; re-run.
- [ ] **Step 4:** Commit `docs: agentcore UI capability-gating capability tables + prose`.

---

### Task 10: PR, babysit, merge

- [ ] Push; `gh pr create` (body: spec link; the four moves — flags, gating, backend-routing, dropdown; sqlite-unchanged proof = existing UI suite green; honest boundaries — observation reads / Diagnostics content aggregates deferred; and a **manual live-smoke checklist**: `BETTER_MEMORY_STORAGE_BACKEND=agentcore` session -> nav hides Episodes+Observations, Reflections/Semantic show real records, dropdown lists real projects, retention panel/Confirm/inline-edit absent; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`).
- [ ] Babysit to green + zero threads -> squash-merge, checkout main, pull.
- [ ] **Memory sweep before finishing** (CLAUDE.md mandatory trigger): record any review-driven fix or non-obvious agentcore gotcha (e.g. namespace-parse edge cases, ListActors degradation) as a `failure`/`neutral` observation with `component=agentcore`/`ui`, `theme=bug`/`gotcha`.
- [ ] Deploy note: no env changes; restart picks up UI code; live smoke is manual (needs AWS creds) — present the checklist, do not run unprompted.

---

## Self-review notes

- Spec §1 -> T1; §2 -> T2 + T6/T7/T8 routing; §3 gating -> T3/T4/T5; §4 read surface -> T7; §5 dropdown -> T8; open decision (text-edit disable) -> T5; boundaries honoured (observation reads, Diagnostics aggregates deferred).
- Every task carries an explicit sqlite-preservation clause; the existing `tests/ui/*` suite is the standing pin (flags all `True`, no gate fires, backend methods wrap identical queries).
- T4 depends on T7's agentcore row-only read — sequence T7 before T4's fetch-branch step, or land T4's template conditional first and the fetch-branch when T7 merges (noted in T4 Step 3).
- New backend surface is minimal and each method has an existing primitive: `supports_*` (properties), `distinct_projects` (`ListActors`+ledger), `list_reflections_for_ui` (`retrieve`), `get_reflection_for_ui` (record parse). Semantic CRUD + promote/retire reuse existing methods (EASY per mapping).
- Doc-sync (G2) is a dedicated task, not an afterthought; brutalist-CSS (G4) and Playwright-textContent (G5) guardrails cited at the gating tasks that touch templates/tests.
