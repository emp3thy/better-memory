# Background lifecycle (sub-design C) — design

**Status:** Approved 2026-05-03
**Branch target:** `phase-c` (already created off `main`)
**Predecessor:** sub-design B (`2026-05-02-synthesis-route-hardening-design.md`) — landed in PR #18
**Successor sub-design:** D (type & identity infra) — out of scope here.

## Goal

Two coherent operational improvements bundled into a single PR:

1. **Auto-driven retention.** `RetentionService.run()` exists and works but is invoked only manually via the `memory.run_retention` MCP tool. As a result, observations that should be `archived` per spec §9 stay `active` indefinitely, bloating retrieval. This spec drives retention automatically via two triggers (session-start hook + `memory.retrieve`) guarded by a 24h cadence.

2. **Hook error visibility.** Hooks (`observer.py`, `session_start.py`, `session_close.py`, `post_commit.py`) all swallow exceptions silently per "Hooks must never fail" — correct policy, but invisibility means a broken hook is undetectable until manually investigated. This spec adds a `hook_errors` SQLite table that hooks defensively write to on failure, plus a `/diagnostics` UI page to view and purge.

## Why now

Both issues were flagged in the original tech-debt audit (items #4 and #9). Bundling them is justified because they share infra (the new `/diagnostics` page hosts visibility for both) and because retention's own audit trail (`retention_runs` table) is the same shape as `hook_errors` — both append-only operational logs queryable from the same UI.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Retention trigger | `memory.retrieve` only (NOT session-start hook), guarded by single `last_retention_at` 24h check | Originally chose dual-trigger (c). Verify-before-commit (memory `dde30588`) found `hooks/session_start.py` is spool-write only with NO SQLite connection — adding retention there would force a 50–200ms hook-startup penalty plus a cross-module import the hook deliberately avoids. Per CLAUDE.md the user mandates `memory.retrieve` at every session start, making the session_start trigger redundant. Same coverage, lower cost. |
| Where the timestamp lives | New `retention_runs` table | Mirrors existing `synth_runs` precedent. Audit trail (counts + trigger source) is genuinely useful for debugging "did retention run last week?" |
| Cadence | 24h hardcoded | Retention work uses `retention_days=90` so eligible-row set changes by a few per day. 24h is "boringly often, never wasteful." Promote to env var later if it becomes a complaint. |
| Auto-retention behaviour | Archive by default; opt-in prune via `BETTER_MEMORY_AUTO_PRUNE` env var | Archive is reversible (status flag). Prune is irreversible. Opt-in protects users who don't realise auto-cleanup is happening. |
| README documentation | Document `BETTER_MEMORY_AUTO_PRUNE=1` in README's Configuration section | What it does, when to use it, irreversibility caveat. |
| Hook error storage | SQLite table `hook_errors` (NOT a log file) | Visible in UI, queryable, purgeable. Log files are write-only and require terminal access. |
| Hook error UI | New `/diagnostics` page hosting BOTH panels (hook errors + retention runs) at launch | Both panels use the same shape (recent-first chronological list). Single nav item. Diagnostics is the operational surface. |
| UI actions | Per-row delete + bulk purge for hook errors | Per-row for "I fixed this specific issue, clear the noise"; bulk for "start fresh." |
| Configuration injection | Extend `Config` dataclass with `auto_prune: bool` field; `RetentionScheduler.__init__(conn, *, auto_prune)` takes it explicitly | Cleaner than env-reads inside the scheduler (testable without monkeypatching env, no subprocess leakage). |

## Approach

Single branch (`phase-c`), four logical commits + spec/plan:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `chore(db): 0005_phase_c.sql migration for retention_runs + hook_errors` | `better_memory/db/migrations/0005_phase_c.sql` (new), `tests/db/test_phase_c_migration.py` (new) | Schema |
| 2 | `feat(retention): RetentionScheduler with retrieve-trigger and 24h guard` | `better_memory/config.py` (add `auto_prune` field), `better_memory/services/retention_scheduler.py` (new), `better_memory/mcp/server.py` (call `maybe_run` after spool drain), `tests/services/test_retention_scheduler.py` (new) | Feature |
| 3 | `feat(hooks): record_hook_error helper writes to hook_errors table` | `better_memory/hooks/_error_log.py` (new), `better_memory/hooks/observer.py`, `better_memory/hooks/session_start.py`, `better_memory/hooks/session_close.py`, `better_memory/hooks/post_commit.py` (call helper from each broad except), `tests/hooks/test_error_log.py` (new), updates to existing hook tests | Feature |
| 4 | `feat(ui): /diagnostics page with hook errors + retention runs panels` | `better_memory/ui/app.py`, `better_memory/ui/queries.py`, `better_memory/ui/templates/diagnostics.html` (new), `better_memory/ui/templates/fragments/{panel_hook_errors,panel_retention_runs}.html` (new), nav update in base template, `tests/ui/test_diagnostics.py` (new) | Feature |
| 5 | `docs(superpowers): spec + plan + README update` | `docs/superpowers/specs/2026-05-03-background-lifecycle-design.md` (this file), `docs/superpowers/plans/2026-05-03-background-lifecycle.md` (the plan), `README.md` (BETTER_MEMORY_AUTO_PRUNE section) | Docs |

## Commit 1 — Schema migration

New file `better_memory/db/migrations/0005_phase_c.sql`:

```sql
-- Phase C: retention scheduler audit trail + hook error visibility.

CREATE TABLE retention_runs (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    archived_via_retired_reflection INTEGER NOT NULL,
    archived_via_consumed_without_reflection INTEGER NOT NULL,
    archived_via_no_outcome_episode INTEGER NOT NULL,
    pruned INTEGER NOT NULL,
    triggered_by TEXT NOT NULL  -- 'session_start' | 'retrieve' | 'manual'
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

The migration runner (`apply_migrations` in `better_memory/db/schema.py:49`) picks this up automatically on the next MCP startup.

**Test:** `tests/db/test_phase_c_migration.py` opens a fresh connection, calls `apply_migrations`, and asserts both tables exist with the expected columns and indexes.

## Commit 2 — Retention scheduler

### Config extension

Extend `Config` dataclass in `better_memory/config.py`:

```python
@dataclass(frozen=True)
class Config:
    # ... existing fields ...
    auto_prune: bool

def get_config() -> Config:
    # ... existing lookups ...
    return Config(
        # ... existing kwargs ...
        auto_prune=_resolve_bool("BETTER_MEMORY_AUTO_PRUNE", default=False),
    )
```

### New module: `better_memory/services/retention_scheduler.py`

```python
"""24h-guarded retention runner.

Wraps RetentionService with a "has it run in the last 24h?" check
and an audit-trail row in retention_runs. Two callers (session_start
hook, memory.retrieve) both invoke maybe_run; the timestamp guard
ensures retention runs at most once per 24h regardless.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from better_memory.services.retention import RetentionService


_RETENTION_DAYS = 90
_PRUNE_AGE_DAYS = 365
_GUARD_HOURS = 24


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RetentionScheduler:
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

        Idempotent enough: if two callers race past the guard within
        ~50ms, both run RetentionService.run() and both write a row.
        SQLite serializes; second is essentially free (UPDATE matches
        zero new rows). Documented in the design as accepted.
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

    def _record_run(self, report, *, triggered_by: str) -> None:
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

### Wiring

**In `mcp/server.py`** — after spool drain in `memory.retrieve` (the existing `spool.drain()` call at `mcp/server.py:485`), call:
```python
try:
    RetentionScheduler(memory_conn, auto_prune=config.auto_prune).maybe_run(
        triggered_by="retrieve"
    )
except Exception:  # noqa: BLE001 — retention is best-effort
    pass
```

The MCP server already wraps spool drain in a similar best-effort except. A retention failure NEVER blocks `memory.retrieve`.

**Hooks NOT touched for retention.** The session_start hook stays spool-only (avoids the 50–200ms cost of opening a SQLite connection in a hook subprocess). Retention coverage is via the mandatory-at-session-start `memory.retrieve` per CLAUDE.md.

### Tests

`tests/services/test_retention_scheduler.py`:
- `test_first_call_runs_retention` — empty `retention_runs`, scheduler runs and writes a row
- `test_within_guard_skips` — controlled clock; row exists 1h ago; scheduler is no-op
- `test_after_guard_runs_again` — controlled clock; row exists 25h ago; scheduler runs
- `test_records_triggered_by` — caller passes `"retrieve"`, the row has it
- `test_records_counts_from_report` — RetentionService returns counts; row has them
- `test_auto_prune_false_passes_prune_false` — explicit `auto_prune=False`, observed via mocked RetentionService
- `test_auto_prune_true_passes_prune_true` — same with `auto_prune=True`

Tests use the explicit `auto_prune` parameter and a controlled clock — never `monkeypatch.setenv`.

## Commit 3 — Hook error DB writes

### New module: `better_memory/hooks/_error_log.py`

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
    """Write one row to hook_errors. Swallows all exceptions."""
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

### Hook integration

Each hook's broad except (e.g., `hooks/observer.py:97`) becomes:

```python
except Exception as exc:  # noqa: BLE001
    from better_memory.hooks._error_log import record_hook_error
    record_hook_error(hook_name="observer", exc=exc)
    # Hooks MUST NOT fail. Silent miss is far better than ...
```

The lazy import inside the except keeps the happy path zero-overhead (only imports if the hook actually errors).

### Tests

`tests/hooks/test_error_log.py`:
- `test_record_hook_error_inserts_row` — call helper, query DB, assert row
- `test_record_hook_error_swallows_db_failure` — patch `connect` to raise, helper returns None, no exception propagates
- One test per existing hook module verifying the broad except now calls the helper

## Commit 4 — `/diagnostics` UI

### Verified patterns (read at spec-write time)

- **`base.html` nav** has three tabs (Episodes / Observations / Reflections) using `<a class="tab {% if active_tab == 'X' %}active{% endif %}" href="{{ url_for('X') }}">X</a>`. Adding "Diagnostics" is one anchor after Reflections.
- **Page template pattern** (verified in `observations.html`): extends `base.html`, sets `{% block title %}`, contains a `<section>` with `<div hx-get="..." hx-trigger="load, every 30s, ..." hx-swap="innerHTML">` panel container plus an optional `<div id="X-drawer">`.
- **Panel fragment pattern** (verified in `panel_observations.html`): iterates `days` (list of `(day_str, rows)` tuples) with `<h3 class="day-header">{{ day }}</h3>`, includes a row fragment per row; renders `<div class="empty-state">` when no rows.
- **Row fragment pattern** (verified in `observation_row.html`): each row uses `<div class="card observation-row" hx-get="{{ url_for('observation_drawer', id=row.id) }}" hx-target="#X-drawer" hx-swap="innerHTML">` for click-to-detail.
- **Browser test pattern** (verified in `test_browser_observations.py`): uses `ui_url` fixture from `tests.ui.conftest_browser` returning `(url, home_path)`; seeds via direct `connect()` + INSERT; marks `@pytest.mark.integration`; asserts via `page.wait_for_selector` and `expect(page.get_by_text(...)).to_be_visible()`.

### Routes (in `ui/app.py`)

- `GET /diagnostics` → renders `diagnostics.html` with `active_tab='diagnostics'`
- `GET /diagnostics/panel/hook-errors` → renders `panel_hook_errors.html`
- `GET /diagnostics/panel/retention-runs` → renders `panel_retention_runs.html`
- `GET /diagnostics/hook-errors/<id>/drawer` → renders `hook_error_drawer.html` (full traceback view — multi-line content too big for the row)
- `POST /diagnostics/hook-errors/<id>/delete` → DELETE single row, return empty fragment + `HX-Trigger: hook-errors-changed` so the panel re-fetches
- `POST /diagnostics/hook-errors/purge` → DELETE all rows, return empty fragment + same `HX-Trigger`

All POSTs require `Origin` header per existing `_origin_check` (memory `35056f4b`).

### Queries (in `ui/queries.py`)

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
    """Return recent hook errors, newest first, grouped by day at the
    template layer (mirrors observation_list_for_ui's shape)."""


def hook_error_detail(
    conn: sqlite3.Connection, *, error_id: str,
) -> HookErrorRow | None:
    """Single row by id for the drawer view."""


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
```

### Template structure (concrete)

**`templates/diagnostics.html`** (extends base, single `<section>` containing both panels + drawer):

```html
{% extends "base.html" %}
{% block title %}Diagnostics — better-memory{% endblock %}
{% block main %}
<section class="diagnostics">
  <h2>Hook errors</h2>
  <div id="hook-errors-panel"
       hx-get="{{ url_for('hook_errors_panel') }}"
       hx-trigger="load, every 30s, hook-errors-changed from:body"
       hx-swap="innerHTML"></div>

  <h2>Retention runs</h2>
  <div id="retention-runs-panel"
       hx-get="{{ url_for('retention_runs_panel') }}"
       hx-trigger="load, every 60s"
       hx-swap="innerHTML"></div>

  <div id="hook-error-drawer"></div>
</section>
{% endblock %}
```

**`templates/fragments/panel_hook_errors.html`** (top action bar + day-grouped rows):

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

**`templates/fragments/hook_error_row.html`**:

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

**`templates/fragments/panel_retention_runs.html`**: same day-grouped pattern with run counts displayed inline. No actions, no drawer (runs are append-only audit data).

**`templates/fragments/hook_error_drawer.html`**: shows full traceback in a `<pre>` block plus metadata (cwd, exception type, full message).

### Nav (one-line edit to `base.html`)

After the Reflections anchor, before the closing `</nav>`:

```html
<a class="tab {% if active_tab == 'diagnostics' %}active{% endif %}" href="{{ url_for('diagnostics') }}">Diagnostics</a>
```

### Tests

`tests/ui/test_diagnostics.py` (unit, mirrors `tests/ui/test_observations.py`):
- `test_diagnostics_page_renders` — GET returns 200 + nav shows Diagnostics active
- `test_panel_hook_errors_renders_recent_errors` — seed errors, panel HTML contains them
- `test_panel_hook_errors_empty_state` — no errors → empty-state markup
- `test_panel_retention_runs_renders_recent_runs` — seed runs, panel HTML contains them
- `test_hook_error_drawer_renders_traceback` — drawer view contains full traceback
- `test_delete_hook_error_removes_row` — POST delete, row gone from DB, response includes HX-Trigger
- `test_purge_all_removes_all_rows` — POST purge, table empty
- `test_delete_returns_403_without_origin` (covers `_origin_check`)
- `test_purge_returns_403_without_origin`

`tests/ui/test_browser_diagnostics.py` (Playwright, mirrors `test_browser_observations.py`):
- `test_diagnostics_tab_visible_in_nav`
- `test_hook_errors_panel_lists_seeded_rows`
- `test_clicking_error_opens_drawer`
- `test_delete_button_removes_row` (with confirm dialog accepted)

All browser tests use the `ui_url` fixture, marked `@pytest.mark.integration`.

## Commit 5 — Spec + plan + README

- Commit this spec file.
- Commit the plan file (will be created by `superpowers:writing-plans`).
- Add a `BETTER_MEMORY_AUTO_PRUNE` section to README.md's Configuration block.

## Confidence per commit

Per memory `dde30588` (apply confidence scoring; embed mitigations for sub-90%; verify-before-commit).

| Commit | Confidence | Notes |
|---|---|---|
| 1. Schema migration | 98% | Pure SQL; migration mechanism verified at `db/schema.py:49`. |
| 2. RetentionScheduler + retrieve wiring | 92% | Trigger simplified to retrieve-only after verify-before-commit found session_start hook would force a 50–200ms penalty. Cleaner than dual-trigger. Wiring is one call after the existing `spool.drain()` line at `mcp/server.py:485` (verified). |
| 3. Hook errors DB writes | 92% | Each hook's broad except gets a 2-line addition (lazy import + helper call). The `_error_log.py` helper is wrapped in `try/except BaseException` so a DB write failure can't break the hook. |
| 4. `/diagnostics` UI | 93% | Bumped from 88% after reading all reference patterns at spec-write time: `base.html` nav structure pinned (one-line addition); page template fully spec'd (extends base, two panels + drawer); panel/row/drawer fragment patterns spec'd verbatim from `panel_observations.html` + `observation_row.html`; browser-test pattern pinned from `test_browser_observations.py` (uses `ui_url` fixture, `@pytest.mark.integration`); HTMX trigger-on-change pattern (`HX-Trigger: hook-errors-changed`) mirrors the existing `observations-synthesized` event. No remaining unverified UI assumptions. |
| 5. Spec + plan + README | 98% | Pure docs. |

**Plan-wide confidence: ~93%.**

**Verify-before-commit checklist** (already verified at spec-write time):
- [x] `apply_migrations` mechanism: confirmed at `db/schema.py:49` (loads SQL files from `migrations/` dir)
- [x] `templates/base.html` exists at `better_memory/ui/templates/base.html`
- [x] `session_start.py` has NO DB connection (justified dropping the hook trigger)
- [x] `mcp/server.py:485` calls `spool.drain()` (the wiring point for retention-trigger)
- [ ] `templates/base.html` nav structure: read before editing in commit 4 (mitigation note above)

## Risks (accepted)

1. **Hook DB write latency on error path (assumption #2).** Accepted — only failing hooks pay; happy path unchanged.
2. **24h cadence is opinionated.** If real-world use exceeds the tolerance, expose as env var later. Single-line change.
3. **Diagnostics page nav crowding.** Adds a 4th nav link. Tolerable; consolidate if Phase D adds another.
4. **Single trigger means retention only runs when `memory.retrieve` is called.** A user who runs only writes (`memory.observe` + `memory.record_use` without retrieves) would never trigger retention. Acceptable: CLAUDE.md mandates retrieve at session start, so writers-only sessions don't really exist in this workflow.

## Out of scope

- Cadence as env var (deferred)
- Hook errors filter/search UI (just chronological)
- Spool inspection panel (future addition; not in audit)
- Pyright / Phase D items
- Per-tech retention policies (single global cadence)
