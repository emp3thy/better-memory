# AgentCore Parity (PR-E) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full learning-loop parity on the agentcore backend — local exposure ledger, shared AWS counters, real Wilson ranking, server-side semantic relevance, evidence contract, receipt events.

**Architecture:** A shared `services/exposure_log.py` gives both backends one exposure-ledger implementation over the local `memory.db` (which already exists and migrates in agentcore mode). `AgentCoreBackend` gains `local_conn`; its sweep stamps locally, pushes counters through its existing `credit_one` machinery, and appends one extraction-skipped `CreateEvent` receipt. Ranking unifies on `wilson_lower_bound`; `query` fuses `RetrieveMemoryRecords` cosine ranks with the Wilson prior; the contextual gate uses a new `relevance_ranks` protocol method on the agentcore path.

**Tech Stack:** Python 3.12, sqlite, boto3 (stubbed in tests via MagicMock — established pattern in `tests/storage/test_agentcore_unit.py:47-60`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-agentcore-parity-design.md`

## Global Constraints

- Branch `feat/agentcore-parity` (exists; spec `ee5994b`).
- Test command `./.venv/Scripts/python.exe -m pytest <path> -v`; pyright 0 errors; NO live AWS in tests (MagicMock clients only).
- Sqlite behaviour byte-identical throughout — every extraction/refactor is behaviour-preserving for the sqlite path and pinned by the existing suites.
- All AWS calls best-effort: reuse `_retry_on_transient_404` and the reserved-metadata-strip helper (`x-amz-agentcore-memory-*` keys must be stripped before update writes — `agentcore.py:658-684`).
- Local-db absence in agentcore mode degrades exposure calls to today's no-op/empty behaviour — never raises.
- Evidence contract identical on both backends (non-ignored requires trimmed ≤500; batch-atomic rejection).
- ASCII only; ruff line 100; stage exact paths; commit per task with footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- No new env keys (nothing to add to the conftest strip list).

## Verified-against-source facts (do not re-derive)

| Fact | Where |
|---|---|
| `AgentCoreBackend.__init__(*, config, data_client, control_client, session_id, project)`; `_require_session_id(operation)` re-resolves per call | `agentcore.py:69-115` |
| Test pattern: `MagicMock(name="bedrock-agentcore-data")` clients + `backend` fixture | `tests/storage/test_agentcore_unit.py:10-60` |
| `record_exposures` no-op docstring-only; `list_session_exposures` returns empty envelope | `agentcore.py:1341-1353` |
| `credit_one` fully implemented: `_RATING_TO_COUNTER` (`cited/shaped→useful_count`, `ignored→ignored_count`, `misled→times_misled`, `overlooked→overlooked_count`, `agentcore.py:50-56`); body path `_credit_body_counter` (`:701-757`) for migrated records, metadata path `batch_update_memory_records` (`:1430`) otherwise | `agentcore.py:1355-1449` |
| `apply_session_ratings` = sqlite-parity batch validation minus evidence, then per-entry credit_one; skip buckets mostly inert | `agentcore.py:1451-1540` |
| `times_ignored` hard-coded 0 at `agentcore.py:522`; semantic parser never reads `ignored_count`; `ignored_count` IS declared in both strategy schemas (`cli/_agentcore_strategies.py:11-78`) and IS written by credit_one | investigation 2026-07-24 |
| Legacy sort `-(useful + 3*_overlooked), -confidence, -_updated_at_ts` at `agentcore.py:353-362`; `_OVERLOOKED_RANKING_WEIGHT=3` at `:48` with stale comment | read |
| `retrieve`'s `query` accepted-and-ignored (`agentcore.py:232-234`); `retrieve_memory_records` already used for semantic search at `:909` (shape reference); returns cosine `score`, `topK` ≤100, metadata pre-filters (AWS docs, researched) | read + research |
| No optimistic concurrency on record updates (last-write-wins); batch ≤100; batch APIs 20 TPS pool; `CreateEvent` supports `extractionMode:"SKIP"`, `metadata` (≤15 string entries), payload ≤10MB, immediately listable | AWS docs research 2026-07-24 |
| `memory.db` opened+migrated unconditionally by the server (`mcp/server.py:165-166`); Stop-hook sweep reads it ungated (`session_close.py:167-170`); contextual hook passes `conn=None` in agentcore mode (`contextual_inject.py:115-119`); bootstrap hook deliberately avoids sqlite (`session_bootstrap.py:91-105`, comment line 93 — the rule this PR revises) | read |
| Generic exposure writer `SessionBootstrapService.record_exposures` (dedup INSERT..WHERE NOT EXISTS) at `session_bootstrap.py:145-186`; unrated-list query at `:318-368`; sqlite retrieve-path exposure write w/ `via_exploration` lives in `reflection.py` (~:1560-1600) | read |
| Shared validation: `_validate_evidence` + batch-check loop in `services/memory_rating.py:64-84, 263-297`; `EVIDENCE_MAX_CHARS=500` | read |
| `build_backend(*, config, memory_conn, embedder, sync_embedder, session_id, project)` — agentcore path currently drops `memory_conn` | `storage/factory.py` |
| `retrieve_relevant(backend, *, query, project, conn=None, sync_embedder=None, vec_floor, max_items, ...)` — agentcore branch = keyword fallback via `conn is None` / `qvec is None or conn is None` | `services/relevant.py` (PR-D/PR-B state) |

---

### Task 1: `services/exposure_log.py` — shared ledger module

**Files:**
- Create: `better_memory/services/exposure_log.py`
- Modify: `better_memory/services/session_bootstrap.py` (`record_exposures` delegates; `list_session_exposures` service path delegates)
- Test: `tests/services/test_exposure_log.py`

**Interfaces:**
- Produces (exact):

```python
def record(conn, *, session_id: str, items: list[tuple[str, str]], source: str,
           now: str, exploration_ids: frozenset[str] = frozenset()) -> None
def list_unrated(conn, *, session_id: str) -> list[sqlite3.Row]
    # rows: memory_kind, memory_id, exposed_at (min), source (min), display
def stamp(conn, *, session_id: str, kind: str, memory_id: str,
          classification: str, evidence: str | None, now: str) -> int
    # returns rowcount; the caller decides skip semantics
```

`record` implements the existing first-source-wins INSERT..WHERE NOT EXISTS with `via_exploration = 1` for ids in `exploration_ids`. `list_unrated` is the existing grouped query from `session_bootstrap.py:318-368` (including the reflections/semantic display join). `stamp` is the existing `_apply_one` exposure UPDATE (`SET rated_at=?, classification=?, evidence=? ... AND rated_at IS NULL`). No commits inside the module — callers commit (matches both existing call sites).

- Consumed by Tasks 2, 6. Sqlite services delegate but keep their public signatures; `MemoryRatingService._apply_one` is NOT rewired (its UPDATE stays inline — behaviour-preserving, no churn in the rating service).

- [ ] **Step 1: Write the failing tests** — port the behaviour pins: first-source-wins dedup (bootstrap-then-retrieve keeps `bootstrap`), exploration tagging (id in set ⇒ `via_exploration=1`; dedup wins over tag), unrated list groups by (kind,id) and excludes rated, stamp writes evidence and only unrated rows, no-commit contract (caller sees rows only after its own commit on a second connection — use two connections to the same tmp db to prove it, or simpler: assert `conn.in_transaction` is True after `record`). Use the `tmp_memory_db` fixture + `apply_migrations`; seed reflections/semantics with raw SQL as in `tests/services/test_exploration_tagging.py`.

- [ ] **Step 2:** Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_exposure_log.py -v` — FAIL (module missing).

- [ ] **Step 3:** Implement the module (SQL lifted verbatim from the two source sites, parameterised); delegate `SessionBootstrapService.record_exposures` body to `exposure_log.record(...)` + its own commit, and the service-side unrated listing to `exposure_log.list_unrated`. Do NOT touch `hooks/session_close.py` (standalone hook-side copy stays — note added to its docstring pointing at the module).

- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_exposure_log.py tests/services/test_session_bootstrap.py tests/services/test_exposure_dedup.py tests/services/test_exploration_tagging.py tests/integration/test_memory_rating_e2e.py -q` — all pass (sqlite pins prove behaviour preserved).

- [ ] **Step 5:** Commit `refactor(exposure): shared exposure_log module for both backends`.

---

### Task 2: AgentCoreBackend local ledger

**Files:**
- Modify: `better_memory/storage/agentcore.py` (ctor; `record_exposures`; `list_session_exposures`), `better_memory/storage/factory.py` (thread `memory_conn` → `local_conn`), `better_memory/hooks/contextual_inject.py` (open local db in agentcore mode), `better_memory/hooks/session_bootstrap.py` (revised rule + conn), `better_memory/storage/protocol.py` (docstrings: exposure methods backend-agnostic)
- Test: `tests/storage/test_agentcore_unit.py` (extend), `tests/hooks/test_contextual_inject.py` (agentcore-mode exposure test), `tests/hooks/test_session_bootstrap.py` (agentcore exposure test)

**Interfaces:**
- Produces: `AgentCoreBackend.__init__(..., local_conn: sqlite3.Connection | None = None)`; `record_exposures`/`list_session_exposures` delegate to `exposure_log` when `local_conn` is not None (committing after `record`/`stamp` calls), else exactly today's no-op/empty. Factory: agentcore branch passes `local_conn=memory_conn`. Hooks: `contextual_inject` builds a real `connect(cfg.memory_db)` context in BOTH modes (replacing the `nullcontext(None)` branch) but still passes `conn=None` into `retrieve_relevant` for agentcore (that param means "sqlite FTS/vec available" — unchanged until Task 6); `session_bootstrap` hook passes `memory_conn` to `build_backend` in agentcore mode with the line-93 comment rewritten to the revised rule (content never local; session-operational state is).

- [ ] **Step 1: Failing tests** — backend fixture gains a real tmp sqlite conn (apply_migrations); tests: exposure roundtrip through the backend (`record_exposures` → `list_session_exposures` returns the envelope with rows), no-conn backend keeps legacy empty behaviour, hook-level agentcore-mode test asserting a `contextual` exposure row lands in the local db (mirror the existing sqlite hook test, storage_backend monkeypatched to agentcore with a stubbed backend — follow `tests/hooks/test_contextual_inject.py`'s agentcore test's stubbing style).

- [ ] **Step 2:** Run — FAIL (ctor rejects kwarg).

- [ ] **Step 3:** Implement per Interfaces. `list_session_exposures` resolves the session id via the existing `_require_session_id` fallback pattern but must NOT raise when unresolvable — return the empty envelope (matches sqlite's no-session behaviour).

- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/storage tests/hooks -q` — all pass.

- [ ] **Step 5:** Commit `feat(agentcore): local exposure ledger via shared exposure_log`.

---

### Task 3: Counter read-back (`ignored_count` → `times_ignored`)

**Files:**
- Modify: `better_memory/storage/agentcore.py` (`_parse_reflection_record` ~:495-524; `_semantic_summary_to_model` ~:1016-1030)
- Test: `tests/storage/test_agentcore_unit.py` (extend the parser tests)

**Interfaces:**
- Produces: reflection dicts carry `times_ignored` = body `ignored_count` first, else metadata `ignored_count`, else 0 (same body-first rule as the other counters, via the existing `_count_body_first` helper); `SemanticMemory.times_ignored` likewise populated. The `:522` hard-coded 0 and its comment are deleted; `protocol.py`'s "always 0 on agentcore" sentence updated.

- [ ] **Step 1: Failing tests** — three fixtures per kind: body-shape record with `ignored_count` in content JSON, metadata-shape with `ignored_count` numberValue, neither (→0). Assert `times_ignored` on the returned dict / dataclass.
- [ ] **Step 2:** Run — FAIL (0 everywhere).
- [ ] **Step 3:** Implement (one `_count_body_first("ignored_count", "ignored_count")` call per parser; semantic parser mirrors its existing counter reads).
- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/storage -q` — all pass.
- [ ] **Step 5:** Commit `feat(agentcore): read ignored_count back into times_ignored`.

---

### Task 4: Wilson ranking + exploration slot + retrieve-path exposures

**Files:**
- Modify: `better_memory/storage/agentcore.py` (`_fetch_reflection_buckets` sort ~:353-362 + bucket build; delete `_OVERLOOKED_RANKING_WEIGHT` ~:48; `retrieve` records exposures)
- Test: `tests/storage/test_agentcore_unit.py` (extend)

**Interfaces:**
- Consumes: `wilson_lower_bound` (`services/scoring.py`), `exposure_log` via `local_conn` (Task 2), counters (Task 3).
- Produces: agentcore bucket ordering = `wilson_lower_bound(useful+overlooked, useful+overlooked+ignored)` desc, confidence desc, `_updated_at_ts` desc — identical formula/tiebreaks to sqlite; per-bucket reserved slot for `<3-rated` candidates when `limit_per_bucket >= 2` (same rule as `reflection.py`'s `EXPLORATION_RATED_FLOOR`/two-pass fill — import the constant, replicate the index-based two-pass over each bucket list); `retrieve` writes `source='retrieve'` exposures for returned ids with `exploration_ids` tagged, via `exposure_log` when `local_conn` present (session id via `_require_session_id` fallback-to-skip).

- [ ] **Step 1: Failing tests** — Wilson-order parity fixture: three records (67/125, 3/1, 0/0 counters) assert newcomer-beats-workhorse order (same numbers as `tests/services/test_wilson_ranking.py` so the parity is literal); exploration slot test (3 proven + 1 untested, cap 3 ⇒ untested holds slot 3); exposure rows written with `via_exploration` tag; no-local-conn ⇒ no exposure write, retrieval unaffected.
- [ ] **Step 2:** Run — FAIL (legacy order).
- [ ] **Step 3:** Implement; keep `_` internal keys intact through the sort; strip before return as today.
- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/storage tests/mcp/test_server_backend_dispatch.py -q` — all pass.
- [ ] **Step 5:** Commit `feat(agentcore): shared Wilson ordering, exploration slot, retrieve exposures`.

---

### Task 5: `query` → RetrieveMemoryRecords fusion

**Files:**
- Modify: `better_memory/storage/agentcore.py` (`retrieve` query path ~:232-234; new `_relevance_rank_map` helper)
- Test: `tests/storage/test_agentcore_unit.py` (extend)

**Interfaces:**
- Produces: when `query` is non-blank, call `retrieve_memory_records` (same client call shape as `:909`: `searchCriteria={"searchQuery": query, "topK": 50}`, per reflections namespace) → rank map `{record_id: rank}` from result order; final order = RRF over (wilson_rank, relevance_rank) with constant 60 (`score = 1/(60+wr) + 1/(60+rr)`, missing leg contributes nothing), ties broken by wilson order; AWS error or empty result ⇒ pure Wilson order (today's Task-4 behaviour). Blank/absent query ⇒ no Retrieve call.

- [ ] **Step 1: Failing tests** — stub `retrieve_memory_records` returning a semantically-relevant low-Wilson record first: assert it outranks a high-Wilson non-relevant one under `query`, and that order reverts to Wilson without `query`; error-degradation test (`side_effect=ClientError`-shaped exception ⇒ Wilson order, no raise); assert no Retrieve call when query blank.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/storage -q` — all pass.
- [ ] **Step 5:** Commit `feat(agentcore): query-conditioned retrieval via server-side semantic search`.

---

### Task 6: `relevance_ranks` protocol method + contextual gate parity

**Files:**
- Modify: `better_memory/storage/protocol.py` (new method), `better_memory/storage/sqlite.py` (impl), `better_memory/storage/agentcore.py` (impl), `better_memory/services/relevant.py` (agentcore branch), `better_memory/hooks/contextual_inject.py` (pass backend-scored path through)
- Test: `tests/services/test_relevant.py` (extend), `tests/storage/test_agentcore_unit.py` + `tests/storage/test_sqlite_backend.py` (method tests)

**Interfaces:**
- Produces (exact):

```python
def relevance_ranks(self, *, query: str, kinds: tuple[str, ...] = ("reflection", "semantic"),
                    top_k: int = 50) -> dict[tuple[str, str], int]
    # (kind, id) -> rank (0 best). Empty dict = no evidence / unavailable.
```

Sqlite impl: thin wrapper over the existing BM25 (`reflection_fts`) + vec legs, RRF-merged — implemented for protocol completeness; `retrieve_relevant`'s sqlite path is NOT rewired (keeps its in-function legs; zero behaviour change, pinned by existing tests). Agentcore impl: `retrieve_memory_records` per kind's namespace, rank by result order, best-effort empty on error. `retrieve_relevant`: when `conn is None` and the backend exposes `relevance_ranks`, the evidence gate becomes membership in the rank map (Wilson still ranks-not-qualifies; RRF fuses prior rank + relevance rank); keyword fallback applies only when the map comes back empty (AWS error) — semantics unchanged for sqlite callers.

- [ ] **Step 1: Failing tests** — relevant.py agentcore-mode test: stub backend with `relevance_ranks` returning the target pair ⇒ injected without token overlap (the DirectedEmbedder-style scenario, no embedder involved); empty map ⇒ keyword fallback still works (existing test extended); sqlite `relevance_ranks` unit test (seeded FTS row found); agentcore `relevance_ranks` unit test (stubbed Retrieve).
- [ ] **Step 2:** Run — FAIL (method missing).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/services/test_relevant.py tests/storage tests/hooks/test_contextual_inject.py -q` — all pass.
- [ ] **Step 5:** Commit `feat(contextual): backend relevance_ranks — semantic evidence gate on agentcore`.

---

### Task 7: Sweep + credit parity + receipt event

**Files:**
- Modify: `better_memory/storage/agentcore.py` (`apply_session_ratings` ~:1451-1540; `credit_one` ~:1355-1449; `_RATING_TO_COUNTER` usage), `better_memory/services/memory_rating.py` (export `_validate_evidence` + batch validation as reusable — rename to public `validate_evidence` / extract `validate_ratings_batch(ratings) -> list[tuple]` if needed, keeping sqlite callers working)
- Test: `tests/storage/test_agentcore_unit.py` (extend), new `tests/storage/test_agentcore_rating_loop.py` (e2e)

**Interfaces:**
- Produces: agentcore `apply_session_ratings`: (1) shared batch validation incl. evidence contract (identical errors); (2) per entry — `exposure_log.stamp` on `local_conn` (skip buckets `not_exposed`/`already_rated` real when ledger present; without ledger, today's behaviour); (3) counter push via existing credit machinery incl. `ignored` → `ignored_count`; (4) one best-effort `create_event` receipt after a successful sweep: `extractionMode="SKIP"`, `metadata={"type": {"stringValue": "ratings"}}` (shape per CreateEvent metadata — string entries), `sessionId` = resolved session, payload = one blob item containing the rated batch JSON (ids, classes, evidence). Event failure never affects the sweep result. `credit_one`: rejects `ignored` with the same ValueError text shape as sqlite; stamps the local row when present; evidence validated via the shared helper.

- [ ] **Step 1: Failing tests** — evidence rejection parity (shaped w/o evidence ⇒ ValueError, batch untouched); `ignored` on credit ⇒ ValueError; full sweep e2e (stubbed clients + real tmp ledger): seed exposures via backend, sweep a mixed batch ⇒ local rows stamped w/ evidence, `batch_update_memory_records` called per non-skip entry (inspect mock calls incl. an `ignored_count` bump), exactly one `create_event` with `extractionMode="SKIP"` and the batch JSON in payload; event `side_effect=Exception` ⇒ sweep still succeeds; skip buckets: unrated-only stamping (`already_rated` on second sweep).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement (shared validation import; keep `_RATING_TO_COUNTER` for the sweep path, remove `ignored` acceptance from the credit path only).
- [ ] **Step 4:** Run: `./.venv/Scripts/python.exe -m pytest tests/storage tests/mcp/test_rating_tools.py tests/services/test_rating_evidence.py -q` — all pass.
- [ ] **Step 5:** Commit `feat(agentcore): real rating sweep — local stamps, shared counters, receipt events`.

---

### Task 8: Docs, website, pyright, full suite

- [ ] **Step 1:** Update `website/agentcore-setup.md` (capability table: learning loop now backend-agnostic; what differs: synthesis/episodes/retention, evidence browsing local until the event read path; silent-metadata-drop callout with the memoryStrategyId/schema rule), `website/architecture.md` (rating-loop section: backend-agnostic), `protocol.py`/`agentcore.py` docstrings final pass, `docs/hooks-setup.md` if it states the old never-open-sqlite rule. Grep synonyms: `no-op`, `sqlite-only`, `not available in agentcore`, `always 0`.
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m pyright` → 0 errors.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m pytest tests -q --junitxml=suiteE.xml > suiteE.txt 2>&1` once; fix stragglers minimally; final full run; delete suiteE.* before staging.
- [ ] **Step 4:** Commit `docs: agentcore parity capability tables + drop-trap callout`.

---

### Task 9: PR, babysit, merge, deploy

- [ ] Push; `gh pr create` (body: spec link, the four moves, honest limits, stubbed-test coverage, and a **manual live-smoke checklist**: `better-memory agentcore smoke`; one real session with `BETTER_MEMORY_STORAGE_BACKEND=agentcore`; verify a sweep stamps local rows, bumps a counter via `get_memory_record`, and lands one `type=ratings` event via `list_events`; footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`).
- [ ] Babysit to green + zero threads → squash-merge, checkout main, pull.
- [ ] Deploy note: no env changes; restart picks up server code; live smoke is manual (needs AWS creds) — present the checklist to the user rather than running it unprompted.

---

## Self-review notes

- Spec §1→T1/T2, §2→T7, §3→T3/T4/T5, §4 contextual→T6, alignments→T3/T7/T8, non-goals honoured (no event read path, no episode parity, no race hardening).
- `relevance_ranks` signature identical in T6's protocol/sqlite/agentcore/consumer references.
- Sqlite-preservation pins named in every extraction task (T1 suites, T6 "not rewired").
- The hook-side `session_close` sweep copy deliberately untouched (T1 note) — it already reads the shared table; consolidation is future cleanup, not parity-critical.
