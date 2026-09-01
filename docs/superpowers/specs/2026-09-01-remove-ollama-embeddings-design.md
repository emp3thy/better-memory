# Remove Ollama and local embeddings — design

**Date:** 2026-09-01
**Status:** Approved (section-by-section walkthrough in brainstorming session)
**Scope decision:** Clean break (option A of A/B/C), single feature branch, single PR.

## Context

better-memory has two orthogonal backend knobs:

- `storage_backend`: `sqlite` | `agentcore` — where memories live.
- `embeddings_backend`: `ollama` | `sqlite` — how local retrieval ranks. The
  `sqlite` value means FTS5/BM25 only, with no embedder constructed anywhere.

The `ollama` embeddings path is the only consumer of Ollama and of local
vector search (sqlite-vec vec0 tables). The sole user runs `agentcore`
storage at home (server-side semantic search; local vectors computed and
discarded) and `sqlite` embeddings at work (Ollama disallowed there). The
ollama path has caused real bugs: #97 (write lock held across blocking
embeds) and, pending confirmation, #96 (blocking `thread.join` from async
handlers). Decision: delete the Ollama/embedding subsystem entirely.

## Goals

- No better-memory process contacts Ollama or computes embeddings, in any mode.
- Retrieval collapses to one architecture per storage mode:
  - sqlite storage: BM25 over FTS5 (trigger-maintained) + keyword fallback —
    byte-for-byte the existing `embeddings_backend=sqlite` behaviour.
  - agentcore storage: server-side `relevance_ranks` semantic search, unchanged.
- Existing databases shed their vec0 tables via migration.
- Stale environment variables on any machine are silently unread — no errors,
  no deprecation shims.

## Non-goals

- Removing the `sqlite-vec` pip dependency this release. `db/connection.py`
  keeps `sqlite_vec.load()` so migration 0015 can `DROP` the vec0 virtual
  tables (SQLite cannot drop a virtual table whose module is unregistered).
  Dep + load removal is a follow-up release, tracked by a new issue filed at
  branch end.
- Rewriting historical migrations (0001/0002/0014 still create vec tables;
  0015 drops them — fresh installs create-then-drop, accepted).
- Editing historical docs (specs/plans/archive keep their ollama mentions).
- Replacing local vector quality with anything new. BM25 + keyword is the
  accepted local retrieval floor.

## Component changes

### Config (`config.py`, `_diag.py`, `mcp/_util.py`)

Delete fields `ollama_host`, `embed_model`, `embeddings_backend`,
`context_vec_floor`; delete `_resolve_embeddings_backend` and the
`_DEFAULT_OLLAMA_HOST` / `_DEFAULT_EMBED_MODEL` /
`_DEFAULT_EMBEDDINGS_BACKEND` / `_VALID_EMBEDDINGS_BACKENDS` constants and
the env-var doc comment. Delete the `BETTER_MEMORY_EMBED_LOG` read in
`_diag.py` and its comments. Env vars `OLLAMA_HOST`, `EMBED_MODEL`,
`BETTER_MEMORY_EMBEDDINGS_BACKEND`, `BETTER_MEMORY_EMBED_LOG` become unread.
`storage_backend` resolution and all other knobs untouched.

### Wiring (`mcp/server.py`, `hooks/contextual_inject.py`, `ui/app.py`, `cli/`)

- `mcp/server.py`: delete `_probe_ollama` and its startup call; delete
  embedder/sync_embedder imports and construction; delete
  `ServerContext.embedder` and the `aclose()` cleanup branch; construct
  `ObservationService`, `build_backend`, `ReflectionSynthesisService`,
  `SemanticMemoryService` without embedder arguments.
- `hooks/contextual_inject.py`: delete embedder build and `vec_floor`
  pass-through; keep `conn` (feeds BM25 in sqlite mode). This structurally
  fixes the agentcore wasted-embed defect (query embedded then discarded
  because `_vec_qualifiers` returns `{}` for `conn=None`).
- `ui/app.py`: delete `_build_sync_embedder` and the `sync_embedder`
  parameter/resolution. UI semantic edits no longer re-embed; agentcore
  re-embeds server-side, sqlite FTS triggers reindex on UPDATE.
- `cli/backfill_embeddings.py`: delete the file and the stale comment
  referencing it in `reflection.py`.

### Services and search

- `services/semantic.py`: remove `sync_embedder` param, `_store_embedding`,
  and the three embed calls. `update_text` reverts to plain UPDATE +
  rowcount `ValueError`; the PR #125 `SELECT 1` fast-fail guard existed only
  to avoid a wasted embed and goes with it. The rollback-on-zero-rowcount
  stays (guards the WAL lock).
- `services/reflection.py`: remove `sync_embedder` param, `_store_embedding`,
  `_heal_missing_embeddings`, `_vec_ranks`, the retrieval query embed, batch
  embeds, the `sqlite_vec` import, and the whole `embed_tasks`
  collect-then-embed-after-commit machinery introduced by #97/#125.
- `services/relevant.py`: remove `qvec`, `_vec_qualifiers`, both vec legs,
  the `vec_floor` parameter, and the `sqlite_vec` import. Evidence gate
  becomes: BM25 match, OR agentcore `relevance_ranks` membership (branch
  untouched), OR keyword fallback when FTS is structurally unavailable. RRF
  fusion over Wilson prior + BM25 rank unchanged.
- `services/observation.py`: remove the async `embedder` parameter and the
  vec0 write path.
- `storage/sqlite.py`: `search_hybrid` stops embedding the query.
- `search/hybrid.py`: remove the vector leg; collapses to FTS5/BM25 only. If
  the survivor is a trivial wrapper, fold it into its caller — decided at
  implementation with the reviewer, not silently.

### Schema

New `db/migrations/0015_drop_vec_tables.sql`:

```sql
DROP TABLE IF EXISTS observation_embeddings;
DROP TABLE IF EXISTS reflection_embeddings;
DROP TABLE IF EXISTS semantic_embeddings;
```

Legal because the sqlite-vec module load survives this release. Implementation
verifies no triggers/views reference these tables (grep migrations 0001–0014)
and adds `DROP TRIGGER IF EXISTS` lines only if found. Idempotent DROPs make
issue #27's non-atomic executescript hazard harmless here — a rerun completes
the migration. Row data in the three tables is destroyed; vectors are derived
data never read after this branch.

### Deletions (whole artifacts)

- `better_memory/embeddings/` (ollama.py, sync_embed.py, `__init__.py`)
- `better_memory/cli/backfill_embeddings.py`
- `tests/embeddings/` (3 files), `tests/cli/test_backfill_embeddings.py`,
  `tests/services/test_vec_fusion.py`,
  `tests/services/test_reflection_embedding_write.py`,
  `tests/services/test_semantic_embedding_write.py`,
  `tests/services/_embedding_fakes.py`

### Tests

~25 files modified to drop embedder fakes/params/skip-markers (relevant,
hybrid, sqlite backend, factory, connection — sqlite-vec load tests stay —
ui, mcp server/dispatch/integration, e2e journey/negative/tripwires,
env-helper contract, contextual_inject, config/diag/setup). Two vec-table
schema tests flip to asserting the tables are gone post-0015.

Added:

- FTS-substrate regression test: under default config, observe → FTS row
  exists → retrieval returns it via BM25 evidence. Pins the only remaining
  local evidence leg as unconditional.
- Migration 0015 test: apply on a DB with populated vec tables → gone;
  rerun clean.

Gate: full pytest suite and Pyright green after every task on the branch.

### Docs (guardrail: website-sync reflection, confidence 1.0)

Update living docs: `website/configuration.md` env table,
`website/architecture.md` retrieval/synthesis prose, `website/index.md`
tagline/showcase, `README.md`, living `docs/agentcore-ui/*` SyncEmbedder and
re-embedding mentions. Historical `docs/superpowers/**` untouched. Run
`graphify update .` after code changes land.

### Issues and release

- PR claims `Fixes #96` only after confirming the deleted embed path is the
  sole `run_async_in_worker`/`thread.join` caller chain; otherwise #96 stays
  open with a scope-narrowing comment.
- #27 untouched. New follow-up issue: remove sqlite-vec dep + connection load.
- Single squash-merge PR per repo convention.

## Assumption surface

**Real concerns (1):**

1. FTS5 substrate must be unconditional — BM25 becomes the only local
   evidence leg. Believed unconditional (triggers live in migrations), but if
   indexing was ever gated on the old embeddings knob, sqlite-storage
   retrieval starves. Mitigation: explicit verification task in the plan +
   the FTS-substrate regression test. If gating is found, making triggers
   unconditional becomes a plan task, not a silent fix.

**Verified safe (3):**

1. agentcore mode makes zero local embed/vec calls (read `storage/agentcore.py`,
   `mcp/handlers/*`, `mcp/server.py:261-263`).
2. Stale env vars on any machine become unread no-ops.
3. vec0 DROP is legal because the module load survives this release.

**Minor/accepted (4):**

1. Inert `BETTER_MEMORY_EMBED_LOG` entry remains in the user's `.claude.json`
   MCP registration until manually removed.
2. Historical docs keep ollama mentions.
3. PR #125's `embed_tasks` machinery is deleted the same day it merged (the
   merge was still correct — it unblocked the database immediately).
4. Fresh installs create-then-drop vec tables via 0015.
