# RATE_MEMORIES Display Snapshots + Terse Directive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop-hook RATE_MEMORIES directive shows each pending memory's title instead of an opaque id, and shrinks to a minimal format; `memory.list_session_exposures` shows titles for agentcore-backed memories too.

**Architecture:** Snapshot the memory's display text into a new nullable `display` column on `session_memory_exposure` at exposure-record time (every write site holds the full record then). Read path selects `COALESCE(e.display, r.title, s.content)` so pre-migration local rows keep resolving via the join. The Stop hook directive drops all rule text that lives in the `rate-session-memories` skill.

**Tech Stack:** Python 3.12, sqlite3, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-06-rate-directive-display-design.md`

## Guardrails (from project memory / standards)

- **[[stop-hook-sync]]** (decision doc `docs/decisions/stop-hook-must-be-sync.md`): the Stop hook is synchronous and blocking. NO network calls, NO AWS SDK imports in `session_close.py`. This plan reads only the local ledger. (conf: accepted ADR)
- **[[docs-in-sync]]** (reflection mem-f3ce58e6, conf 1.0, used 21x): sweep `website/mcp-tools.md`, `README.md`, and module docstrings in the same PR; state "docs unaffected" explicitly if so. Task 6.
- **[[hooks-never-fail]]** (repo convention, session_close.py docstring): hooks always exit 0; exposure writes are best-effort and must never block retrieve/inject. Preserve every existing try/except guard.
- **[[ruff-py312]]** (standards doc): `from datetime import UTC`, no `(str, Enum)`, run `uv run ruff check` on all copied code before commit.
- Dismissed: visualiser/plan-render reflections (process, not code); worktree-on-Windows reflection (no worktree used); htmx/TypeScript/tempfile reflections (not touched by this plan).

## Global Constraints

- The Stop hook (`better_memory/hooks/session_close.py`) keeps its standalone inline SQL copy — do NOT import `better_memory.services.exposure_log` from it (no service-layer dependency, per the comment at session_close.py:69-72).
- `exposure_log` functions never call `conn.commit()` — callers own the transaction (module docstring contract).
- Exposure-ledger semantics are unchanged: at most one row per (session, kind, id), first source wins, dedup beats exploration tag.
- Display snapshots: truncate to 120 chars at write time; directive renders at most 80 chars per line (existing `TRUNC = 80`).
- The two inline sqlite exposure INSERTs in `services/reflection.py:1634` and `services/semantic.py:349` are intentionally NOT changed: they only ever write local-table ids, which the `r.title`/`s.content` join fallback resolves live. (Spec bullet listing them as snapshot writers is superseded by this — recorded here deliberately.)
- Test suite runner: `uv run pytest`. Lint: `uv run ruff check .`.

---

### Task 1: Migration — `display` column on session_memory_exposure

**Files:**
- Create: `better_memory/db/migrations/0017_exposure_display.sql`
- Test: `tests/services/test_exposure_log.py` (new test in existing file — its `conn` fixture already applies migrations)

**Interfaces:**
- Produces: `session_memory_exposure.display TEXT NULL` — Tasks 2-5 depend on the column existing.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/test_exposure_log.py` (top-level, after existing classes; reuse the module's `conn` fixture):

```python
class TestDisplayColumn:
    def test_session_memory_exposure_has_display_column(self, conn):
        cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            )
        }
        assert "display" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_exposure_log.py::TestDisplayColumn -v`
Expected: FAIL — `assert 'display' in cols` (column absent)

- [ ] **Step 3: Write the migration**

Create `better_memory/db/migrations/0017_exposure_display.sql`:

```sql
-- Display-text snapshot captured at exposure-record time. Nullable: rows
-- written before this migration (and callers with no display in hand) leave
-- it NULL and the read path falls back to joining reflections.title /
-- semantic_memories.content — which only resolves local-table ids, not
-- agentcore's AWS-minted mem-<uuid> ids. See
-- docs/superpowers/specs/2026-08-06-rate-directive-display-design.md.
ALTER TABLE session_memory_exposure ADD COLUMN display TEXT;
```

(Before writing, open `better_memory/db/migrations/0016_rating_evidence.sql` and copy its exact header/comment style if it differs from the above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_exposure_log.py::TestDisplayColumn -v`
Expected: PASS

- [ ] **Step 5: Run the full existing exposure/migration suites (regression)**

Run: `uv run pytest tests/services/test_exposure_log.py tests/db -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add better_memory/db/migrations/0017_exposure_display.sql tests/services/test_exposure_log.py
git commit -m "feat: add display snapshot column to session_memory_exposure"
```

**Confidence: 95%** — additive nullable column, migration framework verified (`schema.py` globs `NNNN_*.sql`, 0016 is the latest).

---

### Task 2: `exposure_log.record` triples + `list_unrated` COALESCE

**Files:**
- Modify: `better_memory/services/exposure_log.py`
- Test: `tests/services/test_exposure_log.py`

**Interfaces:**
- Consumes: `display` column from Task 1.
- Produces: `record(conn, *, session_id, items: list[tuple[str, str, str | None]], source, now, exploration_ids)` — items are `(kind, id, display)` triples, display may be None, truncated to 120 chars on write. `list_unrated` rows' `display` column becomes `COALESCE(e.display, r.title, s.content)`. Tasks 3-5 rely on exactly these signatures.

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/test_exposure_log.py`:

```python
class TestRecordDisplay:
    def test_display_persisted(self, conn):
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "mem-aws-1", "AWS Title")],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        row = conn.execute(
            "SELECT display FROM session_memory_exposure "
            "WHERE memory_id = 'mem-aws-1'"
        ).fetchone()
        assert row["display"] == "AWS Title"

    def test_display_truncated_to_120(self, conn):
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "mem-aws-2", "x" * 300)],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        row = conn.execute(
            "SELECT display FROM session_memory_exposure "
            "WHERE memory_id = 'mem-aws-2'"
        ).fetchone()
        assert row["display"] == "x" * 120

    def test_display_none_allowed(self, conn):
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "mem-aws-3", None)],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        row = conn.execute(
            "SELECT display FROM session_memory_exposure "
            "WHERE memory_id = 'mem-aws-3'"
        ).fetchone()
        assert row["display"] is None


class TestListUnratedDisplayCoalesce:
    def test_snapshot_beats_join(self, conn):
        conn.execute(
            "INSERT INTO reflections (id, title, project, phase, polarity,"
            " use_cases, hints, confidence, created_at, updated_at)"
            " VALUES ('r1', 'Live Title', 'p', 'general', 'do', 'uc', '[]',"
            " 0.5, '2026-01-01', '2026-01-01')"
        )
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "r1", "Snapshot Title")],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        rows = exposure_log.list_unrated(conn, session_id="s1")
        assert rows[0]["display"] == "Snapshot Title"

    def test_null_snapshot_falls_back_to_join(self, conn):
        conn.execute(
            "INSERT INTO reflections (id, title, project, phase, polarity,"
            " use_cases, hints, confidence, created_at, updated_at)"
            " VALUES ('r2', 'Live Title', 'p', 'general', 'do', 'uc', '[]',"
            " 0.5, '2026-01-01', '2026-01-01')"
        )
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "r2", None)],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        rows = exposure_log.list_unrated(conn, session_id="s1")
        assert rows[0]["display"] == "Live Title"

    def test_foreign_id_null_snapshot_yields_none(self, conn):
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "mem-aws-9", None)],
            source="retrieve", now="2026-08-06T10:00:00+00:00",
        )
        rows = exposure_log.list_unrated(conn, session_id="s1")
        assert rows[0]["display"] is None
```

- [ ] **Step 2: Update existing `record` call sites in this test file to triples**

Every existing `exposure_log.record(...)` call in `tests/services/test_exposure_log.py` passes 2-tuples like `("reflection", "r1")`. Change each to a 3-tuple appending `None`: `("reflection", "r1", None)`. Pure mechanical sweep of that one file.

- [ ] **Step 3: Run tests to verify the new ones fail**

Run: `uv run pytest tests/services/test_exposure_log.py -v`
Expected: `TestRecordDisplay` / `TestListUnratedDisplayCoalesce` FAIL (record raises or display column never written); pre-existing tests also FAIL on arity until Step 4.

- [ ] **Step 4: Implement in `better_memory/services/exposure_log.py`**

`record` — new signature and INSERT (dedup guard, no-commit contract, and exploration tagging unchanged):

```python
_DISPLAY_TRUNC = 120


def record(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    items: list[tuple[str, str, str | None]],
    source: str,
    now: str,
    exploration_ids: frozenset[str] = frozenset(),
) -> None:
    if not session_id or not items:
        return
    conn.executemany(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source, "
        " via_exploration, display) "
        "SELECT ?, ?, ?, ?, ?, ?, ? "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM session_memory_exposure "
        "  WHERE session_id = ? AND memory_kind = ? AND memory_id = ?)",
        [
            (
                session_id, kind, mid, now, source,
                1 if mid in exploration_ids else 0,
                (display[:_DISPLAY_TRUNC] if display else None),
                session_id, kind, mid,
            )
            for kind, mid, display in items
        ],
    )
```

Update the docstring: items are `(kind, id, display)` triples; display is a snapshot of the memory's title/content captured at exposure time (None when the caller has none), truncated to 120 chars — needed because agentcore ids don't exist in local content tables.

`list_unrated` — change only the display expression:

```sql
COALESCE(e.display, r.title, s.content) AS display
```

Update the module docstring's "lifted verbatim" paragraph to note the display extension (this module is no longer byte-identical to the original inline SQL).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_exposure_log.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/exposure_log.py tests/services/test_exposure_log.py
git commit -m "feat: exposure_log takes (kind, id, display) triples; COALESCE snapshot on read"
```

**Confidence: 95%** — single module, contract pinned by its own test suite which this task updates.

---

### Task 3: Thread display through protocol + backends + hooks

**Files:**
- Modify: `better_memory/storage/protocol.py:377-383` (record_exposures signature + docstring)
- Modify: `better_memory/storage/sqlite.py:439-448`
- Modify: `better_memory/storage/agentcore.py` (`_record_retrieve_exposures` ~:352-385, `record_exposures` ~:2076-2114)
- Modify: `better_memory/services/session_bootstrap.py` (`record_exposures` :146-177, `_record_exposure` :179-197, `bootstrap` deferred :285-289 and non-deferred :357-361)
- Modify: `better_memory/hooks/contextual_inject.py:167-173`
- Test: `tests/services/test_exposure_dedup.py`, `tests/services/test_exposure_tracking.py`, `tests/storage/test_agentcore_rating_loop.py`, `tests/storage/test_agentcore_unit.py`, `tests/integration/test_contextual_rating_e2e.py`, `tests/hooks/test_contextual_inject.py`, `tests/hooks/test_session_bootstrap.py`

**Interfaces:**
- Consumes: Task 2's triple-taking `exposure_log.record`.
- Produces: `StorageBackend.record_exposures(*, session_id: str, items: list[tuple[str, str, str | None]], source: str)` — every implementer and caller uses `(kind, id, display)` triples. No other Protocol methods change.

- [ ] **Step 1: Write the failing test (new behaviour: agentcore retrieve snapshots titles)**

Add to `tests/storage/test_agentcore_unit.py`, near the existing `record_exposures` tests (~:2740; reuse that test's `backend_with_local_conn` fixture and follow its call style):

```python
def test_record_exposures_persists_display(backend_with_local_conn):
    backend, conn = backend_with_local_conn
    backend.record_exposures(
        session_id="s-disp",
        items=[("reflection", "mem-aws-1", "AWS Reflection Title")],
        source="contextual",
    )
    row = conn.execute(
        "SELECT display FROM session_memory_exposure "
        "WHERE session_id = 's-disp' AND memory_id = 'mem-aws-1'"
    ).fetchone()
    assert row["display"] == "AWS Reflection Title"
```

(If `backend_with_local_conn` unpacks differently, mirror the fixture usage of the test at :2740 exactly.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/storage/test_agentcore_unit.py::test_record_exposures_persists_display -v`
Expected: FAIL (TypeError on tuple arity, or display NULL)

- [ ] **Step 3: Update production code — every site below, in one pass**

1. `storage/protocol.py:381`: `items: list[tuple[str, str, str | None]]`; docstring: "Record (kind, id, display) memory exposures for later rating. display is a snapshot of the memory's title/content at exposure time (None when unavailable); it makes agentcore-id exposures renderable without any local content row."
2. `storage/sqlite.py:443`: same annotation; body unchanged (pass-through).
3. `storage/agentcore.py:2080`: same annotation; body unchanged (items flow into `exposure_log.record` verbatim).
4. `storage/agentcore.py` `_record_retrieve_exposures`: replace the `all_ids` list-comprehension + items construction:

```python
        all_items = [
            ("reflection", r["id"], r.get("title"))
            for bucket in buckets.values() for r in bucket
        ]
        if not all_items:
            return
        ...
            exposure_log.record(
                self._local_conn,
                session_id=sid,
                items=all_items,
                source="retrieve",
                now=datetime.now(UTC).isoformat(),
                exploration_ids=frozenset(exploration_ids),
            )
```

(Parsed records carry `"title"` — set in `_parse_reflection_record` at agentcore.py:953.)
5. `services/session_bootstrap.py:150`: `items: list[tuple[str, str, str | None]]` (docstring: "(kind, id, display) triples"); body unchanged (delegates to `exposure_log.record`).
6. `services/session_bootstrap.py` `_record_exposure`: change signature to take display maps and build triples:

```python
    def _record_exposure(
        self,
        *,
        session_id: str,
        reflection_ids: list[str],
        semantic_ids: list[str],
        reflection_display: dict[str, str | None],
        semantic_display: dict[str, str | None],
    ) -> None:
        self.record_exposures(
            session_id=session_id,
            items=[("reflection", rid, reflection_display.get(rid))
                   for rid in reflection_ids]
            + [("semantic", sid, semantic_display.get(sid))
               for sid in semantic_ids],
            source="bootstrap",
        )
```

Callers build the maps from objects already in scope:
   - Non-deferred (`bootstrap`, ~:357): `reflection_display={r["id"]: r.get("title") for _, r in flat_reflections}`, `semantic_display={m.id: m.content for m in semantic}`.
   - Deferred (~:285): `reflection_display={}`, `semantic_display={m.id: m.content for m in general_only}`.
7. `hooks/contextual_inject.py:167`: `survivors = [(m.kind, m.id, m.text) for m in items]` — but note `seen.mark_seen(survivors)` at :179 and `seen.filter_unseen(pairs, ...)` operate on (kind, id) pairs. Keep the pair list for the seen-store and build a separate triple list for `record_exposures`:

```python
                    survivors = [(m.kind, m.id) for m in items]
                    exposure_items = [(m.kind, m.id, m.text) for m in items]
                    ...
                        backend.record_exposures(
                            session_id=session_id,
                            items=exposure_items,
                            source="contextual",
                        )
                    ...
                    seen.mark_seen(survivors)
```

- [ ] **Step 4: Sweep remaining 2-tuple call sites**

Run: `grep -rn "record_exposures(\|exposure_log.record(" better_memory/ tests/`
Every call passing 2-tuples gets `None` appended (tests) or a real display (production — but after Step 3 there should be NO production 2-tuple sites left; if grep finds one, fix it with a real display, not None). Files known to need the mechanical test sweep: `tests/services/test_exposure_dedup.py`, `tests/services/test_exposure_tracking.py`, `tests/storage/test_agentcore_rating_loop.py`, `tests/storage/test_agentcore_unit.py`, `tests/integration/test_contextual_rating_e2e.py`, `tests/hooks/test_contextual_inject.py` (its fake `record_exposures` at :305 forwards to `exposure_log.record` — forward the triples), `tests/hooks/test_session_bootstrap.py:428`.

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/services tests/storage tests/hooks tests/integration -v`
Expected: all PASS (including Step 1's new test)

- [ ] **Step 6: Type-check and lint**

Run: `uv run ruff check . && uv run mypy better_memory` (skip mypy if the repo has no mypy config — check `pyproject.toml`; ruff is mandatory)
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add better_memory/storage/protocol.py better_memory/storage/sqlite.py better_memory/storage/agentcore.py better_memory/services/session_bootstrap.py better_memory/hooks/contextual_inject.py tests/
git commit -m "feat: thread display snapshots through record_exposures to all write sites"
```

**Confidence: 91%** — all production call sites read and enumerated above; residual risk is test-fixture shapes in the agentcore suites, contained by the Step 4 grep + Step 5 suite run.

---

### Task 4: Terse directive + inline COALESCE in the Stop hook

**Files:**
- Modify: `better_memory/hooks/session_close.py:73-134` (`_emit_rating_directive_if_unrated`)
- Test: `tests/hooks/test_session_close_rating_directive.py`

**Interfaces:**
- Consumes: `display` column (Task 1). No dependency on Tasks 2-3 (inline SQL copy).
- Produces: directive format below. `reason` string and block-payload shape unchanged.

Directive format (exact):

```
RATE_MEMORIES: {total} unrated. Invoke skill `rate-session-memories`.
Evidence line first; none possible = `ignored`.
Reflections ({n}):
- {memory_id} [{source}] {display-or-empty, max 80 chars}
Semantic ({m}):
- {memory_id} [{source}] {display-or-empty, max 80 chars}
```

Rules: a bucket with zero entries is omitted entirely (no "(none)" placeholder). A NULL display renders the line as `- {id} [{source}]` with no trailing space. The 8 KB cap and its truncation suffix (`(list truncated; call memory.list_session_exposures for the full set)`) are kept verbatim.

- [ ] **Step 1: Update/replace tests**

In `tests/hooks/test_session_close_rating_directive.py`:

1. DELETE `test_directive_lists_overlooked_class` (class list now lives only in the skill).
2. REPLACE the body of `test_directive_requires_evidence_first` with:

```python
    def test_directive_keeps_one_rule_line(self, tmp_path, tmp_memory_db):
        _seed_unrated_exposure(tmp_memory_db, "S1")
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Evidence line first; none possible = `ignored`." in directive
        assert "rate-session-memories" in directive
        # Rules that now live only in the skill must NOT be restated.
        assert "Non-ignored ratings" not in directive
        assert "overlooked" not in directive
```

3. REPLACE `test_directive_shows_source_labels_and_counts` with:

```python
    def test_directive_shows_source_labels_without_counts_line(
        self, tmp_path, tmp_memory_db,
    ):
        _seed_unrated_exposure(tmp_memory_db, "S1", source="bootstrap")
        _seed_semantic_exposure(tmp_memory_db, "S1", source="contextual")
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "[bootstrap]" in directive
        assert "[contextual]" in directive
        assert "sources:" not in directive
        assert "My Title" in directive
        assert "My Semantic Fact" in directive
```

4. ADD two tests:

```python
    def test_display_snapshot_shown_for_foreign_id(
        self, tmp_path, tmp_memory_db,
    ):
        """An agentcore-style exposure (mem-<uuid> id, NO local content row)
        renders its snapshotted display text."""
        c = connect(tmp_memory_db)
        apply_migrations(c)
        c.execute(
            """INSERT INTO session_memory_exposure
               (session_id, memory_kind, memory_id, exposed_at, source, display)
               VALUES ('S1', 'reflection', 'mem-abc-123',
                       '2026-08-06T10:00:00+00:00', 'retrieve',
                       'Snapshotted AWS Title')"""
        )
        c.commit()
        c.close()
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Snapshotted AWS Title" in directive

    def test_empty_semantic_bucket_omitted(self, tmp_path, tmp_memory_db):
        _seed_unrated_exposure(tmp_memory_db, "S1")  # reflection only
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Semantic" not in directive
        assert "(none)" not in directive
```

5. KEEP `test_non_empty_unrated_emits_decision_block`, `test_multi_row_exposure_dedupes_in_directive` (its `directive.count("- r-dup [") == 1` assertion is format-compatible), `test_empty_unrated_writes_marker_no_directive`, `test_stop_hook_active_reentry_writes_marker_and_no_directive`, `test_db_error_falls_back_to_marker` — unchanged.

- [ ] **Step 2: Run tests to verify new/changed ones fail**

Run: `uv run pytest tests/hooks/test_session_close_rating_directive.py -v`
Expected: the four new/replaced tests FAIL against the old directive; kept tests PASS.

- [ ] **Step 3: Implement in `session_close.py`**

In `_emit_rating_directive_if_unrated`:

1. Inline query: change the display expression to `COALESCE(e.display, r.title, s.content) AS display` (keep everything else, including the standalone-copy comment — update that comment to mention the display column).
2. Replace the line/directive construction (drop `source_counts` entirely):

```python
        TRUNC = 80
        CAP_BYTES = 8 * 1024
        refl_lines = []
        sem_lines = []
        for r in rows:
            display = (r["display"] or "")[:TRUNC]
            source = r["source"] or "bootstrap"
            line = f"- {r['memory_id']} [{source}] {display}".rstrip()
            if r["memory_kind"] == "reflection":
                refl_lines.append(line)
            else:
                sem_lines.append(line)

        sections = [
            f"RATE_MEMORIES: {len(rows)} unrated. "
            "Invoke skill `rate-session-memories`.",
            "Evidence line first; none possible = `ignored`.",
        ]
        if refl_lines:
            sections.append(f"Reflections ({len(refl_lines)}):")
            sections.extend(refl_lines)
        if sem_lines:
            sections.append(f"Semantic ({len(sem_lines)}):")
            sections.extend(sem_lines)
        directive = "\n".join(sections)
```

3. Keep the `CAP_BYTES` truncation block and the `payload` dict (reason string included) byte-identical to today.

- [ ] **Step 4: Run the hook suite**

Run: `uv run pytest tests/hooks/test_session_close_rating_directive.py -v`
Expected: all PASS

- [ ] **Step 5: Run the e2e journey (pinned substrings: `RATE_MEMORIES` in reason, sem_id + `rate-session-memories` in directive — all survive)**

Run: `uv run pytest tests/e2e/test_sqlite_journey.py -v`
Expected: PASS. If any assertion trips on directive text, update it to the new format — but the substrings verified at plan time all survive.

- [ ] **Step 6: Commit**

```bash
git add better_memory/hooks/session_close.py tests/hooks/test_session_close_rating_directive.py
git commit -m "feat: terse RATE_MEMORIES directive with display snapshots"
```

**Confidence: 93%** — directive is a pure string format change behind tests updated in the same task; inline SQL mirrors Task 2's verified expression.

---

### Task 5: Full suite + lint gate

**Files:** none new.

- [ ] **Step 1: Run everything**

Run: `uv run pytest`
Expected: all PASS. Fix any straggler (most likely: a test asserting old directive text or 2-tuple exposure items missed by Task 3's grep).

- [ ] **Step 2: Lint**

Run: `uv run ruff check .`
Expected: clean

- [ ] **Step 3: Commit only if fixes were needed**

```bash
git add -u
git commit -m "test: sweep stragglers for display-snapshot exposure items"
```

**Confidence: 95%**

---

### Task 6: Docs sweep

**Files:**
- Modify (check each; edit only where stale): `website/mcp-tools.md`, `README.md`, `website/architecture.md`, `website/observation-lifecycle.md`

- [ ] **Step 1: Sweep for stale claims**

Run: `grep -rn "list_session_exposures\|RATE_MEMORIES\|session_memory_exposure" website/ README.md docs/decisions/`
For each hit, verify the prose against the new behaviour:
- `website/mcp-tools.md`: if it documents `list_session_exposures`'s `title`/`content` fields as join-derived or nullable-only-for-agentcore, update to "display snapshot captured at exposure time; falls back to live title/content for local rows".
- `docs/decisions/stop-hook-must-be-sync.md` quotes only the `reason` string — unchanged, leave it.
- Any doc quoting the old directive body verbatim: update to the new format.

- [ ] **Step 2: Commit (or record "docs unaffected")**

If edits were made:

```bash
git add website/ README.md
git commit -m "docs: display-snapshot exposure ledger + terse RATE_MEMORIES directive"
```

If no edits were needed, note "docs unaffected" in the eventual PR description — do not skip the grep.

**Confidence: 95%**
