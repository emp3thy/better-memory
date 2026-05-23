# Trigram-FTS5 Backend — Design

**Status:** Approved (design phase)
**Date:** 2026-05-23
**Author:** gethin (with Claude)
**Supersedes:** `2026-05-22-tfidf-embeddings-backend-design.md` (Python TF-IDF backend, merged 2026-05-22 in PR #65)

## Goal

Replace the Python TF-IDF backend with a pure-SQL implementation built on SQLite FTS5's trigram tokenizer. Same public switch (`BETTER_MEMORY_EMBEDDINGS_BACKEND`), same retrieval contract, same default (`ollama`), but the no-Ollama path becomes ~60% less code with zero in-memory state and zero refit cost.

The non-Ollama backend value changes from `tfidf` to `sqlite` (one-day-old config break, no installed user base).

## Non-Goals

- Replacing the `ollama` backend — it stays exactly as it is.
- A third backend. The previous design's TF-IDF path is **deleted**, not parallelised.
- Reflections-side indexing. Still out of scope.
- Backfilling vec0 rows for sqlite-era observations on switchback to ollama (same mixed-backend half-state documented in the superseded spec).

## Constraints

- Single user (project author). Corpus: 467 observations today, ~5K upper bound planned.
- No Python tokenizer code, no in-memory state, no model files.
- SQLite 3.34+ for the trigram tokenizer (3.49.1 confirmed locally).
- Windows + Linux + macOS host compatibility.

## Architecture

```
                  BETTER_MEMORY_EMBEDDINGS_BACKEND
                                 |
              +------------------+------------------+
              |                                     |
           "ollama"                            "sqlite"
              |                                     |
   OllamaEmbedder() + probe         (no embedder; FTS5 triggers
              |                       handle indexing on insert)
              |                                     |
              +---> ObservationService <------------+
                            |
                            +---> memory.observe (write path)
                            |       - INSERT observations row
                            |       - triggers: observation_fts (word) + observation_trigram_fts (NEW)
                            |       - ollama: embed + INSERT vec0 row
                            |
                            +---> memory.retrieve (read path)
                                    hybrid_search(..., second_source=...)
                                      - word-FTS5 BM25 (always)
                                      - ollama: vec0 kNN
                                      - sqlite: trigram-FTS5 BM25
                                      - RRF fuse + reinforcement + recency
```

## Decisions

| Question | Decision | Reasoning |
|---|---|---|
| Replace or coexist? | Replace | Python TF-IDF is one day old, no installed users; keeping both means owning two non-Ollama paths for no benefit |
| Search shape | Symmetric two-source RRF (word-FTS5 + trigram-FTS5) | Mirrors the ollama path (word + second source), preserves crisp exact-match ranking, trigrams add morphology on top |
| Switch value name | Rename `tfidf` → `sqlite` | The new implementation does not compute TF-IDF; calling it `tfidf` would be a misnomer baked into the public surface |
| Code structure | Unify into `hybrid_search` via a `second_source` parameter | Single source-of-truth for fusion + finalize; delete the parallel `tfidf_search` module |
| Schema migration | New migration file with `CREATE VIRTUAL TABLE`, triggers, AND backfill SQL | The trigram FTS5 table needs to be populated from existing observations at migration time, not at first observation write |
| Conditional indexing? | No — both FTS5 tables populated in both backends | Conditional-trigger logic adds complexity for trivial storage savings (~1 MB at current scale) |

## Components

### `better_memory/db/migrations/0011_trigram_fts.sql` — new

Creates a contentless FTS5 virtual table backed by `observations.content`, populates it from existing rows, and adds INSERT/UPDATE/DELETE triggers mirroring the existing `observation_fts` triggers.

```sql
-- 0011_trigram_fts.sql

CREATE VIRTUAL TABLE observation_trigram_fts USING fts5(
    content,
    content='observations',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Backfill from existing rows.
INSERT INTO observation_trigram_fts(rowid, content)
SELECT rowid, content FROM observations;

-- Keep in sync with the observations table.
CREATE TRIGGER observation_trigram_fts_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_trigram_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER observation_trigram_fts_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER observation_trigram_fts_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO observation_trigram_fts(rowid, content) VALUES (new.rowid, new.content);
END;
```

The trigger names mirror the existing `observation_fts_ai/ad/au` pattern in the earlier migration that created `observation_fts`. (Verify exact pattern at implementation time and align.)

### `better_memory/search/hybrid.py` — modify

`hybrid_search` gains one new parameter:

```python
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
```

Branching:
- `second_source="vec0"` (default) — existing behaviour: needs `query_vector`, calls `_vec_candidates`
- `second_source="trigram"` — new path: ignores `query_vector`, calls a new `_trigram_candidates(query_text, ...)` helper that runs FTS5 BM25 against `observation_trigram_fts`
- `second_source="none"` — FTS5 word-only mode (useful for tests or fallback)

The fusion + finalize stays unchanged; the source identity tag in RRF stays `"vec"` for both vec0 and trigram (downstream code already treats it as "the second source").

### `better_memory/services/observation.py` — modify

- Drop the `retriever` kwarg + the XOR check (added in PR #65, now removed).
- Drop the post-commit `retriever.add_doc` call — FTS5 triggers handle indexing automatically.
- `create()`: only branches on `embedder is None` for the vec0 INSERT step. The two FTS5 tables are populated by triggers regardless of backend.
- `retrieve()` and `_list_observations_via_hybrid_search`: call `hybrid_search(..., second_source="vec0" if self._embedder is not None else "trigram", ...)`.
- The conditional `query_vector = await self._embedder.embed(query)` stays — but only when `embedder` is set.

### `better_memory/config.py` — modify

Rename the Literal value and validator tuple:

```python
_DEFAULT_EMBEDDINGS_BACKEND = "ollama"
_VALID_EMBEDDINGS_BACKENDS = ("ollama", "sqlite")  # was ("ollama", "tfidf")

@dataclass(frozen=True)
class Config:
    ...
    embeddings_backend: Literal["ollama", "sqlite"]  # was Literal["ollama", "tfidf"]
```

The resolver's error message updates accordingly. Module docstring's knob list updates the value.

### `better_memory/mcp/server.py` — modify

Drop `TfidfRetriever` import and instantiation. The build_server branch becomes:

```python
embedder: OllamaEmbedder | None = None
if config.embeddings_backend == "ollama":
    embedder = OllamaEmbedder()
    _probe_ollama(config.ollama_host)
# sqlite mode: embedder stays None; FTS5 triggers handle indexing

observations = ObservationService(memory_conn, embedder=embedder, episodes=episodes)
```

The cleanup guard for `embedder.aclose()` is unchanged (already `if embedder is not None`).

### Deletes

- `better_memory/embeddings/tfidf.py` — TfidfRetriever class, fully removed
- `better_memory/search/tfidf_search.py` — parallel module, fully removed
- `tests/embeddings/test_tfidf_unit.py` — tokenizer + retriever unit tests
- `tests/search/test_tfidf_search.py` — parallel-fusion tests
- `better_memory/embeddings/__init__.py` — remove TfidfRetriever export

### Test changes

- `tests/services/test_observation_tfidf.py` → rename `test_observation_sqlite.py`. Drop the retriever-XOR test. The remaining service-integration tests now exercise the sqlite backend by setting `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite` (or by constructing `ObservationService(conn, embedder=None, ...)` directly).
- `tests/mcp/test_server_tfidf.py` → rename `test_server_sqlite.py`. Same dead-host trick (`OLLAMA_HOST=http://does-not-exist.invalid:1`) plus a real tool call.
- `tests/search/test_hybrid.py` (existing) — extend with cases for `second_source="trigram"` and `second_source="none"`.
- New: `tests/db/test_migration_0011_trigram_fts.py` — exercise the migration against a pre-populated `observations` table, verify backfill plus trigger sync on subsequent INSERT/UPDATE/DELETE.

### Docs

- `README.md` — rename `tfidf` → `sqlite` in the env-var table; replace description ("hand-rolled TF-IDF in-memory") with "trigram-FTS5 fusion in SQL, no in-memory state".
- `website/configuration.md` — rename the row; update link to architecture section.
- `website/architecture.md` — rewrite the "Embeddings backends" section to describe trigram-FTS5 (not Python TF-IDF). Mention that the trigram table is populated even in ollama mode (so switching backends is just a config flip — no migration needed at runtime).
- `better_memory/config.py` module docstring — rename value in knob list.

## Data flow

### Write — `memory.observe` (sqlite mode)

```
1. Resolve project, scope, episode (unchanged).
2. SAVEPOINT observation_create.
3. INSERT observations row.
     → trigger fires: INSERT observation_fts (word tokenizer)
     → trigger fires: INSERT observation_trigram_fts (trigram tokenizer)
4. Write audit row.
5. RELEASE SAVEPOINT, conn.commit().
6. Return obs_id.

NO post-commit indexing.
NO in-memory state to update.
NO refit cost.
```

The vec0 INSERT step is skipped (no embedder). Both FTS5 tables are populated by triggers regardless of backend.

### Read — `memory.retrieve` (sqlite mode)

```
For each bucket (do / dont / neutral):
  hybrid_search(
    conn,
    query_text=fts_query_text,
    second_source="trigram",
    filters=...,
    limit=..., candidate_k=..., reinforcement_alpha=..., clock=self._clock,
  )
  a. Word-FTS5 BM25 top-K (SQL, existing _fts_candidates)
  b. Trigram-FTS5 BM25 top-K (SQL, NEW _trigram_candidates)
  c. RRF fuse the two rankings (existing _add_rrf_ranks)
  d. Hydrate rows via IN-list (existing _fetch_rows)
  e. Apply reinforcement multiplier + recency decay (existing _finalize)
  f. Sort by final_score, truncate to limit
```

## Error handling

- `BETTER_MEMORY_EMBEDDINGS_BACKEND` unknown value: `ValueError` at `get_config()` (unchanged behaviour, updated valid set).
- Trigram tokenizer unavailable (SQLite < 3.34): the migration's `CREATE VIRTUAL TABLE` raises a clear error at migration apply time. Document the SQLite version requirement.
- Empty query in sqlite mode: `hybrid_search` short-circuits to `[]` (unchanged behaviour from existing query_text-None guard).
- Switching ollama → sqlite or sqlite → ollama mid-deployment: FTS5 tables are always populated, so no data migration needed. The only half-state is the vec0 table — already documented in the superseded design and unchanged here.

## Testing strategy

- **Unit (search):** extend `tests/search/test_hybrid.py` with parameterised cases for `second_source="vec0"` (existing behaviour), `second_source="trigram"` (new), `second_source="none"` (FTS5-only fallback).
- **Unit (migration):** new `tests/db/test_migration_0011_trigram_fts.py` — pre-populate `observations`, apply migration, assert `observation_trigram_fts` row count matches; then INSERT/UPDATE/DELETE observations and assert the trigram table stays in sync.
- **Service integration:** renamed `tests/services/test_observation_sqlite.py` — uses `ObservationService(conn, embedder=None)`, exercises create + retrieve + list_observations end-to-end against a sqlite backend, asserts no observation_embeddings rows are written.
- **MCP integration:** renamed `tests/mcp/test_server_sqlite.py` — same dead-host trick that caught the Task 9 wiring gap in PR #65.
- **No regression on ollama path:** existing `test_observation.py` (+28 tests) and `test_hybrid.py` (existing kNN tests) must remain green.

## Documentation

- README env-var table: rename value, rewrite description.
- `website/configuration.md`: rename row.
- `website/architecture.md`: rewrite the "Embeddings backends" section. Note that the trigram FTS5 table is always-populated, so switching backends needs no data migration.
- `better_memory/config.py` module docstring: rename value in knob list.
- Add SUPERSEDED notice to `docs/superpowers/specs/2026-05-22-tfidf-embeddings-backend-design.md` and its plan, pointing at this spec.

## Rollout

- Default unchanged → zero behaviour change for existing users.
- Schema migration 0011 applied at next process start; backfill is a one-time SQL pass (<1 second at 467 obs).
- Switching `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite` requires no data migration — the trigram table is already populated.
- Going back to `ollama` works as before; new observations get embedded, sqlite-era observations lack vec0 rows (same mixed-backend half-state as the superseded design).
- `tfidf` is no longer a valid value; the resolver's `ValueError` makes the rename immediately discoverable on startup if anyone has it set.

## Open questions (deferred)

- A `memory.reindex` MCP tool to backfill vec0 rows from existing observations (for users switching sqlite → ollama who want full retrieval quality). Still deferred from the superseded design; only build if the half-state causes complaints.
- Whether the trigram table should be conditional on backend (saves ~1 MB in ollama-only deployments). Not worth the trigger complexity at this scale.
