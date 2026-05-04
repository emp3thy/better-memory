# Semantic Memories UI Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/semantic` tab to the Archive UI so users can view, create, edit, scope-toggle, and delete semantic memories — and promote useful observations into semantic memories from the existing observation drawer.

**Architecture:** Three commits. (1) Two new methods on `SemanticMemoryService` (`set_scope`, `create_from_observation`) with full TDD. (2) Six new templates plus a new top-level page route + four htmx fragment routes for the panel UI, mirroring the existing reflections-panel pattern. (3) One new POST route + a small drawer template change for the "promote observation" flow.

**Tech Stack:** Python 3.12+, Flask, htmx, Jinja2, SQLite, pytest. Builds on PR #34's `SemanticMemoryService` base + migration 0008.

**Branch:** `semantic-memories-ui` off `main` (PR #34 already merged at `aa34ad14`). Single PR, three commits, will land as **#37** (issues #35 nav-redesign and #36 filter/search reserved the prior slots).

**Spec:** `docs/superpowers/specs/2026-05-04-semantic-memories-ui-design.md` — read before starting.

**Test/type discipline:** every behavior change is TDD (red → green → commit). Pyright (`uv run pyright`) and pytest (`uv run pytest -q`) must be green at every commit boundary.

---

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `better_memory/services/semantic.py` | Modify (extend) | Add `set_scope` and `create_from_observation` methods. |
| `tests/services/test_semantic.py` | Modify (extend) | Two new test classes covering 10 cases. |
| `better_memory/ui/app.py` | Modify | Add 7 new routes (5 for /semantic + 1 for promote + the page route). |
| `better_memory/ui/templates/base.html` | Modify | Insert nav-rail entry between Reflections and Diagnostics. |
| `better_memory/ui/templates/semantic.html` | Create | Top-level page (extends base.html), hosts the panel + drawer slot. |
| `better_memory/ui/templates/fragments/panel_semantic.html` | Create | Panel content — empty state branch + iterated rows + create-form include. |
| `better_memory/ui/templates/fragments/semantic_row.html` | Create | Single memory card with content + scope badge + edit/scope/delete buttons. |
| `better_memory/ui/templates/fragments/semantic_drawer.html` | Create | Expanded view with content textarea + scope select + Save/Cancel. |
| `better_memory/ui/templates/fragments/semantic_create_form.html` | Create | Top-of-panel form with content textarea + scope select + Add button. |
| `better_memory/ui/templates/fragments/observation_drawer.html` | Modify | Add the "Promote to semantic memory" form at the bottom (active observations only). |
| `better_memory/ui/templates/fragments/observation_promoted_card.html` | Create | Brief confirmation card swapped in after a successful promote. |
| `tests/ui/test_semantic.py` | Create | Route-level tests for /semantic page + 5 panel actions + drawer. |
| `tests/ui/test_observations.py` | Modify (extend) | New test class covering the drawer promote affordance + the new POST route. |

---

## Pre-implementation setup

- [ ] **Step 0a: Create branch**

```bash
git checkout main
git pull --rebase
git checkout -b semantic-memories-ui
```

- [ ] **Step 0b: Sanity baseline**

```bash
uv run pytest -q
```

Expected: full green (post-PR #34 baseline; ~660+ passed).

```bash
uv run pyright
```

Expected: 0 errors.

---

# Commit 1 — Service additions (`set_scope` + `create_from_observation`)

### Task 1: `set_scope` method (TDD)

**Confidence:** 95% — direct analog of `update_text` from PR #34, including the same rollback-before-raise pattern.

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_semantic.py`

- [ ] **Step 1: Append failing tests to `tests/services/test_semantic.py`**

```python
class TestSetScope:
    def test_set_scope_changes_value(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")  # default scope=project
        svc.set_scope(id=memory_id, scope="general")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_set_scope_round_trip(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1", scope="general")
        svc.set_scope(id=memory_id, scope="project")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "project"

    def test_set_scope_bumps_updated_at(self, conn, fixed_clock):
        from datetime import timedelta
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")
        # advance the clock for the scope change
        svc._clock = lambda: fixed_clock() + timedelta(hours=1)
        svc.set_scope(id=memory_id, scope="general")
        row = conn.execute(
            "SELECT created_at, updated_at FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["updated_at"] != row["created_at"]

    def test_set_scope_rejects_invalid(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")
        with pytest.raises(ValueError, match="scope"):
            svc.set_scope(id=memory_id, scope="invalid")

    def test_set_scope_raises_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.set_scope(id="nope", scope="general")

    def test_set_scope_missing_id_rolls_back_implicit_transaction(
        self, conn, fixed_clock,
    ):
        """Mirror update_text's regression — sqlite3 default isolation_level
        opens an implicit BEGIN before the UPDATE; the failure path must
        rollback to release the WAL write lock."""
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.set_scope(id="nope", scope="general")
        assert conn.in_transaction is False
```

- [ ] **Step 2: Run, verify 6 fail with AttributeError**

```bash
uv run pytest tests/services/test_semantic.py::TestSetScope -v
```
Expected: 6 errors with `AttributeError: 'SemanticMemoryService' object has no attribute 'set_scope'`.

- [ ] **Step 3: Implement `set_scope`**

Append to `SemanticMemoryService` in `better_memory/services/semantic.py`, after `update_text`:

```python
    def set_scope(self, *, id: str, scope: str) -> None:
        """Toggle a semantic memory's scope between 'project' and 'general'.

        Bumps updated_at. No-op-style: setting scope to the same value
        still bumps updated_at (the update is real DB-side; we don't
        short-circuit). Raises ValueError on invalid scope or missing id.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        now = self._clock().isoformat()
        cur = self._conn.execute(
            "UPDATE semantic_memories SET scope = ?, updated_at = ? "
            "WHERE id = ?",
            (scope, now, id),
        )
        if cur.rowcount == 0:
            # Roll back the implicit BEGIN sqlite3 opened — same defense
            # as update_text per PR #34's BugBot finding.
            self._conn.rollback()
            raise ValueError(f"semantic memory not found: {id}")
        self._conn.commit()
```

- [ ] **Step 4: Run, verify 6 pass**

```bash
uv run pytest tests/services/test_semantic.py::TestSetScope -v
```
Expected: 6 passed.

### Task 2: `create_from_observation` method (TDD)

**Confidence:** 90% — multi-step orchestration with SAVEPOINT envelope + observation-status flip + scope validation. Tests cover all 5 paths (happy / missing / non-active / scope variants / atomicity).

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_semantic.py`

- [ ] **Step 1: Append failing tests**

```python
class TestCreateFromObservation:
    def _seed_active_observation(self, conn, *, obs_id="o1", project="p1",
                                 content="bug found", episode_id=None):
        if episode_id is None:
            episode_id = "ep-default"
            conn.execute(
                "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
                "(?, ?, '2026-04-01T00:00:00+00:00')",
                (episode_id, project),
            )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "(?, ?, ?, ?, 'active', 'success', "
            "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, content, project, episode_id),
        )
        conn.commit()

    def test_create_from_observation_happy_path(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1", content="rule text")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create_from_observation(observation_id="o1")
        # New semantic memory exists with the observation's content + project.
        mem_row = conn.execute(
            "SELECT content, project, scope FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert mem_row["content"] == "rule text"
        assert mem_row["project"] == "p1"
        assert mem_row["scope"] == "project"  # default
        # Source observation flipped to consumed_without_reflection.
        obs_row = conn.execute(
            "SELECT status, status_changed_at FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert obs_row["status"] == "consumed_without_reflection"
        assert obs_row["status_changed_at"] == "2026-05-04T12:00:00+00:00"

    def test_create_from_observation_with_general_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create_from_observation(
            observation_id="o1", scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_create_from_observation_rejects_invalid_scope(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="scope"):
            svc.create_from_observation(observation_id="o1", scope="invalid")
        # Observation status unchanged.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "active"

    def test_create_from_observation_raises_on_missing_observation(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="observation not found"):
            svc.create_from_observation(observation_id="ghost")

    def test_create_from_observation_raises_on_already_consumed(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        # Seed a consumed_into_reflection observation.
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', 'p1', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "('o1','x','p1','ep1','consumed_into_reflection','success',"
            "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')"
        )
        conn.commit()
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not active"):
            svc.create_from_observation(observation_id="o1")
        # Observation row unchanged.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        # No semantic_memories row created.
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memories"
        ).fetchone()[0]
        assert count == 0

    def test_create_from_observation_atomic_on_failure(
        self, conn, fixed_clock, monkeypatch,
    ):
        """If the second UPDATE (observation status flip) fails, the
        SAVEPOINT envelope must roll back the inserted semantic memory.
        Forces failure by patching the second .execute call.
        """
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)

        original_execute = conn.execute
        call_count = [0]
        def boom_on_obs_update(sql, *args, **kwargs):
            call_count[0] += 1
            if "UPDATE observations" in sql:
                raise RuntimeError("simulated failure mid-promote")
            return original_execute(sql, *args, **kwargs)
        monkeypatch.setattr(conn, "execute", boom_on_obs_update)

        with pytest.raises(RuntimeError, match="simulated"):
            svc.create_from_observation(observation_id="o1")

        # Restore — verify rollback happened.
        monkeypatch.setattr(conn, "execute", original_execute)
        # No semantic_memories row should exist (insert was rolled back).
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memories"
        ).fetchone()[0]
        assert count == 0
        # Observation status unchanged.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "active"
```

- [ ] **Step 2: Run, verify 6 fail**

```bash
uv run pytest tests/services/test_semantic.py::TestCreateFromObservation -v
```
Expected: 6 errors, mostly `AttributeError`.

- [ ] **Step 3: Implement `create_from_observation`**

Append to `SemanticMemoryService`, after `set_scope`:

```python
    def create_from_observation(
        self, *, observation_id: str, scope: str = "project"
    ) -> str:
        """Promote an active observation into a new semantic memory.

        Atomically (within SAVEPOINT promote_observation):
        1. Read the observation; raise if missing or not status='active'.
        2. INSERT a new semantic_memories row with the observation's
           content + project, the requested scope, and current timestamp.
        3. UPDATE the observation status='consumed_without_reflection'
           and bump status_changed_at.

        Raises ValueError on invalid scope, missing observation, or
        already-consumed observation. Returns the new memory id.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        row = self._conn.execute(
            "SELECT content, project, status FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"observation not found: {observation_id}")
        if row["status"] != "active":
            raise ValueError(
                f"observation {observation_id} is not active "
                f"(status={row['status']!r}); cannot promote"
            )

        memory_id = uuid4().hex
        now = self._clock().isoformat()
        self._conn.execute("SAVEPOINT promote_observation")
        try:
            self._conn.execute(
                """
                INSERT INTO semantic_memories
                    (id, content, project, scope, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, row["content"], row["project"], scope, now, now),
            )
            self._conn.execute(
                "UPDATE observations "
                "SET status = 'consumed_without_reflection', status_changed_at = ? "
                "WHERE id = ?",
                (now, observation_id),
            )
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT promote_observation")
            self._conn.execute("RELEASE SAVEPOINT promote_observation")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT promote_observation")
        self._conn.commit()
        return memory_id
```

- [ ] **Step 4: Run, verify 6 pass**

```bash
uv run pytest tests/services/test_semantic.py::TestCreateFromObservation -v
```
Expected: 6 passed.

### Task 3: Verify + Commit 1

**Confidence:** 99%.

- [ ] **Step 1: Full service test suite green**

```bash
uv run pytest tests/services/test_semantic.py -q
```
Expected: all pass (existing 15 from PR #34 + 12 new = 27).

- [ ] **Step 2: Pyright clean**

```bash
uv run pyright better_memory/services/semantic.py tests/services/test_semantic.py
```
Expected: 0 errors.

- [ ] **Step 3: Run full test suite — confirm no regressions**

```bash
uv run pytest -q
```
Expected: full green.

- [ ] **Step 4: Commit**

```bash
git add better_memory/services/semantic.py tests/services/test_semantic.py
git commit -m "$(cat <<'EOF'
feat(semantic): set_scope + create_from_observation service methods

Two new methods on SemanticMemoryService for the upcoming UI tab:

- set_scope(*, id, scope) — toggle scope between 'project' and
  'general'. Bumps updated_at. ValueError on invalid scope or
  missing id (with rollback before raise, mirroring update_text).

- create_from_observation(*, observation_id, scope='project') -> id —
  atomically promote an active observation into a new semantic memory.
  Wraps INSERT + observation-status-flip in SAVEPOINT promote_observation.
  Source observation becomes status='consumed_without_reflection'
  (reusing existing status; no migration needed). Raises ValueError on
  missing or already-consumed observations.

UI routes + drawer integration follow in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Commit 2 — `/semantic` panel UI

### Task 4: Page route + nav-rail entry

**Confidence:** 95% — direct port of the reflections page pattern.

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `better_memory/ui/templates/base.html`
- Create: `better_memory/ui/templates/semantic.html`
- Modify: `tests/ui/test_observations.py` (we'll create `test_semantic.py` separately in Task 5)

- [ ] **Step 1: Create `better_memory/ui/templates/semantic.html`**

```jinja
{% extends "base.html" %}
{% block title %}Semantic memories — better-memory{% endblock %}
{% block main %}
<section class="semantic">
  <div id="semantic-panel"
       hx-get="{{ url_for('semantic_panel') }}"
       hx-trigger="load, every 30s, semantic-changed from:body"
       hx-swap="innerHTML">
  </div>

  <div id="semantic-drawer"></div>
</section>
{% endblock %}
```

- [ ] **Step 2: Add nav-rail entry to `better_memory/ui/templates/base.html`**

Find the existing rail-link block (around line 37-50). Insert a new entry between `reflections` and `diagnostics`:

```jinja
      <a class="rail-link {% if active_tab == 'semantic' %}active{% endif %}" href="{{ url_for('semantic') }}">
        <em>IV.</em> Semantic
      </a>
```

Adjust the diagnostics link's roman numeral from `IV.` to `V.` (preserve the existing visual order).

- [ ] **Step 3: Add the page route to `better_memory/ui/app.py`**

Find the existing `reflections` route (`@app.get("/reflections")`). After it, add:

```python
    @app.get("/semantic")
    def semantic() -> str:
        return render_template("semantic.html", active_tab="semantic")

    @app.get("/semantic/panel")
    def semantic_panel() -> str:
        from better_memory.services.semantic import SemanticMemoryService
        conn = app.extensions["db_connection"]
        project = request.args.get("project") or project_name()
        svc = SemanticMemoryService(conn)
        rows = svc.list_for_project(project=project)
        return render_template(
            "fragments/panel_semantic.html", rows=rows, project=project,
        )
```

(The `request` import is already at the top of `app.py`; confirm via grep before adding the route.)

- [ ] **Step 4: Create `tests/ui/test_semantic.py` with the page test**

```python
"""Route tests for the /semantic UI tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient


class TestSemanticPage:
    def test_returns_200_with_active_nav_tab(
        self, client: FlaskClient,
    ):
        response = client.get("/semantic")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Nav-rail entry has the active class for this tab.
        assert "rail-link active" in body
        assert "Semantic" in body
```

(`client` fixture is defined in `tests/ui/conftest.py`, same as test_observations.py uses.)

- [ ] **Step 5: Run, verify the page test passes**

```bash
uv run pytest tests/ui/test_semantic.py::TestSemanticPage -v
```
Expected: 1 passed.

### Task 5: Panel route + create form + tests

**Confidence:** 92% — the panel template + create form interact via htmx; tests need to assert on the rendered HTML structure.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/panel_semantic.html`
- Create: `better_memory/ui/templates/fragments/semantic_create_form.html`
- Modify: `tests/ui/test_semantic.py`

- [ ] **Step 1: Create the create-form fragment**

`better_memory/ui/templates/fragments/semantic_create_form.html`:

```jinja
<form id="semantic-create-form"
      class="semantic-create-form"
      hx-post="{{ url_for('semantic_create') }}"
      hx-target="#semantic-panel"
      hx-swap="none">
  <textarea name="content" rows="2" placeholder="Add a semantic memory…" required></textarea>
  <div class="form-row">
    <select name="scope">
      <option value="project" selected>project</option>
      <option value="general">general</option>
    </select>
    <button type="submit">Add memory</button>
  </div>
</form>
```

- [ ] **Step 2: Create the panel fragment**

`better_memory/ui/templates/fragments/panel_semantic.html`:

```jinja
{% include "fragments/semantic_create_form.html" %}

{% if not rows %}
  <div class="empty-state">
    <p>No semantic memories yet. Use the form above to add one, or
       promote an observation from the Observations tab.</p>
  </div>
{% else %}
  <div class="semantic-list">
    {% for row in rows %}
      {% include "fragments/semantic_row.html" %}
    {% endfor %}
  </div>
{% endif %}
```

- [ ] **Step 3: Create a minimal `semantic_row.html` (placeholder; Task 6 fills it)**

```jinja
<div class="semantic-row scope-{{ row.scope }}">
  <div class="row-content">{{ row.content }}</div>
  <div class="row-meta">
    <span class="scope-badge scope-{{ row.scope }}">{{ row.scope }}</span>
    <span class="updated-at">{{ row.updated_at }}</span>
  </div>
</div>
```

(Task 6 adds the buttons. Splitting keeps each task small.)

- [ ] **Step 4: Add the create route**

Append to `app.py` after `semantic_panel`:

```python
    @app.post("/semantic")
    def semantic_create() -> tuple[str, int, dict[str, str]]:
        from better_memory.services.semantic import SemanticMemoryService
        from markupsafe import escape
        conn = app.extensions["db_connection"]
        project = project_name()
        content = request.form.get("content", "").strip()
        scope = request.form.get("scope") or "project"
        svc = SemanticMemoryService(conn)
        try:
            svc.create(content=content, project=project, scope=scope)
        except ValueError as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})
```

- [ ] **Step 5: Append failing tests for panel + create**

```python
class TestSemanticPanel:
    def test_empty_state_when_no_memories(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-empty")
        response = client.get("/semantic/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No semantic memories yet" in body
        # Create form is always rendered.
        assert 'id="semantic-create-form"' in body

    def test_renders_seeded_rows_newest_first(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','older rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00'),"
                "('m2','newer rule','proj-a','general',"
                " '2026-05-04T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/panel")
        body = response.get_data(as_text=True)
        # Both visible.
        assert "older rule" in body
        assert "newer rule" in body
        # Newer one appears before the older one in the rendered HTML.
        assert body.index("newer rule") < body.index("older rule")

    def test_includes_general_from_other_projects(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','proj-a project rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00'),"
                "('m2','proj-b general rule','proj-b','general',"
                " '2026-05-04T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/panel")
        body = response.get_data(as_text=True)
        assert "proj-a project rule" in body
        assert "proj-b general rule" in body


class TestSemanticCreate:
    def test_creates_row_and_returns_hx_trigger(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic",
            data={"content": "new rule", "scope": "general"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        # Row landed in the DB.
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT content, scope, project FROM semantic_memories"
            ).fetchone()
        assert row[0] == "new rule"
        assert row[1] == "general"
        assert row[2] == "proj-a"

    def test_empty_content_returns_400_card_error(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post("/semantic", data={"content": "   "})
        assert response.status_code == 400
        body = response.get_data(as_text=True)
        assert "card-error" in body
```

- [ ] **Step 6: Run, verify the new tests pass**

```bash
uv run pytest tests/ui/test_semantic.py -v
```
Expected: TestSemanticPage (1) + TestSemanticPanel (3) + TestSemanticCreate (2) = 6 passed.

### Task 6: Row buttons + scope toggle + delete

**Confidence:** 92% — the htmx target choice (closest container vs body) needs care; the empty-state and populated-state both need to be re-rendered correctly after a delete.

**Files:**
- Modify: `better_memory/ui/templates/fragments/semantic_row.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_semantic.py`

- [ ] **Step 1: Replace `semantic_row.html` with the full version**

```jinja
<div class="semantic-row scope-{{ row.scope }}">
  <div class="row-content">{{ row.content }}</div>
  <div class="row-meta">
    <span class="scope-badge scope-{{ row.scope }}">{{ row.scope }}</span>
    <span class="updated-at">{{ row.updated_at }}</span>
  </div>
  <div class="row-actions">
    <button type="button"
            class="action-edit"
            hx-get="{{ url_for('semantic_drawer', id=row.id) }}"
            hx-target="#semantic-drawer"
            hx-swap="innerHTML">
      Edit
    </button>
    {% if row.scope == 'project' %}
      <button type="button"
              class="action-scope"
              hx-post="{{ url_for('semantic_scope', id=row.id) }}"
              hx-vals='{"scope": "general"}'
              hx-swap="none">
        Make general
      </button>
    {% else %}
      <button type="button"
              class="action-scope"
              hx-post="{{ url_for('semantic_scope', id=row.id) }}"
              hx-vals='{"scope": "project"}'
              hx-swap="none">
        Make project
      </button>
    {% endif %}
    <button type="button"
            class="action-delete"
            hx-post="{{ url_for('semantic_delete', id=row.id) }}"
            hx-confirm="Delete this semantic memory?"
            hx-swap="none">
      Delete
    </button>
  </div>
</div>
```

(All POST routes return `HX-Trigger: semantic-changed`, which the panel listens for via `hx-trigger="… semantic-changed from:body"` from Task 4 — the panel re-fetches automatically.)

- [ ] **Step 2: Add the scope + delete routes to `app.py`**

Append after `semantic_create`:

```python
    @app.post("/semantic/<id>/scope")
    def semantic_scope(id: str) -> tuple[str, int, dict[str, str]]:
        from better_memory.services.semantic import SemanticMemoryService
        from markupsafe import escape
        conn = app.extensions["db_connection"]
        scope = request.form.get("scope") or "project"
        svc = SemanticMemoryService(conn)
        try:
            svc.set_scope(id=id, scope=scope)
        except ValueError as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})

    @app.post("/semantic/<id>/delete")
    def semantic_delete(id: str) -> tuple[str, int, dict[str, str]]:
        from better_memory.services.semantic import SemanticMemoryService
        conn = app.extensions["db_connection"]
        svc = SemanticMemoryService(conn)
        svc.delete(id=id)  # idempotent
        return ("", 200, {"HX-Trigger": "semantic-changed"})
```

- [ ] **Step 3: Append failing tests**

```python
class TestSemanticScope:
    def test_toggle_project_to_general(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/semantic/m1/scope", data={"scope": "general"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT scope FROM semantic_memories WHERE id='m1'"
            ).fetchone()
        assert row[0] == "general"

    def test_missing_id_returns_400(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic/ghost/scope", data={"scope": "general"},
        )
        assert response.status_code == 400


class TestSemanticDelete:
    def test_delete_removes_row(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post("/semantic/m1/delete")
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            count = check.execute(
                "SELECT COUNT(*) FROM semantic_memories"
            ).fetchone()[0]
        assert count == 0

    def test_delete_missing_is_idempotent(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post("/semantic/ghost/delete")
        assert response.status_code == 200
```

- [ ] **Step 4: Run, verify scope + delete tests pass**

```bash
uv run pytest tests/ui/test_semantic.py::TestSemanticScope tests/ui/test_semantic.py::TestSemanticDelete -v
```
Expected: 4 passed.

### Task 7: Edit drawer + update route

**Confidence:** 93% — drawer is a familiar pattern; the only nuance is the htmx response after a successful update should clear the drawer + trigger panel reload.

**Files:**
- Create: `better_memory/ui/templates/fragments/semantic_drawer.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_semantic.py`

- [ ] **Step 1: Create the drawer template**

`better_memory/ui/templates/fragments/semantic_drawer.html`:

```jinja
<div class="semantic-drawer" id="semantic-drawer-{{ memory.id }}">
  <header class="drawer-header">
    <h3>Edit semantic memory</h3>
    <button class="close-drawer"
            type="button"
            onclick="document.getElementById('semantic-drawer').innerHTML = '';">
      ×
    </button>
  </header>

  <form hx-post="{{ url_for('semantic_update', id=memory.id) }}"
        hx-target="#semantic-drawer"
        hx-swap="innerHTML">
    <label>
      Content
      <textarea name="content" rows="4" required>{{ memory.content }}</textarea>
    </label>
    <div class="drawer-meta">
      <span>Project: {{ memory.project }}</span>
      <span>Scope: <strong>{{ memory.scope }}</strong></span>
      <span>Updated: {{ memory.updated_at }}</span>
    </div>
    <div class="drawer-actions">
      <button type="submit">Save</button>
      <button type="button"
              class="cancel"
              onclick="document.getElementById('semantic-drawer').innerHTML = '';">
        Cancel
      </button>
    </div>
  </form>
</div>
```

- [ ] **Step 2: Add drawer + update routes to `app.py`**

```python
    @app.get("/semantic/<id>/drawer")
    def semantic_drawer(id: str):
        from better_memory.services.semantic import SemanticMemoryService
        conn = app.extensions["db_connection"]
        svc = SemanticMemoryService(conn)
        # Fetch the single row directly — list_for_project + filter is overkill.
        row = conn.execute(
            "SELECT id, content, project, scope, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            abort(404)
        memory = {
            "id": row["id"], "content": row["content"],
            "project": row["project"], "scope": row["scope"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        return render_template(
            "fragments/semantic_drawer.html", memory=memory,
        )

    @app.post("/semantic/<id>/update")
    def semantic_update(id: str) -> tuple[str, int, dict[str, str]]:
        from better_memory.services.semantic import SemanticMemoryService
        from markupsafe import escape
        conn = app.extensions["db_connection"]
        content = request.form.get("content", "").strip()
        svc = SemanticMemoryService(conn)
        try:
            svc.update_text(id=id, content=content)
        except ValueError as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})
```

(`abort` is already imported at the top of `app.py`; confirm via grep.)

- [ ] **Step 3: Append failing tests**

```python
class TestSemanticDrawer:
    def test_drawer_renders_edit_form_with_current_values(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','existing rule','proj-a','general',"
                " '2026-05-01T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/m1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "existing rule" in body
        assert "general" in body
        assert "proj-a" in body

    def test_drawer_returns_404_for_missing(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.get("/semantic/ghost/drawer")
        assert response.status_code == 404


class TestSemanticUpdate:
    def test_update_changes_content(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','old text','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/semantic/m1/update", data={"content": "new text"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT content FROM semantic_memories WHERE id='m1'"
            ).fetchone()
        assert row[0] == "new text"

    def test_update_empty_content_returns_400(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic/anything/update", data={"content": "   "},
        )
        assert response.status_code == 400
```

- [ ] **Step 4: Run, verify drawer + update tests pass**

```bash
uv run pytest tests/ui/test_semantic.py::TestSemanticDrawer tests/ui/test_semantic.py::TestSemanticUpdate -v
```
Expected: 4 passed.

### Task 8: Verify + Commit 2

**Confidence:** 99%.

- [ ] **Step 1: Full UI test suite green**

```bash
uv run pytest tests/ui/ -q
```
Expected: full green; ~12 new semantic tests added.

- [ ] **Step 2: Pyright clean**

```bash
uv run pyright
```
Expected: 0 errors.

- [ ] **Step 3: Full pytest green**

```bash
uv run pytest -q
```
Expected: full green.

- [ ] **Step 4: Manual smoke** (optional but recommended)

Start the UI: `uv run python -m better_memory.ui`. Visit `/semantic`, click "Add memory", confirm a row appears, click Edit, change content, click "Make general", click Delete. Verify all htmx triggers refresh the panel.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/base.html \
        better_memory/ui/templates/semantic.html \
        better_memory/ui/templates/fragments/panel_semantic.html \
        better_memory/ui/templates/fragments/semantic_create_form.html \
        better_memory/ui/templates/fragments/semantic_row.html \
        better_memory/ui/templates/fragments/semantic_drawer.html \
        tests/ui/test_semantic.py
git commit -m "$(cat <<'EOF'
feat(ui): /semantic panel with create/edit/scope-toggle/delete

Adds a Semantic memories tab to The Archive UI, mirroring the
Reflections panel pattern (page → htmx-loaded panel → row cards →
edit drawer). New routes:

- GET /semantic                  — top-level page
- GET /semantic/panel            — htmx fragment with create form + rows
- POST /semantic                 — create memory
- GET /semantic/<id>/drawer      — edit form
- POST /semantic/<id>/update     — apply content edit
- POST /semantic/<id>/scope      — toggle project↔general
- POST /semantic/<id>/delete     — hard delete (idempotent)

All non-GET routes return HX-Trigger: semantic-changed; the panel
re-fetches via the existing 'semantic-changed from:body' trigger
pattern (same as reflections-changed).

Nav-rail gains a Semantic entry between Reflections and Diagnostics.

Promote-from-observation drawer integration follows in the next
commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Commit 3 — Promote-from-observation drawer integration

### Task 9: Promote route + confirmation card (TDD)

**Confidence:** 93%.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/observation_promoted_card.html`
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Append failing tests to `tests/ui/test_observations.py`**

```python
class TestPromoteToSemantic:
    def _seed_active_observation(self, conn, *, obs_id="o1", project="proj-a"):
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', ?, '2026-04-01T00:00:00+00:00')",
            (project,),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "(?, 'durable rule', ?, 'ep1', 'active', 'success',"
            " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, project),
        )
        conn.commit()

    def test_promote_creates_memory_and_flips_status(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            self._seed_active_observation(seed_conn, obs_id="o1")
        response = client.post(
            "/observations/o1/promote-to-semantic",
            data={"scope": "general"},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Confirmation card mentions the chosen scope.
        assert "general" in body
        # HX-Trigger fires both panels' refresh events.
        trigger = response.headers.get("HX-Trigger") or ""
        assert "observations-changed" in trigger
        assert "semantic-changed" in trigger
        # New semantic memory exists with the observation's content.
        with sqlite3.connect(tmp_db) as check:
            mem = check.execute(
                "SELECT content, scope, project FROM semantic_memories"
            ).fetchone()
            obs = check.execute(
                "SELECT status FROM observations WHERE id='o1'"
            ).fetchone()
        assert mem[0] == "durable rule"
        assert mem[1] == "general"
        assert mem[2] == "proj-a"
        assert obs[0] == "consumed_without_reflection"

    def test_promote_default_scope_is_project(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            self._seed_active_observation(seed_conn, obs_id="o1")
        response = client.post("/observations/o1/promote-to-semantic")
        assert response.status_code == 200
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT scope FROM semantic_memories"
            ).fetchone()
        assert row[0] == "project"

    def test_promote_already_consumed_returns_400(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO episodes (id, project, started_at) VALUES "
                "('ep1', 'proj-a', '2026-04-01T00:00:00+00:00')"
            )
            seed_conn.execute(
                "INSERT INTO observations (id, content, project, episode_id, "
                "status, outcome, created_at, status_changed_at) VALUES "
                "('o1','x','proj-a','ep1','consumed_into_reflection','success',"
                " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post("/observations/o1/promote-to-semantic")
        assert response.status_code == 400
        body = response.get_data(as_text=True)
        assert "card-error" in body

    def test_promote_missing_observation_returns_400(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post("/observations/ghost/promote-to-semantic")
        assert response.status_code == 400
```

- [ ] **Step 2: Run, verify 4 fail**

```bash
uv run pytest tests/ui/test_observations.py::TestPromoteToSemantic -v
```
Expected: 4 errors with `Not Found: /observations/o1/promote-to-semantic`.

- [ ] **Step 3: Create the confirmation card template**

`better_memory/ui/templates/fragments/observation_promoted_card.html`:

```jinja
<div class="card card-success observation-promoted">
  <p>Promoted as <strong>{{ scope }}</strong> semantic memory.
     <a href="{{ url_for('semantic') }}">View in Semantic memories →</a></p>
</div>
```

- [ ] **Step 4: Add the promote route to `app.py`**

Find an appropriate place near the existing `/observations/<id>/drawer` route and append:

```python
    @app.post("/observations/<id>/promote-to-semantic")
    def observation_promote_to_semantic(
        id: str,
    ) -> tuple[str, int, dict[str, str]]:
        from better_memory.services.semantic import SemanticMemoryService
        from markupsafe import escape
        conn = app.extensions["db_connection"]
        scope = request.form.get("scope") or "project"
        svc = SemanticMemoryService(conn)
        try:
            memory_id = svc.create_from_observation(
                observation_id=id, scope=scope,
            )
        except ValueError as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        rendered = render_template(
            "fragments/observation_promoted_card.html",
            memory_id=memory_id, scope=scope,
        )
        return (
            rendered, 200,
            {"HX-Trigger": "observations-changed semantic-changed"},
        )
```

- [ ] **Step 5: Run, verify 4 pass**

```bash
uv run pytest tests/ui/test_observations.py::TestPromoteToSemantic -v
```
Expected: 4 passed.

### Task 10: Drawer template promote form (TDD)

**Confidence:** 95%.

**Files:**
- Modify: `better_memory/ui/templates/fragments/observation_drawer.html`
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Append failing template tests**

```python
class TestObservationDrawerPromoteForm:
    def _drawer_for(self, client, conn, obs_id, status):
        # Helper: seed an observation with the given status, return drawer HTML.
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', 'proj-a', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, "
            "status, outcome, created_at, status_changed_at) VALUES "
            "(?, 'rule text', 'proj-a', 'ep1', ?, 'success',"
            " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, status),
        )
        conn.commit()
        return client.get(f"/observations/{obs_id}/drawer").get_data(as_text=True)

    def test_drawer_shows_promote_form_when_active(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            body = self._drawer_for(client, seed_conn, "o1", "active")
        assert "promote-to-semantic" in body
        # Form has a scope select.
        assert 'name="scope"' in body
        assert 'value="project"' in body
        assert 'value="general"' in body

    def test_drawer_hides_promote_form_when_consumed(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            body = self._drawer_for(client, seed_conn, "o1", "consumed_into_reflection")
        assert "promote-to-semantic" not in body
```

- [ ] **Step 2: Run, verify 2 fail**

```bash
uv run pytest tests/ui/test_observations.py::TestObservationDrawerPromoteForm -v
```
Expected: both fail (the template doesn't render the form yet).

- [ ] **Step 3: Update `better_memory/ui/templates/fragments/observation_drawer.html`**

Find the bottom of the template (before the `{% if detail.audit %}` block, so the promote form lives between the linked-reflections and audit sections). Insert:

```jinja
  {% if detail.observation.status == 'active' %}
    <section class="promote-section">
      <h4>Promote to semantic memory</h4>
      <form hx-post="{{ url_for('observation_promote_to_semantic', id=detail.observation.id) }}"
            hx-target="closest .drawer"
            hx-swap="outerHTML"
            class="promote-form">
        <select name="scope">
          <option value="project" selected>project</option>
          <option value="general">general</option>
        </select>
        <button type="submit">Promote</button>
      </form>
    </section>
  {% endif %}
```

- [ ] **Step 4: Run, verify 2 pass**

```bash
uv run pytest tests/ui/test_observations.py::TestObservationDrawerPromoteForm -v
```
Expected: 2 passed.

### Task 11: Verify + Commit 3

**Confidence:** 99%.

- [ ] **Step 1: Full pytest green**

```bash
uv run pytest -q
```
Expected: full green; ~16+ new tests on top of Commit 2.

- [ ] **Step 2: Pyright clean**

```bash
uv run pyright
```
Expected: 0 errors.

- [ ] **Step 3: Pre-flight grep**

```bash
grep -rE "promote-to-semantic|create_from_observation|set_scope" better_memory/ tests/
```

Expected hits:
- `better_memory/services/semantic.py` (the methods)
- `better_memory/ui/app.py` (the routes)
- `better_memory/ui/templates/fragments/observation_drawer.html` (the form)
- `better_memory/ui/templates/fragments/observation_promoted_card.html` (confirmation)
- `better_memory/ui/templates/fragments/semantic_row.html` (scope toggle button URL)
- `tests/services/test_semantic.py`, `tests/ui/test_semantic.py`, `tests/ui/test_observations.py`

No stray hits.

- [ ] **Step 4: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/observation_drawer.html \
        better_memory/ui/templates/fragments/observation_promoted_card.html \
        tests/ui/test_observations.py
git commit -m "$(cat <<'EOF'
feat(ui): promote observation to semantic memory from the drawer

Adds a "Promote to semantic memory" form to the observation drawer
(visible only for status='active' observations). On submit, calls
SemanticMemoryService.create_from_observation in a single SAVEPOINT
envelope: insert new semantic_memories row + flip observation status
to 'consumed_without_reflection'.

New route: POST /observations/<id>/promote-to-semantic
  - Returns 200 with a confirmation card on success
  - Returns 400 with card-error if observation is missing or already
    consumed
  - HX-Trigger: observations-changed semantic-changed (refreshes both
    panels if either is open)

Drawer template gains a small section that's hidden once the
observation has been consumed by any path (synthesis or promotion),
preventing accidental double-promotes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Full test suite + pyright green**

```bash
uv run pytest -q
uv run pyright
```

- [ ] **Step 2: Smoke run** (manual)

1. Start UI: `uv run python -m better_memory.ui`. Verify nav-rail has 5 entries (Episodes / Observations / Reflections / Semantic / Diagnostics).
2. Visit `/semantic`. Empty state shows.
3. Add a memory via the create form. Row appears.
4. Click Edit, change content, save. Drawer closes; row updates.
5. Click "Make general". Scope badge changes; button label flips.
6. Click Delete. Row disappears; empty state may or may not return depending on row count.
7. Visit `/observations`. Click an active observation's row. Drawer opens.
8. Click "Promote" with scope=general. Confirmation card appears with link to /semantic.
9. Visit `/semantic`. Newly-promoted memory is at the top.
10. Visit `/observations`. Promoted observation now shows status=consumed_without_reflection.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin semantic-memories-ui
gh pr create --title "Semantic memories UI tab + promote-from-observation" --body "$(cat <<'EOF'
## Summary

- Adds a Semantic memories tab to The Archive UI: list, create, edit, scope-toggle (project ↔ general), delete.
- Adds a "Promote to semantic memory" form to the observation drawer for active observations — atomically creates the semantic memory and flips the observation's status to consumed_without_reflection.
- Three commits: service additions (set_scope + create_from_observation) → /semantic panel UI → promote-from-observation drawer integration.

## Why

PR #34 made semantic memories first-class data via MCP tools but with no UI affordance. This PR puts them on screen and gives users a way to elevate observations into durable rules without manual MCP gymnastics.

Spec: \`docs/superpowers/specs/2026-05-04-semantic-memories-ui-design.md\`
Plan: \`docs/superpowers/plans/2026-05-04-semantic-memories-ui.md\`

## Out of scope (filed as issues)

- Filter / search on the panel — issue #36
- Horizontal-tab nav redesign — issue #35

## Test Plan

- [x] 12 new service tests (set_scope, create_from_observation paths)
- [x] 12 new UI route tests for /semantic
- [x] 6 new tests for the observation drawer promote form + route
- [x] Full pytest green
- [x] \`uv run pyright\` clean (0 errors)
- [ ] Manual smoke through the UI

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Per-step confidence summary (per the user's reflection rule)

All 11 task headers carry an explicit confidence rating. Steps within tasks below 95% have inline mitigation notes.

| Task | Confidence | Notes |
|---|---|---|
| 1. set_scope (TDD) | 95% | Direct analog of update_text including the rollback regression test. |
| 2. create_from_observation (TDD) | 90% | Multi-step orchestration with SAVEPOINT envelope; tests cover all 5 paths including atomic-on-failure via monkeypatch. |
| 3. Verify + Commit 1 | 99% | |
| 4. Page route + nav-rail entry | 95% | Direct port of reflections page pattern. |
| 5. Panel route + create form | 92% | Multi-template interaction; tests assert on rendered HTML structure. |
| 6. Row buttons + scope toggle + delete | 92% | htmx wiring (target/swap) needs care; same-conn `HX-Trigger` pattern as reflections. |
| 7. Edit drawer + update route | 93% | |
| 8. Verify + Commit 2 | 99% | |
| 9. Promote route + confirmation card | 93% | |
| 10. Drawer template promote form | 95% | |
| 11. Verify + Commit 3 | 99% | |

**No tasks below 90%.** Lowest is 90% (Task 2: create_from_observation), with explicit mitigation notes inline. The plan satisfies the per-step-confidence rule.

**Compound estimate:** ~78-83% the PR lands without a reviewer-driven fix-commit cycle. Slightly higher than PR #34's because (a) all-new code with no surgical removals, (b) the data model is parallel to PR #34's (already battle-tested), (c) the promote-from-observation flow has only one orchestrator method, well-tested.

---

## Self-review

**Spec coverage:**
- Service additions (set_scope, create_from_observation): Tasks 1-2 ✓
- /semantic page route + nav-rail entry: Task 4 ✓
- Panel template + create form: Task 5 ✓
- Row template with edit/scope/delete buttons: Task 6 ✓
- Edit drawer + update route: Task 7 ✓
- Promote route + confirmation card: Task 9 ✓
- Observation drawer promote form (active-only): Task 10 ✓
- HX-Trigger refresh patterns: Tasks 5, 6, 7, 9 ✓
- Atomicity-on-failure for create_from_observation: Task 2 (with monkeypatch test) ✓
- Scope-toggle button label rule (project→make-general, general→make-project): Task 6 ✓
- Test plan for service / panel / drawer / promote: Tasks 1, 2, 5, 6, 7, 9, 10 ✓

**Placeholder scan:** none. Every task has files, code, test code, and run/expect lines. The "minimal `semantic_row.html` placeholder" in Task 5 is fully specified — Task 6 then replaces it with the full version, both shown verbatim.

**Type consistency:** `SemanticMemoryService.set_scope(*, id, scope)` and `create_from_observation(*, observation_id, scope)` signatures are referenced consistently. Route names `semantic_panel` / `semantic_create` / `semantic_drawer` / `semantic_update` / `semantic_scope` / `semantic_delete` / `observation_promote_to_semantic` are referenced consistently in templates' `url_for` calls and in tests' URL strings. `HX-Trigger: semantic-changed` is the single event name used by the panel listener and all mutation routes.

**Scope:** three commits, ~270 LOC code + ~480 LOC tests across 11 files (5 templates created, 2 modified, 4 test files). Tractable for a single subagent-driven cycle.
