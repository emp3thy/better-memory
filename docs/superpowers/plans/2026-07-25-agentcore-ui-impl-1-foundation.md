# Phase F — PR 1: Foundation (flags + backend in create_app + caps)

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this phase task-by-task. Each task is one commit. Steps follow strict TDD: write failing test -> run (FAIL) -> minimal impl -> run (PASS) -> commit.

**Depends on:** None. Lands and merges first.

**Goal:** Add the five new capability flags to the `StorageBackend` protocol and both
backends; build a `StorageBackend` inside `create_app` via `factory.build_backend` while
retaining `app.extensions["db_connection"]` for operational state; expose the six caps to
templates via a `@app.context_processor`; and prove the mechanism end-to-end by gating ONLY
the Episodes nav link on `caps.supports_episodes`. No content route is re-routed through the
backend in this PR — that is the Content phase. This PR is pure foundation.

**Spec:** `docs/superpowers/specs/2026-07-25-agentcore-ui-design.md`
**Reconciliation + dependency graph:** `docs/superpowers/plans/2026-07-25-agentcore-ui-MASTER-plan.md`
**Sub-plan mined (Tasks 1, 5), flag names TRANSLATED to canonical:** `docs/superpowers/plans/2026-07-25-agentcore-ui-backend-wiring.md`

---

## Phase F guardrails

Read these before writing any code. Task references use `[[slug]]`.

- **[[keep-docs-in-sync]]** (0.95) — Task F4 updates `website/architecture.md` +
  `website/agentcore-setup.md` + the protocol docstrings for the five new flags.
  `README.md`, `website/mcp-tools.md`, and `website/configuration.md` tables are
  **UNAFFECTED** (no new env key, no MCP tool change) — state that explicitly in the PR body.
- **[[server-boot-real-call]]** (0.65) — The agentcore-mode `create_app` test must drive a
  REAL route (`GET /episodes`) through the stubbed backend and assert the six caps were read
  FROM the backend during the render (PropertyMock `.assert_called()`), proving no leaked
  local-content read. "Constructs without throwing" is insufficient.
- **[[guard-needs-triggering-test]]** (0.8) — The one edge-guard in this phase is the Episodes
  gate's hidden branch. Its test MUST seed `supports_episodes=False` to actually trigger the
  `{% if %}` false path (the namespace-parse / AWS-degrade / scope_filter fan-out guards named
  at phase level belong to the Content phase, not here).
- **[[brutalist-css-classes]]** (0.75) — The Episodes gate wraps the existing `.rail-link`
  markup in `base.html`. No Bootstrap utility classes; the block is pure Jinja around
  project-native markup.
- **[[playwright-domtext]]** (0.8) — Nav/gating tests assert presence/absence of the nav
  element markup (`<span class="rail-label">Episodes</span>`), never CSS-rendered text or
  visibility.

---

## Canonical contract (use these EXACT names — do NOT emit the superseded `*_for_ui` /
`supports_reflection_confirm` / `supports_retention` / `supports_reflection_mutation` variants)

Capability flags (`StorageBackend` `@property -> bool`; Sqlite all `True`, AgentCore all `False`):

| flag | status |
|---|---|
| `supports_episodes` | EXISTS today — only consume, do not redefine |
| `supports_observations` | NEW (this phase) |
| `supports_provenance` | NEW (this phase) |
| `supports_retention_runs` | NEW (this phase) |
| `supports_reflection_review` | NEW (this phase) |
| `supports_reflection_text_edit` | NEW (this phase) |

Test command: `./.venv/Scripts/python.exe -m pytest <path> -v`. Python 3.12, Flask/Jinja/htmx,
sqlite, boto3 stubbed via MagicMock. pyright 0 errors. ASCII only; ruff line length 100.
One commit per task, footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
No new env keys.

**Sqlite preservation invariant (whole phase):** no SqliteBackend content method is added or
changed; the five new flags are read-only properties returning `True`; the backend is built
in `create_app` but NO content route is re-routed through it in this PR. Every existing
`tests/ui/*` and `tests/storage/*` assertion stays byte-identical (only additive tests).

---

### Task F1: Add the five new capability flags to protocol + both backends

**Files:**
- Modify: `better_memory/storage/protocol.py` (five new `@property` stubs + docstrings, after
  the existing `supports_episodes` block, `protocol.py:57-64`)
- Modify: `better_memory/storage/sqlite.py` (five properties = `True`, after the existing
  `supports_episodes` block, `sqlite.py:86-88`)
- Modify: `better_memory/storage/agentcore.py` (five properties = `False`, after the existing
  `supports_episodes` block, `agentcore.py:105-107`)
- Test: `tests/storage/test_sqlite_backend.py` (extend), `tests/storage/test_agentcore_unit.py`
  (extend)

**Interfaces:**
- Consumes: existing `supports_episodes` / `supports_synthesis` property style
  (`protocol.py:52-64`), the `backend` fixtures in both storage test modules.
- Produces (protocol, exact — read-only properties mirroring `supports_episodes`):
  ```python
  @property
  def supports_observations(self) -> bool: ...
  @property
  def supports_provenance(self) -> bool: ...
  @property
  def supports_retention_runs(self) -> bool: ...
  @property
  def supports_reflection_review(self) -> bool: ...
  @property
  def supports_reflection_text_edit(self) -> bool: ...
  ```
  `SqliteBackend` returns `True` for all five; `AgentCoreBackend` returns `False` for all
  five. Consumed by Task F2 (context processor) and every later phase's template gates.

**Steps:**

1. **Write failing tests.** Append to `tests/storage/test_sqlite_backend.py`:
   ```python
   def test_new_capability_flags_all_true(backend) -> None:
       """The five UI content-capability flags are all True on sqlite —
       sqlite is the full-feature backend."""
       assert backend.supports_observations is True
       assert backend.supports_provenance is True
       assert backend.supports_retention_runs is True
       assert backend.supports_reflection_review is True
       assert backend.supports_reflection_text_edit is True

   def test_supports_episodes_unchanged_regression(backend) -> None:
       """Regression pin: the pre-existing episodes flag is untouched by the
       flag-addition phase."""
       assert backend.supports_episodes is True
   ```
   Append to `tests/storage/test_agentcore_unit.py`:
   ```python
   def test_new_capability_flags_all_false(backend) -> None:
       """AgentCore exposes only extracted memory records: no raw-observation
       store, no provenance chain, no local retention-run ledger, no
       pending_review lifecycle, no free-text reflection edit."""
       assert backend.supports_observations is False
       assert backend.supports_provenance is False
       assert backend.supports_retention_runs is False
       assert backend.supports_reflection_review is False
       assert backend.supports_reflection_text_edit is False

   def test_supports_episodes_still_false_regression(backend) -> None:
       """Regression pin: the pre-existing episodes flag stays False."""
       assert backend.supports_episodes is False
   ```

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py -v`
   — FAIL (`AttributeError`: the five properties do not exist yet).

3. **Implement.** In `better_memory/storage/protocol.py`, immediately after the
   `supports_episodes` property (ends `protocol.py:64`), add:
   ```python
   @property
   def supports_observations(self) -> bool:
       """True when the backend stores raw observations as a first-class,
       listable record type (the Observations tab and observation drawers).
       False in agentcore mode, where AgentCore ingests events and exposes
       only extracted memory records -- there is no raw-observation store to
       list. The management UI hides the Observations tab when this is
       False."""
       ...

   @property
   def supports_provenance(self) -> bool:
       """True when a memory can be traced to the observations / episode it
       was synthesised from (the provenance joins shown in drawers). False in
       agentcore mode, where extraction happens inside AgentCore and no
       per-memory provenance chain is returned. The UI hides provenance
       sections when this is False."""
       ...

   @property
   def supports_retention_runs(self) -> bool:
       """True when retention / pruning executes as recorded local runs the
       UI can list (the Diagnostics retention panel). False in agentcore
       mode, where pruning is event-expiry managed by AgentCore with no local
       run ledger."""
       ...

   @property
   def supports_reflection_review(self) -> bool:
       """True when reflections pass through a local pending_review ->
       confirmed lifecycle the UI can action (the confirm control). False in
       agentcore mode, whose status vocabulary is active / promoted / retired
       with NO pending_review state -- reflections are born active."""
       ...

   @property
   def supports_reflection_text_edit(self) -> bool:
       """True when a reflection's use_cases / hints text is user-editable in
       place (the edit form). False in agentcore mode, where reflection
       bodies are AI-managed by AgentCore extraction and not free-text
       editable."""
       ...
   ```
   In `better_memory/storage/sqlite.py`, immediately after the `supports_episodes` property
   (ends `sqlite.py:88`), add:
   ```python
   @property
   def supports_observations(self) -> bool:
       return True

   @property
   def supports_provenance(self) -> bool:
       return True

   @property
   def supports_retention_runs(self) -> bool:
       return True

   @property
   def supports_reflection_review(self) -> bool:
       return True

   @property
   def supports_reflection_text_edit(self) -> bool:
       return True
   ```
   In `better_memory/storage/agentcore.py`, immediately after the `supports_episodes` property
   (ends `agentcore.py:107`), add:
   ```python
   @property
   def supports_observations(self) -> bool:
       return False

   @property
   def supports_provenance(self) -> bool:
       return False

   @property
   def supports_retention_runs(self) -> bool:
       return False

   @property
   def supports_reflection_review(self) -> bool:
       return False

   @property
   def supports_reflection_text_edit(self) -> bool:
       return False
   ```
   Also update the agentcore module docstring capability-flags list (`agentcore.py:8-15`) to
   note the five new flags are all False (observations/provenance/retention-runs/reflection-
   review/reflection-text-edit) alongside the existing `supports_synthesis` / `supports_episodes`
   entries.

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py -v`
   — PASS. `test_sqlite_backend_satisfies_protocol` and `test_agentcore_backend_satisfies_protocol`
   still pass (both backends satisfy the widened `@runtime_checkable` protocol).

5. **Commit** `feat(storage): add five UI content-capability flags (sqlite True, agentcore False)`.

**Deliverable:** `SqliteBackend().supports_reflection_text_edit is True` and
`AgentCoreBackend(...).supports_reflection_text_edit is False`, verified by the two new
regression-pinned test pairs; `supports_episodes` unchanged on both.

**Sqlite preservation:** the five additions are new read-only `bool` properties only; no
existing SqliteBackend method is touched, so every `tests/ui/*` and existing
`tests/storage/*` assertion is unchanged.

---

### Task F2: Build the backend in `create_app`; retain the operational conn; inject caps

**Files:**
- Modify: `better_memory/ui/app.py` (module-top import; `create_app` body after the extensions
  block `app.py:92-98`; new context processor)
- Test: `tests/ui/test_app.py` (extend)

**Interfaces:**
- Consumes: `build_backend(*, config, memory_conn, embedder=None, sync_embedder=None,
  session_id, project)` (`storage/factory.py:32-40`); `get_config()`, `project_name()`
  (already imported in `app.py:15`); the five flags from Task F1.
- Produces:
  ```python
  # module top, alongside the existing better_memory.config imports
  from better_memory.storage.factory import build_backend

  # in create_app, AFTER app.extensions["db_path"] = resolved_db (app.py:98):
  app.extensions["backend"] = build_backend(
      config=get_config(),
      memory_conn=db_conn,
      sync_embedder=resolved_sync_embedder,
      session_id=None,
      project=project_name(),
  )

  @app.context_processor
  def _inject_caps() -> dict[str, object]:
      b = app.extensions["backend"]
      return {"caps": {
          "supports_episodes": b.supports_episodes,
          "supports_observations": b.supports_observations,
          "supports_provenance": b.supports_provenance,
          "supports_retention_runs": b.supports_retention_runs,
          "supports_reflection_review": b.supports_reflection_review,
          "supports_reflection_text_edit": b.supports_reflection_text_edit,
      }}
  ```
  `embedder` is omitted from the `build_backend` call (the UI has no ObservationService
  embedder — sqlite indexes via FTS triggers). `app.extensions["db_connection"]` and the
  existing `episode_service` / `reflection_service` / `sync_embedder` / `db_path` extensions
  are UNCHANGED. No route body changes; no template gate yet (Task F3). The `caps` dict keys
  are the exact canonical flag names, consumed by every later phase's template gates.

**Steps:**

1. **Write failing tests.** Append to `tests/ui/test_app.py`:
   ```python
   class TestBackendWiring:
       def test_create_app_builds_sqlite_backend_and_retains_conn(
           self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
       ) -> None:
           from better_memory.storage.sqlite import SqliteBackend

           monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
           app = create_app(start_watchdog=False, db_path=tmp_db)
           backend = app.extensions["backend"]
           assert isinstance(backend, SqliteBackend)
           # Operational conn retained and still usable.
           conn = app.extensions["db_connection"]
           assert conn.execute(
               "SELECT COUNT(*) FROM observations"
           ).fetchone()[0] == 0
           # Sqlite path shares the single connection -- no second store.
           assert backend._conn is conn

       def test_build_backend_called_with_canonical_kwargs(
           self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
       ) -> None:
           from unittest.mock import MagicMock

           from better_memory.config import project_name

           monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
           stub = MagicMock(name="stub-backend")
           spy = MagicMock(return_value=stub)
           monkeypatch.setattr("better_memory.ui.app.build_backend", spy)
           app = create_app(start_watchdog=False, db_path=tmp_db)
           assert app.extensions["backend"] is stub
           spy.assert_called_once()
           _, kwargs = spy.call_args
           assert kwargs["memory_conn"] is app.extensions["db_connection"]
           assert kwargs["sync_embedder"] is app.extensions["sync_embedder"]
           assert kwargs["session_id"] is None
           assert kwargs["project"] == project_name()
           assert "config" in kwargs  # get_config() forwarded to the factory

       def test_caps_read_from_backend_on_a_real_route(
           self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
       ) -> None:
           # [[server-boot-real-call]]: drive an ACTUAL route through a stubbed
           # agentcore backend and prove the six caps were sourced FROM the
           # backend during the render -- no leaked local-content read.
           from unittest.mock import MagicMock, PropertyMock

           monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
           # Fresh throwaway type per test so PropertyMocks don't leak.
           StubBackend = type("StubBackend", (), {})
           stub = StubBackend()
           props: dict[str, PropertyMock] = {}
           for name in (
               "supports_episodes", "supports_observations",
               "supports_provenance", "supports_retention_runs",
               "supports_reflection_review", "supports_reflection_text_edit",
           ):
               p = PropertyMock(return_value=False)
               setattr(StubBackend, name, p)
               props[name] = p
           monkeypatch.setattr(
               "better_memory.ui.app.build_backend",
               MagicMock(return_value=stub),
           )
           app = create_app(start_watchdog=False, db_path=tmp_db)
           app.config["TESTING"] = True
           with app.test_client() as c:
               resp = c.get("/episodes")
           assert resp.status_code == 200
           # Every cap was read off the backend object during the real render.
           for name, p in props.items():
               p.assert_called()
   ```

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_app.py::TestBackendWiring -v`
   — FAIL (`KeyError: 'backend'` / `build_backend` not importable from `better_memory.ui.app`).

3. **Implement** per Interfaces: add the `from better_memory.storage.factory import
   build_backend` import at module top; add the `app.extensions["backend"] = build_backend(...)`
   call directly after `app.extensions["db_path"] = resolved_db` (`app.py:98`); register the
   `_inject_caps` context processor. Do not remove or reorder any existing extension. Do not
   change any route body.

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui -q` — all pass. The full existing
   `tests/ui` suite (TestNav, TestHealthz, TestOriginCheck, semantic/reflection/observation
   route tests) stays green: the backend is built but unused by content routes, and `caps` is
   injected but not yet consumed by any template.

5. **Commit** `feat(ui): build StorageBackend in create_app + inject caps context processor`.

**Deliverable:** `app.extensions["backend"]` is a `StorageBackend`; `app.extensions
["db_connection"]` is retained; a real `GET /episodes` render reads all six caps off the
backend (PropertyMock-asserted), proving no local-content leak.

**Sqlite preservation:** `db_connection` and all existing extensions are unchanged; no content
route is re-routed; `caps` is injected but unconsumed until F3. Every `tests/ui/*` assertion
is unchanged.

---

### Task F3: Reference gate — wrap ONLY the Episodes nav link on `caps.supports_episodes`

**Files:**
- Modify: `better_memory/ui/templates/base.html` (Episodes `.rail-link`, `base.html:91-94`)
- Test: `tests/ui/test_app.py` (extend)

**Interfaces:**
- Consumes: `caps.supports_episodes` from the F2 context processor.
- Produces: the existing Episodes `.rail-link` anchor (`base.html:91-94`) wrapped verbatim in
  `{% if caps.supports_episodes %} ... {% endif %}`. No other nav link is gated in this PR
  (Observations / Reflections / Semantic / Diagnostics gating is later-phase work). No new CSS
  class; the block is pure Jinja around the untouched `.rail-link` markup
  (`[[brutalist-css-classes]]`).

**Steps:**

1. **Write failing tests.** Append to `tests/ui/test_app.py`:
   ```python
   class TestEpisodesGate:
       _RAIL_LINK = '<span class="rail-label">Episodes</span>'

       def test_episodes_link_present_in_sqlite_mode(
           self, client: FlaskClient
       ) -> None:
           # sqlite backend -> supports_episodes True -> link renders as today.
           body = client.get("/episodes").get_data(as_text=True)
           assert self._RAIL_LINK in body

       def test_episodes_link_hidden_when_flag_false(
           self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
       ) -> None:
           # [[guard-needs-triggering-test]]: seed supports_episodes=False to
           # trigger the {% if %} false branch. [[playwright-domtext]]: assert
           # on nav-element markup presence/absence, not CSS visibility.
           from unittest.mock import MagicMock

           monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
           StubBackend = type("StubBackend", (), {})
           stub = StubBackend()
           # Only Episodes is gated this phase; the other five stay True so
           # the rest of the rail renders normally.
           setattr(type(stub), "supports_episodes", property(lambda self: False))
           for name in (
               "supports_observations", "supports_provenance",
               "supports_retention_runs", "supports_reflection_review",
               "supports_reflection_text_edit",
           ):
               setattr(type(stub), name, property(lambda self: True))
           monkeypatch.setattr(
               "better_memory.ui.app.build_backend",
               MagicMock(return_value=stub),
           )
           app = create_app(start_watchdog=False, db_path=tmp_db)
           app.config["TESTING"] = True
           with app.test_client() as c:
               body = c.get("/episodes").get_data(as_text=True)
           assert self._RAIL_LINK not in body
           # Sibling links unaffected -- prove only Episodes was gated.
           assert '<span class="rail-label">Reflections</span>' in body
   ```

2. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui/test_app.py::TestEpisodesGate -v`
   — the hidden-branch test FAILs (link still renders unconditionally).

3. **Implement.** In `better_memory/ui/templates/base.html`, wrap the Episodes anchor
   (`base.html:91-94`) exactly:
   ```html
   {% if caps.supports_episodes %}
   <a class="rail-link {% if active_tab == 'episodes' %}active{% endif %}" href="{{ url_for('episodes') }}">
     <span class="rail-num">01</span>
     <span class="rail-label">Episodes</span>
   </a>
   {% endif %}
   ```
   Leave the Observations / Reflections / Semantic / Diagnostics links untouched.

4. **Run** `./.venv/Scripts/python.exe -m pytest tests/ui -q` — all pass, including the
   existing `TestNav.test_nav_shows_episodes_and_reflections` pin (sqlite mode → Episodes still
   shown) and `TestNav.test_nav_hides_old_tabs` (unchanged).

5. **Commit** `feat(ui): gate Episodes nav link on caps.supports_episodes (reference gate)`.

**Deliverable:** In sqlite mode the Episodes rail-link renders identically to today; with a
backend reporting `supports_episodes=False` the rail-link markup is absent while sibling links
remain — the caps mechanism proven end-to-end via DOM-markup presence/absence.

**Sqlite preservation:** `caps.supports_episodes` is `True` on sqlite, so the wrapped block
always renders → sqlite HTML is byte-identical; the existing `TestNav` pins are unedited.

---

### Task F4: Docs, pyright, full suite

**Files:**
- Modify: `website/architecture.md` (storage / UI prose)
- Modify: `website/agentcore-setup.md` (capability-flags note)
- (Protocol docstrings already written in F1 — no further edit.)

**Steps:**

1. **Docs.** Per `[[keep-docs-in-sync]]`, verifying every factual token against the code just
   written:
   - `website/architecture.md`: update the storage / UI paragraph to state the management UI
     now builds a `StorageBackend` in `create_app` (via `factory.build_backend`, sharing the
     same connection + `sync_embedder`), retains `app.extensions["db_connection"]` for
     session-operational state, and exposes the six capability flags to templates via a
     `caps` context processor; the Episodes nav link is gated on `caps.supports_episodes` as
     the reference consumer. Do NOT claim any content route is routed through the backend yet
     (it is not — that is the Content phase).
   - `website/agentcore-setup.md`: add / update a capability-flags note listing the six flags
     (`supports_episodes`, `supports_observations`, `supports_provenance`,
     `supports_retention_runs`, `supports_reflection_review`, `supports_reflection_text_edit`),
     all `False` in agentcore mode, and note that in this release only the Episodes tab is
     gated (the remaining flags are wired for later UI gating).
   - State explicitly in the PR body (Task F5) that `README.md`, `website/mcp-tools.md`, and
     `website/configuration.md` tables are **UNAFFECTED** (no new env key, no MCP tool change).

2. **Run** `./.venv/Scripts/python.exe -m pyright` — 0 errors. (New properties return literal
   `bool`; the context processor returns `dict[str, object]`.)

3. **Run** `./.venv/Scripts/python.exe -m pytest tests -q` — full green. Fix stragglers
   minimally (none expected — this phase is additive).

4. **Commit** `docs: capability flags + StorageBackend-in-create_app foundation`.

**Deliverable:** docs describe the foundation accurately; pyright clean; full suite green.

**Sqlite preservation:** docs-only + verification; no code change beyond F1-F3.

---

### Task F5: PR, babysit, merge

**Steps:**

1. Push the branch; `gh pr create`. PR body includes: spec link; the foundation summary
   (five new caps flags sqlite-True / agentcore-False; backend built in `create_app` with the
   operational conn retained; `caps` context processor injecting the six flags; Episodes nav
   gated as the reference consumer; NO content route re-routed this PR); sqlite-byte-identical
   proof (existing `tests/ui/*` + `tests/storage/*` green, assertions unedited); the explicit
   "docs UNAFFECTED for README / mcp-tools / configuration tables" note; footer
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
2. Babysit bots to green with zero unresolved threads → squash-merge, checkout main, pull.
3. Memory sweep before finishing (CLAUDE.md phase trigger): record any non-obvious review fix
   as a `failure` observation; record the "caps sourced from `app.extensions['backend']`, six
   canonical flag names, Episodes gate as reference" decision as a `success` / `decision`
   observation if not already captured.

**Deliverable:** merged PR 1; `main` carries the flags + backend wiring + caps + Episodes gate;
later phases build on `app.extensions["backend"]` and `caps.*`.

---

## Produced signatures later phases consume

- **Protocol / both backends** (`StorageBackend` `@property -> bool`):
  - `supports_observations` — Sqlite `True`, AgentCore `False`
  - `supports_provenance` — Sqlite `True`, AgentCore `False`
  - `supports_retention_runs` — Sqlite `True`, AgentCore `False`
  - `supports_reflection_review` — Sqlite `True`, AgentCore `False`
  - `supports_reflection_text_edit` — Sqlite `True`, AgentCore `False`
  - (`supports_episodes` pre-existing, unchanged: Sqlite `True`, AgentCore `False`)
- **App wiring** (`better_memory/ui/app.py`):
  - `app.extensions["backend"]: StorageBackend` — built via
    `build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`
  - `app.extensions["db_connection"]` — retained, OPERATIONAL state
  - `@app.context_processor _inject_caps` injects template global `caps` — a `dict[str, bool]`
    keyed by the six canonical flag names:
    `supports_episodes`, `supports_observations`, `supports_provenance`,
    `supports_retention_runs`, `supports_reflection_review`, `supports_reflection_text_edit`
- **Template** (`base.html`): Episodes `.rail-link` gated on `{% if caps.supports_episodes %}`;
  all other nav links ungated (later-phase gates attach to the remaining `caps.*` keys).
