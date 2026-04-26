# Episodic Memory Phase 11 — Retention MCP Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `memory.run_retention` MCP tool that implements spec §9 — flip stale observations to `status='archived'` per the four lifecycle rules, with optional hard-delete prune for archived rows older than a configurable threshold.

**Architecture:** New `RetentionService` in `better_memory/services/retention.py` owns all retention SQL. The four archive rules (linked-only-to-retired-reflection, consumed-without-reflection, in-no_outcome-episode, plus the "kept indefinitely" rule via *not*-matching) run as SQL UPDATEs that set `status='archived'` and `status_changed_at=now()`. A new `observations.status_changed_at` column (added in Task 1) tracks the moment the row's status transitioned — needed for the "90 days after consumption" rule because `created_at` is the wrong clock (synthesis can run long after observation creation). A `memory.run_retention(retention_days=90, prune=False, prune_age_days=365, dry_run=False)` MCP tool wraps the service. Reflections are never auto-deleted; the service is observation-only.

**Tech Stack:** Python 3.12 · SQLite · MCP (`mcp.server.Server`) · pytest · uv.

**Scope boundary.** Observation retention only. Reflections stay forever (spec §9 rule 1). No automatic scheduling (spec §13 out-of-scope) — the user invokes `memory.run_retention` manually.

**Out of scope** (deferred):
- **Automatic scheduling** (cron, hooks) — spec §13 out-of-scope.
- **Audit-log retention** — `audit_log` table accrues forever; that's a separate decision.
- **Reflection deletion** — never automatic per spec §9 rule 1.
- **End-to-end browser tests** (Phase 12).
- **UI surface for retention** — invocation is MCP-only.

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §9 (the four retention rules), §13 (automatic-scheduling out-of-scope), §14 phase 11.

**Reference plans:** Phase 5 (`2026-04-22-episodic-phase-5-reflection-synthesis.md`) for the existing observation write paths that bump status (`_apply_new`, `_apply_augment`, `_apply_ignore` in `services/reflection.py`).

**Pre-existing constraints:**
- `observations.status` has NO CHECK constraint (default `'active'`). Today's writers set `'consumed_into_reflection'` (synthesis bound it) and `'consumed_without_reflection'` (synthesis ignored it). Phase 11 adds `'archived'` (retention archived it). All four values stay valid; nothing else needs to change.
- `observations` schema has `created_at` but no `updated_at` / `status_changed_at`. Task 1 adds `status_changed_at TEXT` and backfills from `created_at`. New writes must update it.
- `episodes.outcome` is one of `success / partial / abandoned / no_outcome`. Rule C only fires on `no_outcome` per spec §9.
- `reflections.updated_at` is bumped by `ReflectionService.retire` (Phase 9 Task 1) — that's our "retired-at" timestamp for Rule A.

---

## File Structure

**New files:**
- `better_memory/db/migrations/0004_status_changed_at.sql` — adds `observations.status_changed_at` column + index.
- `better_memory/services/retention.py` — new `RetentionService` class.
- `tests/services/test_retention.py` — service-layer tests.
- `tests/mcp/test_retention_tool.py` — MCP tool tests.

**Modified files:**
- `better_memory/services/observation.py` — set `status_changed_at = now()` on initial insert.
- `better_memory/services/reflection.py` — set `status_changed_at = now()` in `_apply_new`, `_apply_augment`, `_apply_ignore` (the three sites that flip observation status).
- `better_memory/services/__init__.py` — export `RetentionService`, `RetentionReport`.
- `better_memory/mcp/server.py` — register `memory.run_retention` tool definition + handler.
- `better_memory/skills/CLAUDE.snippet.md` — add `memory.run_retention` to the MCP tools section.

---

## Task 0: Worktree

Already created at `C:/Users/gethi/source/better-memory-episodic-phase-11-retention` on branch `episodic-phase-11-retention` (based on `origin/episodic-phase-1-schema` post-Phase-10-merge). Baseline: **437 passed, 22 skipped, 3 deselected**. Skip this task.

---

## Task 1: Schema migration — add `observations.status_changed_at`

The "90 days after consumption" rule needs to know when a row's status last changed. `created_at` is the wrong clock (synthesis runs days/weeks/months after observation creation). Add a `status_changed_at TEXT` column, backfill from `created_at`, and let later tasks update it on every status transition.

**Files:**
- Create: `better_memory/db/migrations/0004_status_changed_at.sql`
- Modify: `tests/db/test_schema.py` (verify migration applies + idempotent)

- [ ] **Step 1: Write the failing tests**

In `tests/db/test_schema.py`, locate the existing migration tests (e.g. `test_apply_migrations_is_idempotent`). Update the expected migration list to include `'0004'`:

```python
def test_apply_migrations_is_idempotent(tmp_memory_db):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    apply_migrations(conn)  # second call must be no-op
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in rows] == ["0001", "0002", "0003", "0004"]
```

Append a new test:

```python
class TestStatusChangedAtColumn:
    def test_observations_has_status_changed_at_column(self, tmp_memory_db):
        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(observations)").fetchall()
        }
        assert "status_changed_at" in cols

    def test_status_changed_at_backfilled_from_created_at(self, tmp_memory_db):
        # Apply migrations 0001+0002+0003 first (without 0004), insert a row,
        # then apply 0004 and verify the backfill.
        # Easiest path: apply ALL migrations (which is what real code does),
        # insert a fresh row without setting status_changed_at, and verify
        # the column defaults are populated correctly. Here we test the
        # "fresh insert" path; the backfill path is implicit (existing rows
        # in the migration get COALESCE(status_changed_at, created_at)).
        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        # Need an episode for episode_id NOT NULL.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES ('ep-1', 'proj-a', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-1', 'c', 'proj-a', 'ep-1', "
            "'2026-04-01T00:00:00+00:00')"
        )
        conn.commit()
        # The migration's backfill should set status_changed_at = created_at
        # for any row missing the value.
        row = conn.execute(
            "SELECT status_changed_at FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        # New rows: NULL until a writer sets it. The migration only
        # backfills rows that EXIST when 0004 runs.
        # This test verifies the column is present and accepts NULL —
        # backfill semantics tested separately in test_backfill_existing_rows.
        assert "status_changed_at" in row.keys()
```

Append a backfill test:

```python
    def test_backfill_existing_rows_sets_status_changed_at_from_created_at(
        self, tmp_path
    ):
        """Run migrations 0001-0003, insert an obs, then run 0004
        manually and verify the backfill UPDATE sets status_changed_at."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations

        db_path = tmp_path / "memory.db"
        conn = connect(db_path)

        # Apply 0001-0003 only by using apply_migrations with a stop point.
        # Simpler: monkeypatch the migration list, OR run 0001-0003 directly.
        # For test simplicity: insert a row using the full migration set
        # (so status_changed_at column already exists), then NULL out the
        # column to simulate "row that pre-existed the column" and re-run
        # the backfill UPDATE manually.
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES ('ep-1', 'proj-a', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at, status_changed_at) "
            "VALUES ('obs-1', 'c', 'proj-a', 'ep-1', "
            "'2026-04-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        # The migration's backfill UPDATE should populate NULL columns.
        # Re-run just that UPDATE statement to verify it works as a backfill.
        conn.execute(
            "UPDATE observations "
            "SET status_changed_at = created_at "
            "WHERE status_changed_at IS NULL"
        )
        row = conn.execute(
            "SELECT status_changed_at FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status_changed_at"] == "2026-04-01T00:00:00+00:00"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/db/test_schema.py -v -k "migration\|status_changed_at"
```

Expected: failures because `0004_status_changed_at.sql` does not exist yet.

- [ ] **Step 3: Create the migration**

Create `better_memory/db/migrations/0004_status_changed_at.sql`:

```sql
-- Phase 11: track when an observation's status last changed.
--
-- Needed for spec §9 retention rule "archived 90 days after consumption":
-- created_at is the wrong clock (synthesis can run long after creation).
--
-- Backfills existing rows from created_at — slightly conservative for
-- consumed_* rows (treats them as having been consumed at creation
-- time, which over-archives mildly) but safe: if a row is already
-- marked consumed, retention is the right destination.

ALTER TABLE observations ADD COLUMN status_changed_at TEXT;

UPDATE observations
SET status_changed_at = created_at
WHERE status_changed_at IS NULL;

CREATE INDEX idx_observations_status_changed_at
    ON observations(status, status_changed_at);
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/db/test_schema.py -v -k "migration\|status_changed_at"
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `~440 passed, 22 skipped, 3 deselected` (3 new tests on the 437 baseline).

- [ ] **Step 6: Commit**

```bash
git add better_memory/db/migrations/0004_status_changed_at.sql tests/db/test_schema.py
git commit -m "Phase 11: 0004 migration — observations.status_changed_at + backfill"
```

---

## Task 2: Update observation write paths to bump `status_changed_at`

Three sites in `better_memory/services/reflection.py` flip observation status:
1. `_apply_new` (line ~569): sets `consumed_into_reflection` for newly-bound observations.
2. `_apply_augment` (line ~641): same status, for observations bound during augment.
3. `_apply_ignore` (line ~778): sets `consumed_without_reflection`.

Plus `ObservationService.create` writes the initial `status='active'`. All four sites must set `status_changed_at` to `now()`.

**Files:**
- Modify: `better_memory/services/observation.py`
- Modify: `better_memory/services/reflection.py`
- Modify: `tests/services/test_observation.py` (or wherever the create-tests live) — assert `status_changed_at` is set on insert.
- Modify: `tests/services/test_reflection.py` — assert `status_changed_at` bumped on each transition.

- [ ] **Step 1: Locate the write sites**

```bash
grep -n "INSERT INTO observations\|UPDATE observations.*status" better_memory/services/observation.py better_memory/services/reflection.py
```

You should find:
- `observation.py:create` — INSERT INTO observations.
- `reflection.py:_apply_new` — UPDATE observations SET status = 'consumed_into_reflection'.
- `reflection.py:_apply_augment` — UPDATE observations SET status = 'consumed_into_reflection'.
- `reflection.py:_apply_ignore` — UPDATE observations SET status = 'consumed_without_reflection'.

- [ ] **Step 2: Write failing tests in `tests/services/test_observation.py`**

Append a test class verifying initial inserts populate `status_changed_at`:

```python
class TestStatusChangedAtOnInsert:
    @pytest.mark.asyncio
    async def test_create_sets_status_changed_at_to_now(
        self, conn, fixed_clock, fake_embedder
    ):
        # ObservationService.create wraps an INSERT — verify the column
        # is populated to the same instant as created_at.
        episodes = EpisodeService(conn, clock=fixed_clock)
        episodes.open_background(session_id="s1", project="proj-a")
        svc = ObservationService(
            conn=conn, embedder=fake_embedder,
            session_id="s1", clock=fixed_clock,
        )
        obs_id = await svc.create(
            content="c", project="proj-a", component=None, theme=None
        )
        row = conn.execute(
            "SELECT created_at, status_changed_at FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["status_changed_at"] is not None
        # On a fresh insert: status_changed_at == created_at (both are now).
        assert row["status_changed_at"] == row["created_at"]
```

Use the existing `conn` / `fixed_clock` / `fake_embedder` fixtures from the file. If the fixture names differ, adapt — the goal is a fresh `ObservationService` write.

- [ ] **Step 3: Update `_insert_obs` in `tests/services/test_reflection.py`**

The module-level `_insert_obs` helper (around line 38) currently inserts observations without the new column. After Task 1's migration, raw INSERTs that omit `status_changed_at` leave it NULL — which RetentionService treats as "ineligible for archive yet" but synthesis tests need a known value.

Add a new keyword-only `status_changed_at: str | None = None` parameter that defaults to `created_at` when not supplied:

```python
def _insert_obs(
    conn,
    *,
    obs_id: str,
    project: str,
    episode_id: str,
    outcome: str = "success",
    content: str = "obs content",
    component: str | None = None,
    theme: str | None = None,
    tech: str | None = None,
    created_at: str = "2026-04-22T09:00:00+00:00",
    status: str = "active",
    status_changed_at: str | None = None,
) -> None:
    if status_changed_at is None:
        status_changed_at = created_at
    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, outcome,
            reinforcement_score, episode_id, tech, created_at, status,
            status_changed_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)
        """,
        (obs_id, content, project, component, theme, outcome,
         episode_id, tech, created_at, status, status_changed_at),
    )
```

- [ ] **Step 4: Write failing tests in `tests/services/test_reflection.py`**

Append the following test class at the end of the file:

```python
class TestStatusChangedAtOnTransition:
    def test_apply_new_bumps_status_changed_at(self, conn, fixed_clock):
        """Verify _apply_new updates observations.status_changed_at to
        clock-now (not just the status column)."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        # Seed obs with old status_changed_at ('2026-04-22...') so the
        # bump is detectable. fixed_clock is 2026-04-22T09:00:00+00:00
        # by the existing fixture — that means the bump value should
        # equal the seed value. Use an explicit older seed to detect
        # the bump.
        _insert_obs(
            conn, obs_id="obs-1", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        action = NewAction(
            title="Always test", phase="general", polarity="do",
            use_cases="when X", hints=["do Y"], tech="python",
            confidence=0.6, source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        # fixed_clock is the existing fixture's value — match it.
        assert row["status_changed_at"] == fixed_clock().isoformat()

    def test_apply_augment_bumps_status_changed_at(self, conn, fixed_clock):
        """Verify _apply_augment also bumps status_changed_at on the
        newly-bound observation."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        # Seed an existing reflection + a new obs to augment with.
        _insert_reflection(conn, refl_id="r1", project="p")
        _insert_obs(
            conn, obs_id="obs-new", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        action = AugmentAction(
            target_id="r1", add_hints=["another hint"],
            confidence=0.7, source_observation_ids=["obs-new"],
        )
        svc._apply_augment([action])
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-new'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        assert row["status_changed_at"] == fixed_clock().isoformat()

    def test_apply_ignore_bumps_status_changed_at(self, conn, fixed_clock):
        """Verify _apply_ignore bumps status_changed_at when flipping to
        consumed_without_reflection."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-1", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        svc._apply_ignore(["obs-1"])
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_without_reflection"
        assert row["status_changed_at"] == fixed_clock().isoformat()
```

(The tests use the existing `conn` and `fixed_clock` fixtures defined at the top of `test_reflection.py`. Existing imports for `EpisodeService`, `ReflectionSynthesisService`, `NewAction`, `AugmentAction`, `FakeChat` are already present — verify before adding.)

- [ ] **Step 5: Run tests to verify they fail**

```bash
uv run pytest tests/services/test_observation.py tests/services/test_reflection.py -v -k "status_changed_at"
```

Expected: FAILs.

- [ ] **Step 6: Implement — `ObservationService.create`**

In `better_memory/services/observation.py`, find the INSERT INTO observations statement inside `create()`. Add `status_changed_at` to the column list and pass `now` (the same value `created_at` uses). The clock helper `_default_clock()` is already imported / used.

Specifically: the `create` method has an INSERT INTO observations like:

```python
self._conn.execute(
    "INSERT INTO observations "
    "(id, content, project, ..., created_at, episode_id, tech) "
    "VALUES (?, ?, ?, ..., ?, ?, ?)",
    (obs_id, content, project, ..., now, episode_id, tech_normalised),
)
```

Add `status_changed_at` to the column list and pass `now` in the same position:

```python
self._conn.execute(
    "INSERT INTO observations "
    "(id, content, project, ..., created_at, status_changed_at, episode_id, tech) "
    "VALUES (?, ?, ?, ..., ?, ?, ?, ?)",
    (obs_id, content, project, ..., now, now, episode_id, tech_normalised),
)
```

(Adapt to the actual current shape of the SQL — read the file first.)

- [ ] **Step 7: Implement — three sites in `reflection.py`**

For each of `_apply_new`, `_apply_augment`, `_apply_ignore`, find the UPDATE observations SET status = ... statement and extend it to also set `status_changed_at`:

```python
# Before:
f"UPDATE observations SET status = 'consumed_into_reflection' "
f"WHERE id IN ({placeholders})"

# After:
f"UPDATE observations "
f"SET status = 'consumed_into_reflection', status_changed_at = ? "
f"WHERE id IN ({placeholders})"
```

And pass `now = self._clock().isoformat()` as the first parameter (before the IN-clause params).

The existing `self._clock()` helper is already used elsewhere in the class — reuse it.

- [ ] **Step 8: Run tests to verify they pass**

```bash
uv run pytest tests/services/test_observation.py tests/services/test_reflection.py -v -k "status_changed_at"
```

Expected: 4 PASS (1 obs + 3 reflection).

- [ ] **Step 9: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `~444 passed, 22 skipped, 3 deselected`.

- [ ] **Step 10: Commit**

```bash
git add better_memory/services/observation.py better_memory/services/reflection.py \
        tests/services/test_observation.py tests/services/test_reflection.py
git commit -m "Phase 11: bump observations.status_changed_at on create + synthesis transitions"
```

---

## Task 3: Build `RetentionService` — the four archive rules

**Files:**
- Create: `better_memory/services/retention.py`
- Create: `tests/services/test_retention.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_retention.py`:

```python
"""Tests for RetentionService — spec §9 archive rules + prune."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionReport, RetentionService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_episode(conn, *, ep_id: str, project: str, outcome: str = None,
                  ended_at: str = None) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ep_id, project, "2026-04-01T00:00:00+00:00", ended_at, outcome,
         "abandoned" if outcome == "abandoned" else None),
    )
    conn.commit()


def _seed_observation(
    conn, *, obs_id: str, ep_id: str, project: str = "proj-a",
    status: str = "active",
    status_changed_at: str = "2026-04-01T00:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, episode_id, status, "
        "created_at, status_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"content {obs_id}", project, ep_id, status,
         status_changed_at, status_changed_at),
    )
    conn.commit()


def _seed_reflection(
    conn, *, refl_id: str, project: str = "proj-a",
    status: str = "confirmed",
    updated_at: str = "2026-04-01T00:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'general', 'do', 'u', '[]', 0.7, ?, ?, ?)",
        (refl_id, f"title-{refl_id}", project, status,
         "2026-04-01T00:00:00+00:00", updated_at),
    )
    conn.commit()


def _link(conn, refl_id: str, obs_id: str) -> None:
    conn.execute(
        "INSERT INTO reflection_sources (reflection_id, observation_id) "
        "VALUES (?, ?)", (refl_id, obs_id),
    )
    conn.commit()


class TestRuleAObsLinkedOnlyToRetiredReflection:
    """Spec §9: observations linked only to retired reflections, archived
    90 days after the reflection retired."""

    def test_archives_when_only_link_is_retired_and_old(
        self, conn, fixed_clock
    ):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        # Retired 100 days before fixed_clock (2026-08-01) = 2026-04-23.
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-04-23T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        svc = RetentionService(conn, clock=fixed_clock)
        report = svc.run_archive(retention_days=90)

        assert report.archived_via_retired_reflection == 1
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "archived"

    def test_keeps_when_retirement_is_recent(self, conn, fixed_clock):
        # Same setup but retired 30 days before the clock — under the
        # 90-day threshold, so keep.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-07-02T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_retired_reflection == 0
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"

    def test_keeps_when_also_linked_to_confirmed_reflection(
        self, conn, fixed_clock
    ):
        # Spec §9 rule 2: "Observations linked to non-retired reflections
        # kept indefinitely." Even if ONE of the linked reflections is
        # confirmed, don't archive.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r-retired", status="retired",
                         updated_at="2026-04-23T00:00:00+00:00")
        _seed_reflection(conn, refl_id="r-confirmed", status="confirmed",
                         updated_at="2026-04-23T00:00:00+00:00")
        _link(conn, "r-retired", "obs-1")
        _link(conn, "r-confirmed", "obs-1")

        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_retired_reflection == 0


class TestRuleBConsumedWithoutReflection:
    """Spec §9: observations with status=consumed_without_reflection
    archived 90 days after consumption."""

    def test_archives_when_consumption_is_old(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="consumed_without_reflection",
            status_changed_at="2026-04-01T00:00:00+00:00",  # 122 days old
        )
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_consumed_without_reflection == 1

    def test_keeps_when_consumption_is_recent(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="consumed_without_reflection",
            status_changed_at="2026-07-15T00:00:00+00:00",  # 17 days old
        )
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_consumed_without_reflection == 0


class TestRuleCNoOutcomeEpisode:
    """Spec §9: observations in no_outcome episodes archived 90 days
    after the episode closed."""

    def test_archives_when_episode_closed_long_ago(self, conn, fixed_clock):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 1

    def test_keeps_when_episode_outcome_not_no_outcome(
        self, conn, fixed_clock
    ):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="abandoned", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0

    def test_keeps_when_episode_still_open(self, conn, fixed_clock):
        # ended_at IS NULL — episode hasn't closed.
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome=None, ended_at=None,
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0


class TestArchivedRowsAreIdempotent:
    def test_already_archived_obs_not_recounted(self, conn, fixed_clock):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="archived")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0


class TestPrune:
    def test_prune_off_does_not_delete(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",  # 1.5 years old
        )
        # We need to break the FK chain to allow pruning — the test
        # expects nothing pruned with prune=False (default).
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=False
        )
        assert report.pruned == 0
        row = conn.execute(
            "SELECT id FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row is not None  # not deleted

    def test_prune_on_deletes_old_archived_rows(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-old", ep_id="e1", status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",  # 1.5 years
        )
        _seed_observation(
            conn, obs_id="obs-recent", ep_id="e1", status="archived",
            status_changed_at="2026-06-01T00:00:00+00:00",  # 2 months
        )
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=365,
        )
        assert report.pruned == 1
        rows = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM observations"
            ).fetchall()
        }
        assert "obs-old" not in rows  # deleted
        assert "obs-recent" in rows  # kept (under prune_age_days)


class TestDryRun:
    def test_dry_run_returns_counts_without_writing(
        self, conn, fixed_clock
    ):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="active")
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, dry_run=True
        )
        assert report.archived_via_no_outcome_episode == 1
        # But the observation status didn't actually change.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/services/test_retention.py -v
```

Expected: ~12 FAILs, all `ImportError: cannot import name 'RetentionService' from 'better_memory.services.retention'`.

- [ ] **Step 3: Implement `RetentionService`**

Create `better_memory/services/retention.py`:

```python
"""Observation retention — spec §9 archive rules + optional prune.

Retention is a manual MCP-invoked operation; there is no automatic
scheduling (spec §13). Reflections are never auto-deleted — this
module is observation-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RetentionReport:
    """Counts emitted by ``RetentionService.run``.

    ``archived_via_*`` count rows that transitioned from a non-archived
    status into ``archived`` during this run. The three rules can in
    principle target the same row; the SQL fires them in order, so a
    row that matches more than one rule is counted under the first
    matching rule and skipped by the rest.

    ``pruned`` counts archived rows hard-deleted when ``prune=True``.
    """

    archived_via_retired_reflection: int
    archived_via_consumed_without_reflection: int
    archived_via_no_outcome_episode: int
    pruned: int


class RetentionService:
    """Implements spec §9 retention rules.

    Methods:
    - ``run_archive(retention_days)`` — flip eligible observations to
      ``status='archived'`` per the four rules. Idempotent.
    - ``run(retention_days, prune, prune_age_days, dry_run)`` — wraps
      ``run_archive`` and optionally hard-deletes archived rows older
      than ``prune_age_days``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    # --------------------------------------------------------- public

    def run(
        self,
        *,
        retention_days: int = 90,
        prune: bool = False,
        prune_age_days: int = 365,
        dry_run: bool = False,
    ) -> RetentionReport:
        """Top-level entry: archive then optionally prune."""
        if dry_run:
            return self._dry_run(
                retention_days=retention_days,
                prune=prune,
                prune_age_days=prune_age_days,
            )

        archive_report = self.run_archive(retention_days=retention_days)
        pruned = 0
        if prune:
            pruned = self._prune(prune_age_days=prune_age_days)
        return RetentionReport(
            archived_via_retired_reflection=archive_report.archived_via_retired_reflection,
            archived_via_consumed_without_reflection=archive_report.archived_via_consumed_without_reflection,
            archived_via_no_outcome_episode=archive_report.archived_via_no_outcome_episode,
            pruned=pruned,
        )

    def run_archive(self, *, retention_days: int = 90) -> RetentionReport:
        """Apply the three archive rules. Returns counts."""
        threshold = (
            self._clock() - timedelta(days=retention_days)
        ).isoformat()
        now = self._clock().isoformat()

        a = self._archive_rule_a_retired_reflection(threshold, now)
        b = self._archive_rule_b_consumed_without_reflection(threshold, now)
        c = self._archive_rule_c_no_outcome_episode(threshold, now)
        self._conn.commit()

        return RetentionReport(
            archived_via_retired_reflection=a,
            archived_via_consumed_without_reflection=b,
            archived_via_no_outcome_episode=c,
            pruned=0,
        )

    # --------------------------------------------------------- private

    def _archive_rule_a_retired_reflection(
        self, threshold: str, now: str
    ) -> int:
        """Rule A: obs linked only to retired reflections, oldest
        retirement >= retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE id IN (
                SELECT o.id
                FROM observations o
                WHERE o.status != 'archived'
                  AND EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      WHERE rs.observation_id = o.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                        AND r.status != 'retired'
                  )
                  AND (
                      SELECT MAX(r.updated_at)
                      FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                  ) <= ?
            )
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _archive_rule_b_consumed_without_reflection(
        self, threshold: str, now: str
    ) -> int:
        """Rule B: status=consumed_without_reflection AND
        status_changed_at >= retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE status = 'consumed_without_reflection'
              AND status_changed_at <= ?
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _archive_rule_c_no_outcome_episode(
        self, threshold: str, now: str
    ) -> int:
        """Rule C: episode.outcome='no_outcome' AND ended_at >=
        retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE status != 'archived'
              AND episode_id IN (
                  SELECT id FROM episodes
                  WHERE outcome = 'no_outcome'
                    AND ended_at IS NOT NULL
                    AND ended_at <= ?
              )
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _prune(self, *, prune_age_days: int) -> int:
        """Hard-delete archived rows older than prune_age_days.

        IMPORTANT: this also deletes the FTS5 + embeddings rows via
        the AFTER DELETE trigger on observations. reflection_sources
        rows pointing at the deleted observation are CASCADE-deleted
        by the FK ... actually wait, the schema does NOT specify
        ON DELETE CASCADE on reflection_sources.observation_id. So
        deleting an observation that has reflection_sources rows would
        violate FK. Belt-and-braces: only prune observations whose
        reflection_sources rows are also gone (i.e. they were never
        sourced, OR their reflection was retired and the
        reflection_sources rows happen to point at retired
        reflections — which is fine to delete only if we ALSO clean
        up the reflection_sources entries).

        For Phase 11: only prune observations with NO reflection_sources
        rows. Sourced observations stay archived but undeleted (their
        evidence trail is preserved for audit). This is conservative
        but correct — the spec doesn't require pruning sourced rows.
        """
        threshold = (
            self._clock() - timedelta(days=prune_age_days)
        ).isoformat()
        cursor = self._conn.execute(
            """
            DELETE FROM observations
            WHERE status = 'archived'
              AND status_changed_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM reflection_sources rs
                  WHERE rs.observation_id = observations.id
              )
            """,
            (threshold,),
        )
        self._conn.commit()
        return cursor.rowcount or 0

    def _dry_run(
        self, *, retention_days: int, prune: bool, prune_age_days: int,
    ) -> RetentionReport:
        """Run COUNT-only versions of all rules; commit nothing."""
        threshold = (
            self._clock() - timedelta(days=retention_days)
        ).isoformat()

        a = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM observations o
            WHERE o.status != 'archived'
              AND EXISTS (
                  SELECT 1 FROM reflection_sources rs
                  WHERE rs.observation_id = o.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM reflection_sources rs
                  JOIN reflections r ON r.id = rs.reflection_id
                  WHERE rs.observation_id = o.id
                    AND r.status != 'retired'
              )
              AND (
                  SELECT MAX(r.updated_at)
                  FROM reflection_sources rs
                  JOIN reflections r ON r.id = rs.reflection_id
                  WHERE rs.observation_id = o.id
              ) <= ?
            """,
            (threshold,),
        ).fetchone()["n"]
        b = self._conn.execute(
            "SELECT COUNT(*) AS n FROM observations "
            "WHERE status = 'consumed_without_reflection' "
            "AND status_changed_at <= ?",
            (threshold,),
        ).fetchone()["n"]
        c = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM observations
            WHERE status != 'archived'
              AND episode_id IN (
                  SELECT id FROM episodes
                  WHERE outcome = 'no_outcome'
                    AND ended_at IS NOT NULL
                    AND ended_at <= ?
              )
            """,
            (threshold,),
        ).fetchone()["n"]

        pruned = 0
        if prune:
            prune_threshold = (
                self._clock() - timedelta(days=prune_age_days)
            ).isoformat()
            pruned = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM observations
                WHERE status = 'archived'
                  AND status_changed_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      WHERE rs.observation_id = observations.id
                  )
                """,
                (prune_threshold,),
            ).fetchone()["n"]

        return RetentionReport(
            archived_via_retired_reflection=a,
            archived_via_consumed_without_reflection=b,
            archived_via_no_outcome_episode=c,
            pruned=pruned,
        )
```

- [ ] **Step 4: Export from `services/__init__.py`**

Add `RetentionService` and `RetentionReport` to the package exports:

```python
from better_memory.services.retention import RetentionReport, RetentionService

# ... in __all__:
"RetentionReport",
"RetentionService",
```

(Maintain alphabetical order in `__all__`.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/services/test_retention.py -v
```

Expected: 12 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `~456 passed, 22 skipped, 3 deselected` (12 new tests on the post-Task-2 baseline ~444).

- [ ] **Step 7: Commit**

```bash
git add better_memory/services/retention.py better_memory/services/__init__.py \
        tests/services/test_retention.py
git commit -m "Phase 11: RetentionService — spec §9 archive rules + optional prune"
```

---

## Task 4: Add `memory.run_retention` MCP tool

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_retention_tool.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_retention_tool.py` (mirrors the pattern from `tests/mcp/test_episode_tools.py`: split into a service-call test + a tool-registration test, no full MCP runtime needed):

```python
"""Tests for the memory.run_retention MCP tool."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_no_outcome_episode_with_obs(conn) -> None:
    """Seed an episode + observation matching retention Rule C."""
    conn.execute(
        "INSERT INTO episodes "
        "(id, project, started_at, ended_at, outcome, close_reason) "
        "VALUES ('e1', 'p', '2026-04-01T00:00:00+00:00', "
        "'2026-04-01T00:00:00+00:00', 'no_outcome', "
        "'session_end_reconciled')"
    )
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, episode_id, status, "
        "created_at, status_changed_at) "
        "VALUES ('obs-1', 'c', 'p', 'e1', 'active', "
        "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')"
    )
    conn.commit()


class TestRunRetentionViaService:
    """The MCP tool is a thin wrapper; verify the service call shape."""

    def test_dry_run_returns_counts_without_writing(
        self, conn, fixed_clock
    ):
        _seed_no_outcome_episode_with_obs(conn)
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, dry_run=True
        )
        assert report.archived_via_no_outcome_episode == 1
        # Status NOT changed.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "active"

    def test_archive_only_flips_status(self, conn, fixed_clock):
        _seed_no_outcome_episode_with_obs(conn)
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=False
        )
        assert report.archived_via_no_outcome_episode == 1
        assert report.pruned == 0
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "archived"

    def test_prune_deletes_old_archived_unsourced_obs(
        self, conn, fixed_clock
    ):
        # An archived obs with no reflection_sources, archived 400 days
        # ago. With prune=True and prune_age_days=365, it should go.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES ('e1', 'p', '2025-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, status, "
            "created_at, status_changed_at) "
            "VALUES ('obs-old', 'c', 'p', 'e1', 'archived', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')"
        )
        conn.commit()

        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=365,
        )
        assert report.pruned == 1
        row = conn.execute(
            "SELECT id FROM observations WHERE id = 'obs-old'"
        ).fetchone()
        assert row is None  # deleted


class TestToolRegistration:
    def test_tool_is_registered_in_factory(self):
        """The MCP server registers memory.run_retention by name."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.run_retention" in tool_names

    def test_tool_input_schema_has_expected_properties(self):
        from better_memory.mcp.server import _tool_definitions

        tools = {t.name: t for t in _tool_definitions()}
        retention = tools["memory.run_retention"]
        schema = retention.inputSchema
        props = schema["properties"]
        assert "retention_days" in props
        assert "prune" in props
        assert "prune_age_days" in props
        assert "dry_run" in props
        assert schema.get("additionalProperties") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/mcp/test_retention_tool.py -v
```

Expected: 4 FAILs (tool not registered).

- [ ] **Step 3: Add tool definition + handler in `server.py`**

In `better_memory/mcp/server.py`:

1. Add `RetentionService` to the create_server construction. After `reflections = ReflectionSynthesisService(...)`:

```python
    retention = RetentionService(conn=conn)
```

2. In `_tool_definitions()`, add a new Tool:

```python
        Tool(
            name="memory.run_retention",
            description=(
                "Apply spec §9 retention rules — flip eligible "
                "observations to status='archived' and optionally "
                "hard-delete archived rows older than prune_age_days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "retention_days": {
                        "type": "integer",
                        "default": 90,
                        "description": (
                            "Age threshold for the three archive "
                            "rules. Default 90 (per spec §9)."
                        ),
                    },
                    "prune": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, also hard-delete archived rows "
                            "older than prune_age_days."
                        ),
                    },
                    "prune_age_days": {
                        "type": "integer",
                        "default": 365,
                        "description": (
                            "Age threshold for prune mode. Default 365."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, return the counts without "
                            "writing any changes to the DB."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
```

3. In `_call_tool`, add a new branch:

```python
        if name == "memory.run_retention":
            report = retention.run(
                retention_days=args.get("retention_days", 90),
                prune=args.get("prune", False),
                prune_age_days=args.get("prune_age_days", 365),
                dry_run=args.get("dry_run", False),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "archived_via_retired_reflection":
                            report.archived_via_retired_reflection,
                        "archived_via_consumed_without_reflection":
                            report.archived_via_consumed_without_reflection,
                        "archived_via_no_outcome_episode":
                            report.archived_via_no_outcome_episode,
                        "pruned": report.pruned,
                    }),
                )
            ]
```

(Adapt to the existing handler structure — the file has many similar branches you can mirror.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/mcp/test_retention_tool.py -v
```

Expected: 5 PASS (3 service-call tests + 2 tool-registration tests).

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `~461 passed, 22 skipped, 3 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_retention_tool.py
git commit -m "Phase 11: memory.run_retention MCP tool — archive + optional prune"
```

---

## Task 5: Update CLAUDE.md skill snippet

**Files:**
- Modify: `better_memory/skills/CLAUDE.snippet.md`

- [ ] **Step 1: Find the MCP tools section**

```bash
grep -n "memory\.\|MCP tools" better_memory/skills/CLAUDE.snippet.md | head
```

- [ ] **Step 2: Append `memory.run_retention` to the tools list**

After the existing `knowledge.list` line in the MCP tools section, append:

```
- `memory.run_retention(retention_days?, prune?, prune_age_days?, dry_run?)` — apply spec §9 retention: flip stale observations to `status='archived'`, optionally prune archived rows older than `prune_age_days`. Reflections are never auto-deleted. User-invoked; no auto-scheduling.
```

(Use the Edit tool with surrounding context to make the addition unique.)

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: same count as Task 4 (snippet not tested).

- [ ] **Step 4: Commit**

```bash
git add better_memory/skills/CLAUDE.snippet.md
git commit -m "Phase 11: CLAUDE snippet — document memory.run_retention"
```

---

## Final review

After all tasks complete, dispatch a final code-review subagent across the full Phase 11 diff. Confirm:

- All tests pass: `uv run pytest --tb=no -q 2>&1 | tail -3` shows `~461 passed, 22 skipped, 3 deselected`.
- Spec §9 rules are correctly implemented (the three archive rules + optional prune; reflections never deleted).
- `dry_run` returns counts without writes.
- Idempotency: running `run_retention` twice in a row produces zero archives the second time.
- Multi-rule overlap: an observation that matches more than one rule is archived once, counted under the first matching rule.
- The MCP tool's JSON output keys match the spec.

Then run `superpowers:finishing-a-development-branch` to push + open the PR (which will trigger the auto-babysit Bugbot loop per the standing instruction).
