# Observations View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth UI tab `Observations` that lists raw observations for the current project with status/outcome/component filters and a single button that triggers project-wide LLM synthesis.

**Architecture:** Mirrors the existing Reflections tab. Tab page (`observations.html`) → HTMX panel (`/observations/panel`) → drawer (`/observations/<id>/drawer`). One write route (`POST /observations/synthesize`) calls the existing `ReflectionSynthesisService.synthesize` with `goal="manual synthesis"`, `tech=None`, blocking the request until the LLM returns. Read shapes live in `better_memory/ui/queries.py`; the synthesis service gets wired into `app.extensions` in `create_app`.

**Tech Stack:** Flask + Jinja2 + HTMX (already in the project). `urllib`/`asyncio` stdlib (already used by `mcp/server.py`). pytest + Flask test client + Playwright (existing test infrastructure). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-28-observations-view-design.md`

---

## Confidence and Risk Register

| Task | Confidence | Risk | Mitigation built into the task |
|---|---|---|---|
| 1 — `observation_list_for_ui` | 98% | — | none |
| 2 — `observation_detail` | 95% | — | none |
| 3 — tab page + filter form | 95% | — | none |
| 4 — wire `ReflectionSynthesisService` | **85%** | `ReflectionSynthesisService` requires an `OllamaChat` client; `create_app` doesn't currently build one. The wiring may surface unexpected import or config-resolution issues. | Step 0 reads `mcp/server.py:422-450` to copy the wiring shape verbatim; step uses `get_config()` like the existing pattern. |
| 5 — panel route + row template | 95% | — | none |
| 6 — drawer route + template | 95% | — | none |
| 7 — nav tab | 99% | — | none |
| 8 — synthesis trigger | **80%** | `asyncio.run` inside a sync Flask route is a well-known bridge but easy to get wrong on Windows (the default event loop policy differs). Errors from `synthesize` need to surface as 500 + card-error not propagate. | Test patches `synthesize` and asserts both the success path and the error path. Implementation uses bare `asyncio.run` (matches the project's `mcp/server.py` pattern). |
| 9 — browser tests | **75%** | Browser test depends on Playwright fixtures already wired in `tests/ui/test_browser_*.py`. Stubbing the LLM cleanly across the live-server subprocess is the open question. | Step 1 reads `test_browser_reflections.py` first to confirm the fixture shape; if `monkeypatch` doesn't cross the subprocess, the test seeds the DB so the synthesis call short-circuits naturally. The synthesis-trigger UI flow is not exercised in this task — Task 8's Flask test-client tests cover it. |
| 10 — final integration check | 95% | — | none |

### Process applied for low-confidence items

- **Verify-before-commit.** Read source code (`mcp/server.py`'s service-wiring lines, the existing reflections route shapes) before writing the step that touches it.
- **Match existing patterns exactly.** New routes use the same return shape, error handling, and `HX-Trigger` event names as their reflection counterparts.
- **Stub at the highest layer that gives confidence.** Patching `synthesize` rather than the LLM client itself avoids reproducing context-loading and prompt-building in the test setup.

---

## File Structure

### Create

```
better_memory/ui/templates/observations.html
better_memory/ui/templates/fragments/observation_filter_form.html
better_memory/ui/templates/fragments/panel_observations.html
better_memory/ui/templates/fragments/observation_row.html
better_memory/ui/templates/fragments/observation_drawer.html
better_memory/ui/templates/fragments/observations_synth_banner.html
tests/ui/test_observations.py
tests/ui/test_queries_observations.py
tests/ui/test_browser_observations.py
```

### Modify

- `better_memory/ui/queries.py` — add `ObservationRow`, `ObservationFull`, `LinkedReflectionRow`, `ObservationAuditEntry`, `ObservationDetail`, `observation_list_for_ui`, `observation_detail`.
- `better_memory/ui/app.py` — wire `ReflectionSynthesisService` into `app.extensions`; add four routes (`/observations`, `/observations/panel`, `/observations/<id>/drawer`, `/observations/synthesize`).
- `better_memory/ui/templates/base.html` — add the third nav tab between Episodes and Reflections.

---

## Task 1: `observation_list_for_ui` query [confidence: 98%]

**Files:**
- Modify: `better_memory/ui/queries.py`
- Create: `tests/ui/test_queries_observations.py`

Adds the panel-row data shape and a single-SELECT helper with optional filters.

- [ ] **Step 1: Write the failing test**

Create `tests/ui/test_queries_observations.py`:

```python
"""Tests for observation-related UI query helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.queries import (
    ObservationRow,
    observation_list_for_ui,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_episode(conn, *, eid: str = "ep-1", project: str = "proj-a") -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at) "
        "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
        (eid, project),
    )


def _seed_obs(
    conn,
    *,
    oid: str,
    project: str = "proj-a",
    component: str | None = "ui_launcher",
    theme: str | None = "bug",
    outcome: str = "neutral",
    status: str = "active",
    content: str = "test obs",
    episode_id: str = "ep-1",
    created_at: str = "2026-04-26T10:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, component, theme, outcome, status, "
        " episode_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            oid, content, project, component, theme, outcome, status,
            episode_id, created_at,
        ),
    )
    conn.commit()


class TestObservationListForUi:
    def test_returns_empty_when_no_observations(self, conn):
        rows = observation_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_all_when_no_filters(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        _seed_obs(conn, oid="o-2")

        rows = observation_list_for_ui(conn, project="proj-a")
        ids = {r.id for r in rows}
        assert ids == {"o-1", "o-2"}

    def test_filters_by_project(self, conn):
        _seed_episode(conn, eid="ep-a", project="proj-a")
        _seed_episode(conn, eid="ep-b", project="proj-b")
        _seed_obs(conn, oid="o-a", project="proj-a", episode_id="ep-a")
        _seed_obs(conn, oid="o-b", project="proj-b", episode_id="ep-b")

        rows = observation_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["o-a"]

    def test_filters_by_status(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-active", status="active")
        _seed_obs(conn, oid="o-archived", status="archived")

        rows = observation_list_for_ui(
            conn, project="proj-a", status="active"
        )
        assert [r.id for r in rows] == ["o-active"]

    def test_filters_by_outcome(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-fail", outcome="failure")
        _seed_obs(conn, oid="o-ok", outcome="success")

        rows = observation_list_for_ui(
            conn, project="proj-a", outcome="failure"
        )
        assert [r.id for r in rows] == ["o-fail"]

    def test_filters_by_component(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-ui", component="ui_launcher")
        _seed_obs(conn, oid="o-mcp", component="mcp")

        rows = observation_list_for_ui(
            conn, project="proj-a", component="ui_launcher"
        )
        assert [r.id for r in rows] == ["o-ui"]

    def test_orders_newest_first(self, conn):
        _seed_episode(conn)
        _seed_obs(
            conn, oid="o-old", created_at="2026-04-25T10:00:00+00:00"
        )
        _seed_obs(
            conn, oid="o-new", created_at="2026-04-26T10:00:00+00:00"
        )

        rows = observation_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["o-new", "o-old"]

    def test_respects_limit(self, conn):
        _seed_episode(conn)
        for i in range(5):
            _seed_obs(
                conn,
                oid=f"o-{i}",
                created_at=f"2026-04-26T10:00:0{i}+00:00",
            )

        rows = observation_list_for_ui(conn, project="proj-a", limit=3)
        assert len(rows) == 3

    def test_row_shape_matches_dataclass(self, conn):
        _seed_episode(conn)
        _seed_obs(
            conn,
            oid="o-1",
            content="hello",
            component="ui_launcher",
            theme="bug",
            outcome="failure",
            status="active",
            created_at="2026-04-26T10:00:00+00:00",
        )

        [row] = observation_list_for_ui(conn, project="proj-a")
        assert isinstance(row, ObservationRow)
        assert row.id == "o-1"
        assert row.content == "hello"
        assert row.component == "ui_launcher"
        assert row.theme == "bug"
        assert row.outcome == "failure"
        assert row.status == "active"
        assert row.episode_id == "ep-1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/ui/test_queries_observations.py -v`
Expected: FAIL with `ImportError: cannot import name 'ObservationRow'`.

- [ ] **Step 3: Implement `ObservationRow` and the query**

Add to `better_memory/ui/queries.py` (append after the existing reflection queries):

```python
@dataclass(frozen=True)
class ObservationRow:
    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    status: str
    created_at: str
    episode_id: str | None


def observation_list_for_ui(
    conn: sqlite3.Connection,
    *,
    project: str,
    status: str | None = None,
    outcome: str | None = None,
    component: str | None = None,
    limit: int = 100,
) -> list[ObservationRow]:
    """Project-scoped observation list with optional filters. Newest first.

    No filter defaults: omitting ``status``/``outcome``/``component``
    returns observations across all values for that column. The panel
    shows everything on first load (filters are user-driven).
    """
    clauses = ["project = ?"]
    params: list = [project]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    if component is not None:
        clauses.append("component = ?")
        params.append(component)
    where = " AND ".join(clauses)
    sql = (
        "SELECT id, content, component, theme, outcome, status, "
        "       created_at, episode_id "
        "FROM observations "
        f"WHERE {where} "
        "ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?"
    )
    params.append(limit)
    return [
        ObservationRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            status=r["status"],
            created_at=r["created_at"],
            episode_id=r["episode_id"],
        )
        for r in conn.execute(sql, params).fetchall()
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/ui/test_queries_observations.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check better_memory/ui/queries.py tests/ui/test_queries_observations.py`
Expected: 0 issues.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_observations.py
git commit -m "feat(ui/queries): observation_list_for_ui with status/outcome/component filters"
```

---

## Task 2: `observation_detail` query [confidence: 95%]

**Files:**
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_queries_observations.py`

Adds the drawer data shape: full observation row + audit timeline + linked reflections.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_queries_observations.py`:

```python
class TestObservationDetail:
    def test_returns_none_for_unknown_id(self, conn):
        from better_memory.ui.queries import observation_detail

        result = observation_detail(conn, observation_id="nope")
        assert result is None

    def test_returns_full_observation(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, tech, trigger_type, reinforcement_score, "
            " created_at) "
            "VALUES "
            "('o-1', 'hello', 'proj-a', 'ui_launcher', 'bug', 'failure', "
            " 'active', 'ep-1', 'python', 'review', 1.5, "
            " '2026-04-26T10:00:00+00:00')"
        )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert detail.observation.id == "o-1"
        assert detail.observation.content == "hello"
        assert detail.observation.project == "proj-a"
        assert detail.observation.component == "ui_launcher"
        assert detail.observation.theme == "bug"
        assert detail.observation.outcome == "failure"
        assert detail.observation.status == "active"
        assert detail.observation.tech == "python"
        assert detail.observation.trigger_type == "review"
        assert detail.observation.reinforcement_score == 1.5
        assert detail.observation.episode_id == "ep-1"
        assert detail.audit == []
        assert detail.reflections == []

    def test_returns_audit_timeline_newest_first(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        for at in (
            "2026-04-26T10:00:00+00:00",
            "2026-04-26T11:00:00+00:00",
            "2026-04-26T12:00:00+00:00",
        ):
            conn.execute(
                "INSERT INTO audit_log "
                "(id, entity_type, entity_id, action, actor, created_at) "
                "VALUES (?, 'observation', 'o-1', 'create', 'ai', ?)",
                (f"a-{at}", at),
            )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert len(detail.audit) == 3
        # Newest first.
        ats = [e.at for e in detail.audit]
        assert ats == sorted(ats, reverse=True)

    def test_returns_linked_reflections(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            " confidence, status, evidence_count, created_at, updated_at) "
            "VALUES "
            "('r-1', 'Linked', 'proj-a', NULL, 'general', 'do', "
            " 'uc', 'h', 0.8, 'confirmed', 1, "
            " '2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'o-1')"
        )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert len(detail.reflections) == 1
        assert detail.reflections[0].id == "r-1"
        assert detail.reflections[0].title == "Linked"
        assert detail.reflections[0].polarity == "do"
        assert detail.reflections[0].confidence == 0.8
        assert detail.reflections[0].status == "confirmed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/ui/test_queries_observations.py::TestObservationDetail -v`
Expected: FAIL with `ImportError: cannot import name 'observation_detail'`.

- [ ] **Step 3: Implement `ObservationFull`, `LinkedReflectionRow`, `ObservationAuditEntry`, `ObservationDetail`, `observation_detail`**

Append to `better_memory/ui/queries.py`:

```python
@dataclass(frozen=True)
class ObservationFull:
    """All columns from observations that the drawer renders."""
    id: str
    content: str
    project: str
    component: str | None
    theme: str | None
    tech: str | None
    trigger_type: str | None
    outcome: str
    status: str
    reinforcement_score: float
    episode_id: str | None
    created_at: str


@dataclass(frozen=True)
class LinkedReflectionRow:
    """One reflection that cites the observation under inspection."""
    id: str
    title: str
    polarity: str
    confidence: float
    status: str


@dataclass(frozen=True)
class ObservationAuditEntry:
    at: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None


@dataclass(frozen=True)
class ObservationDetail:
    observation: ObservationFull
    audit: list[ObservationAuditEntry]
    reflections: list[LinkedReflectionRow]


def observation_detail(
    conn: sqlite3.Connection, *, observation_id: str
) -> ObservationDetail | None:
    """Return one observation with audit + linked reflections, or None."""
    obs_row = conn.execute(
        "SELECT id, content, project, component, theme, tech, "
        "       trigger_type, outcome, status, reinforcement_score, "
        "       episode_id, created_at "
        "FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    if obs_row is None:
        return None

    observation = ObservationFull(
        id=obs_row["id"],
        content=obs_row["content"],
        project=obs_row["project"],
        component=obs_row["component"],
        theme=obs_row["theme"],
        tech=obs_row["tech"],
        trigger_type=obs_row["trigger_type"],
        outcome=obs_row["outcome"],
        status=obs_row["status"],
        reinforcement_score=obs_row["reinforcement_score"],
        episode_id=obs_row["episode_id"],
        created_at=obs_row["created_at"],
    )

    audit_rows = conn.execute(
        # audit_log's timestamp column is `created_at` (per 0001_init.sql);
        # alias to `at` so the dataclass field name stays short.
        "SELECT created_at AS at, actor, action, from_status, to_status "
        "FROM audit_log "
        "WHERE entity_type = 'observation' AND entity_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (observation_id,),
    ).fetchall()
    audit = [
        ObservationAuditEntry(
            at=r["at"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
        )
        for r in audit_rows
    ]

    refl_rows = conn.execute(
        """
        SELECT r.id, r.title, r.polarity, r.confidence, r.status
        FROM reflections r
        JOIN reflection_sources rs ON rs.reflection_id = r.id
        WHERE rs.observation_id = ?
        ORDER BY r.confidence DESC, r.id ASC
        """,
        (observation_id,),
    ).fetchall()
    reflections = [
        LinkedReflectionRow(
            id=r["id"],
            title=r["title"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
        )
        for r in refl_rows
    ]

    return ObservationDetail(
        observation=observation,
        audit=audit,
        reflections=reflections,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/ui/test_queries_observations.py -v`
Expected: all tests PASS (9 from Task 1 + 4 new = 13).

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check better_memory/ui/queries.py tests/ui/test_queries_observations.py`
Expected: 0 issues.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_observations.py
git commit -m "feat(ui/queries): observation_detail with audit timeline and linked reflections"
```

---

## Task 3: `/observations` tab page route + filter form template [confidence: 95%]

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/observations.html`
- Create: `better_memory/ui/templates/fragments/observation_filter_form.html`
- Create: `tests/ui/test_observations.py`

The empty tab page that holds the filter form and the panel target.

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_observations.py`:

```python
"""Flask test-client tests for the Observations tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect


def _seed_episode(
    db_path: Path, *, eid: str = "ep-1", project: str = "proj-a"
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
            (eid, project),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_obs(
    db_path: Path,
    *,
    oid: str,
    project: str = "proj-a",
    component: str | None = "ui_launcher",
    theme: str | None = "bug",
    outcome: str = "neutral",
    status: str = "active",
    content: str = "test obs",
    episode_id: str = "ep-1",
    created_at: str = "2026-04-26T10:00:00+00:00",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, content, project, component, theme, outcome, status,
             episode_id, created_at),
        )
        conn.commit()
    finally:
        conn.close()


class TestObservationsPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/observations")
        assert response.status_code == 200

    def test_renders_filter_form(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert 'name="status"' in body
        assert 'name="outcome"' in body
        assert 'name="component"' in body

    def test_renders_run_synthesis_button(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert "Run synthesis" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsPage -v`
Expected: FAIL with 404 (route does not exist).

- [ ] **Step 3: Add the templates**

Create `better_memory/ui/templates/observations.html`:

```jinja
{% extends "base.html" %}
{% block title %}Observations — better-memory{% endblock %}
{% block main %}
<section class="observations">
  {% include "fragments/observation_filter_form.html" %}

  <div id="observation-panel"
       hx-get="{{ url_for('observations_panel') }}"
       hx-include="#observation-filter-form"
       hx-trigger="load, every 30s, observations-synthesized from:body"
       hx-swap="innerHTML">
  </div>

  <div id="observation-drawer"></div>
</section>
{% endblock %}
```

Create `better_memory/ui/templates/fragments/observation_filter_form.html`:

```jinja
<form id="observation-filter-form"
      class="filter-form"
      hx-get="{{ url_for('observations_panel') }}"
      hx-target="#observation-panel"
      hx-trigger="change from:select, change from:input">
  <label>Status
    <select name="status">
      <option value="">all</option>
      <option value="active">active</option>
      <option value="archived">archived</option>
      <option value="consumed_without_reflection">consumed</option>
    </select>
  </label>
  <label>Outcome
    <select name="outcome">
      <option value="">all</option>
      <option value="success">success</option>
      <option value="failure">failure</option>
      <option value="neutral">neutral</option>
    </select>
  </label>
  <label>Component
    <input type="text" name="component" placeholder="(any)">
  </label>
  <button type="button"
          class="primary"
          hx-post="{{ url_for('observations_synthesize') }}"
          hx-target="#observations-synth-banner"
          hx-swap="innerHTML"
          hx-indicator="#observations-synth-spinner">
    Run synthesis
    <span id="observations-synth-spinner" class="htmx-indicator">…</span>
  </button>
  <div id="observations-synth-banner"></div>
</form>
```

- [ ] **Step 4: Add the route and `_project_name` helper if needed**

Edit `better_memory/ui/app.py`. After the existing `/reflections` route block, add:

```python
    @app.get("/observations")
    def observations() -> str:
        return render_template(
            "observations.html", active_tab="observations"
        )
```

(`_project_name` already exists from the reflections work.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsPage -v`
Expected: 3 PASS.

- [ ] **Step 6: Run ruff**

Run: `uv run ruff check better_memory/ui/app.py tests/ui/test_observations.py`
Expected: 0 issues.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py \
        better_memory/ui/templates/observations.html \
        better_memory/ui/templates/fragments/observation_filter_form.html
git commit -m "feat(ui): /observations tab page with status/outcome/component filter form"
```

---

## Task 4: Wire `ReflectionSynthesisService` into `app.extensions` [confidence: 85%]

**Files:**
- Modify: `better_memory/ui/app.py`

The synthesis route in Task 8 needs the service. Wire it now so the route can use it. **Step 0 verification** below confirms the construction shape.

- [ ] **Step 0: Pre-implementation read — copy the wiring shape from `mcp/server.py`**

Read `better_memory/mcp/server.py` lines 422-450 (the `main()` factory). Confirm the construction pattern: `OllamaChat(host=config.ollama_host, model=config.consolidate_model)` then `ReflectionSynthesisService(memory_conn, chat=chat)`. The UI's `create_app` already has the connection (`db_conn`) — only `OllamaChat` and `get_config()` are new imports.

- [ ] **Step 1: Write the failing test**

Append to `tests/ui/test_observations.py`:

```python
class TestServiceWiring:
    def test_reflection_synthesis_service_is_in_app_extensions(
        self, client: FlaskClient
    ) -> None:
        # client fixture builds the app via create_app(); we read
        # extensions from the underlying app object.
        app = client.application
        assert "reflection_synthesis_service" in app.extensions
        svc = app.extensions["reflection_synthesis_service"]
        # Quack-test: it has a synthesize coroutine.
        from inspect import iscoroutinefunction
        assert iscoroutinefunction(svc.synthesize)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/ui/test_observations.py::TestServiceWiring -v`
Expected: FAIL — `assert "reflection_synthesis_service" in app.extensions` (key absent).

- [ ] **Step 3: Wire the service in `create_app`**

Edit `better_memory/ui/app.py`. At the top, add:

```python
from better_memory.config import get_config
from better_memory.llm.ollama import OllamaChat
from better_memory.services.reflection import ReflectionSynthesisService
```

Inside `create_app`, after the `app.extensions["reflection_service"] = ReflectionService(...)` line, add:

```python
    # Synthesis (Ollama LLM client + service). Construction is cheap and
    # does NOT contact Ollama; the first synthesize() call does.
    config = get_config()
    chat = OllamaChat(
        host=config.ollama_host, model=config.consolidate_model
    )
    app.extensions["reflection_synthesis_service"] = (
        ReflectionSynthesisService(db_conn, chat=chat)
    )
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/ui/test_observations.py::TestServiceWiring -v`
Expected: PASS.

- [ ] **Step 5: Run ruff and the full UI test suite (no regressions)**

Run: `uv run ruff check better_memory/ui/app.py`
Expected: 0 issues.

Run: `uv run pytest tests/ui/ -v`
Expected: all UI tests still PASS (existing reflection / episode / app tests must be unaffected).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py
git commit -m "feat(ui): wire ReflectionSynthesisService into app.extensions"
```

---

## Task 5: `/observations/panel` panel route + row template [confidence: 95%]

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/panel_observations.html`
- Create: `better_memory/ui/templates/fragments/observation_row.html`
- Modify: `tests/ui/test_observations.py`

The HTMX swap target that lists observation rows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_observations.py`:

```python
class TestObservationsPanel:
    def test_empty_state_when_no_observations(self, client: FlaskClient):
        response = client.get("/observations/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No observations" in body

    def test_renders_seeded_rows(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="hello world")
        _seed_obs(tmp_db, oid="o-2", content="second")

        response = client.get("/observations/panel?project=proj-a")
        body = response.get_data(as_text=True)
        assert "hello world" in body
        assert "second" in body

    def test_filters_by_outcome(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-fail", outcome="failure", content="bad")
        _seed_obs(tmp_db, oid="o-ok", outcome="success", content="good")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=failure"
        )
        body = response.get_data(as_text=True)
        assert "bad" in body
        assert "good" not in body

    def test_filters_by_status(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-active", status="active", content="A")
        _seed_obs(tmp_db, oid="o-arch", status="archived", content="X")

        response = client.get(
            "/observations/panel?project=proj-a&status=active"
        )
        body = response.get_data(as_text=True)
        assert "A" in body
        assert "X" not in body

    def test_filters_by_component(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-ui", component="ui_launcher", content="ui")
        _seed_obs(tmp_db, oid="o-mcp", component="mcp", content="mcp")

        response = client.get(
            "/observations/panel?project=proj-a&component=ui_launcher"
        )
        body = response.get_data(as_text=True)
        assert "ui" in body
        assert "mcp" not in body

    def test_blank_filter_values_are_treated_as_unset(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", outcome="failure", content="A")
        _seed_obs(tmp_db, oid="o-2", outcome="success", content="B")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=&status="
        )
        body = response.get_data(as_text=True)
        # Both should appear when filters are blank.
        assert "A" in body
        assert "B" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsPanel -v`
Expected: FAIL with 404.

- [ ] **Step 3: Add the templates**

Create `better_memory/ui/templates/fragments/observation_row.html`:

```jinja
<div class="card observation-row"
     hx-get="{{ url_for('observation_drawer', id=row.id) }}"
     hx-target="#observation-drawer"
     hx-swap="innerHTML">
  <span class="chip outcome-{{ row.outcome }}">{{ row.outcome }}</span>
  <span class="component">{{ row.component or "—" }}</span>
  <span class="time">{{ row.created_at[11:16] }}</span>
  <span class="content">{{ row.content }}</span>
</div>
```

Create `better_memory/ui/templates/fragments/panel_observations.html`:

```jinja
{% if days %}
  {% for day, rows in days %}
    <div class="day">
      <h3 class="day-header">{{ day }}</h3>
      {% for row in rows %}
        {% include "fragments/observation_row.html" %}
      {% endfor %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty-state"><p>No observations match these filters.</p></div>
{% endif %}
```

- [ ] **Step 4: Add the route**

Edit `better_memory/ui/app.py`. After the `observations()` view from Task 3, add:

```python
    @app.get("/observations/panel")
    def observations_panel() -> str:
        conn = app.extensions["db_connection"]
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        project = _arg("project") or _project_name()
        rows = queries.observation_list_for_ui(
            conn,
            project=project,
            status=_arg("status"),
            outcome=_arg("outcome"),
            component=_arg("component"),
        )
        from itertools import groupby

        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.created_at[:10])
        ]
        return render_template(
            "fragments/panel_observations.html", days=days
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsPanel -v`
Expected: 6 PASS.

- [ ] **Step 6: Run ruff and confirm no other regressions**

Run: `uv run ruff check better_memory/ui/app.py tests/ui/test_observations.py`
Expected: 0 issues.

Run: `uv run pytest tests/ui/ -v`
Expected: all UI tests PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py \
        better_memory/ui/templates/fragments/panel_observations.html \
        better_memory/ui/templates/fragments/observation_row.html
git commit -m "feat(ui): /observations/panel with filters, grouped by day"
```

---

## Task 6: `/observations/<id>/drawer` route + drawer template [confidence: 95%]

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/observation_drawer.html`
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_observations.py`:

```python
class TestObservationDrawer:
    def test_renders_full_content(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="full content for drawer")

        response = client.get("/observations/o-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "full content for drawer" in body

    def test_returns_404_for_unknown_id(self, client: FlaskClient):
        response = client.get("/observations/nope/drawer")
        assert response.status_code == 404

    def test_renders_metadata_grid(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        conn = connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, component, theme, outcome, status, "
                " episode_id, tech, trigger_type, reinforcement_score, "
                " created_at) "
                "VALUES "
                "('o-1', 'x', 'proj-a', 'ui_launcher', 'bug', 'failure', "
                " 'active', 'ep-1', 'python', 'review', 1.5, "
                " '2026-04-26T10:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get("/observations/o-1/drawer")
        body = response.get_data(as_text=True)
        assert "ui_launcher" in body
        assert "bug" in body
        assert "python" in body
        assert "review" in body
        assert "ep-1" in body
        # reinforcement_score appears as text
        assert "1.5" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationDrawer -v`
Expected: FAIL with 404.

- [ ] **Step 3: Add the drawer template**

Create `better_memory/ui/templates/fragments/observation_drawer.html`:

```jinja
<aside class="drawer">
  <header class="drawer-header">
    <span class="chip outcome-{{ detail.observation.outcome }}">{{ detail.observation.outcome }}</span>
    <span class="created-at">{{ detail.observation.created_at }}</span>
  </header>

  <section class="content">
    <p>{{ detail.observation.content }}</p>
  </section>

  <section class="metadata">
    <dl>
      <dt>id</dt><dd>{{ detail.observation.id }}</dd>
      <dt>component</dt><dd>{{ detail.observation.component or "—" }}</dd>
      <dt>theme</dt><dd>{{ detail.observation.theme or "—" }}</dd>
      <dt>tech</dt><dd>{{ detail.observation.tech or "—" }}</dd>
      <dt>trigger</dt><dd>{{ detail.observation.trigger_type or "—" }}</dd>
      <dt>status</dt><dd>{{ detail.observation.status }}</dd>
      <dt>reinforcement</dt><dd>{{ detail.observation.reinforcement_score }}</dd>
      <dt>episode</dt><dd>{{ detail.observation.episode_id or "—" }}</dd>
    </dl>
  </section>

  {% if detail.reflections %}
    <section class="linked-reflections">
      <h4>Linked reflections</h4>
      <ul>
        {% for r in detail.reflections %}
          <li>
            <span class="chip polarity-{{ r.polarity }}">{{ r.polarity }}</span>
            <span class="confidence">{{ "%.2f"|format(r.confidence) }}</span>
            <span class="title">{{ r.title }}</span>
            <span class="status">{{ r.status }}</span>
          </li>
        {% endfor %}
      </ul>
    </section>
  {% endif %}

  {% if detail.audit %}
    <section class="audit">
      <h4>Audit</h4>
      <ol>
        {% for entry in detail.audit %}
          <li>
            <span class="at">{{ entry.at }}</span>
            <span class="actor">{{ entry.actor }}</span>
            <span class="action">{{ entry.action }}</span>
            {% if entry.from_status or entry.to_status %}
              <span class="transition">{{ entry.from_status or "—" }} → {{ entry.to_status or "—" }}</span>
            {% endif %}
          </li>
        {% endfor %}
      </ol>
    </section>
  {% endif %}
</aside>
```

- [ ] **Step 4: Add the route**

Edit `better_memory/ui/app.py`. After `observations_panel`, add:

```python
    @app.get("/observations/<id>/drawer")
    def observation_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.observation_detail(conn, observation_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/observation_drawer.html", detail=detail
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationDrawer -v`
Expected: 3 PASS.

- [ ] **Step 6: Run ruff**

Run: `uv run ruff check better_memory/ui/app.py tests/ui/test_observations.py`
Expected: 0 issues.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py \
        better_memory/ui/templates/fragments/observation_drawer.html
git commit -m "feat(ui): /observations/<id>/drawer with metadata, linked reflections, audit"
```

---

## Task 7: Add `Observations` nav tab to `base.html` [confidence: 99%]

**Files:**
- Modify: `better_memory/ui/templates/base.html`
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/ui/test_observations.py`:

```python
class TestNavTab:
    def test_observations_tab_appears_in_base_layout(
        self, client: FlaskClient
    ):
        # Tab visible from any page that renders base.html — pick /episodes.
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert ">Observations<" in body
        assert "/observations" in body

    def test_observations_tab_marked_active_on_observations_page(
        self, client: FlaskClient
    ):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        # The nav uses class="tab active" when active_tab matches.
        # Checking for the active class on a link to /observations:
        assert 'class="tab active"' in body
        assert "Observations" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/ui/test_observations.py::TestNavTab -v`
Expected: FAIL — `Observations` text not in `/episodes` body.

- [ ] **Step 3: Add the tab to `base.html`**

Edit `better_memory/ui/templates/base.html`. Replace the `<nav class="tabs">…</nav>` block with:

```jinja
    <nav class="tabs">
      <a class="tab {% if active_tab == 'episodes' %}active{% endif %}" href="{{ url_for('episodes') }}">Episodes</a>
      <a class="tab {% if active_tab == 'observations' %}active{% endif %}" href="{{ url_for('observations') }}">Observations</a>
      <a class="tab {% if active_tab == 'reflections' %}active{% endif %}" href="{{ url_for('reflections') }}">Reflections</a>
    </nav>
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/ui/test_observations.py::TestNavTab -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/templates/base.html tests/ui/test_observations.py
git commit -m "feat(ui): add Observations nav tab between Episodes and Reflections"
```

---

## Task 8: `POST /observations/synthesize` synthesis trigger [confidence: 80%]

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/observations_synth_banner.html`
- Modify: `tests/ui/test_observations.py`

The blocking POST that calls `synthesize` and returns the banner. **Mitigation:** test patches `synthesize` so neither Ollama nor the LLM is contacted, and asserts both success and error paths.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_observations.py`:

```python
class TestObservationsSynthesize:
    def test_calls_service_and_returns_banner(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def fake_synthesize(self, *, goal, tech, project):
            assert goal == "manual synthesis"
            assert tech is None
            assert project == "proj-a"
            return {
                "do": [{"id": "r1"}, {"id": "r2"}],
                "dont": [{"id": "r3"}],
                "neutral": [],
            }

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", fake_synthesize
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == (
            "observations-synthesized"
        )
        body = response.get_data(as_text=True)
        # Banner mentions the bucket counts.
        assert "2" in body and "do" in body
        assert "1" in body and "dont" in body
        assert "0" in body and "neutral" in body

    def test_returns_500_card_error_on_service_failure(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def boom(self, *, goal, tech, project):
            raise RuntimeError("ollama unreachable")

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", boom
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "card-error" in body
        assert "ollama unreachable" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsSynthesize -v`
Expected: FAIL with 404 (route does not exist).

- [ ] **Step 3: Add the banner template**

Create `better_memory/ui/templates/fragments/observations_synth_banner.html`:

```jinja
<div class="banner banner-success">
  Synthesised:
  {{ counts.do }} do · {{ counts.dont }} dont · {{ counts.neutral }} neutral
  <a href="{{ url_for('reflections') }}">View reflections →</a>
</div>
```

- [ ] **Step 4: Add the route**

Edit `better_memory/ui/app.py`. At the top, ensure `import asyncio` is present (the file may already use it; if not, add it next to other stdlib imports). After `observation_drawer`, add:

```python
    @app.post("/observations/synthesize")
    def observations_synthesize() -> tuple[str, int, dict[str, str]]:
        project = _project_name()
        svc = app.extensions["reflection_synthesis_service"]
        try:
            buckets = asyncio.run(svc.synthesize(
                goal="manual synthesis",
                tech=None,
                project=project,
            ))
        except Exception as exc:  # noqa: BLE001 — surface to user
            return (
                f'<div class="card card-error"><p>{escape(str(exc))}</p></div>',
                500,
                {},
            )
        counts = {k: len(v) for k, v in buckets.items()}
        rendered = render_template(
            "fragments/observations_synth_banner.html", counts=counts,
        )
        return rendered, 200, {"HX-Trigger": "observations-synthesized"}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsSynthesize -v`
Expected: 2 PASS.

- [ ] **Step 6: Run ruff and the full UI test suite**

Run: `uv run ruff check better_memory/ui/app.py tests/ui/test_observations.py`
Expected: 0 issues.

Run: `uv run pytest tests/ui/ -v`
Expected: all UI tests PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py \
        better_memory/ui/templates/fragments/observations_synth_banner.html
git commit -m "feat(ui): POST /observations/synthesize triggers project-wide synthesis"
```

---

## Task 9: Browser integration test [confidence: 75%]

**Files:**
- Create: `tests/ui/test_browser_observations.py`

Mirrors `tests/ui/test_browser_reflections.py`. Smoke-tests the full HTMX flow with a Playwright browser. **Mitigation:** stub the synthesis at the Python service level (not the LLM client) so the test doesn't need Ollama. Re-uses the existing browser fixture.

- [ ] **Step 1: Read the existing browser-test pattern**

Read `tests/ui/test_browser_reflections.py` to confirm: which fixture provides `page` and `live_server_url`; how `monkeypatch` is used across the subprocess (it isn't — the existing pattern seeds the DB and uses real services). For our LLM stub, we'll use `monkeypatch` on `ReflectionSynthesisService.synthesize` *before* the live server starts; if the existing fixture forks a subprocess, this won't work and we fall back to seeding the DB so synthesis short-circuits naturally.

- [ ] **Step 2: Write the test**

Create `tests/ui/test_browser_observations.py`:

```python
"""Playwright integration tests for the Observations tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from better_memory.db.connection import connect


def _seed_episode(db_path: Path, *, eid: str = "ep-1") -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES (?, 'proj-a', '2026-04-26T10:00:00+00:00')",
            (eid,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_obs(
    db_path: Path,
    *,
    oid: str,
    content: str,
    outcome: str = "neutral",
    component: str = "ui_launcher",
    status: str = "active",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, created_at) "
            "VALUES "
            "(?, ?, 'proj-a', ?, 'bug', ?, ?, 'ep-1', "
            " '2026-04-26T10:00:00+00:00')",
            (oid, content, component, outcome, status),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
def test_observations_tab_lists_seeded_rows(
    page: Page, live_server_url: str, tmp_db: Path
):
    _seed_episode(tmp_db)
    _seed_obs(tmp_db, oid="o-1", content="visible row")

    page.goto(f"{live_server_url}/observations")
    expect(page.get_by_text("visible row")).to_be_visible()


@pytest.mark.integration
def test_filter_by_outcome_updates_panel(
    page: Page, live_server_url: str, tmp_db: Path
):
    _seed_episode(tmp_db)
    _seed_obs(tmp_db, oid="o-fail", content="bad-row", outcome="failure")
    _seed_obs(tmp_db, oid="o-ok", content="good-row", outcome="success")

    page.goto(f"{live_server_url}/observations")
    page.locator('select[name="outcome"]').select_option("failure")
    expect(page.get_by_text("bad-row")).to_be_visible()
    expect(page.get_by_text("good-row")).not_to_be_visible()


@pytest.mark.integration
def test_clicking_row_opens_drawer(
    page: Page, live_server_url: str, tmp_db: Path
):
    _seed_episode(tmp_db)
    _seed_obs(
        tmp_db, oid="o-1", content="content for drawer test",
    )

    page.goto(f"{live_server_url}/observations")
    page.get_by_text("content for drawer test").click()
    expect(page.locator("#observation-drawer")).to_contain_text(
        "content for drawer test"
    )
```

(No "Run synthesis" browser-test yet — the LLM stub plumbing is out of scope at this granularity. The Flask test-client tests in Task 8 cover that path.)

- [ ] **Step 3: Run the browser tests**

Run: `uv run pytest tests/ui/test_browser_observations.py -v -m integration`
Expected: 3 PASS (or all skip if Playwright/Chromium isn't installed locally — same skip behaviour as the existing browser tests).

- [ ] **Step 4: Run ruff**

Run: `uv run ruff check tests/ui/test_browser_observations.py`
Expected: 0 issues.

- [ ] **Step 5: Commit**

```bash
git add tests/ui/test_browser_observations.py
git commit -m "test(ui): browser integration tests for Observations tab"
```

---

## Task 10: Final integration check [confidence: 95%]

**Files:**
- (none — runs the full suite)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: all PASS. Baseline before this PR was 500 passed / 22 skipped / 3 deselected. Expect ~500 + new test count, no regressions.

- [ ] **Step 2: Manual smoke (optional)**

```bash
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
# In another shell:
curl -fsS "<the printed url>/observations" | head -20
```

Expected: HTML page with the Observations filter form. Click through manually if a browser is available.

- [ ] **Step 3: Confirm no regressions on existing tabs**

Run: `uv run pytest tests/ui/test_episodes.py tests/ui/test_reflections.py tests/ui/test_app.py -v`
Expected: all PASS — the new nav tab and service-wiring changes must not break Episodes or Reflections.

---

## Self-Review Notes

- **Spec coverage:** §3 architecture (Tasks 3–8), §4 queries (Tasks 1–2), §5 synthesis (Task 8), §6 UI layout (Tasks 3, 5, 6, 7), §7 error handling (Task 6 covers 404; Task 8 covers 500), §8 testing (every named test maps to a step), §9 out of scope (none built).
- **No placeholders:** every step contains either exact code, an exact command with expected output, or an exact text replacement.
- **Type consistency:** `ObservationRow` (Task 1) used by Task 5; `ObservationFull` (Task 2) used by Task 6's drawer; `LinkedReflectionRow`/`ObservationAuditEntry`/`ObservationDetail` (Task 2) used by Task 6. The `synthesize(*, goal, tech, project)` signature is consistent across Task 4 wiring and Task 8 use.
- **Confidence and risk applied:** every task carries a confidence percentage in the register at the top. Each of the three tasks below 90% (4, 8, 10) has its mitigation steps inside the task body — Step 0 reads, monkeypatch-on-service stubs, etc.
