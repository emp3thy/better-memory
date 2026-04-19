# Management UI — Phase 2: Pipeline Kanban Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase 1 pipeline placeholder with a working Pipeline Kanban that shows the user's real observations, candidates, insights, and promoted entries; wires up per-stage action buttons (Approve/Reject/Edit/Retire/Demote) against the existing service layer; and stubs out consolidation (Phase 3) and promotion (Phase 7) with clear deferred-feature messages.

**Architecture:** Extend the Phase 1 Flask factory to open a sqlite connection and instantiate `InsightService`, stored on `app.extensions`. Add read-only aggregate queries in `better_memory/ui/queries.py` for kanban counts and per-stage lists. Render the summary bar + drill-in panel from the spec §4 design, with HTMX polling for counts and active panel every 10 s. Ship per-type card fragments and per-action routes. A small `better_memory/ui/jobs.py` holds the single-job `threading.Lock` + `current_job_id` state for the Consolidation button; Phase 2 registers the button but the handler returns a "Phase 3 ships consolidation" placeholder job.

**Tech Stack:** Python 3.12, Flask 3.x + Jinja2, HTMX 2.0.8 (already vendored), sqlite3 (already configured with sqlite-vec, WAL, FK), existing `InsightService`, pytest + Flask test client.

**Scope (Phase 2 only):**
- Builds everything in spec §4 (Pipeline Kanban).
- Does NOT implement `ConsolidationService.dry_run()` / `branch_and_sweep()` — those are Phase 3. The Consolidation button returns a placeholder job fragment.
- Does NOT implement the promotion modal workflow — Phase 7. The Promote action on insights opens a modal with a "Phase 7" message.
- Does NOT implement merge beyond the UI picker — the merge POST returns a "Phase 3 ships merge logic" error fragment. Phase 3 wires the real logic.
- Does NOT touch `ObservationService.retrieve()` (semantic search) — the Observations column uses a plain `queries.py` SQL helper. Semantic retrieval belongs to MCP, not the kanban list view.
- Smoke tests for Approve/Reject/Edit/Retire/Demote action buttons against real candidates/insights are **deferred to Phase 3**. Phase 3 ships the consolidation that creates candidates, so Phase 3 is the right place to smoke-test full workflows end-to-end. Phase 2 covers these with unit/integration tests using seeded fixtures.

---

## File Structure

### Create

```
better_memory/ui/
  queries.py                                 # aggregate SQL helpers (read-only)
  jobs.py                                    # single-job Lock + current_job_id
  templates/
    fragments/
      panel_observations.html                # list of observation rows
      panel_candidates.html                  # list of candidate rows
      panel_insights.html                    # list of insight rows
      panel_promoted.html                    # list of promoted rows
      observation_card_compact.html          # click-to-expand row for obs
      observation_card_expanded.html         # expanded form
      candidate_card_compact.html
      candidate_card_expanded.html
      insight_card_compact.html
      insight_card_expanded.html
      promoted_card_compact.html
      promoted_card_expanded.html
      insight_edit_form.html                 # edit form for candidates + insights
      merge_picker.html                      # list of merge-target candidates
      promotion_stub_modal.html              # Phase 7 placeholder
      consolidation_job.html                 # progress fragment for /jobs/<id>
      insight_sources.html                   # "View sources" observation list
tests/ui/
  test_queries.py                            # unit tests for queries.py
  test_pipeline.py                           # integration tests for Phase 2 routes
```

### Modify

- `better_memory/ui/app.py` — open sqlite connection, construct `InsightService`, register new routes, wire teardown. Add `db_path: Path | None = None` kwarg on `create_app`.
- `better_memory/ui/templates/base.html` — no functional change; CSS selectors may extend.
- `better_memory/ui/templates/pipeline.html` — replace placeholder with summary bar + panel container.
- `better_memory/ui/templates/fragments/badge.html` — no change (already Phase-2-ready).
- `better_memory/ui/static/app.css` — append kanban styles (pills, panel, card states, action buttons).
- `tests/ui/conftest.py` — extend to provide a `tmp_db` fixture + an app configured with it.

---

## Task 1: Wire sqlite connection + InsightService into the app factory

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/conftest.py`

**Context:** Phase 1's `create_app()` has no DB access. Phase 2 needs it. Spec §2: "UI instantiates `ObservationService`, `InsightService`, `KnowledgeService` with a single `sqlite3.Connection` opened at startup (`check_same_thread=False`). Flask runs with `threaded=False`, so exactly one request is in flight at a time and the shared connection is safe."

For Phase 2 we only need `InsightService` (for candidates/insights mutations) and raw sqlite access via `queries.py` (for browse reads). `ObservationService` and `KnowledgeService` instantiation can come later when actually used.

- [ ] **Step 1: Write failing tests**

Extend `tests/ui/conftest.py`:

```python
"""Shared fixtures for UI tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[Path]:
    """Yield a fresh migrated memory.db path in an isolated tmp dir."""
    db_path = tmp_path / "memory.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    yield db_path


@pytest.fixture
def client(tmp_db: Path) -> Iterator[FlaskClient]:
    """Yield a Flask test client backed by a migrated tmp DB."""
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
```

Add a new test class near the top of `tests/ui/test_app.py`:

```python
class TestServiceWiring:
    def test_app_exposes_insight_service(self, client: FlaskClient) -> None:
        # The service is attached to app.extensions for routes to use.
        assert "insight_service" in client.application.extensions

    def test_app_exposes_open_db_connection(
        self, tmp_db: Path
    ) -> None:
        app = create_app(start_watchdog=False, db_path=tmp_db)
        conn = app.extensions["db_connection"]
        # Connection is open and usable against the migrated schema.
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        assert row[0] == 0
```

Append `tmp_db: Path` param to the existing `TestInactivityTimeout` tests that call `create_app()` directly if they need DB access — they don't yet, so we leave them using `tmp_db` via indirect fixture or explicit opt-in. The existing `client` fixture now requires `tmp_db`; this propagates automatically through pytest.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: `TestServiceWiring` tests FAIL because `create_app()` doesn't accept `db_path` and doesn't register `insight_service` or `db_connection` in `app.extensions`.

- [ ] **Step 3: Update `create_app()`**

Edit `better_memory/ui/app.py`. Add imports at top:

```python
import sqlite3
from pathlib import Path

from better_memory.db.connection import connect
from better_memory.services.insight import InsightService
```

Update the factory signature:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
) -> Flask:
```

Inside `create_app()`, after `app = Flask(__name__)` and before the middleware hooks, add:

```python
    # Resolve DB path from arg or config.
    resolved_db = db_path if db_path is not None else resolve_home() / "memory.db"
    db_conn = connect(resolved_db)
    db_conn.execute("PRAGMA foreign_keys=ON")  # defensive; connect() already does this.

    app.extensions["db_connection"] = db_conn
    app.extensions["insight_service"] = InsightService(conn=db_conn)

    @app.teardown_appcontext
    def _close_db_on_teardown(_exc: BaseException | None) -> None:
        # Flask calls this after every request in an app context. We keep
        # the connection open for the life of the app (shared single-request
        # model with threaded=False), so do nothing per-request. The
        # connection is closed when the process exits.
        return None
```

Note: we intentionally do NOT close the connection per-request — the UI opens ONE connection and shares it across all requests (spec §2). The teardown hook is a no-op but declared so future per-request cleanup (e.g. rolling back any half-open transaction) can hook in.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS. Existing tests keep passing because `tmp_db` is now a transitive dependency of `client`.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/conftest.py tests/ui/test_app.py
git commit -m "UI Phase 2: wire sqlite connection + InsightService into factory"
```

---

## Task 2: queries.py — kanban counts

**Files:**
- Create: `better_memory/ui/queries.py`
- Create: `tests/ui/test_queries.py`

**Context:** The summary bar shows four counts. Observations: `SELECT COUNT(*) FROM observations WHERE status='active' AND project=?`. Candidates: insights with `status='pending_review'`. Insights: `status='confirmed'`. Promoted: `status='promoted'`. All filtered by project. Project is resolved via `Path.cwd().name` by default (matching the existing pattern in `ObservationService.__init__`).

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_queries.py`:

```python
"""Unit tests for better_memory.ui.queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.queries import KanbanCounts, kanban_counts


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    status: str = "active",
) -> None:
    conn.execute(
        """
        INSERT INTO observations (id, content, project, status)
        VALUES (?, ?, ?, ?)
        """,
        (id, f"obs-{id}", project, status),
    )
    conn.commit()


def _insert_insight(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO insights (id, title, content, project, status, polarity)
        VALUES (?, ?, ?, ?, ?, 'neutral')
        """,
        (id, f"title-{id}", f"content-{id}", project, status),
    )
    conn.commit()


class TestKanbanCounts:
    def test_empty_project_returns_zero_counts(
        self, conn: sqlite3.Connection
    ) -> None:
        counts = kanban_counts(conn, project="empty-proj")
        assert counts == KanbanCounts(
            observations=0, candidates=0, insights=0, promoted=0
        )

    def test_counts_by_status_and_project(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(conn, id="o1", project="p1")
        _insert_observation(conn, id="o2", project="p1")
        _insert_observation(conn, id="o3", project="p1", status="archived")
        _insert_observation(conn, id="o4", project="p2")  # other project

        _insert_insight(conn, id="c1", project="p1", status="pending_review")
        _insert_insight(conn, id="c2", project="p1", status="pending_review")
        _insert_insight(conn, id="i1", project="p1", status="confirmed")
        _insert_insight(conn, id="pr1", project="p1", status="promoted")
        _insert_insight(conn, id="r1", project="p1", status="retired")

        counts = kanban_counts(conn, project="p1")
        assert counts == KanbanCounts(
            observations=2,  # only active, only p1
            candidates=2,
            insights=1,
            promoted=1,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_queries.py -v`
Expected: FAIL — `No module named 'better_memory.ui.queries'`.

- [ ] **Step 3: Create `queries.py`**

Create `better_memory/ui/queries.py`:

```python
"""Aggregate read-only queries for the Management UI.

These helpers own no transactions — they call SELECT only. Writes go
through the service layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class KanbanCounts:
    observations: int
    candidates: int
    insights: int
    promoted: int


def kanban_counts(conn: sqlite3.Connection, *, project: str) -> KanbanCounts:
    """Return the four pipeline-stage counts for ``project``."""
    obs_row = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE status = 'active' AND project = ?",
        (project,),
    ).fetchone()
    cand_row = conn.execute(
        "SELECT COUNT(*) FROM insights "
        "WHERE status = 'pending_review' AND project = ?",
        (project,),
    ).fetchone()
    ins_row = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE status = 'confirmed' AND project = ?",
        (project,),
    ).fetchone()
    pro_row = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE status = 'promoted' AND project = ?",
        (project,),
    ).fetchone()
    return KanbanCounts(
        observations=obs_row[0],
        candidates=cand_row[0],
        insights=ins_row[0],
        promoted=pro_row[0],
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_queries.py -v`
Expected: All PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries.py
git commit -m "UI Phase 2: queries.py — kanban_counts aggregator"
```

---

## Task 3: queries.py — list functions for each stage

**Files:**
- Modify: `better_memory/services/insight.py`
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_queries.py`

**Context:** Each panel needs a list of rows. For observations, a light dataclass with the columns we'll render in cards. For candidates/insights/promoted, we return `Insight` rows directly (already defined in `better_memory.services.insight`).

The `_row_to_insight` helper in the service module is used internally today. We need to call it from `queries.py`, so promote it to public (`row_to_insight`), matching the Phase-1 `resolve_home` pattern.

- [ ] **Step 1: Rename `_row_to_insight` → `row_to_insight` in `better_memory/services/insight.py`**

Edit `better_memory/services/insight.py`. Rename the helper definition at line 94 from `_row_to_insight` to `row_to_insight`. Then `replace_all` the internal callers inside `insight.py` (there are three or four). Verify with:

Run: `grep -rn "_row_to_insight" better_memory tests`
Expected: No output.

Run: `uv run pytest tests/services/test_insight.py -q`
Expected: All pass — tests don't reference the old name directly.

Commit this rename separately so the queries-introducing commit stays small:

```bash
git add better_memory/services/insight.py
git commit -m "UI Phase 2: promote row_to_insight to public helper"
```

- [ ] **Step 2: Write failing tests**

Append to `tests/ui/test_queries.py`:

```python
from better_memory.services.insight import Insight
from better_memory.ui.queries import (
    ObservationListRow,
    list_candidates,
    list_insights,
    list_observations,
    list_promoted,
)


class TestListObservations:
    def test_empty_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        assert list_observations(conn, project="empty") == []

    def test_returns_active_only_ordered_recent_first(
        self, conn: sqlite3.Connection
    ) -> None:
        # created_at defaults to CURRENT_TIMESTAMP, so insert in order.
        _insert_observation(conn, id="old", project="p")
        _insert_observation(conn, id="new", project="p")
        _insert_observation(conn, id="arc", project="p", status="archived")
        rows = list_observations(conn, project="p")
        ids = [r.id for r in rows]
        assert ids == ["new", "old"]  # DESC by created_at
        assert all(isinstance(r, ObservationListRow) for r in rows)

    def test_respects_limit(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            _insert_observation(conn, id=f"o{i}", project="p")
        rows = list_observations(conn, project="p", limit=2)
        assert len(rows) == 2


class TestListInsightsByStatus:
    def test_list_candidates(self, conn: sqlite3.Connection) -> None:
        _insert_insight(conn, id="c1", project="p", status="pending_review")
        _insert_insight(conn, id="c2", project="p", status="pending_review")
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        candidates = list_candidates(conn, project="p")
        assert [i.id for i in candidates] == ["c2", "c1"]  # newest first
        assert all(isinstance(i, Insight) for i in candidates)

    def test_list_insights_returns_confirmed_only(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="c1", project="p", status="pending_review")
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        _insert_insight(conn, id="pr1", project="p", status="promoted")
        result = list_insights(conn, project="p")
        assert [i.id for i in result] == ["i1"]

    def test_list_promoted_returns_promoted_only(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        _insert_insight(conn, id="pr1", project="p", status="promoted")
        _insert_insight(conn, id="pr2", project="p", status="promoted")
        result = list_promoted(conn, project="p")
        assert sorted(i.id for i in result) == ["pr1", "pr2"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_queries.py -v`
Expected: FAIL — `ObservationListRow`, `list_observations`, `list_candidates`, `list_insights`, `list_promoted` don't exist.

- [ ] **Step 4: Extend `queries.py`**

Append to `better_memory/ui/queries.py`:

```python
from better_memory.services.insight import Insight, row_to_insight


@dataclass(frozen=True)
class ObservationListRow:
    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    created_at: str


def list_observations(
    conn: sqlite3.Connection,
    *,
    project: str,
    limit: int = 50,
) -> list[ObservationListRow]:
    """Return active observations for ``project``, newest first."""
    rows = conn.execute(
        """
        SELECT id, content, component, theme, outcome, created_at
        FROM observations
        WHERE status = 'active' AND project = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (project, limit),
    ).fetchall()
    return [
        ObservationListRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _list_insights_by_status(
    conn: sqlite3.Connection, *, project: str, status: str, limit: int
) -> list[Insight]:
    rows = conn.execute(
        """
        SELECT * FROM insights
        WHERE project = ? AND status = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (project, status, limit),
    ).fetchall()
    return [row_to_insight(r) for r in rows]


def list_candidates(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="pending_review", limit=limit
    )


def list_insights(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="confirmed", limit=limit
    )


def list_promoted(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="promoted", limit=limit
    )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_queries.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries.py
git commit -m "UI Phase 2: queries.py — list functions for each pipeline stage"
```

---

## Task 4: Project resolution helper + real badge count

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

**Context:** The badge currently hardcodes `count=0`. Wire it to `kanban_counts()` to show real candidate count. Project comes from cwd name — matches `ObservationService` pattern.

- [ ] **Step 1: Write the failing test**

Append to `tests/ui/test_app.py`:

```python
import sqlite3 as _sqlite3


class TestBadgeRealCount:
    def test_badge_shows_candidate_count_from_db(
        self, client: FlaskClient, tmp_db: Path
    ) -> None:
        # Insert candidates directly via the app's connection so the
        # project name matches cwd (same as the kanban query).
        from pathlib import Path as _Path
        conn: _sqlite3.Connection = client.application.extensions["db_connection"]
        project = _Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c1', 't', 'c', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c2', 't', 'c', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('x', 't', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/pipeline/badge")
        assert response.status_code == 200
        assert response.data.strip() == b"2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_app.py::TestBadgeRealCount -v`
Expected: FAIL — route still renders `count=0`.

- [ ] **Step 3: Wire the route to real counts**

In `better_memory/ui/app.py`, replace the `pipeline_badge` view:

```python
    @app.get("/pipeline/badge")
    def pipeline_badge() -> str:
        counts = queries.kanban_counts(
            app.extensions["db_connection"], project=_project_name()
        )
        return render_template("fragments/badge.html", count=counts.candidates)
```

Add at module-top:

```python
from better_memory.ui import queries
```

Add a small helper at module scope (above `create_app`):

```python
def _project_name() -> str:
    """Return the current project — cwd name, matching service convention."""
    return Path.cwd().name
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS (including the existing `test_badge_empty_when_zero` — an empty DB has zero candidates, so the badge still renders empty).

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 2: badge route returns real candidate count"
```

---

## Task 5: Pipeline page — summary bar + default panel

**Files:**
- Modify: `better_memory/ui/templates/pipeline.html`
- Modify: `better_memory/ui/app.py`
- Create: `tests/ui/test_pipeline.py`

**Context:** Replace the Phase 1 placeholder. The `/pipeline` route now renders a summary bar (4 pills with counts) plus a panel container that defaults to the Candidates panel. Panel contents are loaded via HTMX on page load (`hx-trigger="load, every 10s"`).

- [ ] **Step 1: Write failing tests**

Create `tests/ui/test_pipeline.py`:

```python
"""Integration tests for the Pipeline Kanban (spec §4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask.testing import FlaskClient


def _insert_candidate(conn: sqlite3.Connection, project: str, id: str) -> None:
    conn.execute(
        "INSERT INTO insights (id, title, content, project, status, polarity) "
        "VALUES (?, ?, ?, ?, 'pending_review', 'neutral')",
        (id, f"title-{id}", f"content-{id}", project),
    )
    conn.commit()


def _insert_observation(conn: sqlite3.Connection, project: str, id: str) -> None:
    conn.execute(
        "INSERT INTO observations (id, content, project, status) "
        "VALUES (?, ?, ?, 'active')",
        (id, f"obs-{id}", project),
    )
    conn.commit()


class TestPipelinePage:
    def test_renders_summary_bar_with_counts(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_observation(conn, project, "o1")

        response = client.get("/pipeline")
        assert response.status_code == 200
        body = response.data.decode()
        # Four stage labels
        assert "Observations" in body
        assert "Candidates" in body
        assert "Insights" in body
        assert "Promoted" in body
        # Real counts rendered inside the <span class="count"> inside each
        # pill — assert the exact token so a stray "1" elsewhere cannot
        # trigger a false pass.
        import re
        count_tokens = re.findall(r'<span class="count">(\d+)</span>', body)
        # Observations=1, Candidates=1, Insights=0, Promoted=0 (in pill order).
        assert count_tokens == ["1", "1", "0", "0"]

    def test_default_panel_is_candidates(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        body = response.data.decode()
        # The panel-candidates fragment is loaded via HTMX — assert the
        # hx-get attribute is present and points to the candidates panel.
        assert "/pipeline/panel/candidates" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: Two FAIL — current `/pipeline` still has the Phase 1 placeholder.

- [ ] **Step 3: Rewrite `pipeline.html`**

Replace `better_memory/ui/templates/pipeline.html` with:

```html
{% extends "base.html" %}
{% block title %}Pipeline — better-memory{% endblock %}
{% block main %}
<section class="kanban">
  <div class="summary-bar">
    <button class="pill {% if active_stage == 'observations' %}active{% endif %}"
            hx-get="{{ url_for('pipeline_panel', stage='observations') }}"
            hx-target="#panel" hx-swap="innerHTML">
      <span class="count">{{ counts.observations }}</span>
      <span class="stage-label">Observations</span>
    </button>
    <button class="pill candidates-pill {% if active_stage == 'candidates' %}active{% endif %}"
            hx-get="{{ url_for('pipeline_panel', stage='candidates') }}"
            hx-target="#panel" hx-swap="innerHTML">
      <span class="count">{{ counts.candidates }}</span>
      <span class="stage-label">Candidates</span>
    </button>
    <button class="pill {% if active_stage == 'insights' %}active{% endif %}"
            hx-get="{{ url_for('pipeline_panel', stage='insights') }}"
            hx-target="#panel" hx-swap="innerHTML">
      <span class="count">{{ counts.insights }}</span>
      <span class="stage-label">Insights</span>
    </button>
    <button class="pill {% if active_stage == 'promoted' %}active{% endif %}"
            hx-get="{{ url_for('pipeline_panel', stage='promoted') }}"
            hx-target="#panel" hx-swap="innerHTML">
      <span class="count">{{ counts.promoted }}</span>
      <span class="stage-label">Promoted</span>
    </button>
    <div class="toolbar">
      <button class="run-consolidation"
              hx-post="{{ url_for('pipeline_consolidate') }}"
              hx-target="#job" hx-swap="innerHTML">
        Run branch-and-sweep
      </button>
    </div>
  </div>

  <div id="job"></div>

  <div id="panel"
       hx-get="{{ url_for('pipeline_panel', stage=active_stage) }}"
       hx-trigger="load, every 10s, job-complete from:body"
       hx-swap="innerHTML">
  </div>
</section>
{% endblock %}
```

- [ ] **Step 4: Update the `/pipeline` route**

In `better_memory/ui/app.py`, replace the `pipeline` view:

```python
    @app.get("/pipeline")
    def pipeline() -> str:
        counts = queries.kanban_counts(
            app.extensions["db_connection"], project=_project_name()
        )
        return render_template(
            "pipeline.html",
            active_tab="pipeline",
            active_stage="candidates",
            counts=counts,
        )
```

- [ ] **Step 5: Add stub routes for panel + consolidate**

So the `url_for()` calls in the template resolve, register placeholder routes. Real implementations in Tasks 6 and 13.

```python
    @app.get("/pipeline/panel/<stage>")
    def pipeline_panel(stage: str) -> str:
        # Task 6 implements this.
        return ""

    @app.post("/pipeline/consolidate")
    def pipeline_consolidate() -> str:
        # Task 13 implements this.
        return ""
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py tests/ui/test_app.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/pipeline.html better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: pipeline page — summary bar + panel container"
```

---

## Task 6: Panel fragment — observations list

**Files:**
- Create: `better_memory/ui/templates/fragments/panel_observations.html`
- Create: `better_memory/ui/templates/fragments/observation_card_compact.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Start with observations since it's read-only (simpler). When `/pipeline/panel/observations` is hit, render a list of compact observation rows. Empty state when no rows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestObservationsPanel:
    def test_empty_shows_empty_state(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/observations")
        assert response.status_code == 200
        body = response.data.decode()
        assert "No observations yet" in body

    def test_lists_observations_newest_first(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_observation(conn, project, "oldest")
        _insert_observation(conn, project, "newest")

        response = client.get("/pipeline/panel/observations")
        body = response.data.decode()
        # Both visible
        assert "obs-newest" in body
        assert "obs-oldest" in body
        # Newest appears before oldest in the rendered HTML
        assert body.index("obs-newest") < body.index("obs-oldest")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestObservationsPanel -v`
Expected: Two FAIL — panel stub returns empty string.

- [ ] **Step 3: Create templates**

Create `better_memory/ui/templates/fragments/observation_card_compact.html`:

```html
<div class="card card-compact observation-card" data-id="{{ obs.id }}">
  <div class="card-meta">
    <span class="outcome outcome-{{ obs.outcome }}">{{ obs.outcome }}</span>
    <span class="component">{{ obs.component or '—' }}</span>
    <span class="timestamp">{{ obs.created_at }}</span>
  </div>
  <div class="card-content">{{ obs.content }}</div>
</div>
```

Create `better_memory/ui/templates/fragments/panel_observations.html`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No observations yet. Memories appear here once the MCP writes them.</p>
  </div>
{% else %}
  <div class="card-list">
    {% for row in rows %}
      {% include "fragments/observation_card_compact.html" with context %}
    {% endfor %}
  </div>
{% endif %}
```

Note: Jinja's `{% include %}` with `with context` passes the outer context. Inside the include the variable is `obs` — so we need to pass it explicitly:

Replace the panel template's loop body to alias each row as `obs`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No observations yet. Memories appear here once the MCP writes them.</p>
  </div>
{% else %}
  <div class="card-list">
    {% for obs in rows %}
      {% include "fragments/observation_card_compact.html" %}
    {% endfor %}
  </div>
{% endif %}
```

- [ ] **Step 4: Wire the route**

In `better_memory/ui/app.py`, replace the panel stub:

```python
    @app.get("/pipeline/panel/<stage>")
    def pipeline_panel(stage: str) -> str:
        conn = app.extensions["db_connection"]
        project = _project_name()
        if stage == "observations":
            return render_template(
                "fragments/panel_observations.html",
                rows=queries.list_observations(conn, project=project),
            )
        # Other stages land in Tasks 7, 8, 9.
        abort(404)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: Observations panel tests PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/ better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: observations panel fragment + /pipeline/panel/observations"
```

---

## Task 7: Panel fragments — candidates, insights, promoted (compact cards)

**Files:**
- Create: `better_memory/ui/templates/fragments/panel_candidates.html`
- Create: `better_memory/ui/templates/fragments/panel_insights.html`
- Create: `better_memory/ui/templates/fragments/panel_promoted.html`
- Create: `better_memory/ui/templates/fragments/candidate_card_compact.html`
- Create: `better_memory/ui/templates/fragments/insight_card_compact.html`
- Create: `better_memory/ui/templates/fragments/promoted_card_compact.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Mirror Task 6 for the three other stages. Each has its own compact card template because actions differ per stage (per spec §4 table).

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestCandidatesPanel:
    def test_empty_shows_run_consolidation_message(
        self, client: FlaskClient
    ) -> None:
        response = client.get("/pipeline/panel/candidates")
        assert response.status_code == 200
        assert b"No candidates" in response.data

    def test_lists_candidates_with_approve_reject(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.get("/pipeline/panel/candidates")
        body = response.data.decode()
        assert "title-c1" in body
        # Compact actions: Approve, Reject
        assert "Approve" in body
        assert "Reject" in body


class TestInsightsPanel:
    def test_empty(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/insights")
        assert response.status_code == 200
        assert b"No insights" in response.data

    def test_lists_insights_with_promote_retire(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 'title-i1', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()
        response = client.get("/pipeline/panel/insights")
        body = response.data.decode()
        assert "title-i1" in body
        assert "Promote" in body
        assert "Retire" in body


class TestPromotedPanel:
    def test_empty(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/promoted")
        assert response.status_code == 200
        assert b"No promoted" in response.data

    def test_lists_promoted_with_view_doc_demote(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('pr1', 'title-pr1', 'c', ?, 'promoted', 'neutral')",
            (project,),
        )
        conn.commit()
        response = client.get("/pipeline/panel/promoted")
        body = response.data.decode()
        assert "title-pr1" in body
        assert "View doc" in body
        assert "Demote" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: The new tests FAIL with 404 (panel route doesn't handle those stages yet).

- [ ] **Step 3: Create compact card templates**

Create `better_memory/ui/templates/fragments/candidate_card_compact.html`:

```html
<div class="card card-compact candidate-card" data-id="{{ c.id }}"
     hx-get="{{ url_for('candidate_card', id=c.id) }}"
     hx-trigger="click"
     hx-target="this" hx-swap="outerHTML">
  <div class="card-meta">
    <span class="polarity polarity-{{ c.polarity }}">{{ c.polarity }}</span>
    <span class="component">{{ c.component or '—' }}</span>
    <span class="confidence">{{ c.confidence }}</span>
  </div>
  <div class="card-title">{{ c.title }}</div>
  <div class="card-actions" onclick="event.stopPropagation()">
    <button hx-post="{{ url_for('candidate_approve', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Approve</button>
    <button hx-post="{{ url_for('candidate_reject', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Reject</button>
  </div>
</div>
```

Create `better_memory/ui/templates/fragments/insight_card_compact.html`:

```html
<div class="card card-compact insight-card" data-id="{{ i.id }}"
     hx-get="{{ url_for('insight_card', id=i.id) }}"
     hx-trigger="click"
     hx-target="this" hx-swap="outerHTML">
  <div class="card-meta">
    <span class="polarity polarity-{{ i.polarity }}">{{ i.polarity }}</span>
    <span class="component">{{ i.component or '—' }}</span>
    <span class="confidence">{{ i.confidence }}</span>
  </div>
  <div class="card-title">{{ i.title }}</div>
  <div class="card-actions" onclick="event.stopPropagation()">
    <button hx-get="{{ url_for('insight_promote', id=i.id) }}"
            hx-target="#modal" hx-swap="innerHTML">Promote</button>
    <button hx-post="{{ url_for('insight_retire', id=i.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Retire</button>
  </div>
</div>
```

Create `better_memory/ui/templates/fragments/promoted_card_compact.html`:

```html
<div class="card card-compact promoted-card" data-id="{{ p.id }}"
     hx-get="{{ url_for('insight_card', id=p.id) }}"
     hx-trigger="click"
     hx-target="this" hx-swap="outerHTML">
  <div class="card-meta">
    <span class="polarity polarity-{{ p.polarity }}">{{ p.polarity }}</span>
    <span class="component">{{ p.component or '—' }}</span>
  </div>
  <div class="card-title">{{ p.title }}</div>
  <div class="card-actions" onclick="event.stopPropagation()">
    <button disabled title="Phase 7 ships the promoted-doc link">View doc</button>
    <button hx-post="{{ url_for('insight_demote', id=p.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Demote</button>
  </div>
</div>
```

- [ ] **Step 4: Create panel templates**

Create `better_memory/ui/templates/fragments/panel_candidates.html`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No candidates pending. Run branch-and-sweep to produce new ones.</p>
  </div>
{% else %}
  <div class="card-list">
    {% for c in rows %}
      {% include "fragments/candidate_card_compact.html" %}
    {% endfor %}
  </div>
{% endif %}
```

Create `better_memory/ui/templates/fragments/panel_insights.html`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No insights yet. Approved candidates land here.</p>
  </div>
{% else %}
  <div class="card-list">
    {% for i in rows %}
      {% include "fragments/insight_card_compact.html" %}
    {% endfor %}
  </div>
{% endif %}
```

Create `better_memory/ui/templates/fragments/panel_promoted.html`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No promoted insights yet. Promote an insight to add it here.</p>
  </div>
{% else %}
  <div class="card-list">
    {% for p in rows %}
      {% include "fragments/promoted_card_compact.html" %}
    {% endfor %}
  </div>
{% endif %}
```

- [ ] **Step 5: Extend the panel route**

Replace the panel route in `better_memory/ui/app.py`:

```python
    @app.get("/pipeline/panel/<stage>")
    def pipeline_panel(stage: str) -> str:
        conn = app.extensions["db_connection"]
        project = _project_name()
        if stage == "observations":
            return render_template(
                "fragments/panel_observations.html",
                rows=queries.list_observations(conn, project=project),
            )
        if stage == "candidates":
            return render_template(
                "fragments/panel_candidates.html",
                rows=queries.list_candidates(conn, project=project),
            )
        if stage == "insights":
            return render_template(
                "fragments/panel_insights.html",
                rows=queries.list_insights(conn, project=project),
            )
        if stage == "promoted":
            return render_template(
                "fragments/panel_promoted.html",
                rows=queries.list_promoted(conn, project=project),
            )
        abort(404)
```

- [ ] **Step 6: Register placeholder routes for action endpoints referenced above**

The cards reference `candidate_approve`, `candidate_reject`, `candidate_card`, `insight_card`, `insight_promote`, `insight_retire`, `insight_demote`. To let `url_for()` resolve without crashing the template render, register placeholder routes in `app.py` (real implementations in Tasks 8–12):

```python
    @app.get("/candidates/<id>/card")
    def candidate_card(id: str) -> str:
        return ""  # Task 8 implements

    @app.post("/candidates/<id>/approve")
    def candidate_approve(id: str) -> str:
        return ""  # Task 10 implements

    @app.post("/candidates/<id>/reject")
    def candidate_reject(id: str) -> str:
        return ""  # Task 10 implements

    @app.get("/insights/<id>/card")
    def insight_card(id: str) -> str:
        return ""  # Task 8 implements

    @app.get("/insights/<id>/promote")
    def insight_promote(id: str) -> str:
        return ""  # Task 12 implements

    @app.post("/insights/<id>/retire")
    def insight_retire(id: str) -> str:
        return ""  # Task 11 implements

    @app.post("/insights/<id>/demote")
    def insight_demote(id: str) -> str:
        return ""  # Task 11 implements
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add better_memory/ui/templates/fragments/ better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: panel fragments for candidates, insights, promoted"
```

---

## Task 8: Expanded card routes and templates

**Files:**
- Create: `better_memory/ui/templates/fragments/candidate_card_expanded.html`
- Create: `better_memory/ui/templates/fragments/insight_card_expanded.html`
- Create: `better_memory/ui/templates/fragments/observation_card_expanded.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Click-to-expand. `/candidates/<id>/card` returns the expanded fragment which replaces the compact row in-place. Expanded card has the full content, metadata, all actions including `Edit` and `Merge` (candidates) or `Edit` and `View sources` (insights).

- [ ] **Step 1: Write failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestExpandedCards:
    def test_candidate_expanded_shows_full_content_and_all_actions(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.get("/candidates/c1/card")
        assert response.status_code == 200
        body = response.data.decode()
        assert "title-c1" in body
        assert "content-c1" in body
        # All four actions: Approve, Reject, Edit, Merge
        assert "Approve" in body
        assert "Reject" in body
        assert "Edit" in body
        assert "Merge" in body
        assert 'data-expanded="true"' in body

    def test_insight_expanded_shows_edit_and_view_sources(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 'title-i1', 'content-i1', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/insights/i1/card")
        body = response.data.decode()
        assert "content-i1" in body
        assert "Promote" in body
        assert "Retire" in body
        assert "Edit" in body
        assert "View sources" in body

    def test_missing_card_returns_404(self, client: FlaskClient) -> None:
        response = client.get("/candidates/does-not-exist/card")
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestExpandedCards -v`
Expected: Three FAIL — routes are stubs returning empty string.

- [ ] **Step 3: Create expanded templates**

Create `better_memory/ui/templates/fragments/candidate_card_expanded.html`:

```html
<div class="card card-expanded candidate-card" data-id="{{ c.id }}" data-expanded="true">
  <div class="card-meta">
    <span class="polarity polarity-{{ c.polarity }}">{{ c.polarity }}</span>
    <span class="component">{{ c.component or '—' }}</span>
    <span class="confidence">{{ c.confidence }}</span>
    <span class="evidence">evidence: {{ c.evidence_count }}</span>
    <button class="collapse-me" aria-label="Collapse"
            hx-get="{{ url_for('candidate_compact_card', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">×</button>
  </div>
  <div class="card-title">{{ c.title }}</div>
  <div class="card-content">{{ c.content }}</div>
  <div class="card-actions">
    <button hx-post="{{ url_for('candidate_approve', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Approve</button>
    <button hx-post="{{ url_for('candidate_reject', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Reject</button>
    <button hx-get="{{ url_for('candidate_edit', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Edit</button>
    <button hx-get="{{ url_for('candidate_merge_picker', id=c.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Merge</button>
  </div>
</div>
```

Create `better_memory/ui/templates/fragments/insight_card_expanded.html`:

```html
<div class="card card-expanded insight-card" data-id="{{ i.id }}" data-expanded="true">
  <div class="card-meta">
    <span class="polarity polarity-{{ i.polarity }}">{{ i.polarity }}</span>
    <span class="component">{{ i.component or '—' }}</span>
    <span class="confidence">{{ i.confidence }}</span>
    <span class="evidence">evidence: {{ i.evidence_count }}</span>
    <button class="collapse-me" aria-label="Collapse"
            hx-get="{{ url_for('insight_compact_card', id=i.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">×</button>
  </div>
  <div class="card-title">{{ i.title }}</div>
  <div class="card-content">{{ i.content }}</div>
  <div class="card-actions">
    <button hx-get="{{ url_for('insight_promote', id=i.id) }}"
            hx-target="#modal" hx-swap="innerHTML">Promote</button>
    <button hx-post="{{ url_for('insight_retire', id=i.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Retire</button>
    <button hx-get="{{ url_for('insight_edit', id=i.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Edit</button>
    <button hx-get="{{ url_for('insight_sources', id=i.id) }}"
            hx-target="#sources-{{ i.id }}" hx-swap="innerHTML">View sources</button>
  </div>
  <div id="sources-{{ i.id }}" class="sources-container"></div>
</div>
```

- [ ] **Step 4: Wire the routes**

In `better_memory/ui/app.py`, replace the `candidate_card` and `insight_card` stubs:

```python
    @app.get("/candidates/<id>/card")
    def candidate_card(id: str) -> str:
        service = app.extensions["insight_service"]
        c = service.get(id)
        if c is None or c.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/candidate_card_expanded.html", c=c
        )

    @app.get("/insights/<id>/card")
    def insight_card(id: str) -> str:
        service = app.extensions["insight_service"]
        i = service.get(id)
        if i is None or i.status not in ("confirmed", "promoted"):
            abort(404)
        return render_template(
            "fragments/insight_card_expanded.html", i=i
        )
```

Also register placeholder routes for the new names referenced above (`candidate_edit`, `candidate_merge_picker`, `insight_edit`, `insight_sources` — Tasks 10, 11, 12):

```python
    @app.get("/candidates/<id>/edit")
    def candidate_edit(id: str) -> str:
        return ""  # Task 10 implements

    @app.post("/candidates/<id>/edit")
    def candidate_edit_save(id: str) -> str:
        return ""  # Task 10 implements

    @app.get("/candidates/<id>/merge")
    def candidate_merge_picker(id: str) -> str:
        return ""  # Task 12 implements

    @app.post("/candidates/<id>/merge")
    def candidate_merge(id: str) -> str:
        return ""  # Task 12 implements

    @app.get("/insights/<id>/edit")
    def insight_edit(id: str) -> str:
        return ""  # Task 11 implements

    @app.post("/insights/<id>/edit")
    def insight_edit_save(id: str) -> str:
        return ""  # Task 11 implements

    @app.get("/insights/<id>/sources")
    def insight_sources(id: str) -> str:
        return ""  # Task 11 implements
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/ better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: expanded card fragments and routes"
```

---

## Task 9: Only-one-expanded behavior via inline script

**Files:**
- Modify: `better_memory/ui/templates/base.html`

**Context:** Spec §4: "Only one card expanded at a time. Each expanded card has `data-expanded='true'` and an `hx-on:click` handler that, before swapping, closes any other `[data-expanded='true']` in the panel."

Implementation: a tiny vanilla-JS listener on the panel that, before any card's expand request completes, collapses any currently-expanded siblings by triggering their collapse handler. Spec notes "Tiny inline script — no extra library."

- [ ] **Step 1: Add the inline script**

Edit `better_memory/ui/templates/base.html`. Before `</body>`, add:

```html
<script>
(function () {
  // Collapse any currently-expanded cards when a new card is clicked to expand.
  // HTMX fires htmx:beforeRequest before every request — we check if the
  // triggering element is a collapsed card on a panel, and if so, fire
  // the collapse handler on any sibling [data-expanded="true"] card.
  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var target = evt.detail.elt;
    // Only act on compact-card expand requests.
    if (!target.classList || !target.classList.contains('card-compact')) return;
    // Find the enclosing panel (card-list) and collapse any expanded sibling.
    var list = target.closest('.card-list');
    if (!list) return;
    var expanded = list.querySelectorAll('[data-expanded="true"]');
    expanded.forEach(function (card) {
      // Trigger the card's collapse handler — it's hx-get on the panel list,
      // but we just need the UI to flip back. Simplest: click the card's
      // .collapse-me button.
      var btn = card.querySelector('.collapse-me');
      if (btn) btn.click();
    });
  });
})();
</script>
```

Note: the script is near-inline (no separate file). Spec says this is the intended approach.

Also add an empty `#modal` div just before `</body>` so promote/merge modals have a target:

```html
<div id="modal"></div>
```

- [ ] **Step 2: Add a smoke-level regression test**

The behavior is browser-only, but we can still guard against the script being deleted or edited out. Append to `tests/ui/test_app.py`:

```python
class TestOnlyOneExpandedScript:
    def test_base_includes_only_one_expanded_listener(
        self, client: FlaskClient
    ) -> None:
        response = client.get("/pipeline")
        body = response.data
        # Script must listen for the HTMX event that fires before any
        # request and walk the .card-list for expanded siblings.
        assert b"htmx:beforeRequest" in body
        assert b"card-compact" in body
        assert b"data-expanded" in body
        assert b"collapse-me" in body
        # Modal target div exists for promote / merge.
        assert b'id="modal"' in body
```

This test will catch deletions or accidental edits that remove the core parts of the logic, without requiring a browser. Behavioural verification still relies on manual browser smoke-testing (Task 15).

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ui/ -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add better_memory/ui/templates/base.html tests/ui/test_app.py
git commit -m "UI Phase 2: inline script for only-one-expanded card behavior"
```

---

## Task 10: Candidate actions — Approve, Reject, Edit

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/insight_edit_form.html`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Candidates transition via `InsightService.update()`:
- Approve: `status='confirmed'`
- Reject: `status='retired'`
- Edit: update title/content (form in/form out)

Each action returns the refreshed compact card (or empty div if the card no longer belongs in Candidates).

- [ ] **Step 1: Write failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestCandidateActions:
    def test_approve_moves_candidate_to_confirmed(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.post(
            "/candidates/c1/approve",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        # After approval the candidate no longer belongs in Candidates;
        # the card response should be empty (card removed from panel).
        assert response.data.strip() == b""
        # DB confirms.
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "confirmed"

    def test_reject_moves_candidate_to_retired(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.post(
            "/candidates/c1/reject",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_edit_form_then_save(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        form_response = client.get("/candidates/c1/edit")
        assert form_response.status_code == 200
        assert b"<form" in form_response.data
        assert b"title-c1" in form_response.data

        save_response = client.post(
            "/candidates/c1/edit",
            data={"title": "new title", "content": "new content"},
            headers={"Origin": "http://localhost"},
        )
        assert save_response.status_code == 200
        # The refreshed compact card renders the new title.
        assert b"new title" in save_response.data

        row = conn.execute(
            "SELECT title, content FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["title"] == "new title"
        assert row["content"] == "new content"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestCandidateActions -v`
Expected: FAIL — the routes are stubs.

- [ ] **Step 3: Create edit form template**

Create `better_memory/ui/templates/fragments/insight_edit_form.html`:

```html
<form class="card card-edit" data-id="{{ row.id }}"
      hx-post="{{ save_url }}" hx-target="closest .card" hx-swap="outerHTML">
  <label>
    Title
    <input type="text" name="title" value="{{ row.title }}" required>
  </label>
  <label>
    Content
    <textarea name="content" rows="6">{{ row.content }}</textarea>
  </label>
  <div class="card-actions">
    <button type="submit">Save</button>
    <button type="button"
            hx-get="{{ cancel_url }}"
            hx-target="closest .card" hx-swap="outerHTML">Cancel</button>
  </div>
</form>
```

The form posts to `save_url` and re-renders the card. `cancel_url` should re-fetch the compact card. The template receives a generic `row` so it can serve candidates AND insights.

- [ ] **Step 4: Implement the routes**

In `better_memory/ui/app.py`, replace the candidate action stubs:

```python
    @app.post("/candidates/<id>/approve")
    def candidate_approve(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        service.update(id, status="confirmed")
        # Removed from candidates panel — return empty.
        return ""

    @app.post("/candidates/<id>/reject")
    def candidate_reject(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        service.update(id, status="retired")
        return ""

    @app.get("/candidates/<id>/edit")
    def candidate_edit(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/insight_edit_form.html",
            row=existing,
            save_url=url_for("candidate_edit_save", id=id),
            cancel_url=url_for("candidate_compact_card", id=id),
        )

    @app.post("/candidates/<id>/edit")
    def candidate_edit_save(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        title = request.form.get("title", existing.title)
        content = request.form.get("content", existing.content)
        service.update(id, title=title, content=content)
        updated = service.get(id)
        return render_template(
            "fragments/candidate_card_compact.html", c=updated
        )
```

Also add a helper route for Cancel to return the compact card:

```python
    @app.get("/candidates/<id>/compact")
    def candidate_compact_card(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/candidate_card_compact.html", c=existing
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/insight_edit_form.html better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: candidate actions — approve, reject, edit"
```

---

## Task 11: Insight actions — Retire, Edit, Demote, View sources

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/insight_sources.html`
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Insights (status=`confirmed`) have Retire, Edit, and View sources actions. Promoted insights (status=`promoted`) have Demote (→confirmed) and Retire. Retire and Demote are one-shot status updates. View sources reads from `insight_sources` joined with `observations` and returns an observation list.

- [ ] **Step 1: Add a query helper for sources**

Append to `better_memory/ui/queries.py`:

```python
def list_insight_sources(
    conn: sqlite3.Connection, *, insight_id: str
) -> list[ObservationListRow]:
    """Return the observations linked to ``insight_id`` via insight_sources."""
    rows = conn.execute(
        """
        SELECT o.id, o.content, o.component, o.theme, o.outcome, o.created_at
        FROM insight_sources s
        JOIN observations o ON o.id = s.observation_id
        WHERE s.insight_id = ?
        ORDER BY o.created_at DESC, o.id DESC
        """,
        (insight_id,),
    ).fetchall()
    return [
        ObservationListRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
```

- [ ] **Step 2: Write failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestInsightActions:
    def _insert_insight(
        self, conn: sqlite3.Connection, project: str, id: str,
        status: str = "confirmed"
    ) -> None:
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES (?, ?, ?, ?, ?, 'neutral')",
            (id, f"title-{id}", f"content-{id}", project, status),
        )
        conn.commit()

    def test_retire_moves_insight_to_retired(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")

        response = client.post(
            "/insights/i1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.data.strip() == b""
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'i1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_demote_promoted_to_confirmed(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "pr1", status="promoted")

        response = client.post(
            "/insights/pr1/demote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'pr1'"
        ).fetchone()
        assert row["status"] == "confirmed"

    def test_edit_form_and_save(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")

        form_response = client.get("/insights/i1/edit")
        assert form_response.status_code == 200
        assert b"<form" in form_response.data

        save_response = client.post(
            "/insights/i1/edit",
            data={"title": "new", "content": "new-content"},
            headers={"Origin": "http://localhost"},
        )
        assert save_response.status_code == 200
        assert b"new" in save_response.data
        row = conn.execute(
            "SELECT title FROM insights WHERE id = 'i1'"
        ).fetchone()
        assert row["title"] == "new"

    def test_view_sources_returns_linked_observations(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")
        _insert_observation(conn, project, "oA")
        conn.execute(
            "INSERT INTO insight_sources (insight_id, observation_id) "
            "VALUES ('i1', 'oA')"
        )
        conn.commit()

        response = client.get("/insights/i1/sources")
        assert response.status_code == 200
        assert b"obs-oA" in response.data

    def test_view_sources_empty(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")
        response = client.get("/insights/i1/sources")
        assert response.status_code == 200
        assert b"No source observations" in response.data
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestInsightActions -v`
Expected: FAIL — routes are stubs.

- [ ] **Step 4: Create sources template**

Create `better_memory/ui/templates/fragments/insight_sources.html`:

```html
<div class="insight-sources">
  <h4>Source observations ({{ rows|length }})</h4>
  {% if not rows %}
    <p class="muted">No source observations linked.</p>
  {% else %}
    <div class="card-list">
      {% for obs in rows %}
        {% include "fragments/observation_card_compact.html" %}
      {% endfor %}
    </div>
  {% endif %}
</div>
```

- [ ] **Step 5: Implement insight action routes**

In `better_memory/ui/app.py`:

```python
    @app.post("/insights/<id>/retire")
    def insight_retire(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        service.update(id, status="retired")
        return ""

    @app.post("/insights/<id>/demote")
    def insight_demote(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "promoted":
            abort(404)
        service.update(id, status="confirmed")
        return ""

    @app.get("/insights/<id>/edit")
    def insight_edit(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        return render_template(
            "fragments/insight_edit_form.html",
            row=existing,
            save_url=url_for("insight_edit_save", id=id),
            cancel_url=url_for("insight_compact_card", id=id),
        )

    @app.post("/insights/<id>/edit")
    def insight_edit_save(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        title = request.form.get("title", existing.title)
        content = request.form.get("content", existing.content)
        service.update(id, title=title, content=content)
        updated = service.get(id)
        template = (
            "fragments/insight_card_compact.html"
            if updated.status == "confirmed"
            else "fragments/promoted_card_compact.html"
        )
        # Pass the insight under both aliases — the two templates use
        # different variable names (`i` vs `p`).
        return render_template(template, i=updated, p=updated)

    @app.get("/insights/<id>/compact")
    def insight_compact_card(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        template = (
            "fragments/insight_card_compact.html"
            if existing.status == "confirmed"
            else "fragments/promoted_card_compact.html"
        )
        return render_template(template, i=existing, p=existing)

    @app.get("/insights/<id>/sources")
    def insight_sources(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None:
            abort(404)
        conn = app.extensions["db_connection"]
        rows = queries.list_insight_sources(conn, insight_id=id)
        if not rows:
            return '<div class="insight-sources"><p class="muted">No source observations linked.</p></div>'
        return render_template("fragments/insight_sources.html", rows=rows)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v tests/ui/test_queries.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/fragments/insight_sources.html better_memory/ui/app.py better_memory/ui/queries.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: insight actions — retire, demote, edit, view sources"
```

---

## Task 12: Promote stub + Merge picker stub

**Files:**
- Create: `better_memory/ui/templates/fragments/promotion_stub_modal.html`
- Create: `better_memory/ui/templates/fragments/merge_picker.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Real promotion ships in Phase 7. Real merge logic ships in Phase 3. Phase 2 needs the UI hooks in place so the existing buttons don't 404. Both render a fragment explaining the deferred status.

- [ ] **Step 1: Write failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestPromoteStub:
    def test_promote_renders_deferred_message(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 't', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/insights/i1/promote")
        assert response.status_code == 200
        assert b"Phase 7" in response.data


class TestMergePicker:
    def test_picker_lists_other_pending_candidates(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_candidate(conn, project, "c2")
        _insert_candidate(conn, project, "c3")

        response = client.get("/candidates/c1/merge")
        assert response.status_code == 200
        body = response.data.decode()
        # The two OTHER candidates listed as merge targets.
        assert "c2" in body
        assert "c3" in body
        # Self is NOT listed.
        assert "id=\"merge-target-c1\"" not in body

    def test_merge_post_returns_phase3_stub(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_candidate(conn, project, "c2")

        response = client.post(
            "/candidates/c1/merge?target=c2",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert b"Phase 3" in response.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestPromoteStub tests/ui/test_pipeline.py::TestMergePicker -v`
Expected: FAIL — stubs still return empty string.

- [ ] **Step 3: Create templates**

Create `better_memory/ui/templates/fragments/promotion_stub_modal.html`:

```html
<div class="modal">
  <div class="modal-body">
    <h3>Promotion workflow</h3>
    <p>The promotion workflow ships in Phase 7.</p>
    <p>When it lands, this modal will let you pick a knowledge-base destination, edit a draft, and save a markdown file.</p>
    <button hx-on:click="document.getElementById('modal').innerHTML = ''">Close</button>
  </div>
</div>
```

Create `better_memory/ui/templates/fragments/merge_picker.html`:

```html
<div class="card card-merge-picker" data-id="{{ source.id }}">
  <h4>Merge <em>{{ source.title }}</em> into…</h4>
  {% if not targets %}
    <p class="muted">No other pending candidates in this project.</p>
    <button hx-get="{{ url_for('candidate_compact_card', id=source.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Cancel</button>
  {% else %}
    <ul class="merge-target-list">
      {% for target in targets %}
        <li id="merge-target-{{ target.id }}">
          <span>{{ target.title }}</span>
          <button hx-post="{{ url_for('candidate_merge', id=source.id) }}?target={{ target.id }}"
                  hx-target="closest .card" hx-swap="outerHTML">Merge into this</button>
        </li>
      {% endfor %}
    </ul>
    <button hx-get="{{ url_for('candidate_compact_card', id=source.id) }}"
            hx-target="closest .card" hx-swap="outerHTML">Cancel</button>
  {% endif %}
</div>
```

- [ ] **Step 4: Wire the routes**

In `better_memory/ui/app.py`:

```python
    @app.get("/insights/<id>/promote")
    def insight_promote(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "confirmed":
            abort(404)
        return render_template("fragments/promotion_stub_modal.html")

    @app.get("/candidates/<id>/merge")
    def candidate_merge_picker(id: str) -> str:
        service = app.extensions["insight_service"]
        source = service.get(id)
        if source is None or source.status != "pending_review":
            abort(404)
        conn = app.extensions["db_connection"]
        project = _project_name()
        all_candidates = queries.list_candidates(conn, project=project)
        targets = [t for t in all_candidates if t.id != id]
        return render_template(
            "fragments/merge_picker.html", source=source, targets=targets
        )

    @app.post("/candidates/<id>/merge")
    def candidate_merge(id: str) -> str:
        # Phase 3 ships the real merge logic (ConsolidationService).
        return (
            '<div class="card card-error">'
            "<p>Merge cannot run: ConsolidationService ships in <strong>Phase 3</strong>. "
            "The picker is live; the logic isn't. Retry after Phase 3 lands.</p>"
            "</div>"
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/ better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: promote stub modal + merge picker (merge logic Phase 3)"
```

---

## Task 13: jobs.py + Consolidation button (Phase 3 stub)

**Files:**
- Create: `better_memory/ui/jobs.py`
- Create: `better_memory/ui/templates/fragments/consolidation_job.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_pipeline.py`

**Context:** Spec §4: the Consolidation button starts a background job. Phase 2 wires the mechanism (lock, job registry, route, polling fragment) but the "job" just immediately returns "ConsolidationService ships in Phase 3 — this button wakes up then". Phase 3 replaces the job body with the real dry-run.

- [ ] **Step 1: Write failing tests**

Append to `tests/ui/test_pipeline.py`:

```python
class TestConsolidationButton:
    def test_click_returns_job_fragment_with_phase3_message(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert b"Phase 3" in response.data
        # Response carries HX-Trigger: job-complete so the candidates
        # panel listener refreshes.
        assert response.headers.get("HX-Trigger") == "job-complete"

    def test_jobs_endpoint_returns_fragment(
        self, client: FlaskClient
    ) -> None:
        import re

        response = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        # Extract the job-id from the rendered fragment's data attribute.
        match = re.search(rb'data-job-id="([a-f0-9]+)"', response.data)
        assert match is not None, "consolidation response must render a job fragment"
        job_id = match.group(1).decode()

        # GET /jobs/<id> returns the job's fragment (Phase 3 message).
        get_resp = client.get(f"/jobs/{job_id}")
        assert get_resp.status_code == 200
        assert b"Phase 3" in get_resp.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_pipeline.py::TestConsolidationButton -v`
Expected: FAIL — route stub returns empty.

- [ ] **Step 3: Create `jobs.py`**

Create `better_memory/ui/jobs.py`:

```python
"""Background-job registry for the Management UI.

Phase 2 provides the plumbing — a lock, a current-job-id, a record of
job state. Phase 3 replaces the job body with ``ConsolidationService``
calls. The public surface is deliberately minimal so Phase 3's
implementation can slot in without churn.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

_lock = threading.Lock()
_current_job_id: str | None = None
_jobs: dict[str, "JobState"] = {}


@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str


def current_job_id() -> str | None:
    """Return the currently-running job id, if any."""
    return _current_job_id


def start_phase3_stub_job() -> JobState:
    """Start a placeholder job that records a Phase-3-not-ready message.

    Phase 3 replaces this function with one that spawns a real
    threading.Thread running ConsolidationService.dry_run().
    """
    global _current_job_id
    if not _lock.acquire(blocking=False):
        # Another job is active — return the existing state.
        existing_id = _current_job_id
        if existing_id is not None and existing_id in _jobs:
            return _jobs[existing_id]
        # Shouldn't happen: lock held but no job recorded. Fall through.
    try:
        job_id = uuid4().hex
        state = JobState(
            id=job_id,
            status="complete",
            message="ConsolidationService ships in Phase 3. This button will run the real dry-run then.",
        )
        _jobs[job_id] = state
        _current_job_id = job_id
        # Phase 2: the "job" is synchronous and complete immediately.
        # Phase 3 will make this async and clear _current_job_id on thread exit.
        return state
    finally:
        _lock.release()
        _current_job_id = None  # Phase 2: job is done by the time we return.


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)
```

- [ ] **Step 4: Create job fragment template**

Create `better_memory/ui/templates/fragments/consolidation_job.html`:

```html
<div class="consolidation-job" data-job-id="{{ job.id }}">
  <div class="job-status job-status-{{ job.status }}">
    {% if job.status == "running" %}
      <span class="spinner">⟳</span>
    {% elif job.status == "complete" %}
      <span class="checkmark">✓</span>
    {% elif job.status == "failed" %}
      <span class="cross">✗</span>
    {% endif %}
    {{ job.status }}
  </div>
  <div class="job-message">{{ job.message }}</div>
</div>
```

- [ ] **Step 5: Wire the routes**

In `better_memory/ui/app.py`, replace the consolidate stub and add `/jobs/<id>`:

```python
    @app.post("/pipeline/consolidate")
    def pipeline_consolidate() -> tuple[str, int, dict[str, str]]:
        state = jobs.start_phase3_stub_job()
        # Fire HX-Trigger so listeners (e.g. candidates panel) refresh.
        rendered = render_template("fragments/consolidation_job.html", job=state)
        return rendered, 200, {"HX-Trigger": "job-complete"}

    @app.get("/jobs/<id>")
    def jobs_get(id: str) -> str:
        state = jobs.get_job(id)
        if state is None:
            abort(404)
        return render_template("fragments/consolidation_job.html", job=state)
```

Add `from better_memory.ui import jobs` at module top.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ui/test_pipeline.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/jobs.py better_memory/ui/templates/fragments/consolidation_job.html better_memory/ui/app.py tests/ui/test_pipeline.py
git commit -m "UI Phase 2: jobs.py + consolidation button (Phase 3 stub)"
```

---

## Task 14: Kanban CSS

**Files:**
- Modify: `better_memory/ui/static/app.css`

**Context:** Append kanban styles. Phase 1's CSS covered the header and placeholders. Phase 2 needs:
- Summary bar pills with counts
- Panel (card-list) layout
- Card states: compact vs expanded
- Action buttons
- Empty state styling
- Modal
- Consolidation job progress
- Edit form
- Merge picker

- [ ] **Step 1: Append kanban styles**

Append to `better_memory/ui/static/app.css`:

```css
/* --------------------------------------------------------------------
 * Kanban (Phase 2)
 * -------------------------------------------------------------------- */

.kanban {
  max-width: 1024px;
  margin: 0 auto;
}

.summary-bar {
  display: flex;
  gap: 8px;
  align-items: stretch;
  margin-bottom: 18px;
}

.pill {
  flex: 1;
  padding: 12px 14px;
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  color: #e0e0e0;
  cursor: pointer;
  text-align: center;
  font-family: inherit;
  transition: background 0.12s;
}

.pill:hover {
  background: #222;
}

.pill.active {
  background: #2a2a2a;
  border-color: #444;
}

.pill.candidates-pill {
  border-color: #3a2a1a;
}

.pill.candidates-pill .count {
  color: #e0a060;
}

.pill .count {
  display: block;
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 4px;
}

.pill .stage-label {
  font-size: 12px;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.summary-bar .toolbar {
  display: flex;
  align-items: center;
  padding-left: 16px;
  border-left: 1px solid #2a2a2a;
}

.run-consolidation {
  padding: 10px 16px;
  background: #2a2a2a;
  border: 1px solid #3a3a3a;
  border-radius: 4px;
  color: #e0e0e0;
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
}

.run-consolidation:hover {
  background: #333;
}

.card-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.card {
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  padding: 10px 14px;
  cursor: pointer;
  transition: border-color 0.12s;
}

.card:hover {
  border-color: #3a3a3a;
}

.card-expanded {
  cursor: default;
}

.card-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: #888;
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.card-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 4px;
}

.card-content {
  font-size: 13px;
  color: #ccc;
  line-height: 1.55;
  margin: 8px 0;
  white-space: pre-wrap;
}

.card-actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.card-actions button {
  padding: 4px 10px;
  background: #2a2a2a;
  border: 1px solid #3a3a3a;
  border-radius: 3px;
  color: #e0e0e0;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
}

.card-actions button:hover {
  background: #333;
}

.card-actions button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.polarity {
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
}

.polarity-do { background: #1a2a1a; color: #90c060; }
.polarity-dont { background: #2a1a1a; color: #e08080; }
.polarity-neutral { background: #2a2a2a; color: #aaa; }

.outcome-success { color: #90c060; }
.outcome-failure { color: #e08080; }
.outcome-neutral { color: #aaa; }

.empty-state {
  padding: 40px 20px;
  text-align: center;
  color: #777;
  background: #141414;
  border: 1px dashed #2a2a2a;
  border-radius: 6px;
}

.collapse-me {
  margin-left: auto;
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 16px;
  padding: 0 6px;
}

.collapse-me:hover {
  color: #e0e0e0;
}

.card-edit label {
  display: block;
  margin-bottom: 8px;
  font-size: 12px;
  color: #999;
}

.card-edit input,
.card-edit textarea {
  display: block;
  width: 100%;
  padding: 6px 8px;
  margin-top: 4px;
  background: #0f0f0f;
  border: 1px solid #2a2a2a;
  border-radius: 3px;
  color: #e0e0e0;
  font-family: inherit;
  font-size: 13px;
}

.card-edit textarea {
  font-family: ui-monospace, Menlo, Consolas, monospace;
}

.card-merge-picker {
  background: #141414;
  border-color: #3a2a1a;
}

.merge-target-list {
  list-style: none;
  margin: 8px 0;
  padding: 0;
}

.merge-target-list li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid #222;
}

.card-error {
  background: #2a1a1a;
  border-color: #3a2a2a;
  color: #e0a0a0;
}

.consolidation-job {
  padding: 10px 14px;
  margin-bottom: 14px;
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  font-size: 13px;
}

.job-status {
  font-weight: 600;
  margin-bottom: 4px;
}

.job-status-complete .checkmark { color: #90c060; }
.job-status-failed .cross { color: #e08080; }

.insight-sources {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #2a2a2a;
}

.insight-sources h4 {
  margin: 0 0 8px 0;
  font-size: 13px;
  color: #ccc;
}

#modal:not(:empty) {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-body {
  max-width: 560px;
  padding: 24px;
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
}

.modal-body h3 {
  margin-top: 0;
}
```

- [ ] **Step 2: Run the existing static asset tests to confirm CSS still loads**

Run: `uv run pytest tests/ui/test_app.py::TestStaticAssets -v`
Expected: Both PASS.

- [ ] **Step 3: Commit**

```bash
git add better_memory/ui/static/app.css
git commit -m "UI Phase 2: kanban CSS — pills, cards, actions, empty states, modal"
```

---

## Task 15: Smoke-run and documentation

**Files:**
- No files modified — verification only.

**Context:** End-to-end smoke run against the user's real memory DB to confirm everything wires up.

- [ ] **Step 1: Seed a test candidate and run the UI**

**Important:** the UI derives `project` from `Path.cwd().name`. To match the user's real observations (which were written with their project as cwd), launch from the project root — not an arbitrary directory.

```bash
# Run from the better-memory project root so project name resolves correctly.
cd C:/Users/gethi/source/better-memory   # or wherever the repo lives

# Ensure no stale UI is running.
test -f ~/.better-memory/ui.url && rm ~/.better-memory/ui.url

# Launch UI.
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui &
sleep 2
URL=$(cat ~/.better-memory/ui.url)

# Observations column should show any observations the user already has.
curl -s "$URL/pipeline/panel/observations" | grep -c 'class="card'
# Expected: count of user's observations in the default project.

# Candidates column should show "No candidates pending" for a fresh DB.
curl -s "$URL/pipeline/panel/candidates" | grep -q "No candidates pending"

# Consolidate button should return Phase 3 message.
curl -s -X POST -H "Origin: $URL" "$URL/pipeline/consolidate" | grep -q "Phase 3"

# Shutdown cleanly.
curl -s -X POST -H "Origin: $URL" "$URL/shutdown" -o /dev/null
sleep 1
test ! -f ~/.better-memory/ui.url && echo "ui.url cleaned up" || echo "FAIL: ui.url remains"
```

Report any check that fails.

- [ ] **Step 2: Confirm deferred items are documented**

The following are intentionally deferred to later phases. Each has a placeholder route/fragment that returns a clear "Phase N ships X" message:

| Feature | Route | Defer to |
|---|---|---|
| `ConsolidationService.dry_run()` | `POST /pipeline/consolidate` | Phase 3 |
| Real merge logic | `POST /candidates/<id>/merge` | Phase 3 |
| Promotion modal workflow | `GET /insights/<id>/promote` | Phase 7 |
| View doc link on promoted cards | button disabled in compact card | Phase 7 |
| Smoke tests for Approve/Reject/Retire/Demote against real (consolidation-generated) data | Phase 3 test suite |

Phase 3's plan should include **smoke tests that exercise Phase 2's action buttons** — Approve on a real candidate, Reject on a real candidate, Retire on a confirmed insight produced by consolidation, Demote on a promoted insight. Phase 2 has unit/integration coverage using seeded fixtures; Phase 3 verifies end-to-end.

- [ ] **Step 3: No commit for verification-only step.**

---

## Self-Review Checklist

Before handoff, walk through spec §4 point by point.

| Spec §4 item | Task |
|---|---|
| Summary bar: 4 pills with counts | 5 |
| Candidates pill amber-tinted | 5 + 14 |
| Clicking pill swaps panel | 5 |
| "Run branch-and-sweep" button | 5 + 13 |
| "Close UI" button on far right | already from Phase 1 |
| Panel defaults to Candidates | 5 |
| Polling every 10 s on counts | already from Phase 1 (`/pipeline/badge` polling in base.html); also summary bar via pipeline page |
| Polling every 10 s on active panel | 5 (hx-trigger="load, every 10s" on #panel) |
| Compact card rendering | 6, 7 |
| Click-to-expand | 8 |
| Only one card expanded at a time | 9 |
| Per-stage actions table | 10, 11, 12 (stub for promote + merge) |
| Merge flow | 12 (picker UI; real logic Phase 3) |
| Consolidation button | 5, 13 |
| Cross-fragment refresh via HX-Trigger: job-complete | 5 (hx-trigger="job-complete from:body") + 13 (HX-Trigger header) |
| Single-job enforcement (threading.Lock + current_job_id) | 13 |
| Empty states for each stage | 6, 7 |

All spec §4 items have a corresponding task. Deferred items (merge logic, promotion workflow, ConsolidationService) are explicitly called out in the plan's Scope section and land in Phases 3 and 7.
