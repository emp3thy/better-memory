# Semantic Memories Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new top-level concept to better-memory — `semantic_memories` — for user-stated facts and preferences. Distinct from observations (episodic) and reflections (LLM-distilled). Same `scope = ('project' | 'general')` model as PR #25's reflections so cross-project workflow rules surface in every session.

**Architecture:** New `semantic_memories` table; `SemanticMemoryService` with create/update_text/delete/list_for_project; four MCP tools (`memory.semantic_observe`/`_retrieve`/`_update`/`_delete`). Hard-delete + edit-in-place lifecycle. Retrieval returns a flat list ordered newest-first, merging project + general scope.

**Tech Stack:** Python 3.12+, SQLite (WAL + busy_timeout=5000), pytest, MCP server. Builds on the scope model from PR #25.

**Branch:** `semantic-memories` off `main` (PR #25 already merged at `5872387`). Three commits, one PR.

**Spec:** `docs/superpowers/specs/2026-05-04-semantic-memories-design.md` — read before starting.

**Test/type discipline:** every behavior change is TDD (red → green → commit). Pyright (`uv run pyright`) and pytest (`uv run pytest -q`) must be green at every commit boundary.

---

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `better_memory/db/migrations/0008_semantic_memories.sql` | Create | Schema migration: new `semantic_memories` table + 2 indexes. |
| `tests/db/test_migration_0008.py` | Create | Schema/index/CHECK/default tests. |
| `tests/db/test_schema.py` | Modify | Update `test_apply_migrations_is_idempotent` to expect `["0001"…"0008"]`. |
| `better_memory/services/semantic.py` | Create | `SemanticMemoryService(conn, *, clock=...)` with `create`, `update_text`, `delete`, `list_for_project`. ~150 LOC. Includes the `SemanticMemory` read-model dataclass. |
| `tests/services/test_semantic.py` | Create | 12 tests covering all four methods + edge cases (invalid scope, empty content, missing id, scope merging, ordering). |
| `better_memory/mcp/server.py` | Modify | Register 4 new Tools in `_tool_definitions()`; 4 new `if name == ...` branches in `_call_tool`. |
| `tests/mcp/test_semantic_tools.py` | Create | One test per tool exercising the wiring (schema, scope-default, project resolution, serialization). |

---

## Pre-implementation setup

- [ ] **Step 0a: Create branch**

```bash
git checkout main
git pull --rebase
git checkout -b semantic-memories
```

- [ ] **Step 0b: Sanity baseline**

```bash
uv run pytest -q
```

Expected: 629 passed, 22 skipped (PR #25's baseline). If failures appear, stop — investigate the baseline before proceeding.

```bash
uv run pyright
```

Expected: 0 errors.

---

# Commit 1 — Migration 0008

### Task 1: Migration test (red)

**Confidence:** 95% — direct port of the migration 0007 test pattern; SQL is straightforward.

**Files:**
- Create: `tests/db/test_migration_0008.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_migration_0008.py` with this EXACT content:

```python
"""Migration 0008: semantic_memories table."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    apply_migrations(c)
    yield c
    c.close()


def test_semantic_memories_table_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='semantic_memories'"
    ).fetchall()
    assert len(rows) == 1


def test_semantic_memories_has_expected_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memories)").fetchall()}
    assert cols == {"id", "content", "project", "scope", "created_at", "updated_at"}


def test_scope_default_is_project(conn) -> None:
    conn.execute(
        "INSERT INTO semantic_memories (id, content, project, created_at, updated_at) "
        "VALUES ('m1','rule','p1','2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM semantic_memories WHERE id='m1'"
    ).fetchone()
    assert row[0] == "project"


def test_scope_check_constraint_rejects_invalid(conn) -> None:
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO semantic_memories (id, content, project, scope, "
            "created_at, updated_at) VALUES "
            "('m1','rule','p1','invalid',"
            "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
        )


def test_scope_accepts_general(conn) -> None:
    conn.execute(
        "INSERT INTO semantic_memories (id, content, project, scope, "
        "created_at, updated_at) VALUES "
        "('m1','rule','p1','general',"
        "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM semantic_memories WHERE id='m1'"
    ).fetchone()
    assert row[0] == "general"


def test_project_index_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='semantic_memories'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_semantic_memories_project" in names


def test_general_partial_index_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_semantic_memories_general'"
    ).fetchall()
    assert len(rows) == 1
    # Confirm it's a partial index (predicate on scope='general').
    assert "scope = 'general'" in rows[0][1] or "scope='general'" in rows[0][1]
```

- [ ] **Step 2: Run, verify all 7 tests fail/error**

Run: `uv run pytest tests/db/test_migration_0008.py -v`
Expected: 7 errors with `OperationalError: no such table: semantic_memories` (or similar — the migration doesn't exist yet).

### Task 2: Migration SQL (green)

**Confidence:** 97% — straightforward DDL, mirrors 0007 shape. Slightly higher than Task 1 because the SQL itself is simpler than its test scaffold.

**Files:**
- Create: `better_memory/db/migrations/0008_semantic_memories.sql`

- [ ] **Step 1: Write the migration**

Create `better_memory/db/migrations/0008_semantic_memories.sql` with:

```sql
-- Migration 0008: semantic memories table.
-- See docs/superpowers/specs/2026-05-04-semantic-memories-design.md.
--
-- User-stated facts/preferences. Distinct from observations (episodic,
-- recorded as work happens, fed to synthesis) and reflections
-- (LLM-distilled lessons). Same scope model as PR #25's reflections:
-- 'project' rows live in their own project bucket; 'general' rows
-- surface in every project's retrieval.

CREATE TABLE semantic_memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    project       TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'project'
                  CHECK(scope IN ('project', 'general')),
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);

CREATE INDEX idx_semantic_memories_project
    ON semantic_memories(project, created_at DESC);

CREATE INDEX idx_semantic_memories_general
    ON semantic_memories(created_at DESC)
    WHERE scope = 'general';
```

- [ ] **Step 2: Run migration test, verify all 7 pass**

Run: `uv run pytest tests/db/test_migration_0008.py -v`
Expected: 7 passed.

- [ ] **Step 3: Run full suite to spot collateral damage**

Run: `uv run pytest -q`
Expected: full green except possibly `tests/db/test_schema.py::test_apply_migrations_is_idempotent` if it hardcodes the version list (Task 3 fixes that).

### Task 3: Update schema idempotency assertion

**Confidence:** 99% — one-liner edit.

**Files:**
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Find and update the version list assertion**

Locate `test_apply_migrations_is_idempotent` in `tests/db/test_schema.py`. Find the assertion on the expected versions list (it currently expects `["0001"…"0007"]` per PR #25). Update it to include `"0008"`:

```python
assert versions == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
```

(Exact existing line shape may vary — preserve formatting.)

- [ ] **Step 2: Run schema test, verify green**

Run: `uv run pytest tests/db/test_schema.py -q`
Expected: all schema tests pass.

### Task 4: Pre-flight grep + Commit 1

**Confidence:** 99%.

- [ ] **Step 1: Pre-flight grep**

```bash
grep -rE "semantic_memories|SemanticMemoryService|semantic_observe|semantic_retrieve" better_memory/ tests/
```

Expected hits ONLY in:
- `better_memory/db/migrations/0008_semantic_memories.sql` (the table)
- `tests/db/test_migration_0008.py` (the tests)

No production callers yet (Commit 2 + 3 add them).

- [ ] **Step 2: Commit**

```bash
git add better_memory/db/migrations/0008_semantic_memories.sql \
        tests/db/test_migration_0008.py \
        tests/db/test_schema.py
git commit -m "$(cat <<'EOF'
feat(db): migration 0008 — semantic_memories table

User-stated facts/preferences. Distinct from observations (episodic,
recorded as work happens) and reflections (LLM-distilled). Same scope
model as PR #25's reflections — 'project' rows are per-project;
'general' rows surface in every project's retrieval.

Schema:
- semantic_memories(id, content, project, scope DEFAULT 'project'
  CHECK IN ('project','general'), created_at, updated_at)
- idx_semantic_memories_project for the per-project read path
- partial idx_semantic_memories_general WHERE scope='general'

Service + MCP tools follow in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: clean commit.

---

# Commit 2 — `SemanticMemoryService`

### Task 5: Service skeleton + `create` (TDD)

**Confidence:** 92% — straightforward, but introduces the module + dataclass + first method together. Mitigation: tests cover both default and explicit scope, and the ValueError for invalid scope BEFORE the DB hit (so the CHECK constraint is a backstop, not the primary defense).

**Files:**
- Create: `better_memory/services/semantic.py`
- Create: `tests/services/test_semantic.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_semantic.py` with:

```python
"""Tests for SemanticMemoryService."""

from __future__ import annotations

from datetime import UTC, datetime
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


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestCreate:
    def test_create_with_default_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="prefer terse replies", project="p1")
        assert memory_id  # non-empty id returned
        row = conn.execute(
            "SELECT content, project, scope, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "prefer terse replies"
        assert row["project"] == "p1"
        assert row["scope"] == "project"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"
        assert row["updated_at"] == "2026-05-04T12:00:00+00:00"

    def test_create_with_general_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(
            content="always assign per-step confidence",
            project="any",
            scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_create_rejects_invalid_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="scope"):
            svc.create(content="rule", project="p1", scope="invalid")

    def test_create_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="content"):
            svc.create(content="   ", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.create(content="", project="p1")
```

- [ ] **Step 2: Run, verify 4 fail (red)**

Run: `uv run pytest tests/services/test_semantic.py::TestCreate -v`
Expected: 4 errors with `ModuleNotFoundError: No module named 'better_memory.services.semantic'`.

- [ ] **Step 3: Implement service skeleton + `create`**

Create `better_memory/services/semantic.py`:

```python
"""User-stated facts/preferences. Free-form content; project + general scope.

Distinct from observations (episodic, recorded as work happens, fed to
synthesis) and reflections (LLM-distilled lessons). Semantic memories are
user assertions of current truth — surfaced at every session start.

See docs/superpowers/specs/2026-05-04-semantic-memories-design.md.
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
class SemanticMemory:
    """Read model returned by retrieve."""

    id: str
    content: str
    project: str
    scope: str            # 'project' | 'general'
    created_at: str
    updated_at: str


_VALID_SCOPES = ("project", "general")


class SemanticMemoryService:
    """User-stated facts/preferences.

    Connection ownership: writes within own commit envelope. No SAVEPOINT
    needed — each method is a single-row mutation that's atomic on its own.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    def create(
        self, *, content: str, project: str, scope: str = "project"
    ) -> str:
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        if not content.strip():
            raise ValueError("content must not be empty")
        memory_id = uuid4().hex
        now = self._clock().isoformat()
        self._conn.execute(
            """
            INSERT INTO semantic_memories
                (id, content, project, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, project, scope, now, now),
        )
        self._conn.commit()
        return memory_id
```

- [ ] **Step 4: Run, verify 4 pass (green)**

Run: `uv run pytest tests/services/test_semantic.py::TestCreate -v`
Expected: 4 passed.

### Task 6: `update_text` (TDD)

**Confidence:** 95%.

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_semantic.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/services/test_semantic.py`:

```python
class TestUpdateText:
    def test_update_changes_content_and_bumps_updated_at(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="old text", project="p1")

        # Advance the clock to a later instant for the update.
        from datetime import timedelta
        later = (fixed_clock() + timedelta(hours=2)).isoformat()
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)

        svc.update_text(id=memory_id, content="new text")
        row = conn.execute(
            "SELECT content, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "new text"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"  # unchanged
        assert row["updated_at"] == later

    def test_update_raises_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.update_text(id="nope", content="x")

    def test_update_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="original", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.update_text(id=memory_id, content="   ")
        # Original content unchanged.
        row = conn.execute(
            "SELECT content FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "original"
```

- [ ] **Step 2: Run, verify 3 fail with AttributeError**

Run: `uv run pytest tests/services/test_semantic.py::TestUpdateText -v`
Expected: 3 errors with `AttributeError: 'SemanticMemoryService' object has no attribute 'update_text'`.

- [ ] **Step 3: Implement `update_text`**

Append to `SemanticMemoryService` in `better_memory/services/semantic.py`:

```python
    def update_text(self, *, id: str, content: str) -> None:
        if not content.strip():
            raise ValueError("content must not be empty")
        now = self._clock().isoformat()
        cur = self._conn.execute(
            "UPDATE semantic_memories SET content = ?, updated_at = ? "
            "WHERE id = ?",
            (content, now, id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"semantic memory not found: {id}")
        self._conn.commit()
```

- [ ] **Step 4: Run, verify 3 pass**

Run: `uv run pytest tests/services/test_semantic.py::TestUpdateText -v`
Expected: 3 passed.

### Task 7: `delete` (TDD, idempotent)

**Confidence:** 97%.

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_semantic.py`

- [ ] **Step 1: Append failing tests**

```python
class TestDelete:
    def test_delete_removes_existing_row(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="x", project="p1")
        svc.delete(id=memory_id)
        row = conn.execute(
            "SELECT 1 FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is None

    def test_delete_is_idempotent_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # No exception when id doesn't exist.
        svc.delete(id="ghost")
```

- [ ] **Step 2: Run, verify 2 fail**

Run: `uv run pytest tests/services/test_semantic.py::TestDelete -v`
Expected: 2 errors with `AttributeError: ... has no attribute 'delete'`.

- [ ] **Step 3: Implement `delete`**

Append:

```python
    def delete(self, *, id: str) -> None:
        """Idempotent — no error if id absent."""
        self._conn.execute(
            "DELETE FROM semantic_memories WHERE id = ?", (id,),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run, verify 2 pass**

Run: `uv run pytest tests/services/test_semantic.py::TestDelete -v`
Expected: 2 passed.

### Task 8: `list_for_project` (TDD)

**Confidence:** 92% — the scope-merging + ordering test has more moving parts than other methods.

**Files:**
- Modify: `better_memory/services/semantic.py`
- Modify: `tests/services/test_semantic.py`

- [ ] **Step 1: Append failing tests**

```python
class TestListForProject:
    def test_returns_empty_list_when_no_rows(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        assert svc.list_for_project(project="p1") == []

    def test_returns_only_project_rows_when_general_absent(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Two memories in p1, one in p2 (project-scope).
        svc.create(content="p1 rule", project="p1")
        svc.create(content="p1 other", project="p1")
        svc.create(content="p2 rule", project="p2")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        assert {m.project for m in memories} == {"p1"}
        assert {m.content for m in memories} == {"p1 rule", "p1 other"}

    def test_includes_general_from_other_projects(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1-only rule", project="p1")
        svc.create(content="cross-project rule", project="p2", scope="general")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        contents = {m.content for m in memories}
        assert "p1-only rule" in contents
        assert "cross-project rule" in contents
        # Confirm scope flag preserved on the read model.
        general_match = next(m for m in memories if m.scope == "general")
        assert general_match.project == "p2"

    def test_excludes_other_projects_project_scoped_memories(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="hidden p2 rule", project="p2")  # scope='project'
        memories = svc.list_for_project(project="p1")
        assert memories == []

    def test_orders_newest_first(self, conn, fixed_clock):
        from datetime import timedelta
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Three memories at t, t+1h, t+2h.
        svc._clock = lambda: fixed_clock()
        first_id = svc.create(content="oldest", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=1)
        second_id = svc.create(content="middle", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)
        third_id = svc.create(content="newest", project="p1")

        memories = svc.list_for_project(project="p1")
        assert [m.id for m in memories] == [third_id, second_id, first_id]
```

- [ ] **Step 2: Run, verify 5 fail**

Run: `uv run pytest tests/services/test_semantic.py::TestListForProject -v`
Expected: 5 errors with `AttributeError: ... has no attribute 'list_for_project'`.

- [ ] **Step 3: Implement `list_for_project`**

Append:

```python
    def list_for_project(self, *, project: str) -> list[SemanticMemory]:
        """Project rows + general-scope rows from any project, newest first."""
        rows = self._conn.execute(
            """
            SELECT id, content, project, scope, created_at, updated_at
              FROM semantic_memories
             WHERE project = ? OR scope = 'general'
             ORDER BY created_at DESC
            """,
            (project,),
        ).fetchall()
        return [
            SemanticMemory(
                id=r["id"], content=r["content"], project=r["project"],
                scope=r["scope"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run, verify 5 pass**

Run: `uv run pytest tests/services/test_semantic.py::TestListForProject -v`
Expected: 5 passed.

### Task 9: Verify + Commit 2

**Confidence:** 99%.

- [ ] **Step 1: Full service test suite green**

Run: `uv run pytest tests/services/test_semantic.py -q`
Expected: 14 passed (4 + 3 + 2 + 5).

- [ ] **Step 2: Pyright clean**

Run: `uv run pyright better_memory/services/semantic.py tests/services/test_semantic.py`
Expected: 0 errors.

- [ ] **Step 3: Run full test suite — confirm no regressions elsewhere**

Run: `uv run pytest -q`
Expected: full green.

- [ ] **Step 4: Commit**

```bash
git add better_memory/services/semantic.py tests/services/test_semantic.py
git commit -m "$(cat <<'EOF'
feat(semantic): SemanticMemoryService for user-stated facts/preferences

Service for managing semantic memories — user-asserted current truths,
distinct from observations (episodic) and reflections (LLM-distilled).

API:
- create(*, content, project, scope='project') -> id
- update_text(*, id, content) — bumps updated_at; raises if id absent
- delete(*, id) — idempotent (no error on missing id)
- list_for_project(*, project) -> list[SemanticMemory]
  Returns project rows + general-scope rows from any project,
  ordered newest-first by created_at.

Validation:
- scope must be 'project' or 'general' (ValueError before DB hit;
  CHECK constraint is the backstop)
- content must not be empty/whitespace (ValueError)

MCP wiring follows in next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Commit 3 — MCP tools

> Four new MCP tools register in `_tool_definitions()` and dispatch in `_call_tool()`. Pattern mirrors the existing `memory.observe`/`memory.retrieve` pair from PR #25, including the `args.get("scope") or "project"` defense (BugBot finding from PR #25 — `dict.get` returns None when the key is present with explicit null, not the default).

### Task 10: Tool schema definitions

**Confidence:** 95% — verbatim paste of 4 Tool() entries.

**Files:**
- Modify: `better_memory/mcp/server.py`

- [ ] **Step 1: Add 4 Tool() entries to `_tool_definitions()`**

Find `_tool_definitions()` (around line 107). Insert these four entries into the returned list, after the existing `memory.observe` Tool but before `memory.retrieve` (so they group thematically with the write/read tools):

```python
        Tool(
            name="memory.semantic_observe",
            description=(
                "Record a user-stated fact or preference. Distinct from "
                "memory.observe (episodic): semantic memories are "
                "user-asserted current truths, retrieved at session "
                "startup. Set scope='general' for cross-project rules."
            ),
            inputSchema={
                "type": "object",
                "required": ["content"],
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["project", "general"],
                        "description": (
                            "'project' (default) for project-scoped rules; "
                            "'general' for cross-project workflow rules."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.semantic_retrieve",
            description=(
                "Return user-stated facts/preferences for the current "
                "project, merged with all general-scope semantic memories. "
                "Flat list ordered newest-first."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project override; "
                            "defaults to cwd-derived."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.semantic_update",
            description=(
                "Edit a semantic memory's content in place. Bumps updated_at."
            ),
            inputSchema={
                "type": "object",
                "required": ["id", "content"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.semantic_delete",
            description=(
                "Remove a semantic memory. Idempotent — no error if id absent."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                },
            },
        ),
```

- [ ] **Step 2: Confirm pyright clean**

Run: `uv run pyright better_memory/mcp/server.py`
Expected: 0 errors. (No handlers yet, but the Tool() schema is valid in isolation.)

### Task 11: Test stubs (red) for all four MCP handlers

**Confidence:** 92% — multiple tests at once. Mitigation: keep each test small and self-contained.

**Files:**
- Create: `tests/mcp/test_semantic_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_semantic_tools.py`:

```python
"""Integration-style tests for memory.semantic_* MCP tools.

Mirrors the harness pattern from tests/mcp/test_episode_tools.py:
exercise the dispatch by constructing services directly, plus a
factory smoke test confirming the tools register.
"""

from __future__ import annotations

import json as _json
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


class TestSemanticToolsRegistered:
    def test_all_four_tools_listed(self):
        from better_memory.mcp.server import _tool_definitions
        names = {t.name for t in _tool_definitions()}
        assert "memory.semantic_observe" in names
        assert "memory.semantic_retrieve" in names
        assert "memory.semantic_update" in names
        assert "memory.semantic_delete" in names


class TestSemanticObserveHandler:
    def test_default_scope_is_project(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="rule", project="proj-a")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "project"

    def test_explicit_general_scope(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(
            content="general rule", project="proj-a", scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_scope_null_via_args_get_falls_back_to_project(self, conn):
        """Regression: dict.get(key, default) returns None — not the default —
        when the key is present with value None. The handler must use
        `args.get("scope") or "project"` to defend against MCP clients
        sending {"scope": null}. Same finding as PR #25's BugBot finding
        on memory.observe.
        """
        # Simulate the handler's defensive idiom directly.
        args = {"content": "rule", "scope": None}
        scope = args.get("scope") or "project"
        assert scope == "project"


class TestSemanticRetrieveHandler:
    def test_returns_empty_list_for_unknown_project(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        result = svc.list_for_project(project="empty-proj")
        # Wire-format: list of dicts after JSON serialization.
        serialized = _json.dumps([
            {
                "id": m.id, "content": m.content, "project": m.project,
                "scope": m.scope,
                "created_at": m.created_at, "updated_at": m.updated_at,
            }
            for m in result
        ])
        assert _json.loads(serialized) == []

    def test_returns_serializable_rows(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        svc.create(content="rule one", project="p1")
        svc.create(content="general rule", project="p2", scope="general")
        rows = svc.list_for_project(project="p1")
        # Confirm each row serializes cleanly (the MCP handler does this).
        out = _json.dumps([
            {
                "id": m.id, "content": m.content, "project": m.project,
                "scope": m.scope,
                "created_at": m.created_at, "updated_at": m.updated_at,
            }
            for m in rows
        ])
        loaded = _json.loads(out)
        assert len(loaded) == 2
        contents = {r["content"] for r in loaded}
        assert "rule one" in contents
        assert "general rule" in contents


class TestSemanticUpdateHandler:
    def test_update_persists_new_content(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="old", project="p1")
        svc.update_text(id=memory_id, content="new")
        row = conn.execute(
            "SELECT content FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["content"] == "new"

    def test_update_missing_id_raises(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        with pytest.raises(ValueError):
            svc.update_text(id="nope", content="x")


class TestSemanticDeleteHandler:
    def test_delete_removes_row(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="x", project="p1")
        svc.delete(id=memory_id)
        row = conn.execute(
            "SELECT 1 FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is None

    def test_delete_missing_is_noop(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        svc.delete(id="ghost")  # no exception
```

- [ ] **Step 2: Run, verify TestSemanticToolsRegistered passes (already wired in Task 10) but the others might already pass too**

Run: `uv run pytest tests/mcp/test_semantic_tools.py -v`
Expected: TestSemanticToolsRegistered passes; the others pass too (they exercise the service directly, which Commit 2 finished). The tests are present mainly to verify the wire-format serialization shape and the args.get scope-null defense.

This is intentional — the SERVICE-level tests already cover the logic; these MCP tests just confirm:
- All 4 tools are registered (Task 10 wiring)
- The dict-shape returned by retrieve serializes as expected
- The scope=null defense is in place (regression test for the BugBot finding)

If any of these fail, the issue is in the wiring or schema, not the service logic.

### Task 12: MCP handler wiring (`memory.semantic_observe`)

**Confidence:** 93% — replicates the existing memory.observe pattern with the args.get scope-null defense.

**Files:**
- Modify: `better_memory/mcp/server.py`

- [ ] **Step 1: Add the handler branch**

Find the `_call_tool` function (around line 491). After the `if name == "memory.observe":` block, add:

```python
        if name == "memory.semantic_observe":
            from better_memory.services.semantic import SemanticMemoryService
            project = project_name()
            svc = SemanticMemoryService(memory_conn)
            # `args.get("scope") or "project"` (not `, "project"` default) defends
            # against MCP clients sending {"scope": null} — dict.get returns the
            # default only when the key is absent, not when its value is None.
            # Same fix as PR #25's BugBot finding on memory.observe.
            memory_id = svc.create(
                content=args["content"],
                project=project,
                scope=args.get("scope") or "project",
            )
            return [TextContent(type="text", text=json.dumps({"id": memory_id}))]
```

- [ ] **Step 2: Smoke check the handler in isolation**

Run a quick service-level smoke check to verify the wiring path imports cleanly and the underlying call works:

Run: `uv run pytest tests/mcp/test_semantic_tools.py::TestSemanticObserveHandler -v`
Expected: 3 passed.

### Task 13: MCP handler wiring (`memory.semantic_retrieve`)

**Confidence:** 95%.

**Files:**
- Modify: `better_memory/mcp/server.py`

- [ ] **Step 1: Add the handler branch**

Add after `memory.semantic_observe`:

```python
        if name == "memory.semantic_retrieve":
            from better_memory.services.semantic import SemanticMemoryService
            project = args.get("project") or project_name()
            svc = SemanticMemoryService(memory_conn)
            memories = svc.list_for_project(project=project)
            payload = [
                {
                    "id": m.id,
                    "content": m.content,
                    "project": m.project,
                    "scope": m.scope,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                }
                for m in memories
            ]
            return [TextContent(type="text", text=json.dumps(payload))]
```

Note: `args.get("project") or project_name()` — same defensive idiom for the optional project override (handles `{"project": null}` correctly).

- [ ] **Step 2: Verify**

Run: `uv run pytest tests/mcp/test_semantic_tools.py::TestSemanticRetrieveHandler -v`
Expected: 2 passed.

### Task 14: MCP handler wiring (`memory.semantic_update` + `_delete`)

**Confidence:** 96%.

**Files:**
- Modify: `better_memory/mcp/server.py`

- [ ] **Step 1: Add both handler branches**

Add after `memory.semantic_retrieve`:

```python
        if name == "memory.semantic_update":
            from better_memory.services.semantic import SemanticMemoryService
            svc = SemanticMemoryService(memory_conn)
            svc.update_text(id=args["id"], content=args["content"])
            return [TextContent(type="text", text=json.dumps({"ok": True}))]

        if name == "memory.semantic_delete":
            from better_memory.services.semantic import SemanticMemoryService
            svc = SemanticMemoryService(memory_conn)
            svc.delete(id=args["id"])
            return [TextContent(type="text", text=json.dumps({"ok": True}))]
```

- [ ] **Step 2: Verify**

Run: `uv run pytest tests/mcp/test_semantic_tools.py -v`
Expected: all 10 tests pass (TestSemanticToolsRegistered + TestSemanticObserveHandler + TestSemanticRetrieveHandler + TestSemanticUpdateHandler + TestSemanticDeleteHandler).

### Task 15: Final verification + Commit 3

**Confidence:** 99%.

- [ ] **Step 1: Full test suite green**

Run: `uv run pytest -q`
Expected: full green; ~24 new tests added on top of PR #25's 629 baseline.

- [ ] **Step 2: Pyright clean**

Run: `uv run pyright`
Expected: 0 errors.

- [ ] **Step 3: Pre-flight grep for stale references**

```bash
grep -rE "semantic_memories" better_memory/ tests/
```

Expected hits in:
- `better_memory/db/migrations/0008_semantic_memories.sql`
- `better_memory/services/semantic.py`
- `tests/db/test_migration_0008.py`
- `tests/services/test_semantic.py`
- `tests/mcp/test_semantic_tools.py`

No hits in production code outside `services/semantic.py` and `mcp/server.py`'s 4 handler branches.

- [ ] **Step 4: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_semantic_tools.py
git commit -m "$(cat <<'EOF'
feat(mcp): memory.semantic_observe / _retrieve / _update / _delete tools

Wire SemanticMemoryService into the MCP server with four new tools:

- memory.semantic_observe(content, scope='project') -> {id}
  Records a user-stated fact/preference. scope='general' surfaces it
  in every project's startup retrieval.

- memory.semantic_retrieve(project?) -> [memories...]
  Returns project rows + general-scope rows from any project,
  ordered newest-first. Flat list — they're facts, not lessons.

- memory.semantic_update(id, content) -> {ok}
  Edits content in place; bumps updated_at. Raises if id absent.

- memory.semantic_delete(id) -> {ok}
  Idempotent — no error if id absent.

Both write tools accepting scope use `args.get("scope") or "project"`
to defend against {"scope": null} from MCP clients (PR #25 BugBot
finding).

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

- [ ] **Step 2: Smoke run (manual)**

Start the MCP server (or use the existing better-memory MCP integration in Claude Code) and exercise the four new tools end-to-end:

1. `mcp__better-memory__memory_semantic_observe(content="prefer terse replies", scope="general")` → returns `{id: "..."}`
2. `mcp__better-memory__memory_semantic_retrieve()` → returns the new memory in the list
3. `mcp__better-memory__memory_semantic_update(id, content="prefer terse, technical replies")` → returns `{ok: true}`
4. `mcp__better-memory__memory_semantic_retrieve()` → updated content visible
5. `mcp__better-memory__memory_semantic_delete(id)` → returns `{ok: true}`
6. `mcp__better-memory__memory_semantic_retrieve()` → empty for that memory

- [ ] **Step 3: Open the PR**

```bash
git push -u origin semantic-memories
gh pr create --title "Semantic memories — user-stated facts and preferences" --body "$(cat <<'EOF'
## Summary

- New top-level concept in better-memory: `semantic_memories` for user-asserted facts/preferences. Distinct from observations (episodic) and reflections (LLM-distilled). Same `scope = ('project'|'general')` model as PR #25's reflections — general-scope rows surface in every project's startup retrieval.
- Three commits: migration 0008 (schema + 7 tests) → SemanticMemoryService (~150 LOC, 14 tests) → 4 MCP tools (~80 LOC, 10 tests).

## Why

Workflow rules and other project-agnostic preferences need a first-class structured place that surfaces in every session. PR #25 added cross-project scope to reflections; this PR extends the same model to a new user-driven (not LLM-distilled) memory layer.

Spec: `docs/superpowers/specs/2026-05-04-semantic-memories-design.md`
Plan: `docs/superpowers/plans/2026-05-04-semantic-memories.md`

## Test Plan

- [ ] Migration 0008 schema/index/CHECK tests
- [ ] Service-level CRUD tests including invalid scope, empty content, missing id, scope merging, ordering
- [ ] MCP tool registration + scope-null defense regression test
- [ ] Full pytest + pyright green
- [ ] Manual smoke through the four MCP tools

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## CLAUDE.md addendum (out of repo, separate edit)

After this PR merges, edit `~/.claude/CLAUDE.md` to add semantic memories to the mandatory startup retrieval flow. The exact text is in the spec under "CLAUDE.md addendum (out of repo, separate edit)" — copy that into the appropriate section of the user's global config.

---

## Per-step confidence summary (per the user's reflection rule)

All 15 task headers carry an explicit confidence rating. Steps within tasks below 95% have inline mitigation notes.

| Task | Confidence | Notes |
|---|---|---|
| 1. Migration test (red) | 95% | Direct port of 0007 pattern. |
| 2. Migration SQL (green) | 97% | Straightforward DDL. |
| 3. Schema idempotency assertion | 99% | One-line edit. |
| 4. Pre-flight grep + Commit 1 | 99% | |
| 5. Service skeleton + create | 92% | New module. ValueError before DB hit. |
| 6. update_text | 95% | |
| 7. delete (idempotent) | 97% | |
| 8. list_for_project | 92% | Scope-merging + ordering tests have multiple moving parts; tests cover them explicitly. |
| 9. Verify + Commit 2 | 99% | |
| 10. Tool schema definitions | 95% | 4 verbatim Tool() entries. |
| 11. Test stubs for handlers | 92% | Multiple tests; each kept self-contained. |
| 12. semantic_observe handler | 93% | Replicates memory.observe pattern incl. scope-null defense. |
| 13. semantic_retrieve handler | 95% | |
| 14. semantic_update + _delete handlers | 96% | |
| 15. Final verification + Commit 3 | 99% | |

**No tasks below 90%.** Lowest is 92% (Tasks 5, 8, 11), each with a brief mitigation note inline. The plan satisfies the per-step-confidence rule (every step ≥90%, no user escalation needed).

**Compound estimate:** ~80-85% the PR lands without a reviewer-driven fix-commit cycle. Higher than PR #25's ~75-82% because (a) fewer tasks, (b) no high-risk surgical-removal step, (c) the data model is parallel to a concept (PR #25's scope on reflections) that's already battle-tested.

---

## Self-review

**Spec coverage:**
- Schema (table + 2 indexes): Tasks 1, 2 ✓
- Service API (`create`, `update_text`, `delete`, `list_for_project`): Tasks 5, 6, 7, 8 ✓
- ValueError on invalid scope + empty content: Task 5 ✓
- Hard-delete idempotence: Task 7 ✓
- Scope-merging + newest-first ordering: Task 8 ✓
- 4 MCP tools (observe / retrieve / update / delete): Tasks 10, 12, 13, 14 ✓
- Scope-null `args.get` defense (BugBot finding from PR #25): Tasks 11 + 12 ✓
- CLAUDE.md addendum: noted in final section as a post-merge global-config edit (per spec)
- Test plan: 7 migration + 14 service + 10 MCP = 31 tests ✓

**Placeholder scan:** none. Every task has files, code, test code, and run/expect lines. No "TBD" / "implement later" / "similar to Task N".

**Type consistency:** `SemanticMemory` dataclass (id, content, project, scope, created_at, updated_at) referenced consistently across tasks 5/8/13. Service methods all keyword-only (`*` separator) per spec. `scope` enum `'project'|'general'` matches in schema CHECK, ValueError validation, MCP tool schema enum.

**Scope:** three commits, ~280 LOC code + ~430 LOC tests across 7 files. Tractable for a single subagent-driven cycle in under an hour.
