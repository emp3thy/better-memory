# Remove Ollama and Local Embeddings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the Ollama embedding subsystem and all local vector search so no better-memory process contacts Ollama or computes embeddings in any mode.

**Architecture:** Sever wiring first (nothing constructs an embedder), then strip services inside-out (every `sync_embedder`/`embedder` parameter defaults to `None`, so each strip is independently green), then delete the orphaned `embeddings/` package, drop the vec0 tables by migration, and sync docs. Retrieval collapses to FTS5/BM25 + keyword fallback (sqlite storage) and server-side `relevance_ranks` (agentcore storage).

**Tech Stack:** Python 3.12, sqlite3 + sqlite-vec (load retained this release), FTS5, pytest, Pyright.

**Spec:** `docs/superpowers/specs/2026-09-01-remove-ollama-embeddings-design.md`

## Global Constraints

- Full `pytest` suite AND Pyright green after every task — no task may end with a dangling import or parameter.
- `sqlite_vec.load()` in `better_memory/db/connection.py:39` is NOT removed (needed by migration 0015; dep removal is a follow-up release).
- Historical docs (`docs/superpowers/specs/*`, `plans/*`, `archive/*`) are never edited.
- Env vars `OLLAMA_HOST`, `EMBED_MODEL`, `BETTER_MEMORY_EMBEDDINGS_BACKEND`, `BETTER_MEMORY_EMBED_LOG` become silently unread — no deprecation shims, no warnings, no error paths.
- Commit after every task; conventional-commit style messages matching repo history.

## Guardrails (from better-memory reflections — surfaced before drafting)

- **[[website-readme-sync]]** (conf 0.95, evidence 7, used 27×): every `better_memory/` change syncs `website/configuration.md`, `website/architecture.md`, `website/index.md`, `website/mcp-tools.md`, `website/observation-lifecycle.md`, `README.md` in the same PR. Module-level docstrings that enumerate env vars are part of the code change (Task 3), NOT the docs task. No MCP tools are added/removed here, so tool-count strings should NOT change — verify, don't edit.
- **[[verify-red-is-red]]** (conf 0.75, evidence 2): Task 1's FTS regression test is EXPECTED GREEN immediately — it is a verification pin, not a red-green TDD cycle. If it is RED, stop: the spec's real concern (FTS gated on the old knob) is live, and un-gating becomes a new task inserted before Task 2.
- **[[planning-retrieval]]** (conf 0.9): satisfied — planning + implementation memories and the standards doc were retrieved before drafting this plan.

Dismissed as not applicable: freeze-localization logging (0.55 — no debugging task here), fail-fast ordering comments (0.55 — Task 4 preserves the existing rollback comment verbatim), Playwright CSS text-transform (0.8 — no UI restyling), TypeScript Partial<T> (0.6 — not TypeScript), mkstemp fd leak (0.6 — no temp files).

---

### Task 1: FTS-substrate verification + regression pin — **confidence 90%**

The spec's single real concern, done first because a failure changes the plan.

**Files:**
- Create: `tests/services/test_fts_substrate.py`
- Read (verify only): `better_memory/db/migrations/0002_episodic.sql`, `0014_semantic_embeddings.sql`, all other `db/migrations/*.sql`

**Interfaces:**
- Consumes: `ObservationService` (existing), `retrieve_relevant` from `better_memory/services/relevant.py` (existing).
- Produces: nothing new — a regression test later tasks must keep green.

- [ ] **Step 1: Verify FTS trigger DDL is unconditional**

Run: `grep -n -i "fts" better_memory/db/migrations/*.sql` and `grep -rn "embeddings_backend" better_memory/db/`
Expected: FTS virtual tables + triggers created unconditionally in migration SQL; zero hits for `embeddings_backend` under `db/`. If FTS creation is conditional anywhere, STOP and report — un-gating becomes a new task before Task 2.

- [ ] **Step 2: Write the regression test**

```python
"""Pins the FTS5/BM25 evidence leg as unconditional.

After the Ollama/vector removal, BM25 is the only local evidence leg for
sqlite-storage retrieval. This test constructs services with NO embedder
(the only construction mode after this branch) and proves observe -> FTS
row -> BM25-evidenced retrieval works end to end.
EXPECTED GREEN from the moment it is written (verification pin, not TDD red).
"""
from better_memory.db.connection import connect_memory  # adjust to the real helper used by sibling service tests
from better_memory.services.observation import ObservationService


def test_observe_populates_fts_without_embedder(tmp_path):
    conn = connect_memory(tmp_path / "memory.db")  # same fixture idiom as tests/services/test_observation.py
    svc = ObservationService(conn, episodes=None)   # no embedder argument
    svc.observe(content="growatt inverter polling uses Timespan.hour", project="p1")
    fts_rows = conn.execute(
        "SELECT count(*) FROM observations_fts WHERE observations_fts MATCH 'growatt'"
    ).fetchone()[0]
    assert fts_rows >= 1


def test_retrieve_relevant_returns_bm25_evidence_without_embedder(tmp_path):
    # Same substrate as above; retrieval must surface the item on BM25
    # evidence alone. Reuse the backend fixture from tests/services/test_relevant.py.
    conn = connect_memory(tmp_path / "memory.db")
    backend = make_sqlite_backend(conn)  # the fixture/helper test_relevant.py already uses
    svc = ObservationService(conn, episodes=None)
    svc.observe(content="growatt inverter polling uses Timespan.hour", project="p1")
    items = retrieve_relevant(
        backend, query="growatt inverter polling", project="p1",
        conn=conn, sync_embedder=None, max_items=5,
    )  # after Task 6 the sync_embedder argument disappears; drop it then
    assert any("growatt" in str(item).lower() for item in items)
```

The implementer MUST adapt fixture/table names to what `tests/services/test_observation.py` and `tests/services/test_relevant.py` actually use (e.g. the real FTS table name may be `observation_fts` or reflection-scoped) — the assertion shape (observe → FTS MATCH hit → retrieval hit with no embedder anywhere) is the requirement, not the literal identifiers above.

- [ ] **Step 3: Run the new tests**

Run: `pytest tests/services/test_fts_substrate.py -v`
Expected: PASS (green pin). If FAIL: stop the plan, report — spec's contingency activates.

- [ ] **Step 4: Full suite + Pyright**

Run: `pytest -q && pyright`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/services/test_fts_substrate.py
git commit -m "test: pin FTS5 substrate as unconditional local evidence leg"
```

---

### Task 2: Sever wiring — server, hook, UI, CLI — **confidence 93%**

After this task nothing in the codebase CONSTRUCTS an embedder; services still accept the (defaulted) parameters until Tasks 4–6.

**Files:**
- Modify: `better_memory/mcp/server.py` (delete lines at :64-65 imports, :96 `_probe_ollama`, :146 `ServerContext.embedder`, :173-189 construction, :182 probe call, :206/:223-224/:232/:235 args, :342-344 aclose)
- Modify: `better_memory/hooks/contextual_inject.py` (:33-34 imports, :147-149 build, :156 vec_floor arg)
- Modify: `better_memory/ui/app.py` (:18-19 imports, :40-49 `_build_sync_embedder`, :74/:90/:104 param plumbing)
- Delete: `better_memory/cli/backfill_embeddings.py`, `tests/cli/test_backfill_embeddings.py`
- Test: `tests/mcp/test_server_sqlite.py`, `tests/mcp/test_server_backend_dispatch.py`, `tests/mcp/test_server_integration.py`, `tests/ui/test_app.py`, `tests/hooks/test_contextual_inject.py`, `tests/e2e/test_tripwires.py` + `tests/e2e/test_sqlite_negative.py` + `tests/e2e/test_sqlite_journey.py` (drop OllamaEmbedder skip-markers / embedder-related env setup and comments)

**Interfaces:**
- Consumes: services' existing `embedder=None` / `sync_embedder=None` keyword defaults (verify each default exists before removing the argument at the call site; if any parameter is required, pass nothing and fix the signature in the same edit).
- Produces: `create_server_context()` (or the real factory name in `mcp/server.py`) builds all services with no embedder arguments; `ServerContext` has no `embedder` field; UI `create_app`/`app.py` entry has no `sync_embedder` parameter.

- [ ] **Step 1: Update wiring tests first**

In the five test files: delete tests asserting `OllamaEmbedder` is constructed under the ollama backend / probe behavior; update `SyncEmbedder`-tracking dispatch tests to assert services receive no embedder. Keep tests asserting sqlite-mode behavior (no embedder) — they describe the new universal reality; strip their backend-env setup.

- [ ] **Step 2: Run updated tests to verify they fail against current code**

Run: `pytest tests/mcp tests/ui tests/hooks -q`
Expected: failures where tests now assert no-embedder wiring (current code still builds embedders under default config).

- [ ] **Step 3: Apply the wiring deletions** (file list above, line anchors from the blast-radius map). Also delete the stale comment `better_memory/services/reflection.py:334` pointing at the backfill CLI.

- [ ] **Step 4: Full suite + Pyright**

Run: `pytest -q && pyright`
Expected: green. `tests/embeddings/*` still passes (package still exists, directly tested — untouched until Task 8).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: stop constructing embedders in server, hook, UI; delete backfill CLI"
```

---

### Task 3: Delete config knobs + env reads + module docstrings — **confidence 95%**

**Files:**
- Modify: `better_memory/config.py` (:14 docstring env list, :33-36 defaults, :223-228 fields `ollama_host`/`embed_model`/`embeddings_backend`, :236 `context_vec_floor`, :260-266 `_resolve_embeddings_backend`, :369-374/:382 resolution)
- Modify: `better_memory/_diag.py` (:11 comment, :53 `BETTER_MEMORY_EMBED_LOG` read), `better_memory/mcp/_util.py` (:37 comment)
- Test: `tests/test_config.py` (4 knob tests out), `tests/test_diag.py` (2), `tests/setup/test_engine.py` (1), `tests/e2e_meta/test_env_helper_contract.py`, `tests/e2e_meta/test_env_bleed.py` (drop the deleted vars from contract lists), plus any e2e fixture that sets `BETTER_MEMORY_EMBEDDINGS_BACKEND` in its environment setup (grep `tests/` for the var name)

**Interfaces:**
- Consumes: Task 2 (no remaining readers of the four fields — verify with `grep -rn "embeddings_backend\|ollama_host\|embed_model\|context_vec_floor" better_memory/` before editing; `services/relevant.py`'s `vec_floor` parameter keeps its plain default until Task 6).
- Produces: `Config` dataclass without the four fields — later tasks and all call sites rely on their absence.

- [ ] **Step 1: Update config/diag/setup/e2e-meta tests** to assert the fields and env reads are gone (attribute access raises `AttributeError`; env contract lists shrink).
- [ ] **Step 2: Run those tests — expect failures against current code.** `pytest tests/test_config.py tests/test_diag.py -q`
- [ ] **Step 3: Apply the deletions**, INCLUDING the `config.py` module docstring's env-var enumeration ([[website-readme-sync]]: docstrings are code, this task, not Task 10).
- [ ] **Step 4: Full suite + Pyright green.** `pytest -q && pyright`
- [ ] **Step 5: Commit** — `git commit -am "refactor: remove ollama/embeddings/vec-floor config knobs and EMBED_LOG diag"`

---

### Task 4: Strip `services/semantic.py` — **confidence 95%**

**Files:**
- Modify: `better_memory/services/semantic.py` (:21 import, ctor `sync_embedder` param, :66 `_store_embedding`, :94/:121/:199 embed calls)
- Delete: `tests/services/test_semantic_embedding_write.py`
- Test: remaining semantic service tests (drop fake-embedder fixtures)

**Interfaces:**
- Consumes: Task 2 (no caller passes `sync_embedder`).
- Produces: `SemanticMemoryService(conn)` — no embedder parameter. `update_text(*, id, content)` behavior: plain UPDATE; `rowcount == 0` → rollback + `ValueError(f"semantic memory not found: {id}")`.

- [ ] **Step 1: Delete `test_semantic_embedding_write.py`; update remaining semantic tests** to construct the service without embedder and assert `update_text` on a missing id raises `ValueError` (rowcount path).
- [ ] **Step 2: Run — expect failures** (ctor signature still has the param, SELECT guard still present). `pytest tests/services -k semantic -q`
- [ ] **Step 3: Strip the service.** `update_text` becomes:

```python
def update_text(self, *, id: str, content: str) -> None:
    if not content.strip():
        raise ValueError("content must not be empty")
    now = self._clock().isoformat()
    cur = self._conn.execute(
        "UPDATE semantic_memories SET content = ?, updated_at = ? "
        "WHERE id = ?",
        (content, now, id),
    )
    if cur.rowcount == 0:
        # Roll back the implicit BEGIN so we don't strand the WAL
        # write lock for callers sharing this connection. Mirrors
        # ObservationService.set_outcome (better_memory/services/observation.py:435).
        self._conn.rollback()
        raise ValueError(f"semantic memory not found: {id}")
    self._conn.commit()
```

(The PR #125 `SELECT 1` fast-fail and the pre-embed comments go — they existed only to protect a now-deleted embed. The rollback comment is preserved near-verbatim; it guards the WAL lock, which is still real.)

- [ ] **Step 4: Full suite + Pyright green.**
- [ ] **Step 5: Commit** — `git commit -am "refactor: remove embedding writes from SemanticMemoryService"`

---

### Task 5: Strip `services/reflection.py` — **confidence 92%**

Largest single-file strip; the #97 `embed_tasks` machinery makes it fiddly. Mitigation folded in: Step 1 maps every deletion target by grep before editing.

**Files:**
- Modify: `better_memory/services/reflection.py` (:34 sqlite_vec import, ctor `sync_embedder`, :379 `_store_embedding`, :856/:1032/:1828 embed calls, :1346 batch embed, :1367-1397 `_heal_missing_embeddings`, :1398+ `_vec_ranks`, :1605 query embed, the `embed_tasks` parameters on `_apply_new`/`_apply_augment` and their collect-embed-after-commit caller logic in `apply_decision`)
- Delete: `tests/services/test_reflection_embedding_write.py`
- Test: `tests/services/test_reflection.py` (drop embedder fixtures/params)

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces: `ReflectionSynthesisService(conn)` — no embedder parameter; `_apply_new(actions, *, project)` / `_apply_augment(...)` without `embed_tasks`; retrieval ranks from BM25 + prior only (vec rank leg gone).

- [ ] **Step 1: Map deletions** — `grep -n "sync_embedder\|embed_tasks\|_store_embedding\|_heal_missing\|_vec_ranks\|sqlite_vec\|embed_text\|embed_batch" better_memory/services/reflection.py` and list every hit against the anchors above; anything unaccounted for gets read before deletion.
- [ ] **Step 2: Delete `test_reflection_embedding_write.py`; update `test_reflection.py`** (no embedder fixtures; synthesis asserts row writes only). Run: expect failures against current signatures.
- [ ] **Step 3: Strip the service.** `apply_decision` returns to: SAVEPOINT → `_apply_new`/`_apply_augment` write rows → commit. No post-commit embed pass.
- [ ] **Step 4: Full suite + Pyright green.**
- [ ] **Step 5: Commit** — `git commit -am "refactor: remove embedding writes, heal, and vec ranks from reflection synthesis"`

---

### Task 6: Strip `services/relevant.py` + `services/observation.py` — **confidence 93%**

**Files:**
- Modify: `better_memory/services/relevant.py` (:42 import, :102-150 `_vec_qualifiers`, :233 qvec, :246-253 vec legs, `sync_embedder`/`vec_floor` params of `retrieve_relevant`)
- Modify: `better_memory/services/observation.py` (:62 import, ctor `embedder` param, :199-region vec0 write path)
- Delete: `tests/services/test_vec_fusion.py`, `tests/services/_embedding_fakes.py`
- Test: `tests/services/test_relevant.py`, `tests/services/test_observation.py`, `tests/services/test_observation_sqlite.py`, `tests/hooks/test_contextual_inject.py`

**Interfaces:**
- Consumes: Task 2 (hook already passes no `sync_embedder`/`vec_floor`).
- Produces: `retrieve_relevant(backend, *, query, project, conn, max_items, include_neutral, now)` — exact surviving signature; evidence gate = BM25 match OR agentcore `relevance_ranks` membership OR keyword fallback when `conn is None` for sqlite... **No** — keyword fallback fires when FTS is structurally unavailable, agentcore branch unchanged: preserve the existing gate semantics minus the vec leg, and keep the docstring's agentcore paragraph intact. `ObservationService(conn, episodes=...)` — no embedder parameter.

- [ ] **Step 1: Update tests** — delete `test_vec_fusion.py` + `_embedding_fakes.py`; in `test_relevant.py` remove vec-leg cases and embedder fixtures, keep BM25/keyword/agentcore gate cases; `test_observation*` drop embedder args and the vec0-skip marker test. Run: expect signature failures.
- [ ] **Step 2: Strip `relevant.py`** — remove `_vec_qualifiers`, `qvec`, both `vec_r`/`vec_s` legs and their RRF inputs; `fts_unavailable = conn is None` logic and the agentcore `relevance_ranks` branch stay byte-identical.
- [ ] **Step 3: Strip `observation.py`** — remove `embedder` param and the vec0 write; the `# When embedder is None ... skip the vec0 path` comment becomes dead and goes with it.
- [ ] **Step 4: Full suite + Pyright green.**
- [ ] **Step 5: Commit** — `git commit -am "refactor: remove vector legs from retrieval and observation write path"`

---

### Task 7: Collapse `search/hybrid.py` + `storage/sqlite.py` — **confidence 92%**

**Files:**
- Modify: `better_memory/search/hybrid.py` (:31 import, :83-88 signature/`second_source`, :128 vec0 check, :151 kNN, :271 serialize)
- Modify: `better_memory/storage/sqlite.py` (:339 query embed in `search_hybrid`, ctor `sync_embedder` if present)
- Test: `tests/search/test_hybrid.py`, `tests/storage/test_sqlite_backend.py`, `tests/storage/test_factory.py`

**Interfaces:**
- Consumes: Tasks 2, 6.
- Produces: `hybrid_search(conn, query, ...)` FTS5-only (no `query_vector`/`second_source` params). Decision rule, pre-made: KEEP the module and function name (callers + docs reference it); fold into the caller ONLY if the reviewer flags the survivor as trivial.

- [ ] **Step 1: Update `test_hybrid.py`** — delete vector-leg cases (~half of 18), keep BM25 ranking/tie cases; update backend/factory tests to embedder-less construction. Run: expect failures.
- [ ] **Step 2: Strip both files.**
- [ ] **Step 3: Full suite + Pyright green.**
- [ ] **Step 4: Commit** — `git commit -am "refactor: collapse hybrid search to FTS5-only"`

---

### Task 8: Delete the `embeddings/` package — **confidence 96%**

**Files:**
- Delete: `better_memory/embeddings/` (ollama.py, sync_embed.py, `__init__.py`), `tests/embeddings/` (3 files)
- Verify: `better_memory/async_bridge.py` remaining consumers

**Interfaces:**
- Consumes: Tasks 2–7 (zero imports remain — verified, not assumed).
- Produces: issue-#96 verdict for the PR description.

- [ ] **Step 1: Verify orphanhood** — `grep -rn "embeddings\.\|OllamaEmbedder\|SyncEmbedder\|EmbeddingError" better_memory/ tests/ --include="*.py"` → only hits inside the two directories being deleted.
- [ ] **Step 2: #96 verdict** — `grep -rn "run_async_in_worker\|async_bridge" better_memory/ --include="*.py"` excluding `embeddings/`. Zero non-embeddings callers → `async_bridge.py` is dead too: delete it + its tests, PR claims `Fixes #96`. Any other caller → keep `async_bridge.py`, #96 stays open, note the narrowing for the PR description.
- [ ] **Step 3: Delete the directories** (+ `async_bridge.py` per verdict).
- [ ] **Step 4: Full suite + Pyright green.**
- [ ] **Step 5: Commit** — `git commit -am "refactor: delete ollama embeddings package"` (append `\n\nRemoves the SyncEmbedder worker bridge behind #96.` if verdict positive).

---

### Task 9: Migration 0015 — drop vec0 tables — **confidence 92%**

**Files:**
- Create: `better_memory/db/migrations/0015_drop_vec_tables.sql`
- Test: `tests/db/test_schema.py` (:59 FTS+vec assertion, :1057-1058 semantic_embeddings test), new migration test in the same file

**Interfaces:**
- Consumes: `sqlite_vec.load()` still active in `db/connection.py` (Global Constraints).
- Produces: post-migration schema without the three vec tables — Task 1's pin still green (FTS untouched).

- [ ] **Step 1: Trigger sweep** — `grep -n -i "embeddings" better_memory/db/migrations/0*.sql` → confirm no trigger/view references the vec tables (investigator found none); add `DROP TRIGGER IF EXISTS` lines only for actual hits.
- [ ] **Step 2: Write the migration test** (schema-test idiom of `test_schema.py`): build a DB through 0014, insert a row into each vec table, apply 0015 → tables absent from `sqlite_master`; apply 0015 again on a fresh connection → no error (idempotent). Update :59/:1057 tests to assert absence. Run: expect FAIL (no 0015 yet).
- [ ] **Step 3: Write the migration:**

```sql
-- 0015: local vector search removed; vec0 tables dropped.
-- sqlite-vec module is still loaded by db/connection.py this release,
-- which is what makes these DROPs legal. IF EXISTS keeps the script
-- idempotent, so the non-atomic executescript hazard (#27) cannot
-- strand it half-applied in a harmful state.
DROP TABLE IF EXISTS observation_embeddings;
DROP TABLE IF EXISTS reflection_embeddings;
DROP TABLE IF EXISTS semantic_embeddings;
```

- [ ] **Step 4: Full suite + Pyright green.**
- [ ] **Step 5: Commit** — `git commit -am "feat(db): migration 0015 drops vec0 embedding tables"`

---

### Task 10: Docs + website sync — **confidence 94%**

Guardrail [[website-readme-sync]] (conf 0.95): this task is mandatory, same PR.

**Files:**
- Modify: `website/configuration.md` (env table: remove `OLLAMA_HOST`, `EMBED_MODEL`, `BETTER_MEMORY_EMBEDDINGS_BACKEND`, `BETTER_MEMORY_EMBED_LOG` rows), `website/architecture.md` (retrieval + synthesis prose: no embed step, evidence legs per storage mode), `website/index.md` (tagline/showcase if it claims vector search or ollama), `website/observation-lifecycle.md` (if synthesis prose mentions embedding), `website/mcp-tools.md` (only if tool descriptions mention vectors — tool COUNT must not change), `README.md` (prerequisites: drop Ollama/nomic-embed-text; troubleshooting: drop ollama entries), living `docs/agentcore-ui/agentcore-mapping.md:53` + `docs/agentcore-ui/write-spec.md` re-embedding mentions.
- Never touch: `docs/superpowers/**`.

- [ ] **Step 1: Grep the living docs** — `grep -rn -i "ollama\|embed\|vec0\|nomic" website/ README.md docs/agentcore-ui/ --include="*.md"` and triage every hit: update, or record "historical, untouched".
- [ ] **Step 2: Apply edits.** Per guardrail: every factual token in REWRITTEN lines verified against the post-branch source (function names, env vars, table names) — no carrying forward.
- [ ] **Step 3: Run `graphify update .`** (project CLAUDE.md requirement) so the graph drops the deleted subsystem.
- [ ] **Step 4: Commit** — `git commit -am "docs: remove ollama/embeddings from website, README, living agentcore docs"`

---

### Task 11: Final sweep + follow-up issue — **confidence 95%**

**Files:** none new (verification + GitHub).

- [ ] **Step 1: Remnant grep** — `grep -rn -i "ollama\|sync_embed\|embed_model\|embeddings_backend\|vec_floor\|_vec_" better_memory/ tests/ --include="*.py"` → expected survivors ONLY: `sqlite_vec` import/load in `db/connection.py`, migration SQL files, migration-0015 test. Anything else is a missed deletion — fix it here.
- [ ] **Step 2: Full suite + Pyright, one last time.** `pytest -q && pyright`
- [ ] **Step 3: File the follow-up issue** — `gh issue create -R emp3thy/better-memory --title "Remove sqlite-vec dependency and connection load" --body "..."`: body notes it requires all live DBs to have run migration 0015 (drop the `sqlite_vec.load()` at `db/connection.py:39`, the import, the pyproject dependency, and the two connection tests that cover the load).
- [ ] **Step 4: Commit any Step-1 fixes**, then hand off to the whole-branch review per subagent-driven-development (most capable model), then `superpowers:finishing-a-development-branch`. PR: squash-merge, description lists closed issues (`Fixes #96` only per Task 8's verdict) and the docs-sync note per [[website-readme-sync]].
