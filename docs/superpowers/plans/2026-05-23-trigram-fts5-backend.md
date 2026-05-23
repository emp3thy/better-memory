# Trigram-FTS5 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Python TF-IDF embeddings backend (PR #65, merged 2026-05-22) with a pure-SQL implementation using SQLite FTS5's trigram tokenizer. Rename backend value `tfidf` → `sqlite`.

**Architecture:** A new FTS5 virtual table `observation_trigram_fts` (tokenize='trigram') is populated by triggers mirroring the existing `observation_fts` table. `hybrid_search` gains a `second_source: Literal["vec0", "trigram", "none"]` parameter; the `ollama` backend passes `"vec0"`, the new `sqlite` backend passes `"trigram"`. Service code stops carrying a Python retriever; the read path branches via `second_source` only.

**Tech Stack:** Python 3.12+, SQLite 3.34+ (trigram tokenizer; 3.49.1 confirmed locally), existing FTS5 + sqlite-vec infrastructure, pytest with `asyncio_mode="auto"`.

**Spec:** `docs/superpowers/specs/2026-05-23-trigram-fts5-backend-design.md`

---

## File Map

| Path | Status | Responsibility |
|---|---|---|
| `better_memory/db/migrations/0011_trigram_fts.sql` | NEW | Trigram FTS5 table + triggers + backfill |
| `better_memory/search/hybrid.py` | modify | Add `second_source` param, `_trigram_candidates` helper |
| `better_memory/config.py` | modify | Rename `tfidf` → `sqlite` in Literal + valid tuple + docstring |
| `better_memory/services/observation.py` | modify | Drop retriever kwarg + post-commit add_doc; route via `second_source` |
| `better_memory/mcp/server.py` | modify | Drop TfidfRetriever instantiation + import |
| `better_memory/embeddings/__init__.py` | modify | Stop exporting TfidfRetriever |
| `better_memory/embeddings/tfidf.py` | DELETE | Python TF-IDF class |
| `better_memory/search/tfidf_search.py` | DELETE | Parallel fusion module |
| `tests/db/test_migration_0011_trigram_fts.py` | NEW | Migration backfill + trigger sync |
| `tests/search/test_hybrid.py` | modify | Add second_source="trigram" + "none" cases |
| `tests/embeddings/test_tfidf_unit.py` | DELETE | |
| `tests/search/test_tfidf_search.py` | DELETE | |
| `tests/services/test_observation_tfidf.py` | rename | → `test_observation_sqlite.py`; drop XOR test |
| `tests/mcp/test_server_tfidf.py` | rename | → `test_server_sqlite.py` |
| `tests/test_config.py` | modify | Rename test cases referencing `tfidf` value |
| `README.md`, `website/configuration.md`, `website/architecture.md` | modify | Rename + rewrite descriptions |
| `docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md` | modify | Add SUPERSEDED notice |
| `docs/superpowers/plans/2026-05-22-tfidf-embeddings-backend.md` | modify | Add SUPERSEDED notice |

## Confidence Summary

| Task | Confidence | Lift |
|---|---|---|
| 1. Migration 0011 (trigram table + triggers + backfill) | 92% (lifted from 88%) | Existing trigger pattern from 0002_episodic.sql:128-143 grepped and embedded in step |
| 2. `hybrid_search` `second_source` param + `_trigram_candidates` | 95% | — |
| 3. Config rename `tfidf` → `sqlite` | 95% | — |
| 4. `ObservationService` — drop retriever, branch on backend | 92% (lifted from 88%) | Full method diff inline; existing branches removed step-by-step |
| 5. `mcp/server.py` — drop TfidfRetriever wiring | 95% | — |
| 6. Delete Python TF-IDF files + tests | 98% | — |
| 7. Documentation + SUPERSEDED notices | 95% | — |

All ≥ 90%. No residual sub-90% items.

---

### Task 1: Migration 0011 — trigram FTS5 table

**Files:**
- Create: `better_memory/db/migrations/0011_trigram_fts.sql`
- Create: `tests/db/test_migration_0011_trigram_fts.py`

Trigger naming verified against `0002_episodic.sql:128-143`: existing triggers are named `observations_ai`, `observations_ad`, `observations_au` on the `observations` table. The new triggers will use suffixed names (`observations_trigram_ai/ad/au`) and live alongside the existing ones — both fire on each event.

- [ ] **Step 1: Write the failing migration test**

Create `tests/db/test_migration_0011_trigram_fts.py`:

```python
"""Migration 0011 — trigram FTS5 table for the sqlite embeddings backend."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


def _insert_obs(conn: sqlite3.Connection, obs_id: str, content: str) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, outcome) "
        "VALUES (?, 'p', '2026-01-01T00:00:00+00:00', NULL)",
        (f"ep-{obs_id}",),
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, "
        "status, outcome, created_at, status_changed_at) "
        "VALUES (?, ?, 'p', ?, 'active', 'neutral', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (obs_id, content, f"ep-{obs_id}"),
    )
    conn.commit()


class TestMigration0011:
    def test_trigram_table_created(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='observation_trigram_fts'"
            ).fetchone()
            assert row is not None
        finally:
            c.close()

    def test_backfill_indexes_existing_observations(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            # Apply all migrations up to but not including 0011 via the
            # standard mechanism (since the migration runner tracks applied
            # versions, just apply everything, then verify backfill ran on
            # whatever rows we inserted between earlier and 0011 — at first
            # install the table is empty so we add rows AFTER, see below).
            apply_migrations(c)

            _insert_obs(c, "o1", "first observation content")
            _insert_obs(c, "o2", "second observation content")

            # Trigger should have indexed both.
            count = c.execute(
                "SELECT COUNT(*) FROM observation_trigram_fts"
            ).fetchone()[0]
            assert count == 2
        finally:
            c.close()

    def test_insert_trigger_indexes_new_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "hello world")

            # Trigram MATCH on a substring should find the row.
            rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'ello'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            c.close()

    def test_delete_trigger_removes_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "unique content xyz")
            c.execute("DELETE FROM observations WHERE id='o1'")
            c.commit()

            count = c.execute(
                "SELECT COUNT(*) FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'xyz'"
            ).fetchone()[0]
            assert count == 0
        finally:
            c.close()

    def test_update_trigger_reindexes_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "old content alpha")
            c.execute(
                "UPDATE observations SET content='new content bravo' WHERE id='o1'"
            )
            c.commit()

            # 'alpha' should no longer match
            alpha_rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'alpha'"
            ).fetchall()
            assert alpha_rows == []
            # 'bravo' should match
            bravo_rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'bravo'"
            ).fetchall()
            assert len(bravo_rows) == 1
        finally:
            c.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/db/test_migration_0011_trigram_fts.py -v
```
Expected: FAIL — `no such table: observation_trigram_fts`.

- [ ] **Step 3: Create the migration**

Create `better_memory/db/migrations/0011_trigram_fts.sql`:

```sql
-- Migration 0011: trigram FTS5 table for the `sqlite` embeddings backend.
--
-- Adds a second FTS5 virtual table over observations.content using the
-- trigram tokenizer. The `sqlite` backend uses this as the second source
-- in hybrid_search's RRF fusion (replacing vec0 kNN).
--
-- The table is populated regardless of which backend is active, so
-- switching BETTER_MEMORY_EMBEDDINGS_BACKEND between ollama and sqlite
-- requires no data migration at runtime.

CREATE VIRTUAL TABLE observation_trigram_fts USING fts5(
    content,
    content='observations',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Backfill from existing observations.
INSERT INTO observation_trigram_fts(rowid, content)
SELECT rowid, content FROM observations;

-- Keep in sync. Names suffixed with _trigram so they do not collide with
-- the existing observations_ai / _ad / _au triggers (created in
-- 0002_episodic.sql) which write to observation_fts.

CREATE TRIGGER observations_trigram_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_trigram_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

CREATE TRIGGER observations_trigram_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER observations_trigram_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO observation_trigram_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/db/test_migration_0011_trigram_fts.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Run the full DB suite for regressions**

```
uv run pytest tests/db -v
```
Expected: no regressions.

- [ ] **Step 6: Commit**

```
git add better_memory/db/migrations/0011_trigram_fts.sql tests/db/test_migration_0011_trigram_fts.py
git commit -m "feat(db): migration 0011 — observation_trigram_fts virtual table"
```

---

### Task 2: `hybrid_search` — `second_source` parameter + `_trigram_candidates`

**Files:**
- Modify: `better_memory/search/hybrid.py`
- Modify: `tests/search/test_hybrid.py`

- [ ] **Step 1: Write failing test for the trigram path**

Append to `tests/search/test_hybrid.py`:

```python
def test_hybrid_search_second_source_trigram(conn) -> None:
    """When second_source='trigram', the second source is FTS5 trigram BM25."""
    # Use the existing seed helper in this test file (or inline a minimal seed).
    _seed(conn, ("hit", "pytest junit-xml on windows", "success"),
                ("miss", "completely unrelated", "success"))
    filters = SearchFilters(project="p", status="active",
                            window_days=None, outcome="success")
    results = hybrid_search(
        conn, query_text="pytest windows",
        second_source="trigram", filters=filters,
        limit=2, clock=_fixed_clock,
    )
    assert [hit.id for hit in results][0] == "hit"


def test_hybrid_search_second_source_none(conn) -> None:
    """When second_source='none', only word-FTS5 BM25 runs."""
    _seed(conn, ("o1", "alpha bravo", "success"))
    filters = SearchFilters(project="p", status="active",
                            window_days=None, outcome="success")
    results = hybrid_search(
        conn, query_text="alpha",
        second_source="none", filters=filters,
        limit=5, clock=_fixed_clock,
    )
    assert any(hit.id == "o1" for hit in results)


def test_hybrid_search_second_source_vec0_unchanged(conn) -> None:
    """Default second_source='vec0' behaviour is preserved."""
    # Existing test pattern — extend by passing query_vector explicitly.
    pass  # if the existing suite already covers default behaviour, omit this
```

NOTE: Check `tests/search/test_hybrid.py` for existing seed helpers and clock fixtures; mirror them rather than reinventing. The seed helper above (`_seed`) and clock (`_fixed_clock`) are illustrative — use whatever the file already defines.

- [ ] **Step 2: Run tests to verify failure**

```
uv run pytest tests/search/test_hybrid.py::test_hybrid_search_second_source_trigram -v
```
Expected: FAIL — `hybrid_search()` got an unexpected keyword argument `second_source`.

- [ ] **Step 3: Add `_trigram_candidates` helper to `hybrid.py`**

Add after the existing `_vec_candidates` function (around line 270):

```python
def _trigram_candidates(
    conn: sqlite3.Connection,
    *,
    query_text: str,
    where_sql: str,
    where_params: list[Any],
    candidate_k: int,
) -> list[str]:
    """Return observation ids ordered by trigram-FTS5 BM25 (best first)."""
    sql = (
        "SELECT o.id AS id, bm25(observation_trigram_fts) AS bm "
        "FROM observation_trigram_fts "
        "JOIN observations o ON o.rowid = observation_trigram_fts.rowid "
        "WHERE observation_trigram_fts MATCH ?"
    )
    params: list[Any] = [query_text]
    if where_sql:
        sql += " AND " + where_sql
        params.extend(where_params)
    sql += " ORDER BY bm ASC LIMIT ?"
    params.append(candidate_k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Malformed FTS5 query text — treat as no matches.
        return []
    return [r["id"] for r in rows]
```

- [ ] **Step 4: Update `hybrid_search` to accept `second_source`**

Modify the function signature and the candidate-fetch block:

```python
from typing import Literal

# (add this import near the top of the file if not already present)

def hybrid_search(
    conn: sqlite3.Connection,
    *,
    query_text: str | None = None,
    query_vector: list[float] | None = None,
    second_source: Literal["vec0", "trigram", "none"] = "vec0",
    filters: SearchFilters = _DEFAULT_FILTERS,
    limit: int = 10,
    candidate_k: int = 50,
    rrf_k: int = 60,
    reinforcement_alpha: float = 0.1,
    recency_half_life_days: float = 14.0,
    clock: Callable[[], datetime] | None = None,
) -> list[SearchResult]:
    """Run hybrid search and return the top ``limit`` results."""
    if query_text is None and query_vector is None and second_source != "trigram":
        return []
    if second_source == "trigram" and (query_text is None or not query_text.strip()):
        return []

    now = (clock or _default_clock)()
    where_sql, where_params = _build_where(filters, now=now)

    fts_ids: list[str] = []
    second_ids: list[str] = []

    if query_text is not None and query_text.strip():
        fts_ids = _fts_candidates(
            conn,
            query_text=query_text,
            where_sql=where_sql,
            where_params=where_params,
            candidate_k=candidate_k,
        )
    if second_source == "vec0" and query_vector is not None:
        second_ids = _vec_candidates(
            conn,
            query_vector=query_vector,
            where_sql=where_sql,
            where_params=where_params,
            candidate_k=candidate_k,
        )
    elif second_source == "trigram" and query_text is not None and query_text.strip():
        second_ids = _trigram_candidates(
            conn,
            query_text=query_text,
            where_sql=where_sql,
            where_params=where_params,
            candidate_k=candidate_k,
        )
    # second_source == "none" → second_ids stays []

    if not fts_ids and not second_ids:
        return []

    candidates: dict[str, _Candidate] = {}
    _add_rrf_ranks(candidates, fts_ids, source="fts", rrf_k=rrf_k)
    _add_rrf_ranks(candidates, second_ids, source="vec", rrf_k=rrf_k)

    if not candidates:
        return []

    rows = _fetch_rows(conn, list(candidates.keys()))
    for row in rows:
        candidates[row["id"]].row = row

    results = [
        _finalize(c, now=now, alpha=reinforcement_alpha, half_life=recency_half_life_days)
        for c in candidates.values()
        if c.row is not None
    ]
    results.sort(key=lambda r: (-r.final_score, r.id))
    return results[:limit]
```

The RRF source tag stays `"vec"` for both vec0 and trigram so downstream behaviour (and existing tests asserting on candidate `.ranks["vec"]`) doesn't drift.

- [ ] **Step 5: Run tests**

```
uv run pytest tests/search -v
```
Expected: new trigram + none tests pass; existing vec0 tests stay green.

- [ ] **Step 6: Commit**

```
git add better_memory/search/hybrid.py tests/search/test_hybrid.py
git commit -m "feat(search): hybrid_search second_source param (vec0|trigram|none)"
```

---

### Task 3: Config rename — `tfidf` → `sqlite`

**Files:**
- Modify: `better_memory/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Update the config tests**

In `tests/test_config.py`, find the three `test_embeddings_backend_*` tests added in PR #65. Rename `tfidf` to `sqlite` everywhere they appear:

```python
def test_embeddings_backend_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", raising=False)
    cfg = get_config()
    assert cfg.embeddings_backend == "ollama"


def test_embeddings_backend_sqlite_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    cfg = get_config()
    assert cfg.embeddings_backend == "sqlite"


def test_embeddings_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "tfidf")  # was valid in PR #65, now invalid
    with pytest.raises(ValueError, match="BETTER_MEMORY_EMBEDDINGS_BACKEND"):
        get_config()
```

Renaming the test function name (`test_embeddings_backend_tfidf_when_env_set` → `_sqlite_when_env_set`) is required.

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_config.py -k embeddings_backend -v
```
Expected: FAIL — `embeddings_backend` resolves to `"tfidf"`-class behaviour or the unknown-value test no longer catches `tfidf`.

- [ ] **Step 3: Update `config.py`**

Find and update three places in `better_memory/config.py`:

```python
# Constants (near the existing defaults)
_VALID_EMBEDDINGS_BACKENDS = ("ollama", "sqlite")  # was ("ollama", "tfidf")

# Config dataclass
embeddings_backend: Literal["ollama", "sqlite"]  # was Literal["ollama", "tfidf"]

# _resolve_embeddings_backend return annotation
def _resolve_embeddings_backend() -> Literal["ollama", "sqlite"]:
    raw = os.environ.get("BETTER_MEMORY_EMBEDDINGS_BACKEND", _DEFAULT_EMBEDDINGS_BACKEND)
    if raw not in _VALID_EMBEDDINGS_BACKENDS:
        raise ValueError(
            f"BETTER_MEMORY_EMBEDDINGS_BACKEND must be one of "
            f"{_VALID_EMBEDDINGS_BACKENDS}, got {raw!r}"
        )
    return raw  # type: ignore[return-value]
```

Also update the module-level docstring's external-service-knob list (added in PR #65 Task 10) to use the new value.

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_config.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add better_memory/config.py tests/test_config.py
git commit -m "refactor(config): rename embeddings backend value tfidf -> sqlite"
```

---

### Task 4: `ObservationService` — drop retriever, branch via `second_source`

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `better_memory/embeddings/__init__.py`
- Rename: `tests/services/test_observation_tfidf.py` → `tests/services/test_observation_sqlite.py`

Confidence lift: full method diff shown below; existing retriever-related branches removed in-place to keep the diff readable.

- [ ] **Step 1: Update `embeddings/__init__.py`**

Edit `better_memory/embeddings/__init__.py`:

```python
"""Embedding clients for better-memory."""

from better_memory.embeddings.ollama import EmbeddingError, OllamaEmbedder

__all__ = ["OllamaEmbedder", "EmbeddingError"]
```

(Removes the `TfidfRetriever` import and export — see Task 6 for the file deletion.)

- [ ] **Step 2: Rename the service test file and simplify**

Move `tests/services/test_observation_tfidf.py` → `tests/services/test_observation_sqlite.py` (use `git mv` so history is preserved).

Inside the renamed file, remove the import of `TfidfRetriever` and update fixtures. The `service` fixture becomes:

```python
@pytest.fixture
def service(conn: sqlite3.Connection, fixed_clock) -> ObservationService:
    episodes = EpisodeService(conn, clock=fixed_clock)
    return ObservationService(
        conn,
        embedder=None,
        clock=fixed_clock,
        project_resolver=lambda: "p",
        scope_resolver=lambda: None,
        session_id="sess",
        episodes=episodes,
    )
```

Delete the `test_create_requires_exactly_one_of_embedder_retriever` test — the XOR check is removed in step 4.

Update the assertion in `test_create_indexes_into_retriever` to assert instead that the row appears in `observation_trigram_fts` after create:

```python
async def test_create_populates_trigram_index(
    service: ObservationService, conn: sqlite3.Connection
) -> None:
    obs_id = await service.create(content="unique-marker-xyz", outcome="neutral")
    row = conn.execute(
        "SELECT rowid FROM observation_trigram_fts WHERE "
        "observation_trigram_fts MATCH 'marker'"
    ).fetchone()
    assert row is not None
```

Keep `test_create_skips_vec0_insert_in_tfidf_mode` (rename to `_in_sqlite_mode`). Keep `test_retrieve_returns_bucketed_results_in_tfidf_mode` (rename to `_in_sqlite_mode`). Keep `test_list_observations_query_mode_in_tfidf` (rename to `_in_sqlite`).

- [ ] **Step 3: Run renamed tests to verify they fail**

```
uv run pytest tests/services/test_observation_sqlite.py -v
```
Expected: FAIL — `ObservationService.__init__()` got an unexpected keyword argument `retriever` (because the fixture no longer passes it but the service still expects it), OR the retrieve test fails because the service tries to call `self._embedder.embed(None)`.

- [ ] **Step 4: Simplify `ObservationService.__init__`**

Edit `better_memory/services/observation.py`. Restore the simpler constructor (drop the `retriever` kwarg + XOR check added in PR #65):

```python
def __init__(
    self,
    conn: sqlite3.Connection,
    embedder: Any = None,
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
    # ... rest of init unchanged
```

Remove the `self._retriever = retriever` line and the entire `if (embedder is None) == (retriever is None): raise ValueError(...)` block.

- [ ] **Step 5: Simplify `create()` — drop the post-commit retriever path**

In `create()`, find the post-commit block (added in PR #65 Task 6, around line 300):

```python
            # Post-commit indexing for the TF-IDF backend...
            if self._retriever is not None:
                try:
                    self._retriever.add_doc(obs_id, content)
                except Exception:
                    _diag.step(fn, "retriever_add_doc_failed")
```

Delete this block entirely. The FTS5 triggers handle indexing automatically.

The `vec_blob: bytes | None = None` guard around the embed and vec0 INSERT stays — it's correct for `embedder is None` (sqlite mode).

- [ ] **Step 6: Branch `retrieve()` via `second_source`**

In `retrieve()`, find the inner `_run()` closure (modified in PR #65 Task 7). Replace its body to use `second_source`:

```python
        def _run(outcome: Outcome, limit: int) -> list[SearchResult]:
            filters = SearchFilters(outcome=outcome, **base_kwargs)
            second_source = "vec0" if self._embedder is not None else "trigram"
            return hybrid_search(
                self._conn,
                query_text=fts_query_text,
                query_vector=query_vector,
                second_source=second_source,
                filters=filters,
                limit=limit,
                candidate_k=candidate_k,
                reinforcement_alpha=reinforcement_alpha,
                clock=self._clock,
            )
```

Remove the inline `from better_memory.search.tfidf_search import tfidf_search` and the duplicated `tfidf_search` call branch — `hybrid_search` now handles both.

The embed-skip guard `if query is not None and query.strip() and self._embedder is not None: query_vector = await self._embedder.embed(query)` stays as-is (correct for sqlite mode where `_embedder is None`).

- [ ] **Step 7: Branch `_list_observations_via_hybrid_search` via `second_source`**

Same pattern as Step 6. Find the method (modified in PR #65 Task 8). Replace with:

```python
    async def _list_observations_via_hybrid_search(
        self,
        *,
        project: str,
        component: str | None,
        outcome: Outcome | None,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        fts_query_text = sanitize_fts5_query(query) or None
        filters = SearchFilters(
            project=project,
            component=component,
            outcome=outcome,
            status=None,
            window_days=None,
        )
        second_source = "vec0" if self._embedder is not None else "trigram"
        vector: list[float] | None = None
        if self._embedder is not None:
            vector = await self._embedder.embed(query)
        results = hybrid_search(
            self._conn,
            query_text=fts_query_text,
            query_vector=vector,
            second_source=second_source,
            filters=filters,
            limit=limit,
            clock=self._clock,
        )
        return [
            {
                "id": r.id,
                "content": r.content,
                "component": r.component,
                "theme": r.theme,
                "outcome": r.outcome,
                "reinforcement_score": r.reinforcement_score,
                "created_at": r.created_at,
            }
            for r in results
        ]
```

(Verify the trailing dict keys match the existing implementation; copy from the file rather than from this snippet if different.)

- [ ] **Step 8: Run tests**

```
uv run pytest tests/services/test_observation_sqlite.py tests/services/test_observation.py -v
```
Expected: all pass.

- [ ] **Step 9: Run the full service suite for regressions**

```
uv run pytest tests/services -v
```

- [ ] **Step 10: Commit**

```
git add better_memory/services/observation.py better_memory/embeddings/__init__.py tests/services/test_observation_sqlite.py tests/services/test_observation_tfidf.py
git commit -m "refactor(observation): drop Python TF-IDF retriever, route via second_source"
```

---

### Task 5: `mcp/server.py` — drop TfidfRetriever wiring

**Files:**
- Modify: `better_memory/mcp/server.py`
- Rename: `tests/mcp/test_server_tfidf.py` → `tests/mcp/test_server_sqlite.py`

- [ ] **Step 1: Rename + update the MCP test**

Move `tests/mcp/test_server_tfidf.py` → `tests/mcp/test_server_sqlite.py` (via `git mv`).

Update the test to set the new env var value and assert the sqlite backend boots without Ollama:

```python
async def test_server_builds_in_sqlite_mode_without_ollama(
    tmp_memory_db: Path, tmp_knowledge_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")  # was "tfidf"
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
    monkeypatch.setenv("OLLAMA_HOST", "http://does-not-exist.invalid:1")

    # ... rest unchanged (the dead-host trick + _dispatch_for_tests assertion)
```

- [ ] **Step 2: Run renamed test to verify failure**

```
uv run pytest tests/mcp/test_server_sqlite.py -v
```
Expected: FAIL — the env var `sqlite` is not yet accepted at this point of the plan ordering (Task 3 has already accepted it; this should pass if Tasks 1-4 are merged). Actually expected to pass on first run, with Task 5's changes being a simplification. If it passes, that's fine — proceed to Step 3 to simplify the implementation.

- [ ] **Step 3: Drop `TfidfRetriever` from `mcp/server.py`**

Find the import block (~line 61-62):

```python
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.embeddings.tfidf import TfidfRetriever  # DELETE THIS LINE
```

Remove the second import.

Find the build_server block (around line 934, modified in PR #65 Task 9):

```python
    # Build embedder OR retriever depending on the configured backend.
    embedder = None
    retriever = None
    if config.embeddings_backend == "ollama":
        embedder = OllamaEmbedder()
        _probe_ollama(config.ollama_host)
    else:
        retriever = TfidfRetriever(memory_conn)
        retriever.fit_from_db()
```

Replace with:

```python
    # Embedder is only built for the ollama backend. For sqlite, FTS5
    # triggers handle indexing and no embedder is needed.
    embedder: OllamaEmbedder | None = None
    if config.embeddings_backend == "ollama":
        embedder = OllamaEmbedder()
        _probe_ollama(config.ollama_host)
```

Find the `ObservationService` construction:

```python
    observations = ObservationService(
        memory_conn, embedder=embedder, retriever=retriever, episodes=episodes
    )
```

Replace with:

```python
    observations = ObservationService(memory_conn, embedder=embedder, episodes=episodes)
```

The cleanup function's `if embedder is not None: await embedder.aclose()` guard stays as-is.

- [ ] **Step 4: Run tests**

```
uv run pytest tests/mcp -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add better_memory/mcp/server.py tests/mcp/test_server_sqlite.py tests/mcp/test_server_tfidf.py
git commit -m "refactor(mcp): drop TfidfRetriever wiring; sqlite backend uses FTS5 triggers"
```

---

### Task 6: Delete Python TF-IDF files + tests

**Files:**
- Delete: `better_memory/embeddings/tfidf.py`
- Delete: `better_memory/search/tfidf_search.py`
- Delete: `tests/embeddings/test_tfidf_unit.py`
- Delete: `tests/search/test_tfidf_search.py`

- [ ] **Step 1: Verify no remaining imports of the deleted modules**

```
git grep -n "from better_memory.embeddings.tfidf"
git grep -n "from better_memory.search.tfidf_search"
git grep -n "TfidfRetriever"
git grep -n "tfidf_search"
```
Expected: no matches (Tasks 4 and 5 removed all consumers).

- [ ] **Step 2: Delete the files**

```
git rm better_memory/embeddings/tfidf.py
git rm better_memory/search/tfidf_search.py
git rm tests/embeddings/test_tfidf_unit.py
git rm tests/search/test_tfidf_search.py
```

- [ ] **Step 3: Run the full test suite to confirm no orphan references**

```
uv run pytest --ignore=tests/ui -q 2>&1 | tail -10
```
Expected: all green.

- [ ] **Step 4: Commit**

```
git commit -m "chore: delete Python TF-IDF backend (superseded by trigram-FTS5)"
```

---

### Task 7: Documentation + SUPERSEDED notices

**Files:**
- Modify: `README.md`
- Modify: `website/configuration.md`
- Modify: `website/architecture.md`
- Modify: `docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md` (SUPERSEDED notice)
- Modify: `docs/superpowers/plans/2026-05-22-tfidf-embeddings-backend.md` (SUPERSEDED notice)

- [ ] **Step 1: Update README env-var table**

In `README.md`, find the row added in PR #65 Task 10:

```
| `BETTER_MEMORY_EMBEDDINGS_BACKEND` | `ollama` (default) — local Ollama at `OLLAMA_HOST`; `tfidf` — in-memory TF-IDF, stdlib only, no model downloads. |
```

Replace with:

```
| `BETTER_MEMORY_EMBEDDINGS_BACKEND` | `ollama` (default) — local Ollama at `OLLAMA_HOST`; `sqlite` — pure-SQL trigram-FTS5 fusion, no model downloads and no in-memory state. |
```

Update the prose Requirements bullet too (if it references `tfidf`).

- [ ] **Step 2: Update `website/configuration.md`**

Find the corresponding env-var table row and update value + description to match the README.

- [ ] **Step 3: Rewrite `website/architecture.md` "Embeddings backends" section**

Find the section added in PR #65 Task 10. Replace the `tfidf` paragraph with:

```markdown
**`sqlite`.** A second FTS5 virtual table (`observation_trigram_fts`,
tokenizer=`trigram`) is populated by triggers alongside the existing
`observation_fts` (word tokenizer). Retrieval fuses both via RRF — no
external service, no model downloads, no in-memory state. Lower
recall on long paraphrased queries than Ollama embeddings, but
substring and morphology bridging via trigrams works well for
keyword-dense observations.
```

Update the trailing paragraph about backend-switching to mention that both FTS5 tables are always populated, so backend switches require no data migration at runtime (only the vec0 half degrades for cross-backend rows).

- [ ] **Step 4: Update `config.py` module docstring**

In `better_memory/config.py`, find the docstring's external-service-knob list (updated in PR #65 Task 10) and change `tfidf` to `sqlite`.

- [ ] **Step 5: Add SUPERSEDED notices to the prior spec and plan**

In `docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md`, insert at the top (line 2, after the H1):

```markdown
> **SUPERSEDED 2026-05-23** by `2026-05-23-trigram-fts5-backend-design.md` (replaced the Python TF-IDF backend with SQL-only trigram-FTS5).
```

Same in `docs/superpowers/plans/2026-05-22-tfidf-embeddings-backend.md`:

```markdown
> **SUPERSEDED 2026-05-23** by `2026-05-23-trigram-fts5-backend.md`.
```

- [ ] **Step 6: Sanity-check the docs build**

```
uv run --group docs mkdocs build 2>&1 | tail -10
```
Expected: clean build.

- [ ] **Step 7: Commit**

```
git add README.md website/configuration.md website/architecture.md better_memory/config.py docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md docs/superpowers/plans/2026-05-22-tfidf-embeddings-backend.md
git commit -m "docs: rename tfidf -> sqlite, supersede prior TF-IDF spec/plan"
```

---

## Self-Review

**Spec coverage:**
- Migration 0011 (trigram table + triggers + backfill) → Task 1 ✓
- `hybrid_search` `second_source` parameter + `_trigram_candidates` → Task 2 ✓
- Config rename `tfidf` → `sqlite` → Task 3 ✓
- `ObservationService` simplification (drop retriever + post-commit add_doc + branch via second_source) → Task 4 ✓
- `mcp/server.py` simplification → Task 5 ✓
- Deletes (embeddings/tfidf.py, search/tfidf_search.py, tests) → Task 6 ✓
- Docs (README, website/configuration.md, website/architecture.md, config.py docstring) → Task 7 ✓
- SUPERSEDED notices on prior spec + plan → Task 7 ✓
- Error handling: ValueError on unknown backend (Task 3 test), migration failure on SQLite < 3.34 (documented in spec; falls out of CREATE VIRTUAL TABLE error), empty query short-circuit (preserved in Task 2's hybrid_search guard) ✓

**Placeholder scan:** No "TBD"/"TODO"/"implement later" patterns. One "verify dict keys match the existing implementation" instruction in Task 4 Step 7 — this is a defensive plan-time check (the spec is concrete; we just need to mirror the file's existing shape), not a placeholder.

**Type consistency:**
- `second_source: Literal["vec0", "trigram", "none"]` consistent in Task 2 (definition) and Task 4 (caller).
- `ObservationService(conn, embedder=None, *, ..., episodes=episodes)` consistent between Task 4 (constructor change) and Task 5 (server.py caller).
- `embeddings_backend: Literal["ollama", "sqlite"]` consistent between Task 3 and the spec.

No gaps or inconsistencies found.
