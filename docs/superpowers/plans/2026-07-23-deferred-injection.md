# Deferred Injection (PR-D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Near-empty SessionStart injection; the contextual channel becomes primary with a three-leg evidence-gated scorer; exploration serves tagged and excluded from the headline metric.

**Architecture:** `BETTER_MEMORY_INJECT_MODE` flag selects deferred vs legacy bootstrap. `services/relevant.py` is rewritten around BM25 (`reflection_fts`) + vec cosine (unit-norm vectors ⇒ L2 threshold) + Wilson prior, with an evidence gate that keeps popularity from qualifying anything. Hook processes get a file-persisted embed cooldown because the in-process breaker dies with each hook process. CLAUDE.snippet rewrite + drift sentinel ship in the same PR (absorbed PR-C).

**Tech Stack:** Python 3.12, sqlite + sqlite-vec + FTS5, Ollama nomic-embed-text (768-dim unit-norm), pytest.

**Spec:** `docs/superpowers/specs/2026-07-23-deferred-injection-design.md`

## Global Constraints

- Branch: `feat/deferred-injection` (exists; spec `f75143a`).
- Test command: `./.venv/Scripts/python.exe -m pytest <path> -v`; typecheck `./.venv/Scripts/python.exe -m pyright` stays 0 errors.
- Hooks NEVER raise and never block (existing `except BaseException` shells stay).
- `legacy` mode must be byte-identical to today's bootstrap output; flag misparse coerces to `legacy`.
- Every Ollama touchpoint best-effort; hook-side stall bounded by the file cooldown.
- ASCII only in code/comments/CLI output. Ruff line length 100.
- Website sync guardrail (conf-1.0): prose updated in-PR (Task 9).
- Commits per task, footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Verified-against-source facts (do not re-derive)

| Fact | Where verified |
|---|---|
| Stored reflection embeddings are unit-norm (5/5 sampled, norm 1.0000) ⇒ cosine c ⇔ L2 dist² ≤ 2(1−c); floor 0.55 ⇒ dist² ≤ 0.9 | live DB probe 2026-07-23 |
| Hook = fresh process per firing ⇒ in-process breaker state dies each firing; needs file persistence | `hooks/contextual_inject.py` structure (main() per invocation) |
| Hook currently builds backend with `embedder=None`; payload carries `session_id`, `cwd`, `prompt`/`tool_name`+`tool_input`; mode gate `_enabled(event, cfg.context_inject_mode)` | `contextual_inject.py:62-116` |
| `SeenStore`: JSON file per session in `<home>/state`, `{"turn": int, "seen": {"kind:id": turn}}`, `bump_turn/filter_unseen/mark_seen`, `prune_stale(7d)`; corrupt ⇒ empty | `services/context_seen.py` |
| `retrieve_relevant(backend, *, query, project, min_hits, max_items, include_neutral, now)` returns `RelevantMemory(kind,id,text,polarity,confidence,useful_count,age_days,hits,score)`; `format_relevant` renders block; `_activation` + `min_hits` are the parts being deleted | `services/relevant.py` (full read) |
| Semantic memories have NO FTS index (LIKE/trigram only) ⇒ no BM25 leg for semantics | schema + `services/semantic.py` |
| kNN template: `SELECT reflection_id, distance FROM reflection_embeddings WHERE embedding MATCH ? AND k = ? ORDER BY distance` (distance selectable) | `search/hybrid.py:274-296`, PR-A `_vec_ranks` |
| Bootstrap render: semantic full + reflection full slices by `bootstrap_top_n`, index lines, `_FOOTER`; exposures recorded only for full-rendered ids | `services/session_bootstrap.py:188-316` |
| Config pattern: dataclass field + `_resolve_*` in `get_config()`; existing keys `context_inject_mode`, `context_min_hits`, `context_max_items`, `context_reinject_turns`, `bootstrap_top_n` | `config.py:225-357` |
| Hook install shapes are golden-value-tested (`tests/cli/test_install_hooks.py`, `tests/e2e/test_install_hooks.py::EXPECTED_ENTRIES`, `tests/e2e/test_setup_sh.py`) — PreToolUse matcher change must update them | prior PR experience (#81) |
| `SyncEmbedder(factory, *, clock, cooldown, timeout)`; `_down_until` in-process; `embed_text/embed_batch` → value or None | `embeddings/sync_embed.py` |
| `wilson_lower_bound(positive, n)` in `services/scoring.py`; counters exposed on retrieve rows and `SemanticMemory` incl. `times_ignored` | PR-A |
| Exposure write is first-source-wins INSERT..WHERE NOT EXISTS in three places; `record_exposures(session_id, items, source)` on backend | #81 dedup + `storage/protocol.py` |
| Migration numbering: 0014 is the latest; THIS PR takes **0015** (PR-B renumbers to 0016) | `db/migrations/` |

---

### Task 1: Config keys — inject mode + vec floor

**Files:**
- Modify: `better_memory/config.py` (dataclass ~225-235, resolver ~348-357)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `Config.inject_mode: Literal["deferred", "legacy"]` (env `BETTER_MEMORY_INJECT_MODE`, default `"legacy"`, unknown values coerce to `"legacy"`); `Config.context_vec_floor: float` (env `BETTER_MEMORY_CONTEXT_VEC_FLOOR`, default `0.55`, clamped to [0.0, 1.0], malformed ⇒ default). Consumed by Tasks 4, 5, 6.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`, mirroring its existing monkeypatch-env style — read the file's newest config test first and copy its fixture usage):

```python
class TestInjectModeConfig:
    def test_default_is_legacy(self, monkeypatch):
        monkeypatch.delenv("BETTER_MEMORY_INJECT_MODE", raising=False)
        assert get_config().inject_mode == "legacy"

    def test_deferred_selected(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        assert get_config().inject_mode == "deferred"

    def test_unknown_coerces_to_legacy(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "yolo")
        assert get_config().inject_mode == "legacy"


class TestVecFloorConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", raising=False)
        assert get_config().context_vec_floor == 0.55

    def test_override_and_clamp(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "0.7")
        assert get_config().context_vec_floor == 0.7
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "1.7")
        assert get_config().context_vec_floor == 1.0

    def test_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "high")
        assert get_config().context_vec_floor == 0.55
```

(If `get_config` caches, mirror how existing tests bust the cache — check the file.)

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_config.py -v -k "InjectMode or VecFloor"`
Expected: FAIL — attributes missing.

- [ ] **Step 3: Implement** — dataclass fields:

```python
    inject_mode: Literal["deferred", "legacy"]
    context_vec_floor: float
```

Resolvers (place beside `_resolve_context_inject_mode`, following its shape):

```python
def _resolve_inject_mode() -> Literal["deferred", "legacy"]:
    raw = (os.environ.get("BETTER_MEMORY_INJECT_MODE") or "legacy").strip().lower()
    # Fail-safe: anything unrecognised means today's behaviour.
    return "deferred" if raw == "deferred" else "legacy"


def _resolve_vec_floor() -> float:
    raw = os.environ.get("BETTER_MEMORY_CONTEXT_VEC_FLOOR")
    if raw is None:
        return 0.55
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        return 0.55
```

Wire both into the `get_config()` constructor call.

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/test_config.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/config.py tests/test_config.py
git commit -m "feat(config): inject_mode + context_vec_floor keys

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Migration 0015 — via_exploration tag + write-path threading

**Files:**
- Create: `better_memory/db/migrations/0015_via_exploration.sql`
- Modify: `better_memory/services/reflection.py` (bucket-fill from Task PR-A.4; exposure write ~1500s)
- Test: `tests/db/test_schema.py` (append), `tests/services/test_exploration_tagging.py`

**Interfaces:**
- Produces: `session_memory_exposure.via_exploration INTEGER NOT NULL DEFAULT 0`; `retrieve_reflections` writes 1 for the exposure row of a memory that entered its bucket via the reserved slot. `_bucket_item` gains an `"_exploration": bool` transient key stripped before return (or a parallel id-set — see Step 3). Consumed by the harness metric (Task 10).

- [ ] **Step 1: Write the failing tests**

Migration test (append to `tests/db/test_schema.py`, same pattern as 0014's):

```python
def test_0015_via_exploration_column(tmp_memory_db):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(session_memory_exposure)")]
    assert "via_exploration" in cols
    conn.close()
```

Tagging tests:

```python
# tests/services/test_exploration_tagging.py
"""Exploration-slot serves are tagged so the headline metric can exclude them.

Exploration is an investment the ranker makes, not a relevance claim;
counting it in useful% punishes the system for learning (measured ~2-4pts
drag in the PR-A A/B). Rating flow is unchanged — explorers still rated.
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


def _seed(conn, rid, *, useful=0, ignored=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?, ?)""",
        (rid, rid, useful, ignored),
    )
    conn.commit()


def _flags(conn):
    return {
        r[0]: r[1] for r in conn.execute(
            "SELECT memory_id, via_exploration FROM session_memory_exposure")
    }


class TestExplorationTagging:
    def test_slot_serve_tagged_others_not(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=5, ignored=5)
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=3)
        flags = _flags(conn)
        assert flags["r-untested"] == 1
        assert flags["r-proven-0"] == 0

    def test_dedup_wins_over_tag(self, conn, monkeypatch):
        # Memory already exposed normally: a later exploration serve writes
        # nothing, so the flag stays 0 (first-source-wins).
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-x")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=None)   # normal serve
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=5, ignored=5)
        svc.retrieve_reflections(project="p", limit_per_bucket=3)      # r-x now explorer
        assert _flags(conn)["r-x"] == 0

    def test_unlimited_cap_never_tags(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=None)
        assert _flags(conn)["r-untested"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/db -q -k 0015 && ./.venv/Scripts/python.exe -m pytest tests/services/test_exploration_tagging.py -v`
Expected: FAIL — column missing.

- [ ] **Step 3: Implement**

Migration:

```sql
-- Migration 0015: tag exposures that came from the exploration slot.
--
-- The reserved per-bucket slot (spec 2026-07-23 retrieval-quality, section 2)
-- serves under-rated memories to earn them ratings. Those serves are an
-- investment the ranker makes, not a relevance claim, so the headline
-- usefulness metric excludes them. Ratings still apply to them normally.

ALTER TABLE session_memory_exposure
    ADD COLUMN via_exploration INTEGER NOT NULL DEFAULT 0;
```

In `reflection.py`'s two-pass bucket fill: collect the ids that took the
reserved slot into a local set (`exploration_ids: set[str]`) — the index
chosen from `untested_idx[0]` per bucket. Then in the exposure write for the
retrieve path, thread the flag (the write is the INSERT..WHERE NOT EXISTS
from #81):

```python
                        self._conn.executemany(
                            "INSERT INTO session_memory_exposure "
                            "(session_id, memory_kind, memory_id, exposed_at, "
                            " source, via_exploration) "
                            "SELECT ?, 'reflection', ?, ?, 'retrieve', ? "
                            "WHERE NOT EXISTS ("
                            "  SELECT 1 FROM session_memory_exposure "
                            "  WHERE session_id = ? AND memory_kind = 'reflection' "
                            "    AND memory_id = ?)",
                            [(sid, rid, now, 1 if rid in exploration_ids else 0,
                              sid, rid) for rid in all_ids],
                        )
```

(`all_ids` already exists in that block. `exploration_ids` is empty when
`reserve` is False — the unlimited-cap case, pinning test 3.)

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/db tests/services/test_exploration_tagging.py tests/services/test_exposure_dedup.py tests/services/test_exploration_slot.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(db): tag exploration-slot exposures (migration 0015)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: File-persisted embed cooldown for hook processes

**Files:**
- Modify: `better_memory/embeddings/sync_embed.py`
- Test: `tests/embeddings/test_sync_embed.py` (append)

**Interfaces:**
- Produces: `SyncEmbedder(factory, *, clock=time.monotonic, cooldown=60.0, timeout=15.0, down_state_file: Path | None = None)`. When `down_state_file` is set: before any embed, if the file exists and holds a wall-clock epoch (`time.time()`) in the future ⇒ instant None; on failure, write `time.time() + cooldown` to it (best-effort). In-process `_down_until` behaviour unchanged. Consumed by Task 5 (hooks).

**Why:** hooks run a fresh process per firing; the in-process breaker dies with the process, so an Ollama outage would stall EVERY prompt ~5s instead of once per cooldown window. Wall-clock (not monotonic) because the timestamp crosses processes.

- [ ] **Step 1: Write the failing tests** (append):

```python
class TestFilePersistedCooldown:
    def test_failure_writes_down_file_and_second_instance_skips(self, tmp_path):
        down = tmp_path / "embed_down_until"
        fake = FakeEmbedder(fail=True)
        s1 = SyncEmbedder(lambda: fake, down_state_file=down)
        assert s1.embed_text("x") is None
        assert down.exists()
        # Fresh instance = fresh hook process. Must skip without touching
        # the embedder.
        fake2 = FakeEmbedder()
        s2 = SyncEmbedder(lambda: fake2, down_state_file=down)
        assert s2.embed_text("y") is None
        assert fake2.calls == []

    def test_expired_down_file_allows_embedding(self, tmp_path):
        down = tmp_path / "embed_down_until"
        down.write_text("1.0", encoding="utf-8")          # epoch 1970 — expired
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake, down_state_file=down)
        assert s.embed_text("x") is not None

    def test_corrupt_down_file_is_ignored(self, tmp_path):
        down = tmp_path / "embed_down_until"
        down.write_text("not-a-number", encoding="utf-8")
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake, down_state_file=down)
        assert s.embed_text("x") is not None

    def test_no_file_param_keeps_old_behaviour(self):
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake)
        assert s.embed_text("x") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/embeddings/test_sync_embed.py -v -k FilePersisted`
Expected: FAIL — unexpected keyword `down_state_file`.

- [ ] **Step 3: Implement** — ctor stores `self._down_file = down_state_file`; in `_run`, after the in-process check:

```python
        if self._down_file is not None and self._file_down():
            return None
```

and in the exception handler, alongside setting `_down_until`:

```python
            self._write_down_file()
```

Helpers:

```python
    def _file_down(self) -> bool:
        try:
            until = float(self._down_file.read_text(encoding="utf-8").strip())
        except Exception:
            return False
        return time.time() < until

    def _write_down_file(self) -> None:
        if self._down_file is None:
            return
        try:
            self._down_file.parent.mkdir(parents=True, exist_ok=True)
            self._down_file.write_text(
                str(time.time() + self._cooldown), encoding="utf-8")
        except Exception:
            pass
```

(Wall-clock `time.time()` here deliberately, documented in the docstring;
the injected `clock` stays monotonic for the in-process window.)

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/embeddings -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add better_memory/embeddings/sync_embed.py tests/embeddings/test_sync_embed.py
git commit -m "feat(embeddings): file-persisted embed cooldown for per-process hooks

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: relevant.py rewrite — three-leg scorer + evidence gate

**Files:**
- Modify: `better_memory/services/relevant.py` (full rewrite of scoring; `RelevantMemory` + `format_relevant` keep their shapes)
- Delete tests: the `_activation`/min-hits-specific cases in `tests/services/test_relevant.py` (replace file content; `tests/services/test_relevant_format.py` stays untouched — format unchanged)
- Test: `tests/services/test_relevant.py` (rewritten)

**Interfaces:**
- Consumes: `wilson_lower_bound` (scoring.py), `sanitize_fts5_query` (search/query.py), `sqlite_vec.serialize_float32`, `count_keyword_hits`/`extract_keywords` (keywords.py — kept for the fallback path), `SyncEmbedder` instance.
- Produces:

```python
def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    conn: sqlite3.Connection | None = None,      # sqlite FTS/vec access; None = fallback path
    sync_embedder: Any = None,                    # SyncEmbedder | None
    vec_floor: float = 0.55,
    max_items: int = 3,
    include_neutral: bool = False,
    now: Callable[[], datetime] | None = None,
) -> list[RelevantMemory]
```

`RelevantMemory` unchanged except `hits` now holds BM25-or-fallback hit count (0 allowed when vec-qualified). `min_hits` parameter and `_activation` deleted. Consumed by Task 5.

- [ ] **Step 1: Write the failing tests** (replace `tests/services/test_relevant.py`; read the old file first and keep any fixture helpers that seed reflections through the backend — the new tests construct a real sqlite backend the same way the old ones did; copy that setup):

```python
# tests/services/test_relevant.py  (rewritten for the three-leg gate)
"""Contextual relevance: BM25 + vec + Wilson prior behind an evidence gate.

The gate is the point: a memory injects only with positive relevance
evidence (BM25 match on the query, or vec cosine >= floor). The Wilson
prior RANKS qualifiers but can never qualify a memory alone — popularity
must not force irrelevant injections (that failure mode measured 13% useful
as bootstrap). Vectors are unit-norm, so cosine >= c is L2 dist^2 <= 2(1-c).
"""
```

Test cases (write full bodies mirroring the old file's backend-construction
helper; each seeds via SQL like PR-A tests):

1. `test_bm25_match_qualifies` — reflection titled "Retention archives by
   confidence"; query "how does retention archive things"; no embedder;
   returned.
2. `test_no_evidence_no_injection` — popular reflection (useful=50) with
   unrelated title; query shares no tokens; no embedder ⇒ `[]` (Wilson
   cannot qualify alone).
3. `test_vec_match_qualifies_without_token_overlap` — DirectedEmbedder
   (import from `tests/services/_embedding_fakes.py`; extend that file with
   the PR-A `DirectedEmbedder` if not already shared) maps query and target
   title to the same unit vector; embedding stored for the reflection
   (INSERT via `sqlite_vec.serialize_float32`); query wording shares no
   tokens ⇒ returned.
4. `test_vec_below_floor_does_not_qualify` — embedder maps query and memory
   to orthogonal unit vectors (cos 0) ⇒ `[]`.
5. `test_wilson_ranks_among_qualifiers` — two BM25-qualifying reflections,
   one 5/6 useful-rated, one 0/6 ⇒ better hit-rate first.
6. `test_semantic_qualifies_via_vec` — semantic memory + matching directed
   embedding in `semantic_embeddings` ⇒ returned with kind "semantic".
7. `test_semantic_fallback_keyword_when_no_embedder` — no embedder;
   semantic content shares >=2 distinct keywords with query ⇒ returned
   (keyword fallback keeps semantics reachable without Ollama).
8. `test_max_items_cap` — 5 qualifiers ⇒ 3 returned.
9. `test_no_conn_falls_back_to_keywords` — `conn=None` (agentcore shape):
   reflections qualify via >=2 keyword hits, ranked by Wilson; no FTS/vec
   used (no crash).
10. `test_embedder_failure_degrades_to_bm25` — FakeEmbedder(fail=True);
    BM25-qualifying memory still returned.

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_relevant.py -v`
Expected: FAIL — new signature/behaviour absent.

- [ ] **Step 3: Implement**

Rewrite the scoring half of `relevant.py` (keep `RelevantMemory`, `_age_days`,
`format_relevant`, header/footer constants):

```python
_FALLBACK_MIN_HITS = 2   # keyword evidence floor when FTS/vec are unavailable


def _bm25_qualifiers(conn, query: str) -> dict[str, int]:
    """reflection_id -> BM25 rank (0 best) for reflections matching query."""
    sanitized = sanitize_fts5_query(query)
    tokens = [t for t in sanitized.split() if len(t) > 2]
    if not tokens or conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT r.id, bm25(reflection_fts) AS bm "
            "FROM reflection_fts JOIN reflections r ON r.rowid = reflection_fts.rowid "
            "WHERE reflection_fts MATCH ? ORDER BY bm ASC",
            (" OR ".join(tokens),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: i for i, row in enumerate(rows)}


def _vec_qualifiers(conn, table: str, id_col: str, query_vector, vec_floor: float,
                    ) -> dict[str, int]:
    """id -> vec rank for rows within the cosine floor (unit-norm vectors:
    cosine >= c  <=>  L2 distance^2 <= 2*(1-c))."""
    if conn is None or query_vector is None:
        return {}
    max_dist_sq = 2.0 * (1.0 - vec_floor)
    try:
        rows = conn.execute(
            f"SELECT {id_col}, distance FROM {table} "
            f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(query_vector), 50),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, int] = {}
    for row in rows:
        if float(row[1]) ** 2 <= max_dist_sq or float(row[1]) <= max_dist_sq:
            # sqlite-vec reports L2 distance; accept either dist or dist^2
            # convention defensively — see Step 3a calibration note.
            out[row[0]] = len(out)
    return out
```

**Step 3a (MANDATORY, do before finalising `_vec_qualifiers`):** probe which
convention sqlite-vec returns (distance vs squared distance) with a 3-line
script against two known unit vectors (`[1,0,...]` vs `[0,1,...]` ⇒ L2
distance sqrt(2)≈1.414, squared 2.0). Keep ONLY the correct comparison and
delete the defensive double-check; add the probe result as a comment.

Main function: fetch buckets + semantics through the backend exactly as the
old code did (`track_exposure=False`); compute:

```python
    bm = _bm25_qualifiers(conn, query)
    qvec = sync_embedder.embed_text(query) if sync_embedder is not None else None
    vec_r = _vec_qualifiers(conn, "reflection_embeddings", "reflection_id",
                            qvec, vec_floor)
    vec_s = _vec_qualifiers(conn, "semantic_embeddings", "memory_id",
                            qvec, vec_floor)
    keywords = extract_keywords(query)     # fallback evidence only
```

Qualification per reflection row `r`:

```python
    fts_unavailable = conn is None
    qualifies = (r_id in bm) or (r_id in vec_r) or (
        fts_unavailable and count_keyword_hits(text, keywords) >= _FALLBACK_MIN_HITS
    )
```

Semantic rows: `s_id in vec_s or (qvec is None and keyword hits >= _FALLBACK_MIN_HITS)`
(semantics have no FTS — the keyword fallback applies whenever the vec leg
is absent, not only when conn is None).

Score for ranking (RRF over available legs + Wilson prior rank):

```python
    # Build rank lists among qualifiers, then RRF-fuse:
    #   prior_rank: order by wilson_lower_bound desc (reflections use
    #   useful+overlooked / +ignored counters; semantics likewise)
    #   bm rank, vec rank: from the dicts above (absent -> no term)
    score = sum(1.0 / (60 + rank) for rank in present_ranks)
```

Sort `(-score, id)`, cap `max_items`. `hits` field = BM25 presence ? keyword
hit count for display : fallback hits (0 when vec-only). Delete
`_activation`; `min_hits` parameter gone.

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/services/test_relevant.py tests/services/test_relevant_format.py -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(contextual): three-leg evidence-gated relevance scorer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Hook wiring — embedder, latch, matcher

**Files:**
- Modify: `better_memory/hooks/contextual_inject.py`
- Modify: `better_memory/services/context_seen.py` (latch flag)
- Modify: `better_memory/cli/install_hooks.py` (PreToolUse matcher `Skill|Task|Write` → None/all-tools)
- Modify: `tests/cli/test_install_hooks.py`, `tests/e2e/test_install_hooks.py` (EXPECTED_ENTRIES), `tests/e2e/test_setup_sh.py` (golden shapes)
- Test: `tests/hooks/test_contextual_inject.py` (extend existing file — read it first, follow its payload-driven style), `tests/services/test_context_seen.py` (append latch tests)

**Interfaces:**
- Consumes: Task 3's `down_state_file`, Task 4's signature, Task 1's config.
- Produces: `SeenStore.pretool_fired() -> bool` and `SeenStore.mark_pretool_fired() -> None` (persisted as `{"pretool_fired": true}` in the same JSON). Hook behaviour: PreToolUse events after the first per session exit immediately (before any DB/embedder work); embedder built as `SyncEmbedder(lambda: OllamaEmbedder(timeout=5.0, max_retries=1), down_state_file=cfg.home / "state" / "embed_down_until")` when `cfg.embeddings_backend == "ollama"` else None.

- [ ] **Step 1: Write the failing tests**

`test_context_seen.py` additions:

```python
class TestPretoolLatch:
    def test_defaults_false_then_persists(self, tmp_path):
        s = SeenStore(tmp_path, "sess")
        assert s.pretool_fired() is False
        s.mark_pretool_fired()
        assert SeenStore(tmp_path, "sess").pretool_fired() is True

    def test_corrupt_state_means_not_fired(self, tmp_path):
        (tmp_path / "context_seen_sess.json").write_text("{", encoding="utf-8")
        assert SeenStore(tmp_path, "sess").pretool_fired() is False
```

`test_contextual_inject.py` additions (mirror the file's existing
subprocess/payload harness — read first):
- PreToolUse fires once: two PreToolUse payloads same session ⇒ second emits
  empty additionalContext and does not bump `contextual_fired_pretool` twice.
- UserPromptSubmit unaffected by the latch.

- [ ] **Step 2: Run to verify failure** — latch methods missing.

- [ ] **Step 3: Implement**

`context_seen.py`:

```python
    def pretool_fired(self) -> bool:
        return bool(self._data.get("pretool_fired"))

    def mark_pretool_fired(self) -> None:
        self._data["pretool_fired"] = True
        self._save()
```

`contextual_inject.py` in `main()`, after `seen = SeenStore(...)`:

```python
            if event == "PreToolUse":
                if seen.pretool_fired():
                    raise _SkipInjection()      # module-local sentinel; caught
                seen.mark_pretool_fired()       # below, renders empty output
```

(Implement with a small module-level `class _SkipInjection(Exception)` caught
in the existing try to leave `rendered = ""` — do NOT use BaseException
paths for control flow; add an explicit `except _SkipInjection: rendered=""`
BEFORE the broad handler.)

Embedder + new call:

```python
                sync_embedder = None
                if cfg.embeddings_backend == "ollama":
                    from better_memory.embeddings.ollama import OllamaEmbedder
                    from better_memory.embeddings.sync_embed import SyncEmbedder
                    sync_embedder = SyncEmbedder(
                        lambda: OllamaEmbedder(timeout=5.0, max_retries=1),
                        down_state_file=cfg.home / "state" / "embed_down_until",
                    )
                items = retrieve_relevant(
                    backend, query=query, project=project,
                    conn=conn,
                    sync_embedder=sync_embedder,
                    vec_floor=cfg.context_vec_floor,
                    max_items=cfg.context_max_items,
                )
```

(imports move to module top per house style; `conn` is None in agentcore
mode already — matches Task 4's fallback contract. `cfg.context_min_hits`
config key stays for back-compat but is no longer read here; note it as
deprecated in config.py's comment.)

`install_hooks.py`: PreToolUse HookSpec matcher `"Skill|Task|Write"` → `None`
(unscoped = all tools), with a comment: first-fire latch in the hook makes
an unscoped matcher cheap — one real firing per session, later events
short-circuit on the state file. Update the three golden-shape test files
accordingly (matcher None ⇒ no `matcher` key in the group, mirroring how
SessionStart/Stop entries assert).

- [ ] **Step 4: Run**

Run: `./.venv/Scripts/python.exe -m pytest tests/hooks tests/services/test_context_seen.py tests/cli/test_install_hooks.py tests/e2e/test_install_hooks.py tests/e2e/test_setup_sh.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(hooks): contextual inject gains vec leg, per-session PreToolUse latch, all-tools matcher

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Deferred bootstrap mode

**Files:**
- Modify: `better_memory/services/session_bootstrap.py` (render path, ~188-316)
- Test: `tests/services/test_session_bootstrap.py` (append class)

**Interfaces:**
- Consumes: `get_config().inject_mode` (Task 1).
- Produces: in `deferred` mode `bootstrap()` renders: header (unchanged), ALL general-scope semantic memories in full (existing `_render_semantic_full`), one index line `"better-memory knows {n_refl} reflections + {n_sem} semantic memories for this project; relevant ones will surface as you work - or ask via memory_retrieve with a task query."`, footer (unchanged). Exposures recorded ONLY for the general semantics rendered. Project-scoped semantics and ALL reflections: neither rendered nor exposed (counts still computed for the index line). `legacy` mode: byte-identical output to today.

- [ ] **Step 1: Write the failing tests** (append; reuse the file's existing fixtures/seed helpers — read it first):

```python
class TestDeferredBootstrap:
    def test_deferred_renders_general_semantics_and_index_only(self, ..., monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        # seed: 2 general semantics, 3 project semantics, 4 reflections
        out = <bootstrap call per file's pattern>
        assert "<general semantic content>" in out.context
        assert "<project semantic content>" not in out.context
        assert "<reflection title>" not in out.context
        assert "knows 4 reflections + 5 semantic memories" in out.context

    def test_deferred_exposes_only_general_semantics(self, ..., monkeypatch):
        # session_memory_exposure rows: exactly the general-semantic ids,
        # source='bootstrap'

    def test_legacy_mode_byte_identical(self, ..., monkeypatch):
        # render once with flag unset, once with BETTER_MEMORY_INJECT_MODE=legacy,
        # assert outputs equal; then with =deferred assert different
```

(Write full bodies against the file's real fixture names during
implementation — the assertions above are the contract; the seeding
boilerplate is copied from neighbouring tests in the same file.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — in `bootstrap()`, after fetching semantics +
reflections (fetches stay — the index line needs counts), branch:

```python
        if get_config().inject_mode == "deferred":
            general = [s for s in semantics if s.scope == "general"]
            n_refl = sum(len(v) for v in refl_buckets.values())
            n_sem = len(semantics)
            parts = [_HEADER..., *(_render_semantic_full(s) for s in general),
                     f"better-memory knows {n_refl} reflections + {n_sem} "
                     "semantic memories for this project; relevant ones will "
                     "surface as you work - or ask via memory_retrieve with "
                     "a task query.",
                     _FOOTER]
            self._record_exposure(session_id=session_id,
                                  reflection_ids=[],
                                  semantic_ids=[s.id for s in general])
            return <same return shape as legacy path>
```

(Adapt names to the function's real locals — the implementer reads the
function; the contract is fixed by the tests. The legacy path is NOT
touched — same lines, same order.)

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/services/test_session_bootstrap.py tests/hooks -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(bootstrap): deferred mode - general semantics + index line only

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: CLAUDE.snippet rewrite + drift sentinel

**Files:**
- Modify: `better_memory/skills/CLAUDE.snippet.md` (behavioural rewrite)
- Create: `better_memory/hooks/_claude_md_sentinel.py`
- Modify: `better_memory/hooks/session_bootstrap.py` (append sentinel warning to additionalContext, best-effort)
- Test: `tests/hooks/test_claude_md_sentinel.py`

**Interfaces:**
- Produces: `check_claude_md(text: str, schemas: dict[str, set[str]]) -> list[str]` — pure function; `schemas` maps MCP-rendered tool name (e.g. `memory_retrieve`) to its valid property names; returns warning strings. `build_schemas() -> dict[str, set[str]]` derives it from `better_memory.mcp.tools.tool_definitions()` (wire name dots → underscores). Hook glue reads `~/.claude/CLAUDE.md` (path via `Path.home()`), appends at most one warning line.

- [ ] **Step 1: Write the failing tests**

```python
# tests/hooks/test_claude_md_sentinel.py
"""CLAUDE.md drift sentinel: prose that enumerates tool params rots.

The 2026-07 incident: the user-level CLAUDE.md documented component/
scope_path/window on memory_retrieve for weeks after they ceased to exist,
training every session to make silently-degraded calls. The rewrite removes
enumerations; the sentinel catches regressions.
"""
from __future__ import annotations

from better_memory.hooks._claude_md_sentinel import build_schemas, check_claude_md


def test_phantom_param_detected():
    schemas = {"memory_retrieve": {"query", "project", "tech"}}
    text = "call memory_retrieve with query and scope_path=src/"
    warnings = check_claude_md(text, schemas)
    assert warnings and "scope_path" in warnings[0]


def test_valid_params_silent():
    schemas = {"memory_retrieve": {"query", "project", "tech"}}
    text = "call memory_retrieve with query='task' and project=x"
    assert check_claude_md(text, schemas) == []


def test_lines_without_tool_names_ignored():
    schemas = {"memory_retrieve": {"query"}}
    assert check_claude_md("window=30d is a fine phrase alone", schemas) == []


def test_common_words_not_flagged():
    schemas = {"memory_retrieve": {"query"}}
    # 'e.g.' / 'i.e.' style tokens and words without = or : suffix don't count
    assert check_claude_md("memory_retrieve is documented here", schemas) == []


def test_build_schemas_covers_retrieve():
    schemas = build_schemas()
    assert "query" in schemas["memory_retrieve"]


def test_malformed_input_never_raises():
    assert check_claude_md("", {}) == []
```

- [ ] **Step 2: Run to verify failure** — module missing.

- [ ] **Step 3: Implement**

```python
# better_memory/hooks/_claude_md_sentinel.py
"""Detect parameter-enumeration drift in the user's CLAUDE.md.

Pure functions; the session_bootstrap hook wires them in best-effort.
Only lines that mention a better-memory tool name are scanned, and only
tokens shaped like parameter usage (word= or word:) are checked against
the live schema, so prose can't false-positive.
"""

from __future__ import annotations

import re

_PARAM_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\s*[=:]")
_IGNORE = {"http", "https", "note", "example", "warning", "default"}


def build_schemas() -> dict[str, set[str]]:
    from better_memory.mcp.tools import tool_definitions
    out: dict[str, set[str]] = {}
    for tool in tool_definitions():
        rendered = tool.name.replace(".", "_")
        props = set((tool.inputSchema or {}).get("properties", {}).keys())
        out[rendered] = props
    return out


def check_claude_md(text: str, schemas: dict[str, set[str]]) -> list[str]:
    warnings: list[str] = []
    try:
        for line in (text or "").splitlines():
            hit_tools = [name for name in schemas if name in line]
            if not hit_tools:
                continue
            for token in _PARAM_RE.findall(line):
                if token in _IGNORE or any(token in schemas[t] for t in hit_tools):
                    continue
                if token in schemas:      # a tool name followed by ':' etc.
                    continue
                warnings.append(
                    f"CLAUDE.md documents parameter '{token}' near "
                    f"{'/'.join(hit_tools)} but the live tool schema has no "
                    "such parameter - fix or drop the enumeration.")
    except Exception:
        return []
    return warnings[:1]     # at most one line of noise per session
```

Hook glue in `session_bootstrap.py` (inside the existing try, after the
context is built): read `Path.home() / ".claude" / "CLAUDE.md"` (missing →
skip), run `check_claude_md(text, build_schemas())`, append the single
warning to the additionalContext string.

Snippet rewrite (`CLAUDE.snippet.md`): behavioural only. Core lines:
retrieval — "When you begin a task, call `memory_retrieve` with a `query`
describing it. Do not do broad no-query retrieval at session start; memories
surface contextually as you work."; knowledge — startup `knowledge_list`
mandate unchanged; recording — unchanged sections carried over minus every
parameter table/enumeration; credit — "credit with a one-line evidence
statement when a memory shapes your work" (forward-compatible with PR-B).

- [ ] **Step 4: Run** `./.venv/Scripts/python.exe -m pytest tests/hooks -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(docs): behavioural CLAUDE.snippet + parameter-drift sentinel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Vec-floor calibration

**Files:**
- Create: `C:/Users/gethi/source/autoresearch/memuse-260721-run/calibrate_floor.py` (harness-side, not committed to repo)
- Possibly modify: `better_memory/config.py` default (only if calibration contradicts 0.55)

**Interfaces:** consumes the A/B corpus (192 sandbox session DBs + their prompts in `tasks.json`) and live embeddings.

- [ ] **Step 1:** Write `calibrate_floor.py`: for each (task prompt, memory rated cited/shaped in that task's sessions) pair → positive example; for each (prompt, memory rated ignored ≥3 times for that task) → negative. Embed prompts + memory texts via `OllamaEmbedder` (direct asyncio.run, batches). Report cosine distributions and the floor maximising F1, plus precision/recall at 0.45/0.50/0.55/0.60/0.65.
- [ ] **Step 2:** Run it. If best-F1 floor ∈ [0.50, 0.60], keep default 0.55 (within noise); else change the `config.py` default in a dedicated commit citing the numbers.
- [ ] **Step 3:** Paste the output table into the task report; note the chosen floor in the plan ledger.

---

### Task 9: Website sync, pyright, full suite

**Files:** `website/architecture.md`, `website/configuration.md`, `website/mcp-tools.md`, `website/index.md` (only stale paragraphs)

- [ ] **Step 1:** `grep -rn "bootstrap\|inject\|BOOTSTRAP_TOP_N\|min_hits\|context_min" website/ | head -30`; update: bootstrap section describes both modes + flag (legacy default until gate); contextual section describes three-leg gate + floor + latch; configuration.md documents `BETTER_MEMORY_INJECT_MODE`, `BETTER_MEMORY_CONTEXT_VEC_FLOOR`, marks `BETTER_MEMORY_CONTEXT_MIN_HITS` deprecated. Semantic synonyms in the grep (lesson: "usefulness" escaped the last sweep — also grep "relevant\|keyword").
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q` → zero failures.
- [ ] **Step 4:** Commit `docs(website): deferred-injection prose + config keys`.

---

### Task 10: Merge (legacy default), deploy, env-flag A/B gate, flip

**Interfaces:** gate = deferred arm's headline useful% (via_exploration=0 denominator) not statistically below legacy arm's (one-sided z, α=0.05, 24 sessions/arm) AND deferred injection precision ≥ legacy bootstrap+contextual combined.

- [ ] **Step 1:** Push, `gh pr create` (body: spec link, mode-flag rollout, sentinel, calibration numbers; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), babysit to squash-merge (checks green + threads resolved).
- [ ] **Step 2:** Deploy: pull main; restart note (migration 0015 applies on next server start). Live default stays `legacy` — no env var set anywhere yet. Apply the live `~/.claude/CLAUDE.md` edit now (mirror the snippet rewrite).
- [ ] **Step 3:** Harness updates (`memuse-260721-run/`): `metric.py` + `analyze.py` gain the three numbers (headline excl. exploration, all-in, exploration conversion rate — conversion = exploration-tagged exposures whose memory has a later non-ignored rating in any session). Arms `D-legacy` / `D-deferred` added to `runner.py`: identical `spec` (live MAIN_REPO code, sync stop, contextual on) differing ONLY in `spec["env"]["BETTER_MEMORY_INJECT_MODE"]`. Refresh sandbox base. 24 sessions per arm.
- [ ] **Step 4:** Gate:

```bash
python analyze.py --arms D-legacy,D-deferred
python - <<'EOF'
import math
# fill from analyze headline columns:
u_l, n_l = USEFUL_LEGACY, EXPOSED_LEGACY_EXCL_EXPLORATION
u_d, n_d = USEFUL_DEFERRED, EXPOSED_DEFERRED_EXCL_EXPLORATION
p_l, p_d = u_l/n_l, u_d/n_d
p = (u_l+u_d)/(n_l+n_d)
z = (p_d-p_l)/math.sqrt(p*(1-p)*(1/n_l+1/n_d))
print(f"legacy={p_l:.4f} deferred={p_d:.4f} z={z:.3f}")
print("GATE:", "PASS" if z > -1.645 else "FAIL (one re-run allowed)")
EOF
```

Plus the precision condition from analyze's per-source table (deferred contextual useful% vs legacy bootstrap+contextual pooled).

- [ ] **Step 5:** PASS → flip live: add `"BETTER_MEMORY_INJECT_MODE": "deferred"` to the `env` of the better-memory server entry in `~/.claude.json` AND to the `env` block of `~/.claude/settings.json` (hooks read process env via the session env block — the runner arms prove this channel works). Restart note. FAIL twice → leave legacy live, report.
- [ ] **Step 6:** Ledger the outcome; schedule the legacy-path deletion PR after a stable week (note only).

---

## Self-review notes

- Spec §1-§4 all covered: flag+floor (T1), tagging+metric (T2, T10), breaker persistence gap discovered in spike (T3 — spec's latency promise required it), scorer+gate (T4), cadence+latch+matcher (T5), deferred bootstrap (T6), absorbed PR-C (T7), calibration (T8 — the spec's R1 mitigation), website guardrail (T9), rollout+gate+flip (T10).
- Two implementer-judgment points are contract-pinned rather than line-pinned (T6 render locals, T5 payload-harness style) with explicit read-first instructions — the files' internals shift too easily for line-level edits to survive.
- One in-plan probe (T4 Step 3a: sqlite-vec distance convention) deliberately left to implementation with an exact 3-line experiment — cheaper than pinning a possibly-wrong convention now.
- Migration number 0015 claimed here; PR-B's plan must renumber to 0016.
