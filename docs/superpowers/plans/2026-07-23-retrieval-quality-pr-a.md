# Retrieval Quality PR-A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace popularity ranking with a Wilson-score prior + exploration slot, and add reflection/semantic embeddings with three-leg RRF query fusion.

**Architecture:** SQL keeps filtering; ranking moves to Python (`services/scoring.py`). The Ollama embedder is threaded into `ReflectionSynthesisService` and `SemanticMemoryService`; sync code reaches it through `async_bridge.run_async_in_worker`. Query relevance fuses prior rank + BM25 rank + vec-kNN rank via RRF. Everything embedding-related is best-effort and degrades to today's BM25-only behaviour.

**Tech Stack:** Python 3.12, sqlite + sqlite-vec (vec0), FTS5, Ollama `nomic-embed-text` (768-dim), pytest.

**Spec:** `docs/superpowers/specs/2026-07-23-retrieval-quality-design.md`

## Global Constraints

- Branch: `feat/retrieval-quality` (exists; spec committed as `604fb79`).
- Test command: `./.venv/Scripts/python.exe -m pytest <path> -v` from repo root (full suite: `./.venv/Scripts/python.exe -m pytest tests -q`, ~10 min, integration deselected by default).
- Typecheck: `./.venv/Scripts/python.exe -m pyright` must stay at 0 errors.
- Every Ollama touchpoint: best-effort, catch `Exception`, never raise into caller.
- `sqlite3.Connection` is bound to its creating thread — embeddings may be computed on the bridge worker thread, but ALL DB reads/writes stay on the caller thread.
- Ruff line length 100.
- Website sync guardrail (conf 1.0 reflection): `website/index.md` / `website/architecture.md` prose must match tool behaviour changes in the same PR.
- Commit after every task, message style `feat(scope): ...` / `test(scope): ...`, footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Wilson scoring module

**Files:**
- Create: `better_memory/services/scoring.py`
- Test: `tests/services/test_scoring.py`

**Interfaces:**
- Produces: `wilson_lower_bound(positive: int, n: int, z: float = 1.96) -> float` — 0.0 when `n == 0`; never negative. Consumed by Tasks 2, 3, 4.

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
- Modify: `better_memory/services/reflection.py` (retrieve_reflections, ~lines 1256-1400)
- Modify: `better_memory/services/memory_rating.py` (delete `IGNORED_DEMOTION_FLOOR`, `IGNORED_DEMOTION_WEIGHT`, `OVERLOOKED_RANKING_WEIGHT`)
- Modify: `tests/services/test_useful_count_ranking.py`
- Delete: `tests/services/test_ignored_demotion.py` (superseded — its scenarios move to the new test file)
- Test: `tests/services/test_wilson_ranking.py`

**Interfaces:**
- Consumes: `wilson_lower_bound` from Task 1.
- Produces: `retrieve_reflections` rows now include `times_overlooked` and `times_ignored`; SQL no longer orders; Python sorts by `(wilson DESC, confidence DESC, updated_at DESC)`. `_wilson_rated(row) -> int` and `_wilson_score(row) -> float` module-level helpers in `reflection.py` — Task 4 reuses `_wilson_rated` for the untested test.

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


class TestWilsonRanking:
    def test_hit_rate_beats_raw_count(self):
        pass  # replaced below — see class body

    
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

Delete the placeholder `TestWilsonRanking` class (editing artifact) before running; keep only `TestWilsonOrdering`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_wilson_ranking.py -v`
Expected: FAIL — ordering assertions (current code ranks r-workhorse first) and `test_demotion_constants_are_gone`.

- [ ] **Step 3: Implement**

In `better_memory/services/reflection.py`:

3a. Replace the import of the deleted constants (top of file):

```python
from better_memory.search.query import sanitize_fts5_query
from better_memory.services.scoring import wilson_lower_bound
```

(remove the `from better_memory.services.memory_rating import (IGNORED_DEMOTION_FLOOR, ...)` block).

3b. Add module-level helpers directly above `class ReflectionSynthesisService`:

```python
def _wilson_rated(row) -> int:
    """Total rated exposures backing this memory's score."""
    return (row["useful_count"] + row["times_overlooked"]
            + row["times_ignored"])


def _wilson_score(row) -> float:
    positive = row["useful_count"] + row["times_overlooked"]
    return wilson_lower_bound(positive, _wilson_rated(row))
```

3c. In `retrieve_reflections`, replace the SELECT (drop the demotion params and ORDER BY; add the two counter columns; sort in Python):

```python
            where = " AND ".join(clauses)
            _diag.step(fn, "executing_select")
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

(`params` no longer receives the two demotion appends — delete those lines too.)

3d. The bucket-fill loop and `_fuse_by_relevance` call stay as they are, but the bucket dicts must carry the new counters — extend the `bucket.append({...})` literal with:

```python
                    "times_overlooked": r["times_overlooked"],
                    "times_ignored": r["times_ignored"],
```

In `better_memory/services/memory_rating.py`: delete the `OVERLOOKED_RANKING_WEIGHT`, `IGNORED_DEMOTION_FLOOR`, `IGNORED_DEMOTION_WEIGHT` constants and their comment blocks.

In `tests/services/test_useful_count_ranking.py`: this file pins the OLD ordering (`useful_count` beats confidence regardless of ignores). Rewrite its two assertions to the Wilson contract or delete the file if fully covered by `test_wilson_ranking.py` — it is fully covered; delete it. Also delete `tests/services/test_ignored_demotion.py` (scenarios live on in `test_proven_dead_weight_sinks_below_modest_performer` + `test_demotion_constants_are_gone`).

- [ ] **Step 4: Run the affected tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_wilson_ranking.py tests/services/test_reflection_query_relevance.py tests/services/test_reflection_retrieve_fields.py tests/mcp -q`
Expected: all pass (query-relevance tests still pass — fusion consumes the new ordering transparently).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(ranking): reflections ranked by Wilson prior, demotion stack deleted

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Semantic memories ranked by Wilson score

**Files:**
- Modify: `better_memory/services/semantic.py` (list_for_project SELECT/ORDER, ~lines 237-260; imports ~19-23; SemanticMemory dataclass)
- Test: `tests/services/test_semantic.py` (add class; existing tests keep passing)

**Interfaces:**
- Consumes: `wilson_lower_bound` (Task 1).
- Produces: `SemanticMemory` dataclass gains `times_ignored: int = 0`; `list_for_project` ordered by `(wilson DESC, created_at DESC)`.

- [ ] **Step 1: Write the failing test** (append to `tests/services/test_semantic.py`, reusing that file's existing fixtures/helpers for creating memories — check its seed helper name before writing):

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

(Adapt the fixture name to the file's existing `conn` fixture; if creation timestamps collide within the test, monkeypatch the service clock as neighbouring tests in that file do.)

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic.py -v -k Wilson`
Expected: FAIL (current order puts the 67-count row first; `times_ignored` not selected).

- [ ] **Step 3: Implement**

In `better_memory/services/semantic.py`:

- Imports: replace the three-constant import block with `from better_memory.services.scoring import wilson_lower_bound`.
- `SemanticMemory` dataclass: add `times_ignored: int = 0` and `last_ignored_at: str | None = None` after the overlooked fields.
- `list_for_project` SQL: add `times_ignored, last_ignored_at` to the SELECT column list; replace the ORDER BY block and its three `params.append(...)` lines with plain `"ORDER BY created_at DESC"` (recency pre-sort; final order applied in Python).
- After `rows = self._conn.execute(sql, params).fetchall()` build `results` as now (passing the two new fields into the dataclass), then sort:

```python
        results.sort(
            key=lambda m: wilson_lower_bound(
                m.useful_count + m.times_overlooked,
                m.useful_count + m.times_overlooked + m.times_ignored,
            ),
            reverse=True,
        )
```

(Stable sort preserves the SQL `created_at DESC` order inside equal scores.)

- [ ] **Step 4: Run the semantic + bootstrap tests**

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
- Modify: `better_memory/services/reflection.py` (bucket-fill loop in retrieve_reflections)
- Test: `tests/services/test_exploration_slot.py`

**Interfaces:**
- Consumes: `_wilson_rated` (Task 2). Untested ≡ `_wilson_rated(row) < 3` — the constant lives as `EXPLORATION_RATED_FLOOR = 3` in `reflection.py`.
- Produces: per-bucket shortlist = up to `cap-1` best tested rows + the best untested row (fused/ranked order), when both kinds exist. `cap is None` (bootstrap) ⇒ no reservation.

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
Expected: `test_last_slot_goes_to_best_untested` and `test_two_ratings_is_still_untested_three_is_not` FAIL (untested rows sort last today and never enter a full bucket).

- [ ] **Step 3: Implement**

In `reflection.py`, add near `_wilson_score`:

```python
#: A memory with fewer than this many rated exposures is "untested": its
#: Wilson score is statistically meaningless, so it competes for the
#: reserved exploration slot instead of the proven slots.
EXPLORATION_RATED_FLOOR = 3
```

Replace the bucket-fill loop (currently: iterate `rows`, append until `cap`) with:

```python
            cap = limit_per_bucket if limit_per_bucket is not None else sys.maxsize
            reserve = limit_per_bucket is not None and cap >= 2
            buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
            # rows is in final ranked order (Wilson sort, then optional
            # relevance fusion). Untested rows keep that order too — the
            # first untested row in `rows` is the most query-relevant one.
            explorers: dict[str, dict] = {}
            for r in rows:
                bucket = buckets[r["polarity"]]
                untested = _wilson_rated(r) < EXPLORATION_RATED_FLOOR
                if reserve and untested and r["polarity"] not in explorers:
                    explorers[r["polarity"]] = self._bucket_item(r)
                    continue
                if len(bucket) >= (cap - 1 if reserve else cap):
                    continue
                bucket.append(self._bucket_item(r))
            # Fill the reserved slot: best untested if one exists, else the
            # next proven row that was displaced by the reservation.
            if reserve:
                for polarity, bucket in buckets.items():
                    if polarity in explorers:
                        bucket.append(explorers[polarity])
                    else:
                        continue
```

Then handle the no-untested case: the loop above under-fills by one when no untested row exists for a bucket. Implement precisely by two passes instead if clearer — the contract the tests pin is:

1. `cap-1` best tested rows in ranked order,
2. slot `cap` = best untested if any, else next best tested,
3. `cap None` ⇒ plain fill, no reservation.

A clean two-pass implementation (preferred — use this one):

```python
            cap = limit_per_bucket if limit_per_bucket is not None else sys.maxsize
            reserve = limit_per_bucket is not None and cap >= 2
            buckets = {"do": [], "dont": [], "neutral": []}
            by_polarity: dict[str, list] = {"do": [], "dont": [], "neutral": []}
            for r in rows:
                by_polarity[r["polarity"]].append(r)
            for polarity, group in by_polarity.items():
                bucket = buckets[polarity]
                if not reserve:
                    bucket.extend(self._bucket_item(r) for r in group[:cap])
                    continue
                tested = [r for r in group
                          if _wilson_rated(r) >= EXPLORATION_RATED_FLOOR]
                untested = [r for r in group
                            if _wilson_rated(r) < EXPLORATION_RATED_FLOOR]
                chosen = tested[: cap - 1]
                if untested:
                    chosen.append(untested[0])
                remaining = [r for r in group if r not in chosen]
                chosen.extend(remaining[: cap - len(chosen)])
                bucket.extend(self._bucket_item(r) for r in chosen)
```

Extract the existing `bucket.append({...})` dict literal into a method `_bucket_item(self, r) -> dict` so both call sites share it (it now includes the Task 2 counter fields).

Note: `sqlite3.Row` supports `in`-list identity checks because each row object is unique per fetch — `r not in chosen` compares identity, which is correct here (same objects from one fetch).

Update the `_diag.step(fn, "bucketed", ...)` call to follow the new loop.

- [ ] **Step 4: Run the reflection test files**

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
- Test: append to `tests/db/test_schema.py` (follow that file's existing per-migration test style — read it first and mirror the newest migration's test)

**Interfaces:**
- Produces: vec0 table `semantic_embeddings(memory_id TEXT PRIMARY KEY, embedding FLOAT[768])`, consumed by Tasks 7-9.

- [ ] **Step 1: Write the failing test** (mirroring the file's existing pattern — a test that applies migrations to a fresh DB and asserts the table exists):

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
Expected: FAIL — table absent.

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
Expected: all pass (including any schema-drift tests — if one hardcodes the migration count/list, update it).

- [ ] **Step 5: Commit**

```bash
git add better_memory/db/migrations/0014_semantic_embeddings.sql tests/db
git commit -m "feat(db): semantic_embeddings vec0 table (migration 0014)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Embedder in ReflectionSynthesisService — write-path embedding

**Files:**
- Modify: `better_memory/services/reflection.py` (ctor line ~317; `_apply_new` ~718, `_apply_augment` ~817, `_apply_merge` ~921)
- Modify: `better_memory/mcp/server.py:222` (pass embedder)
- Modify: `better_memory/storage/sqlite.py:68` (pass embedder — its ctor already receives one for observations; check the field name, likely `self._embedder` or the ctor arg)
- Test: `tests/services/test_reflection_embedding_write.py`

**Interfaces:**
- Consumes: `OllamaEmbedder.embed(text) -> list[float]` (async), `run_async_in_worker` from `better_memory.async_bridge`.
- Produces: `ReflectionSynthesisService(conn, *, clock=None, embedder=None)`; `_embed_text(self, text) -> list[float] | None` (sync, best-effort); `_embedding_source_text(title, use_cases, hints) -> str` module function; embeddings written to `reflection_embeddings` on new/augment/merge. Tasks 8-9 reuse `_embed_text` and `_embedding_source_text`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_reflection_embedding_write.py
"""Synthesis writes reflection embeddings, best-effort.

reflection_embeddings sat at 0 rows from migration 0002 until this change:
the write path simply never embedded. Ollama failures must never block
synthesis — a missing embedding degrades retrieval to BM25, nothing more.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import EmbeddingError
from better_memory.services.reflection import (
    ReflectionSynthesisService,
    _embedding_source_text,
)


class FakeEmbedder:
    def __init__(self, fail=False):
        self.calls: list[str] = []
        self.fail = fail

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise EmbeddingError("ollama down")
        return [0.1] * 768

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


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


class TestEmbeddingSourceText:
    def test_joins_title_use_cases_hints(self):
        text = _embedding_source_text("T", "when X", ["h1", "h2"])
        assert text == "T\nwhen X\nh1\nh2"


class TestWritePathEmbedding:
    def test_embed_text_none_embedder_returns_none(self, conn):
        svc = ReflectionSynthesisService(conn)
        assert svc._embed_text("anything") is None

    def test_embed_text_failure_returns_none(self, conn):
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder(fail=True))
        assert svc._embed_text("anything") is None

    def test_embed_text_success_returns_vector(self, conn):
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder())
        vec = svc._embed_text("anything")
        assert vec is not None and len(vec) == 768
```

Plus one integration-style test per apply path. The apply methods take action
dataclasses — read `_apply_new`'s signature and the `NewAction` dataclass
(`reflection.py` ~line 136) first, then write:

```python
class TestApplyPathsEmbed:
    def test_apply_new_writes_embedding_row(self, conn):
        fake = FakeEmbedder()
        svc = ReflectionSynthesisService(conn, embedder=fake)
        # Construct the minimal NewAction the codebase defines (check
        # dataclass fields; fill required ones only) and call _apply_new
        # via the public apply_decision path used by synthesize tests —
        # mirror tests/services/test_reflection_writes.py setup.
        ...
        assert _vec_count(conn) == 1
        assert fake.calls  # source text was embedded

    def test_apply_new_with_failing_embedder_still_writes_reflection(self, conn):
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder(fail=True))
        ...
        assert _vec_count(conn) == 0   # no embedding
        # reflection row exists — synthesis unaffected
```

**Concrete instruction for the `...`:** copy the action-construction and
`apply_decision` invocation verbatim from the nearest existing test in
`tests/services/test_reflection_writes.py` (it exercises `_apply_new` /
augment / merge already); add the embedder kwarg and the two assertions.
Do the same for augment (asserts a second `fake.calls` entry after augment)
and merge (embedding row for the merge target).

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_reflection_embedding_write.py -v`
Expected: FAIL — `_embedding_source_text` import error.

- [ ] **Step 3: Implement**

In `reflection.py`:

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
        embedder: Any = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or default_clock
        self._embedder = embedder
```

Sync bridge helper (method):

```python
    def _embed_text(self, text: str) -> list[float] | None:
        """Best-effort sync embedding. None embedder / any failure -> None.

        The embed call runs on the bridge worker thread; the DB write that
        consumes the vector stays on the caller thread (sqlite3 conns are
        thread-bound).
        """
        if self._embedder is None:
            return None
        try:
            return run_async_in_worker(self._embedder.embed(text))
        except Exception:
            return None

    def _store_embedding(self, reflection_id: str, vector: list[float] | None) -> None:
        if vector is None:
            return
        self._conn.execute(
            "DELETE FROM reflection_embeddings WHERE reflection_id = ?",
            (reflection_id,),
        )
        self._conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            (reflection_id, _serialize_vector(vector)),
        )
```

For `_serialize_vector`, reuse whatever `ObservationService.create` does at
`observation.py:204-210` to insert into `observation_embeddings` (it already
solves vec0 serialisation — likely `sqlite_vec.serialize_float32` or a JSON
string; copy that exact call, import from the same place). DELETE+INSERT
rather than INSERT OR REPLACE because vec0 virtual tables historically
mishandle UPSERT.

Imports: `from better_memory.async_bridge import run_async_in_worker` and
`from typing import Any` (if not present).

Call sites — at the end of the row-write in each of `_apply_new`,
`_apply_augment`, `_apply_merge` (inside their existing transaction, after
the reflection row is written, before commit):

```python
        self._store_embedding(
            reflection_id,
            self._embed_text(_embedding_source_text(title, use_cases, hints)),
        )
```

(with the local variable names each method actually has — augment/merge
re-read the post-edit title/use_cases/hints they just wrote).

Wiring: `server.py:222` → `ReflectionSynthesisService(memory_conn, embedder=embedder)`; `storage/sqlite.py:68` → pass the embedder SqliteBackend already holds for observations (check its ctor field name and thread it).

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_reflection_embedding_write.py tests/services/test_reflection_writes.py tests/mcp/test_server_backend_dispatch.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(embeddings): reflections embedded at synthesis write time, best-effort

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Embedder in SemanticMemoryService

**Files:**
- Modify: `better_memory/services/semantic.py` (ctor ~54; `create` ~63; `update_text` ~85; `create_from_observation` ~125)
- Modify: instantiation sites — grep `SemanticMemoryService(` in `better_memory/mcp/server.py` and `better_memory/storage/sqlite.py`, pass `embedder=`
- Test: `tests/services/test_semantic_embedding_write.py`

**Interfaces:**
- Consumes: bridge + serialisation exactly as Task 6 (duplicate `_embed_text`/`_store_embedding` shape into this service, table `semantic_embeddings`, PK `memory_id`; source text = the memory's `content`).
- Produces: `SemanticMemoryService(conn, *, clock=None, embedder=None)`; embeddings written on `create`, `update_text`, `create_from_observation`.

- [ ] **Step 1: Write the failing tests** (same FakeEmbedder — move it to `tests/services/_embedding_fakes.py` and import from both test files):

```python
# tests/services/test_semantic_embedding_write.py
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
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
        svc = SemanticMemoryService(conn, embedder=fake)
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 1
        assert fake.calls == ["the fact"]

    def test_update_text_reembeds(self, conn):
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, embedder=fake)
        mid = svc.create(content="v1", project="p")
        svc.update_text(id=mid, content="v2")
        assert _vec_count(conn) == 1          # replaced, not duplicated
        assert fake.calls == ["v1", "v2"]

    def test_failure_never_blocks_create(self, conn):
        svc = SemanticMemoryService(conn, embedder=FakeEmbedder(fail=True))
        mid = svc.create(content="the fact", project="p")
        assert mid                              # row created
        assert _vec_count(conn) == 0

    def test_no_embedder_no_rows(self, conn):
        svc = SemanticMemoryService(conn)
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic_embedding_write.py -v`
Expected: FAIL — ctor rejects `embedder` kwarg.

- [ ] **Step 3: Implement** — mirror Task 6 exactly in `semantic.py` (ctor kwarg, `_embed_text`, `_store_embedding` targeting `semantic_embeddings(memory_id, ...)`, calls at the end of `create`, `update_text`, `create_from_observation` before their commits). Wire instantiation sites.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_semantic_embedding_write.py tests/services/test_semantic.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(embeddings): semantic memories embedded on create/update, best-effort

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Three-leg fusion + lazy self-heal in retrieve

**Files:**
- Modify: `better_memory/services/reflection.py` (`_fuse_by_relevance` ~1189; `retrieve_reflections` query path)
- Test: `tests/services/test_vec_fusion.py`

**Interfaces:**
- Consumes: `_embed_text` (Task 6), `reflection_embeddings` rows (Task 6), `sanitize_fts5_query` (existing).
- Produces: `_fuse_by_relevance(rows, *, query, rrf_k=60)` unchanged signature, now internally three-leg; self-heal constant `SELF_HEAL_BATCH_CAP = 20`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_vec_fusion.py
"""Query fusion gains a vector leg; missing embeddings self-heal on retrieve.

Degradation contract: no embedder / Ollama down / row unembedded -> exactly
the two-leg (prior + BM25) behaviour that shipped in #81. The vec leg only
ever promotes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
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
    """Embeds query and one target title to the same vector; noise elsewhere."""

    def __init__(self, match_text: str):
        super().__init__()
        self.match_text = match_text

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.match_text in text:
            return [1.0] + [0.0] * 767
        return [0.0, 1.0] + [0.0] * 766


class TestVecFusion:
    def test_semantically_close_row_promoted_without_token_overlap(self, conn):
        # Query wording shares no tokens with the target title: BM25 misses,
        # the vec leg must carry it.
        _seed(conn, "r-target", title="Stdout handling on win32 interpreters")
        _seed(conn, "r-noise-1", title="Unrelated advice alpha", useful=5, ignored=1)
        _seed(conn, "r-noise-2", title="Unrelated advice beta", useful=4, ignored=1)
        emb = DirectedEmbedder("Stdout handling")
        # pretend the query is 'semantically identical' to the target:
        emb.match_text_query = True

        class QueryDirectedEmbedder(DirectedEmbedder):
            async def embed(self, text):
                self.calls.append(text)
                if "console output" in text or self.match_text in text:
                    return [1.0] + [0.0] * 767
                return [0.0, 1.0] + [0.0] * 766

        emb = QueryDirectedEmbedder("Stdout handling")
        svc = ReflectionSynthesisService(conn, embedder=emb)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="console output disappears on windows",
        )["do"]]
        assert ids[0] == "r-target"

    def test_no_embedder_matches_shipped_behaviour(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = ReflectionSynthesisService(conn)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]                    # BM25-only path still works

    def test_embedder_failure_degrades_silently(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder(fail=True))
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]


class TestSelfHeal:
    def test_unembedded_candidates_healed_on_query_retrieve(self, conn):
        _seed(conn, "r-a", title="Alpha")
        _seed(conn, "r-b", title="Beta")
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything at all")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 2

    def test_heal_capped_at_batch_limit(self, conn):
        from better_memory.services.reflection import SELF_HEAL_BATCH_CAP
        for i in range(SELF_HEAL_BATCH_CAP + 5):
            _seed(conn, f"r-{i:03}", title=f"Title {i}")
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == SELF_HEAL_BATCH_CAP

    def test_no_query_no_heal(self, conn):
        _seed(conn, "r-a", title="Alpha")
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder())
        svc.retrieve_reflections(project="p")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 0

    def test_heal_failure_silent(self, conn):
        _seed(conn, "r-a", title="Alpha")
        svc = ReflectionSynthesisService(conn, embedder=FakeEmbedder(fail=True))
        rows = svc.retrieve_reflections(project="p", query="anything")
        assert rows["do"]                        # retrieval unaffected
```

Clean the first test before running: keep only the `QueryDirectedEmbedder` version (the earlier `emb` assignments are editing artifacts — delete them).

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_vec_fusion.py -v`
Expected: `SELF_HEAL_BATCH_CAP` import error; promotion test fails.

- [ ] **Step 3: Implement**

In `reflection.py`:

```python
#: Max embeddings written per retrieve call by the lazy self-heal. Keeps
#: worst-case added latency bounded (~cap x embed time on first call after
#: a backlog); the CLI backfill is the bulk path.
SELF_HEAL_BATCH_CAP = 20
```

In `retrieve_reflections`, in the `if query:` branch, before fusion:

```python
            if query:
                self._heal_missing_embeddings(rows)
                query_vector = self._embed_text(query)
                rows = self._fuse_by_relevance(
                    rows, query=query, query_vector=query_vector,
                )
                _diag.step(fn, "relevance_fused", n_rows=len(rows))
```

New methods:

```python
    def _heal_missing_embeddings(self, rows) -> None:
        """Embed up to SELF_HEAL_BATCH_CAP candidates that lack vectors.

        Historical rows (embedding written before the write-path existed)
        and write-time failures repair themselves on their first relevant
        retrieval. Entirely best-effort.
        """
        if self._embedder is None or not rows:
            return
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        have = {
            r[0] for r in self._conn.execute(
                f"SELECT reflection_id FROM reflection_embeddings "
                f"WHERE reflection_id IN ({placeholders})", ids,
            )
        }
        todo = [r for r in rows if r["id"] not in have][:SELF_HEAL_BATCH_CAP]
        if not todo:
            return
        texts = [
            _embedding_source_text(
                r["title"], r["use_cases"], json.loads(r["hints"]),
            )
            for r in todo
        ]
        try:
            vectors = run_async_in_worker(self._embedder.embed_batch(texts))
        except Exception:
            return
        for r, vec in zip(todo, vectors):
            self._store_embedding(r["id"], vec)
        self._conn.commit()

    def _vec_ranks(self, query_vector, candidate_ids) -> dict[str, int]:
        """reflection_id -> vec rank (0 = closest) among the candidates."""
        if query_vector is None or not candidate_ids:
            return {}
        try:
            rows = self._conn.execute(
                "SELECT reflection_id FROM reflection_embeddings "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (_serialize_vector(query_vector), max(len(candidate_ids), 50)),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        wanted = set(candidate_ids)
        out: dict[str, int] = {}
        for r in rows:
            if r[0] in wanted:
                out[r[0]] = len(out)
        return out
```

(sqlite-vec kNN cannot take extra predicates — fetch top-k then filter to
candidates, same workaround documented in `search/hybrid.py:262-299`.)

`_fuse_by_relevance` gains the vec leg — signature `(self, rows, *, query, query_vector=None, rrf_k=60)`; after building `rel_rank` (BM25), add:

```python
        vec_rank = self._vec_ranks(query_vector, ids)
```

and in the scoring loop:

```python
            vr = vec_rank.get(row["id"])
            if vr is not None:
                score += 1.0 / (rrf_k + vr)
```

Bail-out condition changes: currently returns rows unchanged when BM25 finds nothing; now return unchanged only when BOTH `rel_rows` and `vec_rank` are empty.

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_vec_fusion.py tests/services/test_reflection_query_relevance.py tests/services/test_wilson_ranking.py tests/services/test_exploration_slot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(retrieval): three-leg RRF fusion (prior + BM25 + vec) with lazy embedding self-heal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Backfill CLI

**Files:**
- Create: `better_memory/cli/backfill_embeddings.py`
- Test: `tests/cli/test_backfill_embeddings.py`

**Interfaces:**
- Consumes: `_embedding_source_text` (Task 6), services' `_store_embedding` shape (duplicated locally — the CLI writes directly, it is a maintenance tool).
- Produces: `python -m better_memory.cli.backfill_embeddings [--home PATH]` — embeds every active reflection and semantic memory missing a vector; prints `backfilled reflections=N semantics=M skipped=K`; exit 0 even when Ollama is down (prints a warning, backfills nothing).

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_backfill_embeddings.py
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.cli.backfill_embeddings import backfill
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def test_backfills_reflections_and_semantics(conn):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at)
           VALUES ('r1', 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""")
    conn.execute(
        """INSERT INTO semantic_memories (id, content, project, scope,
           created_at, updated_at)
           VALUES ('s1', 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""")
    conn.commit()

    stats = backfill(conn, FakeEmbedder())
    assert stats == {"reflections": 1, "semantics": 1, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0] == 1


def test_idempotent(conn):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at)
           VALUES ('r1', 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""")
    conn.commit()
    fake = FakeEmbedder()
    backfill(conn, fake)
    stats = backfill(conn, fake)
    assert stats == {"reflections": 0, "semantics": 0, "skipped": 0}


def test_retired_reflections_skipped(conn):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at, status)
           VALUES ('r1', 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', 'retired')""")
    conn.commit()
    stats = backfill(conn, FakeEmbedder())
    assert stats == {"reflections": 0, "semantics": 0, "skipped": 0}


def test_embed_failure_counted_as_skipped(conn):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at)
           VALUES ('r1', 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""")
    conn.commit()
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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import (
    _embedding_source_text,
    _serialize_vector,
)


def backfill(conn, embedder) -> dict[str, int]:
    stats = {"reflections": 0, "semantics": 0, "skipped": 0}

    refl = conn.execute(
        """SELECT r.id, r.title, r.use_cases, r.hints FROM reflections r
           WHERE r.status IN ('pending_review', 'confirmed')
             AND r.id NOT IN (SELECT reflection_id FROM reflection_embeddings)"""
    ).fetchall()
    for r in refl:
        text = _embedding_source_text(r["title"], r["use_cases"],
                                      json.loads(r["hints"]))
        try:
            vec = asyncio.run(embedder.embed(text))
        except Exception:
            stats["skipped"] += 1
            continue
        conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)", (r["id"], _serialize_vector(vec)))
        stats["reflections"] += 1

    sems = conn.execute(
        """SELECT id, content FROM semantic_memories
           WHERE id NOT IN (SELECT memory_id FROM semantic_embeddings)"""
    ).fetchall()
    for s in sems:
        try:
            vec = asyncio.run(embedder.embed(s["content"]))
        except Exception:
            stats["skipped"] += 1
            continue
        conn.execute(
            "INSERT INTO semantic_embeddings (memory_id, embedding) "
            "VALUES (?, ?)", (s["id"], _serialize_vector(vec)))
        stats["semantics"] += 1

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

    stats = backfill(conn, OllamaEmbedder())
    print(f"backfilled reflections={stats['reflections']} "
          f"semantics={stats['semantics']} skipped={stats['skipped']}")
    if stats["skipped"]:
        print("warning: some rows skipped (Ollama unreachable?); "
              "re-run later or let retrieve self-heal them", file=sys.stderr)


if __name__ == "__main__":
    main()
```

Note: `asyncio.run` per item is fine here — standalone process, no running
loop, and the N≈150 corpus embeds in seconds. FakeEmbedder's `embed` is a
plain coroutine, so `asyncio.run` works in tests too.

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

### Task 10: Website sync, typecheck, full suite

**Files:**
- Modify: `website/index.md` (tools showcase — memory.retrieve description: task-conditioned, Wilson-ranked, semantic fusion)
- Modify: `website/architecture.md` (ranking prose: replace popularity/demotion description with Wilson + exploration + three-leg fusion; add embeddings lifecycle para)
- Modify: `website/configuration.md` — only if it documents ranking constants (grep `OVERLOOKED` / `DEMOTION`; remove if present; no new env vars were added)

**Interfaces:** none — prose.

- [ ] **Step 1: Grep for stale prose**

Run: `grep -rn "useful_count\|OVERLOOKED\|DEMOTION\|popularity" website/ | head -20`

- [ ] **Step 2: Update each hit** to describe: Wilson lower bound on (useful+overlooked)/rated; reserved exploration slot; embeddings written at synthesis + self-heal + backfill CLI; three-leg RRF. Keep each edit to the paragraph that is actually wrong — no rewrites beyond the stale claims.

- [ ] **Step 3: Typecheck**

Run: `./.venv/Scripts/python.exe -m pyright`
Expected: 0 errors

- [ ] **Step 4: Full suite**

Run: `./.venv/Scripts/python.exe -m pytest tests -q`
Expected: everything passes (~1500 tests). Fix any straggler that pinned old ordering before proceeding.

- [ ] **Step 5: Commit**

```bash
git add website
git commit -m "docs(website): ranking + embeddings prose matches PR-A behaviour

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: A/B validation gate, PR, babysit

**Files:**
- Modify: `C:/Users/gethi/source/autoresearch/memuse-260721-run/runner.py` (add arm `A8`: `spec["code"] = str(WT)` equivalent pointing at this branch — copy the `A6` arm, change the code path to the main repo checkout on `feat/retrieval-quality`)
- No repo files.

**Interfaces:** gate = distinct useful% from 24 sessions not statistically below 22.96% (two-proportion z-test, α=0.05, one re-run allowed on borderline).

- [ ] **Step 1: Point the harness at this branch.** In `runner.py` `arm_spec`, add:

```python
    elif arm == "A8":
        # PR-A validation: main repo checkout sits on feat/retrieval-quality.
        spec["code"] = str(MAIN_REPO)
```

Confirm `git -C C:/Users/gethi/source/better-memory branch --show-current` prints `feat/retrieval-quality` before launching — the arm runs whatever that checkout has.

- [ ] **Step 2: Refresh the sandbox base DB** (new schema needed):

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

- [ ] **Step 3: Run the arm**

```bash
python runner.py --arm A8 --repeats 2 --timeout 420 > logs-A8.txt 2>&1
```

(24 sessions, ~2h, ~$40. Run in background; strip-and-retry any 429 rows as done previously.)

- [ ] **Step 4: Evaluate the gate**

```bash
python analyze.py --arms A6,A8
python - <<'EOF'
# two-proportion z-test: A8 distinct vs 22.96% baseline (62/270)
import math
# fill from analyze output:
u8, n8 = USEFUL_A8, EXPOSED_A8
p1, n1 = 62/270, 270
p2 = u8/n8
p = (62 + u8) / (n1 + n8)
z = (p2 - p1) / math.sqrt(p*(1-p)*(1/n1 + 1/n8))
print(f"A8={p2:.4f} baseline={p1:.4f} z={z:.2f}")
print("GATE:", "PASS" if z > -1.645 else "FAIL (one re-run allowed)")
EOF
```

Gate: PASS ⇒ proceed. FAIL twice ⇒ stop, report, do not open the PR.

- [ ] **Step 5: PR + babysit**

```bash
cd C:/Users/gethi/source/better-memory
git push -u origin feat/retrieval-quality
gh pr create --title "feat(retrieval): Wilson prior, exploration slot, embeddings + vec fusion" --body "<summary: spec link, A/B numbers from step 4, migration 0014, backfill CLI deploy note>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Babysit loop (as PR #81/#82): poll `statusCheckRollup` + unresolved `reviewThreads`; fix findings; when CLEAN + zero threads → `gh pr merge --squash --delete-branch`; then run migration + backfill on live:

```bash
git checkout main && git pull origin main
./.venv/Scripts/python.exe -m better_memory.cli.backfill_embeddings
```

(migration 0014 applies on the MCP server's next start; backfill after it — if the table is missing, `apply_migrations` in the CLI's `main` creates it first, so this ordering is safe.)

---

## Self-review notes

- Spec §1-§3 covered by Tasks 1-9; §"Delivery A" gate by Task 11; website guardrail by Task 10. Spec §4 (evidence) and §5 (docs/sentinel) are PR-B / PR-C — separate plans, deliberately absent here.
- Two test snippets contain flagged editing artifacts with explicit cleanup instructions (Task 2 placeholder class, Task 8 first test) — the implementer deletes the marked lines; both are called out inline.
- Task 6's apply-path tests reference `tests/services/test_reflection_writes.py` for the action-construction boilerplate rather than inventing dataclass fields that may drift — the implementer copies the file's existing working setup.
- `_serialize_vector` is defined by copying the exact serialisation used at `observation.py:204-210`; Tasks 8, 9 import it from `reflection.py`.
