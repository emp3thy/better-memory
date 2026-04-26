# Episodic Memory Phase 8 — Episodes UI Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Flask **Episodes** tab — a day-grouped timeline of episodes for the current project with a per-episode drawer that lists owning observations + linked reflections and exposes close-as-{success,partial,abandoned,no_outcome} actions for open episodes. Wire the navigation to the new tab, hide old (now-broken) tabs, and surface a banner when prior-session episodes are unclosed.

**Architecture:** Two read helpers in `better_memory/ui/queries.py` (sync SELECT-only) feed the new templates: `episode_list_for_ui()` (timeline rows with observation/reflection counts) and `episode_detail()` (single episode + observations + reflections via `reflection_sources`). One small service-layer addition — `EpisodeService.close_by_id()` — lets the UI close an episode without binding the UI process to a session (the existing `close_active` is session-bound and unsuitable for cross-session UI closes). All routes follow the existing HTMX-partial pattern from `/pipeline`: full-page render on initial GET, drawer + actions swap inner HTML.

**Tech Stack:** Python 3.12 · Flask · Jinja2 · HTMX · SQLite + sqlite-vec + FTS5 · pytest · uv.

**Scope boundary.** Episodes tab only.

**Out of scope** (deferred):
- **Reflections tab** (Phase 9). The nav exposes a Reflections placeholder route returning a stub page; row-list/filter/drawer interactions land in Phase 9.
- **Stripping old UI surfaces** (Phase 10). Pipeline/Sweep/Knowledge/Audit/Graph routes and templates stay on disk; only their nav links are removed in this phase. The routes still 500 against dropped insight tables — same as today's `main` post-Phase 1 — and Phase 10 deletes them.
- **Retention MCP tool** (Phase 11).
- **End-to-end browser tests** (Phase 12 covers the cross-tab Playwright suite). The existing `tests/ui/test_browser.py` is module-level skipped pending Phase 12; we leave it untouched. Phase 8 verifies behaviour via the in-process Flask test client, which exercises the route handlers, query helpers, and template rendering end-to-end without HTMX-driven clicks.
- **`memory.close_episode` MCP cross-session bug.** The existing handler is session-bound; Phase 8 adds `close_by_id` for the UI but does **not** rewire the MCP handler. Tracked as a separate follow-up.

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §3 (lifecycle), §4 (schema), §8 (UI), §14 (build order).

**Reference plans:** Phase 2 (`2026-04-21-episodic-phase-2-episode-service.md`) for `EpisodeService` shape; UI Phase 1 (`2026-04-19-ui-phase-1-web-skeleton.md`) and UI Phase 2 (`2026-04-19-ui-phase-2-pipeline-kanban.md`) for the existing Flask + HTMX conventions this plan extends.

---

## File Structure

**New files:**
- `better_memory/ui/templates/episodes.html` — full-page Episodes tab (banner + timeline)
- `better_memory/ui/templates/fragments/panel_episodes.html` — timeline panel (HTMX swap target)
- `better_memory/ui/templates/fragments/episode_row.html` — one row in the timeline
- `better_memory/ui/templates/fragments/episode_drawer.html` — drawer with details + actions
- `better_memory/ui/templates/fragments/episode_banner.html` — "N unclosed episodes from prior sessions" banner
- `better_memory/ui/templates/reflections.html` — Phase 9 placeholder ("Coming in Phase 9")
- `tests/services/test_episode_close_by_id.py` — service-layer tests for the new method
- `tests/ui/test_episodes.py` — Flask test client tests for routes + drawer + close
- `tests/ui/test_queries_episodes.py` — query helper tests

**Modified files:**
- `better_memory/services/episode.py` — add `close_by_id` method
- `better_memory/ui/queries.py` — add `EpisodeRow`, `EpisodeDetail`, `episode_list_for_ui`, `episode_detail`, `unclosed_episode_count`
- `better_memory/ui/app.py` — register routes (`/episodes`, `/episodes/<id>/drawer`, `/episodes/<id>/close`, `/episodes/banner`, `/reflections`); flip `/` redirect to `/episodes`
- `better_memory/ui/templates/base.html` — replace nav: keep brand + Close UI button; show **Episodes** + **Reflections** tabs only; remove Pipeline / Sweep / Knowledge / Audit / Graph nav links
- `better_memory/ui/static/app.css` — add `.timeline`, `.episode-row`, `.outcome-badge`, `.episode-drawer`, `.banner-unclosed` styles

---

## Task 0: Create worktree off the integration branch

Phase 8 builds on every prior episodic phase. Branch from `episodic-phase-1-schema` (the integration branch where Phases 1-6 are merged) so the new schema, services, and MCP tools are all available.

**Files:**
- Create: worktree at `C:/Users/gethi/source/better-memory-episodic-phase-8-episodes-ui`

- [ ] **Step 1: Fetch and create worktree**

From the main checkout:

```bash
git fetch origin
git worktree add -b episodic-phase-8-episodes-ui \
  ../better-memory-episodic-phase-8-episodes-ui origin/episodic-phase-1-schema
```

- [ ] **Step 2: Verify worktree state**

```bash
cd ../better-memory-episodic-phase-8-episodes-ui
git status
```

Expected: `On branch episodic-phase-8-episodes-ui`, working tree clean.

- [ ] **Step 3: Verify baseline suite is green**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `344 passed, 141 skipped, 4 deselected` with zero failures.

---

## Task 1: Add `EpisodeService.close_by_id` for cross-session UI closes

`close_active` requires a `session_id` and walks `episode_sessions` to find the active row. The UI does not bind to an MCP session, so it cannot use that method to close an arbitrary prior-session episode. Add a sibling method that closes by episode id directly.

**Files:**
- Modify: `better_memory/services/episode.py` (append a new method to `EpisodeService`)
- Create: `tests/services/test_episode_close_by_id.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_episode_close_by_id.py`:

```python
"""Tests for EpisodeService.close_by_id (cross-session UI close)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestCloseById:
    def test_closes_open_episode_and_marks_all_session_bindings(
        self, conn, fixed_clock
    ):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        # Simulate a continuing-session bind from a second session.
        conn.execute(
            "INSERT INTO episode_sessions "
            "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
            (episode_id, "sess-2", "2026-04-25T11:00:00+00:00"),
        )
        conn.commit()

        closed = svc.close_by_id(
            episode_id=episode_id,
            outcome="abandoned",
            close_reason="abandoned",
            summary="user marked as abandoned in UI",
        )

        assert closed == episode_id
        ep = conn.execute(
            "SELECT ended_at, close_reason, outcome, summary "
            "FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert ep["ended_at"] == "2026-04-25T12:00:00+00:00"
        assert ep["close_reason"] == "abandoned"
        assert ep["outcome"] == "abandoned"
        assert ep["summary"] == "user marked as abandoned in UI"

        rows = conn.execute(
            "SELECT session_id, left_at FROM episode_sessions "
            "WHERE episode_id = ? ORDER BY session_id",
            (episode_id,),
        ).fetchall()
        assert len(rows) == 2
        # All open bindings get stamped; previously-closed bindings are
        # left alone.
        assert all(r["left_at"] == "2026-04-25T12:00:00+00:00" for r in rows)

    def test_raises_when_episode_does_not_exist(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Episode not found"):
            svc.close_by_id(
                episode_id="does-not-exist",
                outcome="abandoned",
                close_reason="abandoned",
            )

    def test_raises_when_episode_already_closed(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        svc.close_by_id(
            episode_id=episode_id, outcome="success", close_reason="goal_complete"
        )
        with pytest.raises(ValueError, match="already closed"):
            svc.close_by_id(
                episode_id=episode_id,
                outcome="abandoned",
                close_reason="abandoned",
            )

    def test_summary_is_optional(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        svc.close_by_id(
            episode_id=episode_id,
            outcome="no_outcome",
            close_reason="session_end_reconciled",
        )
        row = conn.execute(
            "SELECT summary FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row["summary"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/services/test_episode_close_by_id.py -v
```

Expected: 4 FAILs, all with `AttributeError: 'EpisodeService' object has no attribute 'close_by_id'`.

- [ ] **Step 3: Implement `close_by_id`**

Append after `close_active` in `better_memory/services/episode.py`:

```python
    def close_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close an episode by id, regardless of session binding.

        Used by the UI to close prior-session or cross-session episodes
        that ``close_active`` cannot reach (it requires a session_id and
        only finds the open episode bound to *that* session).

        Marks every still-open ``episode_sessions`` row for this episode
        as left at ``now`` so the invariant "exactly-one-open-binding-
        per-session" continues to hold for any session that was still
        bound.

        Raises ``ValueError`` if no episode with this id exists, or if
        the episode is already closed.
        """
        row = self._conn.execute(
            "SELECT ended_at FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Episode not found: {episode_id}")
        if row["ended_at"] is not None:
            raise ValueError(f"Episode already closed: {episode_id}")

        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT episode_close_by_id")
        try:
            conn.execute(
                "UPDATE episodes "
                "SET ended_at = ?, close_reason = ?, outcome = ?, summary = ? "
                "WHERE id = ?",
                (now, close_reason, outcome, summary, episode_id),
            )
            conn.execute(
                "UPDATE episode_sessions "
                "SET left_at = ? "
                "WHERE episode_id = ? AND left_at IS NULL",
                (now, episode_id),
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_close_by_id")
            conn.execute("RELEASE SAVEPOINT episode_close_by_id")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT episode_close_by_id")
        conn.commit()
        return episode_id
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/services/test_episode_close_by_id.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `348 passed, 141 skipped, 4 deselected` (4 new tests added).

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode_close_by_id.py
git commit -m "Phase 8: EpisodeService.close_by_id for cross-session UI closes"
```

---

## Task 2: Add `episode_list_for_ui` query helper

Sync SELECT helper that returns timeline rows (newest-first) with observation and reflection counts attached. The UI groups them by day in the template.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Create: `tests/ui/test_queries_episodes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_queries_episodes.py`:

```python
"""Tests for episode-related UI query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.ui.queries import EpisodeRow, episode_list_for_ui


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _ts(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, 0, 0, tzinfo=UTC).isoformat()


class TestEpisodeListForUi:
    def test_returns_empty_list_when_no_episodes(self, conn):
        rows = episode_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_rows_for_project_newest_first(self, conn):
        clock_a = lambda: datetime(2026, 4, 22, 9, 0, 0, tzinfo=UTC)
        clock_b = lambda: datetime(2026, 4, 24, 9, 0, 0, tzinfo=UTC)
        EpisodeService(conn, clock=clock_a).open_background(
            session_id="s1", project="proj-a"
        )
        EpisodeService(conn, clock=clock_b).open_background(
            session_id="s2", project="proj-a"
        )

        rows = episode_list_for_ui(conn, project="proj-a")
        assert len(rows) == 2
        assert rows[0].started_at == _ts(2026, 4, 24, 9)
        assert rows[1].started_at == _ts(2026, 4, 22, 9)

    def test_filters_by_project(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        EpisodeService(conn).open_background(session_id="s2", project="proj-b")

        rows = episode_list_for_ui(conn, project="proj-a")
        assert len(rows) == 1
        assert rows[0].project == "proj-a"

    def test_includes_goal_tech_outcome_close_reason(self, conn):
        svc = EpisodeService(conn)
        ep_id = svc.open_background(session_id="s1", project="proj-a")
        svc.start_foreground(
            session_id="s1", project="proj-a", goal="ship feature X", tech="python"
        )
        svc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )

        [row] = episode_list_for_ui(conn, project="proj-a")
        assert row.id == ep_id
        assert row.goal == "ship feature X"
        assert row.tech == "python"
        assert row.outcome == "success"
        assert row.close_reason == "goal_complete"
        assert row.ended_at is not None

    def test_observation_and_reflection_counts(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        # Two observations on this episode.
        for i in range(2):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, 'proj-a', ?)",
                (f"obs-{i}", f"content {i}", ep_id),
            )
        # One reflection sourced from one of those observations.
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, created_at, updated_at) "
            "VALUES ('refl-1', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-0')"
        )
        conn.commit()

        [row] = episode_list_for_ui(conn, project="proj-a")
        assert row.observation_count == 2
        assert row.reflection_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_queries_episodes.py -v
```

Expected: 5 FAILs, all with `ImportError: cannot import name 'EpisodeRow' from 'better_memory.ui.queries'`.

- [ ] **Step 3: Implement `EpisodeRow` + `episode_list_for_ui`**

Append to `better_memory/ui/queries.py`:

```python
@dataclass(frozen=True)
class EpisodeRow:
    """Read model for one row in the Episodes timeline."""

    id: str
    project: str
    tech: str | None
    goal: str | None
    started_at: str
    hardened_at: str | None
    ended_at: str | None
    close_reason: str | None
    outcome: str | None
    observation_count: int
    reflection_count: int


def episode_list_for_ui(
    conn: sqlite3.Connection, *, project: str
) -> list[EpisodeRow]:
    """Return episodes for ``project`` newest-first with attached counts.

    Counts are computed via correlated subqueries (one each for observations
    and reflection_sources joined back through observations) — the timeline
    is small (capped at recent activity for one project) so this is cheap
    and avoids fan-out duplication.
    """
    sql = """
        SELECT
            e.id,
            e.project,
            e.tech,
            e.goal,
            e.started_at,
            e.hardened_at,
            e.ended_at,
            e.close_reason,
            e.outcome,
            (
                SELECT COUNT(*) FROM observations o
                WHERE o.episode_id = e.id
            ) AS observation_count,
            (
                SELECT COUNT(DISTINCT rs.reflection_id)
                FROM reflection_sources rs
                JOIN observations o ON o.id = rs.observation_id
                WHERE o.episode_id = e.id
            ) AS reflection_count
        FROM episodes e
        WHERE e.project = ?
        ORDER BY e.started_at DESC, e.rowid DESC
    """
    return [
        EpisodeRow(
            id=r["id"],
            project=r["project"],
            tech=r["tech"],
            goal=r["goal"],
            started_at=r["started_at"],
            hardened_at=r["hardened_at"],
            ended_at=r["ended_at"],
            close_reason=r["close_reason"],
            outcome=r["outcome"],
            observation_count=r["observation_count"],
            reflection_count=r["reflection_count"],
        )
        for r in conn.execute(sql, (project,)).fetchall()
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_queries_episodes.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `353 passed, 141 skipped, 4 deselected` (5 new tests added).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_episodes.py
git commit -m "Phase 8: episode_list_for_ui query with obs + reflection counts"
```

---

## Task 3: Add `episode_detail` query helper

The drawer needs the episode plus the list of bound observations and the list of reflections those observations seeded.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_queries_episodes.py` (append a new class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_queries_episodes.py`:

```python
from better_memory.ui.queries import (
    EpisodeDetail,
    EpisodeObservationRow,
    EpisodeReflectionRow,
    episode_detail,
)


class TestEpisodeDetail:
    def test_returns_none_for_missing_episode(self, conn):
        assert episode_detail(conn, episode_id="nope") is None

    def test_returns_episode_with_no_observations_or_reflections(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        detail = episode_detail(conn, episode_id=ep_id)
        assert detail is not None
        assert detail.episode.id == ep_id
        assert detail.observations == []
        assert detail.reflections == []

    def test_returns_observations_newest_first(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        # Insert with explicit created_at to control ordering
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, component, theme, outcome, "
            "created_at) "
            "VALUES (?, ?, 'proj-a', ?, ?, ?, ?, ?)",
            ("obs-old", "older content", ep_id, "comp", "bug", "failure",
             "2026-04-24T08:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, component, theme, outcome, "
            "created_at) "
            "VALUES (?, ?, 'proj-a', ?, ?, ?, ?, ?)",
            ("obs-new", "newer content", ep_id, "comp", "decision", "success",
             "2026-04-24T10:00:00+00:00"),
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert [o.id for o in detail.observations] == ["obs-new", "obs-old"]
        assert detail.observations[0].component == "comp"
        assert detail.observations[0].theme == "decision"
        assert detail.observations[0].outcome == "success"

    def test_returns_reflections_with_owning_episode_outcome(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES ('obs-1', 'c', 'proj-a', ?)",
            (ep_id,),
        )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, status, created_at, updated_at) "
            "VALUES ('refl-1', 'lesson', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, 'pending_review', '2026-04-25T00:00:00+00:00', "
            "'2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-1')"
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert len(detail.reflections) == 1
        r = detail.reflections[0]
        assert r.id == "refl-1"
        assert r.title == "lesson"
        assert r.phase == "general"
        assert r.polarity == "do"
        assert r.status == "pending_review"
        assert r.confidence == 0.8

    def test_dedupes_reflections_when_multiple_observations_share_one(
        self, conn
    ):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        for i in range(2):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, 'proj-a', ?)",
                (f"obs-{i}", "c", ep_id),
            )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, created_at, updated_at) "
            "VALUES ('refl-1', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-0'), ('refl-1', 'obs-1')"
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert len(detail.reflections) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_queries_episodes.py::TestEpisodeDetail -v
```

Expected: 5 FAILs, all `ImportError`.

- [ ] **Step 3: Implement `episode_detail`**

Append to `better_memory/ui/queries.py` (note: we construct `Episode` inline rather than reusing the `_row_to_episode` helper from `services/episode.py`, which is module-private):

```python
from better_memory.services.episode import Episode


def _episode_from_row(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        project=row["project"],
        tech=row["tech"],
        goal=row["goal"],
        started_at=row["started_at"],
        hardened_at=row["hardened_at"],
        ended_at=row["ended_at"],
        close_reason=row["close_reason"],
        outcome=row["outcome"],
        summary=row["summary"],
    )


@dataclass(frozen=True)
class EpisodeObservationRow:
    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    created_at: str


@dataclass(frozen=True)
class EpisodeReflectionRow:
    id: str
    title: str
    phase: str
    polarity: str
    confidence: float
    status: str


@dataclass(frozen=True)
class EpisodeDetail:
    episode: Episode
    observations: list[EpisodeObservationRow]
    reflections: list[EpisodeReflectionRow]


def episode_detail(
    conn: sqlite3.Connection, *, episode_id: str
) -> EpisodeDetail | None:
    """Return one episode with its observations and seeded reflections.

    Returns ``None`` if no episode with this id exists.

    Reflections are deduped (an episode's two observations seeding the
    same reflection produces a single row).
    """
    ep_row = conn.execute(
        "SELECT * FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep_row is None:
        return None

    obs_rows = conn.execute(
        "SELECT id, content, component, theme, outcome, created_at "
        "FROM observations WHERE episode_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (episode_id,),
    ).fetchall()
    observations = [
        EpisodeObservationRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in obs_rows
    ]

    refl_rows = conn.execute(
        """
        SELECT DISTINCT
            r.id, r.title, r.phase, r.polarity, r.confidence, r.status
        FROM reflections r
        JOIN reflection_sources rs ON rs.reflection_id = r.id
        JOIN observations o ON o.id = rs.observation_id
        WHERE o.episode_id = ?
        ORDER BY r.confidence DESC, r.id ASC
        """,
        (episode_id,),
    ).fetchall()
    reflections = [
        EpisodeReflectionRow(
            id=r["id"],
            title=r["title"],
            phase=r["phase"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
        )
        for r in refl_rows
    ]

    return EpisodeDetail(
        episode=_episode_from_row(ep_row),
        observations=observations,
        reflections=reflections,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_queries_episodes.py -v
```

Expected: 10 PASS (5 + 5).

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `358 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_episodes.py
git commit -m "Phase 8: episode_detail query (observations + linked reflections)"
```

---

## Task 4: Add `unclosed_episode_count` for the banner

The banner shows when there are unclosed episodes from prior sessions. Returning a count keeps the polled HTMX endpoint cheap.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_queries_episodes.py` (append a new class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_queries_episodes.py`:

```python
from better_memory.ui.queries import unclosed_episode_count


class TestUnclosedEpisodeCount:
    def test_zero_when_no_open_episodes(self, conn):
        assert unclosed_episode_count(conn, project="proj-a") == 0

    def test_counts_open_episodes_for_project(self, conn):
        # Open background for proj-a (counts).
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        # Closed background for proj-a (does NOT count).
        svc2 = EpisodeService(conn)
        svc2.open_background(session_id="s2", project="proj-a")
        svc2.close_active(
            session_id="s2", outcome="abandoned", close_reason="abandoned"
        )
        # Open background for proj-b (does NOT count — wrong project).
        EpisodeService(conn).open_background(session_id="s3", project="proj-b")

        assert unclosed_episode_count(conn, project="proj-a") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_queries_episodes.py::TestUnclosedEpisodeCount -v
```

Expected: 2 FAILs, `ImportError`.

- [ ] **Step 3: Implement `unclosed_episode_count`**

Append to `better_memory/ui/queries.py`:

```python
def unclosed_episode_count(
    conn: sqlite3.Connection, *, project: str
) -> int:
    """Return the number of unclosed episodes for ``project``.

    Used by the Episodes-tab banner: any value > 0 surfaces the banner.
    Filtering to a specific session is intentionally NOT done here — the
    UI does not bind to a session, and the banner is meant to flag
    "anything still open" so the user can act on it.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes "
        "WHERE project = ? AND ended_at IS NULL",
        (project,),
    ).fetchone()
    return int(row["n"])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_queries_episodes.py -v
```

Expected: 12 PASS (10 + 2).

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `360 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_episodes.py
git commit -m "Phase 8: unclosed_episode_count for Episodes banner"
```

---

## Task 5: Wire the `EpisodeService` into the Flask app + flip root redirect

`create_app` currently constructs only an `InsightService`. Add an `EpisodeService` constructed against the same shared connection so close-actions can call into it. Flip the `/` redirect from `pipeline` to `episodes` (the route does not exist yet — implemented in Task 6 — but the endpoint name resolves at request time, so this is safe to land here).

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py` (update or add tests for the new redirect target)

- [ ] **Step 1: Update the existing root-redirect test**

The existing test `tests/ui/test_app.py::TestRootRedirect::test_redirects_to_pipeline` (around line 43) asserts the redirect lands on `/pipeline`. Replace it with:

```python
class TestRootRedirect:
    def test_redirects_to_episodes(self, client: FlaskClient) -> None:
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/episodes")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/ui/test_app.py::TestRootRedirect -v
```

Expected: FAIL — the route still redirects to `/pipeline`.

- [ ] **Step 3: Add `EpisodeService` and a placeholder `episodes` route**

In `better_memory/ui/app.py`:

Add the import near the existing service imports:

```python
from better_memory.services.episode import EpisodeService
```

Inside `create_app`, after `app.extensions["insight_service"] = InsightService(conn=db_conn)`:

```python
    app.extensions["episode_service"] = EpisodeService(conn=db_conn)
```

Replace the existing `root` handler:

```python
    @app.get("/")
    def root() -> Response:
        return redirect(url_for("episodes"))
```

Add a placeholder route below `root` (full implementation lands in Task 6):

```python
    @app.get("/episodes")
    def episodes() -> str:
        return "episodes-placeholder"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/ui/test_app.py::TestRootRedirect -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `360 passed, 141 skipped, 4 deselected` (one existing test was modified, not added).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_app.py
git commit -m "Phase 8: register EpisodeService + flip root redirect to /episodes"
```

---

## Task 6: Build the Episodes timeline page

Replace the placeholder route with a full-page render: banner + day-grouped timeline, populated from `episode_list_for_ui`.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/episodes.html`
- Create: `better_memory/ui/templates/fragments/panel_episodes.html`
- Create: `better_memory/ui/templates/fragments/episode_row.html`
- Create: `better_memory/ui/templates/fragments/episode_banner.html`
- Create: `tests/ui/test_episodes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_episodes.py`:

```python
"""Flask test-client tests for the Episodes tab."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.services.episode import EpisodeService


def _seed(db_path: Path, project: str = "better-memory") -> str:
    conn = connect(db_path)
    try:
        clock = lambda: datetime(2026, 4, 24, 9, 0, 0, tzinfo=UTC)
        svc = EpisodeService(conn, clock=clock)
        ep_id = svc.open_background(session_id="ui-test", project=project)
        svc.start_foreground(
            session_id="ui-test",
            project=project,
            goal="ship Episodes tab",
            tech="python",
        )
        return ep_id
    finally:
        conn.close()


class TestEpisodesPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/episodes")
        assert response.status_code == 200

    def test_empty_state_when_no_episodes(self, client: FlaskClient):
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert "No episodes yet" in body

    def test_shows_episode_in_timeline(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The UI infers project from cwd().name. Monkeypatch _project_name
        # to a stable value matching the seed.
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert "ship Episodes tab" in body
        assert "python" in body

    def test_groups_by_day_heading(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        # ISO date prefix from started_at appears in a day-group heading.
        assert "2026-04-24" in body


class TestEpisodesBanner:
    def test_banner_zero_when_no_open_episodes(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        response = client.get("/episodes/banner")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Empty banner partial — no "unclosed" text.
        assert "unclosed" not in body.lower()

    def test_banner_shows_count_when_open(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes/banner")
        body = response.get_data(as_text=True)
        assert "1" in body
        assert "unclosed" in body.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_episodes.py -v
```

Expected: 6 FAILs (placeholder route returns text not HTML, no banner route).

- [ ] **Step 3: Implement the templates**

Create `better_memory/ui/templates/episodes.html`:

```html
{% extends "base.html" %}
{% block title %}Episodes — better-memory{% endblock %}
{% block main %}
<section class="episodes">
  <div id="banner"
       hx-get="{{ url_for('episodes_banner') }}"
       hx-trigger="load, every 30s"
       hx-swap="innerHTML">
  </div>

  <div id="timeline"
       hx-get="{{ url_for('episodes_panel') }}"
       hx-trigger="load, every 30s, episode-closed from:body"
       hx-swap="innerHTML">
  </div>

  <div id="drawer"></div>
</section>
{% endblock %}
```

Create `better_memory/ui/templates/fragments/panel_episodes.html`:

```html
{% if not days %}
  <div class="empty-state">
    <p>No episodes yet. Open one with <code>memory.start_episode</code> in chat.</p>
  </div>
{% else %}
  <div class="timeline">
    {% for day, rows in days %}
      <h2 class="day-heading">{{ day }}</h2>
      <div class="card-list">
        {% for row in rows %}
          {% include "fragments/episode_row.html" %}
        {% endfor %}
      </div>
    {% endfor %}
  </div>
{% endif %}
```

Create `better_memory/ui/templates/fragments/episode_row.html`:

```html
<div class="episode-row {% if row.outcome %}outcome-{{ row.outcome }}{% endif %}"
     hx-get="{{ url_for('episodes_drawer', id=row.id) }}"
     hx-target="#drawer"
     hx-swap="innerHTML">
  <div class="row-main">
    <span class="goal">
      {% if row.goal %}{{ row.goal }}{% else %}background session{% endif %}
    </span>
    <span class="meta">
      <span class="project">{{ row.project }}</span>
      {% if row.tech %}<span class="tech">{{ row.tech }}</span>{% endif %}
      <span class="time">
        {{ row.started_at }}{% if row.ended_at %} → {{ row.ended_at }}{% endif %}
      </span>
    </span>
  </div>
  <div class="row-side">
    {% if row.outcome %}
      <span class="outcome-badge outcome-{{ row.outcome }}">{{ row.outcome }}</span>
    {% else %}
      <span class="outcome-badge outcome-open">open</span>
    {% endif %}
    {% if row.close_reason %}
      <span class="close-reason">{{ row.close_reason }}</span>
    {% endif %}
    <span class="counts">
      {{ row.observation_count }} obs · {{ row.reflection_count }} refl
    </span>
  </div>
</div>
```

Create `better_memory/ui/templates/fragments/episode_banner.html`:

```html
{% if count > 0 %}
  <div class="banner-unclosed">
    {{ count }} unclosed episode{{ 's' if count != 1 else '' }} —
    open the row{{ 's' if count != 1 else '' }} to reconcile.
  </div>
{% endif %}
```

- [ ] **Step 4: Replace the placeholder route + add panel + banner + drawer-stub routes**

In `better_memory/ui/app.py`, replace the placeholder `episodes` handler with:

```python
    @app.get("/episodes")
    def episodes() -> str:
        return render_template("episodes.html", active_tab="episodes")

    @app.get("/episodes/panel")
    def episodes_panel() -> str:
        conn = app.extensions["db_connection"]
        rows = queries.episode_list_for_ui(conn, project=_project_name())
        # Group by ISO date prefix (YYYY-MM-DD) of started_at, preserving
        # newest-first ordering. itertools.groupby works because rows are
        # already sorted by started_at DESC.
        from itertools import groupby

        days = [
            (day, list(group))
            for day, group in groupby(
                rows, key=lambda r: r.started_at[:10]
            )
        ]
        return render_template(
            "fragments/panel_episodes.html", days=days
        )

    @app.get("/episodes/banner")
    def episodes_banner() -> str:
        conn = app.extensions["db_connection"]
        count = queries.unclosed_episode_count(
            conn, project=_project_name()
        )
        return render_template(
            "fragments/episode_banner.html", count=count
        )

    # Drawer stub — required so episode_row.html can resolve url_for()
    # at render time. Task 7 replaces this with the real implementation.
    @app.get("/episodes/<id>/drawer")
    def episodes_drawer(id: str) -> str:
        return ""
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_episodes.py -v
```

Expected: 6 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `366 passed, 141 skipped, 4 deselected`.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py better_memory/ui/templates/episodes.html \
        better_memory/ui/templates/fragments/panel_episodes.html \
        better_memory/ui/templates/fragments/episode_row.html \
        better_memory/ui/templates/fragments/episode_banner.html \
        tests/ui/test_episodes.py
git commit -m "Phase 8: Episodes timeline page with day grouping + banner"
```

---

## Task 7: Build the episode drawer

Click on a row → drawer fetches `/episodes/<id>/drawer` → drawer renders full episode + observations + reflections + close-actions for open episodes.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/episode_drawer.html`
- Modify: `tests/ui/test_episodes.py` (add `TestEpisodeDrawer`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_episodes.py`:

```python
class TestEpisodeDrawer:
    def test_404_for_unknown_episode(self, client: FlaskClient):
        response = client.get("/episodes/does-not-exist/drawer")
        assert response.status_code == 404

    def test_renders_drawer_for_open_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")
        response = client.get(f"/episodes/{ep_id}/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "ship Episodes tab" in body
        # Open-episode actions present.
        assert "Close as success" in body
        assert "Close as partial" in body
        assert "Close as abandoned" in body
        assert "Close as no_outcome" in body
        assert "Continuing" in body

    def test_omits_close_actions_for_closed_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")
        # Close it.
        conn = connect(tmp_db)
        try:
            EpisodeService(conn).close_active(
                session_id="ui-test",
                outcome="success",
                close_reason="goal_complete",
            )
        finally:
            conn.close()

        response = client.get(f"/episodes/{ep_id}/drawer")
        body = response.get_data(as_text=True)
        assert "Close as success" not in body
        assert "Continuing" not in body
        assert "success" in body  # outcome badge still rendered
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_episodes.py::TestEpisodeDrawer -v
```

Expected: 3 FAILs (route not yet defined → 404 for all).

- [ ] **Step 3: Add the drawer template**

Create `better_memory/ui/templates/fragments/episode_drawer.html`:

```html
<div class="episode-drawer" id="episode-drawer-{{ detail.episode.id }}">
  <header class="drawer-header">
    <h3>
      {% if detail.episode.goal %}
        {{ detail.episode.goal }}
      {% else %}
        background session
      {% endif %}
    </h3>
    <button class="close-drawer"
            type="button"
            onclick="document.getElementById('drawer').innerHTML = '';">
      ×
    </button>
  </header>

  <dl class="drawer-meta">
    <dt>Project</dt><dd>{{ detail.episode.project }}</dd>
    {% if detail.episode.tech %}
      <dt>Tech</dt><dd>{{ detail.episode.tech }}</dd>
    {% endif %}
    <dt>Started</dt><dd>{{ detail.episode.started_at }}</dd>
    {% if detail.episode.hardened_at %}
      <dt>Hardened</dt><dd>{{ detail.episode.hardened_at }}</dd>
    {% endif %}
    {% if detail.episode.ended_at %}
      <dt>Ended</dt><dd>{{ detail.episode.ended_at }}</dd>
      <dt>Close reason</dt><dd>{{ detail.episode.close_reason }}</dd>
      <dt>Outcome</dt><dd>{{ detail.episode.outcome }}</dd>
      {% if detail.episode.summary %}
        <dt>Summary</dt><dd>{{ detail.episode.summary }}</dd>
      {% endif %}
    {% endif %}
  </dl>

  {% if not detail.episode.ended_at %}
    <div class="drawer-actions">
      {% for outcome in ['success', 'partial', 'abandoned', 'no_outcome'] %}
        <button class="action-close action-{{ outcome }}"
                hx-post="{{ url_for('episode_close', id=detail.episode.id, outcome=outcome) }}"
                hx-target="#drawer"
                hx-swap="innerHTML">
          Close as {{ outcome }}
        </button>
      {% endfor %}
      <button class="action-continuing"
              type="button"
              onclick="document.getElementById('drawer').innerHTML = '';">
        Continuing (no-op)
      </button>
    </div>
  {% endif %}

  <section class="drawer-section">
    <h4>Observations ({{ detail.observations | length }})</h4>
    {% if detail.observations %}
      <ul class="observation-list">
        {% for obs in detail.observations %}
          <li class="observation-item outcome-{{ obs.outcome }}">
            <div class="obs-content">{{ obs.content }}</div>
            <div class="obs-meta">
              {% if obs.component %}<span>{{ obs.component }}</span>{% endif %}
              {% if obs.theme %}<span>{{ obs.theme }}</span>{% endif %}
              <span>{{ obs.outcome }}</span>
              <span>{{ obs.created_at }}</span>
            </div>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="empty-inline">No observations yet.</p>
    {% endif %}
  </section>

  <section class="drawer-section">
    <h4>Reflections seeded ({{ detail.reflections | length }})</h4>
    {% if detail.reflections %}
      <ul class="reflection-list">
        {% for refl in detail.reflections %}
          <li class="reflection-item polarity-{{ refl.polarity }}">
            <span class="title">{{ refl.title }}</span>
            <span class="phase">{{ refl.phase }}</span>
            <span class="polarity">{{ refl.polarity }}</span>
            <span class="confidence">{{ '%.2f' | format(refl.confidence) }}</span>
            <span class="status">{{ refl.status }}</span>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="empty-inline">No reflections seeded by this episode yet.</p>
    {% endif %}
  </section>
</div>
```

- [ ] **Step 4: Replace the drawer stub with the real handler + add close-action stub**

In `better_memory/ui/app.py`, replace the Task 6 stub `episodes_drawer` with:

```python
    @app.get("/episodes/<id>/drawer")
    def episodes_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.episode_detail(conn, episode_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/episode_drawer.html", detail=detail
        )

    # Close-action stub — required so episode_drawer.html can resolve
    # url_for('episode_close', ...) at render time. Task 8 replaces.
    @app.post("/episodes/<id>/close")
    def episode_close(id: str) -> tuple[str, int]:
        return "", 200
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_episodes.py::TestEpisodeDrawer -v
```

Expected: 3 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `369 passed, 141 skipped, 4 deselected`.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/episode_drawer.html \
        tests/ui/test_episodes.py
git commit -m "Phase 8: episode drawer with observations + reflections + actions"
```

---

## Task 8: Wire close-as-{success,partial,abandoned,no_outcome}

POST `/episodes/<id>/close?outcome=...` calls `EpisodeService.close_by_id` with a default `close_reason` derived from the outcome (matching the MCP handler's mapping in `server.py:539`). On success, returns the freshly-rendered drawer (showing the closed-episode read-only view) and fires an `HX-Trigger: episode-closed` header so the timeline polls fresh.

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_episodes.py` (add `TestEpisodeClose`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_episodes.py`:

```python
class TestEpisodeClose:
    def test_close_as_success_marks_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")

        response = client.post(
            f"/episodes/{ep_id}/close?outcome=success",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "episode-closed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT outcome, close_reason FROM episodes WHERE id = ?",
                (ep_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "success"
        assert row["close_reason"] == "goal_complete"

    def test_close_as_abandoned_uses_abandoned_reason(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")

        client.post(
            f"/episodes/{ep_id}/close?outcome=abandoned",
            headers={"Origin": "http://localhost"},
        )

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT close_reason FROM episodes WHERE id = ?",
                (ep_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["close_reason"] == "abandoned"

    def test_close_returns_404_for_unknown_episode(
        self, client: FlaskClient
    ):
        response = client.post(
            "/episodes/does-not-exist/close?outcome=success",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 404

    def test_close_returns_400_for_invalid_outcome(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")

        response = client.post(
            f"/episodes/{ep_id}/close?outcome=bogus",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400

    def test_close_returns_409_for_already_closed_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")
        # First close: success.
        client.post(
            f"/episodes/{ep_id}/close?outcome=success",
            headers={"Origin": "http://localhost"},
        )
        # Second close: already closed.
        response = client.post(
            f"/episodes/{ep_id}/close?outcome=abandoned",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_episodes.py::TestEpisodeClose -v
```

Expected: 5 FAILs (route not defined).

- [ ] **Step 3: Replace the close-action stub with the real handler**

In `better_memory/ui/app.py`, replace the Task 7 stub `episode_close` with the real handler (defined inside `create_app`, matching the indentation of surrounding handlers):

```python
    _DEFAULT_CLOSE_REASONS = {
        "success": "goal_complete",
        "partial": "superseded",
        "abandoned": "abandoned",
        "no_outcome": "session_end_reconciled",
    }

    @app.post("/episodes/<id>/close")
    def episode_close(id: str) -> tuple[str, int, dict[str, str]]:
        outcome = request.args.get("outcome", "")
        if outcome not in _DEFAULT_CLOSE_REASONS:
            return (
                f'<div class="card card-error">'
                f"<p>Invalid outcome: {escape(outcome)}</p>"
                "</div>"
            ), 400, {}
        conn = app.extensions["db_connection"]
        if queries.episode_detail(conn, episode_id=id) is None:
            abort(404)
        try:
            app.extensions["episode_service"].close_by_id(
                episode_id=id,
                outcome=outcome,
                close_reason=_DEFAULT_CLOSE_REASONS[outcome],
            )
        except ValueError as exc:
            # close_by_id raises for "already closed" or "not found".
            # We already checked existence, so this path is the
            # already-closed race — return 409 with an error card.
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        # Re-render the drawer (now showing the closed view) and fire
        # episode-closed so the timeline reloads.
        detail = queries.episode_detail(conn, episode_id=id)
        rendered = render_template(
            "fragments/episode_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "episode-closed"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_episodes.py::TestEpisodeClose -v
```

Expected: 5 PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `374 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_episodes.py
git commit -m "Phase 8: POST /episodes/<id>/close with outcome → close_reason map"
```

---

## Task 9: Add the Reflections placeholder route

Phase 9 builds the full Reflections tab. For Phase 8 we just need a route the nav can link to without 404-ing.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/reflections.html`
- Modify: `tests/ui/test_episodes.py` (add `TestReflectionsPlaceholder`)

- [ ] **Step 1: Write the failing test**

Append to `tests/ui/test_episodes.py`:

```python
class TestReflectionsPlaceholder:
    def test_reflections_route_returns_200(self, client: FlaskClient):
        response = client.get("/reflections")
        assert response.status_code == 200
        assert b"Coming in Phase 9" in response.data
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/ui/test_episodes.py::TestReflectionsPlaceholder -v
```

Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the route + template**

Create `better_memory/ui/templates/reflections.html`:

```html
{% extends "base.html" %}
{% block title %}Reflections — better-memory{% endblock %}
{% block main %}
<section class="placeholder">
  <h1>Reflections</h1>
  <p>Coming in Phase 9.</p>
</section>
{% endblock %}
```

In `better_memory/ui/app.py`, add below `episode_close`:

```python
    @app.get("/reflections")
    def reflections() -> str:
        return render_template("reflections.html", active_tab="reflections")
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/ui/test_episodes.py::TestReflectionsPlaceholder -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `375 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py better_memory/ui/templates/reflections.html \
        tests/ui/test_episodes.py
git commit -m "Phase 8: Reflections placeholder route for Phase 9 nav stub"
```

---

## Task 10: Replace the nav in `base.html`

Hide Pipeline / Sweep / Knowledge / Audit / Graph from the nav. Show Episodes (active when `active_tab == 'episodes'`) + Reflections. The badge polling loop on Pipeline goes away.

**Files:**
- Modify: `better_memory/ui/templates/base.html`
- Modify: `tests/ui/test_app.py` (update or add tests for the new nav)

- [ ] **Step 1: Add nav assertions**

The only existing test asserting old-tab presence is `tests/ui/test_app.py::TestLayoutShell::test_pipeline_renders_base_layout` (around line 49), which is already `@pytest.mark.skip`-decorated and tests the broken `/pipeline` route. Leave it skipped — Phase 10 deletes that test along with the route.

The `TestEmptyViews` tests in `test_app.py` GET `/sweep`, `/knowledge`, `/audit`, `/graph` and assert page-body content. Those routes still render their templates (only the nav links are removed in this task), so the tests continue to pass unchanged.

Add a new `TestNav` class to `tests/ui/test_app.py`:

```python
class TestNav:
    def test_nav_shows_episodes_and_reflections(self, client: FlaskClient) -> None:
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert ">Episodes<" in body
        assert ">Reflections<" in body

    def test_nav_hides_old_tabs(self, client: FlaskClient) -> None:
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        for label in ("Pipeline", "Sweep", "Knowledge", "Audit", "Graph"):
            assert f">{label}<" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_app.py::TestNav -v
```

Expected: FAILs (old labels still in nav).

- [ ] **Step 3: Replace the nav block**

In `better_memory/ui/templates/base.html`, replace the `<nav class="tabs">` block:

```html
    <nav class="tabs">
      <a class="tab {% if active_tab == 'episodes' %}active{% endif %}" href="{{ url_for('episodes') }}">Episodes</a>
      <a class="tab {% if active_tab == 'reflections' %}active{% endif %}" href="{{ url_for('reflections') }}">Reflections</a>
    </nav>
```

The Pipeline badge HTMX polling block, all five removed nav links, and the badge `<span>` go away.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_app.py::TestNav -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `377 passed, 141 skipped, 4 deselected` (2 new tests added; any test that previously asserted old-tab presence has already been updated in Step 1).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/base.html tests/ui/test_app.py
git commit -m "Phase 8: replace nav with Episodes + Reflections only"
```

---

## Task 11: Add CSS for the new surfaces

Match the existing pipeline.html / panel_*.html visual language: `card-list`, `pill`, etc.

**Files:**
- Modify: `better_memory/ui/static/app.css`

- [ ] **Step 1: Append the new styles**

Append to `better_memory/ui/static/app.css`:

```css
/* ---------- Episodes tab ---------- */

.banner-unclosed {
  background: #fff4d6;
  border: 1px solid #e6c95a;
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  margin-bottom: 1rem;
}

.timeline {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.day-heading {
  font-size: 0.85rem;
  font-weight: 600;
  text-transform: uppercase;
  color: #666;
  margin: 0 0 0.5rem 0;
}

.episode-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #fff;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  padding: 0.6rem 0.85rem;
  cursor: pointer;
  transition: background 0.1s;
}

.episode-row:hover {
  background: #f6f9fc;
}

.episode-row .row-main { display: flex; flex-direction: column; gap: 0.2rem; }
.episode-row .goal { font-weight: 500; }
.episode-row .meta { font-size: 0.85rem; color: #666; display: flex; gap: 0.5rem; }
.episode-row .row-side { display: flex; align-items: center; gap: 0.5rem; }

.outcome-badge {
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 8px;
  text-transform: uppercase;
}

.outcome-badge.outcome-success   { background: #d6f3d6; color: #1a4f1a; }
.outcome-badge.outcome-partial   { background: #fff0c0; color: #6f4f00; }
.outcome-badge.outcome-abandoned { background: #f3d6d6; color: #6f1a1a; }
.outcome-badge.outcome-no_outcome{ background: #e0e0e0; color: #444; }
.outcome-badge.outcome-open      { background: #d6e4ff; color: #1a3a6f; }

.counts { font-size: 0.8rem; color: #666; }

/* ---------- Episode drawer ---------- */

.episode-drawer {
  background: #fafbfc;
  border: 1px solid #d0d0d0;
  border-radius: 6px;
  padding: 1rem 1.2rem;
  margin-top: 1.5rem;
}

.episode-drawer .drawer-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.episode-drawer .drawer-meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.25rem 1rem;
  margin: 0.75rem 0;
  font-size: 0.9rem;
}

.episode-drawer .drawer-meta dt { color: #666; font-weight: 500; }
.episode-drawer .drawer-meta dd { margin: 0; }

.drawer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0.75rem 0;
}

.drawer-actions button {
  padding: 0.35rem 0.7rem;
  border: 1px solid #c0c0c0;
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
}

.drawer-actions button:hover { background: #f0f4fa; }

.drawer-section {
  margin-top: 1rem;
  border-top: 1px solid #e0e0e0;
  padding-top: 0.75rem;
}

.observation-list, .reflection-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.observation-item, .reflection-item {
  padding: 0.4rem 0;
  border-bottom: 1px dashed #e0e0e0;
  font-size: 0.9rem;
}

.observation-item:last-child, .reflection-item:last-child {
  border-bottom: none;
}

.obs-meta { display: flex; gap: 0.5rem; color: #666; font-size: 0.8rem; }
.empty-inline { color: #888; font-style: italic; }
.placeholder { padding: 2rem; text-align: center; color: #666; }
```

- [ ] **Step 2: Run the full suite (no behaviour change, just sanity)**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `377 passed, 141 skipped, 4 deselected` (no change — CSS is not exercised in tests).

- [ ] **Step 3: Commit**

```bash
git add better_memory/ui/static/app.css
git commit -m "Phase 8: CSS for Episodes tab — timeline, drawer, banner, badges"
```

---

## Task 12: Update CLAUDE.md skill snippet to reference the Episodes tab

The snippet currently mentions "the Episodes UI surface (Phase 8+) will eventually let users reconcile in bulk." Phase 8 ships the surface — update the snippet to mention it as a reconcile channel alongside the in-chat prompt.

**Files:**
- Modify: `better_memory/skills/CLAUDE.snippet.md`

- [ ] **Step 1: Read the current snippet section on reconciliation**

```bash
grep -n "Episodes UI\|reconcile in bulk\|Phase 8" better_memory/skills/CLAUDE.snippet.md
```

- [ ] **Step 2: Update the wording**

Replace the line `the Episodes UI surface (Phase 8+) will eventually let users reconcile in bulk.` with:

```
The Episodes tab in the management UI also lists unclosed episodes —
clicking a row opens a drawer with the same close actions, useful for
bulk reconcile or follow-up the LLM declined to handle in chat.
```

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: same count as Task 12 (snippet content is not tested).

- [ ] **Step 4: Commit**

```bash
git add better_memory/skills/CLAUDE.snippet.md
git commit -m "Phase 8: CLAUDE snippet — reference Episodes tab as reconcile channel"
```

---

## Final review

After all tasks complete, dispatch a final code-review subagent across the full diff (commits from Task 1 onward). Confirm:

- All tests pass: `uv run pytest --tb=no -q 2>&1 | tail -3` shows `377 passed, 141 skipped, 4 deselected`.
- Spec §8 row-field set is complete (`goal` · `project` · `tech` · time range · `close_reason` · outcome badge · obs count · refl count).
- Spec §8 drawer set is complete (full episode details + observations + linked reflections + close actions for open).
- Banner appears when unclosed > 0 (Task 4 + Task 6).
- Old nav tabs hidden (Task 10) + root flips to `/episodes` (Task 5).
- No regressions in existing UI tests (the broken Pipeline/Sweep/Knowledge/Audit/Graph routes still 500 on direct hit, by design — Phase 10 strips them).

Then run `superpowers:finishing-a-development-branch` to push + open the PR or merge.
