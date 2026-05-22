# TF-IDF Embeddings Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `BETTER_MEMORY_EMBEDDINGS_BACKEND=ollama|tfidf` config switch with a hand-rolled, stdlib-only TF-IDF retriever as a no-Ollama, no-download alternative.

**Architecture:** Two backends behind a config switch. Ollama path unchanged — `OllamaEmbedder` continues to populate `observation_embeddings` vec0 rows and `hybrid_search` does the rest. TF-IDF path uses a new in-memory `TfidfRetriever` (refit per observe) and a new `tfidf_search` module that mirrors `hybrid_search` with FTS5 BM25 in SQL and TF-IDF cosine in Python, reusing `hybrid.py`'s private fusion/finalize helpers.

**Tech Stack:** Python 3.12+, stdlib only (re, math, collections), sqlite3, existing FTS5 + sqlite-vec infrastructure, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md`

---

## File Map

| Path | Status | Responsibility |
|---|---|---|
| `better_memory/config.py` | modify | Add `embeddings_backend` field + env var read |
| `better_memory/embeddings/tfidf.py` | NEW | Tokenizer + TfidfRetriever class |
| `better_memory/embeddings/__init__.py` | modify | Export TfidfRetriever |
| `better_memory/search/tfidf_search.py` | NEW | FTS5 + TF-IDF + RRF fusion (Python) |
| `better_memory/services/observation.py` | modify | Accept retriever; branch write + read paths |
| `better_memory/mcp/server.py` | modify | Build embedder OR retriever per backend |
| `tests/test_config.py` | modify | Add backend env var tests |
| `tests/embeddings/test_tfidf_unit.py` | NEW | Tokenizer + retriever unit tests |
| `tests/search/test_tfidf_search.py` | NEW | End-to-end fusion tests |
| `tests/services/test_observation_tfidf.py` | NEW | Service integration with retriever |
| `README.md` | modify | Env var table + prerequisites |
| `website/configuration.md` | modify | Env var row |
| `website/architecture.md` | modify | Backends section |

## Confidence Summary

| Task | Confidence | Lift applied |
|---|---|---|
| 1. Config switch | 95% | — |
| 2. Tokenizer function | 95% | — |
| 3. TfidfRetriever (in-memory math) | 92% | — |
| 4. TfidfRetriever DB integration | 92% (lifted from 88%) | Concrete SQL + fixture imports inline |
| 5. tfidf_search module | 92% (lifted from 85%) | Exact import line + helper signatures inline |
| 6. ObservationService.create branch | 92% (lifted from 87%) | Full method diff with post-commit add_doc |
| 7. ObservationService.retrieve branch | 90% | — |
| 8. ObservationService drill-down branch | 90% | — |
| 9. mcp/server.py wiring | 92% (lifted from 88%) | Concrete diff with cleanup guard |
| 10. Documentation | 95% | — |

All tasks ≥ 90%. No residual sub-90% items.

---

### Task 1: Config switch

**Files:**
- Modify: `better_memory/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_embeddings_backend_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", raising=False)
    cfg = get_config()
    assert cfg.embeddings_backend == "ollama"


def test_embeddings_backend_tfidf_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "tfidf")
    cfg = get_config()
    assert cfg.embeddings_backend == "tfidf"


def test_embeddings_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "nope")
    with pytest.raises(ValueError, match="BETTER_MEMORY_EMBEDDINGS_BACKEND"):
        get_config()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_config.py::test_embeddings_backend_defaults_to_ollama -v
```
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'embeddings_backend'`.

- [ ] **Step 3: Add the field and resolver to `config.py`**

Edit `better_memory/config.py`. Add near the top, after `_DEFAULT_EMBED_MODEL`:

```python
_DEFAULT_EMBEDDINGS_BACKEND = "ollama"
_VALID_EMBEDDINGS_BACKENDS = ("ollama", "tfidf")
```

Update the `Literal` import (top of file):

```python
from typing import Literal
```

Add field to `Config` dataclass (after `diag_logging: bool`):

```python
embeddings_backend: Literal["ollama", "tfidf"]
```

Add a resolver helper above `get_config`:

```python
def _resolve_embeddings_backend() -> Literal["ollama", "tfidf"]:
    raw = os.environ.get("BETTER_MEMORY_EMBEDDINGS_BACKEND", _DEFAULT_EMBEDDINGS_BACKEND)
    if raw not in _VALID_EMBEDDINGS_BACKENDS:
        raise ValueError(
            f"BETTER_MEMORY_EMBEDDINGS_BACKEND must be one of "
            f"{_VALID_EMBEDDINGS_BACKENDS}, got {raw!r}"
        )
    return raw  # type: ignore[return-value]
```

In `get_config()` body, set the field:

```python
return Config(
    ...
    diag_logging=_resolve_bool("BETTER_MEMORY_DIAG_LOGGING", default=False),
    embeddings_backend=_resolve_embeddings_backend(),
)
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_config.py -v -k embeddings_backend
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add better_memory/config.py tests/test_config.py
git commit -m "feat(config): add BETTER_MEMORY_EMBEDDINGS_BACKEND switch"
```

---

### Task 2: Tokenizer function

**Files:**
- Create: `better_memory/embeddings/tfidf.py`
- Create: `tests/embeddings/test_tfidf_unit.py`

- [ ] **Step 1: Write the failing tokenizer tests**

Create `tests/embeddings/test_tfidf_unit.py`:

```python
"""Unit tests for :mod:`better_memory.embeddings.tfidf`."""

from __future__ import annotations

import math

import pytest

from better_memory.embeddings.tfidf import tokenize


class TestTokenize:
    def test_lowercases_and_splits_on_non_alnum(self) -> None:
        result = tokenize("Hello, World! 123")
        words = [t for t in result if not t.startswith("#")]
        assert words == ["hello", "world", "123"]

    def test_keeps_snake_case_whole(self) -> None:
        words = [t for t in tokenize("session_bootstrap") if not t.startswith("#")]
        assert words == ["session_bootstrap"]

    def test_drops_tokens_shorter_than_two_chars(self) -> None:
        words = [t for t in tokenize("a bb ccc") if not t.startswith("#")]
        assert "a" not in words
        assert "bb" in words
        assert "ccc" in words

    def test_emits_char_4grams_prefixed_with_hash(self) -> None:
        result = tokenize("abcde")
        ngrams = [t for t in result if t.startswith("#")]
        assert "#abcd" in ngrams
        assert "#bcde" in ngrams

    def test_empty_string_returns_empty(self) -> None:
        assert tokenize("") == []

    def test_short_text_yields_no_ngrams(self) -> None:
        result = tokenize("ab")
        ngrams = [t for t in result if t.startswith("#")]
        assert ngrams == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/embeddings/test_tfidf_unit.py -v
```
Expected: collection error — `ModuleNotFoundError: better_memory.embeddings.tfidf`.

- [ ] **Step 3: Create the tokenizer**

Create `better_memory/embeddings/tfidf.py`:

```python
"""TF-IDF retriever for the no-Ollama embeddings backend.

Pure stdlib. State is in-memory only; rebuilt on MCP startup via
:meth:`TfidfRetriever.fit_from_db` and on every write via
:meth:`TfidfRetriever.add_doc`.

Tokenization combines word tokens (lowercased ASCII alphanumeric + underscore,
length >= 2) with character 4-grams. The 4-grams are prefixed with ``"#"``
to keep them in a distinct namespace from word tokens — ``"#"`` is not a
valid word-token char so collisions are impossible.

Vectors are sparse ``dict[token, float]`` with TF*IDF weights, L2-normalised
so cosine similarity reduces to dot product.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable

_WORD_RE = re.compile(r"[a-z0-9_]+")
_NGRAM_N = 4


def tokenize(text: str) -> list[str]:
    """Return word tokens plus character 4-gram tokens for ``text``.

    Word tokens are lowercased, ASCII alphanumeric + underscore, length >= 2.
    Character n-grams are 4-grams over the lowercased text (spaces and
    punctuation included), prefixed with ``"#"`` to namespace them apart
    from word tokens.
    """
    lower = text.lower()
    words = [w for w in _WORD_RE.findall(lower) if len(w) >= 2]
    ngrams = [f"#{lower[i : i + _NGRAM_N]}" for i in range(len(lower) - _NGRAM_N + 1)]
    return words + ngrams
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/embeddings/test_tfidf_unit.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add better_memory/embeddings/tfidf.py tests/embeddings/test_tfidf_unit.py
git commit -m "feat(embeddings): add tokenizer for TF-IDF backend"
```

---

### Task 3: TfidfRetriever — in-memory state

**Files:**
- Modify: `better_memory/embeddings/tfidf.py`
- Modify: `tests/embeddings/test_tfidf_unit.py`

- [ ] **Step 1: Write failing tests for fit / vectorize / score**

Append to `tests/embeddings/test_tfidf_unit.py`:

```python
from better_memory.embeddings.tfidf import TfidfRetriever


class TestTfidfRetrieverInMemory:
    def test_fit_populates_vocab_and_doc_vectors(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world", "d2": "world peace"})
        assert "hello" in r._vocab
        assert "world" in r._vocab
        assert set(r._doc_vectors.keys()) == {"d1", "d2"}

    def test_idf_higher_for_rarer_tokens(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "rare", "d2": "common", "d3": "common"})
        # "rare" appears in 1/3 docs; "common" in 2/3 — rare should have higher IDF
        assert r._idf["rare"] > r._idf["common"]

    def test_vectors_are_l2_normalised(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world", "d2": "different stuff"})
        for vec in r._doc_vectors.values():
            norm_sq = sum(v * v for v in vec.values())
            assert math.isclose(norm_sq, 1.0, abs_tol=1e-9)

    def test_score_returns_higher_for_similar_query(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs(
            {
                "match": "the quick brown fox jumps",
                "nope": "completely unrelated content here",
            }
        )
        scored = dict(r.score("quick brown fox", ["match", "nope"]))
        assert scored["match"] > scored["nope"]

    def test_score_for_oov_query_returns_zero(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world"})
        scored = dict(r.score("xenoglossolalia", ["d1"]))
        assert scored["d1"] == 0.0

    def test_score_for_empty_corpus_returns_empty(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        assert r.score("anything", []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/embeddings/test_tfidf_unit.py::TestTfidfRetrieverInMemory -v
```
Expected: FAIL with `ImportError: cannot import name 'TfidfRetriever'`.

- [ ] **Step 3: Add the retriever class to `tfidf.py`**

Append to `better_memory/embeddings/tfidf.py`:

```python
class TfidfRetriever:
    """In-memory TF-IDF retriever.

    State is rebuilt on every ``add_doc`` / ``remove_doc`` and on
    ``fit_from_db``. At ~500 documents, fit cost is ~50 ms.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._vocab: set[str] = set()
        self._idf: dict[str, float] = {}
        self._doc_vectors: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------ public
    def vectorize(self, text: str) -> dict[str, float]:
        """Return a sparse, L2-normalised TF*IDF vector for ``text``.

        Tokens not in the fitted vocabulary are dropped silently.
        """
        tokens = tokenize(text)
        if not tokens:
            return {}
        tf = Counter(t for t in tokens if t in self._vocab)
        if not tf:
            return {}
        weighted = {t: count * self._idf[t] for t, count in tf.items()}
        return _l2_normalise(weighted)

    def score(
        self, query: str, candidate_ids: list[str]
    ) -> list[tuple[str, float]]:
        """Score ``candidate_ids`` against ``query`` by cosine similarity.

        Returns ``[(id, score), ...]`` sorted by score descending. Unknown
        ids are skipped. Empty query or empty candidates returns ``[]``.
        """
        if not candidate_ids:
            return []
        qv = self.vectorize(query)
        scored: list[tuple[str, float]] = []
        for doc_id in candidate_ids:
            dv = self._doc_vectors.get(doc_id)
            if dv is None:
                continue
            scored.append((doc_id, _cosine_normalised(qv, dv)))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored

    # ----------------------------------------------------------- internals
    def _fit_docs(self, docs: dict[str, str]) -> None:
        """Rebuild vocab, IDF, and doc_vectors from ``docs``."""
        tokenised = {doc_id: tokenize(text) for doc_id, text in docs.items()}
        n_docs = len(tokenised)
        df: Counter[str] = Counter()
        for tokens in tokenised.values():
            df.update(set(tokens))
        # Smoothed IDF: log((N + 1) / (df + 1)) + 1
        self._idf = {
            t: math.log((n_docs + 1) / (count + 1)) + 1.0
            for t, count in df.items()
        }
        self._vocab = set(self._idf.keys())
        self._doc_vectors = {}
        for doc_id, tokens in tokenised.items():
            if not tokens:
                self._doc_vectors[doc_id] = {}
                continue
            tf = Counter(tokens)
            weighted = {t: count * self._idf[t] for t, count in tf.items() if t in self._idf}
            self._doc_vectors[doc_id] = _l2_normalise(weighted)


def _l2_normalise(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0.0:
        return {}
    return {t: v / norm for t, v in vec.items()}


def _cosine_normalised(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine sim assuming both inputs are already L2-normalised => dot product."""
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * larger.get(t, 0.0) for t, v in smaller.items())
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/embeddings/test_tfidf_unit.py -v
```
Expected: 12 passed (6 tokenizer + 6 retriever).

- [ ] **Step 5: Commit**

```
git add better_memory/embeddings/tfidf.py tests/embeddings/test_tfidf_unit.py
git commit -m "feat(embeddings): TfidfRetriever in-memory state and scoring"
```

---

### Task 4: TfidfRetriever DB integration (`fit_from_db`, `add_doc`, `remove_doc`)

**Files:**
- Modify: `better_memory/embeddings/tfidf.py`
- Modify: `tests/embeddings/test_tfidf_unit.py`

Confidence lift: SQL is concrete (uses the same `observations` table the existing service queries), sqlite fixture is imported from `tests/conftest.py` (`tmp_memory_db`), migrations are applied so the schema matches production.

- [ ] **Step 1: Write failing tests for DB integration**

Append to `tests/embeddings/test_tfidf_unit.py`:

```python
import sqlite3
from pathlib import Path

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _insert_obs(conn: sqlite3.Connection, obs_id: str, content: str) -> None:
    # Episode required by FK; create a background episode.
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


class TestTfidfRetrieverDB:
    def test_fit_from_db_loads_existing_observations(self, conn: sqlite3.Connection) -> None:
        _insert_obs(conn, "o1", "first observation content")
        _insert_obs(conn, "o2", "second observation content")

        r = TfidfRetriever(conn)
        r.fit_from_db()

        assert set(r._doc_vectors.keys()) == {"o1", "o2"}

    def test_fit_from_db_empty_corpus_is_safe(self, conn: sqlite3.Connection) -> None:
        r = TfidfRetriever(conn)
        r.fit_from_db()
        assert r._doc_vectors == {}
        assert r._vocab == set()

    def test_add_doc_refits_and_includes_new(self, conn: sqlite3.Connection) -> None:
        _insert_obs(conn, "o1", "alpha bravo")
        r = TfidfRetriever(conn)
        r.fit_from_db()

        _insert_obs(conn, "o2", "charlie delta")
        r.add_doc("o2", "charlie delta")

        assert "o2" in r._doc_vectors
        assert "charlie" in r._vocab

    def test_remove_doc_refits_without_removed(self, conn: sqlite3.Connection) -> None:
        _insert_obs(conn, "o1", "stay")
        _insert_obs(conn, "o2", "leave")
        r = TfidfRetriever(conn)
        r.fit_from_db()

        conn.execute("DELETE FROM observations WHERE id = 'o2'")
        conn.commit()
        r.remove_doc("o2")

        assert "o2" not in r._doc_vectors
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/embeddings/test_tfidf_unit.py::TestTfidfRetrieverDB -v
```
Expected: FAIL with `AttributeError: 'TfidfRetriever' object has no attribute 'fit_from_db'`.

- [ ] **Step 3: Add DB methods to TfidfRetriever**

Insert into `better_memory/embeddings/tfidf.py` after the `vectorize` method (still inside `TfidfRetriever`):

```python
    def fit_from_db(self) -> None:
        """Reload corpus from the ``observations`` table and refit."""
        rows = self._conn.execute(
            "SELECT id, content FROM observations WHERE status != 'deleted'"
        ).fetchall()
        docs = {row[0]: row[1] for row in rows}
        self._fit_docs(docs)

    def add_doc(self, doc_id: str, text: str) -> None:
        """Add a doc to the corpus and refit.

        Re-fetches the full corpus so the IDF and vectors reflect the new
        document plus any other observations that exist in the DB.
        """
        rows = self._conn.execute(
            "SELECT id, content FROM observations WHERE status != 'deleted'"
        ).fetchall()
        docs = {row[0]: row[1] for row in rows}
        # Ensure the newly-supplied doc is present even if not yet visible
        # to a concurrent reader of the connection.
        docs[doc_id] = text
        self._fit_docs(docs)

    def remove_doc(self, doc_id: str) -> None:
        """Drop a doc from the corpus and refit."""
        rows = self._conn.execute(
            "SELECT id, content FROM observations WHERE status != 'deleted' AND id != ?",
            (doc_id,),
        ).fetchall()
        docs = {row[0]: row[1] for row in rows}
        self._fit_docs(docs)
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/embeddings/test_tfidf_unit.py -v
```
Expected: 16 passed (4 new DB tests + 12 prior).

- [ ] **Step 5: Commit**

```
git add better_memory/embeddings/tfidf.py tests/embeddings/test_tfidf_unit.py
git commit -m "feat(embeddings): TfidfRetriever DB sync (fit_from_db/add/remove)"
```

---

### Task 5: `tfidf_search` module — FTS5 BM25 + TF-IDF + RRF fusion

**Files:**
- Create: `better_memory/search/tfidf_search.py`
- Create: `tests/search/test_tfidf_search.py`

Confidence lift: the `hybrid.py` private helpers we reuse are concrete — `_fts_candidates`, `_build_where`, `_add_rrf_ranks`, `_fetch_rows`, `_finalize`, `_Candidate` — all defined in `better_memory/search/hybrid.py` lines 62–367. Same-package private imports are conventional; we are not relying on names that need a refactor. If a later phase wants to make them public, that's a follow-up.

- [ ] **Step 1: Write the failing fusion test**

Create `tests/search/test_tfidf_search.py`:

```python
"""End-to-end tests for :mod:`better_memory.search.tfidf_search`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.search.hybrid import SearchFilters
from better_memory.search.tfidf_search import tfidf_search


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _seed(conn: sqlite3.Connection, *docs: tuple[str, str, str]) -> None:
    """Insert (id, content, outcome) rows + episodes."""
    for obs_id, content, outcome in docs:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) "
            "VALUES (?, 'p', '2026-01-01T00:00:00+00:00', NULL)",
            (f"ep-{obs_id}",),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, "
            "status, outcome, created_at, status_changed_at) "
            "VALUES (?, ?, 'p', ?, 'active', ?, "
            "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
            (obs_id, content, f"ep-{obs_id}", outcome),
        )
    conn.commit()


def _clock() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


def test_tfidf_search_returns_relevant_doc_first(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        ("hit", "pytest junit-xml output capture on windows", "success"),
        ("miss", "completely unrelated config setting docs", "success"),
    )
    r = TfidfRetriever(conn)
    r.fit_from_db()

    filters = SearchFilters(
        project="p", status="active", window_days=None, outcome="success"
    )
    results = tfidf_search(
        conn, r, query_text="pytest windows", filters=filters,
        limit=2, clock=_clock,
    )

    assert [hit.id for hit in results][0] == "hit"


def test_tfidf_search_empty_query_returns_empty(conn: sqlite3.Connection) -> None:
    _seed(conn, ("o1", "anything", "neutral"))
    r = TfidfRetriever(conn)
    r.fit_from_db()
    filters = SearchFilters(project="p", status="active", window_days=None)
    assert tfidf_search(conn, r, query_text=None, filters=filters,
                        limit=10, clock=_clock) == []


def test_tfidf_search_respects_outcome_filter(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        ("s", "alpha beta gamma", "success"),
        ("f", "alpha beta gamma", "failure"),
    )
    r = TfidfRetriever(conn)
    r.fit_from_db()

    filters = SearchFilters(
        project="p", status="active", window_days=None, outcome="success"
    )
    results = tfidf_search(
        conn, r, query_text="alpha", filters=filters,
        limit=10, clock=_clock,
    )
    assert {hit.id for hit in results} == {"s"}
```

- [ ] **Step 2: Run the tests to verify failure**

```
pytest tests/search/test_tfidf_search.py -v
```
Expected: collection error — `ModuleNotFoundError: better_memory.search.tfidf_search`.

- [ ] **Step 3: Create `tfidf_search` module**

Create `better_memory/search/tfidf_search.py`:

```python
"""TF-IDF + FTS5 BM25 hybrid search for the TF-IDF embeddings backend.

Mirrors :func:`better_memory.search.hybrid.hybrid_search` but uses an
in-memory :class:`TfidfRetriever` for the vector half instead of
sqlite-vec. Reuses private helpers from ``hybrid`` for FTS5 candidates,
RRF fusion, row hydration, and the reinforcement+recency finalisation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.search.hybrid import (
    SearchFilters,
    SearchResult,
    _add_rrf_ranks,
    _build_where,
    _Candidate,
    _fetch_rows,
    _finalize,
    _fts_candidates,
)
from better_memory.search.query import sanitize_fts5_query  # used by callers; harmless import here


_DEFAULT_FILTERS = SearchFilters()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def tfidf_search(
    conn: sqlite3.Connection,
    retriever: TfidfRetriever,
    *,
    query_text: str | None = None,
    filters: SearchFilters = _DEFAULT_FILTERS,
    limit: int = 10,
    candidate_k: int = 50,
    rrf_k: int = 60,
    reinforcement_alpha: float = 0.1,
    recency_half_life_days: float = 14.0,
    clock: Callable[[], datetime] | None = None,
) -> list[SearchResult]:
    """Run FTS5 BM25 + TF-IDF cosine, fuse via RRF, return top ``limit``."""
    if query_text is None or not query_text.strip():
        return []

    now = (clock or _default_clock)()
    where_sql, where_params = _build_where(filters, now=now)

    # --- FTS5 candidates (SQL) -------------------------------------------
    fts_ids = _fts_candidates(
        conn,
        query_text=query_text,
        where_sql=where_sql,
        where_params=where_params,
        candidate_k=candidate_k,
    )

    # --- TF-IDF candidates (Python over filter-matched ids) --------------
    sql = "SELECT o.id AS id FROM observations o"
    params: list[Any] = []
    if where_sql:
        sql += " WHERE " + where_sql
        params.extend(where_params)
    filter_ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
    tfidf_scored = retriever.score(query_text, filter_ids)
    tfidf_ids = [doc_id for doc_id, score in tfidf_scored[:candidate_k] if score > 0.0]

    if not fts_ids and not tfidf_ids:
        return []

    # --- RRF fuse --------------------------------------------------------
    candidates: dict[str, _Candidate] = {}
    _add_rrf_ranks(candidates, fts_ids, source="fts", rrf_k=rrf_k)
    _add_rrf_ranks(candidates, tfidf_ids, source="vec", rrf_k=rrf_k)
    if not candidates:
        return []

    # --- Hydrate + finalise ----------------------------------------------
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

- [ ] **Step 4: Run the tests to verify pass**

```
pytest tests/search/test_tfidf_search.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add better_memory/search/tfidf_search.py tests/search/test_tfidf_search.py
git commit -m "feat(search): tfidf_search FTS5+TFIDF+RRF fusion module"
```

---

### Task 6: `ObservationService.create` — backend branching

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `better_memory/embeddings/__init__.py`
- Create: `tests/services/test_observation_tfidf.py`

Confidence lift: full method diff below shows the exact replacement, including the post-commit `add_doc` call. The retriever is added as a *keyword-only* parameter with default `None` so all existing call sites (which pass only the positional embedder) keep working.

- [ ] **Step 1: Export TfidfRetriever**

Edit `better_memory/embeddings/__init__.py`:

```python
"""Embedding clients for better-memory."""

from better_memory.embeddings.ollama import EmbeddingError, OllamaEmbedder
from better_memory.embeddings.tfidf import TfidfRetriever

__all__ = ["OllamaEmbedder", "EmbeddingError", "TfidfRetriever"]
```

- [ ] **Step 2: Write the failing service test**

Create `tests/services/test_observation_tfidf.py`:

```python
"""Tests for ObservationService with the TF-IDF retriever backend."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def service(conn: sqlite3.Connection, fixed_clock) -> ObservationService:
    retriever = TfidfRetriever(conn)
    retriever.fit_from_db()
    episodes = EpisodeService(conn, clock=fixed_clock)
    return ObservationService(
        conn,
        embedder=None,
        retriever=retriever,
        clock=fixed_clock,
        project_resolver=lambda: "p",
        scope_resolver=lambda: None,
        session_id="sess",
        episodes=episodes,
    )


async def test_create_skips_vec0_insert_in_tfidf_mode(
    service: ObservationService, conn: sqlite3.Connection
) -> None:
    obs_id = await service.create(content="hello tfidf world", outcome="success")

    # Observation row exists.
    row = conn.execute(
        "SELECT id FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row is not None

    # No vec0 row written.
    vec_count = conn.execute(
        "SELECT COUNT(*) FROM observation_embeddings WHERE observation_id = ?",
        (obs_id,),
    ).fetchone()[0]
    assert vec_count == 0


async def test_create_indexes_into_retriever(
    service: ObservationService,
) -> None:
    obs_id = await service.create(content="unique-marker-xyz", outcome="neutral")
    assert obs_id in service._retriever._doc_vectors  # type: ignore[union-attr]


async def test_create_requires_exactly_one_of_embedder_retriever(
    conn: sqlite3.Connection, fixed_clock
) -> None:
    episodes = EpisodeService(conn, clock=fixed_clock)
    with pytest.raises(ValueError, match="exactly one of embedder/retriever"):
        ObservationService(
            conn, embedder=None, retriever=None,
            clock=fixed_clock, episodes=episodes,
        )
```

- [ ] **Step 3: Run the tests to verify failure**

```
pytest tests/services/test_observation_tfidf.py -v
```
Expected: errors — `ObservationService.__init__()` does not accept `retriever`.

- [ ] **Step 4: Update `ObservationService.__init__` to accept retriever**

Edit `better_memory/services/observation.py`. Update the constructor signature and store both:

```python
def __init__(
    self,
    conn: sqlite3.Connection,
    embedder: Any = None,
    *,
    retriever: Any = None,
    clock: Callable[[], datetime] | None = None,
    project_resolver: Callable[[], str] | None = None,
    scope_resolver: Callable[[], str | None] | None = None,
    session_id: str | None = None,
    audit_log_retrieved: bool | None = None,
    episodes: EpisodeService | None = None,
) -> None:
    if (embedder is None) == (retriever is None):
        raise ValueError(
            "exactly one of embedder/retriever must be supplied (got both or neither)"
        )
    self._conn = conn
    self._embedder = embedder
    self._retriever = retriever
    # ... rest of init unchanged
```

- [ ] **Step 5: Branch the embed/index step in `create()`**

Edit the body of `create()`. Replace the existing `_diag.step(fn, "about_to_embed", ...)` through `_diag.step(fn, "vec_serialized", ...)` block (around lines 209–217) and the `_diag.step(fn, "insert_embedding_row")` + `INSERT INTO observation_embeddings` block (around lines 252–257) with:

Block 1 (replaces the embed compute before SAVEPOINT, around line 209–217):

```python
            # Embed-or-defer step: with an embedder, compute the vec0 blob
            # before opening the SAVEPOINT so a slow Ollama doesn't hold it
            # open. With a retriever, defer the index step until AFTER commit
            # so a refit failure can't kill a durable write.
            vec_blob: bytes | None = None
            if self._embedder is not None:
                _diag.step(fn, "about_to_embed", model=getattr(self._embedder, "_model", "?"))
                vector = await self._embedder.embed(content)
                _diag.step(fn, "embed_returned", dim=len(vector))
                vec_blob = sqlite_vec.serialize_float32(vector)
                _diag.step(fn, "vec_serialized", bytes=len(vec_blob))
```

Block 2 (replaces the `INSERT INTO observation_embeddings` block around lines 252–257):

```python
                if vec_blob is not None:
                    _diag.step(fn, "insert_embedding_row")
                    conn.execute(
                        "INSERT INTO observation_embeddings (observation_id, embedding) "
                        "VALUES (?, ?)",
                        (obs_id, vec_blob),
                    )
```

Block 3 (immediately after `conn.commit()` near line 282, BEFORE the `return obs_id`):

```python
            # Post-commit indexing for the TF-IDF backend. If add_doc raises,
            # the observation is still durable; the next MCP restart will
            # rebuild the in-memory state via fit_from_db.
            if self._retriever is not None:
                try:
                    self._retriever.add_doc(obs_id, content)
                except Exception:  # noqa: BLE001 — best-effort post-commit index
                    _diag.step(fn, "retriever_add_doc_failed")
```

- [ ] **Step 6: Run all tests to verify pass and no regressions**

```
pytest tests/services/test_observation_tfidf.py tests/services/test_observation.py -v
```
Expected: 3 new tests pass + existing test_observation.py suite still passes.

- [ ] **Step 7: Commit**

```
git add better_memory/services/observation.py better_memory/embeddings/__init__.py tests/services/test_observation_tfidf.py
git commit -m "feat(observation): branch create() on embedder vs tfidf retriever"
```

---

### Task 7: `ObservationService.retrieve` — backend branching

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `tests/services/test_observation_tfidf.py`

- [ ] **Step 1: Write the failing retrieve test**

Append to `tests/services/test_observation_tfidf.py`:

```python
async def test_retrieve_returns_bucketed_results_in_tfidf_mode(
    service: ObservationService,
) -> None:
    await service.create(content="windows pytest junit-xml output", outcome="success")
    await service.create(content="merge conflict resolution strategy", outcome="failure")
    await service.create(content="unrelated trivia notes", outcome="neutral")

    buckets = await service.retrieve(
        query="windows pytest",
        do_limit=5, dont_limit=5, neutral_limit=5,
        window_days=None,
    )
    assert len(buckets.do) >= 1
    assert buckets.do[0].content == "windows pytest junit-xml output"
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/services/test_observation_tfidf.py::test_retrieve_returns_bucketed_results_in_tfidf_mode -v
```
Expected: FAIL — either the call routes to `hybrid_search` (which has no vec0 rows for these obs and falls back to FTS5 only, so may still pass by accident — but the assertion on `buckets.do[0].content` may not hold). Or AttributeError if `self._embedder.embed` is called with `None`.

- [ ] **Step 3: Branch the `retrieve()` method**

Edit `better_memory/services/observation.py`. Inside `retrieve()`, find the block:

```python
        query_vector: list[float] | None = None
        if query is not None and query.strip():
            query_vector = await self._embedder.embed(query)
```

Replace with:

```python
        query_vector: list[float] | None = None
        if query is not None and query.strip() and self._embedder is not None:
            query_vector = await self._embedder.embed(query)
```

Then update the inner `_run()` closure. Find:

```python
        def _run(outcome: Outcome, limit: int) -> list[SearchResult]:
            filters = SearchFilters(outcome=outcome, **base_kwargs)
            return hybrid_search(
                self._conn,
                query_text=fts_query_text,
                query_vector=query_vector,
                filters=filters,
                limit=limit,
                candidate_k=candidate_k,
                reinforcement_alpha=reinforcement_alpha,
                clock=self._clock,
            )
```

Replace with:

```python
        def _run(outcome: Outcome, limit: int) -> list[SearchResult]:
            filters = SearchFilters(outcome=outcome, **base_kwargs)
            if self._retriever is not None:
                from better_memory.search.tfidf_search import tfidf_search
                return tfidf_search(
                    self._conn,
                    self._retriever,
                    query_text=fts_query_text,
                    filters=filters,
                    limit=limit,
                    candidate_k=candidate_k,
                    reinforcement_alpha=reinforcement_alpha,
                    clock=self._clock,
                )
            return hybrid_search(
                self._conn,
                query_text=fts_query_text,
                query_vector=query_vector,
                filters=filters,
                limit=limit,
                candidate_k=candidate_k,
                reinforcement_alpha=reinforcement_alpha,
                clock=self._clock,
            )
```

- [ ] **Step 4: Run to verify pass and regressions are clean**

```
pytest tests/services/test_observation_tfidf.py tests/services/test_observation.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add better_memory/services/observation.py tests/services/test_observation_tfidf.py
git commit -m "feat(observation): branch retrieve() on backend (tfidf_search vs hybrid_search)"
```

---

### Task 8: Drill-down (`_list_observations_via_hybrid_search`) — backend branching

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `tests/services/test_observation_tfidf.py`

- [ ] **Step 1: Write the failing drill-down test**

Append to `tests/services/test_observation_tfidf.py`:

```python
async def test_list_observations_query_mode_in_tfidf(
    service: ObservationService,
) -> None:
    await service.create(content="alpha bravo charlie", outcome="success")
    await service.create(content="completely separate topic", outcome="neutral")

    results = await service.list_observations(query="alpha", limit=5)
    contents = {r["content"] for r in results}
    assert "alpha bravo charlie" in contents
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/services/test_observation_tfidf.py::test_list_observations_query_mode_in_tfidf -v
```
Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'embed'`.

- [ ] **Step 3: Branch `_list_observations_via_hybrid_search`**

Edit `better_memory/services/observation.py`. Find `_list_observations_via_hybrid_search`. Replace the body's embed + hybrid_search call:

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
        if self._retriever is not None:
            from better_memory.search.tfidf_search import tfidf_search
            results = tfidf_search(
                self._conn,
                self._retriever,
                query_text=fts_query_text,
                filters=filters,
                limit=limit,
                clock=self._clock,
            )
        else:
            vector = await self._embedder.embed(query)
            results = hybrid_search(
                self._conn,
                query_text=fts_query_text,
                query_vector=vector,
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
                "final_score": r.final_score,
            }
            for r in results
        ]
```

Note: copy the **exact** trailing `return [ {...} for r in results ]` shape from the file you're editing; the snippet above mirrors the current code in `services/observation.py` lines 598–610 but verify the dict keys match.

- [ ] **Step 4: Run to verify pass**

```
pytest tests/services/test_observation_tfidf.py -v
```
Expected: all 5 tfidf tests pass.

- [ ] **Step 5: Run the full service suite**

```
pytest tests/services -v
```
Expected: no regressions.

- [ ] **Step 6: Commit**

```
git add better_memory/services/observation.py tests/services/test_observation_tfidf.py
git commit -m "feat(observation): branch drill-down list_observations on backend"
```

---

### Task 9: MCP server wiring

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_server_integration.py` (or create a new focused test if the existing file is dense)

Confidence lift: concrete diff for the build_server changes including the cleanup guard.

- [ ] **Step 1: Write the failing server-boot test**

Add to `tests/mcp/test_server_integration.py` (or create `tests/mcp/test_server_tfidf.py`):

```python
async def test_server_builds_in_tfidf_mode_without_ollama(
    tmp_memory_db: Path, tmp_knowledge_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server should start with backend=tfidf even when Ollama is unreachable."""
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "tfidf")
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
    monkeypatch.setenv("OLLAMA_HOST", "http://does-not-exist.invalid:1")

    from better_memory.mcp.server import build_server
    server, cleanup = await build_server()
    try:
        # Server is constructed; no Ollama probe should have been attempted.
        assert server is not None
    finally:
        await cleanup()
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/mcp/test_server_tfidf.py -v
```
Expected: FAIL — server tries to construct an `OllamaEmbedder` and the cleanup may attempt `await embedder.aclose()` on `None`, or `_probe_ollama` may produce a stderr warning. Detect the actual failure mode and proceed.

- [ ] **Step 3: Branch embedder/retriever construction in `build_server`**

Edit `better_memory/mcp/server.py`. Find the block starting around line 934:

```python
    # One embedder per server. ...
    embedder = OllamaEmbedder()

    # Cheap reachability probe ...
    _probe_ollama(config.ollama_host)
```

Replace with:

```python
    # Build embedder OR retriever depending on the configured backend.
    # Exactly one is non-None and is passed to ObservationService.
    embedder = None
    retriever = None
    if config.embeddings_backend == "ollama":
        embedder = OllamaEmbedder()
        # Cheap reachability probe — warns on stderr if Ollama is down but
        # does not block startup; memory.observe / memory.retrieve will
        # surface a clean EmbeddingError on first call if it stays down.
        _probe_ollama(config.ollama_host)
    else:
        # tfidf backend: load corpus into the retriever's in-memory state.
        # No Ollama probe, no HTTP client.
        retriever = TfidfRetriever(memory_conn)
        retriever.fit_from_db()
```

Update the `ObservationService` construction around line 957:

```python
    observations = ObservationService(
        memory_conn, embedder=embedder, retriever=retriever, episodes=episodes
    )
```

Add the `TfidfRetriever` import at the top of `server.py` (near the existing `OllamaEmbedder` import on line 61):

```python
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.embeddings.tfidf import TfidfRetriever
```

Find the cleanup function around line 1518 with `await embedder.aclose()` and guard it:

```python
            if embedder is not None:
                await embedder.aclose()
```

- [ ] **Step 4: Run to verify pass**

```
pytest tests/mcp/test_server_tfidf.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the full MCP suite for regressions**

```
pytest tests/mcp -v
```
Expected: no regressions.

- [ ] **Step 6: Commit**

```
git add better_memory/mcp/server.py tests/mcp/test_server_tfidf.py
git commit -m "feat(mcp): wire TF-IDF backend in build_server"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `website/configuration.md`
- Modify: `website/architecture.md`

- [ ] **Step 1: Update README env-var table**

In `README.md`, find the env-var / prerequisites section. Add a row:

```
| `BETTER_MEMORY_EMBEDDINGS_BACKEND` | `ollama` (default) — local Ollama at `OLLAMA_HOST`; `tfidf` — in-memory TF-IDF, stdlib only, no model downloads. |
```

In the prerequisites list, note: "Ollama only required when `BETTER_MEMORY_EMBEDDINGS_BACKEND=ollama` (the default)."

- [ ] **Step 2: Update `website/configuration.md`**

Add a row to the env-var table matching the README format. Use the same one-line description.

- [ ] **Step 3: Update `website/architecture.md`**

After the existing "Storage" / "Synthesis" sections, add a new "Embeddings backends" subsection (~3 short paragraphs):

```markdown
### Embeddings backends

better-memory supports two backends behind the
`BETTER_MEMORY_EMBEDDINGS_BACKEND` env var.

**`ollama` (default).** Observation text is embedded by a local Ollama
server (model: `nomic-embed-text`, 768-dim). Vectors land in the
`observation_embeddings` virtual table; retrieval fuses FTS5 BM25 with
sqlite-vec kNN via Reciprocal Rank Fusion. Same quality as previous
versions; requires Ollama running on `OLLAMA_HOST`.

**`tfidf`.** A hand-rolled TF-IDF retriever (`TfidfRetriever`) lives
in-memory and refits after every observation write. The vector half of
hybrid search runs in pure Python instead of sqlite-vec. No model
downloads, no external service. Lower semantic quality than Ollama
(lexical match plus character 4-grams) but works in environments that
block Ollama or model file downloads.

Switching backends does not migrate existing data. Observations written
under one backend remain searchable via FTS5 BM25 under the other; only
the vector half degrades for cross-backend rows. A future
`memory.reindex` MCP tool can backfill if the half-state becomes a
problem.
```

- [ ] **Step 4: Sanity-check the docs build (if mkdocs is wired)**

```
mkdocs build --strict --verbose
```
Expected: clean build. If `--strict` is not the project default, run without it.

- [ ] **Step 5: Commit**

```
git add README.md website/configuration.md website/architecture.md
git commit -m "docs: document BETTER_MEMORY_EMBEDDINGS_BACKEND switch"
```

---

## Self-Review

**Spec coverage:**
- Goal (single config switch, ollama+tfidf) → Task 1 ✓
- Tokenizer (words + char 4-grams) → Task 2 ✓
- TfidfRetriever in-memory math → Task 3 ✓
- TfidfRetriever DB integration (fit/add/remove) → Task 4 ✓
- tfidf_search module (FTS5 + cosine + RRF + finalize) → Task 5 ✓
- ObservationService.create branch + post-commit add_doc → Task 6 ✓
- ObservationService.retrieve branch → Task 7 ✓
- Drill-down `list_observations` branch → Task 8 ✓
- MCP server wiring, conditional probe, cleanup guard → Task 9 ✓
- Docs (README, configuration.md, architecture.md) → Task 10 ✓
- Error handling: ValueError on unknown backend (Task 1), empty corpus safe (Task 4), OOV query returns 0 (Task 3), post-commit refit failure logged not raised (Task 6) ✓

**Placeholder scan:**
- No "TBD", "TODO", "implement later" strings in any task.
- All code blocks are complete (no `# ... rest unchanged` shortcuts in the operative diffs; small "# rest of init unchanged" appears in Task 6 Step 4 but the constructor's other lines are kept verbatim — the only changes are the new params, the validation, and storing `self._retriever`).

**Type consistency:**
- `TfidfRetriever` constructor signature `(conn: sqlite3.Connection)` consistent in Tasks 3, 4, 6, 9.
- `tfidf_search(conn, retriever, *, query_text, filters, limit, candidate_k, rrf_k, reinforcement_alpha, recency_half_life_days, clock)` matches between Task 5 (definition) and Tasks 7, 8 (callers).
- `ObservationService(conn, embedder=None, *, retriever=None, ...)` consistent between Task 6 (definition) and Task 9 (caller in server.py).
- `embeddings_backend: Literal["ollama", "tfidf"]` on Config consistent between Task 1 (definition) and Task 9 (read).

No gaps or inconsistencies found.
