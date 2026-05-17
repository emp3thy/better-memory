# Overlooked Memory-Rating Class Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 5th closed-loop memory-rating class, `overlooked`, for a memory that was relevant but not applied until the user intervened.

**Architecture:** A new migration widens the `session_memory_exposure.classification` CHECK and adds `times_overlooked`/`last_overlooked_at` counters to `reflections` and `semantic_memories`. `MemoryRatingService` learns the class; retrieval ranking weights it via `useful_count + 3 × times_overlooked`; the MCP tools, session-close directive, rating skill, and management UI surface it.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), Flask + Jinja + htmx UI, pytest. Spec: `docs/superpowers/specs/2026-05-17-overlooked-memory-rating-class-design.md`.

**Branch:** `feat/overlooked-rating-class` (already created; the design spec is committed there).

---

## Task 1: Migration 0010 — schema

**Files:**
- Create: `better_memory/db/migrations/0010_overlooked_rating.sql`
- Test: `tests/db/test_migration_0010.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_migration_0010.py`:

```python
"""Migration 0010 — overlooked rating class."""
from __future__ import annotations

import sqlite3
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


class TestExposureClassificationCheck:
    def test_overlooked_classification_accepted(self, conn):
        conn.execute(
            """INSERT INTO session_memory_exposure
               (session_id, memory_kind, memory_id, exposed_at, source,
                rated_at, classification)
               VALUES ('S1', 'reflection', 'r1', '2026-05-17T10:00:00+00:00',
                       'bootstrap', '2026-05-17T11:00:00+00:00', 'overlooked')"""
        )
        row = conn.execute(
            "SELECT classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["classification"] == "overlooked"

    def test_unknown_classification_still_rejected(self, conn):
        # Guard: the CHECK is widened, not dropped.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO session_memory_exposure
                   (session_id, memory_kind, memory_id, exposed_at, source,
                    classification)
                   VALUES ('S1', 'reflection', 'r1',
                           '2026-05-17T10:00:00+00:00', 'bootstrap', 'bogus')"""
            )

    def test_exposure_table_columns_preserved(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall()
        }
        assert cols == {
            "session_id", "memory_kind", "memory_id",
            "exposed_at", "source", "rated_at", "classification",
        }

    def test_primary_key_preserved(self, conn):
        pk_cols = [
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall() if r["pk"] > 0
        ]
        assert pk_cols == [
            "session_id", "memory_kind", "memory_id", "exposed_at",
        ]

    def test_indexes_recreated(self, conn):
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='session_memory_exposure'"
            ).fetchall()
        }
        assert "idx_sme_session_unrated" in names
        assert "idx_sme_memory" in names


class TestOverlookedCounterColumns:
    def test_reflections_have_overlooked_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(reflections)"
            ).fetchall()
        }
        assert {"times_overlooked", "last_overlooked_at"} <= cols

    def test_semantic_memories_have_overlooked_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(semantic_memories)"
            ).fetchall()
        }
        assert {"times_overlooked", "last_overlooked_at"} <= cols

    def test_times_overlooked_defaults_to_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        row = conn.execute(
            "SELECT times_overlooked, last_overlooked_at "
            "FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["times_overlooked"] == 0
        assert row["last_overlooked_at"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/db/test_migration_0010.py -v`
Expected: FAIL — `test_overlooked_classification_accepted` raises `IntegrityError` (the 0009 CHECK rejects `overlooked`), and the counter-column tests fail (`times_overlooked` does not exist). `test_unknown_classification_still_rejected` and the structural tests pass.

- [ ] **Step 3: Create the migration**

Create `better_memory/db/migrations/0010_overlooked_rating.sql`:

```sql
-- Migration 0010: overlooked rating class.
--
-- Adds a 5th closed-loop rating class, `overlooked`: a memory that was
-- relevant and should have been applied, but was not — until the user
-- explicitly intervened. See issue #60 and
-- docs/superpowers/specs/2026-05-17-overlooked-memory-rating-class-design.md.

-- Widen the classification CHECK to admit 'overlooked'. SQLite cannot
-- ALTER a CHECK constraint, so session_memory_exposure is recreated.
-- No table holds a foreign key into session_memory_exposure, so no
-- foreign_keys pragma toggling is required.

CREATE TABLE session_memory_exposure_new (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve')),
    rated_at       TEXT,
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled', 'overlooked')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

INSERT INTO session_memory_exposure_new
    (session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification)
SELECT
    session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification
FROM session_memory_exposure;

DROP TABLE session_memory_exposure;
ALTER TABLE session_memory_exposure_new RENAME TO session_memory_exposure;

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);

-- Per-memory overlooked counters, parallel to useful_count / times_misled
-- added in migration 0009.

ALTER TABLE reflections       ADD COLUMN times_overlooked   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections       ADD COLUMN last_overlooked_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_overlooked   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_overlooked_at TEXT;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/db/test_migration_0010.py -v`
Expected: PASS — all 9 tests green.

- [ ] **Step 5: Run the existing migration suite for regressions**

Run: `pytest tests/db/ -v`
Expected: PASS — 0006/0007/0008/0009 tests and `test_schema.py` still green.

- [ ] **Step 6: Commit**

```bash
git add better_memory/db/migrations/0010_overlooked_rating.sql tests/db/test_migration_0010.py
git commit -m "feat(db): migration 0010 — overlooked rating class schema"
```

---

## Task 2: Rating service — `overlooked` class

**Files:**
- Modify: `better_memory/services/memory_rating.py`
- Test: `tests/services/test_memory_rating.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/test_memory_rating.py`. Append a new test class and one method inside `TestApplySessionRatings`:

```python
class TestCreditOneOverlooked:
    def test_overlooked_bumps_times_overlooked_on_reflection(
        self, conn, fixed_clock,
    ):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1",
            classification="overlooked",
        )
        assert result == {"applied": "overlooked", "skipped": None}
        row = conn.execute(
            "SELECT useful_count, times_misled, times_overlooked, "
            "last_overlooked_at FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["times_overlooked"] == 1
        assert row["last_overlooked_at"] == "2026-05-11T12:00:00+00:00"
        assert row["useful_count"] == 0
        assert row["times_misled"] == 0

    def test_overlooked_bumps_times_overlooked_on_semantic(
        self, conn, fixed_clock,
    ):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_semantic(conn, "s1")
        _seed_exposure(conn, "S1", "semantic", "s1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="semantic", id="s1",
            classification="overlooked",
        )
        row = conn.execute(
            "SELECT times_overlooked FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert row["times_overlooked"] == 1

    def test_overlooked_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1",
            classification="overlooked",
        )
        row = conn.execute(
            "SELECT classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["classification"] == "overlooked"
```

Add this method inside the existing `TestApplySessionRatings` class:

```python
    def test_overlooked_counted_in_applied(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "overlooked"},
            ],
        )
        assert result["applied"]["overlooked"] == 1
        row = conn.execute(
            "SELECT times_overlooked FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["times_overlooked"] == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/services/test_memory_rating.py::TestCreditOneOverlooked tests/services/test_memory_rating.py::TestApplySessionRatings::test_overlooked_counted_in_applied -v`
Expected: FAIL — `credit_one` raises `ValueError: Invalid classification: 'overlooked'`; `apply_session_ratings` raises `ValueError` for the invalid class.

- [ ] **Step 3: Implement the service changes**

In `better_memory/services/memory_rating.py`:

(a) Widen the type aliases:

```python
Kind = Literal["reflection", "semantic"]
Classification = Literal["cited", "shaped", "ignored", "misled", "overlooked"]
CreditClassification = Literal["cited", "shaped", "misled", "overlooked"]
```

(b) Add `overlooked` to `AppliedCounts`:

```python
class AppliedCounts(TypedDict):
    cited: int
    shaped: int
    ignored: int
    misled: int
    overlooked: int
```

(c) Widen the class sets and add the ranking-weight constant:

```python
_VALID_KINDS: set[str] = {"reflection", "semantic"}
# Used by apply_session_ratings (Task 3). credit_one accepts only the
# subset _CREDIT_CLASSES below.
_VALID_CLASSES: set[str] = {"cited", "shaped", "ignored", "misled", "overlooked"}
_CREDIT_CLASSES: set[str] = {"cited", "shaped", "misled", "overlooked"}

# Retrieval-ranking weight for the `overlooked` class. One `overlooked`
# rating contributes this many points to a memory's rank score — the same
# unit as one `useful_count`. Per issue #60, an overlooked memory should
# rank harder than a cited one. Imported by reflection.py and semantic.py.
OVERLOOKED_RANKING_WEIGHT = 3
```

(d) In `credit_one`'s docstring, update the validation and apply-outcome lines:

```
        - classification must be 'cited', 'shaped', 'misled', or
          'overlooked' (NOT 'ignored').
```

```
        Apply outcomes:
        - 'cited' / 'shaped' → useful_count++, last_useful_at = now.
        - 'misled'           → times_misled++, last_misled_at = now.
        - 'overlooked'       → times_overlooked++, last_overlooked_at = now.
```

(e) In `_apply_one`, add the `overlooked` branch immediately after the `misled` branch (before the `# 'ignored' is a no-op` comment):

```python
        elif classification == "overlooked":
            self._conn.execute(
                f"UPDATE {table} "
                f"SET times_overlooked = times_overlooked + 1, "
                f"last_overlooked_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
```

(f) In `apply_session_ratings`, add `overlooked` to the `applied` initialiser:

```python
        applied: AppliedCounts = {
            "cited": 0, "shaped": 0, "ignored": 0, "misled": 0,
            "overlooked": 0,
        }
```

- [ ] **Step 4: Fix the two existing assertions broken by the new `AppliedCounts` key**

In `tests/services/test_memory_rating.py`, `test_each_class_produces_right_updates` — change the `applied` assertion to:

```python
        assert result["applied"] == {
            "cited": 1, "shaped": 1, "ignored": 1, "misled": 1,
            "overlooked": 0,
        }
```

In `test_all_four_skip_counts_exercised` — change the `applied` assertion to:

```python
        assert result["applied"] == {
            "cited": 0, "shaped": 0, "ignored": 0, "misled": 0,
            "overlooked": 0,
        }
```

- [ ] **Step 5: Run the full rating-service test file**

Run: `pytest tests/services/test_memory_rating.py -v`
Expected: PASS — the four new tests pass, and the two amended assertions pass.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/memory_rating.py tests/services/test_memory_rating.py
git commit -m "feat(rating): MemoryRatingService accepts the overlooked class"
```

---

## Task 3: MCP tool schemas — `overlooked` in the enums

**Files:**
- Modify: `better_memory/mcp/server.py`
- Test: `tests/mcp/test_rating_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_rating_tools.py`:

```python
class TestOverlookedClassInSchemas:
    def test_credit_tool_class_enum_includes_overlooked(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions() if t.name == "memory.credit"
        )
        enum = tool.inputSchema["properties"]["class"]["enum"]
        assert "overlooked" in enum

    def test_apply_ratings_tool_class_enum_includes_overlooked(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.apply_session_ratings"
        )
        enum = (
            tool.inputSchema["properties"]["ratings"]
            ["items"]["properties"]["class"]["enum"]
        )
        assert "overlooked" in enum

    def test_credit_dispatch_accepts_overlooked(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        result = run_async(srv_mod._dispatch_for_tests(
            "memory.credit",
            {"kind": "reflection", "id": "r1", "class": "overlooked"},
        ))
        payload = json.loads(result[0].text)
        assert payload == {"applied": "overlooked", "skipped": None}
```

- [ ] **Step 2: Run the schema tests to verify they fail**

Run: `pytest tests/mcp/test_rating_tools.py::TestOverlookedClassInSchemas -v`
Expected: FAIL — `test_credit_tool_class_enum_includes_overlooked` and `test_apply_ratings_tool_class_enum_includes_overlooked` fail (`overlooked` not in the enum). `test_credit_dispatch_accepts_overlooked` already passes (Task 2 made the service accept it) — it is a regression guard.

- [ ] **Step 3: Update the tool schemas**

In `better_memory/mcp/server.py`:

In the `memory.apply_session_ratings` tool definition, the `class` enum:

```python
                                "class": {
                                    "enum": [
                                        "cited", "shaped", "ignored",
                                        "misled", "overlooked",
                                    ]
                                },
```

In the `memory.credit` tool definition, the description's final line:

```python
                "Resolved server-side from CLAUDE_SESSION_ID. "
                "class must be 'cited', 'shaped', 'misled', or "
                "'overlooked' — NOT 'ignored'. Use 'overlooked' when the "
                "user pointed you back to a memory you already had but "
                "had not applied."
```

And the `memory.credit` `class` enum:

```python
                "class": {
                    "enum": ["cited", "shaped", "misled", "overlooked"]
                },
```

- [ ] **Step 4: Run the test file to verify it passes**

Run: `pytest tests/mcp/test_rating_tools.py -v`
Expected: PASS — all tests including the existing ones.

- [ ] **Step 5: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_rating_tools.py
git commit -m "feat(mcp): allow overlooked in credit + apply_session_ratings"
```

---

## Task 4: Retrieval ranking + semantic read-model

**Files:**
- Modify: `better_memory/services/reflection.py` (`retrieve_reflections`)
- Modify: `better_memory/services/semantic.py` (`SemanticMemory`, `list_for_project`)
- Test: `tests/services/test_useful_count_ranking.py`

- [ ] **Step 1: Write the failing tests**

In `tests/services/test_useful_count_ranking.py`, replace the `_seed_reflection` helper with this extended version (adds the `times_overlooked` kwarg + column):

```python
def _seed_reflection(conn, rid, *, useful_count=0, confidence=0.5,
                     polarity="do", updated_at="2026-01-01",
                     times_overlooked=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count,
            times_overlooked)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful_count,
         times_overlooked),
    )
    conn.commit()
```

And replace the `_seed_semantic` helper with:

```python
def _seed_semantic(conn, sid, *, useful_count=0, created_at="2026-01-01",
                   times_overlooked=0):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at,
            useful_count, times_overlooked)
           VALUES (?, 'fact', 'p', 'project', ?, ?, ?, ?)""",
        (sid, created_at, created_at, useful_count, times_overlooked),
    )
    conn.commit()
```

Append a new test class:

```python
class TestOverlookedRanking:
    def test_overlooked_outranks_lower_useful_count(self, conn):
        """One overlooked (weight 3) beats useful_count=2 (score 2 < 3)."""
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-useful-2", useful_count=2,
                         times_overlooked=0)
        _seed_reflection(conn, "r-overlooked-1", useful_count=0,
                         times_overlooked=1)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids.index("r-overlooked-1") < ids.index("r-useful-2")

    def test_high_useful_count_still_beats_one_overlooked(self, conn):
        """useful_count=4 (score 4) beats one overlooked (score 3)."""
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-useful-4", useful_count=4,
                         times_overlooked=0)
        _seed_reflection(conn, "r-overlooked-1", useful_count=0,
                         times_overlooked=1)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids.index("r-useful-4") < ids.index("r-overlooked-1")

    def test_semantic_overlooked_outranks_lower_useful_count(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-useful-2", useful_count=2,
                       times_overlooked=0)
        _seed_semantic(conn, "s-overlooked-1", useful_count=0,
                       times_overlooked=1)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids.index("s-overlooked-1") < ids.index("s-useful-2")

    def test_semantic_high_useful_count_still_beats_one_overlooked(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-useful-4", useful_count=4,
                       times_overlooked=0)
        _seed_semantic(conn, "s-overlooked-1", useful_count=0,
                       times_overlooked=1)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids.index("s-useful-4") < ids.index("s-overlooked-1")

    def test_semantic_read_model_carries_times_overlooked(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s1", times_overlooked=5)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        assert results[0].times_overlooked == 5
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/services/test_useful_count_ranking.py::TestOverlookedRanking -v`
Expected: FAIL — `test_overlooked_outranks_lower_useful_count` and the semantic equivalent fail (ranking still keys on `useful_count` only); `test_semantic_read_model_carries_times_overlooked` fails with `AttributeError` (`SemanticMemory` has no `times_overlooked`). The two `high_useful_count` tests pass already.

- [ ] **Step 3: Update reflection ranking**

In `better_memory/services/reflection.py`, add this import alongside the other `from better_memory...` imports near the top of the file:

```python
from better_memory.services.memory_rating import OVERLOOKED_RANKING_WEIGHT
```

In `retrieve_reflections`, change the SELECT block. Replace:

```python
            where = " AND ".join(clauses)
            _diag.step(fn, "executing_select")
            rows = self._conn.execute(
                f"""
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count, useful_count
                FROM reflections
                WHERE {where}
                ORDER BY useful_count DESC, confidence DESC, updated_at DESC
                """,
                params,
            ).fetchall()
```

with:

```python
            where = " AND ".join(clauses)
            params.append(OVERLOOKED_RANKING_WEIGHT)
            _diag.step(fn, "executing_select")
            rows = self._conn.execute(
                f"""
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count, useful_count
                FROM reflections
                WHERE {where}
                ORDER BY (useful_count + ? * times_overlooked) DESC,
                         confidence DESC, updated_at DESC
                """,
                params,
            ).fetchall()
```

The `?` in `ORDER BY` binds to the appended weight (the last positional parameter).

- [ ] **Step 4: Update the semantic read-model and ranking**

In `better_memory/services/semantic.py`, add the import alongside the existing imports:

```python
from better_memory.services.memory_rating import OVERLOOKED_RANKING_WEIGHT
```

Extend the `SemanticMemory` dataclass — add two fields after `last_misled_at`:

```python
@dataclass(frozen=True)
class SemanticMemory:
    """Read model returned by retrieve."""

    id: str
    content: str
    project: str
    scope: str            # 'project' | 'general'
    created_at: str
    updated_at: str
    useful_count: int = 0
    last_useful_at: str | None = None
    times_misled: int = 0
    last_misled_at: str | None = None
    times_overlooked: int = 0
    last_overlooked_at: str | None = None
```

In `list_for_project`, replace the `sql` / `rows` block:

```python
        sql = (
            "SELECT id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at "
            "FROM semantic_memories "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY useful_count DESC, created_at DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
```

with:

```python
        sql = (
            "SELECT id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at, "
            "times_overlooked, last_overlooked_at "
            "FROM semantic_memories "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY (useful_count + ? * times_overlooked) DESC, "
            "created_at DESC"
        )
        params.append(OVERLOOKED_RANKING_WEIGHT)
        rows = self._conn.execute(sql, params).fetchall()
```

And in the `SemanticMemory(...)` constructor inside the list comprehension, add the two fields:

```python
            SemanticMemory(
                id=r["id"], content=r["content"], project=r["project"],
                scope=r["scope"],
                created_at=r["created_at"], updated_at=r["updated_at"],
                useful_count=r["useful_count"] or 0,
                last_useful_at=r["last_useful_at"],
                times_misled=r["times_misled"] or 0,
                last_misled_at=r["last_misled_at"],
                times_overlooked=r["times_overlooked"] or 0,
                last_overlooked_at=r["last_overlooked_at"],
            )
```

- [ ] **Step 5: Run the ranking test file to verify it passes**

Run: `pytest tests/services/test_useful_count_ranking.py -v`
Expected: PASS — `TestOverlookedRanking` green, and the existing `TestReflectionRanking` / `TestSemanticRanking` tests still green.

- [ ] **Step 6: Run the reflection + semantic service suites for regressions**

Run: `pytest tests/services/test_reflection.py tests/services/test_semantic.py tests/services/test_exposure_tracking.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/services/reflection.py better_memory/services/semantic.py tests/services/test_useful_count_ranking.py
git commit -m "feat(retrieval): weight times_overlooked into ranking (useful + 3x overlooked)"
```

---

## Task 5: Session-close directive + rating skill

**Files:**
- Modify: `better_memory/hooks/session_close.py`
- Modify: `.claude/skills/rate-session-memories/SKILL.md`
- Test: `tests/hooks/test_session_close_rating_directive.py`

- [ ] **Step 1: Write the failing test**

Add to the `TestRatingDirectiveEmission` class in `tests/hooks/test_session_close_rating_directive.py`:

```python
    def test_directive_lists_overlooked_class(self, tmp_path, tmp_memory_db):
        _seed_unrated_exposure(tmp_memory_db, "S1")
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "overlooked" in directive
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/hooks/test_session_close_rating_directive.py::TestRatingDirectiveEmission::test_directive_lists_overlooked_class -v`
Expected: FAIL — the directive does not contain `overlooked`.

- [ ] **Step 3: Update the directive text**

In `better_memory/hooks/session_close.py`, in the `directive` string, replace:

```python
            + "\n\nFor each id, classify as one of:\n"
            "  cited / shaped / ignored / misled (default: ignored)\n\n"
```

with:

```python
            + "\n\nFor each id, classify as one of:\n"
            "  cited / shaped / ignored / misled / overlooked "
            "(default: ignored)\n\n"
```

- [ ] **Step 4: Run the directive test file to verify it passes**

Run: `pytest tests/hooks/test_session_close_rating_directive.py -v`
Expected: PASS — the new test and all existing directive tests green. (The 8 KB cap test still passes — the added word is well under the cap.)

- [ ] **Step 5: Update the rating skill**

In `.claude/skills/rate-session-memories/SKILL.md`, in STEP 2, replace the class list block:

```markdown
- **cited** — you quoted or directly referenced the memory in a reply.
- **shaped** — the memory guided a decision but wasn't cited verbatim.
- **ignored** — you saw it but it didn't affect this session. (Default.)
- **misled** — it caused a wrong direction or wasted effort.
```

with:

```markdown
- **cited** — you quoted or directly referenced the memory in a reply.
- **shaped** — the memory guided a decision but wasn't cited verbatim.
- **ignored** — you saw it but it didn't affect this session. (Default.)
- **misled** — it caused a wrong direction or wasted effort.
- **overlooked** — the memory was relevant and you should have applied
  it, but you didn't, until the user explicitly pointed you back to it.
```

And in the `Rules:` block, replace:

```markdown
- If genuinely uncertain between two classes, prefer the lower one
  (shaped > cited, ignored > shaped). `misled` is never a fallback.
- Default is `ignored`. "Shaped" requires evidence you can point to.
```

with:

```markdown
- If genuinely uncertain between two classes, prefer the lower one
  (shaped > cited, ignored > shaped). `misled` is never a fallback.
- `overlooked` is never a fallback. Use it ONLY when the user explicitly
  pointed you back to a memory you already had and had not applied —
  that user intervention is the observable anchor. Test for it first,
  separately from the cited/shaped/ignored axis. No anchor event → not
  `overlooked`.
- Default is `ignored`. "Shaped" requires evidence you can point to.
```

(No automated test — `SKILL.md` is markdown guidance. Verify by reading the edited file.)

- [ ] **Step 6: Commit**

```bash
git add better_memory/hooks/session_close.py .claude/skills/rate-session-memories/SKILL.md tests/hooks/test_session_close_rating_directive.py
git commit -m "feat(rating): list overlooked in session-close directive + skill"
```

---

## Task 6: UI read-models — reflection rows + drawer

**Files:**
- Modify: `better_memory/ui/queries.py`
- Test: `tests/ui/test_queries_reflections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_queries_reflections.py`:

```python
class TestOverlookedInReadModel:
    def test_reflection_list_includes_times_overlooked(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, times_overlooked)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 4)"""
        )
        conn.commit()
        rows = reflection_list_for_ui(conn, project="p")
        assert any(r.times_overlooked == 4 for r in rows)

    def test_reflection_list_times_overlooked_defaults_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        [row] = reflection_list_for_ui(conn, project="p")
        assert row.times_overlooked == 0

    def test_reflection_detail_includes_overlooked_fields(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at,
                times_overlooked, last_overlooked_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01',
                       3, '2026-05-17T11:00:00+00:00')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.times_overlooked == 3
        assert detail.last_overlooked_at == "2026-05-17T11:00:00+00:00"

    def test_reflection_detail_overlooked_defaults_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.times_overlooked == 0
        assert detail.last_overlooked_at is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/ui/test_queries_reflections.py::TestOverlookedInReadModel -v`
Expected: FAIL — `AttributeError` on `times_overlooked` / `last_overlooked_at`.

- [ ] **Step 3: Update `queries.py`**

In `better_memory/ui/queries.py`:

(a) `ReflectionListRow` — add a field after `times_misled`:

```python
    useful_count: int = 0
    times_misled: int = 0
    times_overlooked: int = 0
```

(b) `reflection_list_for_ui` — add `times_overlooked` to the SELECT column list:

```python
    sql = (
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, evidence_count, updated_at, "
        "useful_count, times_misled, times_overlooked "
        f"FROM reflections WHERE {where} "
        "ORDER BY confidence DESC, updated_at DESC, rowid DESC "
        "LIMIT ?"
    )
```

and add it to the `ReflectionListRow(...)` constructor:

```python
            useful_count=r["useful_count"],
            times_misled=r["times_misled"],
            times_overlooked=r["times_overlooked"],
```

(c) `ReflectionFull` — add two fields after `last_misled_at`:

```python
    useful_count: int = 0
    last_useful_at: str | None = None
    times_misled: int = 0
    last_misled_at: str | None = None
    times_overlooked: int = 0
    last_overlooked_at: str | None = None
```

(d) `ReflectionDetail` — add two delegating properties after `last_misled_at`:

```python
    @property
    def times_overlooked(self) -> int:
        return self.reflection.times_overlooked

    @property
    def last_overlooked_at(self) -> str | None:
        return self.reflection.last_overlooked_at
```

(e) `reflection_detail` — add `times_overlooked, last_overlooked_at` to the SELECT:

```python
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, scope, "
        "created_at, updated_at, "
        "useful_count, last_useful_at, times_misled, last_misled_at, "
        "times_overlooked, last_overlooked_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
```

and to the `ReflectionFull(...)` constructor:

```python
            useful_count=r_row["useful_count"] or 0,
            last_useful_at=r_row["last_useful_at"],
            times_misled=r_row["times_misled"] or 0,
            last_misled_at=r_row["last_misled_at"],
            times_overlooked=r_row["times_overlooked"] or 0,
            last_overlooked_at=r_row["last_overlooked_at"],
```

- [ ] **Step 4: Run the test file to verify it passes**

Run: `pytest tests/ui/test_queries_reflections.py -v`
Expected: PASS — `TestOverlookedInReadModel` and all existing query tests green.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_reflections.py
git commit -m "feat(ui): reflection read-models carry times_overlooked"
```

---

## Task 7: Inline `overlooked` badge on list rows

**Files:**
- Modify: `better_memory/ui/templates/fragments/_rating_stat.html`
- Modify: `better_memory/ui/templates/fragments/reflection_row.html`
- Modify: `better_memory/ui/templates/fragments/semantic_row.html`
- Modify: `better_memory/ui/static/app.css`
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`

- [ ] **Step 1: Write the failing tests**

In `tests/ui/test_reflections.py`, replace the `_seed_reflection` helper signature and body to add the `times_overlooked` kwarg + column:

```python
def _seed_reflection(
    db_path: Path,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    evidence_count: int = 0,
    scope: str = "project",
    useful_count: int = 0,
    times_misled: int = 0,
    times_overlooked: int = 0,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, useful_count, "
            "times_misled, times_overlooked, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, title or f"title-{rid}", project, tech, phase, polarity,
                use_cases, hints, confidence, status, evidence_count, scope,
                useful_count, times_misled, times_overlooked,
            ),
        )
        conn.commit()
    finally:
        conn.close()
```

Append to the `TestReflectionRowRatingStat` class in `tests/ui/test_reflections.py`:

```python
    def test_row_shows_overlooked_badge_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Zero rated")
        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "overlooked 0" in body

    def test_overlooked_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", title="Overlooked one", times_overlooked=2,
        )
        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "overlooked 2" in body
        assert "rating-overlooked" in body
```

Append to the `TestSemanticRowRatingStat` class in `tests/ui/test_semantic.py`:

```python
    def test_row_shows_overlooked_badge_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "overlooked 0" in body

    def test_overlooked_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, times_overlooked, "
                " created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project', 3,"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "overlooked 3" in body
        assert "rating-overlooked" in body
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/ui/test_reflections.py::TestReflectionRowRatingStat tests/ui/test_semantic.py::TestSemanticRowRatingStat -v`
Expected: FAIL — the rendered rows have no `overlooked` badge.

- [ ] **Step 3: Update the shared partial**

Replace the entire content of `better_memory/ui/templates/fragments/_rating_stat.html` with:

```html
{# Useful / overlooked / misled rating triple for a memory list row.
   Expects `rating_useful`, `rating_overlooked`, `rating_misled` (ints)
   in context — each row supplies them via {% with %}. Each badge is
   classed by its own count: rating-useful / rating-overlooked /
   rating-misled when > 0, else rating-zero (grey, default-value). #}
<span class="rating-stat">
  <span class="rating-badge rating-{{ 'useful' if rating_useful else 'zero' }}">useful {{ rating_useful or 0 }}</span>
  <span class="rating-badge rating-{{ 'overlooked' if rating_overlooked else 'zero' }}">overlooked {{ rating_overlooked or 0 }}</span>
  <span class="rating-badge rating-{{ 'misled' if rating_misled else 'zero' }}">misled {{ rating_misled or 0 }}</span>
</span>
```

- [ ] **Step 4: Update the row templates**

In `better_memory/ui/templates/fragments/reflection_row.html`, replace:

```jinja
      {% with rating_useful = row.useful_count, rating_misled = row.times_misled %}
        {% include "fragments/_rating_stat.html" %}
      {% endwith %}
```

with:

```jinja
      {% with rating_useful = row.useful_count, rating_overlooked = row.times_overlooked, rating_misled = row.times_misled %}
        {% include "fragments/_rating_stat.html" %}
      {% endwith %}
```

In `better_memory/ui/templates/fragments/semantic_row.html`, replace:

```jinja
    {% with rating_useful = row.useful_count, rating_misled = row.times_misled %}
      {% include "fragments/_rating_stat.html" %}
    {% endwith %}
```

with:

```jinja
    {% with rating_useful = row.useful_count, rating_overlooked = row.times_overlooked, rating_misled = row.times_misled %}
      {% include "fragments/_rating_stat.html" %}
    {% endwith %}
```

- [ ] **Step 5: Add the CSS rule**

In `better_memory/ui/static/app.css`, immediately after the `.rating-badge.rating-misled { ... }` block and before the `/* .rating-zero keeps the muted base style ... */` comment, insert:

```css
.rating-badge.rating-overlooked {
  border-color: var(--brut-amber);
  background: var(--brut-paper);
  color: var(--brut-amber-ink);
}
```

This is an amber outline (amber border, paper fill) — the warning family with `misled`, but visually distinct from `misled`'s amber fill.

- [ ] **Step 6: Run the test files to verify they pass**

Run: `pytest tests/ui/test_reflections.py tests/ui/test_semantic.py -v`
Expected: PASS — new badge tests green; existing `useful`/`misled` row + drawer tests still green (the shared partial still renders both).

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/fragments/_rating_stat.html better_memory/ui/templates/fragments/reflection_row.html better_memory/ui/templates/fragments/semantic_row.html better_memory/ui/static/app.css tests/ui/test_reflections.py tests/ui/test_semantic.py
git commit -m "feat(ui): inline overlooked badge on reflection + semantic rows"
```

---

## Task 8: `Overlooked` line in the drawers

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html`
- Modify: `better_memory/ui/templates/fragments/semantic_drawer.html`
- Modify: `better_memory/ui/app.py` (`semantic_drawer` route)
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`

- [ ] **Step 1: Write the failing tests**

In `tests/ui/test_reflections.py`, append a test next to the existing drawer-Misled test (the class containing `test "<dt>Misled</dt>" in body`):

```python
class TestReflectionDrawerOverlookedAlwaysShown:
    def test_drawer_shows_overlooked_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="confirmed")
        body = client.get("/reflections/r-1/drawer").get_data(as_text=True)
        assert "<dt>Overlooked</dt>" in body

    def test_drawer_shows_overlooked_count_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import re
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="confirmed", times_overlooked=4,
        )
        body = client.get("/reflections/r-1/drawer").get_data(as_text=True)
        assert "<dt>Overlooked</dt>" in body
        # Anchor on the Overlooked <dd> so an incidental "4" elsewhere
        # (dates, confidence) cannot satisfy the assertion.
        m = re.search(r"<dt>Overlooked</dt>\s*<dd>\s*(\d+)", body)
        assert m is not None and m.group(1) == "4"
```

In `tests/ui/test_semantic.py`, append to the `TestSemanticDrawerMisledAlwaysShown` class:

```python
    def test_drawer_shows_overlooked_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "<dt>Overlooked</dt>" in body
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/ui/test_reflections.py::TestReflectionDrawerOverlookedAlwaysShown tests/ui/test_semantic.py::TestSemanticDrawerMisledAlwaysShown::test_drawer_shows_overlooked_line_at_zero -v`
Expected: FAIL — the drawers render no `Overlooked` line.

- [ ] **Step 3: Update the reflection drawer**

In `better_memory/ui/templates/fragments/reflection_drawer.html`, in the `<dl class="drawer-meta">`, insert an `Overlooked` row between the `Useful` and `Misled` rows:

```jinja
    <dt>Useful</dt>
    <dd>{{ detail.reflection.useful_count }}{% if detail.reflection.last_useful_at %} (last: {{ detail.reflection.last_useful_at }}){% endif %}</dd>
    <dt>Overlooked</dt>
    <dd>{{ detail.reflection.times_overlooked }}{% if detail.reflection.last_overlooked_at %} (last: {{ detail.reflection.last_overlooked_at }}){% endif %}</dd>
    <dt>Misled</dt>
    <dd class="text-danger">{{ detail.reflection.times_misled }}{% if detail.reflection.last_misled_at %} (last: {{ detail.reflection.last_misled_at }}){% endif %}</dd>
```

- [ ] **Step 4: Update the semantic drawer route to carry the new fields**

In `better_memory/ui/app.py`, in the `semantic_drawer` route, replace the SELECT + `memory` dict:

```python
        row = conn.execute(
            "SELECT id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at, "
            "times_overlooked, last_overlooked_at "
            "FROM semantic_memories WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            abort(404)
        memory = {
            "id": row["id"], "content": row["content"],
            "project": row["project"], "scope": row["scope"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "useful_count": row["useful_count"] or 0,
            "last_useful_at": row["last_useful_at"],
            "times_misled": row["times_misled"] or 0,
            "last_misled_at": row["last_misled_at"],
            "times_overlooked": row["times_overlooked"] or 0,
            "last_overlooked_at": row["last_overlooked_at"],
        }
```

- [ ] **Step 5: Update the semantic drawer template**

In `better_memory/ui/templates/fragments/semantic_drawer.html`, in the `<dl class="drawer-meta">`, insert an `Overlooked` row between `Useful` and `Misled`:

```jinja
      <dt>Useful</dt>
      <dd>{{ memory.useful_count }}{% if memory.last_useful_at %} (last: {{ memory.last_useful_at }}){% endif %}</dd>
      <dt>Overlooked</dt>
      <dd>{{ memory.times_overlooked }}{% if memory.last_overlooked_at %} (last: {{ memory.last_overlooked_at }}){% endif %}</dd>
      <dt>Misled</dt>
      <dd class="text-danger">{{ memory.times_misled }}{% if memory.last_misled_at %} (last: {{ memory.last_misled_at }}){% endif %}</dd>
```

- [ ] **Step 6: Run the test files to verify they pass**

Run: `pytest tests/ui/test_reflections.py tests/ui/test_semantic.py -v`
Expected: PASS — new drawer tests green; existing drawer tests still green.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/fragments/reflection_drawer.html better_memory/ui/templates/fragments/semantic_drawer.html better_memory/ui/app.py tests/ui/test_reflections.py tests/ui/test_semantic.py
git commit -m "feat(ui): always-shown Overlooked line in reflection + semantic drawers"
```

---

## Task 9: `/diagnostics` overlooked total

**Files:**
- Modify: `better_memory/ui/app.py` (`diagnostics` route)
- Modify: `better_memory/ui/templates/diagnostics.html`
- Test: `tests/ui/test_diagnostics_ratings.py`

- [ ] **Step 1: Write the failing test**

Append to the `TestDiagnosticsPanel` class in `tests/ui/test_diagnostics_ratings.py`:

```python
    def test_overlooked_total_displayed(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        # One reflection overlooked twice, one semantic memory overlooked once.
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, times_overlooked)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 2)"""
        )
        conn.execute(
            """INSERT INTO semantic_memories
               (id, content, project, scope, created_at, updated_at,
                times_overlooked)
               VALUES ('s1', 'fact', 'p', 'project',
                       '2026-01-01', '2026-01-01', 1)"""
        )
        conn.commit()

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app()
        client = app.test_client()
        body = client.get("/diagnostics").data.decode("utf-8")
        assert "overlooked (total)" in body
        assert "memories the agent dropped until the user intervened" in body
        # Anchor on the overlooked-total <dd> so an incidental "3"
        # elsewhere on the page cannot satisfy the assertion.
        import re
        m = re.search(r"overlooked \(total\)</dt>\s*<dd>\s*(\d+)", body)
        assert m is not None and m.group(1) == "3"  # 2 + 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/ui/test_diagnostics_ratings.py::TestDiagnosticsPanel::test_overlooked_total_displayed -v`
Expected: FAIL — `"overlooked (total)"` is not in the page.

- [ ] **Step 3: Update the diagnostics route**

In `better_memory/ui/app.py`, in the `diagnostics` route, after the `rating_diagnostics` dict is built and before `render_template`, add the aggregate query and pass it to the template:

```python
        rating_diagnostics = {r["metric"]: r["value"] for r in diag_rows}
        overlooked_total = conn.execute(
            "SELECT "
            "(SELECT COALESCE(SUM(times_overlooked), 0) FROM reflections) "
            "+ "
            "(SELECT COALESCE(SUM(times_overlooked), 0) FROM semantic_memories) "
            "AS total"
        ).fetchone()["total"]
        return render_template(
            "diagnostics.html",
            active_tab="diagnostics",
            recent_ratings=recent_ratings,
            rating_diagnostics=rating_diagnostics,
            overlooked_total=overlooked_total,
        )
```

- [ ] **Step 4: Update the diagnostics template**

In `better_memory/ui/templates/diagnostics.html`, in the "Rating diagnostics" `<dl>`, add an `overlooked (total)` entry after the `session_id_missing` one:

```jinja
    <dl>
      <dt>session_id_missing</dt>
      <dd>{{ rating_diagnostics.get('session_id_missing', 0) }}
        (calls where CLAUDE_SESSION_ID was unset)</dd>
      <dt>overlooked (total)</dt>
      <dd>{{ overlooked_total }}
        (memories the agent dropped until the user intervened)</dd>
    </dl>
```

- [ ] **Step 5: Run the test file to verify it passes**

Run: `pytest tests/ui/test_diagnostics_ratings.py tests/ui/test_diagnostics.py -v`
Expected: PASS — the new test and existing diagnostics tests green.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py better_memory/ui/templates/diagnostics.html tests/ui/test_diagnostics_ratings.py
git commit -m "feat(ui): /diagnostics shows total overlooked count"
```

---

## Task 10: Documentation sync

**Files:**
- Modify: `website/mcp-tools.md`
- Modify: `website/architecture.md`
- Modify: `README.md`
- Modify: `docs/hooks-setup.md`
- Modify: `docs/superpowers/specs/2026-05-10-memory-rating-design.md`

- [ ] **Step 1: Update `website/mcp-tools.md`**

Find the `memory.credit` and `memory.apply_session_ratings` tool entries. Wherever the rating `class` values are listed (`cited` / `shaped` / `ignored` / `misled`), add `overlooked`. For `memory.credit`, note `overlooked` is accepted (only `ignored` is rejected there).

- [ ] **Step 2: Update `website/architecture.md`**

Find the description of the closed-loop rating model. Update the class list from four to five classes, adding `overlooked` — *relevant, not applied until the user intervened; bumps `times_overlooked` and weights retrieval ranking*.

- [ ] **Step 3: Update `README.md`**

Search `README.md` for the rating classes (`cited`, `shaped`, `ignored`, `misled`). If they are enumerated, add `overlooked`. The MCP tool-count tables are unaffected — no tool is added or removed.

Run: `grep -n "misled\|cited\|rating" README.md`
Apply edits only where the four-class list appears.

- [ ] **Step 4: Update `docs/hooks-setup.md`**

Search for the `RATE_MEMORIES` directive or the rating class list. If the four classes are shown, add `overlooked` to match `session_close.py`.

Run: `grep -n "misled\|RATE_MEMORIES\|cited" docs/hooks-setup.md`
Apply edits only where the class list appears.

- [ ] **Step 5: Add an update note to the prior design doc**

At the top of `docs/superpowers/specs/2026-05-10-memory-rating-design.md`, directly under the title, add:

```markdown
> **Update (2026-05-17):** The rating model was extended to a 5th class,
> `overlooked`, by issue #60. See
> `docs/superpowers/specs/2026-05-17-overlooked-memory-rating-class-design.md`.
```

- [ ] **Step 6: Verify no four-class list was missed**

Run: `grep -rn "cited.*shaped.*ignored.*misled\|cited / shaped / ignored / misled" website/ README.md docs/`
Expected: every remaining match is either historical context or already includes `overlooked`. Fix any current-tense four-class list that should now read five.

- [ ] **Step 7: Commit**

```bash
git add website/mcp-tools.md website/architecture.md README.md docs/hooks-setup.md docs/superpowers/specs/2026-05-10-memory-rating-design.md
git commit -m "docs: sync rating-model docs with the overlooked class"
```

---

## Final verification

- [ ] **Run the full test suite**

Run: `pytest -q`
Expected: PASS — no regressions across `tests/db/`, `tests/services/`, `tests/mcp/`, `tests/hooks/`, `tests/ui/`, `tests/integration/`.

- [ ] **Confirm the branch is ready**

Run: `git log --oneline main..HEAD`
Expected: the spec commit plus ten task commits, all on `feat/overlooked-rating-class`.
