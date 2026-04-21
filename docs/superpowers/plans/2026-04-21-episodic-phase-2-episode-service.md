# Episodic Memory Phase 2 — Episode Service + MCP Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `EpisodeService` that owns episode lifecycle (background-open, harden-foreground, supersede, close, reconcile), wire `ObservationService` to auto-bind `episode_id` and accept `tech`, and expose four new MCP tools (`memory.start_episode`, `memory.close_episode`, `memory.reconcile_episodes`, `memory.list_episodes`) plus `tech` on `memory.observe`. Unskip the service-layer tests that Phase 1 parked.

**Architecture:** New `better_memory/services/episode.py` module owns all episode-state transitions and reads against `episodes` + `episode_sessions`. `ObservationService.create` takes an optional `EpisodeService` dependency and uses it to resolve or lazily-open an active episode at write time (defaulting to a background episode when no foreground goal has been declared). The MCP server constructs one `EpisodeService` alongside `ObservationService` and opens a background episode at startup bound to the server's session_id. Reflection synthesis is **deferred to Phase 5** — `memory.start_episode` in this phase returns `{episode_id}` only, no `reflections` field.

**Tech Stack:** Python 3.12 · SQLite + sqlite-vec + FTS5 · MCP (`mcp.server.Server`) · pytest · uv.

**Scope boundary.** Service layer + MCP tool surface + startup wiring. **Out of scope** (deferred):
- **Hooks** (Phase 3 session-start, Phase 4 git post-commit / plan-complete). Phase 2's `memory.close_episode` is called manually by the LLM or the UI; there is no automatic close on commit.
- **Reflection synthesis** (Phase 5). `memory.start_episode` does **not** trigger synthesis. It returns the episode id only.
- **Legacy insight retrieval** — `memory.retrieve` today calls `insights.search()` against the dropped `insights` table and would crash. Task 7 removes that code path (returns empty `insights: []` array) until reflections replace it in Phase 6.
- **UI changes** (Phase 8+).

**Suggested split point.** Tasks 1-6 (service layer + ObservationService update) are independently shippable as "Phase 2a". Tasks 7-14 (MCP tools + server wiring) form "Phase 2b". If you want separate PRs, call out the split when executing; each half still leaves the branch green.

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §3 (episode lifecycle), §6 (MCP tool surface).

**Reference plan (Phase 1):** `docs/superpowers/plans/2026-04-20-episodic-phase-1-schema.md` — schema migration; Phase 2 builds on top of this.

---

## Task 0: Create worktree off the Phase 1 branch

Phase 2's branch needs Phase 1's schema, so it branches from `episodic-phase-1-schema` (already pushed), not `main`.

**Files:**
- Create: worktree at `C:/Users/gethi/source/better-memory-episodic-phase-2-service`

- [ ] **Step 1: Fetch and create worktree**

From the main checkout:

```bash
git fetch origin
git worktree add -b episodic-phase-2-service \
  ../better-memory-episodic-phase-2-service origin/episodic-phase-1-schema
```

- [ ] **Step 2: Verify worktree state**

```bash
cd ../better-memory-episodic-phase-2-service
git status
```

Expected: `On branch episodic-phase-2-service`, working tree clean.

- [ ] **Step 3: Verify baseline suite is green**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `187 passed, 172 skipped, 4 deselected` with zero failures.

---

## Task 1: `EpisodeService` skeleton — open_background + active_episode

Create the service module with the minimum shape to open a background episode (no goal) and look up the active episode for a session. This is the seed that Tasks 2-5 build on.

**Files:**
- Create: `better_memory/services/episode.py`
- Create: `tests/services/test_episode.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_episode.py`:

```python
"""Tests for EpisodeService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import Episode, EpisodeService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _fixed_clock():
    return datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)


class TestOpenBackground:
    def test_creates_background_episode_with_null_goal(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        row = conn.execute(
            "SELECT id, project, goal, hardened_at, started_at, ended_at "
            "FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row["project"] == "proj-a"
        assert row["goal"] is None
        assert row["hardened_at"] is None
        assert row["ended_at"] is None
        assert row["started_at"] == "2026-04-21T10:00:00+00:00"

    def test_creates_episode_sessions_row(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        row = conn.execute(
            "SELECT episode_id, session_id, joined_at, left_at "
            "FROM episode_sessions WHERE episode_id = ? AND session_id = ?",
            (episode_id, "sess-1"),
        ).fetchone()
        assert row is not None
        assert row["joined_at"] == "2026-04-21T10:00:00+00:00"
        assert row["left_at"] is None


class TestActiveEpisode:
    def test_returns_none_when_no_active_episode(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        assert svc.active_episode("sess-never") is None

    def test_returns_background_episode_after_open(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        active = svc.active_episode("sess-1")
        assert isinstance(active, Episode)
        assert active.id == episode_id
        assert active.goal is None

    def test_does_not_return_closed_episode(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        conn.execute(
            "UPDATE episodes SET ended_at = ? WHERE id = ?",
            ("2026-04-21T11:00:00+00:00", episode_id),
        )
        conn.execute(
            "UPDATE episode_sessions SET left_at = ? "
            "WHERE episode_id = ? AND session_id = ?",
            ("2026-04-21T11:00:00+00:00", episode_id, "sess-1"),
        )
        conn.commit()
        assert svc.active_episode("sess-1") is None

    def test_other_session_does_not_see_episode(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        assert svc.active_episode("sess-other") is None
```

- [ ] **Step 2: Run — all tests fail**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'better_memory.services.episode'`.

- [ ] **Step 3: Create the service module**

Write `better_memory/services/episode.py`:

```python
"""Episode lifecycle service.

Owns all writes to the ``episodes`` and ``episode_sessions`` tables.
Observations resolve their ``episode_id`` through this service at write
time; MCP tools ``memory.start_episode`` / ``memory.close_episode`` /
``memory.reconcile_episodes`` / ``memory.list_episodes`` wrap the same
API.

Reflection synthesis is NOT triggered here — that lives in Phase 5's
reflection service and is invoked from the MCP tool wrapper, not this
class. Phase 2 keeps the service pure-state.

Spec: §3 (lifecycle) + §4 (schema) of the episodic-memory design doc.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Episode:
    """Read model for an ``episodes`` row."""

    id: str
    project: str
    tech: str | None
    goal: str | None
    started_at: str
    hardened_at: str | None
    ended_at: str | None
    close_reason: str | None
    outcome: str | None
    summary: str | None


def _row_to_episode(row: sqlite3.Row) -> Episode:
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


class EpisodeService:
    """Manages episode open/harden/close transitions.

    Connection ownership: this service writes within its own transaction
    (SAVEPOINT + commit). Callers must not share a connection that has an
    open outer transaction with other services.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    def open_background(self, *, session_id: str, project: str) -> str:
        """Create a background episode (goal=NULL) for ``session_id``.

        Returns the new episode id. Also inserts the matching
        ``episode_sessions`` row with ``left_at = NULL``.
        """
        episode_id = uuid4().hex
        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT episode_open_background")
        try:
            conn.execute(
                "INSERT INTO episodes (id, project, started_at) "
                "VALUES (?, ?, ?)",
                (episode_id, project, now),
            )
            conn.execute(
                "INSERT INTO episode_sessions "
                "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
                (episode_id, session_id, now),
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_open_background")
            conn.execute("RELEASE SAVEPOINT episode_open_background")
            raise
        conn.execute("RELEASE SAVEPOINT episode_open_background")
        conn.commit()
        return episode_id

    def active_episode(self, session_id: str) -> Episode | None:
        """Return the open episode bound to ``session_id``, or None.

        "Open" means ``episodes.ended_at IS NULL`` AND there is a matching
        ``episode_sessions`` row with ``left_at IS NULL``. One-active-per-
        session is an invariant the lifecycle methods maintain.
        """
        row = self._conn.execute(
            """
            SELECT e.*
            FROM episodes e
            JOIN episode_sessions s ON s.episode_id = e.id
            WHERE s.session_id = ?
              AND s.left_at IS NULL
              AND e.ended_at IS NULL
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return _row_to_episode(row) if row is not None else None
```

- [ ] **Step 4: Run tests — all pass**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode.py
git commit -m "Phase 2: EpisodeService skeleton with open_background + active_episode"
```

---

## Task 2: `EpisodeService.start_foreground` — harden / supersede / open-new

Implements the spec's trigger table row: `memory.start_episode(goal, tech?)` — harden background, supersede any prior foreground, or open a new foreground episode from scratch.

**Files:**
- Modify: `better_memory/services/episode.py`
- Modify: `tests/services/test_episode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_episode.py`:

```python
class TestStartForeground:
    def test_hardens_existing_background_episode(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        background_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )

        foreground_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="implement phase 2",
            tech="python",
        )

        assert foreground_id == background_id  # hardened, not replaced
        row = conn.execute(
            "SELECT goal, tech, hardened_at, ended_at FROM episodes WHERE id = ?",
            (background_id,),
        ).fetchone()
        assert row["goal"] == "implement phase 2"
        assert row["tech"] == "python"
        assert row["hardened_at"] == "2026-04-21T10:00:00+00:00"
        assert row["ended_at"] is None

    def test_supersedes_prior_foreground_when_goal_differs(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        first = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="first goal",
            tech="python",
        )

        # New goal comes in while first is still active.
        second = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="second goal",
            tech="sqlite",
        )

        assert second != first
        first_row = conn.execute(
            "SELECT ended_at, close_reason, outcome FROM episodes WHERE id = ?",
            (first,),
        ).fetchone()
        assert first_row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert first_row["close_reason"] == "superseded"
        assert first_row["outcome"] == "no_outcome"

        second_row = conn.execute(
            "SELECT goal, tech, hardened_at FROM episodes WHERE id = ?",
            (second,),
        ).fetchone()
        assert second_row["goal"] == "second goal"
        assert second_row["tech"] == "sqlite"
        assert second_row["hardened_at"] == "2026-04-21T10:00:00+00:00"

    def test_opens_new_foreground_when_no_background_exists(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        # No prior open_background call.
        foreground_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="brand new work",
        )

        row = conn.execute(
            "SELECT goal, hardened_at, started_at FROM episodes WHERE id = ?",
            (foreground_id,),
        ).fetchone()
        assert row["goal"] == "brand new work"
        assert row["hardened_at"] == "2026-04-21T10:00:00+00:00"
        # For a net-new foreground, started_at == hardened_at.
        assert row["started_at"] == "2026-04-21T10:00:00+00:00"

        # And the session is bound.
        session_row = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (foreground_id, "sess-1"),
        ).fetchone()
        assert session_row["left_at"] is None

    def test_tech_is_lowercased_on_write(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        episode_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="lowercase me",
            tech="Python",
        )
        row = conn.execute(
            "SELECT tech FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        assert row["tech"] == "python"
```

- [ ] **Step 2: Run — four new tests fail**

```bash
uv run pytest tests/services/test_episode.py::TestStartForeground -v
```

Expected: FAIL with `AttributeError: 'EpisodeService' object has no attribute 'start_foreground'`.

- [ ] **Step 3: Add `start_foreground` to EpisodeService**

Append the method to `EpisodeService` in `better_memory/services/episode.py`:

```python
    def start_foreground(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        """Harden a background episode, or supersede prior foreground.

        Semantics (spec §3):
        - If an active background episode exists for this session
          (goal IS NULL), stamp goal/tech/hardened_at on it.
        - Else if an active foreground episode exists with a different
          goal, close it as ``close_reason='superseded'``,
          ``outcome='no_outcome'`` and open a new foreground.
        - Else open a new foreground from scratch (started_at = now).

        Returns the resulting episode id (the hardened/new one).
        """
        now = self._clock().isoformat()
        tech_normalised = tech.lower() if tech is not None else None

        conn = self._conn
        conn.execute("SAVEPOINT episode_start_foreground")
        try:
            active = self._active_episode_row(session_id)

            if active is not None and active["goal"] is None:
                # Harden the background episode.
                conn.execute(
                    "UPDATE episodes "
                    "SET goal = ?, tech = ?, hardened_at = ? "
                    "WHERE id = ?",
                    (goal, tech_normalised, now, active["id"]),
                )
                result_id = active["id"]
            else:
                # Supersede any prior active foreground (active with a goal),
                # then open a new foreground.
                if active is not None:
                    conn.execute(
                        "UPDATE episodes "
                        "SET ended_at = ?, close_reason = 'superseded', "
                        "    outcome = 'no_outcome' "
                        "WHERE id = ?",
                        (now, active["id"]),
                    )
                    conn.execute(
                        "UPDATE episode_sessions "
                        "SET left_at = ? "
                        "WHERE episode_id = ? AND session_id = ?",
                        (now, active["id"], session_id),
                    )

                result_id = uuid4().hex
                conn.execute(
                    "INSERT INTO episodes "
                    "(id, project, tech, goal, started_at, hardened_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (result_id, project, tech_normalised, goal, now, now),
                )
                conn.execute(
                    "INSERT INTO episode_sessions "
                    "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
                    (result_id, session_id, now),
                )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_start_foreground")
            conn.execute("RELEASE SAVEPOINT episode_start_foreground")
            raise
        conn.execute("RELEASE SAVEPOINT episode_start_foreground")
        conn.commit()
        return result_id

    def _active_episode_row(self, session_id: str) -> sqlite3.Row | None:
        """Internal helper: returns the raw active episode Row (not Episode)."""
        return self._conn.execute(
            """
            SELECT e.*
            FROM episodes e
            JOIN episode_sessions s ON s.episode_id = e.id
            WHERE s.session_id = ?
              AND s.left_at IS NULL
              AND e.ended_at IS NULL
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
```

- [ ] **Step 4: Run — all 10 tests pass**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode.py
git commit -m "Phase 2: EpisodeService.start_foreground (harden / supersede / open-new)"
```

---

## Task 3: `EpisodeService.close_active` — explicit close with outcome

Implements `memory.close_episode(outcome, summary?)` — closes the active episode for a session with a given outcome. Used for `abandoned` (LLM explicit), `success` (manual, before hook lands), `partial`, or `no_outcome`.

**Files:**
- Modify: `better_memory/services/episode.py`
- Modify: `tests/services/test_episode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_episode.py`:

```python
class TestCloseActive:
    def test_closes_foreground_with_success(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        fg = svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="ship it"
        )

        closed_id = svc.close_active(
            session_id="sess-1",
            outcome="success",
            close_reason="goal_complete",
        )

        assert closed_id == fg
        row = conn.execute(
            "SELECT ended_at, close_reason, outcome, summary "
            "FROM episodes WHERE id = ?",
            (fg,),
        ).fetchone()
        assert row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert row["close_reason"] == "goal_complete"
        assert row["outcome"] == "success"
        assert row["summary"] is None

    def test_abandoned_close_records_summary(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="rejected work"
        )

        svc.close_active(
            session_id="sess-1",
            outcome="abandoned",
            close_reason="abandoned",
            summary="user asked me to stop and change direction",
        )

        row = conn.execute(
            "SELECT outcome, summary FROM episodes WHERE ended_at IS NOT NULL"
        ).fetchone()
        assert row["outcome"] == "abandoned"
        assert row["summary"] == "user asked me to stop and change direction"

    def test_marks_episode_session_left_at(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        fg = svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="x"
        )

        svc.close_active(
            session_id="sess-1",
            outcome="success",
            close_reason="goal_complete",
        )

        row = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (fg, "sess-1"),
        ).fetchone()
        assert row["left_at"] == "2026-04-21T10:00:00+00:00"

    def test_raises_when_no_active_episode(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        with pytest.raises(ValueError, match="No active episode"):
            svc.close_active(
                session_id="sess-nobody",
                outcome="success",
                close_reason="goal_complete",
            )

    def test_closes_background_episode_too(self, conn):
        """Closing a background (unhardened) episode is valid (e.g. session_end_reconciled with no_outcome)."""
        svc = EpisodeService(conn, clock=_fixed_clock)
        bg = svc.open_background(session_id="sess-1", project="proj-a")

        closed_id = svc.close_active(
            session_id="sess-1",
            outcome="no_outcome",
            close_reason="session_end_reconciled",
        )

        assert closed_id == bg
        row = conn.execute(
            "SELECT goal, hardened_at, ended_at, outcome FROM episodes WHERE id = ?",
            (bg,),
        ).fetchone()
        assert row["goal"] is None
        assert row["hardened_at"] is None
        assert row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert row["outcome"] == "no_outcome"
```

- [ ] **Step 2: Run — 5 new tests fail**

```bash
uv run pytest tests/services/test_episode.py::TestCloseActive -v
```

Expected: FAIL with `AttributeError: ... 'close_active'`.

- [ ] **Step 3: Add `close_active` to EpisodeService**

Append to `EpisodeService`:

```python
    def close_active(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close the currently-active episode bound to ``session_id``.

        Raises ``ValueError`` if no active episode exists. Works on both
        background and foreground episodes (reconciliation may close a
        background that never hardened).
        """
        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT episode_close_active")
        try:
            active = self._active_episode_row(session_id)
            if active is None:
                raise ValueError(
                    f"No active episode for session_id={session_id!r}"
                )
            conn.execute(
                "UPDATE episodes "
                "SET ended_at = ?, close_reason = ?, outcome = ?, summary = ? "
                "WHERE id = ?",
                (now, close_reason, outcome, summary, active["id"]),
            )
            conn.execute(
                "UPDATE episode_sessions "
                "SET left_at = ? "
                "WHERE episode_id = ? AND session_id = ? AND left_at IS NULL",
                (now, active["id"], session_id),
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_close_active")
            conn.execute("RELEASE SAVEPOINT episode_close_active")
            raise
        conn.execute("RELEASE SAVEPOINT episode_close_active")
        conn.commit()
        return active["id"]
```

- [ ] **Step 4: Run — all tests pass**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode.py
git commit -m "Phase 2: EpisodeService.close_active"
```

---

## Task 4: `EpisodeService.unclosed_episodes` — reconciliation lookup

Implements `memory.reconcile_episodes()` — returns open episodes from prior sessions that need the user's answer.

**Files:**
- Modify: `better_memory/services/episode.py`
- Modify: `tests/services/test_episode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_episode.py`:

```python
class TestUnclosedEpisodes:
    def test_empty_when_no_episodes(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        assert svc.unclosed_episodes() == []

    def test_returns_open_episodes_across_sessions(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-a", project="p")
        svc.start_foreground(
            session_id="sess-b", project="p", goal="pending"
        )

        result = svc.unclosed_episodes()
        assert len(result) == 2

    def test_excludes_specified_sessions(self, conn):
        """Current session's episode is filtered so the LLM doesn't prompt itself."""
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="sess-old", project="p")
        svc.open_background(session_id="sess-current", project="p")

        result = svc.unclosed_episodes(exclude_session_ids={"sess-current"})
        assert len(result) == 1
        # Only the old one should remain.
        assert result[0].project == "p"

    def test_excludes_closed_episodes(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.start_foreground(
            session_id="sess-a", project="p", goal="done"
        )
        svc.close_active(
            session_id="sess-a",
            outcome="success",
            close_reason="goal_complete",
        )

        assert svc.unclosed_episodes() == []
```

- [ ] **Step 2: Run — four new tests fail**

```bash
uv run pytest tests/services/test_episode.py::TestUnclosedEpisodes -v
```

Expected: FAIL.

- [ ] **Step 3: Add `unclosed_episodes` to EpisodeService**

```python
    def unclosed_episodes(
        self,
        *,
        exclude_session_ids: set[str] | None = None,
    ) -> list[Episode]:
        """Return all episodes with ``ended_at IS NULL``.

        ``exclude_session_ids`` drops any episode that has an open binding
        to one of those sessions (typically the caller's current session).
        """
        exclude = exclude_session_ids or set()
        rows = self._conn.execute(
            """
            SELECT DISTINCT e.*
            FROM episodes e
            JOIN episode_sessions s ON s.episode_id = e.id
            WHERE e.ended_at IS NULL
            ORDER BY e.started_at ASC, e.id ASC
            """
        ).fetchall()

        if not exclude:
            return [_row_to_episode(r) for r in rows]

        # Filter out episodes that have an active binding to any excluded session.
        out: list[Episode] = []
        for r in rows:
            active_sessions = self._conn.execute(
                "SELECT session_id FROM episode_sessions "
                "WHERE episode_id = ? AND left_at IS NULL",
                (r["id"],),
            ).fetchall()
            active_set = {row["session_id"] for row in active_sessions}
            if active_set & exclude:
                continue
            out.append(_row_to_episode(r))
        return out
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode.py
git commit -m "Phase 2: EpisodeService.unclosed_episodes for reconciliation"
```

---

## Task 5: `EpisodeService.list_episodes` — UI/tooling lookup

Implements `memory.list_episodes(project?, outcome?, only_open?)`.

**Files:**
- Modify: `better_memory/services/episode.py`
- Modify: `tests/services/test_episode.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_episode.py`:

```python
class TestListEpisodes:
    def test_empty_when_nothing(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        assert svc.list_episodes() == []

    def test_filter_by_project(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="s1", project="proj-a")
        svc.open_background(session_id="s2", project="proj-b")

        result = svc.list_episodes(project="proj-a")
        assert len(result) == 1
        assert result[0].project == "proj-a"

    def test_filter_by_outcome(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.start_foreground(
            session_id="s1", project="p", goal="won"
        )
        svc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        svc.start_foreground(
            session_id="s2", project="p", goal="lost"
        )
        svc.close_active(
            session_id="s2", outcome="abandoned", close_reason="abandoned"
        )

        result = svc.list_episodes(outcome="success")
        assert len(result) == 1
        assert result[0].goal == "won"

    def test_only_open(self, conn):
        svc = EpisodeService(conn, clock=_fixed_clock)
        svc.open_background(session_id="s-open", project="p")
        svc.start_foreground(
            session_id="s-closed", project="p", goal="done"
        )
        svc.close_active(
            session_id="s-closed",
            outcome="success",
            close_reason="goal_complete",
        )

        result = svc.list_episodes(only_open=True)
        assert len(result) == 1
        # The still-open background has ended_at IS NULL.
        assert result[0].ended_at is None

    def test_orders_newest_first(self, conn):
        svc = EpisodeService(
            conn,
            clock=lambda: datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC),
        )
        first = svc.open_background(session_id="s1", project="p")

        svc_later = EpisodeService(
            conn,
            clock=lambda: datetime(2026, 4, 21, 11, 0, 0, tzinfo=UTC),
        )
        second = svc_later.open_background(session_id="s2", project="p")

        result = svc.list_episodes()
        assert [e.id for e in result] == [second, first]
```

- [ ] **Step 2: Run — 5 new tests fail**

```bash
uv run pytest tests/services/test_episode.py::TestListEpisodes -v
```

Expected: FAIL.

- [ ] **Step 3: Add `list_episodes` to EpisodeService**

```python
    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Episode]:
        """Return episodes matching the filters, newest-first.

        Args:
            project: filter by project, None = all projects.
            outcome: filter by outcome; None = no filter.
            only_open: if True, only ``ended_at IS NULL`` episodes.
        """
        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        if only_open:
            clauses.append("ended_at IS NULL")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM episodes {where} "
            f"ORDER BY started_at DESC, rowid DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_episode(r) for r in rows]
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/services/test_episode.py -v
```

Expected: 24 passed.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/episode.py tests/services/test_episode.py
git commit -m "Phase 2: EpisodeService.list_episodes with filters"
```

---

## Task 6: `ObservationService.create` auto-binds `episode_id` and accepts `tech`

Wire the observation write-path to the `EpisodeService`. Tests in `tests/services/test_observation.py`, `test_observation_retrieve.py`, and `test_audit_trail.py` are all currently skipped awaiting this. Un-skip them and update fixture shapes where needed.

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `tests/services/test_observation.py`
- Modify: `tests/services/test_observation_retrieve.py`
- Modify: `tests/services/test_audit_trail.py`

- [ ] **Step 1: Write the new failing tests for auto-episode binding + tech**

Append to `tests/services/test_episode.py` (a cross-service test lives here because it exercises the service contract, not the ObservationService internals):

```python
class TestObservationServiceEpisodeIntegration:
    """The observation write path must produce a valid episode_id.

    These are integration-level tests against ObservationService + EpisodeService;
    the pure-ObservationService unit tests live in tests/services/test_observation.py.
    """

    async def test_observation_write_opens_background_episode_lazily(self, conn):
        from better_memory.services.observation import ObservationService

        class _StubEmbedder:
            async def embed(self, text):
                return [0.0] * 768

        epsvc = EpisodeService(conn, clock=_fixed_clock)
        obs_svc = ObservationService(
            conn,
            _StubEmbedder(),
            clock=_fixed_clock,
            project_resolver=lambda: "proj-a",
            session_id="sess-1",
            episodes=epsvc,
        )

        obs_id = await obs_svc.create(content="first observation")

        row = conn.execute(
            "SELECT episode_id, tech FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["episode_id"] is not None
        assert row["tech"] is None

        # Subsequent writes reuse the same background episode.
        obs_id2 = await obs_svc.create(content="second")
        row2 = conn.execute(
            "SELECT episode_id FROM observations WHERE id = ?", (obs_id2,)
        ).fetchone()
        assert row2["episode_id"] == row["episode_id"]

    async def test_observation_accepts_tech_parameter(self, conn):
        from better_memory.services.observation import ObservationService

        class _StubEmbedder:
            async def embed(self, text):
                return [0.0] * 768

        epsvc = EpisodeService(conn, clock=_fixed_clock)
        obs_svc = ObservationService(
            conn,
            _StubEmbedder(),
            clock=_fixed_clock,
            project_resolver=lambda: "proj-a",
            session_id="sess-1",
            episodes=epsvc,
        )

        obs_id = await obs_svc.create(content="x", tech="Python")
        row = conn.execute(
            "SELECT tech FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        # tech is lowercased by the service.
        assert row["tech"] == "python"
```

- [ ] **Step 2: Run — two new tests fail**

```bash
uv run pytest tests/services/test_episode.py::TestObservationServiceEpisodeIntegration -v
```

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'episodes'`.

- [ ] **Step 3: Update `ObservationService` to accept `episodes` dependency and `tech` param**

Edit `better_memory/services/observation.py`:

Update the imports at the top:

```python
from better_memory.services.episode import EpisodeService
```

Update the `__init__` signature to add the `episodes` parameter:

```python
    def __init__(
        self,
        conn: sqlite3.Connection,
        embedder: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        project_resolver: Callable[[], str] | None = None,
        scope_resolver: Callable[[], str | None] | None = None,
        session_id: str | None = None,
        audit_log_retrieved: bool | None = None,
        episodes: EpisodeService | None = None,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._clock: Callable[[], datetime] = clock or _default_clock
        self._project_resolver: Callable[[], str] = (
            project_resolver if project_resolver is not None else (lambda: Path.cwd().name)
        )
        self._scope_resolver: Callable[[], str | None] = (
            scope_resolver if scope_resolver is not None else (lambda: None)
        )
        self._session_id = session_id if session_id is not None else uuid4().hex
        self._audit_log_retrieved: bool = (
            audit_log_retrieved
            if audit_log_retrieved is not None
            else get_config().audit_log_retrieved
        )
        self._episodes = episodes
```

Update the `create` signature and body to accept `tech` and stamp `episode_id`:

```python
    async def create(
        self,
        content: str,
        *,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
    ) -> str:
        """Insert a new observation, embedding and audit row; return its id."""
        obs_id = uuid4().hex

        resolved_project = project if project is not None else self._project_resolver()
        resolved_scope = scope_path if scope_path is not None else self._scope_resolver()
        tech_normalised = tech.lower() if tech is not None else None

        # Resolve episode_id. ObservationService requires an EpisodeService
        # now that episode_id is NOT NULL on observations (Phase 1 schema).
        if self._episodes is None:
            raise RuntimeError(
                "ObservationService.create requires an EpisodeService "
                "(episodes=...). Wire one at construction time."
            )
        active = self._episodes.active_episode(self._session_id)
        if active is None:
            episode_id = self._episodes.open_background(
                session_id=self._session_id,
                project=resolved_project,
            )
        else:
            episode_id = active.id

        # Fail fast: compute the embedding BEFORE opening a write transaction.
        vector = await self._embedder.embed(content)
        vec_blob = sqlite_vec.serialize_float32(vector)

        now = self._clock().isoformat()

        conn = self._conn
        conn.execute("SAVEPOINT observation_create")
        try:
            conn.execute(
                """
                INSERT INTO observations (
                    id, content, project, component, theme, session_id,
                    trigger_type, outcome, reinforcement_score, scope_path,
                    created_at, episode_id, tech
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?)
                """,
                (
                    obs_id,
                    content,
                    resolved_project,
                    component,
                    theme,
                    self._session_id,
                    trigger_type,
                    outcome,
                    resolved_scope,
                    now,
                    episode_id,
                    tech_normalised,
                ),
            )

            conn.execute(
                "INSERT INTO observation_embeddings (observation_id, embedding) "
                "VALUES (?, ?)",
                (obs_id, vec_blob),
            )

            self._write_audit(
                entity_id=obs_id,
                action="created",
                detail={
                    "outcome": outcome,
                    "scope_path": resolved_scope,
                    "component": component,
                    "episode_id": episode_id,
                    "tech": tech_normalised,
                },
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT observation_create")
            conn.execute("RELEASE SAVEPOINT observation_create")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT observation_create")

        conn.commit()
        return obs_id
```

- [ ] **Step 4: Run the new Episode integration tests — pass**

```bash
uv run pytest tests/services/test_episode.py::TestObservationServiceEpisodeIntegration -v
```

Expected: 2 passed.

- [ ] **Step 5: Un-skip the observation service test files**

For each of these three files, remove the module-level skip marker. The existing `pytestmark = pytest.mark.skip(...)` block must be deleted; the `import pytest` above it should be left in place (it's used for `pytest.raises` in the tests).

- `tests/services/test_observation.py`
- `tests/services/test_observation_retrieve.py`
- `tests/services/test_audit_trail.py`

- [ ] **Step 6: Update the un-skipped tests to construct `ObservationService` with an `EpisodeService`**

The un-skipped tests construct `ObservationService` in many places; each construction site needs an `episodes=EpisodeService(conn)` kwarg. Run the tests to see where the constructions are:

```bash
uv run pytest tests/services/test_observation.py tests/services/test_observation_retrieve.py tests/services/test_audit_trail.py -v 2>&1 | head -40
```

For every `ObservationService(` construction site in those three files, add `episodes=EpisodeService(conn)` (importing `EpisodeService` at the top of the file as needed). If helper fixtures build the service, update the fixture.

Run tests iteratively until green:

```bash
uv run pytest tests/services/test_observation.py tests/services/test_observation_retrieve.py tests/services/test_audit_trail.py -v
```

Expected: all tests PASS after construction-site updates.

- [ ] **Step 7: Run the full suite — confirm no regressions**

```bash
uv run pytest --tb=short -q 2>&1 | tail -5
```

Expected: passed count rises by ~35 from the baseline (the three un-skipped files), skipped count drops by the same. Zero failures.

- [ ] **Step 8: Commit**

```bash
git add better_memory/services/observation.py tests/services/
git commit -m "Phase 2: ObservationService auto-binds episode_id and accepts tech"
```

---

**Split point.** Tasks 1-6 above deliver the service layer. Observations can be written again, episodes lifecycle is testable, and ~35 Phase 1-skipped tests are back online. If you want to ship this as "Phase 2a" and start a fresh plan for the MCP surface, this is the clean cut. Otherwise continue into Task 7.

---

## Task 7: Neutralise `memory.retrieve`'s insight path

The MCP tool `memory.retrieve` currently calls `insights.search(query, limit=5)`. The `insights` table is gone — a live retrieve would crash. Neutralise this path by returning an empty list for `insights`. Phase 6 will replace it with reflection retrieval.

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_server_integration.py` (selective un-skip)

- [ ] **Step 1: Read and locate the `memory.retrieve` handler**

In `better_memory/mcp/server.py`, find the block starting `if name == "memory.retrieve":` (around line 363). The handler computes `insight_hits = [...]` via `insights.search(...)`. That call will raise `OperationalError: no such table: insights` at runtime.

- [ ] **Step 2: Replace the insight search with an empty list + reminder comment**

Replace the insight-search block (around lines 384-390) with:

```python
            # Insights table was dropped in Phase 1. Reflection retrieval
            # replaces this path in Phase 6; for now, return [] so clients
            # continue to receive the payload shape they expect.
            insight_hits: list[dict[str, Any]] = []
            knowledge_hits: list[dict[str, Any]] = []
            if query:
                knowledge_hits = [
                    _serialize_knowledge_search(r)
                    for r in knowledge.search(query, limit=5)
                ]
```

Also remove the `insights` service construction at the factory level (`insights = InsightService(memory_conn, embedder=embedder)`) and the `from better_memory.services.insight import ...` imports at the top — they were used only by this handler. Leave the `InsightSearchResult` import out too.

After the edit, the top of `server.py` should no longer import from `better_memory.services.insight`.

Build the updated file and run the parse test:

```bash
uv run python -c "import better_memory.mcp.server; print('import ok')"
```

Expected: `import ok`.

- [ ] **Step 3: Run the `test_parse_window` file (the one non-skipped file in tests/mcp/)**

```bash
uv run pytest tests/mcp/ -v
```

Expected: `test_parse_window.py` still passes; `test_server_integration.py` still skipped.

- [ ] **Step 4: Commit**

```bash
git add better_memory/mcp/server.py
git commit -m "Phase 2: remove InsightService usage from memory.retrieve (Phase 6 reintroduces)"
```

---

## Task 8: `memory.observe` MCP tool accepts `tech`

Add the `tech` field to the `memory.observe` tool schema and pass it through to `ObservationService.create`. Wire the `EpisodeService` construction into the factory.

**Files:**
- Modify: `better_memory/mcp/server.py`

- [ ] **Step 1: Add `tech` to the `memory.observe` input schema**

In `_tool_definitions()` (around line 107), update the `memory.observe` tool's `inputSchema.properties` to add `tech`:

```python
        Tool(
            name="memory.observe",
            description=(
                "Record an observation about the current session (a fact, "
                "decision, bug fix, or outcome). Returns the new observation id."
            ),
            inputSchema={
                "type": "object",
                "required": ["content"],
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string"},
                    "component": {"type": "string"},
                    "theme": {"type": "string"},
                    "trigger_type": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "failure", "neutral"],
                    },
                    "tech": {"type": "string"},
                },
            },
        ),
```

- [ ] **Step 2: Wire `EpisodeService` into the factory**

At the top of `server.py`, add the import:

```python
from better_memory.services.episode import EpisodeService
```

In `create_server()`, after the `memory_conn` / `knowledge_conn` are created and migrated, before `observations = ObservationService(...)`:

```python
    episodes = EpisodeService(memory_conn)
    observations = ObservationService(memory_conn, embedder, episodes=episodes)
```

- [ ] **Step 3: Pass `tech` through the `memory.observe` handler**

Update the handler (around line 353):

```python
        if name == "memory.observe":
            obs_id = await observations.create(
                content=args["content"],
                component=args.get("component"),
                theme=args.get("theme"),
                trigger_type=args.get("trigger_type"),
                outcome=args.get("outcome", "neutral"),
                tech=args.get("tech"),
            )
            return [TextContent(type="text", text=json.dumps({"id": obs_id}))]
```

- [ ] **Step 4: Verify import and run what tests we have**

```bash
uv run python -c "import better_memory.mcp.server; print('import ok')"
uv run pytest tests/mcp/test_parse_window.py -v
```

Expected: `import ok` + parse window tests pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/mcp/server.py
git commit -m "Phase 2: memory.observe MCP tool accepts tech + wires EpisodeService"
```

---

## Task 9: `memory.start_episode` MCP tool

Adds the new tool. No reflection synthesis — returns `{episode_id}` only (Phase 5 will extend).

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing MCP integration test**

Create `tests/mcp/test_episode_tools.py`:

```python
"""Integration tests for the episode MCP tools.

These call the handler function directly (without stdio transport) to keep
the tests fast and deterministic. They exercise the factory wiring and the
tool payload shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_memory.mcp.server import create_server


@pytest.fixture
def server_factory(tmp_path, monkeypatch):
    """Yield a factory that builds a fresh server + cleanup.

    Sets BETTER_MEMORY_HOME to tmp_path so each test has an isolated DB
    and knowledge base.
    """
    home = tmp_path / "bm"
    home.mkdir()
    (home / "knowledge-base").mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

    # Clear the cached config so the new env var takes effect.
    from better_memory import config

    config._cached_config = None
    yield create_server


class TestStartEpisodeTool:
    @pytest.mark.asyncio
    async def test_returns_episode_id(self, server_factory):
        server, cleanup = server_factory()
        try:
            # Access the call_tool handler via the server's registered handlers.
            # mcp.server.Server stores handlers on the request type decorators.
            # The simplest path is to import the module-level _call_tool function
            # — but we registered it as a closure, so we go via the server's
            # request_handlers mapping, which stores the async fn under the
            # `CallToolRequest` key. Tests use the same helper hook as production.
            from mcp.server.lowlevel.server import Server as LowLevelServer  # noqa: F401

            # Find the call_tool handler by scanning request handlers.
            handler = None
            for key, fn in server.request_handlers.items():
                if "CallTool" in key.__name__:
                    handler = fn
                    break
            assert handler is not None, "call_tool handler not found"

            # Build a CallToolRequest manually.
            from mcp.types import CallToolRequest, CallToolRequestParams

            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="memory.start_episode",
                    arguments={
                        "goal": "test goal",
                        "tech": "python",
                    },
                ),
            )
            response = await handler(req)
            content = response.root.content[0]
            payload = json.loads(content.text)
            assert "episode_id" in payload
            assert isinstance(payload["episode_id"], str)
            assert len(payload["episode_id"]) > 0
        finally:
            await cleanup()
```

If the above introspection is too fragile, simplify by testing the handler at the service level instead — construct `EpisodeService` directly and call `start_foreground` to assert the return shape is as the MCP tool would serialise. Delete the MCP-level handler-call complexity if needed; the direct service tests from Task 2 already cover the behaviour.

Fallback test (replaces the body of `test_returns_episode_id` if the handler introspection fails):

```python
    def test_start_episode_integration_via_direct_service(self, tmp_path):
        """End-to-end: the factory wires EpisodeService correctly."""
        import sqlite3
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService

        db = tmp_path / "memory.db"
        conn = connect(db)
        apply_migrations(conn)
        try:
            svc = EpisodeService(conn)
            eid = svc.start_foreground(
                session_id="sess-1",
                project="proj",
                goal="test",
            )
            row = conn.execute(
                "SELECT goal FROM episodes WHERE id = ?", (eid,)
            ).fetchone()
            assert row["goal"] == "test"
        finally:
            conn.close()
```

Pick whichever style works first-try; document the choice in the commit message.

- [ ] **Step 2: Run the new test — it should fail**

```bash
uv run pytest tests/mcp/test_episode_tools.py -v
```

Expected: FAIL — tool not registered.

- [ ] **Step 3: Register `memory.start_episode` in `_tool_definitions()`**

Append to the tool list:

```python
        Tool(
            name="memory.start_episode",
            description=(
                "Declare a goal for the current session. Opens a new "
                "foreground episode or hardens the existing background "
                "episode. Returns the active episode id."
            ),
            inputSchema={
                "type": "object",
                "required": ["goal"],
                "additionalProperties": False,
                "properties": {
                    "goal": {"type": "string"},
                    "tech": {"type": "string"},
                },
            },
        ),
```

- [ ] **Step 4: Add the handler branch in `_call_tool`**

In `create_server()`, the factory already constructs `episodes` (from Task 8). Add this before the `raise ValueError(f"Unknown tool: {name}")` line:

```python
        if name == "memory.start_episode":
            # Phase 2 scope: open/harden foreground episode only — reflection
            # synthesis is Phase 5. Session id is resolved from the
            # ObservationService's session (same id the observation path uses).
            episode_id = episodes.start_foreground(
                session_id=observations._session_id,
                project=Path.cwd().name,
                goal=args["goal"],
                tech=args.get("tech"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"episode_id": episode_id}),
                )
            ]
```

- [ ] **Step 5: Run tests — pass**

```bash
uv run pytest tests/mcp/test_episode_tools.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py
git commit -m "Phase 2: memory.start_episode MCP tool (no synthesis; Phase 5 adds it)"
```

---

## Task 10: `memory.close_episode` MCP tool

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_episode_tools.py`:

```python
class TestCloseEpisodeTool:
    def test_close_via_service(self, tmp_path):
        """The MCP tool is a thin wrapper; verify the service call shape."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService

        db = tmp_path / "memory.db"
        conn = connect(db)
        apply_migrations(conn)
        try:
            svc = EpisodeService(conn)
            svc.start_foreground(
                session_id="sess-1", project="p", goal="g"
            )
            closed_id = svc.close_active(
                session_id="sess-1",
                outcome="abandoned",
                close_reason="abandoned",
                summary="stopped by user",
            )
            row = conn.execute(
                "SELECT outcome, summary FROM episodes WHERE id = ?",
                (closed_id,),
            ).fetchone()
            assert row["outcome"] == "abandoned"
            assert row["summary"] == "stopped by user"
        finally:
            conn.close()
```

(Lightweight: exercises the service layer through the same code path the MCP tool will use, without the MCP handler introspection overhead.)

- [ ] **Step 2: Register the tool in `_tool_definitions()`**

```python
        Tool(
            name="memory.close_episode",
            description=(
                "Close the current session's active episode. outcome is one "
                "of success / partial / abandoned / no_outcome."
            ),
            inputSchema={
                "type": "object",
                "required": ["outcome"],
                "additionalProperties": False,
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "success",
                            "partial",
                            "abandoned",
                            "no_outcome",
                        ],
                    },
                    "close_reason": {
                        "type": "string",
                        "enum": [
                            "goal_complete",
                            "plan_complete",
                            "abandoned",
                            "superseded",
                            "session_end_reconciled",
                        ],
                    },
                    "summary": {"type": "string"},
                },
            },
        ),
```

- [ ] **Step 3: Add the handler branch**

```python
        if name == "memory.close_episode":
            outcome = args["outcome"]
            # Default close_reason: match outcome for the common paths.
            default_reasons = {
                "success": "goal_complete",
                "partial": "superseded",
                "abandoned": "abandoned",
                "no_outcome": "session_end_reconciled",
            }
            close_reason = args.get("close_reason") or default_reasons[outcome]
            closed_id = episodes.close_active(
                session_id=observations._session_id,
                outcome=outcome,
                close_reason=close_reason,
                summary=args.get("summary"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"closed_episode_id": closed_id}),
                )
            ]
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/mcp/test_episode_tools.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py
git commit -m "Phase 2: memory.close_episode MCP tool"
```

---

## Task 11: `memory.reconcile_episodes` MCP tool

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_episode_tools.py`:

```python
class TestReconcileEpisodesTool:
    def test_returns_unclosed_from_other_sessions(self, tmp_path):
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService

        db = tmp_path / "memory.db"
        conn = connect(db)
        apply_migrations(conn)
        try:
            svc = EpisodeService(conn)
            svc.open_background(session_id="sess-prior", project="p")
            svc.open_background(session_id="sess-current", project="p")

            unclosed = svc.unclosed_episodes(
                exclude_session_ids={"sess-current"}
            )
            assert len(unclosed) == 1
            assert unclosed[0].project == "p"
        finally:
            conn.close()
```

- [ ] **Step 2: Register the tool**

```python
        Tool(
            name="memory.reconcile_episodes",
            description=(
                "List episodes that are still open from prior sessions, "
                "for the LLM to prompt the user about."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
```

- [ ] **Step 3: Add the handler branch**

```python
        if name == "memory.reconcile_episodes":
            open_episodes = episodes.unclosed_episodes(
                exclude_session_ids={observations._session_id}
            )
            payload = [
                {
                    "episode_id": e.id,
                    "project": e.project,
                    "tech": e.tech,
                    "goal": e.goal,
                    "started_at": e.started_at,
                }
                for e in open_episodes
            ]
            return [TextContent(type="text", text=json.dumps(payload))]
```

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest tests/mcp/test_episode_tools.py -v
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py
git commit -m "Phase 2: memory.reconcile_episodes MCP tool"
```

---

## Task 12: `memory.list_episodes` MCP tool

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_episode_tools.py`:

```python
class TestListEpisodesTool:
    def test_filters_work_via_service(self, tmp_path):
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService

        db = tmp_path / "memory.db"
        conn = connect(db)
        apply_migrations(conn)
        try:
            svc = EpisodeService(conn)
            svc.open_background(session_id="s1", project="proj-a")
            svc.open_background(session_id="s2", project="proj-b")

            result = svc.list_episodes(project="proj-a")
            assert len(result) == 1
            assert result[0].project == "proj-a"
        finally:
            conn.close()
```

- [ ] **Step 2: Register the tool**

```python
        Tool(
            name="memory.list_episodes",
            description=(
                "List episodes with optional filters. For UI and LLM "
                "introspection."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "success",
                            "partial",
                            "abandoned",
                            "no_outcome",
                        ],
                    },
                    "only_open": {"type": "boolean"},
                },
            },
        ),
```

- [ ] **Step 3: Add the handler branch**

```python
        if name == "memory.list_episodes":
            rows = episodes.list_episodes(
                project=args.get("project"),
                outcome=args.get("outcome"),
                only_open=args.get("only_open", False),
            )
            payload = [
                {
                    "episode_id": e.id,
                    "project": e.project,
                    "tech": e.tech,
                    "goal": e.goal,
                    "started_at": e.started_at,
                    "hardened_at": e.hardened_at,
                    "ended_at": e.ended_at,
                    "close_reason": e.close_reason,
                    "outcome": e.outcome,
                    "summary": e.summary,
                }
                for e in rows
            ]
            return [TextContent(type="text", text=json.dumps(payload))]
```

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest tests/mcp/test_episode_tools.py -v
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py
git commit -m "Phase 2: memory.list_episodes MCP tool"
```

---

## Task 13: MCP server startup opens a background episode

On MCP server boot, the session's background episode should already exist so the first `memory.observe` doesn't have to trigger lazy-open (lazy-open still works as a fallback, but pre-opening makes the episode discoverable to `list_episodes` immediately).

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_episode_tools.py`:

```python
class TestServerStartupBackgroundEpisode:
    async def test_background_episode_opens_on_create_server(
        self, tmp_path, monkeypatch
    ):
        """Constructing the MCP server opens a background episode."""
        home = tmp_path / "bm"
        home.mkdir()
        (home / "knowledge-base").mkdir()
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

        from better_memory import config

        config._cached_config = None

        from better_memory.mcp.server import create_server

        server, cleanup = create_server()
        try:
            from better_memory.db.connection import connect

            resolved = config.get_config()
            conn = connect(resolved.memory_db)
            try:
                rows = conn.execute(
                    "SELECT id, goal, ended_at FROM episodes"
                ).fetchall()
                assert len(rows) == 1
                assert rows[0]["goal"] is None
                assert rows[0]["ended_at"] is None
            finally:
                conn.close()
        finally:
            await cleanup()
```

- [ ] **Step 2: Add the startup call in `create_server()`**

In `create_server()`, after the `episodes = EpisodeService(memory_conn)` line and the `observations = ObservationService(...)` line, before the `knowledge = KnowledgeService(...)` block:

```python
    # Session-start behaviour: open a background episode for this server's
    # session so observations written before the LLM declares a goal still
    # bind to an episode. Phase 3's session-start hook will eventually
    # trigger this externally; Phase 2 does it at factory time.
    try:
        episodes.open_background(
            session_id=observations._session_id,
            project=Path.cwd().name,
        )
    except Exception:  # noqa: BLE001 — best-effort startup hook
        # Don't block server startup; lazy-open in ObservationService.create
        # catches the gap.
        pass
```

- [ ] **Step 3: Run the test**

```bash
uv run pytest tests/mcp/test_episode_tools.py::TestServerStartupBackgroundEpisode -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py
git commit -m "Phase 2: MCP server opens background episode on startup"
```

---

## Task 14: Final verification and push

**Files:**
- No changes — verification only.

- [ ] **Step 1: Confirm the full suite is green**

```bash
uv run pytest --tb=short -q 2>&1 | tail -5
```

Expected: passed count up ~35-40 from Phase 1 baseline (new service + un-skipped tests). Zero failures.

- [ ] **Step 2: Confirm the unit tests for the new service are exhaustive**

```bash
uv run pytest tests/services/test_episode.py tests/mcp/test_episode_tools.py -v
```

Expected: all pass.

- [ ] **Step 3: Confirm working tree is clean**

```bash
git status
```

Expected: `nothing to commit, working tree clean`.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin episodic-phase-2-service
```

---

## Self-review checklist

After writing: verify each spec §3 and §6 requirement is covered by a task.

- §3 triggers table:
  - "Session-start hook → open background episode" → Task 13 approximates this at server startup (hook is Phase 3).
  - "`memory.start_episode(goal, tech?)`" → Tasks 2 + 9 (service + MCP).
  - "Git post-commit hook closes active" → Phase 4 (out of scope).
  - "Plan-complete signal closes active" → Phase 4 (out of scope).
  - "`memory.close_episode(outcome, summary?)`" → Tasks 3 + 10.
  - "Session-end" → no action (matches spec).
  - "Next session-start, reconcile prompt" → Tasks 4 + 11 provide the data; Phase 3 provides the prompt mechanism.
- §3 continue-across-sessions → service supports (via `open_background` / `active_episode` on arbitrary session_id); client wiring lands in Phase 3.
- §3 outcome semantics → CHECK constraints in Phase 1 + service enforcement in Tasks 3 + 10.
- §6 tool surface → `memory.observe` (Task 8), `memory.start_episode` (Task 9), `memory.close_episode` (Task 10), `memory.reconcile_episodes` (Task 11), `memory.list_episodes` (Task 12), `memory.record_use` (unchanged), `memory.start_ui` (unchanged).
- Reflection synthesis deferred to Phase 5 — Task 9 explicitly calls this out.
- `memory.retrieve` reflection path deferred to Phase 6 — Task 7 documents the neutralisation.

Gaps for Phase 3+:
- No session-start hook wiring (external process calling `memory.start_session` or similar).
- No git post-commit hook script.
- No plan-complete integration with `superpowers:executing-plans`.
- No cross-session `continuing` reconciliation UX (the service supports it; the user-facing prompt lives in the LLM runtime).

None of these are Phase 2's responsibility per the spec's §14 build order.
