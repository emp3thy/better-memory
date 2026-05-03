# Background lifecycle (sub-design C) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive retention automatically (lazy-on-`memory.retrieve`, 24h-guarded) and surface hook errors in a new `/diagnostics` UI page.

**Architecture:** Five logical commits on branch `phase-c`. (1) SQL migration for `retention_runs` + `hook_errors` tables. (2) `RetentionScheduler` wrapping `RetentionService` with a 24h-guarded `maybe_run` method, wired into `mcp/server.py`'s `memory.retrieve` after spool drain. (3) `record_hook_error` helper writing to `hook_errors`, called from each hook's broad-except. (4) `/diagnostics` page following the established UI pattern (extends `base.html`, panel + drawer + day-grouped fragments). (5) Spec + plan + README docs.

**Tech Stack:** Python 3.12 (PEP 695 type params), sqlite3, Flask, Jinja2, HTMX 2.x, Playwright (browser tests), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-05-03-background-lifecycle-design.md`

---

## File Structure

| File | Commit | Type |
|---|---|---|
| `better_memory/db/migrations/0005_phase_c.sql` | 1 | Create — schema for `retention_runs` + `hook_errors` |
| `tests/db/test_phase_c_migration.py` | 1 | Create — verifies tables + indexes after `apply_migrations` |
| `better_memory/config.py` | 2 | Modify — add `auto_prune: bool` field to `Config` |
| `better_memory/services/retention_scheduler.py` | 2 | Create — `RetentionScheduler` with `maybe_run` |
| `better_memory/mcp/server.py` | 2 | Modify (~line 488) — call `scheduler.maybe_run(triggered_by="retrieve")` after spool drain |
| `tests/services/test_retention_scheduler.py` | 2 | Create — 7 unit tests with controlled clock + mocked service |
| `better_memory/hooks/_error_log.py` | 3 | Create — `record_hook_error` helper |
| `better_memory/hooks/observer.py` | 3 | Modify — call helper from broad except |
| `better_memory/hooks/session_start.py` | 3 | Modify — call helper from broad except (line 119) |
| `better_memory/hooks/session_close.py` | 3 | Modify — call helper from broad except (line 109) |
| `better_memory/hooks/post_commit.py` | 3 | Modify — call helper from broad except (line 173) |
| `tests/hooks/test_error_log.py` | 3 | Create — 5 unit tests |
| `better_memory/ui/queries.py` | 4 | Modify — add `HookErrorRow` + `RetentionRunRow` dataclasses + 3 query functions |
| `better_memory/ui/app.py` | 4 | Modify — add 6 routes for `/diagnostics` + nav-active state |
| `better_memory/ui/templates/base.html` | 4 | Modify (line 32) — add Diagnostics nav link |
| `better_memory/ui/templates/diagnostics.html` | 4 | Create — page extending `base.html` |
| `better_memory/ui/templates/fragments/panel_hook_errors.html` | 4 | Create — top action bar + day-grouped rows |
| `better_memory/ui/templates/fragments/hook_error_row.html` | 4 | Create — row with click-to-drawer + delete button |
| `better_memory/ui/templates/fragments/hook_error_drawer.html` | 4 | Create — full traceback view |
| `better_memory/ui/templates/fragments/panel_retention_runs.html` | 4 | Create — day-grouped list (no actions) |
| `tests/ui/test_diagnostics.py` | 4 | Create — 9 unit tests |
| `tests/ui/test_browser_diagnostics.py` | 4 | Create — 4 Playwright tests with `@pytest.mark.integration` |
| `docs/superpowers/specs/2026-05-03-background-lifecycle-design.md` | 5 | Already exists — committed as part of docs |
| `docs/superpowers/plans/2026-05-03-background-lifecycle.md` | 5 | Already exists (this file) |
| `README.md` | 5 | Modify — add `BETTER_MEMORY_AUTO_PRUNE` to Configuration table |

---

## Confidence summary

Per memories `dde30588` (confidence-scoring) + `e7961c0c` (verify INTERNAL patterns at spec-write time, not just endpoints).

| Task | Confidence | Notes |
|---|---|---|
| 1. Pre-flight + verify branch | 98% | Read-only checks. |
| 2. Schema migration + test | 96% | Pure SQL, mirrors `0002_episodic.sql` patterns. Migration filename `0005_phase_c.sql` (verified — 0003 and 0004 already exist). |
| 3. Config extension | 98% | One field added to existing `Config` dataclass; mirrors existing `_resolve_bool` pattern. |
| 4. RetentionScheduler module | 95% | New module, well-spec'd. Test uses controlled clock + mocked `RetentionService`. |
| 5. Wire scheduler into `mcp/server.py` | 92% | Single insertion after `spool.drain()` at line 485, wrapped in best-effort except. |
| 6. Commit 2 (scheduler + wire) | 92% | Commit checkpoint. |
| 7. `_error_log.py` helper | 94% | Defensive try/except wrapped around DB write — can't break the hook. |
| 8. Wire `_error_log` into 4 hooks | 92% | 2-line addition in each hook's existing broad-except block. Lazy import keeps happy-path zero-overhead. |
| 9. Commit 3 (hook errors) | 92% | Commit checkpoint. |
| 10. Diagnostics queries (`queries.py`) | 96% | Mirrors existing `observation_list_for_ui` shape. |
| 11. Diagnostics routes (`app.py`) | 92% | 6 new routes follow established pattern. |
| 12. Diagnostics templates (5 new) | 93% | Templates pinned in spec from verified patterns (`panel_observations.html`, `observation_row.html`, `observations.html`, `base.html`). |
| 13. Diagnostics nav-link addition | 98% | One-line edit to `base.html`, exact location pinned (after Reflections at line 32). |
| 14. Diagnostics unit tests | 92% | Mirrors `tests/ui/test_observations.py` patterns. |
| 15. Diagnostics browser tests | 92% | Raised from 88% with concrete flakiness mitigations: `expect(...).to_be_visible(timeout=10000)` everywhere (built-in retry absorbs HTMX async-load races); dialog handler registered immediately after `page.goto`, not before each click; `expect(...).not_to_be_visible()` for negative assertions; specific selectors per assertion; `_HTMX_TIMEOUT_MS = 10_000` constant explains the timeout choice. `ui_url` fixture is function-scoped (verified) so cross-test pollution isn't a concern. |
| 16. Commit 4 (UI) | 92% | Commit checkpoint. |
| 17. README + spec/plan commit | 96% | Pure docs. |
| 18. Final verification | 95% | pytest + ruff. |
| 19. Push + PR (USER GATED) | 95% | Pause-before-push. |

**Plan-wide confidence: ~94%.**

**Plan-wide notes:**
- HEREDOC commit messages must be executed via the **Bash tool**, not PowerShell (memory recurring point — PowerShell here-strings use `@'...'@` syntax).
- pytest output capture: use `> file 2>&1` (not the reverse) on Windows; or use `--junit-xml` for authoritative results (memory `2c17449099`).
- All tests use `monkeypatch.setattr(_app_module, ...)` to patch the symbol the route resolves at call time (not the source module — established pattern in this codebase).

---

## Task 1: Pre-flight + verify branch

**Files:** none (read-only)

- [ ] **Step 1: Confirm we're on `phase-c` branch with a clean working tree**

```bash
git status --porcelain
git branch --show-current
```

Expected: `phase-c`. Working tree clean (or only `.claude/` untracked).

- [ ] **Step 2: Confirm tests pass on `phase-c` baseline**

```bash
rm -f junit.xml
uv run pytest --junit-xml=junit.xml -q --no-header > /tmp/preflight.log 2>&1
echo "EXIT=$?"
python3 -c "import xml.etree.ElementTree as ET; r=ET.parse('junit.xml').getroot(); s=r if r.tag=='testsuite' else r.find('testsuite'); print('SUMMARY:', dict(s.attrib))"
rm -f junit.xml
```

Expected: `EXIT=0`, all baseline tests pass. Note the test count.

- [ ] **Step 3: Verify the spec exists**

```bash
ls -la docs/superpowers/specs/2026-05-03-background-lifecycle-design.md
```

Expected: file exists.

- [ ] **Step 4: Verify the next migration number is 0005**

```bash
ls better_memory/db/migrations/
```

Expected: `0001_init.sql`, `0002_episodic.sql`, `0003_synthesis_runs_last_goal.sql`, `0004_status_changed_at.sql`. Confirms `0005_phase_c.sql` is the next slot.

---

## Task 2: Schema migration

**Files:**
- Create: `better_memory/db/migrations/0005_phase_c.sql`
- Create: `tests/db/test_phase_c_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_phase_c_migration.py`:

```python
"""Migration 0005: retention_runs + hook_errors tables exist with correct shape."""

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


def test_retention_runs_table_exists(conn) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(retention_runs)").fetchall()
    }
    assert cols == {
        "id",
        "run_at",
        "archived_via_retired_reflection",
        "archived_via_consumed_without_reflection",
        "archived_via_no_outcome_episode",
        "pruned",
        "triggered_by",
    }


def test_retention_runs_has_run_at_index(conn) -> None:
    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT * FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'retention_runs'"
        ).fetchall()
    }
    assert "idx_retention_runs_run_at" in indexes


def test_hook_errors_table_exists(conn) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(hook_errors)").fetchall()
    }
    assert cols == {
        "id",
        "created_at",
        "hook_name",
        "exception_type",
        "exception_message",
        "traceback",
        "cwd",
    }


def test_hook_errors_has_created_at_index(conn) -> None:
    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT * FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'hook_errors'"
        ).fetchall()
    }
    assert "idx_hook_errors_created_at" in indexes
```

- [ ] **Step 2: Run, confirm FAIL**

```bash
uv run pytest tests/db/test_phase_c_migration.py -v 2>&1 | tail -10
```

Expected: 4 FAIL — `no such table: retention_runs` and `no such table: hook_errors`.

- [ ] **Step 3: Create the migration file**

Create `better_memory/db/migrations/0005_phase_c.sql`:

```sql
-- better-memory migration 0005: phase C — background lifecycle.
--
-- retention_runs: append-only audit trail for RetentionScheduler runs.
-- One row per run; the most recent row's run_at is consulted by the
-- 24h "too soon" guard.
--
-- hook_errors: best-effort visibility into hook failures. Hooks must
-- never raise (they catch BaseException); this table records what
-- happened so a failing hook is no longer invisible. Surfaced via
-- the /diagnostics UI page.

CREATE TABLE retention_runs (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    archived_via_retired_reflection INTEGER NOT NULL,
    archived_via_consumed_without_reflection INTEGER NOT NULL,
    archived_via_no_outcome_episode INTEGER NOT NULL,
    pruned INTEGER NOT NULL,
    triggered_by TEXT NOT NULL  -- 'retrieve' | 'manual'
);

CREATE INDEX idx_retention_runs_run_at
    ON retention_runs(run_at DESC);

CREATE TABLE hook_errors (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    hook_name TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    exception_message TEXT,
    traceback TEXT,
    cwd TEXT
);

CREATE INDEX idx_hook_errors_created_at
    ON hook_errors(created_at DESC);
```

- [ ] **Step 4: Run, confirm PASS**

```bash
uv run pytest tests/db/test_phase_c_migration.py -v 2>&1 | tail -10
```

Expected: 4 PASS.

- [ ] **Step 5: Run ruff**

```bash
uv run ruff check better_memory/db/migrations/0005_phase_c.sql tests/db/test_phase_c_migration.py 2>&1 | tail -3
```

Expected: All checks passed (ruff doesn't lint .sql but does lint the .py test file).

- [ ] **Step 6: Commit using the Bash tool**

```bash
git add better_memory/db/migrations/0005_phase_c.sql tests/db/test_phase_c_migration.py
git commit -m "$(cat <<'EOF'
chore(db): 0005_phase_c migration for retention_runs + hook_errors

Two new tables for phase C:
- retention_runs: append-only audit trail for RetentionScheduler runs;
  the most recent row's run_at is consulted by the 24h "too soon" guard.
- hook_errors: best-effort visibility into hook failures (hooks catch
  BaseException so previously failures were invisible).

Migration auto-applied on next MCP server startup via apply_migrations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Config extension — add `auto_prune` field

**Files:**
- Modify: `better_memory/config.py`

- [ ] **Step 1: Add the failing config test**

Append to `tests/test_config.py` (file exists; add at end):

```python
def test_config_auto_prune_defaults_false(monkeypatch) -> None:
    """Config.auto_prune defaults to False when env var unset."""
    monkeypatch.delenv("BETTER_MEMORY_AUTO_PRUNE", raising=False)
    from better_memory.config import get_config
    cfg = get_config()
    assert cfg.auto_prune is False


def test_config_auto_prune_true_when_env_set(monkeypatch) -> None:
    """Config.auto_prune is True when BETTER_MEMORY_AUTO_PRUNE=1."""
    monkeypatch.setenv("BETTER_MEMORY_AUTO_PRUNE", "1")
    from better_memory.config import get_config
    cfg = get_config()
    assert cfg.auto_prune is True
```

- [ ] **Step 2: Run, confirm FAIL**

```bash
uv run pytest tests/test_config.py::test_config_auto_prune_defaults_false tests/test_config.py::test_config_auto_prune_true_when_env_set -v 2>&1 | tail -5
```

Expected: 2 FAIL — `AttributeError: 'Config' object has no attribute 'auto_prune'`.

- [ ] **Step 3: Add `auto_prune` field to `Config` dataclass and `get_config()`**

In `better_memory/config.py`, find:

```python
@dataclass(frozen=True)
class Config:
    """Resolved better-memory configuration."""

    home: Path
    memory_db: Path
    knowledge_db: Path
    knowledge_base: Path
    spool_dir: Path
    ollama_host: str
    embed_model: str
    consolidate_model: str
    audit_log_retrieved: bool
```

Replace with:

```python
@dataclass(frozen=True)
class Config:
    """Resolved better-memory configuration."""

    home: Path
    memory_db: Path
    knowledge_db: Path
    knowledge_base: Path
    spool_dir: Path
    ollama_host: str
    embed_model: str
    consolidate_model: str
    audit_log_retrieved: bool
    auto_prune: bool
```

In `get_config()`, find the return statement:

```python
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
    )
```

Replace with:

```python
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
        auto_prune=_resolve_bool("BETTER_MEMORY_AUTO_PRUNE", default=False),
    )
```

- [ ] **Step 4: Run, confirm PASS**

```bash
uv run pytest tests/test_config.py -v 2>&1 | tail -10
```

Expected: all `tests/test_config.py` tests pass (the 2 new ones plus existing ones).

---

## Task 4: RetentionScheduler module

**Files:**
- Create: `better_memory/services/retention_scheduler.py`
- Create: `tests/services/test_retention_scheduler.py`

- [ ] **Step 1: Create the test file with all 7 tests**

Create `tests/services/test_retention_scheduler.py`:

```python
"""Unit tests for RetentionScheduler.

Tests use a controlled clock + a stub for RetentionService to keep
behaviour isolated from actual archive logic.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionReport
from better_memory.services.retention_scheduler import RetentionScheduler


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    apply_migrations(c)
    yield c
    c.close()


def _seed_run(conn, *, run_at: datetime, triggered_by: str = "retrieve") -> None:
    """Insert a fake retention_runs row for the guard to see."""
    conn.execute(
        "INSERT INTO retention_runs (run_at, "
        "archived_via_retired_reflection, "
        "archived_via_consumed_without_reflection, "
        "archived_via_no_outcome_episode, pruned, triggered_by) "
        "VALUES (?, 0, 0, 0, 0, ?)",
        (run_at.isoformat(), triggered_by),
    )
    conn.commit()


def _empty_report() -> RetentionReport:
    return RetentionReport(
        archived_via_retired_reflection=0,
        archived_via_consumed_without_reflection=0,
        archived_via_no_outcome_episode=0,
        pruned=0,
    )


def test_first_call_runs_retention(conn) -> None:
    """No prior run → scheduler invokes RetentionService and records."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

    rows = conn.execute(
        "SELECT triggered_by FROM retention_runs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["triggered_by"] == "retrieve"


def test_within_guard_skips(conn) -> None:
    """Last run was 1 hour ago → scheduler is a no-op."""
    fake_now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
    _seed_run(conn, run_at=fake_now - timedelta(hours=1))

    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        sched = RetentionScheduler(
            conn, auto_prune=False, clock=lambda: fake_now
        )
        sched.maybe_run(triggered_by="retrieve")

        MockService.assert_not_called()
    rows = conn.execute("SELECT id FROM retention_runs").fetchall()
    assert len(rows) == 1  # the seeded row, no new row


def test_after_guard_runs_again(conn) -> None:
    """Last run was 25 hours ago → scheduler runs again."""
    fake_now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
    _seed_run(conn, run_at=fake_now - timedelta(hours=25))

    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(
            conn, auto_prune=False, clock=lambda: fake_now
        )
        sched.maybe_run(triggered_by="retrieve")

        MockService.assert_called_once()
    rows = conn.execute("SELECT id FROM retention_runs").fetchall()
    assert len(rows) == 2  # seeded + new


def test_records_triggered_by(conn) -> None:
    """The triggered_by string the caller passes is persisted."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="manual")

    row = conn.execute(
        "SELECT triggered_by FROM retention_runs"
    ).fetchone()
    assert row["triggered_by"] == "manual"


def test_records_counts_from_report(conn) -> None:
    """Counts from RetentionReport are persisted to the row."""
    report = RetentionReport(
        archived_via_retired_reflection=5,
        archived_via_consumed_without_reflection=3,
        archived_via_no_outcome_episode=2,
        pruned=7,
    )
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = report
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

    row = conn.execute(
        "SELECT * FROM retention_runs"
    ).fetchone()
    assert row["archived_via_retired_reflection"] == 5
    assert row["archived_via_consumed_without_reflection"] == 3
    assert row["archived_via_no_outcome_episode"] == 2
    assert row["pruned"] == 7


def test_auto_prune_false_passes_prune_false(conn) -> None:
    """auto_prune=False → RetentionService.run(prune=False)."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

        call = MockService.return_value.run.call_args
        assert call.kwargs["prune"] is False


def test_auto_prune_true_passes_prune_true(conn) -> None:
    """auto_prune=True → RetentionService.run(prune=True)."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=True)
        sched.maybe_run(triggered_by="retrieve")

        call = MockService.return_value.run.call_args
        assert call.kwargs["prune"] is True
```

- [ ] **Step 2: Run, confirm 7 FAIL**

```bash
uv run pytest tests/services/test_retention_scheduler.py -v 2>&1 | tail -10
```

Expected: 7 FAIL — `ModuleNotFoundError: No module named 'better_memory.services.retention_scheduler'`.

- [ ] **Step 3: Create the scheduler module**

Create `better_memory/services/retention_scheduler.py`:

```python
"""24h-guarded retention runner.

Wraps RetentionService with a "has it run in the last 24h?" check
and an audit-trail row in retention_runs. Caller (memory.retrieve)
invokes maybe_run after spool drain; the timestamp guard ensures
retention runs at most once per 24h.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from better_memory.services.retention import RetentionReport, RetentionService


_RETENTION_DAYS = 90
_PRUNE_AGE_DAYS = 365
_GUARD_HOURS = 24


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RetentionScheduler:
    """24h-guarded wrapper around RetentionService."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        auto_prune: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._auto_prune = auto_prune
        self._clock: Callable[[], datetime] = clock or _default_clock

    def maybe_run(self, *, triggered_by: str) -> None:
        """Run retention IF >24h since last run. Records to retention_runs.

        If two callers race past the guard within ~50ms, both will run
        RetentionService.run() and both write a row. SQLite serializes;
        the second is essentially free (UPDATE matches zero new rows).
        Documented in the design as accepted.
        """
        if self._too_soon():
            return
        report = RetentionService(self._conn).run(
            retention_days=_RETENTION_DAYS,
            prune=self._auto_prune,
            prune_age_days=_PRUNE_AGE_DAYS,
        )
        self._record_run(report, triggered_by=triggered_by)

    def _too_soon(self) -> bool:
        threshold = (
            self._clock() - timedelta(hours=_GUARD_HOURS)
        ).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM retention_runs WHERE run_at > ? LIMIT 1",
            (threshold,),
        ).fetchone()
        return row is not None

    def _record_run(
        self, report: RetentionReport, *, triggered_by: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO retention_runs (run_at, "
            "archived_via_retired_reflection, "
            "archived_via_consumed_without_reflection, "
            "archived_via_no_outcome_episode, pruned, triggered_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._clock().isoformat(),
                report.archived_via_retired_reflection,
                report.archived_via_consumed_without_reflection,
                report.archived_via_no_outcome_episode,
                report.pruned,
                triggered_by,
            ),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run, confirm 7 PASS**

```bash
uv run pytest tests/services/test_retention_scheduler.py -v 2>&1 | tail -15
```

Expected: 7 PASS.

---

## Task 5: Wire scheduler into `mcp/server.py`

**Files:**
- Modify: `better_memory/mcp/server.py` (around line 485)

- [ ] **Step 1: Read the current `memory.retrieve` handler in `mcp/server.py`**

```bash
grep -n -A 20 'name == "memory.retrieve":' better_memory/mcp/server.py | head -25
```

Expected: shows the existing `try: spool.drain() except Exception: pass` at lines 484-487.

- [ ] **Step 2: Add the retention import near the top of `server.py`**

Find the existing import:

```python
from better_memory.services.retention import RetentionService
```

Replace with:

```python
from better_memory.services.retention import RetentionService
from better_memory.services.retention_scheduler import RetentionScheduler
```

- [ ] **Step 3: Wire `RetentionScheduler.maybe_run` after `spool.drain()`**

Find this block in `better_memory/mcp/server.py` (in the `memory.retrieve` branch):

```python
            # 1. Drain spool — must happen before any retrieval so fresh
            #    hook events (session_start, commit_close) are processed.
            #    SpoolService.drain is idempotent.
            try:
                spool.drain()
            except Exception:  # noqa: BLE001 — drain is best-effort
                pass
```

Replace with:

```python
            # 1. Drain spool — must happen before any retrieval so fresh
            #    hook events (session_start, commit_close) are processed.
            #    SpoolService.drain is idempotent.
            try:
                spool.drain()
            except Exception:  # noqa: BLE001 — drain is best-effort
                pass

            # 2. Maybe run retention. Guard ensures at most once per 24h
            #    regardless of how often retrieve is called. Best-effort:
            #    a retention failure must NEVER block memory.retrieve.
            try:
                config = get_config()
                RetentionScheduler(
                    memory_conn, auto_prune=config.auto_prune
                ).maybe_run(triggered_by="retrieve")
            except Exception:  # noqa: BLE001 — retention is best-effort
                pass
```

(`get_config` must be imported. Check if it already is by greppping; if not, add `from better_memory.config import get_config` next to the other config imports.)

- [ ] **Step 4: Verify `get_config` import is present**

```bash
grep -n "from better_memory.config import" better_memory/mcp/server.py
```

Expected: shows the import (existing or just-added). If missing, add it.

- [ ] **Step 5: Run the full pytest suite to confirm no regression**

```bash
rm -f junit.xml
uv run pytest --junit-xml=junit.xml -q --no-header > /tmp/t5.log 2>&1
echo "EXIT=$?"
python3 -c "import xml.etree.ElementTree as ET; r=ET.parse('junit.xml').getroot(); s=r if r.tag=='testsuite' else r.find('testsuite'); print('SUMMARY:', dict(s.attrib))"
rm -f junit.xml
```

Expected: `EXIT=0`, all tests pass (baseline + 4 migration tests + 2 config tests + 7 scheduler tests added so far).

- [ ] **Step 6: Run ruff**

```bash
uv run ruff check better_memory/config.py better_memory/services/retention_scheduler.py better_memory/mcp/server.py tests/test_config.py tests/services/test_retention_scheduler.py 2>&1 | tail -3
```

Expected: All checks passed.

---

## Task 6: Commit 2 — RetentionScheduler + config + wiring

**Files:** committing accumulated changes from Tasks 3-5

- [ ] **Step 1: Stage and commit using the Bash tool**

```bash
git add \
  better_memory/config.py \
  better_memory/services/retention_scheduler.py \
  better_memory/mcp/server.py \
  tests/test_config.py \
  tests/services/test_retention_scheduler.py
git commit -m "$(cat <<'EOF'
feat(retention): RetentionScheduler with retrieve-trigger and 24h guard

Drives retention automatically: every memory.retrieve call invokes
RetentionScheduler.maybe_run() after the existing spool drain. A
24h "too soon" check (consulting the most recent retention_runs
row) ensures actual retention runs at most once per day regardless
of how often retrieve fires.

Auto-retention archives by default (status flip, reversible);
pruning (hard delete, irreversible) is opt-in via the new
BETTER_MEMORY_AUTO_PRUNE=1 env var. Config.auto_prune holds the
resolved value; the scheduler takes it explicitly so tests don't
need monkeypatch.setenv.

Wired into mcp/server.py's memory.retrieve handler with a
best-effort try/except — a retention failure NEVER blocks the
caller. The guarded "too soon" check is itself a single SELECT
so the happy path is essentially free.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `_error_log.py` helper

**Files:**
- Create: `better_memory/hooks/_error_log.py`
- Create: `tests/hooks/test_error_log.py`

- [ ] **Step 1: Create the test file**

Create `tests/hooks/test_error_log.py`:

```python
"""Unit tests for hooks._error_log.record_hook_error."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Point BETTER_MEMORY_HOME at tmp_path and migrate the DB."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    db = tmp_path / "memory.db"
    c = connect(db)
    try:
        apply_migrations(c)
    finally:
        c.close()
    return db


def test_record_hook_error_inserts_row(db_path: Path) -> None:
    """The helper writes one row per call with all fields populated."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(
        hook_name="observer", exc=RuntimeError("simulated failure")
    )

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT hook_name, exception_type, exception_message "
            "FROM hook_errors"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["hook_name"] == "observer"
    assert rows[0]["exception_type"] == "RuntimeError"
    assert rows[0]["exception_message"] == "simulated failure"


def test_record_hook_error_swallows_db_failure(monkeypatch) -> None:
    """If the DB write itself fails, the helper returns silently
    (hooks must never raise)."""
    from better_memory.hooks import _error_log
    from better_memory.hooks._error_log import record_hook_error

    def _raising_connect(*args, **kwargs):
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(_error_log, "connect", _raising_connect)

    # Must NOT raise.
    record_hook_error(
        hook_name="observer", exc=RuntimeError("original error")
    )


def test_record_hook_error_records_traceback(db_path: Path) -> None:
    """The traceback string from sys.exc_info is captured."""
    from better_memory.hooks._error_log import record_hook_error

    try:
        raise ValueError("inner exception with traceback")
    except ValueError as exc:
        record_hook_error(hook_name="observer", exc=exc)

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT traceback FROM hook_errors"
        ).fetchone()
    finally:
        conn.close()
    assert row["traceback"] is not None
    assert "ValueError" in row["traceback"]


def test_record_hook_error_records_cwd(db_path: Path) -> None:
    """os.getcwd() is captured for diagnosability."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(hook_name="observer", exc=RuntimeError("x"))

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT cwd FROM hook_errors").fetchone()
    finally:
        conn.close()
    assert row["cwd"] is not None
    assert len(row["cwd"]) > 0


def test_record_hook_error_uses_uuid_id(db_path: Path) -> None:
    """Each row gets a unique UUID id."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(hook_name="observer", exc=RuntimeError("a"))
    record_hook_error(hook_name="observer", exc=RuntimeError("b"))

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM hook_errors").fetchall()
    finally:
        conn.close()
    ids = {row["id"] for row in rows}
    assert len(ids) == 2  # both unique
    for id_ in ids:
        assert len(id_) == 32  # uuid4().hex is 32 chars
```

- [ ] **Step 2: Run, confirm FAIL**

```bash
uv run pytest tests/hooks/test_error_log.py -v 2>&1 | tail -10
```

Expected: 5 FAIL — `ModuleNotFoundError: No module named 'better_memory.hooks._error_log'`.

- [ ] **Step 3: Create the helper module**

Create `better_memory/hooks/_error_log.py`:

```python
"""Best-effort hook error recorder. Writes to hook_errors table.

Hooks MUST NOT fail. This helper is wrapped in a defensive
try/except BaseException so a DB write failure (locked file,
missing migration, etc.) cannot break the hook itself.
"""

from __future__ import annotations

import os
import traceback
import uuid
from datetime import UTC, datetime

from better_memory.config import resolve_home
from better_memory.db.connection import connect


def record_hook_error(*, hook_name: str, exc: BaseException) -> None:
    """Write one row to hook_errors. Swallows ALL exceptions.

    Caller is expected to be inside a hook's broad except block;
    this helper extends that defensive posture by ensuring its own
    DB write cannot escape.
    """
    try:
        home = resolve_home()
        db_path = home / "memory.db"
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO hook_errors "
                "(id, created_at, hook_name, exception_type, "
                " exception_message, traceback, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    datetime.now(UTC).isoformat(),
                    hook_name,
                    type(exc).__name__,
                    str(exc),
                    traceback.format_exc(),
                    os.getcwd(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except BaseException:  # noqa: BLE001
        pass
```

- [ ] **Step 4: Run, confirm PASS**

```bash
uv run pytest tests/hooks/test_error_log.py -v 2>&1 | tail -10
```

Expected: 5 PASS.

---

## Task 8: Wire `_error_log` into 4 hooks

**Files:**
- Modify: `better_memory/hooks/observer.py` (line 97 area)
- Modify: `better_memory/hooks/session_start.py` (line 119 area)
- Modify: `better_memory/hooks/session_close.py` (line 109 area)
- Modify: `better_memory/hooks/post_commit.py` (line 173 area)

The pattern for each: add lazy-import + helper call inside the existing top-level `except Exception:` block. Lazy import keeps the happy path zero-overhead.

- [ ] **Step 1: Update `better_memory/hooks/observer.py`**

Find:

```python
    except Exception:
        # Hooks MUST NOT fail. Swallow everything; a silent miss is far
```

Replace with:

```python
    except Exception as _exc:
        # Hooks MUST NOT fail. Swallow everything; a silent miss is far
        # better than crashing Claude Code. Best-effort: record the
        # failure to hook_errors for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="observer", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
```

(Note: existing comment text is preserved verbatim except for the 3 new lines added at the top of the block.)

- [ ] **Step 2: Update `better_memory/hooks/session_start.py`**

Find (around line 119):

```python
    except Exception:
        # Hooks must never fail.
        pass
```

Replace with:

```python
    except Exception as _exc:
        # Hooks must never fail. Best-effort: record to hook_errors
        # for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_start", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
```

- [ ] **Step 3: Update `better_memory/hooks/session_close.py`**

Find (around line 109):

```python
    except Exception:
        # Hooks must never fail.
```

Replace with:

```python
    except Exception as _exc:
        # Hooks must never fail. Best-effort: record to hook_errors
        # for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="session_close", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
```

- [ ] **Step 4: Update `better_memory/hooks/post_commit.py`**

Find (around line 173):

```python
    except Exception:
        # Hooks must never fail.
```

Replace with:

```python
    except Exception as _exc:
        # Hooks must never fail. Best-effort: record to hook_errors
        # for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="post_commit", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
```

- [ ] **Step 5: Run the full pytest suite to confirm no regression**

```bash
rm -f junit.xml
uv run pytest --junit-xml=junit.xml -q --no-header > /tmp/t8.log 2>&1
echo "EXIT=$?"
python3 -c "import xml.etree.ElementTree as ET; r=ET.parse('junit.xml').getroot(); s=r if r.tag=='testsuite' else r.find('testsuite'); print('SUMMARY:', dict(s.attrib))"
rm -f junit.xml
```

Expected: all tests pass (the 4 hook files are still imported and run; existing hook tests keep working).

- [ ] **Step 6: Run ruff**

```bash
uv run ruff check better_memory/hooks/ tests/hooks/test_error_log.py 2>&1 | tail -3
```

Expected: All checks passed.

---

## Task 9: Commit 3 — Hook errors

```bash
git add \
  better_memory/hooks/_error_log.py \
  better_memory/hooks/observer.py \
  better_memory/hooks/session_start.py \
  better_memory/hooks/session_close.py \
  better_memory/hooks/post_commit.py \
  tests/hooks/test_error_log.py
git commit -m "$(cat <<'EOF'
feat(hooks): record_hook_error helper writes to hook_errors table

Hooks have always swallowed BaseException to avoid breaking Claude
Code, which means failures were invisible. This commit adds a
defensive _error_log.record_hook_error helper that writes one
row per failure to the new hook_errors table. The helper is itself
wrapped in try/except BaseException so a DB write failure can't
escape the hook.

All four hooks (observer, session_start, session_close, post_commit)
now call the helper from inside their existing broad-except. The
import is lazy (inside the except) so the happy path stays
zero-overhead.

Errors will be surfaced in the new /diagnostics UI (next commit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Diagnostics queries

**Files:**
- Modify: `better_memory/ui/queries.py` (append two dataclasses + three functions at end)

- [ ] **Step 1: Append the new dataclasses and queries to `queries.py`**

Add at the end of `better_memory/ui/queries.py`:

```python
@dataclass(frozen=True)
class HookErrorRow:
    id: str
    created_at: str
    hook_name: str
    exception_type: str
    exception_message: str | None
    traceback: str | None
    cwd: str | None


def hook_errors_list_for_ui(
    conn: sqlite3.Connection, *, limit: int = 100,
) -> list[HookErrorRow]:
    """Return recent hook errors, newest first."""
    rows = conn.execute(
        "SELECT id, created_at, hook_name, exception_type, "
        "       exception_message, traceback, cwd "
        "FROM hook_errors "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        HookErrorRow(
            id=r["id"],
            created_at=r["created_at"],
            hook_name=r["hook_name"],
            exception_type=r["exception_type"],
            exception_message=r["exception_message"],
            traceback=r["traceback"],
            cwd=r["cwd"],
        )
        for r in rows
    ]


def hook_error_detail(
    conn: sqlite3.Connection, *, error_id: str,
) -> HookErrorRow | None:
    """Single row by id for the drawer view."""
    row = conn.execute(
        "SELECT id, created_at, hook_name, exception_type, "
        "       exception_message, traceback, cwd "
        "FROM hook_errors WHERE id = ?",
        (error_id,),
    ).fetchone()
    if row is None:
        return None
    return HookErrorRow(
        id=row["id"],
        created_at=row["created_at"],
        hook_name=row["hook_name"],
        exception_type=row["exception_type"],
        exception_message=row["exception_message"],
        traceback=row["traceback"],
        cwd=row["cwd"],
    )


@dataclass(frozen=True)
class RetentionRunRow:
    id: int
    run_at: str
    archived_via_retired_reflection: int
    archived_via_consumed_without_reflection: int
    archived_via_no_outcome_episode: int
    pruned: int
    triggered_by: str


def retention_runs_list_for_ui(
    conn: sqlite3.Connection, *, limit: int = 30,
) -> list[RetentionRunRow]:
    """Return recent retention runs, newest first."""
    rows = conn.execute(
        "SELECT id, run_at, archived_via_retired_reflection, "
        "       archived_via_consumed_without_reflection, "
        "       archived_via_no_outcome_episode, pruned, triggered_by "
        "FROM retention_runs "
        "ORDER BY run_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        RetentionRunRow(
            id=r["id"],
            run_at=r["run_at"],
            archived_via_retired_reflection=r["archived_via_retired_reflection"],
            archived_via_consumed_without_reflection=r[
                "archived_via_consumed_without_reflection"
            ],
            archived_via_no_outcome_episode=r["archived_via_no_outcome_episode"],
            pruned=r["pruned"],
            triggered_by=r["triggered_by"],
        )
        for r in rows
    ]
```

(Verify `dataclass` and `sqlite3` are already imported at the top of `queries.py` — they are, used by existing dataclasses there.)

---

## Task 11: Diagnostics routes

**Files:**
- Modify: `better_memory/ui/app.py` (add 6 new routes inside `create_app`)

- [ ] **Step 1: Add imports if not present**

At the top of `better_memory/ui/app.py`, ensure this import is present (add if missing):

```python
from better_memory.ui import queries
```

(It's likely already there — used by other routes. Verify with grep before adding.)

- [ ] **Step 2: Add the 6 new routes inside `create_app`**

Find a suitable spot in `create_app` — preferably right after the existing `/observations` routes group (search for `@app.post("/observations/synthesize")`). After that route's body ends, add:

```python
    @app.get("/diagnostics")
    def diagnostics() -> str:
        return render_template(
            "diagnostics.html", active_tab="diagnostics"
        )

    @app.get("/diagnostics/panel/hook-errors")
    def hook_errors_panel() -> str:
        conn = app.extensions["db_connection"]
        rows = queries.hook_errors_list_for_ui(conn)
        from itertools import groupby
        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.created_at[:10])
        ]
        return render_template(
            "fragments/panel_hook_errors.html", days=days
        )

    @app.get("/diagnostics/panel/retention-runs")
    def retention_runs_panel() -> str:
        conn = app.extensions["db_connection"]
        rows = queries.retention_runs_list_for_ui(conn)
        from itertools import groupby
        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.run_at[:10])
        ]
        return render_template(
            "fragments/panel_retention_runs.html", days=days
        )

    @app.get("/diagnostics/hook-errors/<id>/drawer")
    def hook_error_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.hook_error_detail(conn, error_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/hook_error_drawer.html", detail=detail
        )

    @app.post("/diagnostics/hook-errors/<id>/delete")
    def hook_error_delete(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        conn.execute(
            "DELETE FROM hook_errors WHERE id = ?", (id,)
        )
        conn.commit()
        return "", 200, {"HX-Trigger": "hook-errors-changed"}

    @app.post("/diagnostics/hook-errors/purge")
    def hook_errors_purge() -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        conn.execute("DELETE FROM hook_errors")
        conn.commit()
        return "", 200, {"HX-Trigger": "hook-errors-changed"}
```

(Verify `abort` is already imported — search for `from flask import` at the top of `app.py`. It probably is.)

---

## Task 12: Diagnostics templates

**Files:**
- Create: `better_memory/ui/templates/diagnostics.html`
- Create: `better_memory/ui/templates/fragments/panel_hook_errors.html`
- Create: `better_memory/ui/templates/fragments/hook_error_row.html`
- Create: `better_memory/ui/templates/fragments/hook_error_drawer.html`
- Create: `better_memory/ui/templates/fragments/panel_retention_runs.html`

- [ ] **Step 1: Create `diagnostics.html`**

```html
{% extends "base.html" %}
{% block title %}Diagnostics — better-memory{% endblock %}
{% block main %}
<section class="diagnostics">
  <h2>Hook errors</h2>
  <div id="hook-errors-panel"
       hx-get="{{ url_for('hook_errors_panel') }}"
       hx-trigger="load, every 30s, hook-errors-changed from:body"
       hx-swap="innerHTML">
  </div>

  <h2>Retention runs</h2>
  <div id="retention-runs-panel"
       hx-get="{{ url_for('retention_runs_panel') }}"
       hx-trigger="load, every 60s"
       hx-swap="innerHTML">
  </div>

  <div id="hook-error-drawer"></div>
</section>
{% endblock %}
```

- [ ] **Step 2: Create `fragments/panel_hook_errors.html`**

```html
{% if days %}
  <div class="panel-actions">
    <button class="purge-all"
            hx-post="{{ url_for('hook_errors_purge') }}"
            hx-confirm="Delete ALL hook errors? This cannot be undone."
            hx-swap="none">Clear all</button>
  </div>
  {% for day, rows in days %}
    <div class="day">
      <h3 class="day-header">{{ day }}</h3>
      {% for row in rows %}
        {% include "fragments/hook_error_row.html" %}
      {% endfor %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty-state"><p>No hook errors recorded.</p></div>
{% endif %}
```

- [ ] **Step 3: Create `fragments/hook_error_row.html`**

```html
<div class="card hook-error-row">
  <span class="time"
        hx-get="{{ url_for('hook_error_drawer', id=row.id) }}"
        hx-target="#hook-error-drawer"
        hx-swap="innerHTML">{{ row.created_at[11:16] }}</span>
  <span class="hook-name">{{ row.hook_name }}</span>
  <span class="exception-type">{{ row.exception_type }}</span>
  <span class="message">{{ row.exception_message or "—" }}</span>
  <button class="delete-row"
          hx-post="{{ url_for('hook_error_delete', id=row.id) }}"
          hx-confirm="Delete this error?"
          hx-swap="none">×</button>
</div>
```

- [ ] **Step 4: Create `fragments/hook_error_drawer.html`**

```html
<aside class="drawer">
  <header class="drawer-header">
    <span class="exception-type">{{ detail.exception_type }}</span>
    <span class="created-at">{{ detail.created_at }}</span>
  </header>

  <section class="metadata">
    <dl>
      <dt>hook</dt><dd>{{ detail.hook_name }}</dd>
      <dt>cwd</dt><dd>{{ detail.cwd or "—" }}</dd>
      <dt>id</dt><dd>{{ detail.id }}</dd>
    </dl>
  </section>

  <section class="message">
    <h4>Message</h4>
    <p>{{ detail.exception_message or "—" }}</p>
  </section>

  {% if detail.traceback %}
    <section class="traceback">
      <h4>Traceback</h4>
      <pre>{{ detail.traceback }}</pre>
    </section>
  {% endif %}
</aside>
```

- [ ] **Step 5: Create `fragments/panel_retention_runs.html`**

```html
{% if days %}
  {% for day, rows in days %}
    <div class="day">
      <h3 class="day-header">{{ day }}</h3>
      {% for row in rows %}
        <div class="card retention-run-row">
          <span class="time">{{ row.run_at[11:16] }}</span>
          <span class="trigger">{{ row.triggered_by }}</span>
          <span class="counts">
            archived: {{ row.archived_via_retired_reflection
                       + row.archived_via_consumed_without_reflection
                       + row.archived_via_no_outcome_episode }}
            (pruned: {{ row.pruned }})
          </span>
        </div>
      {% endfor %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty-state"><p>No retention runs yet.</p></div>
{% endif %}
```

---

## Task 13: Diagnostics nav-link addition

**Files:**
- Modify: `better_memory/ui/templates/base.html` (line 32 area)

- [ ] **Step 1: Add the Diagnostics nav link to `base.html`**

Find this in `better_memory/ui/templates/base.html` (line 32):

```html
      <a class="tab {% if active_tab == 'reflections' %}active{% endif %}" href="{{ url_for('reflections') }}">Reflections</a>
    </nav>
```

Replace with:

```html
      <a class="tab {% if active_tab == 'reflections' %}active{% endif %}" href="{{ url_for('reflections') }}">Reflections</a>
      <a class="tab {% if active_tab == 'diagnostics' %}active{% endif %}" href="{{ url_for('diagnostics') }}">Diagnostics</a>
    </nav>
```

---

## Task 14: Diagnostics unit tests

**Files:**
- Create: `tests/ui/test_diagnostics.py`

- [ ] **Step 1: Create the test file**

```python
"""Unit tests for the /diagnostics page."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_hook_error(
    db_path: Path, *, error_id: str = "e-1",
    hook_name: str = "observer",
    created_at: str = "2026-05-03T10:00:00+00:00",
) -> None:
    from better_memory.db.connection import connect
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO hook_errors "
            "(id, created_at, hook_name, exception_type, "
            " exception_message, traceback, cwd) "
            "VALUES (?, ?, ?, 'RuntimeError', 'simulated', "
            " 'Traceback...', '/tmp/cwd')",
            (error_id, created_at, hook_name),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_retention_run(
    db_path: Path, *,
    run_at: str = "2026-05-03T08:00:00+00:00",
    triggered_by: str = "retrieve",
) -> None:
    from better_memory.db.connection import connect
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO retention_runs "
            "(run_at, archived_via_retired_reflection, "
            " archived_via_consumed_without_reflection, "
            " archived_via_no_outcome_episode, pruned, triggered_by) "
            "VALUES (?, 5, 2, 1, 0, ?)",
            (run_at, triggered_by),
        )
        conn.commit()
    finally:
        conn.close()


class TestDiagnosticsPage:
    def test_diagnostics_page_renders(self, client) -> None:
        resp = client.get("/diagnostics")
        assert resp.status_code == 200
        assert b"Hook errors" in resp.data
        assert b"Retention runs" in resp.data
        assert b"diagnostics" in resp.data  # active_tab

    def test_panel_hook_errors_renders_recent_errors(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        resp = client.get("/diagnostics/panel/hook-errors")
        assert resp.status_code == 200
        assert b"observer" in resp.data
        assert b"RuntimeError" in resp.data

    def test_panel_hook_errors_empty_state(self, client) -> None:
        resp = client.get("/diagnostics/panel/hook-errors")
        assert resp.status_code == 200
        assert b"No hook errors" in resp.data

    def test_panel_retention_runs_renders_recent_runs(
        self, client, tmp_db: Path
    ) -> None:
        _seed_retention_run(tmp_db)
        resp = client.get("/diagnostics/panel/retention-runs")
        assert resp.status_code == 200
        assert b"retrieve" in resp.data
        assert b"archived: 8" in resp.data  # 5+2+1

    def test_hook_error_drawer_renders_traceback(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        resp = client.get("/diagnostics/hook-errors/e-1/drawer")
        assert resp.status_code == 200
        assert b"Traceback..." in resp.data

    def test_hook_error_drawer_returns_404_when_missing(
        self, client
    ) -> None:
        resp = client.get("/diagnostics/hook-errors/missing/drawer")
        assert resp.status_code == 404

    def test_delete_hook_error_removes_row(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-del")
        resp = client.post(
            "/diagnostics/hook-errors/e-del/delete",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "hook-errors-changed"

        from better_memory.db.connection import connect
        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT id FROM hook_errors WHERE id = ?", ("e-del",)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_purge_all_removes_all_rows(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        _seed_hook_error(tmp_db, error_id="e-2")
        resp = client.post(
            "/diagnostics/hook-errors/purge",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "hook-errors-changed"

        from better_memory.db.connection import connect
        conn = connect(tmp_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM hook_errors"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_delete_returns_403_without_origin(self, client) -> None:
        resp = client.post(
            "/diagnostics/hook-errors/x/delete"
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/ui/test_diagnostics.py -v 2>&1 | tail -15
```

Expected: 9 PASS.

---

## Task 15: Diagnostics browser tests

**Files:**
- Create: `tests/ui/test_browser_diagnostics.py`

**Confidence: 92%** (raised from 88% by applying concrete flakiness mitigations).

**Real flakiness sources for THIS test file** (after reading `conftest_browser.py` — `ui_url` is function-scoped, so cross-test pollution is NOT a concern):

1. **HTMX async load race** — `page.goto()` returns before the `hx-get` panel content arrives.
2. **Two panels load in parallel** — assertions must target the SPECIFIC panel selector, not generic page state.
3. **HTMX trigger-event refresh after action** — delete → POST → `HX-Trigger` → panel re-fetch → table update. Multiple hops can race a bare assertion.
4. **Confirm dialog ordering** — `hx-confirm` JS dialog must have its handler registered BEFORE the click that triggers it.

**Mitigations applied below** (each is in the test code):

- **`expect(...).to_be_visible(timeout=10000)`** instead of `wait_for_selector` + bare assertion. `expect()` has built-in polling/retry up to its timeout — kills the post-action race directly.
- **Register dialog handler immediately after `page.goto`**, not just before the click. Order safety.
- **`expect(...).not_to_be_visible(timeout=10000)`** for negative assertions ("row should be gone after delete") instead of asserting immediately after the click.
- **Wait for the SPECIFIC selector** the assertion cares about (`.hook-error-row`, not `.diagnostics`).
- Mark `@pytest.mark.integration` so tests only run when explicitly requested.

- [ ] **Step 1: Create the browser test file**

```python
"""Playwright integration tests for the Diagnostics tab.

All tests use expect() with explicit timeouts (>= 10s) instead of bare
asserts to absorb HTMX async-load races. The conftest_browser ui_url
fixture is function-scoped — each test gets its own UI subprocess +
fresh DB, so no cross-test pollution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from better_memory.db.connection import connect

pytest_plugins = ["tests.ui.conftest_browser"]

# Generous timeout for HTMX-driven changes (load + every-30s + custom
# events). Default Playwright expect() timeout is 5s; this codebase has
# observed flakes around 5s on Windows under load.
_HTMX_TIMEOUT_MS = 10_000


def _seed_hook_error(
    db_path: Path, *, error_id: str = "e-1",
    hook_name: str = "observer",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO hook_errors "
            "(id, created_at, hook_name, exception_type, "
            " exception_message, traceback, cwd) "
            "VALUES (?, '2026-05-03T10:00:00+00:00', ?, "
            " 'RuntimeError', 'visible message', "
            " 'Traceback (most recent call last):', '/tmp')",
            (error_id, hook_name),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
def test_diagnostics_tab_visible_in_nav(
    ui_url: tuple[str, Path], page: Page
) -> None:
    """Nav link renders on every page."""
    url, _home = ui_url
    page.goto(f"{url}/")
    expect(
        page.get_by_role("link", name="Diagnostics")
    ).to_be_visible(timeout=_HTMX_TIMEOUT_MS)


@pytest.mark.integration
def test_hook_errors_panel_lists_seeded_rows(
    ui_url: tuple[str, Path], page: Page
) -> None:
    """Panel renders the seeded row's text after HTMX load completes."""
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")

    page.goto(f"{url}/diagnostics")
    # expect() polls up to timeout — absorbs the HTMX panel-load race.
    expect(page.locator(".hook-error-row")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    expect(page.get_by_text("visible message")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    expect(page.get_by_text("observer")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )


@pytest.mark.integration
def test_clicking_error_opens_drawer(
    ui_url: tuple[str, Path], page: Page
) -> None:
    """Drawer renders the full traceback when a row is clicked."""
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")

    page.goto(f"{url}/diagnostics")
    expect(page.locator(".hook-error-row")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".hook-error-row .time").first.click()
    # Drawer load is another HTMX hop — expect() polls.
    expect(page.locator("#hook-error-drawer")).to_contain_text(
        "Traceback (most recent call last):", timeout=_HTMX_TIMEOUT_MS
    )


@pytest.mark.integration
def test_per_row_delete_removes_the_row(
    ui_url: tuple[str, Path], page: Page
) -> None:
    """After delete + HX-Trigger refresh, the row is gone from the panel."""
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")
    _seed_hook_error(db_path, error_id="e-2")

    # Register dialog handler IMMEDIATELY after navigation, before any
    # click that might trigger a confirm. Ordering matters.
    page.goto(f"{url}/diagnostics")
    page.on("dialog", lambda d: d.accept())

    expect(page.locator(".hook-error-row")).to_have_count(
        2, timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".hook-error-row .delete-row").first.click()
    # After click → POST → HX-Trigger → panel re-fetch → DOM update.
    # expect() with a generous timeout absorbs all the hops.
    expect(page.locator(".hook-error-row")).to_have_count(
        1, timeout=_HTMX_TIMEOUT_MS
    )


@pytest.mark.integration
def test_purge_all_clears_panel(
    ui_url: tuple[str, Path], page: Page
) -> None:
    """After purge-all + HX-Trigger refresh, the empty state shows."""
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")
    _seed_hook_error(db_path, error_id="e-2")

    # Register dialog handler IMMEDIATELY after navigation.
    page.goto(f"{url}/diagnostics")
    page.on("dialog", lambda d: d.accept())

    expect(page.locator(".purge-all")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".purge-all").click()
    # Negative assertion: rows should disappear; empty state appears.
    expect(page.locator(".hook-error-row")).not_to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    expect(page.get_by_text("No hook errors recorded")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
```

- [ ] **Step 2: Run, confirm PASS** (skip if Playwright browser binaries not installed)

```bash
uv run pytest tests/ui/test_browser_diagnostics.py -v -m integration 2>&1 | tail -10
```

Expected: 5 PASS, OR all skipped if Playwright browser binaries aren't installed.

(Note: 5 tests now, not 4 — added the per-row delete test that was missing from the earlier draft.)

---

## Task 16: Commit 4 — `/diagnostics` UI

```bash
git add \
  better_memory/ui/queries.py \
  better_memory/ui/app.py \
  better_memory/ui/templates/base.html \
  better_memory/ui/templates/diagnostics.html \
  better_memory/ui/templates/fragments/panel_hook_errors.html \
  better_memory/ui/templates/fragments/hook_error_row.html \
  better_memory/ui/templates/fragments/hook_error_drawer.html \
  better_memory/ui/templates/fragments/panel_retention_runs.html \
  tests/ui/test_diagnostics.py \
  tests/ui/test_browser_diagnostics.py
git commit -m "$(cat <<'EOF'
feat(ui): /diagnostics page with hook errors + retention runs panels

New operational surface for visibility into background processes.

- Hook errors panel: lists recent failures recorded by the new
  hooks._error_log helper. Per-row delete button + bulk "Clear all".
  Click any row to open a drawer with the full traceback.
- Retention runs panel: append-only audit log of RetentionScheduler
  activity. Counts per archive rule + pruned count + triggered_by.
- New "Diagnostics" link in the nav (after Reflections).

HTMX trigger pattern: deletes/purges return HX-Trigger:
hook-errors-changed; the panel listens for that event so the table
refreshes after each action — mirrors the existing
observations-synthesized pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: README + spec/plan commit

**Files:**
- Modify: `README.md` (add `BETTER_MEMORY_AUTO_PRUNE` row to Configuration table)
- The spec and plan files already exist; they get committed here.

- [ ] **Step 1: Update README.md Configuration section**

Find the existing Configuration table in `README.md` (search for `BETTER_MEMORY_HOME`). Add this row after the existing rows:

```markdown
| `BETTER_MEMORY_AUTO_PRUNE` | (unset = false) | When set to `1`, the auto-retention runner (which fires on `memory.retrieve`, throttled to once per 24h) ALSO hard-deletes archived observations older than 365 days. **Irreversible.** Default is archive-only (status flip, reversible). Opt in only if you actively want disk space reclaimed. |
```

- [ ] **Step 2: Commit**

```bash
git add \
  docs/superpowers/specs/2026-05-03-background-lifecycle-design.md \
  docs/superpowers/plans/2026-05-03-background-lifecycle.md \
  README.md
git commit -m "$(cat <<'EOF'
docs(superpowers): spec + plan for phase-c + README BETTER_MEMORY_AUTO_PRUNE

Captures the design and TDD-shaped implementation plan for phase C
of the tech-debt audit (auto-driven retention via memory.retrieve
trigger + DB-backed hook error visibility on /diagnostics).

README gains a BETTER_MEMORY_AUTO_PRUNE row in the Configuration
table — explains opt-in pruning (irreversible) vs default archive
(reversible).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 18: Final verification

- [ ] **Step 1: Confirm 5 commits ahead of main**

```bash
git log --oneline main..HEAD
```

Expected: 5 commits (migration → scheduler → hooks → UI → docs).

- [ ] **Step 2: Run full pytest suite**

```bash
rm -f junit.xml
uv run pytest --junit-xml=junit.xml -q --no-header > /tmp/final.log 2>&1
echo "EXIT=$?"
python3 -c "import xml.etree.ElementTree as ET; r=ET.parse('junit.xml').getroot(); s=r if r.tag=='testsuite' else r.find('testsuite'); print('SUMMARY:', dict(s.attrib))"
rm -f junit.xml
```

Expected: `EXIT=0`, ~25 new tests pass on top of baseline.

- [ ] **Step 3: Run ruff**

```bash
uv run ruff check better_memory tests 2>&1 | tail -5
```

Expected: No NEW errors over baseline (28 pre-existing as of phase B).

---

## Task 19: Push + open PR (USER GATED)

- [ ] **Step 1: PAUSE — ask user before push**

Surface to user:

> "Branch `phase-c` ready with 5 commits ahead of main. Full pytest passes. Want me to push and open a PR?"

Wait for explicit confirmation. **Do not push without it.**

- [ ] **Step 2: On confirmation, push and open PR**

```bash
git push -u origin phase-c
gh pr create --title "phase-c: auto-driven retention + DB-backed hook error visibility" --body "$(cat <<'EOF'
## Summary

Phase C of the tech-debt audit. Two coherent operational improvements:

- **feat(retention):** Auto-driven retention. `RetentionScheduler` invokes `RetentionService` on every `memory.retrieve` call, guarded by a 24h "too soon" check (consults the new `retention_runs` audit table). Default archive-only; opt-in pruning via `BETTER_MEMORY_AUTO_PRUNE=1`.
- **feat(hooks):** Hook error visibility. New `hook_errors` table + `record_hook_error` helper called from each hook's broad-except. Failures are recorded for `/diagnostics` visibility instead of silently swallowed.
- **feat(ui):** New `/diagnostics` page with two panels — hook errors (delete + purge actions, click-to-drawer for full traceback) and retention runs (audit history). Follows the established UI pattern (extends `base.html`, HTMX-driven panels).

Spec: `docs/superpowers/specs/2026-05-03-background-lifecycle-design.md`
Plan: `docs/superpowers/plans/2026-05-03-background-lifecycle.md`

## Test plan

- [x] `uv run pytest -q` — ~25 new tests pass on top of baseline
- [x] `uv run ruff check .` — no new errors over baseline
- [ ] Manual smoke: trigger a hook error (e.g. break BETTER_MEMORY_HOME briefly); observe row appears in `/diagnostics`; click it to see traceback; delete it.
- [ ] Manual smoke: call `memory.retrieve` and observe a `retention_runs` row appears.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Memory sweep before declaring done**

Per CLAUDE.md mandatory triggers ("at the end of each phase / PR cycle"), pause and review what was learned.

Candidates this cycle:
- The "verify-before-commit applies to internal patterns too" lesson (already recorded as `e7961c0c` during spec design)
- Any unexpected failures during implementation
- Any deviations the implementer made

If nothing new rose to the bar, no additional observation is needed.

---

## Self-Review

**Spec coverage:**

| Spec section | Plan task |
|---|---|
| Schema migration | Task 2 |
| Config `auto_prune` field | Task 3 |
| RetentionScheduler module | Task 4 |
| Wire scheduler into `mcp/server.py` | Task 5 |
| Commit 2 | Task 6 |
| `_error_log.py` helper | Task 7 |
| Hook integration (4 hooks) | Task 8 |
| Commit 3 | Task 9 |
| Diagnostics queries | Task 10 |
| Diagnostics routes | Task 11 |
| Diagnostics templates (5 new) | Task 12 |
| Diagnostics nav-link | Task 13 |
| Diagnostics unit tests | Task 14 |
| Diagnostics browser tests | Task 15 |
| Commit 4 | Task 16 |
| README + spec/plan commit | Task 17 |
| Final verification | Task 18 |
| Push + PR (USER GATED) | Task 19 |

No spec gaps.

**Placeholder scan:** No "TBD", "TODO", "implement later", "similar to Task N." Every code step has full code; every command step has the exact command and expected output.

**Type consistency:** `RetentionScheduler.__init__(conn, *, auto_prune, clock=None)` — same signature in module + tests. `RetentionReport` dataclass fields used consistently. `HookErrorRow` and `RetentionRunRow` field names match between dataclass definitions, query SQL, and template usage. Migration filename `0005_phase_c.sql` is consistent across spec, plan, and commit message.

The plan is ready to execute.
