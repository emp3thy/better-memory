# Retrieval Quality PR-A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace popularity ranking with a Wilson-score prior + exploration slot, and add reflection/semantic embeddings with three-leg RRF query fusion.

**Architecture:** SQL keeps filtering; ranking moves to Python (`services/scoring.py`). Sync services reach Ollama through a new `SyncEmbedder` (fresh embedder per bridge-worker call — `httpx.AsyncClient` is loop-bound — with a 60s circuit breaker). Query relevance fuses prior rank + BM25 rank + vec-kNN rank via RRF. Every embedding path is best-effort and degrades to the shipped BM25-only behaviour.

**Tech Stack:** Python 3.12, sqlite + sqlite-vec (vec0), FTS5, Ollama `nomic-embed-text` (768-dim), pytest.

**Spec:** `docs/superpowers/specs/2026-07-23-retrieval-quality-design.md`

## Global Constraints

- Branch: `feat/retrieval-quality` (exists; spec `604fb79`).
- Test command: `./.venv/Scripts/python.exe -m pytest <path> -v` from repo root (full suite: `./.venv/Scripts/python.exe -m pytest tests -q`).
- Typecheck: `./.venv/Scripts/python.exe -m pyright` stays at 0 errors.
- Every Ollama touchpoint: best-effort, never raises into the caller.
- `sqlite3.Connection` is thread-bound: embeds happen on the bridge worker; ALL DB I/O stays on the caller thread.
- Vector serialisation is `sqlite_vec.serialize_float32(vector)` everywhere (the proven call from `observation.py:206`); `db/connection.py` loads the extension on every connect, so vec0 works in tests and CLI without ceremony.
- `async_bridge.run_async_in_worker(coro_factory, *, timeout=None)` takes a **factory invoked inside the worker thread** — resources must be constructed inside it.
- Ruff line length 100. Website-sync guardrail applies (conf 1.0 reflection).
- Commit after every task; footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Verified-against-source facts (do not re-derive)

| Fact | Where verified |
|---|---|
| `sqlite_vec.serialize_float32` is the vec0 write format | `services/observation.py:206` |
| kNN SQL: `WHERE embedding MATCH ? AND k = ? ORDER BY distance`, then filter ids in Python (no extra predicates allowed) | `search/hybrid.py:274-296` |
| Deleting vec0 rows is routine | `services/retention.py:250` |
| `OllamaEmbedder.__init__` builds `httpx.AsyncClient` immediately; client is loop-bound; ctor does NOT contact Ollama; `aclose()` closes owned clients | `embeddings/ollama.py:52-107` |
| `run_async_in_worker` signature + fresh-loop-per-call semantics | `async_bridge.py:27-60` |
| `_apply_new` writes title/use_cases/hints from `NewAction` fields directly | `reflection.py:718-775` |
| `_apply_augment` SELECTs only `hints, confidence, status` today; UPDATE branches on `rewrite_use_cases`; title never changes | `reflection.py:817-919` |
| `_apply_merge` changes NO text on the target (counters/evidence only); source becomes `superseded` | `reflection.py:1000-1040` |
| Action dataclasses: `NewAction(title, phase, polarity, use_cases, hints, tech, confidence, source_observation_ids)` etc. | `reflection.py:153-180` |

---

### Task 1: Wilson scoring module

**Files:**
- Create: `better_memory/services/scoring.py`
- Test: `tests/services/test_scoring.py`

**Interfaces:**
- Produces: `wilson_lower_bound(positive: int, n: int, z: float = 1.96) -> float` — 0.0 when `n == 0`; clamped to [0, 1]. Consumed by Tasks 2, 3, 4.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_scoring.py
"""Wilson lower bound: the ranking prior for reflections + semantic memories.

Chosen over raw useful_count because raw counts are rich-get-richer: 67
useful over 192 rated sessions (35% hit rate) permanently outranked 3/4
(75%). The lower bound rewards hit rate while discounting small samples.
"""
from __future__ import annotations

import pytest

from better_memory.services.scoring import wilson_lower_bound


class TestWilsonLowerBound:
    def test_no_data_scores_zero(self):
        assert wilson_lower_bound(0, 0) == 0.0

    def test_proven_dead_weight_scores_near_zero(self):
        assert wilson_lower_bound(0, 58) == pytest.approx(0.0, abs=1e-9)

    def test_high_hit_rate_newcomer_beats_popular_workhorse(self):
        # The design's worked example: 3/4 (75%) must outrank 67/192 (35%).
        assert wilson_lower_bound(3, 4) > wilson_lower_bound(67, 192)

    def test_worked_example_values(self):
        assert wilson_lower_bound(67, 192) == pytest.approx(0.285, abs=0.005)
        assert wilson_lower_bound(3, 4) == pytest.approx(0.301, abs=0.005)

    def test_monotonic_in_positives_at_fixed_n(self):
        scores = [wilson_lower_bound(k, 10) for k in range(11)]
        assert scores == sorted(scores)
        assert scores[0] < scores[10]

    def test_more_evidence_at_same_rate_scores_higher(self):
        assert wilson_lower_bound(30, 40) > wilson_lower_bound(3, 4)

    def test_never_negative_and_never_above_one(self):
        for positive, n in [(0, 1), (1, 1), (1, 1000), (999, 1000)]:
            assert 0.0 <= wilson_lower_bound(positive, n) <= 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'better_memory.services.scoring'`

- [ ] **Step 3: Write the implementation**

```python
# better_memory/services/scoring.py
"""Ranking prior for reflections and semantic memories.

One function, one job: the Wilson score lower bound on the proportion of
rated exposures where the memory was positive (useful or overlooked).
Replaces the raw-count ORDER BY stack (useful_count + overlooked weight +
ignored-demotion CASE) — see the 2026-07-23 retrieval-quality spec §1.

Computed in Python, not SQL: SQLite's sqrt() requires the math extension,
the candidate sets are tiny (~150 rows), and a pure function is testable
against closed-form values.
"""

from __future__ import annotations

import math

#: 95% confidence. Pinned — changing z reorders every list; treat as part
#: of the scoring contract, not a tunable.
WILSON_Z = 1.96


def wilson_lower_bound(positive: int, n: int, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for positive/n.

    ``n == 0`` (never rated) returns 0.0 — untested memories score at the
    bottom and are surfaced by the exploration slot instead (spec §2).
    """
    if n <= 0:
        return 0.0
    p = positive / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, (centre - margin) / denom)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_scoring.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/scoring.py tests/services/test_scoring.py
git commit -m "feat(scoring): Wilson lower bound ranking prior

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Reflections ranked by Wilson score

**Files:**
- Modify: `better_memory/services/reflection.py` (retrieve_reflections ~1256-1400)
- Modify: `better_memory/services/memory_rating.py` (delete the three ranking constants)
- Delete: `tests/services/test_ignored_demotion.py`, `tests/services/test_useful_count_ranking.py` (both pin the superseded ordering; scenarios covered below)
- Test: `tests/services/test_wilson_ranking.py`

**Interfaces:**
- Consumes: `wilson_lower_bound` (Task 1).
- Produces: rows include `times_overlooked` + `times_ignored`; SQL no longer orders; Python sorts `(wilson DESC, confidence DESC, updated_at DESC)`. Module helpers `_wilson_rated(row) -> int`, `_wilson_score(row) -> float` (Task 4 reuses `_wilson_rated`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_wilson_ranking.py
"""Retrieval ranks by Wilson lower bound on (useful+overlooked)/rated.

Replaces the popularity + overlooked-weight + ignored-demotion stack.
Covers the old demotion scenarios too: proven dead weight sinks because
0 positive over many rated gives LB ~ 0, with no special-case code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionSynthesisService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid, *, useful=0, overlooked=0, ignored=0, confidence=0.5,
          polarity="do", updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count,
            times_overlooked, times_ignored)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?, ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful, overlooked, ignored),
    )
    conn.commit()


def _ids(conn, **kw):
    svc = ReflectionSynthesisService(conn)
    return [r["id"] for r in svc.retrieve_reflections(project="p", **kw)["do"]]


class TestWilsonOrdering:
    def test_hit_rate_beats_raw_count(self, conn):
        _seed(conn, "r-workhorse", useful=67, ignored=125)      # 67/192 ~ 0.28
        _seed(conn, "r-newcomer", useful=3, ignored=1)          # 3/4  ~ 0.30
        assert _ids(conn)[:2] == ["r-newcomer", "r-workhorse"]

    def test_overlooked_counts_as_positive(self, conn):
        _seed(conn, "r-overlooked", overlooked=3, ignored=1)
        _seed(conn, "r-plain", useful=1, ignored=3)
        assert _ids(conn)[0] == "r-overlooked"

    def test_proven_dead_weight_sinks_below_modest_performer(self, conn):
        _seed(conn, "r-dead", useful=0, ignored=58)
        _seed(conn, "r-modest", useful=2, ignored=8)
        assert _ids(conn) == ["r-modest", "r-dead"]

    def test_confidence_breaks_wilson_ties(self, conn):
        _seed(conn, "r-low-conf", confidence=0.3)
        _seed(conn, "r-high-conf", confidence=0.9)
        assert _ids(conn) == ["r-high-conf", "r-low-conf"]

    def test_recency_breaks_confidence_ties(self, conn):
        _seed(conn, "r-old", updated_at="2026-01-01")
        _seed(conn, "r-new", updated_at="2026-06-01")
        assert _ids(conn) == ["r-new", "r-old"]

    def test_rows_expose_all_three_counters(self, conn):
        _seed(conn, "r-a", useful=1, overlooked=2, ignored=3)
        svc = ReflectionSynthesisService(conn)
        row = svc.retrieve_reflections(project="p")["do"][0]
        assert row["useful_count"] == 1
        assert row["times_overlooked"] == 2
        assert row["times_ignored"] == 3

    def test_demotion_constants_are_gone(self):
        import better_memory.services.memory_rating as mr
        for name in ("IGNORED_DEMOTION_FLOOR", "IGNORED_DEMOTION_WEIGHT",
                     "OVERLOOKED_RANKING_WEIGHT"):
            assert not hasattr(mr, name), f"{name} should be deleted"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_wilson_ranking.py -v`
Expected: FAIL — ordering assertions and `test_demotion_constants_are_gone`.

- [ ] **Step 3: Implement**

3a. Imports in `reflection.py` — replace the constants import with:

```python
from better_memory.search.query import sanitize_fts5_query
from better_memory.services.scoring import wilson_lower_bound
```

3b. Module helpers above the service class:

```python
def _wilson_rated(row) -> int:
    """Total rated exposures backing this memory's score."""
    return (row["useful_count"] + row["times_overlooked"]
            + row["times_ignored"])


def _wilson_score(row) -> float:
    positive = row["useful_count"] + row["times_overlooked"]
    return wilson_lower_bound(positive, _wilson_rated(row))
```

3c. In `retrieve_reflections`: delete the two demotion `params.append(...)` lines; SELECT gains `times_overlooked, times_ignored`; ORDER BY removed; Python sort after fetch:

```python
            rows = self._conn.execute(
                f"""
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count, useful_count,
                       times_misled, times_overlooked, times_ignored,
                       updated_at
                FROM reflections
                WHERE {where}
                """,
                params,
            ).fetchall()
            _diag.step(fn, "select_done", n_rows=len(rows))

            # Rank in Python: Wilson prior, then confidence, then recency.
            # Chained stable sorts apply tiebreakers lowest-priority first.
            rows = list(rows)
            rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
            rows.sort(key=lambda r: r["confidence"], reverse=True)
            rows.sort(key=_wilson_score, reverse=True)
```

3d. Bucket item dicts gain the two counters:

```python
                    "times_overlooked": r["times_overlooked"],
                    "times_ignored": r["times_ignored"],
```

3e. `memory_rating.py`: delete `OVERLOOKED_RANKING_WEIGHT`, `IGNORED_DEMOTION_FLOOR`, `IGNORED_DEMOTION_WEIGHT` and their comments. Delete the two superseded test files.

- [ ] **Step 4: Run the affected tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_wilson_ranking.py tests/services/test_reflection_query_relevance.py tests/services/test_reflection_retrieve_fields.py tests/mcp -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(ranking): reflections ranked by Wilson prior, demotion stack deleted

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Semantic memories ranked by Wilson score

**Files:**
- Modify: `better_memory/services/semantic.py` (imports ~19-23; dataclass ~26-42; list_for_project ~237-260)
- Test: `tests/services/test_semantic.py` (append class)

**Interfaces:**
- Consumes: `wilson_lower_bound` (Task 1).
- Produces: `SemanticMemory` gains `times_ignored: int = 0`, `last_ignored_at: str | None = None`; `list_for_project` ordered `(wilson DESC, created_at DESC)`.

- [ ] **Step 1: Write the failing test** — append to `tests/services/test_semantic.py`, reusing its existing `conn` fixture/helpers (read the file's fixture name first; monkeypatch the service clock if creation timestamps collide, as neighbouring tests there do):

```python
class TestSemanticWilsonRanking:
    def test_hit_rate_beats_raw_count(self, conn):
        svc = SemanticMemoryService(conn)
        a = svc.create(content="workhorse", project="p")
        b = svc.create(content="newcomer", project="p")
        conn.execute(
            "UPDATE semantic_memories SET useful_count=67, times_ignored=125 WHERE id=?", (a,))
        conn.execute(
            "UPDATE semantic_memories SET useful_count=3, times_ignored=1 WHERE id=?", (b,))
        conn.commit()
        ids = [m.id for m in svc.list_for_project(project="p", track_exposure=False)]
        assert ids == [b, a]

    def test_never_rated_sorts_by_recency_at_bottom(self, conn):
        svc = SemanticMemoryService(conn)
        rated = svc.create(content="rated", project="p")
        conn.execute(
            "UPDATE semantic_memories SET useful_count=1, times_ignored=1 WHERE id=?", (rated,))
        conn.commit()
        unrated = svc.create(content="unrated", project="p")
        ids = [m.id for m in svc.list_for_project(project="p", track_exposure=False)]
        assert ids == [rated, unrated]
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic.py -v -k Wilson`
Expected: FAIL.

- [ ] **Step 3: Implement** — imports: `from better_memory.services.scoring import wilson_lower_bound` (drop the old constants import); dataclass fields added; SELECT gains `times_ignored, last_ignored_at`; ORDER BY block + its `params.append` lines replaced with `"ORDER BY created_at DESC"`; after building `results`:

```python
        results.sort(
            key=lambda m: wilson_lower_bound(
                m.useful_count + m.times_overlooked,
                m.useful_count + m.times_overlooked + m.times_ignored,
            ),
            reverse=True,
        )
```

(Stable sort keeps `created_at DESC` inside equal scores.)

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic.py tests/services/test_session_bootstrap.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/semantic.py tests/services/test_semantic.py
git commit -m "feat(ranking): semantic memories ranked by Wilson prior

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Exploration slot

**Files:**
- Modify: `better_memory/services/reflection.py` (bucket-fill in retrieve_reflections)
- Test: `tests/services/test_exploration_slot.py`

**Interfaces:**
- Consumes: `_wilson_rated` (Task 2). `EXPLORATION_RATED_FLOOR = 3` constant in `reflection.py`.
- Produces: per bucket, up to `cap-1` tested rows + best untested row (ranked order); no untested ⇒ plain fill; `cap None` ⇒ no reservation. `_bucket_item(self, r) -> dict` extracted so both fill paths share the dict literal.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_exploration_slot.py
"""One shortlist slot per bucket is reserved for an untested memory.

Untested = fewer than 3 rated exposures. Wilson scores them 0.0, so
without the slot they would never be served and never earn a rating.
Rating coverage is ~100% (sync Stop hook), so one serve = one rating.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionSynthesisService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid, *, useful=0, ignored=0, updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', ?, ?, ?)""",
        (rid, rid, updated_at, useful, ignored),
    )
    conn.commit()


def _ids(conn, cap=3):
    svc = ReflectionSynthesisService(conn)
    return [r["id"] for r in
            svc.retrieve_reflections(project="p", limit_per_bucket=cap)["do"]]


class TestExplorationSlot:
    def test_last_slot_goes_to_best_untested(self, conn):
        for i in range(4):                       # four proven memories
            _seed(conn, f"r-proven-{i}", useful=5 - i, ignored=5)
        _seed(conn, "r-untested-old", updated_at="2026-02-01")
        _seed(conn, "r-untested-new", updated_at="2026-06-01")
        ids = _ids(conn, cap=3)
        assert len(ids) == 3
        assert ids[:2] == ["r-proven-0", "r-proven-1"]      # cap-1 proven
        assert ids[2] == "r-untested-new"                    # best untested

    def test_no_untested_fills_all_slots_with_proven(self, conn):
        for i in range(4):
            _seed(conn, f"r-proven-{i}", useful=5 - i, ignored=5)
        ids = _ids(conn, cap=3)
        assert ids == ["r-proven-0", "r-proven-1", "r-proven-2"]

    def test_all_untested_fills_normally(self, conn):
        for i in range(4):
            _seed(conn, f"r-untested-{i}")
        assert len(_ids(conn, cap=3)) == 3

    def test_two_ratings_is_still_untested_three_is_not(self, conn):
        _seed(conn, "r-two", useful=1, ignored=1)     # rated == 2: untested
        _seed(conn, "r-three", useful=1, ignored=2)   # rated == 3: tested
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=9, ignored=1)
        ids = _ids(conn, cap=3)
        assert ids[2] == "r-two"

    def test_unlimited_cap_reserves_nothing(self, conn):
        _seed(conn, "r-proven", useful=5, ignored=5)
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        rows = svc.retrieve_reflections(project="p", limit_per_bucket=None)["do"]
        assert len(rows) == 2                        # everything returned anyway
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_exploration_slot.py -v`
Expected: `test_last_slot_goes_to_best_untested` and `test_two_ratings_is_still_untested_three_is_not` FAIL.

- [ ] **Step 3: Implement**

Constant near `_wilson_score`:

```python
#: A memory with fewer than this many rated exposures is "untested": its
#: Wilson score is statistically meaningless, so it competes for the
#: reserved exploration slot instead of the proven slots.
EXPLORATION_RATED_FLOOR = 3
```

Extract the existing `bucket.append({...})` literal into `_bucket_item(self, r) -> dict` (now including the Task 2 counters). Replace the fill loop with the two-pass version (`rows` is already in final ranked order — Wilson sort, then optional fusion — so "best untested" = first untested in `rows`; index-based selection avoids `in`-list identity comparisons entirely):

```python
            cap = limit_per_bucket if limit_per_bucket is not None else sys.maxsize
            reserve = limit_per_bucket is not None and cap >= 2
            buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
            by_polarity: dict[str, list] = {"do": [], "dont": [], "neutral": []}
            for r in rows:
                by_polarity[r["polarity"]].append(r)
            for polarity, group in by_polarity.items():
                if not reserve:
                    buckets[polarity] = [self._bucket_item(r) for r in group[:cap]]
                    continue
                tested_idx = [i for i, r in enumerate(group)
                              if _wilson_rated(r) >= EXPLORATION_RATED_FLOOR]
                untested_idx = [i for i, r in enumerate(group)
                                if _wilson_rated(r) < EXPLORATION_RATED_FLOOR]
                chosen = tested_idx[: cap - 1]
                if untested_idx:
                    chosen.append(untested_idx[0])
                if len(chosen) < cap:               # top up from the remainder
                    taken = set(chosen)
                    for i in range(len(group)):
                        if len(chosen) >= cap:
                            break
                        if i not in taken:
                            chosen.append(i)
                chosen.sort()                        # preserve ranked order
                buckets[polarity] = [self._bucket_item(group[i]) for i in chosen]
```

Update the `_diag.step(fn, "bucketed", ...)` counts to read from the new dict.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_exploration_slot.py tests/services/test_wilson_ranking.py tests/services/test_reflection_query_relevance.py tests/services/test_reflection.py tests/services/test_reflection_retrieve_fields.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(ranking): reserved exploration slot per bucket for untested memories

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Migration 0014 — semantic_embeddings table

**Files:**
- Create: `better_memory/db/migrations/0014_semantic_embeddings.sql`
- Test: append to `tests/db/test_schema.py` (mirror the newest migration's existing test style)

**Interfaces:**
- Produces: vec0 table `semantic_embeddings(memory_id TEXT PRIMARY KEY, embedding FLOAT[768])`. Consumed by Tasks 8-10.

- [ ] **Step 1: Write the failing test**

```python
def test_0014_semantic_embeddings_table(tmp_memory_db):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='semantic_embeddings'"
    ).fetchone()
    assert row is not None
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/db -q -k 0014`
Expected: FAIL.

- [ ] **Step 3: Write the migration**

```sql
-- Migration 0014: vector index for semantic memories.
--
-- reflection_embeddings has existed since 0002; semantic memories never got
-- the parallel table, so they have no semantic-search substrate. Written by
-- SemanticMemoryService on create/update, healed lazily on retrieve, and
-- backfilled once by cli.backfill_embeddings. 768 dims = nomic-embed-text,
-- matching observation_embeddings and reflection_embeddings.

CREATE VIRTUAL TABLE semantic_embeddings USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);
```

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/db -q`
Expected: all pass (update any migration-count fixture if one exists).

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0014_semantic_embeddings.sql tests/db
git commit -m "feat(db): semantic_embeddings vec0 table (migration 0014)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: SyncEmbedder — bridge + fresh-embedder-per-call + circuit breaker

**Files:**
- Create: `better_memory/embeddings/sync_embed.py`
- Create: `tests/services/_embedding_fakes.py`
- Test: `tests/embeddings/test_sync_embed.py`

**Interfaces:**
- Consumes: `run_async_in_worker` (factory semantics), any object with async `embed(text)` / `embed_batch(texts)` and optional async `aclose()`.
- Produces: `SyncEmbedder(factory, *, clock=time.monotonic, cooldown=60.0, timeout=15.0)` with `embed_text(text) -> list[float] | None` and `embed_batch(texts) -> list[list[float]] | None`. Any failure opens a `cooldown`-second breaker during which calls return None instantly. Consumed by Tasks 7, 8, 9.

**Why fresh-embedder-per-call:** `OllamaEmbedder.__init__` builds an `httpx.AsyncClient`, and AsyncClient is bound to the event loop that first uses it (recorded project gotcha). `run_async_in_worker` creates a fresh loop per call, so the coroutine must construct its own embedder inside the worker and close it there. Construction is cheap and does not contact Ollama (`ollama.py` docstring).

- [ ] **Step 1: Write the fakes + failing tests**

```python
# tests/services/_embedding_fakes.py
"""Shared fake embedder for embedding-path tests."""
from __future__ import annotations


class FakeEmbedder:
    def __init__(self, fail: bool = False):
        self.calls: list = []
        self.closed = 0
        self.fail = fail

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("ollama down")
        return [0.1] * 768

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("ollama down")
        return [[0.1] * 768 for _ in texts]

    async def aclose(self) -> None:
        self.closed += 1
```

```python
# tests/embeddings/test_sync_embed.py
"""SyncEmbedder: sync facade over the async embedder for thread-bound code.

Fresh embedder per call (loop-bound AsyncClient), closed in the worker;
60s circuit breaker so an Ollama outage costs one stall per cooldown
window instead of one per call.
"""
from __future__ import annotations

from better_memory.embeddings.sync_embed import SyncEmbedder
from tests.services._embedding_fakes import FakeEmbedder


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestSyncEmbedder:
    def test_embed_text_returns_vector_and_closes_embedder(self):
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake)
        vec = s.embed_text("hello")
        assert vec is not None and len(vec) == 768
        assert fake.calls == ["hello"]
        assert fake.closed == 1

    def test_embed_batch_returns_vectors(self):
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake)
        out = s.embed_batch(["a", "b"])
        assert out is not None and len(out) == 2

    def test_failure_returns_none_and_opens_breaker(self):
        fake = FakeEmbedder(fail=True)
        clock = FakeClock()
        s = SyncEmbedder(lambda: fake, clock=clock)
        assert s.embed_text("x") is None
        assert fake.calls == ["x"]
        # Breaker open: the embedder is not touched again.
        assert s.embed_text("y") is None
        assert fake.calls == ["x"]

    def test_breaker_closes_after_cooldown(self):
        clock = FakeClock()
        calls = []

        class FlakyThenGood(FakeEmbedder):
            async def embed(self, text):
                calls.append(text)
                if len(calls) == 1:
                    raise RuntimeError("down")
                return [0.1] * 768

        s = SyncEmbedder(FlakyThenGood, clock=clock, cooldown=60.0)
        assert s.embed_text("first") is None
        clock.t += 61.0
        assert s.embed_text("second") is not None
        assert calls == ["first", "second"]

    def test_none_factory_disables_everything(self):
        s = SyncEmbedder(None)
        assert s.embed_text("x") is None
        assert s.embed_batch(["x"]) is None

    def test_embedder_without_aclose_is_fine(self):
        class Bare:
            async def embed(self, text):
                return [0.1] * 768

        s = SyncEmbedder(Bare)
        assert s.embed_text("x") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/embeddings/test_sync_embed.py -v`
Expected: FAIL — module not found. (Create `tests/embeddings/__init__.py` if the directory is new.)

- [ ] **Step 3: Implement**

```python
# better_memory/embeddings/sync_embed.py
"""Sync facade over the async embedder, for thread-bound sync services.

Two hazards this class exists to contain:

1. ``httpx.AsyncClient`` is bound to the event loop that first uses it,
   and ``run_async_in_worker`` runs a fresh loop per call — so the
   embedder must be CONSTRUCTED inside the worker coroutine and closed
   there. The factory is invoked per call; ``OllamaEmbedder`` construction
   is cheap and contacts nothing.
2. Retrieval must never hang on a dead Ollama. Any failure opens a
   circuit breaker: for ``cooldown`` seconds every call returns ``None``
   immediately. Worst case is one bounded stall per cooldown window.

Returns ``None`` on every failure path — callers treat a missing vector
as "no vec leg", never as an error.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from better_memory.async_bridge import run_async_in_worker

#: Bridge-level hard stop. The wiring uses OllamaEmbedder(timeout=5.0,
#: max_retries=1), so a healthy-but-slow call ends well inside this; the
#: bridge timeout is the backstop against pathological hangs.
_WORKER_TIMEOUT = 15.0
_DEFAULT_COOLDOWN = 60.0


class SyncEmbedder:
    def __init__(
        self,
        factory: Callable[[], Any] | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = _DEFAULT_COOLDOWN,
        timeout: float = _WORKER_TIMEOUT,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cooldown = cooldown
        self._timeout = timeout
        self._down_until = 0.0

    def embed_text(self, text: str) -> list[float] | None:
        return self._run(lambda emb: emb.embed(text))

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        return self._run(lambda emb: emb.embed_batch(texts))

    def _run(self, op: Callable[[Any], Any]):
        if self._factory is None:
            return None
        if self._clock() < self._down_until:
            return None

        factory = self._factory

        async def _go():
            emb = factory()
            try:
                return await op(emb)
            finally:
                aclose = getattr(emb, "aclose", None)
                if aclose is not None:
                    await aclose()

        try:
            return run_async_in_worker(_go, timeout=self._timeout)
        except Exception:
            self._down_until = self._clock() + self._cooldown
            return None
```

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/embeddings/test_sync_embed.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add better_memory/embeddings/sync_embed.py tests/embeddings tests/services/_embedding_fakes.py
git commit -m "feat(embeddings): SyncEmbedder — bridge facade with fresh-per-call embedder and 60s breaker

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Reflection write-path embedding

**Files:**
- Modify: `better_memory/services/reflection.py` (ctor ~317; `_apply_new` ~718; `_apply_augment` ~817; `_apply_merge` ~921)
- Modify: `better_memory/mcp/server.py` (~172-230: build `SyncEmbedder`, pass to services)
- Modify: `better_memory/storage/sqlite.py:68` (same)
- Test: `tests/services/test_reflection_embedding_write.py`

**Interfaces:**
- Consumes: `SyncEmbedder` (Task 6), `sqlite_vec.serialize_float32`.
- Produces: `ReflectionSynthesisService(conn, *, clock=None, sync_embedder=None)`; module function `_embedding_source_text(title, use_cases, hints) -> str`; method `_store_embedding(reflection_id, vector | None)`. Embedding written on new + augment; merge deletes the superseded source's row (target text does not change — verified). Wiring: `server.py` builds ONE shared `SyncEmbedder(lambda: OllamaEmbedder(timeout=5.0, max_retries=1))` when `config.embeddings_backend == "ollama"`, else `None`, and passes it to both this service and Task 8's; `storage/sqlite.py` mirrors this using the embedder presence it already knows about.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_reflection_embedding_write.py
"""Synthesis writes reflection embeddings, best-effort.

reflection_embeddings sat at 0 rows from migration 0002 until this change:
the write path simply never embedded. Failures must never block synthesis.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.reflection import (
    ReflectionSynthesisService,
    _embedding_source_text,
)
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _vec_count(conn):
    return conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]


def test_embedding_source_text_joins_fields():
    assert _embedding_source_text("T", "when X", ["h1", "h2"]) == "T\nwhen X\nh1\nh2"
```

Then one test per apply path. **Concrete instruction:** open
`tests/services/test_reflection_writes.py`, copy its working setup for
driving `_apply_new` / `_apply_augment` / `_apply_merge` (episode + observation
seeding and action construction) verbatim into this file, then assert:

- new: `_vec_count(conn) == 1` and the fake recorded one embed whose text
  contains the action's title;
- new with `SyncEmbedder(lambda: FakeEmbedder(fail=True))`: reflection row
  exists, `_vec_count(conn) == 0`;
- augment: `_vec_count` still 1 for that id and the fake recorded a second
  embed containing the appended hint;
- merge: after merging source→target, the SOURCE's embedding row is gone
  (`SELECT COUNT(*) FROM reflection_embeddings WHERE reflection_id = source_id`
  is 0) and the fake recorded NO new embed for the target (target text
  unchanged — verified against `_apply_merge`, which updates counters only);
- no `sync_embedder` passed: `_vec_count(conn) == 0`, everything else works.

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_reflection_embedding_write.py -v`
Expected: FAIL — `_embedding_source_text` import error.

- [ ] **Step 3: Implement**

Module bits in `reflection.py`:

```python
import sqlite_vec
```

```python
def _embedding_source_text(title: str, use_cases: str, hints: list[str]) -> str:
    """What gets embedded for a reflection: the discriminating text."""
    return "\n".join([title, use_cases, *hints])
```

Ctor:

```python
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        sync_embedder: "SyncEmbedder | None" = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or default_clock
        self._sync_embedder = sync_embedder
```

(import `SyncEmbedder` under `TYPE_CHECKING` or directly — direct is fine,
no cycle: `embeddings.sync_embed` imports only `async_bridge`.)

Store helper (method):

```python
    def _store_embedding(self, reflection_id: str, vector: list[float] | None) -> None:
        if vector is None:
            return
        # DELETE+INSERT: vec0 tables historically mishandle UPSERT.
        self._conn.execute(
            "DELETE FROM reflection_embeddings WHERE reflection_id = ?",
            (reflection_id,),
        )
        self._conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            (reflection_id, sqlite_vec.serialize_float32(vector)),
        )
```

Call sites (all inside the existing transactions):

- `_apply_new`, after the `reflection_sources` inserts:

```python
            if self._sync_embedder is not None:
                self._store_embedding(
                    reflection_id,
                    self._sync_embedder.embed_text(_embedding_source_text(
                        action.title, action.use_cases, action.hints,
                    )),
                )
```

- `_apply_augment`: extend the row SELECT to
  `"SELECT title, use_cases, hints, confidence, status FROM reflections WHERE id = ?"`,
  and after the UPDATE:

```python
            if self._sync_embedder is not None:
                final_use_cases = (action.rewrite_use_cases
                                   if action.rewrite_use_cases is not None
                                   else row["use_cases"])
                self._store_embedding(
                    action.reflection_id,
                    self._sync_embedder.embed_text(_embedding_source_text(
                        row["title"], final_use_cases, merged_hints,
                    )),
                )
```

- `_apply_merge`: after the source's `superseded` UPDATE:

```python
            # Target text is untouched by merge (counters/evidence only), so
            # no re-embed; drop the superseded source's vector so it stops
            # competing for kNN slots.
            self._conn.execute(
                "DELETE FROM reflection_embeddings WHERE reflection_id = ?",
                (action.source_id,),
            )
```

Wiring — `server.py` after the embedder block (~line 178):

```python
    from better_memory.embeddings.sync_embed import SyncEmbedder

    sync_embedder: SyncEmbedder | None = None
    if embedder is not None:
        # Fresh short-timeout embedder per bridge call (loop-bound client);
        # shared instance so the breaker state is process-wide.
        sync_embedder = SyncEmbedder(
            lambda: OllamaEmbedder(timeout=5.0, max_retries=1)
        )
```

then `ReflectionSynthesisService(memory_conn, sync_embedder=sync_embedder)` at
line ~222. `storage/sqlite.py:68`: build the same `SyncEmbedder` from the
embedder the backend already receives (`sync_embedder = SyncEmbedder(lambda:
OllamaEmbedder(timeout=5.0, max_retries=1)) if embedder is not None else None`)
and pass it. Move imports to module top per house style.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_reflection_embedding_write.py tests/services/test_reflection_writes.py tests/mcp/test_server_backend_dispatch.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(embeddings): reflections embedded at synthesis write time via SyncEmbedder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Semantic write-path embedding

**Files:**
- Modify: `better_memory/services/semantic.py` (ctor; `create`; `update_text`; `create_from_observation`)
- Modify: instantiation sites (grep `SemanticMemoryService(` in `mcp/server.py` + `storage/sqlite.py`; pass `sync_embedder=` from Task 7's shared instance)
- Test: `tests/services/test_semantic_embedding_write.py`

**Interfaces:**
- Consumes: `SyncEmbedder` (Task 6). Source text = the memory's `content`. Table `semantic_embeddings(memory_id, embedding)` (Task 5).
- Produces: `SemanticMemoryService(conn, *, clock=None, sync_embedder=None)` with a private `_store_embedding(memory_id, vector | None)` mirroring Task 7's (DELETE+INSERT).

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_semantic_embedding_write.py
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.semantic import SemanticMemoryService
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _vec_count(conn):
    return conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0]


class TestSemanticEmbeddingWrite:
    def test_create_embeds_content(self, conn):
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, sync_embedder=SyncEmbedder(lambda: fake))
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 1
        assert fake.calls == ["the fact"]

    def test_update_text_reembeds(self, conn):
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, sync_embedder=SyncEmbedder(lambda: fake))
        mid = svc.create(content="v1", project="p")
        svc.update_text(id=mid, content="v2")
        assert _vec_count(conn) == 1          # replaced, not duplicated
        assert fake.calls == ["v1", "v2"]

    def test_failure_never_blocks_create(self, conn):
        svc = SemanticMemoryService(
            conn, sync_embedder=SyncEmbedder(lambda: FakeEmbedder(fail=True)))
        mid = svc.create(content="the fact", project="p")
        assert mid
        assert _vec_count(conn) == 0

    def test_no_embedder_no_rows(self, conn):
        svc = SemanticMemoryService(conn)
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 0

    def test_promote_from_observation_embeds(self, conn):
        # create_from_observation needs an active observation row — seed the
        # minimal one (mirror the seeding used in test_semantic.py's promote
        # tests; copy that helper).
        ...
```

Replace the final `...` by copying the observation-seeding lines from the
existing promote test in `tests/services/test_semantic.py`, then assert
`_vec_count(conn) == 1`.

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic_embedding_write.py -v`
Expected: FAIL — ctor rejects `sync_embedder`.

- [ ] **Step 3: Implement** — ctor kwarg + `import sqlite_vec` + `_store_embedding` (same shape as Task 7, table `semantic_embeddings`, column `memory_id`); at the end of `create`, `update_text`, and `create_from_observation` (before each `commit`):

```python
        if self._sync_embedder is not None:
            self._store_embedding(
                memory_id, self._sync_embedder.embed_text(content))
```

(using each method's local names — in `create_from_observation` the content
is `row["content"]` and the id is the new `memory_id`). Wire both
instantiation sites with the shared `sync_embedder`.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic_embedding_write.py tests/services/test_semantic.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(embeddings): semantic memories embedded on create/update via SyncEmbedder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Three-leg fusion + lazy self-heal

**Files:**
- Modify: `better_memory/services/reflection.py` (`_fuse_by_relevance`; `retrieve_reflections` query branch)
- Test: `tests/services/test_vec_fusion.py`

**Interfaces:**
- Consumes: `self._sync_embedder` (Task 7), `_embedding_source_text`, `_store_embedding`, `sqlite_vec.serialize_float32`.
- Produces: `_fuse_by_relevance(rows, *, query, query_vector=None, rrf_k=60)`; `SELF_HEAL_BATCH_CAP = 20`; `_heal_missing_embeddings(rows)`; `_vec_ranks(query_vector, candidate_ids) -> dict[str, int]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_vec_fusion.py
"""Query fusion gains a vector leg; missing embeddings self-heal on retrieve.

Degradation contract: no embedder / breaker open / row unembedded -> exactly
the two-leg (prior + BM25) behaviour that shipped in #81.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.reflection import ReflectionSynthesisService
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid, *, title, useful=0, ignored=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?, ?)""",
        (rid, title, useful, ignored),
    )
    conn.commit()


class DirectedEmbedder(FakeEmbedder):
    """Maps texts containing any trigger phrase to one vector; noise else.

    Lets a test make the query and one reflection 'semantically identical'
    while sharing zero tokens — isolating the vec leg from BM25.
    """

    def __init__(self, *triggers: str):
        super().__init__()
        self.triggers = triggers

    def _vec(self, text: str) -> list[float]:
        if any(t in text for t in self.triggers):
            return [1.0] + [0.0] * 767
        return [0.0, 1.0] + [0.0] * 766

    async def embed(self, text):
        self.calls.append(text)
        return self._vec(text)

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]


def _svc(conn, embedder):
    return ReflectionSynthesisService(
        conn, sync_embedder=SyncEmbedder(lambda: embedder))


class TestVecFusion:
    def test_semantic_match_promoted_without_token_overlap(self, conn):
        _seed(conn, "r-target", title="Stdout handling on win32 interpreters")
        _seed(conn, "r-noise-1", title="Unrelated advice alpha", useful=5, ignored=1)
        _seed(conn, "r-noise-2", title="Unrelated advice beta", useful=4, ignored=1)
        emb = DirectedEmbedder("Stdout handling", "console output")
        svc = _svc(conn, emb)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="console output disappears on windows",
        )["do"]]
        assert ids[0] == "r-target"

    def test_no_embedder_matches_shipped_behaviour(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = ReflectionSynthesisService(conn)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]

    def test_embedder_failure_degrades_silently(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = _svc(conn, FakeEmbedder(fail=True))
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]


class TestSelfHeal:
    def test_unembedded_candidates_healed_on_query_retrieve(self, conn):
        _seed(conn, "r-a", title="Alpha")
        _seed(conn, "r-b", title="Beta")
        svc = _svc(conn, FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything at all")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 2

    def test_heal_capped_at_batch_limit(self, conn):
        from better_memory.services.reflection import SELF_HEAL_BATCH_CAP
        for i in range(SELF_HEAL_BATCH_CAP + 5):
            _seed(conn, f"r-{i:03}", title=f"Title {i}")
        svc = _svc(conn, FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == SELF_HEAL_BATCH_CAP

    def test_no_query_no_heal_and_no_embed_calls(self, conn):
        _seed(conn, "r-a", title="Alpha")
        fake = FakeEmbedder()
        svc = _svc(conn, fake)
        svc.retrieve_reflections(project="p")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 0
        assert fake.calls == []

    def test_heal_failure_silent(self, conn):
        _seed(conn, "r-a", title="Alpha")
        svc = _svc(conn, FakeEmbedder(fail=True))
        rows = svc.retrieve_reflections(project="p", query="anything")
        assert rows["do"]
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_vec_fusion.py -v`
Expected: `SELF_HEAL_BATCH_CAP` import error; promotion test fails.

- [ ] **Step 3: Implement**

Constant:

```python
#: Max embeddings written per retrieve call by the lazy self-heal. Keeps
#: worst-case added latency bounded; cli.backfill_embeddings is the bulk path.
SELF_HEAL_BATCH_CAP = 20
```

Query branch in `retrieve_reflections`:

```python
            if query:
                self._heal_missing_embeddings(rows)
                query_vector = (
                    self._sync_embedder.embed_text(query)
                    if self._sync_embedder is not None else None
                )
                rows = self._fuse_by_relevance(
                    rows, query=query, query_vector=query_vector,
                )
                _diag.step(fn, "relevance_fused", n_rows=len(rows))
```

Methods:

```python
    def _heal_missing_embeddings(self, rows) -> None:
        """Embed up to SELF_HEAL_BATCH_CAP candidates that lack vectors.

        Historical rows and write-time failures repair themselves on their
        first relevant retrieval. Entirely best-effort; one embed_batch call.
        """
        if self._sync_embedder is None or not rows:
            return
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        have = {
            row[0] for row in self._conn.execute(
                f"SELECT reflection_id FROM reflection_embeddings "
                f"WHERE reflection_id IN ({placeholders})", ids,
            )
        }
        todo = [r for r in rows if r["id"] not in have][:SELF_HEAL_BATCH_CAP]
        if not todo:
            return
        texts = [
            _embedding_source_text(r["title"], r["use_cases"],
                                   json.loads(r["hints"]))
            for r in todo
        ]
        vectors = self._sync_embedder.embed_batch(texts)
        if vectors is None:
            return
        for r, vec in zip(todo, vectors):
            self._store_embedding(r["id"], vec)
        self._conn.commit()

    def _vec_ranks(self, query_vector, candidate_ids) -> dict[str, int]:
        """reflection_id -> vec rank (0 = closest) among the candidates.

        sqlite-vec kNN accepts only ``embedding MATCH ? AND k = ?`` — no
        extra predicates — so fetch top-k then filter, exactly as
        search/hybrid.py:_vec_candidates does.
        """
        if query_vector is None or not candidate_ids:
            return {}
        try:
            knn = self._conn.execute(
                "SELECT reflection_id FROM reflection_embeddings "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (sqlite_vec.serialize_float32(query_vector),
                 max(len(candidate_ids), 50)),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        wanted = set(candidate_ids)
        out: dict[str, int] = {}
        for row in knn:
            if row[0] in wanted:
                out[row[0]] = len(out)
        return out
```

`_fuse_by_relevance`: signature gains `query_vector=None`; after the BM25
`rel_rank` dict is built add `vec_rank = self._vec_ranks(query_vector, ids)`;
bail out unchanged only when BOTH `rel_rows` empty AND `vec_rank` empty; in
the scoring loop add:

```python
            vr = vec_rank.get(row["id"])
            if vr is not None:
                score += 1.0 / (rrf_k + vr)
```

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_vec_fusion.py tests/services/test_reflection_query_relevance.py tests/services/test_wilson_ranking.py tests/services/test_exploration_slot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(retrieval): three-leg RRF fusion (prior + BM25 + vec) with lazy self-heal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Backfill CLI

**Files:**
- Create: `better_memory/cli/backfill_embeddings.py`
- Test: `tests/cli/test_backfill_embeddings.py`

**Interfaces:**
- Consumes: `_embedding_source_text` (Task 7), `sqlite_vec.serialize_float32`, any embedder with async `embed_batch` + optional `aclose`.
- Produces: `backfill(conn, embedder) -> dict` (single event loop, one embedder, `aclose` by `main`); `python -m better_memory.cli.backfill_embeddings [--home PATH]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_backfill_embeddings.py
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.cli.backfill_embeddings import backfill
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, status="pending_review"):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at, status)
           VALUES (?, 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?)""", (rid, status))
    conn.commit()


def _seed_semantic(conn, sid):
    conn.execute(
        """INSERT INTO semantic_memories (id, content, project, scope,
           created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,))
    conn.commit()


def test_backfills_reflections_and_semantics(conn):
    _seed_reflection(conn, "r1")
    _seed_semantic(conn, "s1")
    stats = backfill(conn, FakeEmbedder())
    assert stats == {"reflections": 1, "semantics": 1, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0] == 1


def test_idempotent(conn):
    _seed_reflection(conn, "r1")
    fake = FakeEmbedder()
    backfill(conn, fake)
    assert backfill(conn, fake) == {"reflections": 0, "semantics": 0, "skipped": 0}


def test_retired_reflections_skipped(conn):
    _seed_reflection(conn, "r1", status="retired")
    assert backfill(conn, FakeEmbedder()) == {
        "reflections": 0, "semantics": 0, "skipped": 0}


def test_embed_failure_counted_as_skipped(conn):
    _seed_reflection(conn, "r1")
    stats = backfill(conn, FakeEmbedder(fail=True))
    assert stats == {"reflections": 0, "semantics": 0, "skipped": 1}
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/cli/test_backfill_embeddings.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# better_memory/cli/backfill_embeddings.py
"""One-shot embedding backfill for reflections + semantic memories.

Run at deploy after migration 0014:

    python -m better_memory.cli.backfill_embeddings

Idempotent: only rows missing a vector are embedded. The lazy self-heal in
memory.retrieve covers stragglers afterwards; this exists so the historical
corpus doesn't wait to be retrieved before becoming searchable.

One event loop and one embedder for the whole job (the embedder's
httpx.AsyncClient is loop-bound); batches of 50 per HTTP request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import sqlite_vec

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import _embedding_source_text

_BATCH = 50


def backfill(conn, embedder) -> dict[str, int]:
    stats = {"reflections": 0, "semantics": 0, "skipped": 0}

    refl = conn.execute(
        """SELECT r.id, r.title, r.use_cases, r.hints FROM reflections r
           WHERE r.status IN ('pending_review', 'confirmed')
             AND r.id NOT IN (SELECT reflection_id FROM reflection_embeddings)"""
    ).fetchall()
    sems = conn.execute(
        """SELECT id, content FROM semantic_memories
           WHERE id NOT IN (SELECT memory_id FROM semantic_embeddings)"""
    ).fetchall()

    jobs = [
        ("reflections", "reflection_embeddings", "reflection_id", r["id"],
         _embedding_source_text(r["title"], r["use_cases"],
                                json.loads(r["hints"])))
        for r in refl
    ] + [
        ("semantics", "semantic_embeddings", "memory_id", s["id"], s["content"])
        for s in sems
    ]

    async def _embed_all() -> list[list[list[float]] | None]:
        out = []
        for i in range(0, len(jobs), _BATCH):
            texts = [j[4] for j in jobs[i:i + _BATCH]]
            try:
                out.append(await embedder.embed_batch(texts))
            except Exception:
                out.append(None)
        return out

    batches = asyncio.run(_embed_all()) if jobs else []

    for bi, vectors in enumerate(batches):
        chunk = jobs[bi * _BATCH:(bi + 1) * _BATCH]
        if vectors is None:
            stats["skipped"] += len(chunk)
            continue
        for (kind, table, col, row_id, _), vec in zip(chunk, vectors):
            conn.execute(
                f"INSERT INTO {table} ({col}, embedding) VALUES (?, ?)",
                (row_id, sqlite_vec.serialize_float32(vec)))
            stats[kind] += 1
    conn.commit()
    return stats


def main(argv: list[str] | None = None) -> None:
    from better_memory.config import get_config
    from better_memory.embeddings.ollama import OllamaEmbedder

    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None,
                    help="BETTER_MEMORY_HOME override (default: config)")
    args = ap.parse_args(argv)

    config = get_config()
    db = Path(args.home) / "memory.db" if args.home else config.memory_db
    conn = connect(db)
    apply_migrations(conn)

    if config.embeddings_backend != "ollama":
        print("embeddings backend is not ollama; nothing to backfill")
        return

    embedder = OllamaEmbedder()
    try:
        stats = backfill(conn, embedder)
    finally:
        asyncio.run(embedder.aclose())
    print(f"backfilled reflections={stats['reflections']} "
          f"semantics={stats['semantics']} skipped={stats['skipped']}")
    if stats["skipped"]:
        print("warning: some rows skipped (Ollama unreachable?); "
              "re-run later or let retrieve self-heal them", file=sys.stderr)


if __name__ == "__main__":
    main()
```

Note on `aclose` after `asyncio.run(backfill...)`: the embedder's client was
used on the loop inside `backfill`'s `asyncio.run`, which is closed by then;
`aclose` on a second loop is the documented-acceptable teardown for httpx
(close only releases resources). If it raises, wrap in try/except — teardown
is best-effort.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/cli/test_backfill_embeddings.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/backfill_embeddings.py tests/cli/test_backfill_embeddings.py
git commit -m "feat(cli): one-shot embedding backfill for reflections + semantics

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Website sync, typecheck, full suite

**Files:**
- Modify: `website/index.md`, `website/architecture.md`, `website/configuration.md` (only where stale)

- [ ] **Step 1:** `grep -rn "useful_count\|OVERLOOKED\|DEMOTION\|popularity" website/ | head -20`
- [ ] **Step 2:** Update each stale paragraph: Wilson lower bound on (useful+overlooked)/rated; exploration slot; embeddings at synthesis + self-heal + backfill CLI; three-leg RRF; 60s breaker. Edit only what is wrong.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 4:** `./.venv/Scripts/python.exe -m pytest tests -q` → all pass; fix stragglers pinning old ordering.
- [ ] **Step 5:**

```bash
git add website
git commit -m "docs(website): ranking + embeddings prose matches PR-A behaviour

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: A/B validation gate (48 sessions), PR, babysit

**Files:**
- Modify: `C:/Users/gethi/source/autoresearch/memuse-260721-run/runner.py` (add arm `A8`)

**Interfaces:** gate = distinct useful% from 48 sessions not statistically below 62/270 (22.96%); one-sided two-proportion z at α=0.05; one re-run on borderline; fail-twice = stop, no PR.

- [ ] **Step 1:** Add arm:

```python
    elif arm == "A8":
        # PR-A validation: main repo checkout sits on feat/retrieval-quality.
        spec["code"] = str(MAIN_REPO)
```

Confirm `git -C C:/Users/gethi/source/better-memory branch --show-current` → `feat/retrieval-quality` before launching.

- [ ] **Step 2:** Refresh sandbox base DB (new schema):

```bash
cd C:/Users/gethi/source/autoresearch/memuse-260721-run
python - <<'EOF'
import sqlite3, os
src = os.path.expanduser("~/.better-memory/memory.db")
dst = r"sandbox\base\memory.db"
os.remove(dst)
sqlite3.connect(f"file:{src}?mode=ro", uri=True).execute("VACUUM INTO ?", (dst,))
EOF
```

- [ ] **Step 3:** `python runner.py --arm A8 --repeats 4 --timeout 420 > logs-A8.txt 2>&1` (48 sessions, ~4h, ~$80; background; strip-and-retry 429 rows).

- [ ] **Step 4:** Gate:

```bash
python analyze.py --arms A6,A8
python - <<'EOF'
import math
u8, n8 = USEFUL_A8, EXPOSED_A8          # fill from analyze distinct columns
p1, n1 = 62/270, 270
p2 = u8/n8
p = (62 + u8) / (n1 + n8)
z = (p2 - p1) / math.sqrt(p*(1-p)*(1/n1 + 1/n8))
print(f"A8={p2:.4f} baseline={p1:.4f} z={z:.2f}")
print("GATE:", "PASS" if z > -1.645 else "FAIL (one re-run allowed)")
EOF
```

- [ ] **Step 5:** PASS → push, `gh pr create` (body: spec link, A/B numbers, migration 0014 + backfill deploy note, footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), babysit to squash-merge, then on main: `./.venv/Scripts/python.exe -m better_memory.cli.backfill_embeddings`.

---

## Self-review notes

- Every previously-assumed integration point is now pinned in the "Verified-against-source facts" table; Tasks 6-10 contain only calls whose signatures were read from source this session.
- The two prior plan bugs (coroutine-vs-factory; loop-bound AsyncClient) are structurally prevented by `SyncEmbedder` — no other code touches the bridge.
- Merge-path embedding deliberately does NOT re-embed the target (text unchanged, verified) — a reviewer questioning that finds the rationale in Task 7's test and comment.
- Remaining sub-95 item: Task 12 (92%) — irreducible sampling noise, mitigated by n=48 and the statistical gate; accepted by user 2026-07-23.
