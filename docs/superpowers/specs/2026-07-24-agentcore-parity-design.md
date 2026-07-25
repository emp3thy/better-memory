# PR-E: AgentCore learning-loop parity

**Date:** 2026-07-24
**Status:** approved design (option A + ratings-event append, chosen 2026-07-24), pending implementation
**Predecessors:** #83 (Wilson/exploration/embeddings), #84 (deferred injection), #85 (evidence ratings) — all currently sqlite-only for the learning loop.

## Goal

A team on the `agentcore` backend gets the full learning loop: shared memories
AND shared scoring. Every teammate's ratings move the same Wilson scores;
retrieval and injection are relevance-gated; evidence discipline holds
everywhere. Cross-machine sharing is the point (user requirement 2026-07-24).

## Grounding (investigated 2026-07-24; see the two research reports)

- AgentCore metadata already declares every counter we need on BOTH strategies
  (`useful_count`, `ignored_count`, `overlooked_count`, `times_misled`,
  `last_credited_at`, `status` — `cli/_agentcore_strategies.py:11-78`), and
  `AgentCoreBackend.credit_one` (`agentcore.py:1355-1449`) already performs
  read-modify-write counter bumps via `batch_update_memory_records` for both
  record shapes (metadata for AWS-extracted, body-JSON for migrated).
- **`ignored_count` is written but never read** — `_parse_reflection_record`
  hard-codes `times_ignored=0` (`agentcore.py:522`); the semantic parser never
  reads it either.
- `RetrieveMemoryRecords` is server-side **semantic search** (cosine `score`,
  `topK` ≤100, metadata pre-filters) — a free relevance leg agentcore never
  uses (`memory.retrieve`'s `query` is accepted-and-ignored,
  `agentcore.py:232-234`).
- `memory.db` exists and receives migrations unconditionally in agentcore mode
  (`mcp/server.py:165-166`); the Stop-hook sweep already reads it ungated
  (`hooks/session_close.py:167-170`).
- No optimistic concurrency on record updates (documented absence):
  last-write-wins; concurrent counter bumps can lose increments. Accepted —
  counters are statistics, not ledgers.
- `BatchUpdateMemoryRecords` can update metadata without content; batch ≤100;
  batch APIs share a 20 TPS pool. Events (`CreateEvent`) are a durable,
  immediately-listable log; `extractionMode: "SKIP"` keeps them out of LLM
  extraction; payload ≤10 MB; per-session TPS 10 (non-conversational).
- Silent-drop trap (documented AWS behaviour): batch create/update WITH a
  `memoryStrategyId` silently strips metadata keys absent from that strategy's
  `memoryRecordSchema`. Our counters are all declared, so safe — but the trap
  gets a docs callout.

## Design

### 1. Exposure ledger goes local-in-agentcore (deliberate constraint revision)

`session_memory_exposure` in the local `memory.db` becomes the exposure/
evidence ledger for BOTH backends. Rationale: exposures are per-session
operational state; sessions never span machines; nothing cross-machine is
lost. The old rule "agentcore mode must never open the local sqlite database"
(`hooks/session_bootstrap.py:93`) is revised to: *agentcore mode never stores
MEMORY CONTENT locally; session-operational state (exposure ledger, migration
ledger, hook errors) lives in the local memory.db.* Website + docstrings
updated accordingly.

Mechanics:
- `AgentCoreBackend.__init__` gains `local_conn: sqlite3.Connection | None`;
  `build_backend` forwards the `memory_conn` it already receives.
- `record_exposures` / `list_session_exposures`: stop being no-ops; delegate
  to the same SQL used by the sqlite path (extract the existing
  exposure-write/read helpers from `services/session_bootstrap.py` into a
  small shared module `services/exposure_log.py` consumed by both backends —
  single implementation, no duplication). First-source-wins dedup and
  `via_exploration` tagging carry over unchanged.
- Hooks: `session_bootstrap` and `contextual_inject` open the local db in
  agentcore mode too (for the ledger and seen-store paths); memory-content
  reads still go through the backend. `session_close`'s sweep needs no change
  — it already reads the local table.

### 2. Ratings: local stamp + shared counters + evidence event

`AgentCoreBackend.apply_session_ratings` becomes the real sweep:
1. Validate the batch with the SHARED validation (import
   `MemoryRatingService`'s `_validate_evidence` / batch checks or extract
   them to a shared helper) — evidence contract identical to sqlite,
   including rejecting non-ignored without evidence. (Closes the current
   divergence where agentcore skips evidence validation entirely.)
2. Stamp local exposure rows (`rated_at`, `classification`, `evidence`) —
   same UPDATE as sqlite; skip-buckets (`not_exposed`, `already_rated`)
   become real on agentcore.
3. Push counter bumps to AWS through the existing `credit_one` machinery —
   including `ignored` → `ignored_count` (the sweep is the legitimate writer
   of ignores; see §4 for the tool-surface alignment).
4. **Ratings event (C-lite):** after a successful sweep, one best-effort
   `CreateEvent` (`extractionMode: "SKIP"`, `metadata: {"type": "ratings"}`,
   real `sessionId`, payload = JSON blob of the rated batch incl. evidence
   lines). Durable team-visible receipts; read/UI path deferred to a future
   PR. Failure never blocks the sweep.

`credit_one` (single-memory, mid-session): local stamp when the exposure row
exists + AWS counter bump + evidence stored locally. Class surface aligned
with sqlite: **reject `ignored`** (drop it from `_RATING_TO_COUNTER`'s accepted
input on this path; the sweep applies ignores).

### 3. Ranking + relevance parity

- **Read the counters back:** `times_ignored` parsed from body `ignored_count`
  / metadata `ignored_count` in `_parse_reflection_record` AND
  `_semantic_summary_to_model` (replacing the hard-coded 0 / dataclass
  default). Wilson inputs become real on agentcore.
- **Unified ranking:** `_fetch_reflection_buckets`' legacy linear sort
  (`useful_count + 3*overlooked`, `agentcore.py:353-362`) is replaced by the
  shared Wilson ordering (score desc, confidence desc, recency desc) using
  `services/scoring.wilson_lower_bound` — same formula, same tiebreaks as
  sqlite. The stale `_OVERLOOKED_RANKING_WEIGHT` constant and comment are
  deleted. Exploration slot: same reserved-slot rule as sqlite, applied in
  the bucket build; exploration serves tagged `via_exploration` in the local
  ledger.
- **`query` works:** when `memory.retrieve` gets a `query` in agentcore mode,
  call `RetrieveMemoryRecords(searchQuery=query, topK=~50)` per namespace,
  build a relevance rank from the returned `score` order, and RRF-fuse with
  the Wilson prior rank (same `1/(60+rank)` shape as sqlite's fusion; no BM25
  leg — the server-side semantic search subsumes it). Degrades to
  Wilson-only on AWS error.
- **Contextual gate:** `retrieve_relevant` gains a backend relevance source:
  a new `StorageBackend.relevance_ranks(query, kinds) -> dict[(kind,id), rank]`
  protocol method — sqlite implements it as a thin wrapper over its existing
  BM25+vec legs (no behaviour change); agentcore implements it via
  `RetrieveMemoryRecords`. The evidence gate on agentcore becomes: qualifies
  iff present in the Retrieve result set (server-side semantic evidence);
  keyword fallback remains only for AWS-error degradation. Wilson still ranks,
  never qualifies.

### 4. Contract alignments and cleanups

- Agentcore `credit_one` rejects `ignored` (sqlite parity).
- Evidence validation identical on both backends (shared helper).
- Protocol docstrings updated: exposure methods no longer "sqlite-only";
  `times_ignored` no longer "always 0 on agentcore".
- Docs: silent-metadata-drop trap callout in agentcore-setup.md; capability
  table updated (learning loop now backend-agnostic; what remains
  agentcore-different: synthesis/episodes/retention as before, evidence
  browsing local-only until the ratings-event read path lands).

## Non-goals

- Ratings-event READ path / team evidence-audit UI (future PR; events are
  durable from day one).
- Episode/synthesis/retention parity (unchanged agentcore gaps).
- Counter-race hardening (sharded counters / event-sourced reconciliation) —
  revisit only if lost increments are observed in practice.
- Embedding writes from agentcore mode (server-side search makes local
  vectors unnecessary there).

## Error handling

All AWS calls best-effort with the existing retry-on-transient-404 and
reserved-metadata-strip helpers; ledger writes never block retrieval;
ratings event never blocks the sweep; Retrieve failure degrades ranking to
Wilson-only and the contextual gate to keyword fallback. Local-db absence in
agentcore mode (fresh install, no migrations yet) degrades exposure calls to
the current no-op behaviour rather than raising.

## Validation

- Unit: stubbed-boto tests per the established `test_agentcore_unit.py`
  pattern — counter read-back (body + metadata), Wilson sort parity (same
  fixture ranks identically through both backends), fusion with a stubbed
  Retrieve response, exposure ledger read/write via the shared module,
  sweep = local stamp + credit calls + one SKIP event, evidence rejection
  parity, `ignored` rejection on credit.
- Integration: full rating loop e2e in agentcore mode with stubbed AWS —
  bootstrap/contextual exposure → sweep → local rows stamped + counter
  bumps issued + event emitted.
- Full suite + pyright. No live-AWS tests in CI; a manual smoke checklist
  against the real account goes in the PR body (run `agentcore smoke`, one
  real sweep, verify counters via `get_memory_record`).
