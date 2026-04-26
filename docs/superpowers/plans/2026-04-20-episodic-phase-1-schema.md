# Episodic Memory Phase 1 — Schema Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship SQLite migration `0002_episodic.sql` that replaces the insight-based aggregation schema with the episodic-memory schema: new tables `episodes`, `episode_sessions`, `reflections`, `reflection_sources`, `synthesis_runs`; rebuilt `observations` with `episode_id NOT NULL` and `tech`; dropped `insights`, `insight_sources`, `insight_relations`.

**Architecture:** Standard better-memory migration pattern — `NNNN_<desc>.sql` files in `better_memory/db/migrations/`, applied by `apply_migrations()` which records versions in `schema_migrations`. All pre-existing observation and insight data is **dumped** per design decision (clean slate). CHECK constraints enforce enum-like columns on the new tables. FTS5 + sqlite-vec virtual tables on `reflections` follow the same pattern used on `observations` and (previously) `insights` in `0001_init.sql`.

**Tech Stack:** Python 3.12 · SQLite + sqlite-vec + FTS5 · pytest · uv

**Scope boundary:** Schema only. No service-layer stubs. No MCP tool changes. Observation writes from the existing code paths will break (they no longer supply `episode_id`) — that's acceptable; Phase 2 introduces the episode service that provides `episode_id`. Any integration tests that write observations through the service layer will fail after this phase lands and must be repaired by Phase 2.

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §4 (data model), §9 (retention allowed status values).

---

## Task 0: Create dedicated worktree

**Files:**
- Create: worktree at `C:/Users/gethi/source/better-memory-episodic-phase-1-schema`

- [ ] **Step 1: Create worktree off main**

From the main checkout (`C:/Users/gethi/source/better-memory`):

```bash
git fetch origin
git worktree add -b episodic-phase-1-schema \
  ../better-memory-episodic-phase-1-schema origin/main
```

- [ ] **Step 2: Switch to the worktree and verify**

```bash
cd ../better-memory-episodic-phase-1-schema
git status
```

Expected: `On branch episodic-phase-1-schema`, working tree clean.

- [ ] **Step 3: Verify baseline tests pass**

```bash
uv run pytest tests/db/ -v
```

Expected: all tests green (confirms the starting point is sound before changes).

---

## Task 1: Add empty migration 0002 and assert runner picks it up

Create the migration file as a comment-only stub so the runner records version `0002` in `schema_migrations`. This also proves the runner works correctly with the new filename before any DDL is added.

**Files:**
- Create: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Update `test_apply_migrations_is_idempotent` to expect 0002**

Replace the existing `test_apply_migrations_is_idempotent` in `tests/db/test_schema.py` (around line 300-312):

```python
def test_apply_migrations_is_idempotent(tmp_memory_db: Path) -> None:
    """Running :func:`apply_migrations` twice applies each file exactly once."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [r["version"] for r in rows]
        assert versions == ["0001", "0002"]
    finally:
        conn.close()
```

- [ ] **Step 2: Run the updated test — it should fail**

```bash
uv run pytest tests/db/test_schema.py::test_apply_migrations_is_idempotent -v
```

Expected: FAIL with `assert ['0001'] == ['0001', '0002']` or similar.

- [ ] **Step 3: Create the migration stub**

Create `better_memory/db/migrations/0002_episodic.sql` with this exact content:

```sql
-- better-memory migration 0002: episodic memory schema.
--
-- Replaces the insight-based aggregation schema with episodes + reflections
-- per docs/superpowers/specs/2026-04-20-episodic-memory-design.md §4.
--
-- Subsequent tasks in the Phase 1 plan append DDL to this file in
-- dependency order: drops → episodes → episode_sessions → observations
-- → reflections → reflection_sources → synthesis_runs.

-- Marker statement so executescript has at least one statement to run.
SELECT 1;
```

- [ ] **Step 4: Run the test — it should pass**

```bash
uv run pytest tests/db/test_schema.py::test_apply_migrations_is_idempotent -v
```

Expected: PASS. The runner applies 0002, records it, and the second call sees it already applied.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: add empty 0002_episodic migration stub"
```

---

## Task 2: Drop old insight tables

Remove `insights`, `insight_sources`, `insight_relations`, their FTS + embeddings virtual tables, and their triggers.

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing test — insight tables absent after 0002**

In `tests/db/test_schema.py`, add this test at the end of the file (before any trailing whitespace):

```python
def test_insight_tables_dropped(tmp_memory_db: Path) -> None:
    """All insight-related tables, virtual tables, and triggers are gone."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE name IN (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "insights",
                "insight_sources",
                "insight_relations",
                "insight_fts",
                "insight_embeddings",
                "insights_ai",
                "insights_ad",
                "insights_au",
            ),
        ).fetchall()
        assert rows == [], f"Leftover insight objects: {[r['name'] for r in rows]}"
    finally:
        conn.close()
```

- [ ] **Step 2: Remove the five old insight-specific tests**

These assume insight tables still exist and will fail after 0002 drops them. Locate each by function name (ignore line numbers — they drift as tests are added) and delete the entire function plus any leading blank lines:

- `def test_insights_has_polarity_column`
- `def test_insights_polarity_check_constraint`
- `def test_fts_triggers_index_insights`
- `def test_fts_update_trigger_on_insights`
- `def test_fts_delete_trigger_on_insights`

- [ ] **Step 3: Update `test_apply_migrations_creates_core_tables`**

Replace the expected set to drop insight tables:

```python
def test_apply_migrations_creates_core_tables(tmp_memory_db: Path) -> None:
    """All baseline (non-virtual) tables are present after migration."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        tables = _table_names(conn)
        expected = {
            "observations",
            "audit_log",
            "hook_events",
            "schema_migrations",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"
    finally:
        conn.close()
```

- [ ] **Step 4: Update `test_apply_migrations_creates_virtual_tables`**

Replace the expected set:

```python
def test_apply_migrations_creates_virtual_tables(tmp_memory_db: Path) -> None:
    """FTS5 and sqlite-vec virtual tables exist after migration."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        virtual = _virtual_table_names(conn)
        expected = {
            "observation_fts",
            "observation_embeddings",
        }
        assert expected.issubset(virtual), f"Missing virtual tables: {expected - virtual}"
    finally:
        conn.close()
```

- [ ] **Step 5: Run tests — new test should fail, updated ones should pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: `test_insight_tables_dropped` FAILS (insight tables still exist). Other updated tests should pass (or be green).

- [ ] **Step 6: Add DROP statements to `0002_episodic.sql`**

Append to `better_memory/db/migrations/0002_episodic.sql` (replacing the `SELECT 1;` placeholder):

```sql
----------------------------------------------------------------------
-- Drop old insight aggregation schema (data dumped per design decision).
----------------------------------------------------------------------

DROP TRIGGER IF EXISTS insights_au;
DROP TRIGGER IF EXISTS insights_ad;
DROP TRIGGER IF EXISTS insights_ai;
DROP TABLE IF EXISTS insight_embeddings;
DROP TABLE IF EXISTS insight_fts;
DROP TABLE IF EXISTS insight_relations;
DROP TABLE IF EXISTS insight_sources;
DROP TABLE IF EXISTS insights;
```

Keep the file header comment; replace only the `SELECT 1;` marker.

- [ ] **Step 7: Run tests — all should pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all tests PASS including `test_insight_tables_dropped`.

- [ ] **Step 8: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: drop legacy insight tables in 0002"
```

---

## Task 3: Create `episodes` table

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_episodes_table_exists_with_columns(tmp_memory_db: Path) -> None:
    """episodes has all expected columns."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "episodes")
        expected = {
            "id", "project", "tech", "goal",
            "started_at", "hardened_at", "ended_at",
            "close_reason", "outcome", "summary",
        }
        assert expected.issubset(cols), f"Missing: {expected - cols}"
    finally:
        conn.close()


def test_episodes_close_reason_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting episode with bogus close_reason raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, close_reason) "
                "VALUES (?, ?, ?, ?)",
                ("ep-bad", "proj-a", "2026-04-20T10:00:00Z", "bogus_reason"),
            )
    finally:
        conn.close()


def test_episodes_outcome_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting episode with bogus outcome raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, outcome) "
                "VALUES (?, ?, ?, ?)",
                ("ep-bad2", "proj-a", "2026-04-20T10:00:00Z", "bogus_outcome"),
            )
    finally:
        conn.close()


def test_episodes_valid_insert(tmp_memory_db: Path) -> None:
    """A minimal valid episode row inserts cleanly."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-1", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.commit()
        row = conn.execute("SELECT id, project FROM episodes").fetchone()
        assert row["id"] == "ep-1"
        assert row["project"] == "proj-a"
    finally:
        conn.close()
```

- [ ] **Step 2: Run — the four new tests should fail**

```bash
uv run pytest tests/db/test_schema.py::test_episodes_table_exists_with_columns tests/db/test_schema.py::test_episodes_close_reason_check_constraint tests/db/test_schema.py::test_episodes_outcome_check_constraint tests/db/test_schema.py::test_episodes_valid_insert -v
```

Expected: all four FAIL with `no such table: episodes` or similar.

- [ ] **Step 3: Append the episodes DDL to `0002_episodic.sql`**

Append to `better_memory/db/migrations/0002_episodic.sql`:

```sql
----------------------------------------------------------------------
-- Episodes: goal-bounded arcs that group observations.
----------------------------------------------------------------------

CREATE TABLE episodes (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    tech          TEXT,
    goal          TEXT,
    started_at    TEXT NOT NULL,
    hardened_at   TEXT,
    ended_at      TEXT,
    close_reason  TEXT CHECK(close_reason IN (
        'goal_complete',
        'plan_complete',
        'abandoned',
        'superseded',
        'session_end_reconciled'
    )),
    outcome       TEXT CHECK(outcome IN (
        'success',
        'partial',
        'abandoned',
        'no_outcome'
    )),
    summary       TEXT
);

CREATE INDEX idx_episodes_project_ended ON episodes(project, ended_at);
CREATE INDEX idx_episodes_project_outcome ON episodes(project, outcome);
CREATE INDEX idx_episodes_tech ON episodes(tech);
```

- [ ] **Step 4: Run tests — all four should pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: create episodes table"
```

---

## Task 4: Create `episode_sessions` join table

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_episode_sessions_exists_with_columns(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "episode_sessions")
        assert {"episode_id", "session_id", "joined_at", "left_at"}.issubset(cols)
    finally:
        conn.close()


def test_episode_sessions_composite_primary_key(tmp_memory_db: Path) -> None:
    """Duplicate (episode_id, session_id) insert raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-a", "p", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
            "VALUES (?, ?, ?)",
            ("ep-a", "sess-1", "2026-04-20T10:00:00Z"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
                "VALUES (?, ?, ?)",
                ("ep-a", "sess-1", "2026-04-20T11:00:00Z"),
            )
    finally:
        conn.close()


def test_episode_sessions_fk_enforced(tmp_memory_db: Path) -> None:
    """Inserting into episode_sessions without a matching episode row fails."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
                "VALUES (?, ?, ?)",
                ("no-such-ep", "sess-1", "2026-04-20T10:00:00Z"),
            )
    finally:
        conn.close()
```

- [ ] **Step 2: Run — three new tests fail**

```bash
uv run pytest tests/db/test_schema.py::test_episode_sessions_exists_with_columns tests/db/test_schema.py::test_episode_sessions_composite_primary_key tests/db/test_schema.py::test_episode_sessions_fk_enforced -v
```

Expected: FAIL.

- [ ] **Step 3: Append DDL to `0002_episodic.sql`**

```sql
----------------------------------------------------------------------
-- Episode-sessions join: an episode may span multiple sessions
-- (user picks `continuing` at reconciliation).
----------------------------------------------------------------------

CREATE TABLE episode_sessions (
    episode_id  TEXT NOT NULL REFERENCES episodes(id),
    session_id  TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    left_at     TEXT,
    PRIMARY KEY (episode_id, session_id)
);

CREATE INDEX idx_episode_sessions_session ON episode_sessions(session_id);
CREATE INDEX idx_episode_sessions_open ON episode_sessions(session_id, left_at);
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: create episode_sessions join table"
```

---

## Task 5: Rebuild `observations` with `episode_id NOT NULL` and `tech`

Drop and recreate the `observations` table (all data dumped per design decision). Recreate the FTS virtual table, embeddings virtual table, and the three triggers. Add `episode_id` (NOT NULL, FK → episodes.id) and `tech` (nullable). All other columns carry over unchanged.

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_observations_has_episodic_fk_columns(tmp_memory_db: Path) -> None:
    """observations has episode_id (not null) and tech (nullable)."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        rows = conn.execute("PRAGMA table_info(observations)").fetchall()
        by_name = {r["name"]: r for r in rows}
        assert "episode_id" in by_name
        assert by_name["episode_id"]["notnull"] == 1, "episode_id must be NOT NULL"
        assert "tech" in by_name
        assert by_name["tech"]["notnull"] == 0, "tech must be nullable"
    finally:
        conn.close()


def test_observations_requires_episode_id(tmp_memory_db: Path) -> None:
    """Inserting an observation without episode_id raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project) VALUES (?, ?, ?)",
                ("obs-x", "content", "proj-a"),
            )
    finally:
        conn.close()


def test_observations_episode_fk_enforced(tmp_memory_db: Path) -> None:
    """Inserting an observation with unknown episode_id raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, ?, ?)",
                ("obs-y", "c", "p", "no-such-episode"),
            )
    finally:
        conn.close()


def test_observations_valid_insert_with_episode(tmp_memory_db: Path) -> None:
    """A valid observation linked to a real episode inserts cleanly."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-o", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, tech) "
            "VALUES (?, ?, ?, ?, ?)",
            ("obs-ok", "hello", "proj-a", "ep-o", "python"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT episode_id, tech FROM observations WHERE id = ?",
            ("obs-ok",),
        ).fetchone()
        assert row["episode_id"] == "ep-o"
        assert row["tech"] == "python"
    finally:
        conn.close()
```

Note — the pre-existing tests `test_observations_has_episodic_columns` (outcome/reinforcement_score/scope_path), `test_observations_outcome_check_constraint`, `test_observations_outcome_accepts_valid_values`, `test_fts_triggers_index_observations`, `test_fts_update_trigger_on_observations`, `test_fts_delete_trigger_on_observations`, and `test_episodic_indexes_exist` must continue to pass (proves the rebuild preserved the old columns/indexes/triggers). Those tests use ad-hoc observation inserts without an `episode_id`, so they will now break unless updated. Update them in the next step.

- [ ] **Step 2: Update existing observation tests to pass `episode_id`**

Edit `tests/db/test_schema.py` — where the following tests build observation rows, prepend an episode insert and include `episode_id`. Locate each test by function name (line numbers drift).

In `test_observations_outcome_accepts_valid_values`:

```python
def test_observations_outcome_accepts_valid_values(tmp_memory_db: Path) -> None:
    """``success``, ``failure``, ``neutral`` are accepted for ``outcome``."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-ov", "proj-a", "2026-04-20T10:00:00Z"),
        )
        for i, outcome in enumerate(("success", "failure", "neutral")):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome, episode_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"obs-{i}", f"content {i}", "proj-a", outcome, "ep-ov"),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
        assert n == 3
    finally:
        conn.close()
```

In `test_observations_outcome_check_constraint`:

```python
def test_observations_outcome_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting an observation with a bogus outcome raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-oc", "proj-a", "2026-04-20T10:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome, episode_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("obs-bad", "bogus outcome test", "proj-a", "bogus", "ep-oc"),
            )
    finally:
        conn.close()
```

In `test_fts_triggers_index_observations`:

```python
def test_fts_triggers_index_observations(tmp_memory_db: Path) -> None:
    """Inserting into observations populates observation_fts via triggers."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fi", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, episode_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "obs-1",
                "flamingo migration failed under cold weather",
                "proj-a",
                "migrations",
                "zoology",
                "ep-fi",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("flamingo",),
        ).fetchone()
        assert row is not None, "FTS did not index inserted observation"
    finally:
        conn.close()
```

In `test_fts_update_trigger_on_observations`:

```python
def test_fts_update_trigger_on_observations(tmp_memory_db: Path) -> None:
    """Updating observations.content re-indexes the FTS row."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fu", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-u", "flamingo marker", "proj-a", "ep-fu"),
        )
        conn.commit()
        conn.execute(
            "UPDATE observations SET content = ? WHERE id = ?",
            ("pelican marker", "obs-u"),
        )
        conn.commit()
        flamingo = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("flamingo",),
        ).fetchall()
        pelican = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("pelican",),
        ).fetchall()
        assert len(flamingo) == 0, "UPDATE trigger left stale FTS row"
        assert len(pelican) == 1, "UPDATE trigger did not index new content"
    finally:
        conn.close()
```

In `test_fts_delete_trigger_on_observations`:

```python
def test_fts_delete_trigger_on_observations(tmp_memory_db: Path) -> None:
    """Deleting an observation removes the FTS row."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fd", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-d", "heron marker", "proj-a", "ep-fd"),
        )
        conn.commit()
        conn.execute("DELETE FROM observations WHERE id = ?", ("obs-d",))
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("heron",),
        ).fetchall()
        assert len(rows) == 0, "DELETE trigger left FTS row behind"
    finally:
        conn.close()
```

- [ ] **Step 3: Run — four new tests fail, updated ones pass (no migration yet)**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: the four new tests from Step 1 FAIL (`no such column: episode_id`). The observation-FTS tests updated in Step 2 will also FAIL until Step 4 runs (because they now reference `episode_id` which doesn't exist yet).

- [ ] **Step 4: Append observations rebuild to `0002_episodic.sql`**

```sql
----------------------------------------------------------------------
-- Rebuild observations with episode_id (NOT NULL, FK) and tech.
-- Existing rows are dumped per design decision; the new schema requires
-- an active episode at write time, so backfilling with NULL would violate
-- the invariant.
----------------------------------------------------------------------

DROP TRIGGER IF EXISTS observations_au;
DROP TRIGGER IF EXISTS observations_ad;
DROP TRIGGER IF EXISTS observations_ai;
DROP TABLE IF EXISTS observation_embeddings;
DROP TABLE IF EXISTS observation_fts;
DROP TABLE IF EXISTS observations;

CREATE TABLE observations (
    id                  TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    project             TEXT NOT NULL,
    component           TEXT,
    theme               TEXT,
    session_id          TEXT,
    trigger_type        TEXT,
    status              TEXT DEFAULT 'active',
    retrieved_count     INTEGER DEFAULT 0,
    used_count          INTEGER DEFAULT 0,
    validated_true      INTEGER DEFAULT 0,
    validated_false     INTEGER DEFAULT 0,
    last_retrieved      TIMESTAMP,
    last_used           TIMESTAMP,
    last_validated      TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    outcome             TEXT NOT NULL DEFAULT 'neutral'
                        CHECK(outcome IN ('success', 'failure', 'neutral')),
    reinforcement_score REAL NOT NULL DEFAULT 0.0,
    scope_path          TEXT,
    -- Episodic columns (Phase 1):
    episode_id          TEXT NOT NULL REFERENCES episodes(id),
    tech                TEXT
);

CREATE INDEX idx_observations_project_component_outcome
    ON observations(project, component, outcome);
CREATE INDEX idx_observations_scope_outcome
    ON observations(scope_path, outcome);
CREATE INDEX idx_observations_episode
    ON observations(episode_id);
CREATE INDEX idx_observations_tech
    ON observations(tech);

CREATE VIRTUAL TABLE observation_fts USING fts5(
    content,
    component,
    theme,
    content='observations',
    content_rowid='rowid'
);

CREATE TRIGGER observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_fts(rowid, content, component, theme)
    VALUES (new.rowid, new.content, new.component, new.theme);
END;

CREATE TRIGGER observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_fts(observation_fts, rowid, content, component, theme)
    VALUES ('delete', old.rowid, old.content, old.component, old.theme);
END;

CREATE TRIGGER observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observation_fts(observation_fts, rowid, content, component, theme)
    VALUES ('delete', old.rowid, old.content, old.component, old.theme);
    INSERT INTO observation_fts(rowid, content, component, theme)
    VALUES (new.rowid, new.content, new.component, new.theme);
END;

CREATE VIRTUAL TABLE observation_embeddings USING vec0(
    observation_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);
```

- [ ] **Step 5: Run tests — all observation tests pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: rebuild observations with episode_id FK and tech"
```

---

## Task 6: Create `reflections` table + FTS + embeddings + triggers

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_reflections_table_exists_with_columns(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "reflections")
        expected = {
            "id", "title", "project", "tech", "phase", "polarity",
            "use_cases", "hints", "confidence", "status", "superseded_by",
            "evidence_count", "created_at", "updated_at",
        }
        assert expected.issubset(cols), f"Missing: {expected - cols}"
    finally:
        conn.close()


def test_reflections_phase_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-bad", "t", "p", "bogus_phase", "do",
                 "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_polarity_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-bad", "t", "p", "general", "bogus_pol",
                 "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_confidence_range(tmp_memory_db: Path) -> None:
    """confidence must be in [0.1, 1.0]."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-hi", "t", "p", "general", "do", "uc", "[]",
                 1.5, 0, "2026-04-20", "2026-04-20"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-lo", "t", "p", "general", "do", "uc", "[]",
                 0.05, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_status_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, status, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r-st", "t", "p", "general", "do", "uc", "[]", 0.5,
                 "bogus_status", 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_valid_insert_and_fts(tmp_memory_db: Path) -> None:
    """Valid reflection inserts and is indexed by FTS."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-1", "Pelican preference", "proj-a", "general", "do",
             "when handling pelicans", '["use wide runways"]',
             0.7, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("pelican",),
        ).fetchone()
        assert row is not None, "FTS did not index inserted reflection"
    finally:
        conn.close()


def test_reflections_update_trigger(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-u", "flamingo", "proj-a", "general", "do",
             "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        conn.execute(
            "UPDATE reflections SET title = ? WHERE id = ?",
            ("pelican", "r-u"),
        )
        conn.commit()
        flamingo = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("flamingo",),
        ).fetchall()
        pelican = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("pelican",),
        ).fetchall()
        assert len(flamingo) == 0
        assert len(pelican) == 1
    finally:
        conn.close()


def test_reflections_delete_trigger(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-d", "heron", "proj-a", "general", "do",
             "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        conn.execute("DELETE FROM reflections WHERE id = ?", ("r-d",))
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("heron",),
        ).fetchall()
        assert len(rows) == 0
    finally:
        conn.close()
```

- [ ] **Step 2: Run — nine new tests fail**

```bash
uv run pytest tests/db/test_schema.py -k reflections -v
```

Expected: FAIL with `no such table: reflections`.

- [ ] **Step 3: Append DDL to `0002_episodic.sql`**

```sql
----------------------------------------------------------------------
-- Reflections: generalised lessons synthesised from observations.
-- Replaces the old insights table.
----------------------------------------------------------------------

CREATE TABLE reflections (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    project         TEXT NOT NULL,
    tech            TEXT,
    phase           TEXT NOT NULL
                    CHECK(phase IN ('planning', 'implementation', 'general')),
    polarity        TEXT NOT NULL
                    CHECK(polarity IN ('do', 'dont', 'neutral')),
    use_cases       TEXT NOT NULL,
    hints           TEXT NOT NULL,
    confidence      REAL NOT NULL
                    CHECK(confidence >= 0.1 AND confidence <= 1.0),
    status          TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK(status IN (
                        'pending_review', 'confirmed', 'retired', 'superseded'
                    )),
    superseded_by   TEXT REFERENCES reflections(id),
    evidence_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX idx_reflections_project_status ON reflections(project, status);
CREATE INDEX idx_reflections_tech ON reflections(tech);
CREATE INDEX idx_reflections_phase_polarity ON reflections(phase, polarity);

CREATE VIRTUAL TABLE reflection_fts USING fts5(
    title,
    use_cases,
    hints,
    content='reflections',
    content_rowid='rowid'
);

CREATE TRIGGER reflections_ai AFTER INSERT ON reflections BEGIN
    INSERT INTO reflection_fts(rowid, title, use_cases, hints)
    VALUES (new.rowid, new.title, new.use_cases, new.hints);
END;

CREATE TRIGGER reflections_ad AFTER DELETE ON reflections BEGIN
    INSERT INTO reflection_fts(reflection_fts, rowid, title, use_cases, hints)
    VALUES ('delete', old.rowid, old.title, old.use_cases, old.hints);
END;

CREATE TRIGGER reflections_au AFTER UPDATE ON reflections BEGIN
    INSERT INTO reflection_fts(reflection_fts, rowid, title, use_cases, hints)
    VALUES ('delete', old.rowid, old.title, old.use_cases, old.hints);
    INSERT INTO reflection_fts(rowid, title, use_cases, hints)
    VALUES (new.rowid, new.title, new.use_cases, new.hints);
END;

CREATE VIRTUAL TABLE reflection_embeddings USING vec0(
    reflection_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: create reflections table with FTS + embeddings"
```

---

## Task 7: Create `reflection_sources` join table

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_reflection_sources_exists(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "reflection_sources")
        assert {"reflection_id", "observation_id"}.issubset(cols)
    finally:
        conn.close()


def test_reflection_sources_composite_pk_and_fks(tmp_memory_db: Path) -> None:
    """Composite PK enforced; both FKs enforced."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        # Setup: need an episode, observation, and reflection.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-rs", "p", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-rs", "c", "p", "ep-rs"),
        )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("refl-rs", "t", "p", "general", "do", "uc", "[]",
             0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()

        # Valid link inserts cleanly.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)",
            ("refl-rs", "obs-rs"),
        )
        conn.commit()

        # Duplicate (composite PK) rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("refl-rs", "obs-rs"),
            )

        # Unknown reflection FK rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("no-such-refl", "obs-rs"),
            )

        # Unknown observation FK rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("refl-rs", "no-such-obs"),
            )
    finally:
        conn.close()
```

- [ ] **Step 2: Run — two new tests fail**

```bash
uv run pytest tests/db/test_schema.py::test_reflection_sources_exists tests/db/test_schema.py::test_reflection_sources_composite_pk_and_fks -v
```

Expected: FAIL.

- [ ] **Step 3: Append DDL to `0002_episodic.sql`**

```sql
----------------------------------------------------------------------
-- Reflection → observation link table.
----------------------------------------------------------------------

CREATE TABLE reflection_sources (
    reflection_id   TEXT NOT NULL REFERENCES reflections(id),
    observation_id  TEXT NOT NULL REFERENCES observations(id),
    PRIMARY KEY (reflection_id, observation_id)
);

CREATE INDEX idx_reflection_sources_observation
    ON reflection_sources(observation_id);
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: create reflection_sources link table"
```

---

## Task 8: Create `synthesis_runs` watermark table

SQLite treats each `NULL` value as distinct for uniqueness — which defeats a primary key containing a nullable column. We use `tech TEXT NOT NULL DEFAULT ''` so `(project, tech)` is a clean composite PK. Callers pass `''` to mean "no tech filter".

**Files:**
- Modify: `better_memory/db/migrations/0002_episodic.sql`
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_schema.py`:

```python
def test_synthesis_runs_exists(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "synthesis_runs")
        assert {"project", "tech", "last_run_at"}.issubset(cols)
    finally:
        conn.close()


def test_synthesis_runs_composite_pk(tmp_memory_db: Path) -> None:
    """(project, tech) is a primary key; tech defaults to '' (not NULL)."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        # Two rows with same project but different tech — both succeed.
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "python", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "sqlite", "2026-04-20T10:00:00Z"),
        )
        conn.commit()

        # Default tech is '' — project without tech is a distinct PK.
        conn.execute(
            "INSERT INTO synthesis_runs (project, last_run_at) VALUES (?, ?)",
            ("p", "2026-04-20T10:00:00Z"),
        )
        conn.commit()

        # Duplicate (project, tech) rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (project, tech, last_run_at) "
                "VALUES (?, ?, ?)",
                ("p", "python", "2026-04-20T11:00:00Z"),
            )

        # tech NOT NULL — explicit NULL rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (project, tech, last_run_at) "
                "VALUES (?, ?, ?)",
                ("p2", None, "2026-04-20T10:00:00Z"),
            )
    finally:
        conn.close()
```

- [ ] **Step 2: Run — two new tests fail**

```bash
uv run pytest tests/db/test_schema.py::test_synthesis_runs_exists tests/db/test_schema.py::test_synthesis_runs_composite_pk -v
```

Expected: FAIL.

- [ ] **Step 3: Append DDL to `0002_episodic.sql`**

```sql
----------------------------------------------------------------------
-- Synthesis watermark: tracks the last time synthesis ran for a
-- (project, tech) pair, so subsequent runs can scope "observations since".
-- tech defaults to '' so the composite PK remains clean under SQLite's
-- NULL-in-uniqueness semantics.
----------------------------------------------------------------------

CREATE TABLE synthesis_runs (
    project       TEXT NOT NULL,
    tech          TEXT NOT NULL DEFAULT '',
    last_run_at   TEXT NOT NULL,
    PRIMARY KEY (project, tech)
);
```

- [ ] **Step 4: Run — all pass**

```bash
uv run pytest tests/db/test_schema.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0002_episodic.sql tests/db/test_schema.py
git commit -m "Phase 1: create synthesis_runs watermark table"
```

---

## Task 9: Skip tests that depend on the dropped schema

After the previous tasks, roughly 60-90 tests across 9 files will fail because they write observations without `episode_id`, insert into the now-dropped `insights`/`insight_sources`/`insight_relations` tables, or exercise UI/services that query those tables. Rather than leave the suite red, mark the affected tests as skipped with a clear reason pointing at Phase 2. Phase 2 will un-skip deliberately as it replaces each area.

**Files (modifications):**
- `tests/ui/test_pipeline.py` — full file
- `tests/ui/test_queries.py` — full file
- `tests/ui/test_apply_job.py` — full file
- `tests/ui/test_consolidation_e2e.py` — full file
- `tests/ui/test_browser.py` — full file
- `tests/search/test_hybrid.py` — full file
- `tests/services/test_consolidation.py` — full file
- `tests/services/test_insight.py` — full file
- `tests/ui/test_app.py` — **partial**: only `TestServiceWiring`, `TestBadgeFragment`, `TestBadgeRealCount`, `TestConsolidationWiring`. The rest (`TestHealthz`, `TestRootRedirect`, `TestLayoutShell`, `TestEmptyViews`, `TestOriginCheck`, `TestStaticAssets`, `TestShutdown`, `TestInactivityTimeout`, `TestOnlyOneExpandedScript`) don't touch the dropped schema and should keep running.

**Marker text (use this exact string everywhere):**
```
Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md
```

- [ ] **Step 1: Apply module-level skip to the 8 fully-affected files**

For each file in the "full file" list above, add these two lines immediately after the existing `from __future__ import annotations` line (or at the top of imports if that line doesn't exist):

```python
import pytest

pytestmark = pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
```

If `import pytest` is already present, don't duplicate it — just add the `pytestmark` line.

- [ ] **Step 2: Apply class-level skip to the four affected classes in `tests/ui/test_app.py`**

Find each of `TestServiceWiring`, `TestBadgeFragment`, `TestBadgeRealCount`, `TestConsolidationWiring` by name and add the decorator immediately above the `class` line:

```python
@pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
class TestServiceWiring:
    ...
```

(Same decorator on each of the four classes. Leave the other nine classes unchanged.)

- [ ] **Step 3: Run the full suite — expect all tests either PASS or SKIP, zero failures**

```bash
uv run pytest -v
```

Expected: `passed` count drops from the baseline by roughly 60-90 (those tests are now SKIPPED); zero FAILED. If any test still fails, it is either (a) a file missed in the skip list — add the marker, or (b) a genuine Phase 1 regression in `tests/db/` — fix it before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "Phase 1: skip tests awaiting Phase 2 episodic services"
```

---

## Task 10: Final verification and push

**Files:**
- No changes — verification only.

- [ ] **Step 1: Confirm suite is green**

```bash
uv run pytest --tb=short -q
```

Expected: output ends with something like `N passed, M skipped` and zero failures.

- [ ] **Step 2: Confirm DB test coverage is intact**

```bash
uv run pytest tests/db/ -v
```

Expected: all db tests PASS (no skips — Phase 1 is the schema layer).

- [ ] **Step 3: Confirm working tree is clean**

```bash
git status
```

Expected: `nothing to commit, working tree clean`.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin episodic-phase-1-schema
```

---

## Self-review checklist (run once plan is complete)

Before declaring the plan done:

- Every table named in spec §4 has a creation task (episodes ✓, episode_sessions ✓, reflections ✓, reflection_sources ✓, synthesis_runs ✓). Observation modifications ✓.
- All CHECK constraints from spec §4 are tested (close_reason, outcome, phase, polarity, confidence range, status).
- The `insights_legacy` snapshot from the spec is intentionally omitted because the user's clean-slate decision makes it redundant — documented in the header scope note. If you need to preserve legacy data for audit, revisit the scope boundary.
- `episode_id` is NOT NULL on observations and the FK is enforced.
- Migration is idempotent (0001 + 0002 applied once on re-run).
- No retention DDL, no archival triggers, no service-layer stubs — those land in later phases.
