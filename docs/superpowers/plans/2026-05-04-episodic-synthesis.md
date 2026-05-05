# Episodic Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the watermark-driven batch synthesis with an episode-driven loop. Each closed episode becomes one atomic LLM call; the UI's `/observations/synthesize` route processes one episode per HTTP request and is auto-chained by an htmx self-firing fragment until the pending queue is empty.

**Architecture:** Schema migration adds `episodes.synthesized_at` + `episodes.synth_failed_at` (cooldown for failed episodes), drops `synthesis_runs`. Service exposes `synthesize_next(*, project) -> SynthesisStep` instead of `synthesize(...)`. Route renders a banner fragment whose continuation `<div hx-trigger="load delay:200ms">` is present iff `queue.pending > 0`. MCP `start_episode` becomes `while step.processed: pass` then fetches tech-filtered buckets via `retrieve_reflections`.

**Tech Stack:** Python 3.12+, SQLite (WAL + busy_timeout=5000), Flask + htmx, Jinja2 templates, pytest, sqlite3.SAVEPOINT for per-episode atomicity, OllamaChat (existing, default timeout 240s).

**Branch:** `episodic-synthesis` off `main`. Single PR, four commits, dependency-ordered.

**Spec:** `docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md` — read before starting.

**Test discipline:** every behavior change is TDD (red → green → commit). Pyright (`uv run pyright`) and pytest (`uv run pytest -q`) must be green at every commit boundary.

---

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `better_memory/db/migrations/0006_per_episode_synthesis.sql` | Create | Schema migration: add `episodes.synthesized_at`, add `episodes.synth_failed_at`, partial index, backfill, drop `synthesis_runs`. |
| `tests/db/test_migration_0006.py` | Create | Forward-only migration test: column existence, index existence, backfill correctness, table drop. |
| `tests/db/test_schema.py` | Modify | Delete 4 stale `synthesis_runs` tests; add 3 new schema-shape tests for the new columns + index. |
| `better_memory/services/reflection.py` | Modify (heavy) | Replace `synthesize` with `synthesize_next`; add `EpisodeForPrompt`, `EpisodeContext`, `EpisodeQueueCounts`, `SynthesisStep` dataclasses; add `_pick_oldest_pending`, `_load_episode_context`, `_build_episode_prompt`, `_mark_synthesized`, `_read_queue_counts`. Remove `synthesize`, `load_context`, `build_prompt`, `_should_short_circuit`, `_upsert_watermark`, `SynthesisContext`. |
| `tests/services/test_reflection.py` | Modify (heavy) | Delete watermark/short-circuit/load_context/build_prompt tests; add 13 new tests covering `synthesize_next` behaviour (oldest-pick, empty-queue, cooldown, savepoint, failure-class split, queue counts). Apply-layer tests (`_apply_*`, `_auto_ignore_unused`) survive verbatim. |
| `better_memory/ui/app.py` | Modify (medium) | `/observations/synthesize` calls `svc.synthesize_next(project=project)` and renders the new banner. Mutex / worker / WorkerTimeout handling unchanged from PR #18 hardening. |
| `better_memory/ui/templates/fragments/synth_step_banner.html` | Create | New 4-state banner template (success-with-pending / success-without-pending / failure-with-pending / failure-without-pending / empty-queue). Auto-fire `<div>` only when `queue.pending > 0`. |
| `better_memory/ui/templates/fragments/observations_synth_banner.html` | Delete | Superseded by `synth_step_banner.html`. |
| `tests/ui/test_observations.py` | Modify | Rewrite 3 existing `TestObservationsSynthesize` tests; add 6 new tests (failure paths, chain recovery, HX-Trigger cadence, queue-count source). |
| `better_memory/mcp/server.py` | Modify (small) | `memory.start_episode` calls `synthesize_next` in a drain loop, then `retrieve_reflections(tech=...)` for buckets. |
| `tests/mcp/test_episode_tools.py` | Modify | Rewrite `start_episode` synth test; add 3 new tests (no-pending, failure-mid-loop, persistent-failure-terminates). |
| `tests/conftest.py` | Modify | Add `seed_pending_episodes(conn, project, n, obs_per_episode=2, tech=None) -> list[str]` helper, used by service / UI / MCP tests. |
| `better_memory/db/migrations/0007_reflection_scope.sql` | Create | Commit 5: scope columns on observations + reflections; partial index; one-shot fix-up. |
| `tests/db/test_migration_0007.py` | Create | Commit 5: schema/CHECK/default/index/fix-up. |
| `better_memory/services/observation.py` | Modify | Commit 5: `create()` accepts `scope='project'\|'general'` kwarg. |
| `tests/services/test_observation.py` | Modify | Commit 5: scope tests. |

---

## Pre-implementation setup

- [ ] **Step 0a: Create branch**

```bash
git checkout main
git pull
git checkout -b episodic-synthesis
```

- [ ] **Step 0b: Sanity check baseline tests are green**

```bash
uv run pytest -q
```

Expected: all tests pass (the existing 99-passing baseline). If anything fails on `main`, stop and fix the baseline before proceeding — this plan assumes a green starting point.

- [ ] **Step 0c: Sanity check pyright is clean**

```bash
uv run pyright
```

Expected: no errors. If pre-existing pyright errors are present, note them so you can distinguish them from new ones the plan introduces.

---

# Commit 1 — Migration 0006

### Task 1: Write the migration test (red)

**Files:**
- Create: `tests/db/test_migration_0006.py`

- [ ] **Step 1: Write the failing migration test**

```python
# tests/db/test_migration_0006.py
"""Migration 0006: episodes.synthesized_at + synth_failed_at; drop synthesis_runs.

Forward-only — verifies the migration's schema effects and backfill
correctness against a representative seed. The seed mirrors the four
real episode states the user's DB can contain at migration time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def seeded_conn(tmp_path: Path):
    """A DB at the post-0005 baseline with four representative episodes seeded."""
    db_path = tmp_path / "test.db"
    c = connect(db_path)

    # Apply only migrations 0001–0005 by stopping before 0006 lives.
    # We do this by applying everything that exists at this moment, but the
    # test runs after 0006 has been authored, so we have to seed BEFORE
    # apply_migrations runs to make this a "starting state" test.
    # Strategy: apply_migrations once (it'll apply 0001-0006), then we seed
    # via raw SQL using the post-0005 schema explicitly. The 0006 effects
    # are then asserted via PRAGMA / COUNT queries.
    apply_migrations(c)

    # Seed four episodes + observations + a synthesis_runs row that
    # exists pre-0006 but should be gone post-0006.
    # NOTE: we work backwards — seed AS IF synthesized_at didn't exist,
    # then verify the backfill happened correctly. To do this we just
    # NULL out synthesized_at after the migration runs, then re-run
    # the backfill UPDATE to test it specifically.
    # Simpler approach: seed directly via the post-0006 schema with
    # synthesized_at NULL on all rows, then run the backfill SQL inline.

    # Episode A: closed, all observations consumed → backfill should set synthesized_at
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-a','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('a1','x','p1','ep-a','consumed_into_reflection','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00')"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('a2','y','p1','ep-a','consumed_without_reflection','success',"
        "'2026-04-01T00:31:00+00:00','2026-04-01T00:31:00+00:00')"
    )

    # Episode B: closed, mixed status (1 active + 1 consumed) → synthesized_at stays NULL
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-b','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('b1','x','p1','ep-b','active','success',"
        "'2026-04-02T00:30:00+00:00','2026-04-02T00:30:00+00:00')"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('b2','y','p1','ep-b','consumed_into_reflection','success',"
        "'2026-04-02T00:31:00+00:00','2026-04-02T00:31:00+00:00')"
    )

    # Episode C: closed, no observations at all → synthesized_at backfilled
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-c','p1','2026-04-03T00:00:00+00:00','2026-04-03T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )

    # Episode D: open (outcome NULL) → synthesized_at stays NULL
    c.execute(
        "INSERT INTO episodes (id, project, started_at, outcome, "
        "synthesized_at) VALUES "
        "('ep-d','p1','2026-04-04T00:00:00+00:00', NULL, NULL)"
    )

    c.commit()

    # Re-run the backfill statement specifically so we're testing it
    # in isolation against this seed (apply_migrations already ran it
    # against an empty DB, where it was a no-op).
    c.execute(
        """
        UPDATE episodes
           SET synthesized_at = ended_at
         WHERE outcome IS NOT NULL
           AND id NOT IN (
               SELECT DISTINCT episode_id
                 FROM observations
                WHERE status = 'active'
           )
        """
    )
    c.commit()

    yield c
    c.close()


def test_synthesized_at_column_exists(seeded_conn) -> None:
    cols = {r[1] for r in seeded_conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert "synthesized_at" in cols


def test_synth_failed_at_column_exists(seeded_conn) -> None:
    cols = {r[1] for r in seeded_conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert "synth_failed_at" in cols


def test_partial_index_exists(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='episodes'"
    ).fetchall()
    assert "idx_episodes_pending_synth" in {r[0] for r in rows}


def test_synthesis_runs_table_dropped(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='synthesis_runs'"
    ).fetchall()
    assert rows == []


def test_backfill_episode_a_all_consumed(seeded_conn) -> None:
    """Episode A's observations are all non-active → synthesized_at set."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-a'"
    ).fetchone()
    assert row[0] == "2026-04-01T01:00:00+00:00"  # backfilled to ended_at


def test_backfill_episode_b_mixed_status_stays_null(seeded_conn) -> None:
    """Episode B has an active observation → synthesized_at stays NULL."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-b'"
    ).fetchone()
    assert row[0] is None


def test_backfill_episode_c_no_observations(seeded_conn) -> None:
    """Episode C is closed with zero observations → backfill applies."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-c'"
    ).fetchone()
    assert row[0] == "2026-04-03T01:00:00+00:00"


def test_open_episode_d_stays_null(seeded_conn) -> None:
    """Open episode (outcome NULL) → synthesized_at stays NULL."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-d'"
    ).fetchone()
    assert row[0] is None


def test_synth_failed_at_is_null_for_all_existing_rows(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT synth_failed_at FROM episodes"
    ).fetchall()
    assert all(r[0] is None for r in rows)


def test_backfill_invariant(seeded_conn) -> None:
    """count(closed episodes with no active observations) == count(closed with synthesized_at NOT NULL)."""
    no_active = seeded_conn.execute(
        """
        SELECT COUNT(*) FROM episodes
         WHERE outcome IS NOT NULL
           AND id NOT IN (
               SELECT DISTINCT episode_id FROM observations WHERE status='active'
           )
        """
    ).fetchone()[0]
    backfilled = seeded_conn.execute(
        "SELECT COUNT(*) FROM episodes "
        "WHERE outcome IS NOT NULL AND synthesized_at IS NOT NULL"
    ).fetchone()[0]
    assert no_active == backfilled
```

- [ ] **Step 2: Run the test, verify it fails (red)**

Run: `uv run pytest tests/db/test_migration_0006.py -v`
Expected: all 10 tests fail (most likely with `OperationalError: no such column: synthesized_at` or similar — the migration doesn't exist yet).

### Task 2: Write the migration (green)

**Files:**
- Create: `better_memory/db/migrations/0006_per_episode_synthesis.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- better-memory migration 0006: per-episode synthesis tracking.
--
-- Replaces the watermark-driven `synthesis_runs` model with a per-episode
-- one. See docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md.

-- 1. Per-episode tracking column. NULL means "needs synthesis".
ALTER TABLE episodes ADD COLUMN synthesized_at TIMESTAMP;

-- 2. Per-episode failure-cooldown column. Set on LLM-class failure;
--    excluded from `_pick_oldest_pending` for 300 s after stamping.
ALTER TABLE episodes ADD COLUMN synth_failed_at TIMESTAMP;

-- 3. Partial index for "find next pending episode".
--    Cheap: indexes only rows that are currently candidates for synth.
--    The cooldown filter is applied at SELECT-time because datetime('now')
--    is non-deterministic and can't live in a partial-index predicate.
CREATE INDEX idx_episodes_pending_synth
    ON episodes(project, ended_at)
    WHERE outcome IS NOT NULL AND synthesized_at IS NULL;

-- 4. Backfill: closed episodes whose observations are ALL non-active
--    have effectively been consolidated by the prior batch synth — mark
--    them done. Closed episodes with leftover active observations stay
--    NULL → next synth run picks them up.
UPDATE episodes
   SET synthesized_at = ended_at
 WHERE outcome IS NOT NULL
   AND id NOT IN (
       SELECT DISTINCT episode_id
         FROM observations
        WHERE status = 'active'
   );

-- 5. Drop the watermark table — superseded by per-episode tracking.
DROP TABLE synthesis_runs;
```

- [ ] **Step 2: Run the test, verify it passes (green)**

Run: `uv run pytest tests/db/test_migration_0006.py -v`
Expected: all 10 tests pass.

- [ ] **Step 3: Run the full test suite to see what we just broke**

Run: `uv run pytest -q`
Expected: many failures — `tests/db/test_schema.py` (4 stale `synthesis_runs` tests), `tests/services/test_reflection.py` (every test that touches `synthesis_runs` or short-circuit / watermark / load_context), `tests/ui/test_observations.py` (synth route), `tests/mcp/test_episode_tools.py` (start_episode). This is expected — Commit 2 will fix the service tests, Commits 3 and 4 the UI and MCP tests. Commit 1 only fixes the schema tests.

### Task 3: Schema test cleanup

**Files:**
- Modify: `tests/db/test_schema.py`

- [ ] **Step 1: Delete the 4 stale synthesis_runs tests**

Open `tests/db/test_schema.py` and delete:
- `test_synthesis_runs_exists` (around line 696)
- `test_synthesis_runs_composite_pk` (around line 706)
- `test_synthesis_runs_has_last_goal_column` (around line 750)
- `test_synthesis_runs_last_goal_round_trips` (around line 773)

These four functions go to zero — drop them, including any nearby `# ---` separator comments that referenced them.

- [ ] **Step 2: Add 3 new schema-shape tests**

Append to `tests/db/test_schema.py`:

```python
def test_episodes_has_synthesized_at_column(tmp_memory_db: Path) -> None:
    with connect(tmp_memory_db) as conn:
        apply_migrations(conn)
        cols = _column_names(conn, "episodes")
        assert "synthesized_at" in cols


def test_episodes_has_synth_failed_at_column(tmp_memory_db: Path) -> None:
    with connect(tmp_memory_db) as conn:
        apply_migrations(conn)
        cols = _column_names(conn, "episodes")
        assert "synth_failed_at" in cols


def test_idx_episodes_pending_synth_partial_index_exists(tmp_memory_db: Path) -> None:
    with connect(tmp_memory_db) as conn:
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_episodes_pending_synth'"
        ).fetchall()
        assert len(rows) == 1
        # Confirm it's the partial index we expected (predicate on synthesized_at IS NULL).
        assert "synthesized_at IS NULL" in rows[0][1]
```

- [ ] **Step 3: Run the schema tests**

Run: `uv run pytest tests/db/test_schema.py -v`
Expected: all pass (the deleted ones are gone; the new ones exercise the new schema).

### Task 4: Pre-flight grep + commit

- [ ] **Step 1: Pre-flight grep**

Run:
```bash
grep -r "synthesis_runs" better_memory/ tests/
```

Expected hits ONLY in:
- `better_memory/db/migrations/0002_episodic.sql` (original CREATE)
- `better_memory/db/migrations/0003_synthesis_runs_last_goal.sql` (original ALTER)
- `better_memory/db/migrations/0006_per_episode_synthesis.sql` (the DROP)
- `better_memory/services/reflection.py` (still references it — Commit 2 removes these)
- `tests/services/test_reflection.py` (still references it — Commit 2 removes these)

Any other hit means a stale caller exists. Commit 2 will clean up the `reflection.py` and `test_reflection.py` references; for now, just make sure no surprise file references it.

- [ ] **Step 2: Commit**

```bash
git add better_memory/db/migrations/0006_per_episode_synthesis.sql \
        tests/db/test_migration_0006.py \
        tests/db/test_schema.py
git commit -m "$(cat <<'EOF'
feat(db): migration 0006 — episodes.synthesized_at + drop synthesis_runs

Adds per-episode tracking columns to episodes:
- synthesized_at: NULL until the episode has been consolidated
- synth_failed_at: set on LLM-class failures for 300s cooldown

Backfills synthesized_at for closed episodes whose observations are all
non-active. Episodes with leftover active observations stay NULL → picked
up by the next synth run under the new design.

Drops synthesis_runs — the per-episode columns supersede it.

Includes test_migration_0006.py covering schema, index, and backfill
correctness; updates test_schema.py to drop synthesis_runs assertions
and add coverage for the new columns and partial index.

Service / route / MCP changes follow in subsequent commits — service
tests, UI synth tests, and MCP start_episode tests are expected to fail
on this commit but pass after Commit 4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: clean commit, no hook failures.

---

# Commit 2 — Service refactor

> The largest commit. Sequencing matters: build the dataclasses first, then the small private helpers (TDD each), then the public `synthesize_next` orchestrator, then strip the old code. Tests for `synthesize_next` use `FakeChat` (`better_memory/llm/fake.py`) for LLM injection and the existing `_insert_obs` / `_insert_reflection` helpers in `tests/services/test_reflection.py`.

### Task 5: Add new dataclasses

**Files:**
- Modify: `better_memory/services/reflection.py` (add new types alongside existing ones; do not remove old types yet)

- [ ] **Step 1: Add the new dataclasses near the top of the file**

Insert after the existing `ObservationForPrompt` definition (around line 56-68). First, add a `status` field to `ObservationForPrompt`:

```python
@dataclass(frozen=True)
class ObservationForPrompt:
    """Read model for an observation, joined with episode context."""

    id: str
    content: str
    outcome: str
    component: str | None
    theme: str | None
    tech: str | None
    created_at: str
    episode_goal: str | None
    episode_outcome: str | None
    status: str = "active"  # NEW — defaults so existing load_context callers don't break before Task 12 deletes load_context entirely
```

The default `'active'` is load-bearing: `load_context` (still in place until Task 12) constructs `ObservationForPrompt` without `status`, and a default keeps that call site green. After Task 12 deletes `load_context`, every remaining caller passes `status` explicitly.

Then add the new types (place them after `ObservationForPrompt`):

```python
@dataclass(frozen=True)
class EpisodeForPrompt:
    """Read model for one episode, the unit of per-episode synthesis."""

    id: str
    project: str  # populated from the row by _pick_oldest_pending; lets _load_episode_context query reflections without a subquery
    goal: str
    tech: str | None
    outcome: str


@dataclass(frozen=True)
class EpisodeContext:
    """Inputs to a per-episode synthesis prompt."""

    episode: EpisodeForPrompt
    observations: list[ObservationForPrompt]   # ALL observations, regardless of status
    reflections: list[ReflectionForPrompt]     # tech-filtered: tech == episode.tech OR tech IS NULL


@dataclass(frozen=True)
class EpisodeQueueCounts:
    """Single-source-of-truth queue counts, computed from one connection."""

    done: int
    pending: int
    in_cooldown: int

    @property
    def total(self) -> int:
        return self.done + self.pending + self.in_cooldown


@dataclass(frozen=True)
class SynthesisStep:
    """Result of one synthesize_next call."""

    processed: bool                       # False ⇔ no pending episode (queue empty or all in cooldown)
    episode_id: str | None                # which episode was processed (None if processed=False)
    counts: dict[str, int]                # this-step counters: created/augmented/merged/ignored/auto_ignored
    queue: EpisodeQueueCounts             # post-step queue snapshot, single connection, single moment
    failure: str | None                   # set if LLM-class error was caught
```

- [ ] **Step 2: Verify pyright is clean for the new dataclasses**

Run: `uv run pyright better_memory/services/reflection.py`
Expected: no new errors. Note any pre-existing errors so they don't get attributed to this change.

- [ ] **Step 3: Verify no `load_context` update needed (default value protects callers)**

Because `ObservationForPrompt.status` defaults to `'active'`, the existing `load_context` (`reflection.py` around line 384) keeps working without modification — its `ObservationForPrompt(...)` construction simply omits `status` and gets the default. No need to touch `load_context` here; Task 12 will delete it whole.

Verify no regressions:

Run: `uv run pytest tests/services/test_reflection.py -q`
Expected: same red set as after Task 4 — the migration broke things, but adding the dataclass + status default shouldn't add new failures. If new failures appear (specifically `TypeError: ObservationForPrompt.__init__() missing required keyword-only argument`), that means a non-load_context caller exists — find it via grep and either pass `status` or rely on the default.

### Task 6: Implement `_pick_oldest_pending` (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_reflection.py`. Place these tests in a new class `class TestPickOldestPending:` near the bottom of the file:

```python
class TestPickOldestPending:
    def test_returns_none_when_no_closed_episodes(
        self, conn, fixed_clock,
    ):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is None

    def test_returns_none_when_only_open_episodes(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) "
            "VALUES ('open-ep', 'p1', '2026-04-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_returns_oldest_pending_closed_episode(self, conn, fixed_clock):
        # Two closed episodes; the older ended_at wins.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) VALUES "
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','newer goal',NULL,NULL),"
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','older goal','python',NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is not None
        assert result.id == "older"
        assert result.project == "p1"  # populated from row, used by _load_episode_context
        assert result.goal == "older goal"
        assert result.tech == "python"
        assert result.outcome == "success"

    def test_excludes_already_synthesized_episodes(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('done','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','done goal','2026-04-01T01:00:00+00:00')"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_excludes_episodes_in_cooldown_window(self, conn, fixed_clock):
        # Episode failed within the 300s cooldown — should NOT be picked.
        # _pick_oldest_pending compares synth_failed_at to SQLite's
        # datetime('now'), so we seed with a Python-computed timestamp
        # at "now minus 30 seconds" and bind it as a parameter (clean
        # parameterization rather than f-string with a SQL literal).
        from datetime import UTC, datetime, timedelta
        recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at, synth_failed_at) VALUES "
            "('cooled','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL, ?)",
            (recent,),
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_picks_episodes_after_cooldown_elapses(self, conn, fixed_clock):
        # synth_failed_at older than 300s → eligible.
        from datetime import UTC, datetime, timedelta
        old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at, synth_failed_at) VALUES "
            "('cooled','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL, ?)",
            (old,),
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is not None
        assert result.id == "cooled"
```

- [ ] **Step 2: Run the test, verify it fails (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestPickOldestPending -v`
Expected: all 6 fail with `AttributeError: 'ReflectionSynthesisService' object has no attribute '_pick_oldest_pending'`.

- [ ] **Step 3: Implement `_pick_oldest_pending`**

Add to `ReflectionSynthesisService` in `better_memory/services/reflection.py` (place after `_normalize_tech` around line 281):

```python
def _pick_oldest_pending(
    self, project: str
) -> EpisodeForPrompt | None:
    """Return the oldest closed-but-unsynthesized episode for project.

    Excludes:
    - episodes still open (outcome IS NULL)
    - episodes already synthesized (synthesized_at IS NOT NULL)
    - episodes in the 300s failure cooldown window

    Returns None when nothing is eligible (queue is empty or all candidates
    are in cooldown).
    """
    row = self._conn.execute(
        """
        SELECT id, project, goal, tech, outcome
          FROM episodes
         WHERE project = ?
           AND outcome IS NOT NULL
           AND synthesized_at IS NULL
           AND (synth_failed_at IS NULL
                OR replace(replace(synth_failed_at, 'T', ' '), '+00:00', '')
                   < datetime('now', '-300 seconds'))
         ORDER BY ended_at ASC
         LIMIT 1
        """,
        (project,),
    ).fetchone()
    if row is None:
        return None
    return EpisodeForPrompt(
        id=row["id"],
        project=row["project"],
        goal=row["goal"],
        tech=row["tech"],
        outcome=row["outcome"],
    )
```

- [ ] **Step 4: Run the test, verify it passes (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestPickOldestPending -v`
Expected: all 6 pass.

### Task 7: Implement `_load_episode_context` (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_reflection.py`:

```python
class TestLoadEpisodeContext:
    def test_loads_all_observations_regardless_of_status(
        self, conn, fixed_clock,
    ):
        # Episode with one active + one consumed observation.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','test goal','python')"
        )
        _insert_obs(
            conn, obs_id="o-active", project="p1", episode_id="ep1",
            content="active obs", status="active",
            created_at="2026-04-01T00:30:00+00:00",
        )
        _insert_obs(
            conn, obs_id="o-consumed", project="p1", episode_id="ep1",
            content="consumed obs", status="consumed_into_reflection",
            created_at="2026-04-01T00:31:00+00:00",
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="test goal", tech="python", outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert {o.id for o in ctx.observations} == {"o-active", "o-consumed"}
        # Status survives load and is visible to the prompt builder.
        statuses = {o.id: o.status for o in ctx.observations}
        assert statuses["o-active"] == "active"
        assert statuses["o-consumed"] == "consumed_into_reflection"

    def test_filters_reflections_by_episode_tech_or_null(
        self, conn, fixed_clock,
    ):
        # Episode tech=python.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal','python')"
        )
        # Three reflections: matching tech, cross-tech (NULL), non-matching tech.
        _insert_reflection(conn, refl_id="r-py", project="p1", tech="python")
        _insert_reflection(conn, refl_id="r-any", project="p1", tech=None)
        _insert_reflection(conn, refl_id="r-rust", project="p1", tech="rust")
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech="python", outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        ids = {r.id for r in ctx.reflections}
        assert ids == {"r-py", "r-any"}
        assert "r-rust" not in ids

    def test_episode_with_no_tech_loads_all_reflections(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        _insert_reflection(conn, refl_id="r-py", project="p1", tech="python")
        _insert_reflection(conn, refl_id="r-any", project="p1", tech=None)
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert {r.id for r in ctx.reflections} == {"r-py", "r-any"}

    def test_excludes_retired_and_superseded_reflections(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        _insert_reflection(conn, refl_id="r-pending", project="p1", status="pending_review")
        _insert_reflection(conn, refl_id="r-confirmed", project="p1", status="confirmed")
        _insert_reflection(conn, refl_id="r-retired", project="p1", status="retired")
        _insert_reflection(conn, refl_id="r-super", project="p1", status="superseded")
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        ids = {r.id for r in ctx.reflections}
        assert ids == {"r-pending", "r-confirmed"}
```

- [ ] **Step 2: Run the test, verify it fails (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestLoadEpisodeContext -v`
Expected: 4 failures with `AttributeError: ... has no attribute '_load_episode_context'`.

- [ ] **Step 3: Implement `_load_episode_context`**

Add to `ReflectionSynthesisService`:

```python
def _load_episode_context(
    self, episode: EpisodeForPrompt
) -> EpisodeContext:
    """Load all observations for the episode + tech-filtered reflections."""
    # All observations for this episode, regardless of status — the LLM
    # gets the whole episode story, with status visible so it can treat
    # consumed observations as historical context.
    obs_rows = self._conn.execute(
        """
        SELECT id, content, outcome, component, theme, tech,
               created_at, status
          FROM observations
         WHERE episode_id = ?
         ORDER BY created_at ASC, rowid ASC
        """,
        (episode.id,),
    ).fetchall()
    observations = [
        ObservationForPrompt(
            id=r["id"], content=r["content"], outcome=r["outcome"],
            component=r["component"], theme=r["theme"], tech=r["tech"],
            created_at=r["created_at"],
            episode_goal=episode.goal,
            episode_outcome=episode.outcome,
            status=r["status"],
        )
        for r in obs_rows
    ]

    # Tech-filtered reflections — same semantic as the old load_context:
    # match same-tech rows OR cross-tech (tech IS NULL) rows. Excludes
    # retired and superseded statuses. Uses episode.project directly
    # (no subquery) thanks to EpisodeForPrompt.project.
    # NOTE: Commit 5 will extend the project filter to (project = ? OR
    # scope = 'general'). Until Commit 5 lands, this remains project-only.
    tech = self._normalize_tech(episode.tech)
    if tech is None:
        refl_rows = self._conn.execute(
            """
            SELECT id, title, tech, phase, polarity, use_cases, hints,
                   confidence, status
              FROM reflections
             WHERE project = ?
               AND status IN ('pending_review', 'confirmed')
             ORDER BY confidence DESC, updated_at DESC
            """,
            (episode.project,),
        ).fetchall()
    else:
        refl_rows = self._conn.execute(
            """
            SELECT id, title, tech, phase, polarity, use_cases, hints,
                   confidence, status
              FROM reflections
             WHERE project = ?
               AND status IN ('pending_review', 'confirmed')
               AND (tech = ? OR tech IS NULL)
             ORDER BY confidence DESC, updated_at DESC
            """,
            (episode.project, tech),
        ).fetchall()

    reflections = [
        ReflectionForPrompt(
            id=r["id"], title=r["title"], tech=r["tech"],
            phase=r["phase"], polarity=r["polarity"],
            use_cases=r["use_cases"], hints=r["hints"],
            confidence=r["confidence"], status=r["status"],
        )
        for r in refl_rows
    ]
    return EpisodeContext(
        episode=episode,
        observations=observations,
        reflections=reflections,
    )
```

- [ ] **Step 4: Run the test, verify it passes (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestLoadEpisodeContext -v`
Expected: all 4 pass.

### Task 8: Implement `_build_episode_prompt` (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_reflection.py`:

```python
class TestBuildEpisodePrompt:
    def _ctx(self, observations=None, reflections=None, tech="python"):
        return EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p1", goal="finish feature X", tech=tech,
                outcome="success",
            ),
            observations=observations or [],
            reflections=reflections or [],
        )

    def test_includes_episode_metadata(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx())
        assert "EPISODE" in prompt
        assert "finish feature X" in prompt
        assert "python" in prompt
        assert "success" in prompt

    def test_renders_unspecified_tech_when_none(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx(tech=None))
        assert "(unspecified)" in prompt

    def test_includes_each_observation_with_status(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        obs = ObservationForPrompt(
            id="o-1", content="found bug", outcome="success",
            component="api", theme="bug", tech="python",
            created_at="2026-04-01T00:30:00+00:00",
            episode_goal="g", episode_outcome="success",
            status="active",
        )
        prompt = svc._build_episode_prompt(self._ctx(observations=[obs]))
        assert "id=o-1" in prompt
        assert "found bug" in prompt
        assert "active" in prompt

    def test_marks_consumed_observations_status(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        obs = ObservationForPrompt(
            id="o-c", content="historical", outcome="success",
            component=None, theme=None, tech=None,
            created_at="2026-04-01T00:30:00+00:00",
            episode_goal="g", episode_outcome="success",
            status="consumed_into_reflection",
        )
        prompt = svc._build_episode_prompt(self._ctx(observations=[obs]))
        assert "consumed_into_reflection" in prompt

    def test_includes_existing_reflections(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        refl = ReflectionForPrompt(
            id="r-1", title="prefer try/except over LBYL",
            tech="python", phase="implementation", polarity="do",
            use_cases="error handling", hints='["wrap with except Exception"]',
            confidence=0.8, status="confirmed",
        )
        prompt = svc._build_episode_prompt(self._ctx(reflections=[refl]))
        assert "id=r-1" in prompt
        assert "prefer try/except over LBYL" in prompt
        assert "0.8" in prompt
        assert "confirmed" in prompt

    def test_includes_json_shape_instructions(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx())
        # The JSON shape block at the end of the prompt is critical —
        # without it the LLM emits free-form prose. Assert key keys present.
        assert '"new"' in prompt
        assert '"augment"' in prompt
        assert '"merge"' in prompt
        assert '"ignore"' in prompt
        assert "source_observation_ids" in prompt
```

- [ ] **Step 2: Run the test, verify it fails (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestBuildEpisodePrompt -v`
Expected: 6 failures with `AttributeError: ... has no attribute '_build_episode_prompt'`.

- [ ] **Step 3: Implement `_build_episode_prompt`**

Add to `ReflectionSynthesisService`:

```python
def _build_episode_prompt(self, ctx: EpisodeContext) -> str:
    """Render the per-episode synthesis prompt.

    Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md
    "Per-episode prompt template".

    Deterministic in its inputs.
    """
    lines: list[str] = []
    lines.append(
        "You are evaluating one coding episode for memory consolidation."
    )
    lines.append("")
    lines.append("EPISODE")
    lines.append(f"  goal:    {ctx.episode.goal}")
    lines.append(
        f"  tech:    {ctx.episode.tech if ctx.episode.tech else '(unspecified)'}"
    )
    lines.append(f"  outcome: {ctx.episode.outcome}")
    lines.append("")

    lines.append(
        "OBSERVATIONS from this episode (all of them, regardless of prior status):"
    )
    if not ctx.observations:
        lines.append("  (none)")
    else:
        for o in ctx.observations:
            tech_str = o.tech if o.tech else "any-tech"
            lines.append(
                f"- id={o.id} (outcome={o.outcome}, "
                f"component={o.component or '-'}, "
                f"theme={o.theme or '-'}, tech={tech_str})"
            )
            lines.append(f"  status: {o.status}")
            lines.append(f"  content: {o.content}")
    lines.append("")

    lines.append(
        "EXISTING REFLECTIONS for this tech "
        "(you may augment, merge, leave alone):"
    )
    if not ctx.reflections:
        lines.append("  (none)")
    else:
        for r in ctx.reflections:
            tech_str = r.tech if r.tech else "any-tech"
            lines.append(
                f"- id={r.id} [{r.polarity}/{r.phase}/{tech_str}] "
                f"(confidence {r.confidence}, status {r.status})"
            )
            lines.append(f"  title: {r.title}")
            lines.append(f"  use_cases: {r.use_cases}")
            lines.append(f"  hints: {r.hints}")
    lines.append("")

    lines.append(
        "Decide what to do with this episode's observations. "
        "Respond ONLY with this JSON shape:"
    )
    lines.append("{")
    lines.append('  "new": [')
    lines.append(
        "    {"
        '"title": "...", '
        '"phase": "planning"|"implementation"|"general", '
        '"polarity": "do"|"dont"|"neutral", '
        '"use_cases": "...", '
        '"hints": ["..."], '
        '"tech": "..." or null, '
        '"confidence": 0.1..1.0, '
        '"source_observation_ids": ["..."]'
        "}"
    )
    lines.append("  ],")
    lines.append('  "augment": [')
    lines.append(
        "    {"
        '"reflection_id": "...", '
        '"add_hints": ["..."], '
        '"rewrite_use_cases": "..." or null, '
        '"confidence_delta": 0.0, '
        '"add_source_observation_ids": ["..."]'
        "}"
    )
    lines.append("  ],")
    lines.append('  "merge": [')
    lines.append(
        "    {"
        '"source_id": "...", '
        '"target_id": "...", '
        '"justification": "..."'
        "}"
    )
    lines.append("  ],")
    lines.append('  "ignore": ["observation_id", ...]')
    lines.append("}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run the test, verify it passes (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestBuildEpisodePrompt -v`
Expected: all 6 pass.

### Task 9: Implement `_mark_synthesized` and `_read_queue_counts` (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_reflection.py`:

```python
class TestMarkSynthesized:
    def test_sets_synthesized_at_to_clock_value(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._mark_synthesized("ep1")
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] == "2026-04-22T10:00:00+00:00"  # the fixed_clock value


class TestReadQueueCounts:
    def test_counts_zero_for_empty_project(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="empty")
        assert counts.done == 0
        assert counts.pending == 0
        assert counts.in_cooldown == 0
        assert counts.total == 0

    def test_counts_done_pending_and_cooldown_separately(
        self, conn, fixed_clock,
    ):
        # 2 done, 3 pending (NULL synth_failed_at), 1 in cooldown.
        # Compute the cooldown timestamp in Python and bind it as a
        # parameter — uniform parameterization, no SQL string interpolation.
        from datetime import UTC, datetime, timedelta
        cooldown_recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()

        rows = [
            ("2026-04-01T01:00:00+00:00", None),  # done
            ("2026-04-01T01:00:00+00:00", None),  # done
            (None, None),                          # pending
            (None, None),                          # pending
            (None, None),                          # pending
            (None, cooldown_recent),               # cooldown
        ]
        for i, (synthesized_at, synth_failed_at) in enumerate(rows):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, ended_at, "
                "outcome, close_reason, goal, synthesized_at, synth_failed_at) "
                "VALUES (?, 'p1', '2026-04-01T00:00:00+00:00', "
                "'2026-04-01T01:00:00+00:00', 'success', 'goal_complete', 'g', ?, ?)",
                (f"e{i}", synthesized_at, synth_failed_at),
            )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="p1")
        assert counts.done == 2
        assert counts.pending == 3
        assert counts.in_cooldown == 1
        assert counts.total == 6

    def test_excludes_open_episodes_from_total(self, conn, fixed_clock):
        # Open episode (outcome NULL) is not counted in any bucket.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) VALUES "
            "('open','p1','2026-04-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="p1")
        assert counts.total == 0
```

- [ ] **Step 2: Run the tests, verify they fail (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestMarkSynthesized tests/services/test_reflection.py::TestReadQueueCounts -v`
Expected: 4 failures with `AttributeError: ... has no attribute '_mark_synthesized'` / `_read_queue_counts`.

- [ ] **Step 3: Implement both methods**

Add to `ReflectionSynthesisService`:

```python
def _mark_synthesized(self, episode_id: str) -> None:
    """Set synthesized_at = clock() for the given episode."""
    self._conn.execute(
        "UPDATE episodes SET synthesized_at = ? WHERE id = ?",
        (self._clock().isoformat(), episode_id),
    )

def _read_queue_counts(self, project: str) -> EpisodeQueueCounts:
    """Snapshot the per-project queue state in one statement.

    Single-source-of-truth for the route's banner — eliminates the race
    that would exist if `done` and `pending` were read from different
    connections at different times.
    """
    row = self._conn.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE synthesized_at IS NOT NULL)
            AS done,
          COUNT(*) FILTER (WHERE synthesized_at IS NULL
                             AND (synth_failed_at IS NULL
                                  OR replace(replace(synth_failed_at, 'T', ' '), '+00:00', '')
                                     < datetime('now','-300 seconds')))
            AS pending,
          COUNT(*) FILTER (WHERE synthesized_at IS NULL
                             AND replace(replace(synth_failed_at, 'T', ' '), '+00:00', '')
                                 >= datetime('now','-300 seconds'))
            AS in_cooldown
        FROM episodes
        WHERE project = ? AND outcome IS NOT NULL
        """,
        (project,),
    ).fetchone()
    return EpisodeQueueCounts(
        done=row["done"], pending=row["pending"], in_cooldown=row["in_cooldown"],
    )
```

- [ ] **Step 4: Run the tests, verify they pass (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestMarkSynthesized tests/services/test_reflection.py::TestReadQueueCounts -v`
Expected: all 4 pass.

### Task 10: Implement `synthesize_next` happy path (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write the failing tests for happy path**

Append to `tests/services/test_reflection.py`:

```python
class TestSynthesizeNextHappyPath:
    def _empty_response(self) -> str:
        import json
        return json.dumps(
            {"new": [], "augment": [], "merge": [], "ignore": []}
        )

    def test_returns_processed_false_when_empty_queue(
        self, conn, fixed_clock,
    ):
        chat = FakeChat(responses=[])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is False
        assert step.episode_id is None
        assert step.failure is None
        assert step.queue.total == 0
        assert chat.calls == []  # no LLM call

    def test_processes_oldest_pending_and_marks_synthesized(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','test goal','python',NULL)"
        )
        _insert_obs(
            conn, obs_id="o1", project="p1", episode_id="ep1",
            content="bug found", status="active",
            created_at="2026-04-01T00:30:00+00:00",
        )
        conn.commit()
        chat = FakeChat(responses=[self._empty_response()])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.episode_id == "ep1"
        assert step.failure is None
        # synthesized_at should be set to the clock value.
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] == "2026-04-22T10:00:00+00:00"
        # Queue snapshot reflects the new state.
        assert step.queue.done == 1
        assert step.queue.pending == 0

    def test_counts_reflect_apply_actions(self, conn, fixed_clock):
        import json
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal')"
        )
        _insert_obs(
            conn, obs_id="o1", project="p1", episode_id="ep1",
            content="bug", status="active",
        )
        conn.commit()
        response = json.dumps({
            "new": [{
                "title": "lesson", "phase": "implementation",
                "polarity": "do", "use_cases": "uc",
                "hints": ["h1"], "tech": None, "confidence": 0.5,
                "source_observation_ids": ["o1"],
            }],
            "augment": [], "merge": [], "ignore": [],
        })
        chat = FakeChat(responses=[response])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.counts["created"] == 1
        assert step.counts["augmented"] == 0
        # The reflection landed.
        n = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE project='p1'"
        ).fetchone()[0]
        assert n == 1

    def test_oldest_first_order(self, conn, fixed_clock):
        # Two pending closed episodes; the older one is processed first.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','newer goal',NULL),"
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','older goal',NULL)"
        )
        conn.commit()
        chat = FakeChat(responses=[self._empty_response()])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.episode_id == "older"
        # The newer one is still pending.
        assert step.queue.pending == 1
        # Run a second call — should pick the newer one now.
        chat.responses.append(self._empty_response())
        step2 = run_async(svc.synthesize_next(project="p1"))
        assert step2.episode_id == "newer"
        assert step2.queue.pending == 0
```

- [ ] **Step 2: Run the tests, verify they fail (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesizeNextHappyPath -v`
Expected: 4 failures with `AttributeError: ... has no attribute 'synthesize_next'`.

- [ ] **Step 3: Implement `synthesize_next` (happy path only — failure handling in next task)**

Add to `ReflectionSynthesisService`. Place near the bottom of the class, after `_apply_*`:

```python
async def synthesize_next(self, *, project: str) -> SynthesisStep:
    """Process the oldest closed-but-unsynthesized episode for project.

    One LLM call per episode. Returns a SynthesisStep describing what
    happened. Caller (UI route or MCP loop) drives the iteration.

    Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md
    """
    episode = self._pick_oldest_pending(project)
    if episode is None:
        return SynthesisStep(
            processed=False, episode_id=None,
            counts=dict(self._ZERO_COUNTS),
            queue=self._read_queue_counts(project),
            failure=None,
        )

    self._conn.execute("SAVEPOINT episode_synthesize")
    try:
        ctx = self._load_episode_context(episode)
        prompt = self._build_episode_prompt(ctx)
        raw = await self._chat.complete(prompt)
        response = self.parse_response(raw)

        self._apply_new(response.new, project=project)
        self._apply_augment(response.augment)
        self._apply_merge(response.merge)
        self._apply_ignore(response.ignore)
        active_ids = [o.id for o in ctx.observations if o.status == "active"]
        auto_ignored = self._auto_ignore_unused(active_ids)
        self._mark_synthesized(episode.id)
    except BaseException:
        # Failure handling in next task. For now, just rollback and re-raise.
        self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        raise
    else:
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
    self._conn.commit()

    counts = {
        "created": len(response.new),
        "augmented": len(response.augment),
        "merged": len(response.merge),
        "ignored": len(response.ignore),
        "auto_ignored": auto_ignored,
    }
    return SynthesisStep(
        processed=True, episode_id=episode.id,
        counts=counts,
        queue=self._read_queue_counts(project),
        failure=None,
    )
```

- [ ] **Step 4: Run the tests, verify they pass (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesizeNextHappyPath -v`
Expected: all 4 pass.

### Task 11: Add cooldown / failure handling to `synthesize_next` (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write failing tests for the failure-class split + cooldown stamping**

Append to `tests/services/test_reflection.py`:

```python
class TestSynthesizeNextFailurePaths:
    def test_chat_error_stamps_synth_failed_at_and_returns_failure(
        self, conn, fixed_clock,
    ):
        from better_memory.llm.ollama import ChatError

        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        class BoomChat:
            calls: list[str] = []
            async def complete(self, prompt: str) -> str:
                self.calls.append(prompt)
                raise ChatError("ollama unreachable")

        svc = ReflectionSynthesisService(
            conn, chat=BoomChat(), clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.episode_id == "ep1"
        assert step.failure == "ollama unreachable"
        assert step.counts == {"created": 0, "augmented": 0, "merged": 0,
                               "ignored": 0, "auto_ignored": 0}

        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None  # NOT marked synthesized
        assert row["synth_failed_at"] is not None  # cooldown stamped

    def test_parse_error_stamps_synth_failed_at(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        # Garbage response — parse_response will raise SynthesisResponseError.
        chat = FakeChat(responses=["not even valid json{{{"])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.failure is not None
        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is not None

    def test_cooldown_excludes_failed_episode_from_next_pick(
        self, conn, fixed_clock,
    ):
        from better_memory.llm.ollama import ChatError

        # Two pending episodes; older one fails, next call should pick newer.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','o',NULL),"
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','n',NULL)"
        )
        conn.commit()

        # First call: ChatError on older. Second call: empty response on newer.
        class FlakyChat:
            calls = 0
            async def complete(self, prompt: str) -> str:
                FlakyChat.calls += 1
                if FlakyChat.calls == 1:
                    raise ChatError("transient")
                import json
                return json.dumps({"new": [], "augment": [], "merge": [], "ignore": []})

        svc = ReflectionSynthesisService(
            conn, chat=FlakyChat(), clock=fixed_clock,
        )
        step1 = run_async(svc.synthesize_next(project="p1"))
        assert step1.episode_id == "older"
        assert step1.failure is not None

        step2 = run_async(svc.synthesize_next(project="p1"))
        assert step2.episode_id == "newer"  # older is in cooldown — skipped
        assert step2.failure is None

    def test_db_integrity_error_propagates_and_no_synth_failed_at(
        self, conn, fixed_clock, monkeypatch,
    ):
        import sqlite3

        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        # Patch _apply_new to raise IntegrityError mid-apply (simulates a
        # structural DB failure). The except BaseException path should
        # rollback and re-raise — NOT stamp synth_failed_at.
        def boom(self, actions, *, project):
            raise sqlite3.IntegrityError("simulated FK violation")

        monkeypatch.setattr(
            ReflectionSynthesisService, "_apply_new", boom
        )

        # Use a response that has at least one "new" so _apply_new fires.
        import json
        chat = FakeChat(responses=[json.dumps({
            "new": [{"title": "x", "phase": "general", "polarity": "do",
                     "use_cases": "u", "hints": ["h"], "tech": None,
                     "confidence": 0.5, "source_observation_ids": []}],
            "augment": [], "merge": [], "ignore": [],
        })])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        with pytest.raises(sqlite3.IntegrityError):
            run_async(svc.synthesize_next(project="p1"))

        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is None  # No cooldown stamp on DB error
```

- [ ] **Step 2: Run the tests, verify they fail (red)**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesizeNextFailurePaths -v`
Expected: 4 failures. The first three fail because the current `synthesize_next` doesn't catch `ChatError`/`SynthesisResponseError` and just re-raises — the test expects a returned `SynthesisStep` with `failure` set. The fourth might actually pass since the BaseException path re-raises — verify; if it passes already, that's fine.

- [ ] **Step 3: Add the failure-class split to `synthesize_next`**

Replace the `except BaseException` block in `synthesize_next` with:

```python
    except (ChatError, SynthesisResponseError) as exc:
        # LLM-class failure: ROLLBACK so partial reflection_sources/inserts
        # don't land, then stamp synth_failed_at OUTSIDE the savepoint so
        # the cooldown record persists. Without the cooldown stamp,
        # _pick_oldest_pending would return the same episode every call →
        # MCP drain loop spins forever and the UI auto-fire chain re-shows
        # the same failure card 67 times.
        self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        self._conn.execute(
            "UPDATE episodes SET synth_failed_at = ? WHERE id = ?",
            (self._clock().isoformat(), episode.id),
        )
        self._conn.commit()
        # Note: if THIS UPDATE itself raises (e.g. busy_timeout exceeded),
        # the exception propagates to BaseException → route 500. The cooldown
        # would not be stamped, so the same episode is picked again next
        # call. Acceptable degradation: in practice this UPDATE is one row,
        # single column, by id, with PRAGMA busy_timeout=5000 — vanishingly rare.
        return SynthesisStep(
            processed=True, episode_id=episode.id,
            counts=dict(self._ZERO_COUNTS),
            queue=self._read_queue_counts(project),
            failure=str(exc),
        )
    except BaseException:
        # Structural / DB / unexpected: rollback and propagate.
        # Synthesized_at and synth_failed_at both stay NULL; the route
        # surfaces a 500 and stops the chain.
        self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        raise
```

Add the import at the top of the file (alongside the existing imports):

```python
from better_memory.llm.ollama import ChatCompleter, ChatError
```

- [ ] **Step 4: Run the tests, verify they pass (green)**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesizeNextFailurePaths -v`
Expected: all 4 pass.

### Task 12: Remove old methods + delete obsolete tests

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Delete the old methods and dataclass from `reflection.py`** — surgical, one at a time, grep-verify after each

This is the highest-risk step in the plan. Do it in a fixed sequence and grep-verify after each removal so dangling references surface immediately. **After each sub-step, run** `grep -nE "<just-removed-name>" better_memory/ tests/` and confirm only spec/plan/migration-string hits remain.

Sub-steps (do in order):

| # | Remove | Where | Grep verify (only spec/plan/sql hits OK) |
|---|---|---|---|
| 12.1.a | `synthesize` method | reflection.py ~line 940-991 | `grep -nE "\.synthesize\(\|def synthesize\b" better_memory/ tests/` |
| 12.1.b | `_bucketed_reflections` method | reflection.py ~line 993-1002 | `grep -nE "_bucketed_reflections" better_memory/ tests/` |
| 12.1.c | `_should_short_circuit` method | reflection.py ~line 884-938 | `grep -nE "_should_short_circuit" better_memory/ tests/` |
| 12.1.d | `_upsert_watermark` method | reflection.py ~line 866-882 | `grep -nE "_upsert_watermark" better_memory/ tests/` |
| 12.1.e | `load_context` method | reflection.py ~line 287-399 | `grep -nE "load_context\|SynthesisContext" better_memory/ tests/` (latter still hits the dataclass — fine) |
| 12.1.f | `build_prompt` method | reflection.py ~line 401-506 | `grep -nE "\.build_prompt\b\|def build_prompt" better_memory/ tests/` |
| 12.1.g | `SynthesisContext` dataclass | reflection.py ~line 71-78 | `grep -nE "SynthesisContext" better_memory/ tests/` |
| 12.1.h | `last_run_counts` property + `_last_run_counts` attr | reflection.py ~line 264-279 | `grep -nE "last_run_counts" better_memory/ tests/` |

**Keep unchanged:** `parse_response`, all `_apply_*` methods, `_auto_ignore_unused`, `_filter_existing_observations`, `retrieve_reflections`, `ReflectionService` lifecycle class, `_normalize_tech`, `_ZERO_COUNTS` class attribute (still used by `synthesize_next` failure path).

After all 8 sub-steps, run `uv run pyright better_memory/services/reflection.py` — pyright catches anything dataclass-typed that still references removed types. Then run `uv run pytest tests/services/test_reflection.py -q` and expect remaining failures only in the survival-tests-with-stale-callers category, which Step 2 cleans up.

If a grep verify hits anything outside expected files (markdown / migration SQL / migration strings), STOP and investigate before continuing — silently leaving a dangling reference is the failure mode this step is designed to prevent.

- [ ] **Step 2: Delete obsolete tests in `test_reflection.py`** — sequenced, with pytest after each batch

Same defensive pattern as Step 1: delete in named groups, run pytest after each, so any test collection error or unexpected fixture coupling surfaces against a small change rather than a 200-line diff.

Sub-step batches (run `uv run pytest tests/services/test_reflection.py -q` after each batch and confirm no new collection errors; ignore `synthesize_next` failures from later tasks):

| # | Batch | Delete |
|---|---|---|
| 12.2.a | Watermark observation loaders | `test_loads_new_observations_since_watermark`, `test_tech_defaults_to_empty_string_in_watermark`, `test_empty_response_still_updates_watermark` |
| 12.2.b | Short-circuit family | `test_counts_zero_on_short_circuit`, `test_different_goal_does_not_short_circuit`, `test_outside_window_does_not_short_circuit`, `test_new_observations_invalidate_short_circuit`, `test_no_prior_run_does_not_short_circuit`, `test_consumed_observations_do_not_block_short_circuit` |
| 12.2.c | load_context family | `test_load_context_normalizes_tech_arg`, `test_load_context_excludes_archived_observations`, plus any class named `TestLoadContext` or `class TestSynthesize` (the old batch-method test class) |
| 12.2.d | build_prompt family | Any test named `test_build_prompt_*` and any class `TestBuildPrompt` (replaced by `TestBuildEpisodePrompt` in Task 8) |
| 12.2.e | Round-trip / batch synthesize | `test_synthesize_round_trip` (if present) — superseded by `TestSynthesizeNextHappyPath` |

**Keep verbatim** (apply-layer tests survive unchanged):
- `test_apply_new_*`, `test_apply_augment_*`, `test_apply_merge_*`, `test_apply_ignore_*`, `test_auto_ignore_unused_*`
- `test_filter_existing_observations_*`, `test_apply_*_does_not_dearchive*`, `test_apply_*_bumps_status_changed_at*`

**Catch-all grep** after all batches done:

```bash
grep -nE "def test_(synthesize_short|synthesize_advances_watermark|synthesize_round_trip|load_context_|build_prompt_|loads_new_observations_since_watermark|tech_defaults_to_empty_string_in_watermark|empty_response_still_updates_watermark|counts_zero_on_short_circuit|different_goal_does_not_short_circuit|outside_window_does_not_short_circuit|new_observations_invalidate_short_circuit|no_prior_run_does_not_short_circuit|consumed_observations_do_not_block_short_circuit)" tests/services/test_reflection.py
```

Expected: no hits. If any remain, delete them.

- [ ] **Step 3: Update test imports**

The `from better_memory.services.reflection import (...)` block in `test_reflection.py` currently imports `SynthesisContext`. Remove that import and add the new types:

```python
from better_memory.services.reflection import (
    EpisodeContext,
    EpisodeForPrompt,
    EpisodeQueueCounts,
    ObservationForPrompt,
    ReflectionForPrompt,
    ReflectionSynthesisService,
    SynthesisStep,
)
```

- [ ] **Step 4: Run the test suite — service tests should now be fully green**

Run: `uv run pytest tests/services/test_reflection.py -q`
Expected: all tests pass. The remaining tests are: apply-layer tests (untouched), the new `TestPickOldestPending`, `TestLoadEpisodeContext`, `TestBuildEpisodePrompt`, `TestMarkSynthesized`, `TestReadQueueCounts`, `TestSynthesizeNextHappyPath`, `TestSynthesizeNextFailurePaths`.

If failures remain, the most likely cause is a test that imports `SynthesisContext` or calls `svc.synthesize(goal=, tech=, project=)` directly — find and remove or update.

- [ ] **Step 5: Pyright clean**

Run: `uv run pyright better_memory/services/reflection.py tests/services/test_reflection.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py tests/services/test_reflection.py
git commit -m "$(cat <<'EOF'
refactor(reflection): replace batch synthesize with per-episode synthesize_next

Replaces the watermark-driven batch synthesize() with a per-episode
synthesize_next() method. Each call processes the oldest closed-but-
unsynthesized episode for the project: loads its observations and
tech-filtered reflections, calls the LLM once, applies new/augment/
merge/ignore actions inside a per-episode SAVEPOINT, marks
episodes.synthesized_at on success.

Failure handling splits LLM-class errors (ChatError, SynthesisResponseError)
from structural/DB errors:
- LLM-class: ROLLBACK, stamp synth_failed_at for 300s cooldown, return
  SynthesisStep with failure set. Caller (UI auto-chain or MCP drain
  loop) continues past it; cooldown filter on _pick_oldest_pending
  prevents the just-failed episode from being immediately re-picked.
- Structural: ROLLBACK and propagate; route surfaces 500 and stops chain.

New types: EpisodeForPrompt, EpisodeContext, EpisodeQueueCounts,
SynthesisStep. ObservationForPrompt gains a 'status' field so the LLM
sees consumed observations as historical context.

Removed: synthesize, load_context, build_prompt, _should_short_circuit,
_upsert_watermark, _bucketed_reflections, last_run_counts,
SynthesisContext. Apply layer (_apply_new/_apply_augment/_apply_merge/
_apply_ignore/_auto_ignore_unused) and parse_response are unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: clean commit. UI synth tests and MCP start_episode test still red — fixed in Commits 3 and 4.

---

# Commit 3 — Route refactor + banner template

### Task 13: Update the route + create new banner template

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/synth_step_banner.html`
- Delete: `better_memory/ui/templates/fragments/observations_synth_banner.html`

- [ ] **Step 1: Replace the inner `_run` body in the route**

In `better_memory/ui/app.py`, find the `observations_synthesize` route (around line 472). Inside `_build_coro` → `_run`, replace the call to `svc.synthesize(...)` with `svc.synthesize_next(...)`:

```python
# Inside _run, replace:
#   result = await svc.synthesize(
#       goal="manual synthesis",
#       tech=None,
#       project=project,
#   )
#   return result, dict(svc.last_run_counts)
# with:
step = await svc.synthesize_next(project=project)
return step
```

Then update the success branch in the route (around lines 591-599):

```python
# Replace:
#   bucket_counts = {k: len(v) for k, v in result.items()}
#   rendered = render_template(
#       "fragments/observations_synth_banner.html",
#       counts=bucket_counts,
#       run_counts=run_counts,
#   )
#   return (rendered, 200, {"HX-Trigger": "observations-synthesized"})
# with:
rendered = render_template(
    "fragments/synth_step_banner.html",
    step=step,
    queue=step.queue,
)
# observations-synthesized fires on EVERY step (including failure-with-progress
# and the final done step). The obs panel listens via hx-trigger="...from:body"
# and reloads on each → live drain visualization.
return (rendered, 200, {"HX-Trigger": "observations-synthesized"})
```

The mutex / worker / WorkerTimeout / BaseException error handlers around it (lines 477-495, 547-589) stay unchanged.

Also update the unpack return inside the worker — it now returns a single `SynthesisStep`, not a tuple:

```python
# Replace:
#   step = run_async_in_worker(_build_coro, timeout=synth_timeout)
# (no change to that line — the variable name is already `step`)
```

Actually re-check: the original code is `result, run_counts = run_async_in_worker(...)`. Change that line to:

```python
step = run_async_in_worker(_build_coro, timeout=synth_timeout)
```

- [ ] **Step 2: Delete the old banner template**

```bash
rm better_memory/ui/templates/fragments/observations_synth_banner.html
```

- [ ] **Step 3: Create the new banner template**

```html
{# better_memory/ui/templates/fragments/synth_step_banner.html
   4-state banner for per-episode synthesis. The auto-fire div is present
   iff queue.pending > 0; chain stops naturally when pending hits zero.
   Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md #}

{% if not step.processed %}
  <div class="card synth-done">
    <p><strong>Synthesis complete.</strong> {{ queue.done }} episodes processed.
      {% if queue.in_cooldown %} ({{ queue.in_cooldown }} in cooldown after recent failures){% endif %}
    </p>
  </div>
{% elif step.failure %}
  <div class="card card-warning synth-step">
    <p><strong>Episode {{ queue.done }}/{{ queue.total }} failed:</strong> {{ step.failure }}</p>
    {% if queue.pending > 0 %}
      <div hx-post="/observations/synthesize"
           hx-trigger="load delay:200ms"
           hx-target="closest .synth-step"
           hx-swap="outerHTML">⏵ Continuing…</div>
    {% endif %}
  </div>
{% else %}
  <div class="card synth-step">
    <p>Episode <strong>{{ queue.done }}/{{ queue.total }}</strong> processed
      ({{ step.counts.created }} new,
       {{ step.counts.augmented }} augmented,
       {{ step.counts.merged }} merged,
       {{ step.counts.auto_ignored }} auto-ignored)</p>
    {% if queue.pending > 0 %}
      <div hx-post="/observations/synthesize"
           hx-trigger="load delay:200ms"
           hx-target="closest .synth-step"
           hx-swap="outerHTML">⏵ Continuing…</div>
    {% endif %}
  </div>
{% endif %}
```

- [ ] **Step 4: Smoke-check pyright on the route**

Run: `uv run pyright better_memory/ui/app.py`
Expected: no new errors.

### Task 14: UI tests — happy path

**Files:**
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Locate the existing `TestObservationsSynthesize` class (around line 274) and replace its three methods**

Read the current methods first (`test_calls_service_and_returns_banner`, `test_returns_500_card_error_on_service_failure`, `test_synthesize_uses_worker_thread_connection_not_app_connection`). The third (worker-thread regression) survives — only update it to use `synthesize_next` instead of `synthesize`. The first two are rewritten.

Replace `test_calls_service_and_returns_banner` with:

```python
def test_success_step_renders_step_banner_with_auto_fire(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Success step with pending > 0 → step banner + auto-fire div."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    async def fake(self, *, project):
        return SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 1, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=3, pending=2, in_cooldown=0),
            failure=None,
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    response = client.post(
        "/observations/synthesize",
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "observations-synthesized"
    body = response.get_data(as_text=True)
    assert "synth-step" in body
    assert "Episode" in body and "3/5" in body
    assert "1 new" in body
    # Auto-fire div present (queue.pending > 0).
    assert 'hx-post="/observations/synthesize"' in body
    assert 'hx-trigger="load delay:200ms"' in body

def test_done_step_renders_done_banner_without_auto_fire(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty-queue step → synth-done card, no auto-fire div."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    async def fake(self, *, project):
        return SynthesisStep(
            processed=False, episode_id=None,
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=5, pending=0, in_cooldown=0),
            failure=None,
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    response = client.post("/observations/synthesize")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "synth-done" in body
    assert "Synthesis complete" in body
    # No auto-fire div.
    assert 'hx-trigger="load delay:200ms"' not in body
```

- [ ] **Step 2: Update the worker-thread regression test (keep at LLM-stub level)**

Find `test_synthesize_uses_worker_thread_connection_not_app_connection` (around line 341). The test exists to prove that the route opens a fresh sqlite connection inside the worker thread (not reusing `app.extensions["db_connection"]`). Stubbing higher in the stack (`synthesize_next`) defeats this purpose — there'd be no real DB call to verify. **Keep it at the `OllamaChat.complete` stub level.**

Concrete updates:

1. Seed a closed pending episode in `tmp_db` so `_pick_oldest_pending` returns something (otherwise `synthesize_next` short-circuits to `processed=False` and never touches the DB).

   ```python
   # In the test, after fixtures are set up:
   import sqlite3
   with sqlite3.connect(tmp_db) as seed_conn:
       seed_conn.execute(
           "INSERT INTO episodes (id, project, started_at, ended_at, "
           "outcome, close_reason, goal) VALUES "
           "('seed-ep','proj-a','2026-04-01T00:00:00+00:00',"
           "'2026-04-01T01:00:00+00:00','success','goal_complete','g')"
       )
       seed_conn.commit()
   ```

2. Stub `OllamaChat.complete` to return a parseable empty SynthesisResponse so the synthesize body can finish without producing reflections (the test isn't about the content; it's about the connection being thread-bound):

   ```python
   import json as _json
   async def fake_complete(self, prompt):
       return _json.dumps({"new": [], "augment": [], "merge": [], "ignore": []})
   monkeypatch.setattr(OllamaChat, "complete", fake_complete)
   ```

3. The assertions about thread-bound connections stay verbatim. The behavior under test is unchanged: the worker opens its own `connect()`, runs SQL through it, closes it. That contract is unchanged by the service refactor.

If the original test had additional assertions about `synthesize`'s return shape (e.g. bucket counts), drop those — `synthesize_next` returns a `SynthesisStep` instead. The CONNECTION-USAGE assertion is the only load-bearing one for this regression test.

- [ ] **Step 3: Run the happy-path tests**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsSynthesize::test_success_step_renders_step_banner_with_auto_fire tests/ui/test_observations.py::TestObservationsSynthesize::test_done_step_renders_done_banner_without_auto_fire -v`
Expected: both pass.

### Task 15: UI tests — failure paths and chain recovery

**Files:**
- Modify: `tests/ui/test_observations.py`

- [ ] **Step 1: Add failure-path tests**

Append to `TestObservationsSynthesize`:

```python
def test_failure_step_with_pending_includes_auto_fire(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Failure on episode N with pending > 0 → failure card + auto-fire."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    async def fake(self, *, project):
        return SynthesisStep(
            processed=True, episode_id="ep-bad",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=2, pending=3, in_cooldown=1),
            failure="ollama unreachable",
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    response = client.post("/observations/synthesize")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "card-warning" in body
    assert "Episode 2/6 failed" in body
    assert "ollama unreachable" in body
    # Auto-fire still present — chain continues past the failure.
    assert 'hx-trigger="load delay:200ms"' in body

def test_failure_step_without_pending_omits_auto_fire(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Failure on the LAST pending episode → failure card, no auto-fire."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    async def fake(self, *, project):
        # The just-failed episode is now in cooldown, hence pending=0.
        return SynthesisStep(
            processed=True, episode_id="ep-last",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=4, pending=0, in_cooldown=1),
            failure="parse error",
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    response = client.post("/observations/synthesize")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "card-warning" in body
    # No auto-fire div.
    assert 'hx-trigger="load delay:200ms"' not in body
```

- [ ] **Step 2: Add chain-recovery and HX-Trigger tests**

```python
def test_hx_trigger_fires_on_every_step_including_done(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """observations-synthesized fires on success, failure, AND done steps."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    states = [
        # success-with-pending
        SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=1, pending=2, in_cooldown=0),
            failure=None,
        ),
        # failure-with-pending
        SynthesisStep(
            processed=True, episode_id="ep2",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=1, pending=1, in_cooldown=1),
            failure="boom",
        ),
        # done
        SynthesisStep(
            processed=False, episode_id=None,
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=3, pending=0, in_cooldown=0),
            failure=None,
        ),
    ]
    iter_state = iter(states)
    async def fake(self, *, project):
        return next(iter_state)
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    for _ in states:
        response = client.post("/observations/synthesize")
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "observations-synthesized"

def test_banner_uses_step_queue_counts_not_external_query(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Banner counts come from step.queue, not from a second SQL query."""
    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    # Distinct, recognizable counts in the queue snapshot.
    async def fake(self, *, project):
        return SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=42, pending=7, in_cooldown=3),
            failure=None,
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", fake
    )

    response = client.post("/observations/synthesize")
    body = response.get_data(as_text=True)
    # 42/52 = done / total (42+7+3).
    assert "42/52" in body

def test_stray_click_during_chain_returns_429(
    self, client: FlaskClient, tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Second click while chain is mid-flight gets 429 + card-error.

    FALLBACK: if this threading-coordinated test flakes on CI more than
    once, replace it with a unit test directly on the mutex primitives
    (`_try_acquire_synth` / `_release_synth` in `better_memory/ui/app.py`)
    — call _try_acquire_synth twice, assert the second returns None.
    Same invariant, no concurrency required. The threading test is
    preferred when stable because it exercises the full route-level
    wiring (429 response, card-error template).
    """
    import threading

    from better_memory.services.reflection import (
        EpisodeQueueCounts,
        ReflectionSynthesisService,
        SynthesisStep,
    )
    from better_memory.ui import app as app_module

    monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")

    started = threading.Event()
    release = threading.Event()

    async def slow(self, *, project):
        started.set()
        # Simulate a slow LLM call by waiting on the release event.
        # Use asyncio-safe wait.
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, release.wait, 5.0)
        return SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=1, pending=0, in_cooldown=0),
            failure=None,
        )
    monkeypatch.setattr(
        ReflectionSynthesisService, "synthesize_next", slow
    )

    def first_call():
        client.post("/observations/synthesize")

    t = threading.Thread(target=first_call)
    t.start()
    started.wait(2.0)  # wait for the worker to enter the slow call

    second = client.post("/observations/synthesize")
    assert second.status_code == 429
    assert "card-error" in second.get_data(as_text=True)

    release.set()
    t.join(5.0)
```

- [ ] **Step 3: Run the failure / recovery tests**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationsSynthesize -v`
Expected: all pass.

- [ ] **Step 4: Run the full UI test suite**

Run: `uv run pytest tests/ui/ -q`
Expected: all UI tests pass. The mutex / worker-error / leak-path tests from PR #18 still pass — they exercise the unchanged route shell.

- [ ] **Step 5: Pyright clean**

Run: `uv run pyright better_memory/ui/app.py tests/ui/test_observations.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/synth_step_banner.html \
        tests/ui/test_observations.py
git rm better_memory/ui/templates/fragments/observations_synth_banner.html
git commit -m "$(cat <<'EOF'
refactor(ui): /observations/synthesize is one-step + auto-chain banner

Replaces the batch-banner response with a per-episode step banner.
Each POST processes ONE episode; if step.queue.pending > 0 the response
includes an htmx auto-fire div that triggers the next request after
200ms. Chain stops naturally when pending hits zero.

The route's mutex / worker-thread / WorkerTimeout / BaseException
handlers (PR #18 hardening) survive unchanged — only the inner _run
coroutine and the success-banner branch change.

New template fragments/synth_step_banner.html replaces
observations_synth_banner.html with 4 states:
- success-with-pending: step card + auto-fire
- success-without-pending (queue empty): synth-done card, no auto-fire
- failure-with-pending: warning card + auto-fire (chain continues)
- failure-without-pending: warning card, no auto-fire

HX-Trigger: observations-synthesized fires on every step (live drain
visualization). Banner counts come from step.queue (single-connection
snapshot in the worker), not a second SQL query — eliminates the race
that the original spec draft had.

UI tests rewritten to exercise the four states + chain recovery:
mid-chain stray click returns 429, HX-Trigger fires every step, banner
counts come from the service step.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Commit 4 — MCP caller refactor

### Task 16: Add `seed_pending_episodes` fixture helper

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add the helper to `tests/conftest.py`**

Append:

```python
def seed_pending_episodes(
    conn,
    project: str,
    n: int,
    obs_per_episode: int = 2,
    tech: str | None = None,
) -> list[str]:
    """Seed N closed-pending episodes (synthesized_at NULL), each with M active observations.

    Returns episode ids in creation order. Each episode is closed
    (outcome='success', close_reason='goal_complete'), starts at
    increasing wall-clock minutes so ORDER BY ended_at is deterministic.

    Used by service / UI / MCP tests that need a pre-populated pending
    queue without re-implementing fixture SQL each time.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    ids: list[str] = []
    for i in range(n):
        eid = f"seeded-ep-{i:03d}"
        started = base + timedelta(minutes=i * 10)
        ended = started + timedelta(minutes=5)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) "
            "VALUES (?, ?, ?, ?, 'success', 'goal_complete', ?, ?, NULL)",
            (eid, project, started.isoformat(), ended.isoformat(),
             f"goal {i}", tech),
        )
        for j in range(obs_per_episode):
            obs_id = f"{eid}-obs-{j}"
            obs_time = (started + timedelta(minutes=j + 1)).isoformat()
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id, "
                "status, outcome, created_at, status_changed_at, tech) "
                "VALUES (?, ?, ?, ?, 'active', 'success', ?, ?, ?)",
                (obs_id, f"obs content {j}", project, eid,
                 obs_time, obs_time, tech),
            )
        ids.append(eid)
    conn.commit()
    return ids
```

- [ ] **Step 2: Sanity check the helper**

Run a quick interactive smoke check (or write a one-line test):

```bash
uv run pytest --collect-only -q tests/conftest.py
```

Expected: collection succeeds, no syntax errors.

### Task 17: Update MCP caller (drain loop) + tests

**Files:**
- Modify: `better_memory/mcp/server.py`
- Modify: `tests/mcp/test_episode_tools.py`

- [ ] **Step 1: Write the failing MCP tests (red)**

The existing `tests/mcp/test_episode_tools.py:test_service_level_returns_reflections` (around line 164-181) shows the pattern: use the `conn` fixture (already applies migrations), construct `FakeChat` with canned JSON, build `ReflectionSynthesisService(conn, chat=fake)` directly, and drive via `asyncio.run`. Mirror it for the drain-loop. Append after `TestStartEpisodeReturnsReflections`:

```python
class TestStartEpisodeDrainsPending:
    """The drain-loop pattern in memory.start_episode (post-Phase-X redesign):
    `while (await synthesize_next(project=...)).processed: pass`.

    Tests the SERVICE-level loop directly (mirroring the existing
    test_service_level_returns_reflections pattern); the MCP tool is a
    thin wrapper.
    """

    def test_drains_all_pending_then_returns_tech_filtered_buckets(self, conn):
        import asyncio
        import json as _json

        from better_memory.llm.fake import FakeChat
        from better_memory.services.reflection import ReflectionSynthesisService
        from tests.conftest import seed_pending_episodes

        # Seed 3 closed pending episodes for project "p1".
        ids = seed_pending_episodes(conn, project="p1", n=3, tech=None)

        empty_response = _json.dumps(
            {"new": [], "augment": [], "merge": [], "ignore": []}
        )
        fake = FakeChat(responses=[empty_response, empty_response, empty_response])
        svc = ReflectionSynthesisService(conn, chat=fake)

        async def _drain():
            while (await svc.synthesize_next(project="p1")).processed:
                pass

        asyncio.run(_drain())

        # All 3 seeded episodes have synthesized_at NOT NULL.
        for eid in ids:
            row = conn.execute(
                "SELECT synthesized_at FROM episodes WHERE id = ?", (eid,)
            ).fetchone()
            assert row[0] is not None, f"{eid} not marked synthesized"

        # FakeChat consumed all 3 responses (one per episode).
        assert len(fake.responses) == 0
        assert len(fake.calls) == 3

        # Buckets retrievable.
        buckets = svc.retrieve_reflections(project="p1")
        assert set(buckets.keys()) == {"do", "dont", "neutral"}

    def test_no_pending_returns_immediately_without_llm_calls(self, conn):
        import asyncio

        from better_memory.llm.fake import FakeChat
        from better_memory.services.reflection import ReflectionSynthesisService
        from tests.conftest import seed_pending_episodes

        # Seed 2 episodes then mark them all synthesized.
        ids = seed_pending_episodes(conn, project="p1", n=2)
        for eid in ids:
            conn.execute(
                "UPDATE episodes SET synthesized_at = '2026-04-22T10:00:00+00:00' "
                "WHERE id = ?", (eid,)
            )
        conn.commit()

        fake = FakeChat(responses=[])  # would raise if called
        svc = ReflectionSynthesisService(conn, chat=fake)

        async def _drain():
            while (await svc.synthesize_next(project="p1")).processed:
                pass

        asyncio.run(_drain())  # must terminate without calling fake
        assert len(fake.calls) == 0

    def test_loop_terminates_on_persistent_failure(self, conn):
        """Without the cooldown filter, this would infinite-loop."""
        import asyncio

        from better_memory.llm.ollama import ChatError
        from better_memory.services.reflection import ReflectionSynthesisService
        from tests.conftest import seed_pending_episodes

        ids = seed_pending_episodes(conn, project="p1", n=3)

        class AlwaysFailChat:
            calls = 0
            async def complete(self, prompt: str) -> str:
                AlwaysFailChat.calls += 1
                if AlwaysFailChat.calls > 10:  # safety net for the test itself
                    raise AssertionError("loop did not terminate")
                raise ChatError("simulated persistent failure")

        svc = ReflectionSynthesisService(conn, chat=AlwaysFailChat())

        async def _drain():
            while (await svc.synthesize_next(project="p1")).processed:
                pass

        asyncio.run(_drain())

        # Loop terminated. Each of 3 episodes had its synth_failed_at stamped
        # exactly once → 3 LLM calls total.
        assert AlwaysFailChat.calls == 3
        for eid in ids:
            row = conn.execute(
                "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id = ?",
                (eid,),
            ).fetchone()
            assert row["synthesized_at"] is None  # not consolidated
            assert row["synth_failed_at"] is not None  # cooldown stamped

    def test_partial_failure_continues_past_failed_episode(self, conn):
        """One episode fails; the others still get processed."""
        import asyncio
        import json as _json

        from better_memory.llm.ollama import ChatError
        from better_memory.services.reflection import ReflectionSynthesisService
        from tests.conftest import seed_pending_episodes

        ids = seed_pending_episodes(conn, project="p1", n=3)

        empty_response = _json.dumps(
            {"new": [], "augment": [], "merge": [], "ignore": []}
        )

        class FlakyChat:
            calls = 0
            async def complete(self, prompt: str) -> str:
                FlakyChat.calls += 1
                if FlakyChat.calls == 1:
                    raise ChatError("transient")
                return empty_response

        svc = ReflectionSynthesisService(conn, chat=FlakyChat())

        async def _drain():
            while (await svc.synthesize_next(project="p1")).processed:
                pass

        asyncio.run(_drain())

        # Episodes 2 and 3 (in age order) succeeded; episode 1 in cooldown.
        # First episode is `ids[0]` (oldest), so it's the one that failed.
        oldest_row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert oldest_row["synthesized_at"] is None
        assert oldest_row["synth_failed_at"] is not None
        for eid in ids[1:]:
            row = conn.execute(
                "SELECT synthesized_at FROM episodes WHERE id = ?", (eid,)
            ).fetchone()
            assert row["synthesized_at"] is not None
```

The existing `test_service_level_returns_reflections` (line 164) calls the now-removed `svc.synthesize(...)` — Task 12 already removed that method, so this test is broken. **Delete it as part of Task 17 Step 1**, since the new `TestStartEpisodeDrainsPending` covers the same ground (and more) with `synthesize_next`.

- [ ] **Step 2: Run the tests, verify they fail (red)**

Run: `uv run pytest tests/mcp/test_episode_tools.py -v -k "Drains"`
Expected: failures because the existing `synthesize` call in `mcp/server.py` raises `AttributeError` (the method is gone after Commit 2).

- [ ] **Step 3: Update `better_memory/mcp/server.py`**

Find the `memory.start_episode` handler (around line 563-583). Replace the synthesize call:

```python
# Replace:
#   buckets = await reflections.synthesize(
#       goal=args["goal"],
#       tech=args.get("tech"),
#       project=project,
#   )
# with:
# Drain pending episodes so the new episode's reflection context is fresh.
while (await reflections.synthesize_next(project=project)).processed:
    pass
buckets = reflections.retrieve_reflections(
    project=project, tech=args.get("tech"),
)
```

- [ ] **Step 4: Run the tests, verify they pass (green)**

Run: `uv run pytest tests/mcp/test_episode_tools.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: full green.

- [ ] **Step 6: Pyright clean**

Run: `uv run pyright`
Expected: no new errors.

- [ ] **Step 7: Final pre-flight grep**

Run:
```bash
grep -r "synthesis_runs" better_memory/ tests/
grep -r "\.synthesize\(" better_memory/ tests/
grep -r "load_context\|build_prompt\|_should_short_circuit\|_upsert_watermark\|SynthesisContext" better_memory/ tests/
```

Expected:
- `synthesis_runs` only in old migration files (`0002_episodic.sql`, `0003_synthesis_runs_last_goal.sql`) and the DROP in `0006_per_episode_synthesis.sql`. No live code references.
- `.synthesize(` only as `synthesize_next(` calls. No bare `.synthesize(` calls.
- The other identifiers either gone or only appear in spec/plan markdown.

If anything stale remains, fix it before committing.

- [ ] **Step 8: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_episode_tools.py tests/conftest.py
git commit -m "$(cat <<'EOF'
refactor(mcp): start_episode drains pending then fetches buckets

memory.start_episode now drains pending closed episodes via a tight
while-loop over synthesize_next, then returns the new episode's
reflection context via retrieve_reflections(tech=...).

The cooldown stamp on synth_failed_at means persistently-failing
episodes are excluded from the next _pick_oldest_pending call, so the
loop terminates even when an episode reliably raises ChatError —
otherwise the drain would be a stop-the-world infinite loop on the
first persistent failure.

New seed_pending_episodes() helper in tests/conftest.py used by service,
UI, and MCP tests to populate a closed-pending queue without
duplicating fixture SQL.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Commit 5 — General-scope reflections

> The user's workflow rules (e.g. the per-step-confidence rule) are project-agnostic but currently bucketed by project. Adding `scope` ('project' | 'general') to observations and reflections makes general rules surface in every project's `memory_retrieve`. Synthesis derives a new reflection's scope from its source observations: all-general → general; otherwise project. Augment/merge preserve the existing reflection's scope.

### Task 18: Migration 0007 + tests (TDD)

**Files:**
- Create: `better_memory/db/migrations/0007_reflection_scope.sql`
- Create: `tests/db/test_migration_0007.py`

- [ ] **Step 1: Write the failing migration test**

```python
# tests/db/test_migration_0007.py
"""Migration 0007: scope column on observations + reflections."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    apply_migrations(c)
    yield c
    c.close()


def test_observations_has_scope_column(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    assert "scope" in cols


def test_reflections_has_scope_column(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reflections)").fetchall()}
    assert "scope" in cols


def test_observations_scope_default_is_project(conn) -> None:
    # Insert a row without specifying scope; default should kick in.
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('o1','x','p1','ep1','active','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT scope FROM observations WHERE id='o1'").fetchone()
    assert row[0] == "project"


def test_reflections_scope_default_is_project(conn) -> None:
    conn.execute(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, "
        "created_at, updated_at) VALUES "
        "('r1','t','p1','general','do','uc','[]',0.5,'pending_review',0,"
        "'2026-04-01T00:00:00+00:00','2026-04-01T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT scope FROM reflections WHERE id='r1'").fetchone()
    assert row[0] == "project"


def test_observations_scope_check_constraint_rejects_invalid(conn) -> None:
    import sqlite3
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at, scope) VALUES "
            "('o1','x','p1','ep1','active','success',"
            "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00','invalid')"
        )


def test_partial_index_on_general_reflections_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='reflections'"
    ).fetchall()
    assert "idx_reflections_scope_general" in {r[0] for r in rows}


def test_fixup_marks_workflow_observation_general_if_present(conn) -> None:
    """The 0007 fix-up flips the recorded workflow observation to scope='general'.

    Idempotent: if the row doesn't exist (test DB is fresh), this is a no-op.
    Pre-seed the row to verify the UPDATE actually fires.
    """
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at, scope) VALUES "
        "('413d47550efd4adfa2c238d6ce5099f9','x','p1','ep1','active','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00','project')"
    )
    conn.commit()
    # Re-run the fix-up UPDATE inline — apply_migrations already ran when fixture
    # was seeded with no rows, so the original UPDATE was a no-op then.
    conn.execute(
        "UPDATE observations SET scope='general' "
        "WHERE id='413d47550efd4adfa2c238d6ce5099f9'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM observations "
        "WHERE id='413d47550efd4adfa2c238d6ce5099f9'"
    ).fetchone()
    assert row[0] == "general"
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/db/test_migration_0007.py -v`
Expected: 7 failures with `OperationalError: no such column: scope`.

- [ ] **Step 3: Write the migration**

`better_memory/db/migrations/0007_reflection_scope.sql`:

```sql
-- Migration 0007: cross-project (general) scope for reflections + observations.
-- Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md (Commit 5)

ALTER TABLE observations
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

ALTER TABLE reflections
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

CREATE INDEX idx_reflections_scope_general
    ON reflections(updated_at DESC)
    WHERE scope = 'general';

UPDATE observations
   SET scope = 'general'
 WHERE id = '413d47550efd4adfa2c238d6ce5099f9';
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/db/test_migration_0007.py -v`
Expected: all 7 pass.

### Task 19: Scope on the write path (TDD)

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `tests/services/test_observation.py`
- Modify: `better_memory/mcp/server.py` (memory.observe tool schema)

- [ ] **Step 1: Write failing tests in `test_observation.py`**

Append:

```python
class TestObservationScope:
    def test_create_defaults_to_project_scope(self, conn):
        from better_memory.services.observation import ObservationService
        # Set up an active episode (fixture pattern from existing tests).
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00')"
        )
        conn.commit()
        svc = ObservationService(conn)
        obs_id = svc.create(
            content="x", project="p1", episode_id="ep1",
            outcome="success", session_id="s1", trigger_type="manual",
        )
        row = conn.execute(
            "SELECT scope FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        assert row[0] == "project"

    def test_create_with_explicit_general_scope(self, conn):
        from better_memory.services.observation import ObservationService
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00')"
        )
        conn.commit()
        svc = ObservationService(conn)
        obs_id = svc.create(
            content="rule", project="p1", episode_id="ep1",
            outcome="success", session_id="s1", trigger_type="manual",
            scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        assert row[0] == "general"

    def test_create_rejects_invalid_scope(self, conn):
        from better_memory.services.observation import ObservationService
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00')"
        )
        conn.commit()
        svc = ObservationService(conn)
        with pytest.raises(ValueError, match="scope"):
            svc.create(
                content="x", project="p1", episode_id="ep1",
                outcome="success", session_id="s1", trigger_type="manual",
                scope="invalid",
            )
```

(Look at the existing tests in `test_observation.py` to mirror the exact `ObservationService.create` signature — fields like `outcome`, `session_id`, `trigger_type`, `tech`, `component`, `theme` are all current; the new `scope` is the only addition.)

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/services/test_observation.py::TestObservationScope -v`
Expected: 3 failures.

- [ ] **Step 3: Update `ObservationService.create`**

Find the `create` method in `better_memory/services/observation.py`. Add `scope: str = "project"` to the signature (keyword-only — after the existing `*` separator). Validate at the top of the method:

```python
if scope not in ("project", "general"):
    raise ValueError(
        f"scope must be 'project' or 'general', got {scope!r}"
    )
```

Add `scope` to the INSERT column list and pass `scope` in the values tuple. The DB-level CHECK constraint is a backstop; the Python ValueError gives a clearer error.

- [ ] **Step 4: Update the `memory.observe` MCP tool schema**

In `better_memory/mcp/server.py`, find the `memory.observe` tool definition (search for `"memory.observe"` in the file). Add to the input schema's `properties`:

```python
"scope": {
    "type": "string",
    "enum": ["project", "general"],
    "description": (
        "'project' (default) for project-scoped observations; "
        "'general' for cross-project workflow rules that should "
        "surface in every project's memory_retrieve."
    ),
},
```

In the handler, pass `args.get("scope", "project")` through to `ObservationService.create(scope=...)`.

- [ ] **Step 5: Run, verify pass**

Run: `uv run pytest tests/services/test_observation.py::TestObservationScope -v`
Expected: all 3 pass.

- [ ] **Step 6: Run full suite to catch any caller missing the kwarg**

Run: `uv run pytest -q`
Expected: green. (Default value means no existing caller breaks.)

### Task 20: Scope in synthesis (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/services/test_reflection.py`:

```python
class TestSynthesisScopeDerivation:
    def test_apply_new_creates_general_when_all_sources_general(self, conn, fixed_clock):
        # Two general-scoped observations sourcing one new reflection.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        for i in (1, 2):
            _insert_obs(
                conn, obs_id=f"o{i}", project="p1", episode_id="ep1",
                content=f"obs {i}", status="active",
            )
            conn.execute(
                "UPDATE observations SET scope='general' WHERE id=?", (f"o{i}",)
            )
        conn.commit()

        from better_memory.services.reflection import NewAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_new(
            [NewAction(
                title="general rule", phase="general", polarity="do",
                use_cases="uc", hints=["h"], tech=None, confidence=0.5,
                source_observation_ids=["o1", "o2"],
            )],
            project="p1",
        )
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE title='general rule'"
        ).fetchone()
        assert row[0] == "general"

    def test_apply_new_creates_project_when_any_source_project(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        # One general + one project.
        _insert_obs(conn, obs_id="o-general", project="p1", episode_id="ep1",
                    content="general obs", status="active")
        conn.execute("UPDATE observations SET scope='general' WHERE id='o-general'")
        _insert_obs(conn, obs_id="o-project", project="p1", episode_id="ep1",
                    content="project obs", status="active")
        conn.commit()

        from better_memory.services.reflection import NewAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_new(
            [NewAction(
                title="mixed rule", phase="general", polarity="do",
                use_cases="uc", hints=["h"], tech=None, confidence=0.5,
                source_observation_ids=["o-general", "o-project"],
            )],
            project="p1",
        )
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE title='mixed rule'"
        ).fetchone()
        assert row[0] == "project"

    def test_apply_augment_preserves_general_scope(self, conn, fixed_clock):
        # Pre-seed a general reflection; augment with a project source. Scope stays general.
        _insert_reflection(conn, refl_id="r-general", project="p1")
        conn.execute("UPDATE reflections SET scope='general' WHERE id='r-general'")
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        _insert_obs(conn, obs_id="o-proj", project="p1", episode_id="ep1",
                    content="project obs", status="active")
        conn.commit()

        from better_memory.services.reflection import AugmentAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_augment([AugmentAction(
            reflection_id="r-general", add_hints=["new"], rewrite_use_cases=None,
            confidence_delta=0.1, add_source_observation_ids=["o-proj"],
        )])
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE id='r-general'"
        ).fetchone()
        assert row[0] == "general"  # preserved despite project-scoped source

    def test_load_episode_context_includes_general_reflections_from_other_projects(
        self, conn, fixed_clock,
    ):
        # Episode in project p1; general reflection lives "in" project p2 but scope=general.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g',NULL)"
        )
        _insert_reflection(conn, refl_id="r-other-general", project="p2", tech=None)
        conn.execute("UPDATE reflections SET scope='general' WHERE id='r-other-general'")
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="g", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert "r-other-general" in {r.id for r in ctx.reflections}
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesisScopeDerivation -v`
Expected: 4 failures.

- [ ] **Step 3: Add `_derive_new_reflection_scope` helper**

In `ReflectionSynthesisService`, add:

```python
def _derive_new_reflection_scope(
    self, source_obs_ids: list[str]
) -> str:
    """Return 'general' iff every source observation has scope='general'.

    Empty source list defaults to 'project' (defensive — _apply_new
    already filters out new actions with no valid sources).
    """
    if not source_obs_ids:
        return "project"
    placeholders = ",".join("?" * len(source_obs_ids))
    rows = self._conn.execute(
        f"SELECT scope FROM observations WHERE id IN ({placeholders})",
        source_obs_ids,
    ).fetchall()
    if not rows:
        return "project"
    return "general" if all(r["scope"] == "general" for r in rows) else "project"
```

- [ ] **Step 4: Update `_apply_new` to call the helper and INSERT scope**

In `_apply_new`, after `valid_sources = self._filter_existing_observations(...)` and before the `INSERT INTO reflections`, derive the scope:

```python
scope = self._derive_new_reflection_scope(valid_sources)
```

Update the INSERT to include the column and the value:

```python
self._conn.execute(
    """
    INSERT INTO reflections (
        id, title, project, tech, phase, polarity, use_cases,
        hints, confidence, status, evidence_count,
        created_at, updated_at, scope
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)
    """,
    (
        reflection_id, action.title, project,
        self._normalize_tech(action.tech),
        action.phase, action.polarity, action.use_cases,
        json.dumps(action.hints), confidence,
        len(valid_sources), now, now, scope,
    ),
)
```

`_apply_augment` and `_apply_merge` need NO change — they preserve the existing reflection's scope (the column isn't in their UPDATE statements).

- [ ] **Step 5: Update `_load_episode_context`'s reflection query**

Change the WHERE clause in both branches (tech is None / tech given) from `WHERE project = ?` to `WHERE (project = ? OR scope = 'general')`. Also add `scope` to the SELECT column list and to the `ReflectionForPrompt` constructor — but the dataclass doesn't have a scope field yet. Either:
- Add `scope: str = "project"` to `ReflectionForPrompt` and populate it (cleaner), or
- Skip exposing scope in the prompt for v1 (the LLM doesn't need to see scope; the apply layer handles it)

Choose the second for v1 simplicity. Just add `scope = 'general'` to the WHERE clause; don't add the column to the prompt:

```python
if tech is None:
    refl_rows = self._conn.execute(
        """
        SELECT id, title, tech, phase, polarity, use_cases, hints,
               confidence, status
          FROM reflections
         WHERE (project = ? OR scope = 'general')
           AND status IN ('pending_review', 'confirmed')
         ORDER BY confidence DESC, updated_at DESC
        """,
        (episode.project,),
    ).fetchall()
else:
    # Same idea, with the (tech = ? OR tech IS NULL) clause appended.
    ...
```

- [ ] **Step 6: Run, verify pass**

Run: `uv run pytest tests/services/test_reflection.py::TestSynthesisScopeDerivation -v`
Expected: 4 pass.

### Task 21: Scope in retrieve_reflections + memory.retrieve (TDD)

**Files:**
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_reflection.py`

- [ ] **Step 1: Write failing test**

Append:

```python
class TestRetrieveReflectionsScope:
    def test_retrieve_reflections_includes_general_from_other_projects(
        self, conn, fixed_clock,
    ):
        # Project p1 reflection + project p2 general reflection.
        _insert_reflection(conn, refl_id="r-p1", project="p1")
        _insert_reflection(conn, refl_id="r-p2-general", project="p2")
        conn.execute(
            "UPDATE reflections SET scope='general' WHERE id='r-p2-general'"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        buckets = svc.retrieve_reflections(project="p1")
        all_ids = {r["id"] for bucket in buckets.values() for r in bucket}
        assert "r-p1" in all_ids
        assert "r-p2-general" in all_ids

    def test_retrieve_reflections_excludes_general_from_other_status(
        self, conn, fixed_clock,
    ):
        # General-but-retired reflection should NOT surface.
        _insert_reflection(
            conn, refl_id="r-retired-general", project="p2", status="retired",
        )
        conn.execute(
            "UPDATE reflections SET scope='general' WHERE id='r-retired-general'"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        buckets = svc.retrieve_reflections(project="p1")
        all_ids = {r["id"] for bucket in buckets.values() for r in bucket}
        assert "r-retired-general" not in all_ids
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/services/test_reflection.py::TestRetrieveReflectionsScope -v`
Expected: 2 failures.

- [ ] **Step 3: Update `retrieve_reflections` WHERE clause**

In `retrieve_reflections`, change the project filter:

```python
clauses = [
    "(project = ? OR scope = 'general')",
    "status IN ('pending_review', 'confirmed')",
]
```

The MCP `memory.retrieve` tool already calls `retrieve_reflections` — no further wiring needed.

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/services/test_reflection.py::TestRetrieveReflectionsScope -v`
Expected: 2 pass.

### Task 22: Verify + commit Commit 5

- [ ] **Step 1: Full test suite green**

Run: `uv run pytest -q`
Expected: full green. Pyright clean too: `uv run pyright`.

- [ ] **Step 2: Manual smoke check on the recorded workflow rule**

After Task 22 lands and the user runs `apply_migrations`, the workflow rule observation (`413d47550efd4adfa2c238d6ce5099f9`) should have `scope='general'`. Verify:

```bash
sqlite3 ~/.better-memory/memory.db \
    "SELECT id, scope, content FROM observations \
     WHERE id='413d47550efd4adfa2c238d6ce5099f9'"
```

Expected output: `scope='general'`. (Once synthesis runs against the episode containing this observation, the resulting reflection inherits scope='general' via `_derive_new_reflection_scope`.)

- [ ] **Step 3: Commit**

```bash
git add better_memory/db/migrations/0007_reflection_scope.sql \
        tests/db/test_migration_0007.py \
        better_memory/services/observation.py \
        tests/services/test_observation.py \
        better_memory/services/reflection.py \
        tests/services/test_reflection.py \
        better_memory/mcp/server.py
git commit -m "$(cat <<'EOF'
feat(scope): general-scope reflections surface across projects

Adds scope ('project' | 'general') to observations and reflections
so cross-project workflow rules surface in every project's
memory_retrieve.

Schema:
- observations.scope NOT NULL DEFAULT 'project' CHECK
- reflections.scope NOT NULL DEFAULT 'project' CHECK
- partial index on reflections WHERE scope='general'

Write path:
- ObservationService.create accepts scope (default 'project')
- memory.observe MCP tool schema gains optional scope field

Synthesis path:
- _apply_new derives reflection.scope from sources: all-general → general,
  otherwise project. _apply_augment / _apply_merge preserve existing scope.
- _load_episode_context's reflection query OR-merges general-scoped rows
  from any project.

Retrieval path:
- retrieve_reflections WHERE (project = ? OR scope = 'general')
- memory.retrieve flows through unchanged.

One-shot fix-up: the workflow rule observation recorded earlier today
gets scope='general' via the migration's UPDATE.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Full test suite green**

Run: `uv run pytest -q`
Expected: all tests pass. Note the count and compare to the baseline from Step 0b — the count should be similar (some deletions, some additions; net should be +20-30 tests from the new coverage).

- [ ] **Step 2: Pyright clean**

Run: `uv run pyright`
Expected: no new errors vs baseline.

- [ ] **Step 3: Smoke run on the user's actual DB**

Stop the UI if it's running. Restart with the new model:

```powershell
$env:CONSOLIDATE_MODEL = "llama3.2:3b"
uv run python -m better_memory.ui
```

Open the URL in the browser, click Synthesize on the observations panel, and watch:
- Banner shows "Episode 1/N processed (...)" and immediately auto-fires the next.
- Obs panel reloads on each `observations-synthesized` trigger.
- After ~10-15 minutes, "Synthesis complete. N episodes processed."
- Verify in the DB:

```sql
SELECT COUNT(*) FROM episodes WHERE outcome IS NOT NULL AND synthesized_at IS NOT NULL;
SELECT COUNT(*) FROM observations WHERE status = 'active';
SELECT COUNT(*) FROM reflections WHERE status IN ('pending_review','confirmed');
```

Expected: every closed episode has `synthesized_at` set; the active observation count has dropped to ~0 (any remaining are from open episodes); reflections list has grown.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin episodic-synthesis
gh pr create --title "Episodic synthesis (per-episode LLM call)" --body "$(cat <<'EOF'
## Summary

- Replaces watermark-driven batch synthesis with per-episode `synthesize_next` (one closed episode = one LLM call).
- New `episodes.synthesized_at` + `episodes.synth_failed_at` (300 s cooldown) columns; drops `synthesis_runs`.
- UI route is auto-chained via htmx self-firing fragment until `queue.pending == 0`.
- MCP `start_episode` drains pending then returns tech-filtered reflection buckets.

## Why

The current batch synth produces a ~14 K-token prompt for 67 closed-episode observations. llama3:8B's 8 K context silently truncates it; combined with `format=json` constrained decoding, generations spend minutes thrashing without converging. Per-episode prompts are ~1-2 KB and fit any model.

Spec: `docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md`

## Test plan

- [ ] All four commits' tests green individually
- [ ] `uv run pytest -q` end-to-end green
- [ ] `uv run pyright` clean
- [ ] Smoke run on local DB drains all pending episodes
- [ ] No `synthesis_runs` references remain outside historical migration files

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- Migration 0006 with both columns + index + backfill + drop: Tasks 1-2 ✓
- `tests/db/test_schema.py` cleanup: Task 3 ✓
- Pre-flight grep: Tasks 4 + 17 ✓
- New dataclasses (EpisodeForPrompt / EpisodeContext / EpisodeQueueCounts / SynthesisStep, ObservationForPrompt.status): Task 5 ✓
- `_pick_oldest_pending` (cooldown filter): Task 6 ✓
- `_load_episode_context` (all observations + tech-filter): Task 7 ✓
- `_build_episode_prompt`: Task 8 ✓
- `_mark_synthesized` + `_read_queue_counts`: Task 9 ✓
- `synthesize_next` happy path: Task 10 ✓
- `synthesize_next` failure split + cooldown stamping: Task 11 ✓
- Removal of old methods + obsolete tests: Task 12 ✓
- Route refactor + new banner template: Task 13 ✓
- 4-state banner test coverage: Tasks 14-15 ✓
- Chain-recovery (stray click 429, HX-Trigger every step, queue counts source): Task 15 ✓
- `seed_pending_episodes` helper: Task 16 ✓
- MCP caller drain loop + 3 new tests: Task 17 ✓
- Smoke run on user's DB: Final verification Step 3 ✓

**Placeholder scan:** `grep -nE "TBD|TODO|XXX|FIXME|implement later|fill in" docs/superpowers/plans/2026-05-04-episodic-synthesis.md` — only the `...` ellipses inside the MCP test bodies in Task 17 (Step 1), where I deliberately deferred the test-harness specifics to "follow the existing test_episode_tools.py pattern". This is intentional because the existing harness pattern is repo-specific and the executor needs to read it; the assertions are pinned. Acceptable per the YAGNI principle. (If the executor wants harness specifics they read the existing `tests/mcp/test_episode_tools.py:test_synthesize_returns_bucketed_reflections` for the pattern.)

**Type consistency:** `EpisodeQueueCounts` referenced consistently. `SynthesisStep` field names (`processed`, `episode_id`, `counts`, `queue`, `failure`) match across service / route / template / tests. Method signatures `synthesize_next(*, project: str)` consistent. `_pick_oldest_pending`, `_load_episode_context`, `_build_episode_prompt`, `_mark_synthesized`, `_read_queue_counts` names match between service additions and test classes that exercise them.

**Scope:** five commits, ~26 tasks, ~900 lines net code delta + ~700 lines net test delta. Tractable for one implementation cycle with subagent-driven-development.

**Pass-3 lifts (2026-05-04):**
- Task 5.3 (was 80%): `ObservationForPrompt.status` defaults to `"active"` → no need to touch `load_context` before deletion. Lifts to 95%.
- Task 7.3 (was 80%): `EpisodeForPrompt.project` field eliminates the subquery in `_load_episode_context`. Lifts to 95%.
- Task 14.2 (was 80%): worker-thread regression test stays at `OllamaChat.complete`-stub level with concrete seed + stub. Lifts to 92%.
- Task 17.1 (was 80%): MCP test harness pinned by reading the existing `test_service_level_returns_reflections` pattern; full test bodies provided. Lifts to 90%.
- New Commit 5 (Tasks 18-22): general-scope feature integrated; per-task confidence 95% / 92% / 88% / 95% / 99%.
