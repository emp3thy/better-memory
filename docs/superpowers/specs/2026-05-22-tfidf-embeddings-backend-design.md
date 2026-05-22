# TF-IDF Embeddings Backend — Design

**Status:** Approved (design phase)
**Date:** 2026-05-22
**Author:** gethin (with Claude)

## Goal

Add a single config switch `BETTER_MEMORY_EMBEDDINGS_BACKEND` with two values:

- `ollama` (default) — current behaviour, unchanged
- `tfidf` — hand-rolled TF-IDF retriever, in-memory, stdlib-only, no model downloads

The switch determines how observation text is "vectorised" for semantic retrieval. This unblocks running better-memory in environments where Ollama cannot be installed and language-model files cannot be downloaded (e.g. corporate / locked-down workstations).

**Default is `ollama` so existing users see zero behaviour change.**

## Non-Goals

- Replacing Ollama embeddings for the `ollama` backend — that path stays as-is.
- Cloud-API embedder (OpenAI / Cohere / Voyage). Out of scope for this design; could be added behind the same switch later.
- Reflections-side embedding indexing. The `reflection_embeddings` vec0 table is **vestigial** — created in migration `0002_episodic.sql` but never populated by application code (verified by grep). Out of scope.
- Backfilling existing observation_embeddings rows when switching backends.

## Constraints

- Single user (the project author). 467 observations today, ~5 K is the upper bound we plan for.
- No external dependencies for the tfidf path — stdlib only.
- No model downloads, no HTTP egress.
- Windows + Linux + macOS host compatibility.

## Architecture

```
                  BETTER_MEMORY_EMBEDDINGS_BACKEND
                                 |
              +------------------+------------------+
              |                                     |
           "ollama"                              "tfidf"
              |                                     |
   OllamaEmbedder() + probe               TfidfRetriever().fit_from_db()
              |                                     |
              +---> ObservationService <------------+
                            |
                            +---> memory.observe (write path)
                            |          - INSERT observations row
                            |          - ollama: embed + INSERT vec0 row
                            |          - tfidf: retriever.add_doc + in-mem refit
                            |
                            +---> memory.retrieve (read path)
                                       - ollama: hybrid_search (SQL FTS5 + vec0)
                                       - tfidf: tfidf_search (SQL FTS5 + Python cosine)
```

## Decisions

| Question | Decision | Reasoning |
|---|---|---|
| TF-IDF state storage | In-memory only, regenerated on MCP startup | 467 docs takes ~50 ms to fit; persisting a pickle adds invalidation hazards for no real gain |
| Refit policy | Per-observe (full rebuild on each new observation) | ~50 ms latency at current scale; sole user, won't hit pathological corpus size |
| Refit-cost scaling concern | Accept the soft cap | Sole user, ~5 K obs upper bound; ~500 ms is still acceptable; revisit if scale changes |
| Library choice | Hand-rolled, stdlib only | Sklearn adds 80–100 MB deps for ~60 LOC of work; "no dependencies" is the point of this backend |
| Tokenizer strategy | Words + character 4-grams | Words alone lose morphology bridging (`testing` ↔ `tests`); char n-grams add robustness without sklearn |
| `hybrid_search` integration | Parallel Python-side fusion module (`tfidf_search`) | Keeps `hybrid_search` pure SQL and zero-risk; tfidf path is fully separate |
| Reflections scope | Out of scope | `reflection_embeddings` table is vestigial — confirmed no INSERT path in code |
| Mixed-backend half-state | Document the gotcha; do not auto-detect or auto-reindex | Escape hatch noted: add `memory.reindex` tool later if it becomes a problem |

## Components

### `better_memory/config.py` — modify

Add a new field to `Config`:

```python
embeddings_backend: Literal["ollama", "tfidf"]
```

Read from `BETTER_MEMORY_EMBEDDINGS_BACKEND` env var. Default `"ollama"`. Unknown values raise `ValueError` at `get_config()` time so misconfiguration fails fast at startup.

### `better_memory/embeddings/tfidf.py` — new

Single file. Pure stdlib. ~150 LOC including docstrings.

**Public surface:**

```python
def tokenize(text: str) -> list[str]: ...
    # lowercase
    # word tokens via re.findall(r"[a-z0-9_]+", text), len >= 2
    # char 4-grams via sliding window over lowercased text
    # returns word tokens + char 4-gram tokens (4-grams prefixed e.g. "#abcd")

class TfidfRetriever:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
        # state: _vocab (set[str]), _idf (dict[str, float]),
        #        _doc_vectors (dict[str, dict[str, float]])

    def fit_from_db(self) -> None: ...
        # SELECT id, content FROM observations
        # Build vocab, IDF, doc_vectors. L2-normalise each doc_vector.

    def add_doc(self, doc_id: str, text: str) -> None: ...
        # Full refit: extend corpus, rebuild IDF, rebuild all doc_vectors.

    def remove_doc(self, doc_id: str) -> None: ...
        # Full refit on the remaining corpus.

    def vectorize(self, text: str) -> dict[str, float]: ...
        # Apply tokenizer, count tf, multiply by IDF, L2-normalise.
        # Tokens not in _vocab are dropped silently (sklearn convention).

    def score(self, query: str, candidate_ids: list[str]) -> list[tuple[str, float]]: ...
        # Vectorize query, cosine sim (dot product since both normalised),
        # sort desc, return all candidates with scores.
```

**Tokenizer details:**
- Word tokens: `re.findall(r"[a-z0-9_]+", text.lower())`, drop tokens with `len < 2`.
- Char n-grams: 4-gram sliding window over `text.lower()` (spaces included), prefixed with `"#"` to keep namespace distinct from word tokens.
- Output: `list[word_tokens] + list[ngram_tokens]`.

**IDF formula:** smoothed `idf(t) = log((N + 1) / (df(t) + 1)) + 1`.

**Vector normalisation:** L2 on the post-IDF weights so cosine similarity reduces to dot product.

### `better_memory/search/tfidf_search.py` — new

Mirrors the public shape of `hybrid_search`. Reuses private helpers from `hybrid` rather than duplicating SQL.

```python
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
    # 1. FTS5 BM25 top-K via hybrid._fts_candidates
    # 2. TF-IDF candidates: fetch all observation ids matching the SQL
    #    filters (one SELECT id FROM observations WHERE ...), call
    #    retriever.score(query, those_ids), take top candidate_k.
    #    At 5K obs this is well under 10 ms.
    # 3. Fuse with hybrid._add_rrf_ranks
    # 4. Hydrate rows via hybrid._fetch_rows
    # 5. Apply reinforcement + recency via hybrid._finalize
    # 6. Sort and truncate as in hybrid_search
```

`hybrid.py`'s private helpers (`_fts_candidates`, `_build_where`, `_add_rrf_ranks`, `_fetch_rows`, `_finalize`, `_age_in_days`) are imported directly (same-package private import is conventional). If this proves brittle, a follow-up extracts them into `search/_shared.py`.

### `better_memory/embeddings/__init__.py` — modify

Export the new class:

```python
from better_memory.embeddings.ollama import EmbeddingError, OllamaEmbedder
from better_memory.embeddings.tfidf import TfidfRetriever

__all__ = ["OllamaEmbedder", "EmbeddingError", "TfidfRetriever"]
```

### `better_memory/mcp/server.py` — modify

Branch on backend in `build_server` (around line 936):

```python
if config.embeddings_backend == "ollama":
    embedder = OllamaEmbedder()
    retriever = None
    _probe_ollama(config.ollama_host)
else:
    embedder = None
    retriever = TfidfRetriever(memory_conn)
    retriever.fit_from_db()

observations = ObservationService(
    memory_conn, embedder=embedder, retriever=retriever, episodes=episodes
)
```

Cleanup function close logic gains an `if embedder is not None: await embedder.aclose()` guard.

### `better_memory/services/observation.py` — modify

Constructor signature evolves to accept either an embedder or a retriever (exactly one of them is non-None):

```python
def __init__(
    self,
    conn: sqlite3.Connection,
    embedder: OllamaEmbedder | None = None,
    *,
    retriever: TfidfRetriever | None = None,
    ...
)
```

`create` method gains a backend-switched indexing step replacing the current embed+INSERT block:

```python
if self._embedder is not None:
    vector = await self._embedder.embed(content)
    vec_blob = sqlite_vec.serialize_float32(vector)
    conn.execute(
        "INSERT INTO observation_embeddings (observation_id, embedding) VALUES (?, ?)",
        (obs_id, vec_blob),
    )
else:
    # tfidf path — add to in-memory retriever AFTER the row commits successfully
    # (deferred to post-commit; see Open Question below)
```

`retrieve` and `_list_observations_via_hybrid_search` route to `tfidf_search` when `retriever is not None`. The existing `_run(outcome, limit)` closure inside `retrieve` becomes:

```python
def _run(outcome: Outcome, limit: int) -> list[SearchResult]:
    filters = SearchFilters(outcome=outcome, **base_kwargs)
    if self._embedder is not None:
        return hybrid_search(self._conn, query_text=fts_query_text,
                             query_vector=query_vector, filters=filters, ...)
    return tfidf_search(self._conn, self._retriever,
                        query_text=fts_query_text, filters=filters, ...)
```

### Docs — modify

- `README.md` — add `BETTER_MEMORY_EMBEDDINGS_BACKEND` to the env-var prerequisites table; note that `tfidf` removes the Ollama prerequisite entirely.
- `website/configuration.md` — add row to env var table.
- `website/architecture.md` — new short "Embeddings backends" section describing the switch and trade-offs.

## Data flow

### Write — `memory.observe` (tfidf mode)

```
1. Resolve project, scope, episode (unchanged)
2. SAVEPOINT observation_create
   3. INSERT observations row
   4. Write audit row
5. RELEASE SAVEPOINT, conn.commit()
6. retriever.add_doc(obs_id, content)
   - tokenize content (words + char 4-grams)
   - update vocab
   - recompute IDF over corpus
   - rebuild all doc_vectors (L2-normalised)
   - typical cost: ~50 ms at 500 docs
7. Return obs_id

NOTE: retriever.add_doc happens AFTER commit. If add_doc raises, the
observation row is still durable; next MCP restart's fit_from_db will
pick it up. If the server crashes between commit and add_doc, the
in-mem state is stale for the rest of that session — next session
recovers via fit_from_db.
```

### Read — `memory.retrieve` (tfidf mode)

```
For each outcome bucket (do / dont / neutral):
  1. Build SearchFilters with outcome + project + component + window
  2. tfidf_search:
     a. FTS5 BM25 top-K candidate ids (SQL)
     b. retriever.score(query, candidate_ids) — vectorize query,
        compute cosine vs in-mem doc_vectors, return ranked list
     c. RRF fuse the two ranked lists
     d. Hydrate rows via SQL IN (...)
     e. Apply reinforcement multiplier (1 + alpha * reinforcement_score)
     f. Apply exponential recency decay (14-day half-life)
     g. Sort by final_score desc, truncate to limit
  3. Return SearchResult list
```

Candidate set sizing for the TF-IDF side: at 467 obs, scoring all observations matching the SQL filter is cheap (sub-millisecond). At 5 K, the same approach is still well under 10 ms. No optimisation needed.

## Error handling

- `BETTER_MEMORY_EMBEDDINGS_BACKEND` set to anything other than `ollama`/`tfidf`: raise `ValueError` at `get_config()`. Server refuses to start. Loud, fast, obvious.
- `TfidfRetriever.fit_from_db` on empty observations table: state starts empty; `score()` returns `[]` for any query. Safe.
- `retriever.score(query, [])`: returns `[]`. Safe.
- Query tokens entirely outside vocabulary: query vector is empty dict; cosine returns 0 for all docs; FTS5 BM25 carries the result alone. Safe.
- `retriever.add_doc` raises after `commit`: row is durable; log a warning; next MCP restart recovers via `fit_from_db`.
- Switching backends mid-deployment: documented as a half-state; old `observation_embeddings` rows simply ignored in tfidf mode and absent for tfidf-written rows in ollama mode. No crash, just degraded retrieval for the orphan rows.

## Testing strategy

- **Unit:** `tests/embeddings/test_tfidf_unit.py` covers tokenizer (word + char-ngram outputs, edge cases), IDF computation, vectorize symmetry, score monotonicity, fit/add_doc/remove_doc state consistency.
- **Integration:** `tests/search/test_tfidf_search.py` end-to-end FTS5 + TF-IDF fusion behaviour against a seeded in-memory SQLite. Compare against an oracle (hand-computed cosine on a 5-obs corpus).
- **Service integration:** `tests/services/test_observation_tfidf.py` exercises the tfidf path through `ObservationService.create` and `.retrieve` — fresh sqlite, no embedder, asserts no Ollama HTTP and correct results.
- **No regression on ollama path:** existing test suite continues to use the ollama backend; no test modifications required for them. Add a single smoke test asserting `config.embeddings_backend == "ollama"` is the default.
- **Config validation:** unit test that an unknown backend value raises `ValueError`.

## Documentation

- README env-var table gains the new switch and notes that `tfidf` removes the Ollama prerequisite.
- `website/configuration.md` env-var table updated.
- `website/architecture.md` gains a 1–2 paragraph "Embeddings backends" section after the existing storage description.
- A short troubleshooting line: "If retrieval quality is poor in tfidf mode and the corpus has grown, switch back to ollama or file an issue."

## Open questions (deferred)

- Should `add_doc` happen before or after the observation row's commit? Spec proposes **after** to keep the SAVEPOINT focused on the durable write. Documented retry semantics: rebuild from DB on next session start. Plan task ordering will revisit if a clearer pattern emerges during implementation.
- `memory.reindex` MCP tool to fix mixed-backend half-state: deferred. Only build if the half-state actually causes complaints.

## Rollout

- Default unchanged → no behaviour change for existing users.
- Documentation calls out the switch as the recommended path for work / locked-down environments.
- No data migration required.
- Switching back to ollama works — new observations get embedded fresh; old tfidf-era observations lack vec0 rows and won't appear in the kNN side of hybrid_search, but FTS5 BM25 still surfaces them. Documented as a known degradation; a future `memory.reindex` can fix it if needed.
