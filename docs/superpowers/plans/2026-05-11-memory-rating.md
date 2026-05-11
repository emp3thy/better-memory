# Memory Rating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add closed-loop self-rating of reflections and semantic memories so future retrieval ranks by proven usefulness, not just initial confidence.

**Architecture:** Two complementary rating paths share one data model. Mid-session, the LLM calls `memory_credit(kind, id, class)` opportunistically when it uses a memory. At session end, a Stop hook fires with `{"decision":"block", ...}` forcing Claude to invoke the `rate-session-memories` skill, which batches the remaining unrated exposures via `memory_apply_session_ratings`. All three new MCP tools resolve `session_id` server-side from `os.environ["CLAUDE_SESSION_ID"]`.

**Tech Stack:** Python 3.12, sqlite3, pytest, MCP server SDK (stdio transport), Jinja2 + HTMX (UI), Flask app factory pattern.

**Spec:** `docs/superpowers/specs/2026-05-10-memory-rating-design.md` (commit `e2b397e`)

---

## File Map

**New files:**
- `better_memory/db/migrations/0009_memory_rating.sql` — schema migration
- `better_memory/services/memory_rating.py` — `MemoryRatingService` (credit_one + apply_session_ratings)
- `tests/services/test_memory_rating.py` — service unit tests
- `tests/services/test_exposure_tracking.py` — bootstrap + retrieve exposure tests
- `tests/hooks/test_session_close_rating_directive.py` — Stop hook directive tests
- `tests/mcp/test_rating_tools.py` — MCP tool dispatch tests
- `.claude/skills/rate-session-memories/SKILL.md` — new skill (symlinked to user)

**Modified files:**
- `better_memory/services/session_bootstrap.py` — write exposures after render
- `better_memory/services/reflection.py` — write exposures on retrieve; new ORDER BY
- `better_memory/services/semantic.py` — write exposures on list_for_project; new ORDER BY
- `better_memory/mcp/server.py` — register 3 new tools, wire up `MemoryRatingService`
- `better_memory/hooks/session_close.py` — emit Stop directive before marker write
- `better_memory/cli/install_hooks.py` — symlink the new skill
- `better_memory/skills/memory-retrieve.md` — one-liner reminder to credit
- `better_memory/skills/CLAUDE.snippet.md` — opportunistic-credit bullet
- `better_memory/ui/templates/fragments/reflection_row.html` — useful badge
- `better_memory/ui/templates/fragments/reflection_filter_form.html` — useful-only checkbox
- `better_memory/ui/templates/fragments/reflection_drawer.html` — useful/misled lines
- `better_memory/ui/templates/fragments/semantic_row.html` — useful badge
- `better_memory/ui/templates/fragments/semantic_drawer.html` — useful/misled lines
- `better_memory/ui/templates/diagnostics.html` — Recent ratings panel + counter
- `better_memory/ui/app.py` — diagnostics-panel route

---

## Task 1: Schema Migration (0009)

**Files:**
- Create: `better_memory/db/migrations/0009_memory_rating.sql`
- Test: `tests/db/test_migration_0009.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_migration_0009.py`:

```python
"""Migration 0009 — memory_rating schema."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


class TestExposureTable:
    def test_table_created_with_expected_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall()
        }
        assert cols == {
            "session_id", "memory_kind", "memory_id",
            "exposed_at", "source", "rated_at", "classification",
        }

    def test_primary_key_includes_exposed_at(self, conn):
        pk_cols = [
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall() if r["pk"] > 0
        ]
        assert pk_cols == ["session_id", "memory_kind", "memory_id", "exposed_at"]

    def test_unrated_index_exists(self, conn):
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_sme_session_unrated'"
        ).fetchone()
        assert idx is not None


class TestReflectionsNewColumns:
    def test_useful_count_column_exists(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(reflections)"
            ).fetchall()
        }
        assert {"useful_count", "last_useful_at",
                "times_misled", "last_misled_at"} <= cols

    def test_useful_count_defaults_to_zero(self, conn):
        # insert a reflection and verify defaults
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        row = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0
        assert row["times_misled"] == 0


class TestSemanticMemoriesNewColumns:
    def test_useful_count_column_exists(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(semantic_memories)"
            ).fetchall()
        }
        assert {"useful_count", "last_useful_at",
                "times_misled", "last_misled_at"} <= cols
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/db/test_migration_0009.py -v`
Expected: FAIL — `no such table: session_memory_exposure`

- [ ] **Step 3: Create the migration file**

Create `better_memory/db/migrations/0009_memory_rating.sql`:

```sql
-- Migration 0009: memory rating.
--
-- Closed-loop self-rating of reflections and semantic memories.
-- See docs/superpowers/specs/2026-05-10-memory-rating-design.md.
--
-- session_memory_exposure: one row per (session, kind, memory_id, exposed_at).
-- Composite PK includes exposed_at so the same memory exposed at bootstrap
-- AND again mid-session lands as two rows (different timestamps, both kept
-- for audit). rated_at IS NULL gates re-rating.

CREATE TABLE session_memory_exposure (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve')),
    rated_at       TEXT,
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);

-- Rating counters on the memory rows themselves. useful_count is bumped
-- on 'cited' / 'shaped' classifications; times_misled is bumped on 'misled'.
-- 'ignored' is a no-op on the memory row (only stamps the exposure).

ALTER TABLE reflections ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_useful_at TEXT;
ALTER TABLE reflections ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_misled_at TEXT;

ALTER TABLE semantic_memories ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_useful_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_misled_at TEXT;

-- Diagnostics counters surfaced on the /diagnostics page. Currently one
-- counter: session_id_missing (bumped from any service exposure-write
-- path when CLAUDE_SESSION_ID is unset).

CREATE TABLE rating_diagnostics (
    metric     TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

INSERT INTO rating_diagnostics (metric, value) VALUES ('session_id_missing', 0);
```

Append a third test class to verify the diagnostics table:

```python
class TestRatingDiagnosticsTable:
    def test_table_created(self, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='rating_diagnostics'"
        ).fetchone()
        assert row is not None

    def test_session_id_missing_counter_initialised_to_zero(self, conn):
        row = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()
        assert row["value"] == 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/db/test_migration_0009.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0009_memory_rating.sql tests/db/test_migration_0009.py
git commit -m "feat(rating): migration 0009 — session_memory_exposure + counter columns"
```

---

## Task 2: `MemoryRatingService` skeleton + `credit_one`

**Files:**
- Create: `better_memory/services/memory_rating.py`
- Create: `tests/services/test_memory_rating.py`

- [ ] **Step 1: Write the failing tests for `credit_one`**

Create `tests/services/test_memory_rating.py`:

```python
"""Tests for MemoryRatingService.credit_one."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


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
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_exposure(conn, session_id, kind, memory_id, exposed_at="2026-05-11T11:00:00+00:00"):
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES (?, ?, ?, ?, 'bootstrap')",
        (session_id, kind, memory_id, exposed_at),
    )
    conn.commit()


def _seed_reflection(conn, rid="r1"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid,),
    )
    conn.commit()


def _seed_semantic(conn, sid="s1"):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,),
    )
    conn.commit()


class TestCreditOneClassEffects:
    def test_cited_bumps_useful_count_on_reflection(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": "cited", "skipped": None}
        row = conn.execute(
            "SELECT useful_count, last_useful_at FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1
        assert row["last_useful_at"] == "2026-05-11T12:00:00+00:00"

    def test_shaped_bumps_useful_count_on_semantic(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_semantic(conn, "s1")
        _seed_exposure(conn, "S1", "semantic", "s1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="semantic", id="s1", classification="shaped",
        )
        row = conn.execute(
            "SELECT useful_count FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert row["useful_count"] == 1

    def test_misled_bumps_times_misled(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="misled",
        )
        row = conn.execute(
            "SELECT useful_count, times_misled, last_misled_at "
            "FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0
        assert row["times_misled"] == 1
        assert row["last_misled_at"] == "2026-05-11T12:00:00+00:00"

    def test_credit_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        row = conn.execute(
            "SELECT rated_at, classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["rated_at"] == "2026-05-11T12:00:00+00:00"
        assert row["classification"] == "cited"


class TestCreditOneSkips:
    def test_skip_not_exposed(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        # NO exposure row
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "not_exposed"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0

    def test_skip_already_rated(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        # second call is the no-op
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "already_rated"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1  # not double-bumped

    def test_skip_memory_missing(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        # exposure exists but reflection row does NOT
        _seed_exposure(conn, "S1", "reflection", "r-missing")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r-missing",
            classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_missing"}

    def test_skip_memory_retired(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        conn.execute(
            "UPDATE reflections SET status='retired' WHERE id='r1'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_retired"}


class TestCreditOneValidation:
    def test_ignored_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="ignored"):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="ignored",
            )

    def test_unknown_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="bogus",
            )

    def test_unknown_kind_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.credit_one(
                session_id="S1", kind="observation", id="o1", classification="cited",
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/test_memory_rating.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'better_memory.services.memory_rating'`

- [ ] **Step 3: Implement `MemoryRatingService.credit_one`**

Create `better_memory/services/memory_rating.py`:

```python
"""Memory rating service: closed-loop self-rating of reflections and
semantic memories.

Two public methods:
- credit_one: single-row per-tool-use credit, called via memory.credit MCP tool.
- apply_session_ratings: atomic batch update at session end (see Task 3).

Connection ownership: this service writes within its own SAVEPOINT + commit
envelope. Callers must not share a connection that already has an open outer
transaction with another service.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal


def _default_clock() -> datetime:
    return datetime.now(UTC)


Kind = Literal["reflection", "semantic"]
Classification = Literal["cited", "shaped", "ignored", "misled"]
CreditClassification = Literal["cited", "shaped", "misled"]
SkipReason = Literal[
    "not_exposed", "already_rated", "memory_missing", "memory_retired"
]


_VALID_KINDS: set[str] = {"reflection", "semantic"}
_VALID_CLASSES: set[str] = {"cited", "shaped", "ignored", "misled"}
_CREDIT_CLASSES: set[str] = {"cited", "shaped", "misled"}


class MemoryRatingService:
    """Writes useful_count / times_misled on reflections + semantic memories,
    and stamps rated_at / classification on session_memory_exposure rows.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    # --------------------------------------------------------------- credit_one
    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> dict[str, object]:
        """Apply one rating for (session_id, kind, id).

        Validation (ValueError before any DB write):
        - kind must be 'reflection' or 'semantic'.
        - classification must be 'cited', 'shaped', or 'misled' (NOT 'ignored').

        Skip outcomes (no exception, no write, returned via dict):
        - 'not_exposed' — no matching exposure row for this session.
        - 'already_rated' — exposure row has rated_at IS NOT NULL.
        - 'memory_missing' — the memory id no longer exists.
        - 'memory_retired' — reflection has status retired/superseded
          (semantic memories have no status — skip rule doesn't apply).

        Apply outcomes:
        - 'cited' / 'shaped' → useful_count++, last_useful_at = now.
        - 'misled'           → times_misled++, last_misled_at = now.
        And in all apply outcomes, the exposure row is stamped:
        rated_at = now, classification = <input>.

        Returns:
            {"applied": <class>, "skipped": None}  on apply
            {"applied": None,    "skipped": <reason>}  on skip
        """
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"Invalid kind: {kind!r}. Expected one of {_VALID_KINDS}"
            )
        if classification == "ignored":
            raise ValueError(
                "credit_one does not accept classification='ignored'; "
                "'ignored' is the session-end sweep default."
            )
        if classification not in _CREDIT_CLASSES:
            raise ValueError(
                f"Invalid classification: {classification!r}. "
                f"Expected one of {_CREDIT_CLASSES}"
            )

        now = self._clock().isoformat()
        self._conn.execute("SAVEPOINT memory_credit")
        try:
            outcome = self._apply_one(
                session_id=session_id, kind=kind, memory_id=id,
                classification=classification, now=now,
            )
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT memory_credit")
            self._conn.execute("RELEASE SAVEPOINT memory_credit")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT memory_credit")
        self._conn.commit()
        return outcome

    # ------------------------------------------------------------- _apply_one
    def _apply_one(
        self,
        *,
        session_id: str,
        kind: str,
        memory_id: str,
        classification: str,
        now: str,
    ) -> dict[str, object]:
        """Inside-savepoint per-row apply. Returns the same dict shape as
        credit_one. Shared by credit_one and apply_session_ratings (Task 3).
        """
        # 1. Find the unrated exposure row.
        row = self._conn.execute(
            "SELECT rated_at FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?",
            (session_id, kind, memory_id),
        ).fetchone()
        if row is None:
            return {"applied": None, "skipped": "not_exposed"}
        if row["rated_at"] is not None:
            return {"applied": None, "skipped": "already_rated"}

        # 2. Check the memory still exists.
        if kind == "reflection":
            mem = self._conn.execute(
                "SELECT status FROM reflections WHERE id = ?", (memory_id,),
            ).fetchone()
            if mem is None:
                return {"applied": None, "skipped": "memory_missing"}
            if mem["status"] in ("retired", "superseded"):
                return {"applied": None, "skipped": "memory_retired"}
            table = "reflections"
        else:  # semantic
            mem = self._conn.execute(
                "SELECT id FROM semantic_memories WHERE id = ?", (memory_id,),
            ).fetchone()
            if mem is None:
                return {"applied": None, "skipped": "memory_missing"}
            table = "semantic_memories"

        # 3. Bump the appropriate counter.
        if classification in ("cited", "shaped"):
            self._conn.execute(
                f"UPDATE {table} "
                f"SET useful_count = useful_count + 1, last_useful_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        elif classification == "misled":
            self._conn.execute(
                f"UPDATE {table} "
                f"SET times_misled = times_misled + 1, last_misled_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        # 'ignored' is a no-op on the memory row; reached only via
        # apply_session_ratings, not credit_one.

        # 4. Stamp the exposure row.
        self._conn.execute(
            "UPDATE session_memory_exposure "
            "SET rated_at = ?, classification = ? "
            "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?"
            "  AND rated_at IS NULL",
            (now, classification, session_id, kind, memory_id),
        )

        return {"applied": classification, "skipped": None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/services/test_memory_rating.py -v`
Expected: PASS (10 tests in TestCreditOneClassEffects + TestCreditOneSkips + TestCreditOneValidation)

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/memory_rating.py tests/services/test_memory_rating.py
git commit -m "feat(rating): MemoryRatingService.credit_one with skip / validation paths"
```

---

## Task 3: `MemoryRatingService.apply_session_ratings`

**Files:**
- Modify: `better_memory/services/memory_rating.py`
- Modify: `tests/services/test_memory_rating.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_memory_rating.py`:

```python
class TestApplySessionRatings:
    def test_each_class_produces_right_updates(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_semantic(conn, "s1")
        _seed_semantic(conn, "s2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        _seed_exposure(conn, "S1", "semantic",   "s1")
        _seed_exposure(conn, "S1", "semantic",   "s2")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
                {"kind": "semantic",   "id": "s1", "class": "shaped"},
                {"kind": "semantic",   "id": "s2", "class": "misled"},
            ],
        )
        assert result["session_id"] == "S1"
        assert result["applied"] == {
            "cited": 1, "shaped": 1, "ignored": 1, "misled": 1,
        }
        assert all(v == 0 for v in result["skipped"].values())

        # Verify the actual columns.
        r1 = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r1'"
        ).fetchone()
        assert r1["useful_count"] == 1
        r2 = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r2'"
        ).fetchone()
        assert r2["useful_count"] == 0  # ignored is a no-op on memory
        s1 = conn.execute(
            "SELECT useful_count FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert s1["useful_count"] == 1
        s2 = conn.execute(
            "SELECT times_misled FROM semantic_memories WHERE id='s2'"
        ).fetchone()
        assert s2["times_misled"] == 1

    def test_ignored_still_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.apply_session_ratings(
            session_id="S1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored"}],
        )
        row = conn.execute(
            "SELECT rated_at, classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["rated_at"] is not None
        assert row["classification"] == "ignored"

    def test_all_four_skip_counts_exercised(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        # r1: exists + not exposed
        _seed_reflection(conn, "r1")
        # r2: exists + exposed + already rated
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r2")
        conn.execute(
            "UPDATE session_memory_exposure SET rated_at='x' "
            "WHERE memory_id='r2'"
        )
        conn.commit()
        # r3: missing + exposed
        _seed_exposure(conn, "S1", "reflection", "r3")
        # r4: exists + retired + exposed
        _seed_reflection(conn, "r4")
        conn.execute(
            "UPDATE reflections SET status='retired' WHERE id='r4'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r4")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "cited"},
                {"kind": "reflection", "id": "r3", "class": "cited"},
                {"kind": "reflection", "id": "r4", "class": "cited"},
            ],
        )
        assert result["applied"] == {
            "cited": 0, "shaped": 0, "ignored": 0, "misled": 0,
        }
        assert result["skipped"] == {
            "not_exposed": 1, "already_rated": 1,
            "memory_missing": 1, "memory_retired": 1,
        }

    def test_empty_ratings_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="ratings"):
            svc.apply_session_ratings(session_id="S1", ratings=[])

    def test_empty_session_id_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="session_id"):
            svc.apply_session_ratings(
                session_id="",
                ratings=[{"kind": "reflection", "id": "r1", "class": "cited"}],
            )

    def test_duplicate_kind_id_in_batch_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="duplicate"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "cited"},
                    {"kind": "reflection", "id": "r1", "class": "ignored"},
                ],
            )

    def test_invalid_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "bogus"}],
            )

    def test_savepoint_rollback_on_unexpected_error(
        self, conn, fixed_clock, monkeypatch,
    ):
        """If something inside the loop raises, no partial state should land."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        # Patch _apply_one so r1 succeeds, r2 raises.
        original = svc._apply_one
        calls = {"n": 0}

        def patched(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return original(**kw)

        monkeypatch.setattr(svc, "_apply_one", patched)
        with pytest.raises(RuntimeError, match="boom"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "cited"},
                    {"kind": "reflection", "id": "r2", "class": "cited"},
                ],
            )

        # r1's bump should have been rolled back.
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0

    def test_credit_then_sweep_skips_already_rated(self, conn, fixed_clock):
        """Credit one mid-session, then sweep — credited row is skipped."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        # Sweep both ids; r1 should land in skipped.already_rated.
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "ignored"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
            ],
        )
        assert result["applied"]["ignored"] == 1
        assert result["skipped"]["already_rated"] == 1
        # r1 still has useful_count == 1 from the credit (not bumped to 2).
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/test_memory_rating.py::TestApplySessionRatings -v`
Expected: FAIL — `AttributeError: 'MemoryRatingService' object has no attribute 'apply_session_ratings'`

- [ ] **Step 3: Implement `apply_session_ratings`**

Append to `better_memory/services/memory_rating.py` (inside the class):

```python
    # ----------------------------------------------------- apply_session_ratings
    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> dict[str, object]:
        """Atomic batch update at session end.

        Validates the entire batch BEFORE entering the SAVEPOINT:
        - session_id must be non-empty.
        - ratings must be non-empty.
        - each entry must have kind in {'reflection', 'semantic'},
          class in {'cited', 'shaped', 'ignored', 'misled'},
          and a string id.
        - no duplicate (kind, id) pairs in one batch.

        Inside the SAVEPOINT, each entry runs through _apply_one. Skip
        outcomes are counted; apply outcomes are counted. On any
        unhandled exception, the whole batch rolls back.

        Returns:
            {
                "session_id": str,
                "applied":  {"cited": int, "shaped": int, "ignored": int, "misled": int},
                "skipped":  {"not_exposed": int, "already_rated": int,
                             "memory_missing": int, "memory_retired": int},
            }
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not ratings:
            raise ValueError("ratings must be non-empty")

        seen: set[tuple[str, str]] = set()
        for i, r in enumerate(ratings):
            kind = r.get("kind")
            rid = r.get("id")
            cls = r.get("class")
            if kind not in _VALID_KINDS:
                raise ValueError(
                    f"ratings[{i}].kind: invalid {kind!r}; "
                    f"expected one of {_VALID_KINDS}"
                )
            if cls not in _VALID_CLASSES:
                raise ValueError(
                    f"ratings[{i}].class: invalid {cls!r}; "
                    f"expected one of {_VALID_CLASSES}"
                )
            if not isinstance(rid, str) or not rid:
                raise ValueError(f"ratings[{i}].id: must be non-empty string")
            key = (kind, rid)
            if key in seen:
                raise ValueError(
                    f"ratings[{i}]: duplicate (kind, id) = {key!r}"
                )
            seen.add(key)

        now = self._clock().isoformat()
        applied = {"cited": 0, "shaped": 0, "ignored": 0, "misled": 0}
        skipped = {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }

        self._conn.execute("SAVEPOINT memory_rating_apply")
        try:
            for r in ratings:
                outcome = self._apply_one(
                    session_id=session_id,
                    kind=r["kind"],
                    memory_id=r["id"],
                    classification=r["class"],
                    now=now,
                )
                if outcome["applied"] is not None:
                    applied[outcome["applied"]] += 1
                else:
                    skipped[outcome["skipped"]] += 1
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT memory_rating_apply")
            self._conn.execute("RELEASE SAVEPOINT memory_rating_apply")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT memory_rating_apply")
        self._conn.commit()

        return {
            "session_id": session_id,
            "applied": applied,
            "skipped": skipped,
        }
```

Also: `_apply_one` currently handles 'cited', 'shaped', 'misled'. For batch apply with `class='ignored'`, _apply_one needs to stamp the exposure row but skip the memory-row bump. Update `_apply_one`'s counter-bump section in `better_memory/services/memory_rating.py` to handle ignored:

Replace the `# 3. Bump the appropriate counter.` block with:

```python
        # 3. Bump the appropriate counter (or no-op for 'ignored').
        if classification in ("cited", "shaped"):
            self._conn.execute(
                f"UPDATE {table} "
                f"SET useful_count = useful_count + 1, last_useful_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        elif classification == "misled":
            self._conn.execute(
                f"UPDATE {table} "
                f"SET times_misled = times_misled + 1, last_misled_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        # 'ignored' is a no-op on the memory row; only the exposure row
        # is stamped below.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/services/test_memory_rating.py -v`
Expected: PASS (all tests in the file, ~18 total)

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/memory_rating.py tests/services/test_memory_rating.py
git commit -m "feat(rating): apply_session_ratings batch with single SAVEPOINT"
```

---

## Task 4: Exposure tracking — bootstrap path

**Files:**
- Modify: `better_memory/services/session_bootstrap.py`
- Create: `tests/services/test_exposure_tracking.py`

- [ ] **Step 1: Locate the rendering call**

Read `better_memory/services/session_bootstrap.py` and find the `bootstrap` method that returns `BootstrapResult`. The render flow uses `_render_reflection_bucket` and `_render_semantic`. We need to capture the IDs of memories that were actually rendered (top-N per bucket, after the limit_per_bucket cap).

- [ ] **Step 2: Write the failing test**

Create `tests/services/test_exposure_tracking.py`:

```python
"""Tests for exposure tracking on bootstrap + mid-session retrieve paths."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


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
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_reflection(conn, rid, project="p"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, 't', ?, 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, project),
    )
    conn.commit()


def _seed_semantic(conn, sid, project="p"):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', ?, 'project', '2026-01-01', '2026-01-01')""",
        (sid, project),
    )
    conn.commit()


class TestBootstrapExposureWrite:
    def test_bootstrap_writes_exposure_rows_for_injected_memories(
        self, conn, fixed_clock, monkeypatch,
    ):
        """When bootstrap injects reflections + semantic memories, an
        exposure row is recorded for each."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_semantic(conn, "s1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="S1")

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
            "WHERE session_id = ? ORDER BY memory_kind, memory_id",
            ("S1",),
        ).fetchall()
        kinds_ids = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert ("reflection", "r1") in kinds_ids
        assert ("reflection", "r2") in kinds_ids
        assert ("semantic",   "s1") in kinds_ids
        assert all(r["source"] == "bootstrap" for r in rows)

    def test_bootstrap_skips_exposure_when_no_session_id(
        self, conn, fixed_clock,
    ):
        """If session_id is missing, no exposure rows are written
        (no synthetic ids)."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="")  # empty

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []

    def test_bootstrap_exposure_uses_now_as_exposed_at(
        self, conn, fixed_clock,
    ):
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="S1")

        row = conn.execute(
            "SELECT exposed_at FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["exposed_at"] == "2026-05-11T12:00:00+00:00"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestBootstrapExposureWrite -v`
Expected: FAIL — no rows in `session_memory_exposure`

- [ ] **Step 4: Implement the bootstrap exposure write**

Inside `better_memory/services/session_bootstrap.py`, locate the `bootstrap` method's end (after rendering succeeds and `BootstrapResult` is built). Add:

```python
    def _record_exposure(
        self,
        *,
        session_id: str,
        reflection_ids: list[str],
        semantic_ids: list[str],
    ) -> None:
        """Write one row per injected memory into session_memory_exposure.

        Called only after rendering succeeded, so we don't credit memories
        the LLM never actually saw. Best-effort: skips entirely if
        session_id is empty (e.g., manual invocation without env).
        """
        if not session_id:
            return
        now = self._clock().isoformat()
        rows = (
            [(session_id, "reflection", rid, now, "bootstrap")
             for rid in reflection_ids] +
            [(session_id, "semantic", sid, now, "bootstrap")
             for sid in semantic_ids]
        )
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
```

Then in the `bootstrap` method, after the render block decides which IDs are in the rendered envelope, gather them and call `_record_exposure`. Concretely: locate where `_render_reflection_bucket` returns its rendered set; collect the ids as `rendered_reflection_ids` and the `_render_semantic` output as `rendered_semantic_ids`; then before returning `BootstrapResult`, call:

```python
        self._record_exposure(
            session_id=session_id,
            reflection_ids=rendered_reflection_ids,
            semantic_ids=rendered_semantic_ids,
        )
```

If the existing render functions don't surface the rendered IDs, add a small change to make them return `(rendered_html, rendered_ids)` instead of just `rendered_html`. Keep the change minimal — only add ID collection, don't restructure.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestBootstrapExposureWrite -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/session_bootstrap.py tests/services/test_exposure_tracking.py
git commit -m "feat(rating): record session_memory_exposure for bootstrap-injected memories"
```

---

## Task 5: Exposure tracking — reflection retrieve path

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_exposure_tracking.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_exposure_tracking.py`:

```python
class TestReflectionRetrieveExposureWrite:
    def test_retrieve_writes_exposure_rows(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source "
            "FROM session_memory_exposure WHERE session_id='S1'"
        ).fetchall()
        ids = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert ("reflection", "r1") in ids
        assert ("reflection", "r2") in ids
        assert all(r["source"] == "retrieve" for r in rows)

    def test_retrieve_skips_exposure_when_no_env(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []

    def test_retrieve_skips_exposure_for_empty_result(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        # No reflections seeded.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestReflectionRetrieveExposureWrite -v`
Expected: FAIL — no rows in `session_memory_exposure`

- [ ] **Step 3: Implement the retrieve exposure write**

In `better_memory/services/reflection.py`, at the end of `retrieve_reflections` (just before `return buckets`), add:

```python
        # Best-effort exposure tracking. Skip silently when env is missing
        # (e.g., test or non-Claude context) — see spec §5.2.1.
        import os
        sid = os.environ.get("CLAUDE_SESSION_ID")
        if sid:
            all_ids = [
                r["id"] for bucket in buckets.values() for r in bucket
            ]
            if all_ids:
                now = self._clock().isoformat()
                self._conn.executemany(
                    "INSERT OR IGNORE INTO session_memory_exposure "
                    "(session_id, memory_kind, memory_id, exposed_at, source) "
                    "VALUES (?, 'reflection', ?, ?, 'retrieve')",
                    [(sid, rid, now) for rid in all_ids],
                )
                self._conn.commit()
        return buckets
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestReflectionRetrieveExposureWrite -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing reflection retrieve tests to confirm no regressions**

Run: `python -m pytest tests/services/test_reflection.py -v`
Expected: PASS (all existing tests still pass)

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py tests/services/test_exposure_tracking.py
git commit -m "feat(rating): record exposure on retrieve_reflections"
```

---

## Task 6: Exposure tracking — semantic `list_for_project`

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_exposure_tracking.py`

- [ ] **Step 1: Verify `SemanticMemoryService` has a clock**

Read `better_memory/services/semantic.py` and confirm `SemanticMemoryService.__init__` accepts a `clock` parameter (it does — line 46-54). The exposure write needs `self._clock().isoformat()` like reflection.py.

- [ ] **Step 2: Write the failing test**

Append to `tests/services/test_exposure_tracking.py`:

```python
class TestSemanticListExposureWrite:
    def test_list_for_project_writes_exposure_rows(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.semantic import SemanticMemoryService

        _seed_semantic(conn, "s1")
        _seed_semantic(conn, "s2")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.list_for_project(project="p")

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchall()
        ids = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert ("semantic", "s1") in ids
        assert ("semantic", "s2") in ids
        assert all(r["source"] == "retrieve" for r in rows)

    def test_list_for_project_skips_exposure_when_no_env(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.semantic import SemanticMemoryService

        _seed_semantic(conn, "s1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.list_for_project(project="p")

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestSemanticListExposureWrite -v`
Expected: FAIL — no rows

- [ ] **Step 4: Implement the exposure write**

In `better_memory/services/semantic.py`, at the end of `list_for_project` (just before `return [...]`), capture the rows variable's ids and add the same env-gated insert:

```python
        # rows already fetched above
        results = [
            SemanticMemory(...)  # existing list comprehension stays
            for r in rows
        ]
        # Best-effort exposure tracking.
        import os
        sid = os.environ.get("CLAUDE_SESSION_ID")
        if sid and results:
            now = self._clock().isoformat()
            self._conn.executemany(
                "INSERT OR IGNORE INTO session_memory_exposure "
                "(session_id, memory_kind, memory_id, exposed_at, source) "
                "VALUES (?, 'semantic', ?, ?, 'retrieve')",
                [(sid, m.id, now) for m in results],
            )
            self._conn.commit()
        return results
```

Note: the existing code returns the list comprehension directly. Change it to bind to `results` first, then the exposure write, then `return results`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/services/test_exposure_tracking.py::TestSemanticListExposureWrite -v`
Expected: PASS

- [ ] **Step 6: Run existing semantic tests to confirm no regressions**

Run: `python -m pytest tests/services/test_semantic.py -v`
Expected: PASS (all existing tests still pass)

- [ ] **Step 7: Commit**

```bash
git add better_memory/services/semantic.py tests/services/test_exposure_tracking.py
git commit -m "feat(rating): record exposure on semantic list_for_project"
```

---

## Task 7: Retrieval ranking — `ORDER BY useful_count`

**Files:**
- Modify: `better_memory/services/reflection.py` (3 ORDER BY sites)
- Modify: `better_memory/services/semantic.py` (1 ORDER BY site)
- Create: `tests/services/test_useful_count_ranking.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_useful_count_ranking.py`:

```python
"""Verify useful_count is the primary sort key in retrieval."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, *, useful_count=0, confidence=0.5,
                     polarity="do", updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful_count),
    )
    conn.commit()


class TestReflectionRanking:
    def test_useful_count_beats_confidence(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-low-confidence-but-useful",
                          useful_count=5, confidence=0.3)
        _seed_reflection(conn, "r-high-confidence-unused",
                          useful_count=0, confidence=0.9)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-low-confidence-but-useful"
        assert ids[1] == "r-high-confidence-unused"

    def test_confidence_tiebreaks_when_useful_count_equal(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-mid-confidence", useful_count=0, confidence=0.5)
        _seed_reflection(conn, "r-high-confidence", useful_count=0, confidence=0.9)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-high-confidence"

    def test_updated_at_tiebreaks_when_both_equal(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-older",
                          useful_count=2, confidence=0.5, updated_at="2026-01-01")
        _seed_reflection(conn, "r-newer",
                          useful_count=2, confidence=0.5, updated_at="2026-05-01")
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-newer"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/services/test_useful_count_ranking.py -v`
Expected: FAIL — current ORDER BY is `confidence DESC, updated_at DESC` (no useful_count)

- [ ] **Step 3: Update the three reflection ORDER BY sites**

In `better_memory/services/reflection.py`, find each of the three queries and prepend `useful_count DESC,` to the ORDER BY:

**Site 1** (around line 374, inside `_load_episode_context`):

Find:
```sql
ORDER BY confidence DESC, updated_at DESC
```
Replace with:
```sql
ORDER BY useful_count DESC, confidence DESC, updated_at DESC
```

**Site 2** (around line 387, the other branch of `_load_episode_context`):

Same find/replace.

**Site 3** (around line 1154, inside `retrieve_reflections`):

Same find/replace.

Also update `retrieve_reflections` to select `useful_count` so it's available in the returned dict:

In the SELECT clause (around line 1149-1151), add `useful_count` to the column list:
```python
            f"""
            SELECT id, title, phase, polarity, use_cases, hints,
                   confidence, tech, evidence_count, useful_count
            FROM reflections
            WHERE {where}
            ORDER BY useful_count DESC, confidence DESC, updated_at DESC
            """,
```

And in the dict-building loop (around line 1166-1175), include `useful_count`:
```python
            bucket.append({
                "id": r["id"],
                "title": r["title"],
                "phase": r["phase"],
                "use_cases": r["use_cases"],
                "hints": json.loads(r["hints"]),
                "confidence": r["confidence"],
                "tech": r["tech"],
                "evidence_count": r["evidence_count"],
                "useful_count": r["useful_count"],
            })
```

- [ ] **Step 4: Update the semantic ORDER BY**

In `better_memory/services/semantic.py:230`, find:
```python
"ORDER BY created_at DESC"
```
Replace with:
```python
"ORDER BY useful_count DESC, created_at DESC"
```

(Note: the spec said "updated_at" but the existing code uses `created_at DESC` — keep that as the tiebreaker and prepend useful_count.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/services/test_useful_count_ranking.py tests/services/test_reflection.py tests/services/test_semantic.py -v`
Expected: PASS (new + all existing)

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py better_memory/services/semantic.py tests/services/test_useful_count_ranking.py
git commit -m "feat(rating): ORDER BY useful_count DESC in retrieval queries"
```

---

## Task 8: MCP tool — `memory.list_session_exposures`

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_rating_tools.py`

- [ ] **Step 1: Locate the tool dispatch site**

Read `better_memory/mcp/server.py` around line 791 (`@server.call_tool()` decorator's `_call_tool` function). New tools are dispatched here by name. The `_tool_definitions()` function (around line 200-700) exposes their JSON schemas.

- [ ] **Step 2: Write the failing test**

Create `tests/mcp/test_rating_tools.py`:

```python
"""Tests for the three new memory rating MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.conftest import run_async


@pytest.fixture
def memory_db(tmp_memory_db: Path, monkeypatch):
    """Yield a populated memory DB and configure env so create_server uses it."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c, tmp_memory_db
    finally:
        c.close()


def _seed_exposure(c, sid, kind, mid):
    c.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, ?, ?, '2026-05-11T11:00:00+00:00', 'bootstrap')""",
        (sid, kind, mid),
    )
    c.commit()


def _seed_reflection(c, rid):
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, rid),
    )
    c.commit()


class TestListSessionExposures:
    def test_returns_unrated_for_current_session(
        self, memory_db, monkeypatch,
    ):
        """The tool reads CLAUDE_SESSION_ID from env and returns unrated rows."""
        from better_memory.mcp.server import create_server

        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        server, cleanup = create_server()
        try:
            # _call_tool is a closure inside create_server. Drive it via
            # the server's dispatcher. For simplicity, call the underlying
            # service method directly through the registered handler:
            from better_memory.mcp import server as srv_mod
            result = run_async(
                srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
            )
        finally:
            run_async(cleanup())

        payload = json.loads(result[0].text)
        assert payload["session_id"] == "S1"
        assert len(payload["exposures"]) == 1
        assert payload["exposures"][0]["id"] == "r1"
        assert payload["exposures"][0]["kind"] == "reflection"

    def test_empty_when_no_unrated(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["exposures"] == []

    def test_returns_null_session_when_env_missing(
        self, memory_db, monkeypatch,
    ):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        from better_memory.mcp import server as srv_mod
        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["session_id"] is None
        assert payload["exposures"] == []
```

Note: the test uses a hypothetical `_dispatch_for_tests` helper. If the existing server.py doesn't have one, you'll add a thin wrapper in Step 4 to make testing the dispatch tractable without spinning up the stdio transport.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestListSessionExposures -v`
Expected: FAIL — `AttributeError: module 'better_memory.mcp.server' has no attribute '_dispatch_for_tests'`

- [ ] **Step 4: Add the tool registration + test helper**

In `better_memory/mcp/server.py`:

1. Add `MemoryRatingService` to the imports near the top:
```python
from better_memory.services.memory_rating import MemoryRatingService
```

2. Inside `create_server()`, after the `reflections = ReflectionSynthesisService(memory_conn)` line, add:
```python
    memory_rating = MemoryRatingService(memory_conn)
```

3. In `_tool_definitions()`, add a new `Tool` entry for `memory.list_session_exposures`:
```python
        Tool(
            name="memory.list_session_exposures",
            description=(
                "Return the unrated session_memory_exposure rows for the "
                "current Claude session (resolved server-side from "
                "CLAUDE_SESSION_ID env). Read-only; no side effects. "
                "Used by the rate-session-memories skill as the "
                "authoritative anti-hallucination list."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
```

4. In `_call_tool` (the dispatch), add a case for the new tool name. Find the dispatch elif chain and add:
```python
        elif name == "memory.list_session_exposures":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                payload = {"session_id": None, "exposures": []}
            else:
                rows = memory_conn.execute(
                    """
                    SELECT e.memory_kind, e.memory_id, e.exposed_at, e.source,
                           COALESCE(r.title, s.content) AS display
                      FROM session_memory_exposure e
                      LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                                    AND e.memory_id = r.id
                      LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                                    AND e.memory_id = s.id
                     WHERE e.session_id = ? AND e.rated_at IS NULL
                     ORDER BY e.exposed_at ASC
                    """,
                    (sid,),
                ).fetchall()
                payload = {
                    "session_id": sid,
                    "exposures": [
                        {
                            "kind": r["memory_kind"],
                            "id": r["memory_id"],
                            **({"title": r["display"]} if r["memory_kind"] == "reflection"
                               else {"content": r["display"]}),
                            "exposed_at": r["exposed_at"],
                            "source": r["source"],
                        }
                        for r in rows
                    ],
                }
            return [TextContent(type="text", text=json.dumps(payload))]
```

5. Expose a test-only dispatch wrapper. Below `create_server`, add:
```python
async def _dispatch_for_tests(name: str, arguments: dict) -> list[TextContent]:
    """Test-only entry point that runs one tool invocation against a fresh
    server instance. Intentionally NOT used by production code.
    """
    server, cleanup = create_server()
    try:
        # _call_tool is a closure registered inside create_server; pull it out:
        handler = server.request_handlers[CallToolRequest]
        result = await handler(CallToolRequest(
            params=CallToolParams(name=name, arguments=arguments),
        ))
        return result.content
    finally:
        await cleanup()
```

If the precise handler-lookup pattern differs from the actual MCP SDK shape in this codebase, locate how an existing test (e.g. anywhere in `tests/mcp/`) invokes a tool and mirror that pattern.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestListSessionExposures -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_rating_tools.py
git commit -m "feat(rating): MCP tool memory.list_session_exposures"
```

---

## Task 9: MCP tool — `memory.apply_session_ratings`

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_rating_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_rating_tools.py`:

```python
class TestApplySessionRatingsTool:
    def test_applies_batch(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.apply_session_ratings",
            {"ratings": [
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
            ]},
        ))
        payload = json.loads(result[0].text)
        assert payload["applied"]["cited"] == 1
        assert payload["applied"]["ignored"] == 1
        assert payload["session_id"] == "S1"

    def test_raises_value_error_when_env_missing(
        self, memory_db, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        with pytest.raises(ValueError, match="session"):
            run_async(srv_mod._dispatch_for_tests(
                "memory.apply_session_ratings",
                {"ratings": [{"kind": "reflection", "id": "r1", "class": "cited"}]},
            ))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestApplySessionRatingsTool -v`
Expected: FAIL — unknown tool name

- [ ] **Step 3: Implement the tool**

In `better_memory/mcp/server.py`:

1. Add a `Tool` entry in `_tool_definitions()`:
```python
        Tool(
            name="memory.apply_session_ratings",
            description=(
                "Atomic batch rating for the current Claude session "
                "(resolved server-side from CLAUDE_SESSION_ID). Called "
                "at session end by the rate-session-memories skill."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["ratings"],
                "properties": {
                    "ratings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "id", "class"],
                            "properties": {
                                "kind": {"enum": ["reflection", "semantic"]},
                                "id": {"type": "string"},
                                "class": {
                                    "enum": ["cited", "shaped", "ignored", "misled"]
                                },
                            },
                        },
                    },
                },
            },
        ),
```

2. Add a dispatch case in `_call_tool`:
```python
        elif name == "memory.apply_session_ratings":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                raise ValueError("No active session: CLAUDE_SESSION_ID not set")
            payload = memory_rating.apply_session_ratings(
                session_id=sid,
                ratings=args["ratings"],
            )
            return [TextContent(type="text", text=json.dumps(payload))]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestApplySessionRatingsTool -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_rating_tools.py
git commit -m "feat(rating): MCP tool memory.apply_session_ratings"
```

---

## Task 10: MCP tool — `memory.credit`

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_rating_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_rating_tools.py`:

```python
class TestMemoryCreditTool:
    def test_credit_one(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.credit",
            {"kind": "reflection", "id": "r1", "class": "cited"},
        ))
        payload = json.loads(result[0].text)
        assert payload == {"applied": "cited", "skipped": None}

    def test_no_session_returns_skipped(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        result = run_async(srv_mod._dispatch_for_tests(
            "memory.credit",
            {"kind": "reflection", "id": "r1", "class": "cited"},
        ))
        payload = json.loads(result[0].text)
        assert payload == {"applied": None, "skipped": "no_session"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestMemoryCreditTool -v`
Expected: FAIL — unknown tool name

- [ ] **Step 3: Implement the tool**

In `better_memory/mcp/server.py`:

1. Add a `Tool` entry in `_tool_definitions()`:
```python
        Tool(
            name="memory.credit",
            description=(
                "Per-tool-use credit. When you actively use a memory "
                "retrieved during this session (quote it, follow its "
                "guidance, or it misled you), call this immediately. "
                "Resolved server-side from CLAUDE_SESSION_ID. "
                "class must be 'cited', 'shaped', or 'misled' — NOT 'ignored'."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id", "class"],
                "properties": {
                    "kind": {"enum": ["reflection", "semantic"]},
                    "id":   {"type": "string"},
                    "class":{"enum": ["cited", "shaped", "misled"]},
                },
            },
        ),
```

2. Add a dispatch case:
```python
        elif name == "memory.credit":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                payload = {"applied": None, "skipped": "no_session"}
            else:
                payload = memory_rating.credit_one(
                    session_id=sid,
                    kind=args["kind"],
                    id=args["id"],
                    classification=args["class"],
                )
            return [TextContent(type="text", text=json.dumps(payload))]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/mcp/test_rating_tools.py::TestMemoryCreditTool -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full rating-tool test file**

Run: `python -m pytest tests/mcp/test_rating_tools.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 6: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_rating_tools.py
git commit -m "feat(rating): MCP tool memory.credit (per-tool-use credit)"
```

---

## Task 11: `rate-session-memories` skill

**Files:**
- Create: `.claude/skills/rate-session-memories/SKILL.md`
- Modify: `better_memory/cli/install_hooks.py` (add symlink)

- [ ] **Step 1: Inspect the existing skill-symlink pattern**

Read `better_memory/cli/install_hooks.py` and find where `better-memory-synthesize` is symlinked (search for `synthesize` in the file). The pattern should be roughly: create the target dir, check if symlink exists and points to the right place, otherwise back up and (re)create.

- [ ] **Step 2: Write the failing test**

Create `tests/cli/test_install_skill_symlink.py`:

```python
"""Verify install_hooks creates the rate-session-memories skill symlink."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_skills_dir(tmp_path: Path):
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    return skills


def test_install_creates_rate_session_memories_symlink(
    tmp_skills_dir: Path, monkeypatch,
):
    """After install_hooks runs, the user-level skill symlink exists and
    points at the in-repo SKILL.md."""
    from better_memory.cli import install_hooks as ih

    monkeypatch.setattr(ih, "_resolve_user_skills_dir", lambda: tmp_skills_dir)
    ih.install_skill_symlinks()  # new function

    link = tmp_skills_dir / "rate-session-memories"
    assert link.is_symlink()
    target = link.resolve()
    assert target.name == "rate-session-memories"
    assert (target / "SKILL.md").exists()
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/cli/test_install_skill_symlink.py -v`
Expected: FAIL — `AttributeError: install_hooks has no install_skill_symlinks` or symlink not present

- [ ] **Step 4: Create the SKILL.md**

Create `.claude/skills/rate-session-memories/SKILL.md`:

```markdown
---
name: rate-session-memories
description: Use when a session is about to end and the LLM sees a RATE_MEMORIES directive in additionalContext. Also use when the user explicitly asks to rate this session's memories.
---

# Rate Session Memories

You are about to classify the memories exposed in THIS session that have NOT
already been credited via `memory.credit` mid-session.

## STEP 1 — Refresh the list

Call `memory.list_session_exposures` (no arguments — the server resolves the
current session from env). Read the returned list. This is the ONLY valid set
of ids to rate. The list in the RATE_MEMORIES directive may have been truncated.

## STEP 2 — Classify each id

For each `(kind, id)` pair returned by the list, assign exactly ONE class:

- **cited** — you quoted or directly referenced the memory in a reply.
- **shaped** — the memory guided a decision but wasn't cited verbatim.
- **ignored** — you saw it but it didn't affect this session. (Default.)
- **misled** — it caused a wrong direction or wasted effort.

Rules:
- Quote the id in your reasoning so you can't drift.
- Do not invent ids. Do not skip ids.
- If genuinely uncertain between two classes, prefer the lower one
  (shaped > cited, ignored > shaped). `misled` is never a fallback.
- Default is `ignored`. "Shaped" requires evidence you can point to.

## STEP 3 — Submit ALL ratings in ONE call

Call `memory.apply_session_ratings` with this exact shape (no `session_id`
— the server resolves it):

```json
{
  "ratings": [
    {"kind": "reflection", "id": "r-abc...", "class": "cited"},
    {"kind": "semantic",   "id": "s-def...", "class": "ignored"}
  ]
}
```

ONE call. All ratings. Partial batches will be rejected.

## STEP 4 — Verify

The tool returns `{applied: {...}, skipped: {...}}`. If `skipped.not_exposed > 0`,
the server dropped ids that weren't actually exposed — don't retry; it just
means you classified an id that wasn't in the authoritative list.

The session is now marked rated. Continue closing the session.
```

- [ ] **Step 5: Add the symlink installer**

In `better_memory/cli/install_hooks.py`, add (or extend) a skill-symlink helper. If a clean function for this already exists (search for `synthesize` or `symlink` to find prior art), add `rate-session-memories` to its list and skip the fallback below.

Otherwise, add the helper and a `_resolve_user_skills_dir` shim if needed:

```python
def _resolve_user_skills_dir() -> Path:
    """Return ~/.claude/skills/, creating it if missing."""
    target = Path.home() / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    return target


_SKILLS_TO_INSTALL = ["rate-session-memories"]


def install_skill_symlinks() -> None:
    """Symlink each in-repo skill into ~/.claude/skills/.

    Idempotent: if the link already points to the right target, nothing happens.
    If a different file or symlink exists at the target path, back it up via
    the existing _backup helper, then recreate.
    """
    repo_skills_dir = Path(__file__).resolve().parents[2] / ".claude" / "skills"
    user_skills_dir = _resolve_user_skills_dir()

    for skill_name in _SKILLS_TO_INSTALL:
        source = repo_skills_dir / skill_name
        if not source.is_dir():
            continue
        link = user_skills_dir / skill_name
        if link.is_symlink():
            if link.resolve() == source.resolve():
                continue
            _backup(link)
            link.unlink()
        elif link.exists():
            _backup(link)
            link.unlink() if link.is_file() else None
            if link.is_dir():
                import shutil
                shutil.rmtree(link)
        link.symlink_to(source, target_is_directory=True)
```

(If `_backup` doesn't exist in install_hooks.py, fall back to writing the existing file to `$BETTER_MEMORY_HOME/install-backups/<name>.<timestamp>` using the same pattern the rest of install_hooks uses for hook file backups.)

Call `install_skill_symlinks()` from the CLI entrypoint's `main()` (the function that already runs hook installation).

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/cli/test_install_skill_symlink.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/rate-session-memories/SKILL.md better_memory/cli/install_hooks.py tests/cli/test_install_skill_symlink.py
git commit -m "feat(rating): rate-session-memories skill + install symlink"
```

---

## Task 12: Hook — extend `session_close` with Stop-block directive

**Files:**
- Modify: `better_memory/hooks/session_close.py`
- Create: `tests/hooks/test_session_close_rating_directive.py`

**Confidence note (~90%):** the Stop hook decision-block JSON shape is verified against the Claude Code hook docs (spec §6.2), but this is the first time we use it in this codebase. If the test passes and the integration smoke (Task 16) confirms an LLM turn fires, confidence rises to ~98%.

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_session_close_rating_directive.py`:

```python
"""Tests for the rating directive emission in session_close.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


def _run_hook(env: dict[str, str], stdin_data: str = "") -> subprocess.CompletedProcess:
    """Run session_close.py as a subprocess (mirrors how the hook is invoked)."""
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_close"],
        input=stdin_data,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=10,
    )


def _seed_unrated_exposure(db_path: Path, sid: str = "S1"):
    c = connect(db_path)
    apply_migrations(c)
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES ('r1', 'My Title', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')"""
    )
    c.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, 'reflection', 'r1', '2026-05-11T11:00:00+00:00',
                   'bootstrap')""",
        (sid,),
    )
    c.commit()
    c.close()


class TestRatingDirectiveEmission:
    def test_non_empty_unrated_emits_decision_block(
        self, tmp_path, tmp_memory_db,
    ):
        _seed_unrated_exposure(tmp_memory_db, "S1")
        spool = tmp_path / "spool"
        spool.mkdir()
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        # stdout should contain a single JSON object with decision: block.
        payload = json.loads(result.stdout)
        assert payload["decision"] == "block"
        assert "additionalContext" in payload["hookSpecificOutput"]
        assert "RATE_MEMORIES" in payload["hookSpecificOutput"]["additionalContext"]
        assert "r1" in payload["hookSpecificOutput"]["additionalContext"]
        assert "My Title" in payload["hookSpecificOutput"]["additionalContext"]

    def test_empty_unrated_writes_marker_no_directive(
        self, tmp_path, tmp_memory_db,
    ):
        # Migrate but no unrated rows.
        c = connect(tmp_memory_db)
        apply_migrations(c)
        c.close()
        spool = tmp_path / "spool"
        spool.mkdir()
        env = {
            "BETTER_MEMORY_HOME": str(tmp_path),  # spool lives under here
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        # stdout should be empty (no directive) but the marker file is written.
        assert result.stdout.strip() == ""
        markers = list(spool.glob("*_session_end_*.json"))
        assert len(markers) == 1

    def test_db_error_falls_back_to_marker(self, tmp_path):
        """If the DB doesn't exist, the hook still exits 0 and writes a marker."""
        env = {
            "BETTER_MEMORY_HOME": str(tmp_path / "nonexistent"),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        # Hook must exit 0 even on DB errors.
        assert result.returncode == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/hooks/test_session_close_rating_directive.py -v`
Expected: FAIL — current `session_close.py` doesn't emit JSON; stdout is empty.

- [ ] **Step 3: Modify `session_close.py`**

Edit `better_memory/hooks/session_close.py`. Before the existing marker-write block (around line 94 — `spool_dir = _default_spool_dir()`), add a directive-emission step. Wrap it in try/except so any DB error logs to `hook_errors` and falls through to the marker write.

Insert this helper at module top (after imports):

```python
from better_memory.config import get_config


def _emit_rating_directive_if_unrated(session_id: str) -> None:
    """Best-effort: if the current session has any unrated exposures,
    emit a decision:block directive on stdout asking the LLM to rate
    them via the rate-session-memories skill.

    Never raises. On any failure, swallows the exception and returns.
    The caller proceeds to write the session_end marker regardless.
    """
    try:
        from better_memory.db.connection import connect
        cfg = get_config()
        if not cfg.memory_db.exists():
            return
        conn = connect(cfg.memory_db, readonly=True)
        try:
            rows = conn.execute(
                """
                SELECT e.memory_kind, e.memory_id, e.exposed_at,
                       COALESCE(r.title, s.content) AS display
                  FROM session_memory_exposure e
                  LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                                AND e.memory_id = r.id
                  LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                                AND e.memory_id = s.id
                 WHERE e.session_id = ? AND e.rated_at IS NULL
                 ORDER BY e.exposed_at ASC
                """,
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return

        # Bucket and truncate.
        TRUNC = 80
        CAP_BYTES = 8 * 1024
        refl_lines = []
        sem_lines = []
        for r in rows:
            display = (r["display"] or "")[:TRUNC]
            if r["memory_kind"] == "reflection":
                refl_lines.append(f"- {r['memory_id']}: {display}")
            else:
                sem_lines.append(f"- {r['memory_id']}: {display}")

        directive = (
            "RATE_MEMORIES — before this session ends, classify the "
            "memories that were exposed during this session and that you "
            "did NOT already credit via memory.credit.\n\n"
            f"Reflections ({len(refl_lines)}):\n"
            + ("\n".join(refl_lines) if refl_lines else "  (none)")
            + f"\n\nSemantic memories ({len(sem_lines)}):\n"
            + ("\n".join(sem_lines) if sem_lines else "  (none)")
            + "\n\nFor each id, classify as one of:\n"
            "  cited / shaped / ignored / misled (default: ignored)\n\n"
            "Most exposures default to `ignored` — only flag the few "
            "that actually shaped the session or misled you. Invoke "
            "the skill `rate-session-memories`."
        )
        if len(directive.encode("utf-8")) > CAP_BYTES:
            directive = directive[: CAP_BYTES - 200] + (
                "\n\n(list truncated; call memory.list_session_exposures "
                "for the full set)"
            )

        payload = {
            "decision": "block",
            "hookSpecificOutput": {
                "additionalContext": directive,
            },
        }
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
    except BaseException as _exc:
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_close_rating", exc=_exc)
        except BaseException:
            pass
```

Then inside `main()`, just BEFORE the existing spool-marker-write block, insert:

```python
        session_id_str = (
            os.environ.get("CLAUDE_SESSION_ID")
            or data.get("session_id")
            or ""
        )
        if session_id_str:
            _emit_rating_directive_if_unrated(str(session_id_str))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/hooks/test_session_close_rating_directive.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing session_close tests to confirm no regression**

Run: `python -m pytest tests/hooks/ -v -k session_close`
Expected: PASS (all existing session_close tests)

- [ ] **Step 6: Commit**

```bash
git add better_memory/hooks/session_close.py tests/hooks/test_session_close_rating_directive.py
git commit -m "feat(rating): session_close emits Stop-block rating directive"
```

---

## Task 13: Skill docs — opportunistic crediting reminder

**Files:**
- Modify: `better_memory/skills/memory-retrieve.md`
- Modify: `better_memory/skills/CLAUDE.snippet.md`

- [ ] **Step 1: Read both files to find the right insertion point**

Read both files. The reminder needs to land where the LLM will see it right after a retrieval (in `memory-retrieve.md`) and in the general skill index (in `CLAUDE.snippet.md`).

- [ ] **Step 2: Modify `memory-retrieve.md`**

At the end of the file (under the final section, before any closing horizontal rule), append:

```markdown

## Crediting memories you actually use

When you actively use one of the retrieved memories — quote a hint,
follow its do/dont guidance, or it caused a wrong direction — call
`memory.credit(kind, id, class)` **immediately**. Class is `cited`
if quoted, `shaped` if it guided a decision, `misled` if it led you
astray.

This is the fresh-context signal. Memories you don't credit will
default to `ignored` at session end (caught by the
`rate-session-memories` skill). Credit-as-you-go survives compaction;
the session-end sweep can't recover what your context has forgotten.
```

- [ ] **Step 3: Modify `CLAUDE.snippet.md`**

Find the bullet list under "Record" / "Reinforce" (whichever section enumerates the recording rules). Add one bullet:

```markdown
- **Opportunistic crediting:** when you actively use a memory retrieved this session (quote it, follow its guidance, or it misled you), call `mcp__better-memory__memory_credit(kind, id, class)` immediately. The session-end sweep catches whatever you don't credit, defaulting to `ignored`.
```

- [ ] **Step 4: Verify with a quick file read**

Run: `python -c "import pathlib; print(pathlib.Path('better_memory/skills/memory-retrieve.md').read_text()[-500:])"`
Expected: the new section appears in the tail.

- [ ] **Step 5: Commit**

```bash
git add better_memory/skills/memory-retrieve.md better_memory/skills/CLAUDE.snippet.md
git commit -m "docs(rating): credit memories opportunistically when used"
```

---

## Task 14: UI — reflection row badge + filter checkbox

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_row.html`
- Modify: `better_memory/ui/templates/fragments/reflection_filter_form.html`
- Modify: `better_memory/services/reflection.py` (the read model `ReflectionListRow`)
- Modify: `tests/ui/test_browser_reflections.py` or appropriate test file

- [ ] **Step 1: Locate `ReflectionListRow` and `reflection_list_for_ui`**

Search `better_memory/services/reflection.py` for `ReflectionListRow`. The read model used by the UI must include `useful_count` so the template can render the badge.

- [ ] **Step 2: Write the failing test**

In `tests/ui/test_queries_reflections.py`, add:

```python
class TestUsefulCountInReadModel:
    def test_reflection_list_includes_useful_count(self, conn):
        from better_memory.services.reflection import reflection_list_for_ui
        # Seed a reflection with useful_count = 3.
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, useful_count, times_misled)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 3, 1)"""
        )
        conn.commit()
        rows = reflection_list_for_ui(conn, project="p")
        assert any(r.useful_count == 3 and r.times_misled == 1 for r in rows)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/ui/test_queries_reflections.py::TestUsefulCountInReadModel -v`
Expected: FAIL — `ReflectionListRow` doesn't include `useful_count`.

- [ ] **Step 4: Add the field to the read model**

In `better_memory/services/reflection.py`:

1. Find the `ReflectionListRow` dataclass and add fields:
```python
    useful_count: int = 0
    times_misled: int = 0
```

2. Find `reflection_list_for_ui` (the query function) and update its SELECT to include the two new columns; bind them on `ReflectionListRow` construction.

- [ ] **Step 5: Update the row template**

Edit `better_memory/ui/templates/fragments/reflection_row.html`. Find where the confidence chip is rendered and add, conditionally, the useful badge after it:

```html
{% if row.useful_count > 0 %}
  <span class="badge bg-success" title="Times this reflection was useful">
    ★ useful: {{ row.useful_count }}
  </span>
{% endif %}
```

- [ ] **Step 6: Update the filter form**

Edit `better_memory/ui/templates/fragments/reflection_filter_form.html`. Add a checkbox:

```html
<label>
  <input type="checkbox" name="useful_only" value="1"
         {% if filters.useful_only %}checked{% endif %}>
  Useful only
</label>
```

And in the corresponding Flask route handler in `better_memory/ui/app.py`, plumb `useful_only` through the filters dict to the query. In `reflection_list_for_ui`, add the optional filter:

```python
def reflection_list_for_ui(
    conn, *, project, useful_only=False, **other_filters,
):
    ...
    if useful_only:
        where_clauses.append("useful_count > 0")
    ...
```

- [ ] **Step 7: Run the test + existing UI tests**

Run: `python -m pytest tests/ui/test_queries_reflections.py tests/ui/test_browser_reflections.py -v`
Expected: PASS (new test + no regressions)

- [ ] **Step 8: Commit**

```bash
git add better_memory/services/reflection.py better_memory/ui/templates/fragments/reflection_row.html better_memory/ui/templates/fragments/reflection_filter_form.html better_memory/ui/app.py tests/ui/test_queries_reflections.py
git commit -m "feat(rating): useful badge + useful-only filter on Reflections tab"
```

---

## Task 15: UI — reflection drawer useful/misled lines

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html`
- Modify: `better_memory/services/reflection.py` (the `reflection_detail` read model)
- Modify: `tests/ui/test_browser_reflections.py`

- [ ] **Step 1: Write the failing test**

In `tests/ui/test_browser_reflections.py`, add:

```python
def test_drawer_shows_useful_count_when_positive(page, app):
    # ... seed a reflection with useful_count=5 and times_misled=2 ...
    page.click('text=My reflection title')  # opens drawer
    assert page.locator('text=useful: 5').is_visible()
    assert page.locator('text=misled: 2').is_visible()


def test_drawer_hides_misled_when_zero(page, app):
    # ... seed with useful_count=5, times_misled=0 ...
    page.click('text=My reflection title')
    assert page.locator('text=useful: 5').is_visible()
    assert page.locator('text=misled').count() == 0
```

(Pattern: follow the existing drawer test in the same file.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/ui/test_browser_reflections.py -v -k drawer_shows_useful`
Expected: FAIL — text not present

- [ ] **Step 3: Add fields to the drawer read model**

In `better_memory/services/reflection.py`, locate the `reflection_detail` function and add `useful_count`, `last_useful_at`, `times_misled`, `last_misled_at` to both the SELECT and the returned dataclass.

- [ ] **Step 4: Update `reflection_drawer.html`**

Find the section that renders confidence + evidence_count. After it, add:

```html
<div class="drawer-stats">
  <span title="Times the LLM used this reflection">
    useful: {{ reflection.useful_count }}
    {% if reflection.last_useful_at %}
      (last: {{ reflection.last_useful_at | relative_time }})
    {% endif %}
  </span>
  {% if reflection.times_misled > 0 %}
    <span class="text-danger" title="Times the LLM flagged this as misleading">
      misled: {{ reflection.times_misled }}
      {% if reflection.last_misled_at %}
        (last: {{ reflection.last_misled_at | relative_time }})
      {% endif %}
    </span>
  {% endif %}
</div>
```

If `relative_time` doesn't exist as a Jinja filter, fall back to the raw timestamp.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/ui/test_browser_reflections.py -v -k drawer`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py better_memory/ui/templates/fragments/reflection_drawer.html tests/ui/test_browser_reflections.py
git commit -m "feat(rating): reflection drawer shows useful/misled counts"
```

---

## Task 16: UI — semantic row + drawer mirroring

**Files:**
- Modify: `better_memory/ui/templates/fragments/semantic_row.html`
- Modify: `better_memory/ui/templates/fragments/semantic_drawer.html`
- Modify: `better_memory/services/semantic.py` (add useful_count to read paths)
- Modify: `tests/ui/test_browser_semantic.py` (or the equivalent existing file)

- [ ] **Step 1: Write the failing tests**

In `tests/ui/test_browser_semantic.py`:

```python
def test_semantic_row_shows_useful_badge(page, app):
    # Seed a semantic memory with useful_count=2.
    # Visit /semantic — assert the row shows "★ useful: 2".


def test_semantic_drawer_shows_useful_and_misled(page, app):
    # Seed with useful_count=2, times_misled=1.
    # Open drawer, assert both lines visible.
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/ui/test_browser_semantic.py -v -k useful`
Expected: FAIL

- [ ] **Step 3: Update the `SemanticMemory` dataclass and queries**

In `better_memory/services/semantic.py`:

1. Add `useful_count: int = 0` and `times_misled: int = 0` (plus the `last_*` timestamps) to the `SemanticMemory` dataclass.
2. Add the new columns to the SELECT in `list_for_project` and any other read path.

- [ ] **Step 4: Update `semantic_row.html` and `semantic_drawer.html`**

Mirror the reflection-side changes from Tasks 14 and 15. Same badge in the row; same useful/misled lines in the drawer.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/ui/test_browser_semantic.py -v`
Expected: PASS (new + no regressions)

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/semantic.py better_memory/ui/templates/fragments/semantic_row.html better_memory/ui/templates/fragments/semantic_drawer.html tests/ui/test_browser_semantic.py
git commit -m "feat(rating): semantic row badge + drawer useful/misled lines"
```

---

## Task 17: UI — Diagnostics "Recent ratings" panel + `session_id_missing` counter

**Files:**
- Modify: `better_memory/ui/templates/diagnostics.html`
- Modify: `better_memory/ui/app.py` (new route or extend existing diagnostics route)
- Create: `tests/ui/test_diagnostics_ratings.py`
- Modify: each exposure-write site to bump the counter (see below)

- [ ] **Step 1: Wire the counter bump into the env-missing path**

The `rating_diagnostics` table and the `session_id_missing` counter row were already created in Task 1's migration. This task just bumps the counter from each exposure-writing service.

In each exposure-write site (Tasks 5, 6, and the bootstrap helper from Task 4), where the code currently does `if sid: ...else: return`, change the else-branch to bump the counter. Concretely, in `reflection.py` and `semantic.py`:

```python
        sid = os.environ.get("CLAUDE_SESSION_ID")
        if not sid:
            self._conn.execute(
                "UPDATE rating_diagnostics "
                "SET value = value + 1, updated_at = ? "
                "WHERE metric = 'session_id_missing'",
                (self._clock().isoformat(),),
            )
            self._conn.commit()
            return results  # or buckets
```

Do NOT bump the counter from `SessionBootstrapService` — bootstrap always runs inside the SessionStart hook, which always has the env set. A missing env there means something is broken at hook installation time, not "running outside Claude." Bumping there would inflate the counter on every legitimate non-Claude integration test.

- [ ] **Step 2: Write the failing test**

Create `tests/ui/test_diagnostics_ratings.py`:

```python
def test_recent_ratings_panel_lists_rated_exposures(client, conn):
    # Seed a few rated exposures.
    # Visit /diagnostics.
    # Assert the page shows "Recent ratings" with the expected ids.


def test_session_id_missing_counter_displayed(client, conn):
    # Seed rating_diagnostics with session_id_missing=3.
    conn.execute(
        "UPDATE rating_diagnostics SET value=3 WHERE metric='session_id_missing'"
    )
    conn.commit()
    response = client.get("/diagnostics")
    assert b"session_id_missing" in response.data
    assert b"3" in response.data
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/ui/test_diagnostics_ratings.py -v`
Expected: FAIL

- [ ] **Step 4: Add the panel to `diagnostics.html`**

Append a section to `better_memory/ui/templates/diagnostics.html`:

```html
<section class="diagnostics-panel">
  <h2>Recent ratings</h2>
  <p class="small">Last 20 rated session_memory_exposure rows.</p>
  <table class="table">
    <thead><tr><th>rated_at</th><th>kind</th><th>id</th><th>class</th><th>display</th></tr></thead>
    <tbody>
    {% for r in recent_ratings %}
      <tr>
        <td>{{ r.rated_at }}</td>
        <td>{{ r.memory_kind }}</td>
        <td>{{ r.memory_id }}</td>
        <td><span class="badge bg-{{ r.classification | class_badge_color }}">
          {{ r.classification }}</span></td>
        <td>{{ r.display | truncate(60) }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</section>

<section class="diagnostics-panel">
  <h3>Rating diagnostics</h3>
  <dl>
    <dt>session_id_missing</dt>
    <dd>{{ rating_diagnostics.session_id_missing | default(0) }}
      (calls where CLAUDE_SESSION_ID was unset)</dd>
  </dl>
</section>
```

And in `better_memory/ui/app.py`, extend the `/diagnostics` route to populate `recent_ratings` and `rating_diagnostics` in the context:

```python
@app.route("/diagnostics")
def diagnostics():
    # ... existing context build ...
    recent_ratings = conn.execute(
        """
        SELECT e.rated_at, e.memory_kind, e.memory_id, e.classification,
               COALESCE(r.title, s.content) AS display
          FROM session_memory_exposure e
          LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                        AND e.memory_id = r.id
          LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                        AND e.memory_id = s.id
         WHERE e.rated_at IS NOT NULL
         ORDER BY e.rated_at DESC
         LIMIT 20
        """
    ).fetchall()
    diag_rows = conn.execute(
        "SELECT metric, value FROM rating_diagnostics"
    ).fetchall()
    rating_diagnostics = {r["metric"]: r["value"] for r in diag_rows}
    return render_template(
        "diagnostics.html",
        recent_ratings=recent_ratings,
        rating_diagnostics=rating_diagnostics,
        # ... existing context kwargs ...
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/ui/test_diagnostics_ratings.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/diagnostics.html better_memory/ui/app.py better_memory/services/reflection.py better_memory/services/semantic.py tests/ui/test_diagnostics_ratings.py
git commit -m "feat(rating): /diagnostics Recent ratings panel + session_id_missing counter"
```

---

## Task 18: End-to-end integration test

**Files:**
- Create: `tests/integration/test_memory_rating_e2e.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_memory_rating_e2e.py`:

```python
"""End-to-end: bootstrap exposes → retrieve exposes more → mid-session
credit rates some → session-end sweep handles the rest."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.semantic import SemanticMemoryService
from better_memory.services.session_bootstrap import SessionBootstrapService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(c, rid):
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""", (rid, rid))
    c.commit()


def _seed_semantic(c, sid):
    c.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,))
    c.commit()


def test_full_rating_loop(conn, monkeypatch):
    clock = lambda: datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "SESS-1")

    # Seed three reflections, two semantic memories.
    for r in ("r1", "r2", "r3"):
        _seed_reflection(conn, r)
    for s in ("s1", "s2"):
        _seed_semantic(conn, s)

    # 1) Bootstrap injects them. Exposures appear.
    boot = SessionBootstrapService(conn, clock=clock)
    boot.bootstrap(project="p", session_id="SESS-1")

    exposed_after_boot = conn.execute(
        "SELECT memory_kind, memory_id FROM session_memory_exposure "
        "WHERE session_id='SESS-1'"
    ).fetchall()
    assert len(exposed_after_boot) == 5

    # 2) Mid-session: another retrieve. Two of the reflections get
    # exposed again with source='retrieve'.
    refl = ReflectionSynthesisService(conn, clock=clock)
    refl.retrieve_reflections(project="p")
    sem = SemanticMemoryService(conn, clock=clock)
    sem.list_for_project(project="p")

    sources = conn.execute(
        "SELECT source, COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1' GROUP BY source"
    ).fetchall()
    source_counts = {r["source"]: r["n"] for r in sources}
    assert source_counts["bootstrap"] == 5
    assert source_counts["retrieve"] == 5  # 3 refl + 2 sem on the retrieve pass

    # 3) Credit r1 and s1 mid-session.
    rating = MemoryRatingService(conn, clock=clock)
    rating.credit_one(
        session_id="SESS-1", kind="reflection", id="r1", classification="cited",
    )
    rating.credit_one(
        session_id="SESS-1", kind="semantic", id="s1", classification="shaped",
    )

    # 4) Session-end sweep handles the rest.
    unrated = conn.execute(
        "SELECT memory_kind, memory_id FROM session_memory_exposure "
        "WHERE session_id='SESS-1' AND rated_at IS NULL"
    ).fetchall()
    ratings = [
        {"kind": r["memory_kind"], "id": r["memory_id"], "class": "ignored"}
        for r in unrated
    ]
    result = rating.apply_session_ratings(
        session_id="SESS-1", ratings=ratings,
    )

    # ignored is counted, not the credited rows.
    assert result["applied"]["ignored"] == len(unrated)

    # 5) Re-running the sweep query finds nothing.
    still_unrated = conn.execute(
        "SELECT COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1' AND rated_at IS NULL"
    ).fetchone()["n"]
    assert still_unrated == 0

    # 6) Counters reflect both paths.
    r1 = conn.execute(
        "SELECT useful_count FROM reflections WHERE id='r1'"
    ).fetchone()
    assert r1["useful_count"] == 1  # from credit
    s1 = conn.execute(
        "SELECT useful_count FROM semantic_memories WHERE id='s1'"
    ).fetchone()
    assert s1["useful_count"] == 1  # from credit
    r2 = conn.execute(
        "SELECT useful_count FROM reflections WHERE id='r2'"
    ).fetchone()
    assert r2["useful_count"] == 0  # ignored
```

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/integration/test_memory_rating_e2e.py -v`
Expected: PASS

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS (everything)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_memory_rating_e2e.py
git commit -m "test(rating): end-to-end bootstrap → credit → sweep round-trip"
```

---

## Spec coverage check

| Spec section | Implemented in |
|---|---|
| §4.1 `session_memory_exposure` table | Task 1 |
| §4.2 counter columns on reflections + semantic_memories | Task 1 |
| §5.1 bootstrap exposure write | Task 4 |
| §5.2 retrieve exposure writes (reflections) | Task 5 |
| §5.2 retrieve exposure writes (semantic) | Task 6 |
| §5.2.1 env-resolved session_id with skip-on-missing | Tasks 5, 6, 17 (counter) |
| §6.1 / §6.2 Stop hook with decision:block directive | Task 12 |
| §6.3 directive template | Task 12 |
| §7 `rate-session-memories` skill + symlink | Task 11 |
| §7.5 opportunistic crediting guidance | Task 13 |
| §8.1 `memory.list_session_exposures` | Task 8 |
| §8.2 `memory.apply_session_ratings` | Task 9 |
| §8.3 `memory.credit` | Task 10 |
| §8.4 `MemoryRatingService` | Tasks 2, 3 |
| §9.1 ORDER BY useful_count | Task 7 |
| §10 UI surfacing (reflection row + filter + drawer; semantic row + drawer; diagnostics) | Tasks 14, 15, 16, 17 |
| §11 error handling | Coverage embedded in service / hook tests across Tasks 2–12 |
| §12 testing strategy | Per-task tests + Task 18 e2e |
| §13.1 verifications | Already done at spec time — no implementation tasks needed |

---

## Plan complete

Saved to: `docs/superpowers/plans/2026-05-11-memory-rating.md`

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
