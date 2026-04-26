# Episodic Memory Phase 9 — Reflections UI Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase 8 Reflections placeholder with the real Reflections tab — a filterable list of reflections for the current project (filters: project · tech · phase · polarity · status · min confidence) with a per-reflection drawer that lists source observations + their owning episode's outcome and exposes confirm / retire / edit actions.

**Architecture:** New `ReflectionService` class in `better_memory/services/reflection.py` (sibling to the existing `ReflectionSynthesisService`) owns the three UI writes — `confirm` (`pending_review` → `confirmed`), `retire` (any active status → `retired`), `update_text` (in-place edit of `use_cases` / `hints`, blocked on retired/superseded). Two new sync read helpers in `better_memory/ui/queries.py`: `reflection_list_for_ui` (flat list with all six filters; `min_confidence` floor; ordered by confidence DESC, updated_at DESC) and `reflection_detail` (full reflection + source observations joined through `reflection_sources` and showing each observation's owning episode's `goal` / `outcome` / `close_reason`). Routes follow the existing HTMX-partial pattern from Phase 8: full-page render on initial GET, panel + drawer + edit-form swap inner HTML, filter form GETs to `/reflections/panel?...` and re-renders the list.

**Tech Stack:** Python 3.12 · Flask · Jinja2 · HTMX · SQLite + sqlite-vec + FTS5 · pytest · uv.

**Scope boundary.** Reflections tab confirm + retire + edit only.

**Out of scope** (deferred):
- **Promote-to-knowledge action** — flagged separately as a key follow-up; tracked in better-memory but no phase assignment yet. Drawer leaves room for it (the action button row in `reflection_drawer.html` can grow to fit later).
- **Bulk operations** (multi-select confirm / retire). Single-row actions only.
- **Free-text search across reflection content.** Filter panel is structured fields only; FTS5 is wired in the schema but unused by the UI.
- **Cross-project view.** Filters default to the current project (cwd basename); user can override via the `project` filter, but no curated cross-project lens.
- **Stripping old UI surfaces** (Phase 10) — Pipeline / Sweep / Knowledge / Audit / Graph routes still resolve.
- **End-to-end browser tests** (Phase 12).

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §8 Tab 2 (filter panel, row fields, drawer + actions); §13 (out-of-scope acknowledgement of promote-to-knowledge).

**Reference plans:** Phase 5 (`2026-04-22-episodic-phase-5-reflection-synthesis.md`) for the existing `ReflectionSynthesisService` shape; Phase 8 (`2026-04-25-episodic-phase-8-episodes-ui.md`) for the established Flask + HTMX + dark-theme conventions this plan extends.

---

## File Structure

**New files:**
- `better_memory/ui/templates/fragments/panel_reflections.html` — filtered list panel (HTMX swap target)
- `better_memory/ui/templates/fragments/reflection_row.html` — one row in the list (compact)
- `better_memory/ui/templates/fragments/reflection_drawer.html` — drawer with full reflection + source obs + actions
- `better_memory/ui/templates/fragments/reflection_filter_form.html` — top-of-tab filter form
- `better_memory/ui/templates/fragments/reflection_edit_form.html` — inline edit form (use_cases + hints)
- `tests/services/test_reflection_writes.py` — service-layer tests for `ReflectionService`
- `tests/ui/test_queries_reflections.py` — query-helper tests
- `tests/ui/test_reflections.py` — Flask test-client tests for routes

**Modified files:**
- `better_memory/services/reflection.py` — add new `ReflectionService` class (confirm / retire / update_text)
- `better_memory/ui/queries.py` — add `ReflectionListRow`, `ReflectionDetail`, `ReflectionSourceObservation`, `reflection_list_for_ui`, `reflection_detail`
- `better_memory/ui/app.py` — register `ReflectionService`; replace `/reflections` placeholder; add `/reflections/panel`, `/reflections/<id>/drawer`, `POST /reflections/<id>/confirm`, `POST /reflections/<id>/retire`, `GET/POST /reflections/<id>/edit`
- `better_memory/ui/templates/reflections.html` — replace placeholder with full page (filter form + panel + drawer divs)
- `better_memory/ui/static/app.css` — append reflections-tab styles (dark-theme tokens matching Phase 8)
- `better_memory/skills/CLAUDE.snippet.md` — add Reflections tab note alongside Episodes tab in the management-UI section

---

## Task 0: Create worktree off the integration branch

Phase 9 builds on Phase 8 — when this plan executes, Phase 8 must already be merged into `episodic-phase-1-schema` (PR #4). Branch from the latest `origin/episodic-phase-1-schema`.

**Files:** Create worktree at `C:/Users/gethi/source/better-memory-episodic-phase-9-reflections-ui`.

- [ ] **Step 1: Confirm Phase 8 has landed**

```bash
git fetch origin
git log origin/episodic-phase-1-schema --oneline | head -5
```

Expected: the latest commit references Phase 8 (e.g. `Merge pull request #4 from emp3thy/episodic-phase-8-episodes-ui`). If not, **stop** and merge Phase 8 first.

- [ ] **Step 2: Create worktree**

```bash
git worktree add -b episodic-phase-9-reflections-ui \
  ../better-memory-episodic-phase-9-reflections-ui origin/episodic-phase-1-schema
```

- [ ] **Step 3: Verify worktree state**

```bash
cd ../better-memory-episodic-phase-9-reflections-ui
git status
```

Expected: `On branch episodic-phase-9-reflections-ui`, working tree clean.

- [ ] **Step 4: Verify baseline suite is green**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `382 passed, 141 skipped, 4 deselected` (the suite count Phase 8 lands at). If the count differs, the plan's expected counts in later tasks are off by the delta — adjust each task's expected total by the same amount.

---

## Task 1: Add `ReflectionService` (confirm / retire / update_text)

`ReflectionSynthesisService` already lives in `better_memory/services/reflection.py` and owns synthesis writes plus the bucketed `retrieve_reflections` reader. Add a new sibling class in the same file for the three UI write actions.

**Files:**
- Modify: `better_memory/services/reflection.py` (append new class)
- Create: `tests/services/test_reflection_writes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_reflection_writes.py`:

```python
"""Tests for ReflectionService (UI write actions: confirm / retire / update_text)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionService


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
    fixed = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_reflection(conn, reflection_id: str, status: str = "pending_review") -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, created_at, updated_at) "
        "VALUES (?, ?, 'proj-a', 'general', 'do', 'old uc', 'old h', "
        "0.7, ?, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')",
        (reflection_id, f"title-{reflection_id}", status),
    )
    conn.commit()


class TestConfirm:
    def test_confirms_pending_review(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.confirm(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_confirm_is_idempotent_on_already_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        # No exception; status stays confirmed; updated_at NOT bumped (no-op).
        svc.confirm(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["updated_at"] == "2026-04-25T00:00:00+00:00"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.confirm(reflection_id="nope")

    def test_raises_when_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot confirm reflection in status 'retired'"):
            svc.confirm(reflection_id="r1")

    def test_raises_when_superseded(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="superseded")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot confirm reflection in status 'superseded'"):
            svc.confirm(reflection_id="r1")


class TestRetire:
    def test_retires_pending_review(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "retired"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_retires_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")

        row = conn.execute(
            "SELECT status FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["status"] == "retired"

    def test_retire_is_idempotent_on_already_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")  # no-op, no exception

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "retired"
        assert row["updated_at"] == "2026-04-25T00:00:00+00:00"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.retire(reflection_id="nope")

    def test_raises_when_superseded(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="superseded")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot retire reflection in status 'superseded'"):
            svc.retire(reflection_id="r1")


class TestUpdateText:
    def test_updates_use_cases_and_hints(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(
            reflection_id="r1", use_cases="new uc", hints="new h"
        )

        row = conn.execute(
            "SELECT use_cases, hints, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["use_cases"] == "new uc"
        assert row["hints"] == "new h"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_works_on_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new h")

        row = conn.execute(
            "SELECT use_cases, hints FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["use_cases"] == "new uc"
        assert row["hints"] == "new h"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.update_text(reflection_id="nope", use_cases="x", hints="y")

    def test_raises_when_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot edit reflection in status 'retired'"):
            svc.update_text(reflection_id="r1", use_cases="x", hints="y")

    def test_raises_when_use_cases_empty(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="use_cases must not be empty"):
            svc.update_text(reflection_id="r1", use_cases="   ", hints="y")

    def test_raises_when_hints_empty(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="hints must not be empty"):
            svc.update_text(reflection_id="r1", use_cases="x", hints="")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/services/test_reflection_writes.py -v
```

Expected: 13 FAILs, all `ImportError: cannot import name 'ReflectionService'`.

- [ ] **Step 3: Implement `ReflectionService`**

Append to `better_memory/services/reflection.py` (after the existing `ReflectionSynthesisService` class):

```python
class ReflectionService:
    """UI-facing writes for reflections.

    Sibling of ``ReflectionSynthesisService``: this class does NOT
    synthesise — it handles the three lifecycle actions the user
    drives from the Reflections tab drawer:

    - ``confirm``: pending_review → confirmed (idempotent on confirmed).
    - ``retire``: pending_review/confirmed → retired (idempotent on retired).
    - ``update_text``: edit use_cases / hints in place; blocked on
      retired and superseded so we don't surprise the synthesis
      pipeline by mutating retired text.

    All three bump ``updated_at`` only when the row actually changes
    (no-op cases leave the timestamp untouched so reinforcement /
    audit trails stay honest).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    def confirm(self, *, reflection_id: str) -> None:
        """pending_review → confirmed; no-op on confirmed; raise on retired/superseded."""
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status == "confirmed":
            return
        if status != "pending_review":
            raise ValueError(
                f"Cannot confirm reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET status = 'confirmed', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()

    def retire(self, *, reflection_id: str) -> None:
        """pending_review / confirmed → retired; no-op on retired; raise on superseded."""
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status == "retired":
            return
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot retire reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET status = 'retired', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()

    def update_text(
        self, *, reflection_id: str, use_cases: str, hints: str
    ) -> None:
        """Edit ``use_cases`` and ``hints`` in place.

        Blocked on retired/superseded — once a reflection has left the
        active set, mutating its text would silently change the audit
        trail.
        """
        if not use_cases or not use_cases.strip():
            raise ValueError("use_cases must not be empty")
        if not hints or not hints.strip():
            raise ValueError("hints must not be empty")
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot edit reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET use_cases = ?, hints = ?, updated_at = ? "
            "WHERE id = ?",
            (use_cases, hints, now, reflection_id),
        )
        self._conn.commit()
```

`Callable` and `datetime` and `sqlite3` are already imported at the top of `reflection.py` — verify before adding.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/services/test_reflection_writes.py -v
```

Expected: 13 PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `395 passed, 141 skipped, 4 deselected` (13 new tests added to the 382 baseline).

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py tests/services/test_reflection_writes.py
git commit -m "Phase 9: ReflectionService — confirm / retire / update_text"
```

---

## Task 2: Add `reflection_list_for_ui` query helper

Sync SELECT helper that returns one flat list of reflections matching the six filter fields from spec §8. Ordered by confidence DESC, updated_at DESC. Capped via `limit` (default 100, matching the convention from Phase 8's `episode_list_for_ui`).

**Files:**
- Modify: `better_memory/ui/queries.py`
- Create: `tests/ui/test_queries_reflections.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_queries_reflections.py`:

```python
"""Tests for reflection-related UI query helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.queries import (
    ReflectionListRow,
    reflection_list_for_ui,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(
    conn,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    created_at: str = "2026-04-25T10:00:00+00:00",
    updated_at: str = "2026-04-25T10:00:00+00:00",
    evidence_count: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        "confidence, status, evidence_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, title or f"title-{rid}", project, tech, phase, polarity,
            use_cases, hints, confidence, status, evidence_count,
            created_at, updated_at,
        ),
    )
    conn.commit()


class TestReflectionListForUi:
    def test_returns_empty_when_no_reflections(self, conn):
        rows = reflection_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_only_active_statuses_by_default(self, conn):
        _seed(conn, rid="r-pending", status="pending_review")
        _seed(conn, rid="r-confirmed", status="confirmed")
        _seed(conn, rid="r-retired", status="retired")
        _seed(conn, rid="r-superseded", status="superseded")

        rows = reflection_list_for_ui(conn, project="proj-a")
        ids = {r.id for r in rows}
        assert ids == {"r-pending", "r-confirmed"}

    def test_includes_specific_status_when_filtered(self, conn):
        _seed(conn, rid="r-pending", status="pending_review")
        _seed(conn, rid="r-retired", status="retired")

        rows = reflection_list_for_ui(
            conn, project="proj-a", status="retired"
        )
        assert [r.id for r in rows] == ["r-retired"]

    def test_filters_by_project(self, conn):
        _seed(conn, rid="r-a", project="proj-a")
        _seed(conn, rid="r-b", project="proj-b")
        rows = reflection_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["r-a"]

    def test_filters_by_tech(self, conn):
        _seed(conn, rid="r-py", tech="python")
        _seed(conn, rid="r-go", tech="go")
        _seed(conn, rid="r-none", tech=None)

        rows = reflection_list_for_ui(
            conn, project="proj-a", tech="python"
        )
        assert [r.id for r in rows] == ["r-py"]

    def test_filters_by_phase(self, conn):
        _seed(conn, rid="r-plan", phase="planning")
        _seed(conn, rid="r-impl", phase="implementation")
        _seed(conn, rid="r-gen", phase="general")

        rows = reflection_list_for_ui(
            conn, project="proj-a", phase="planning"
        )
        assert [r.id for r in rows] == ["r-plan"]

    def test_filters_by_polarity(self, conn):
        _seed(conn, rid="r-do", polarity="do")
        _seed(conn, rid="r-dont", polarity="dont")

        rows = reflection_list_for_ui(
            conn, project="proj-a", polarity="dont"
        )
        assert [r.id for r in rows] == ["r-dont"]

    def test_filters_by_min_confidence(self, conn):
        _seed(conn, rid="r-low", confidence=0.3)
        _seed(conn, rid="r-mid", confidence=0.6)
        _seed(conn, rid="r-high", confidence=0.9)

        rows = reflection_list_for_ui(
            conn, project="proj-a", min_confidence=0.6
        )
        assert {r.id for r in rows} == {"r-mid", "r-high"}

    def test_orders_by_confidence_desc_then_updated_at_desc(self, conn):
        _seed(
            conn, rid="r-mid-newer", confidence=0.6,
            updated_at="2026-04-25T12:00:00+00:00",
        )
        _seed(
            conn, rid="r-mid-older", confidence=0.6,
            updated_at="2026-04-25T08:00:00+00:00",
        )
        _seed(conn, rid="r-high", confidence=0.9)

        rows = reflection_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == [
            "r-high", "r-mid-newer", "r-mid-older",
        ]

    def test_row_includes_all_spec_fields(self, conn):
        _seed(
            conn, rid="r-1", project="proj-a", tech="python",
            phase="implementation", polarity="dont", confidence=0.85,
            use_cases="when X happens",
            hints="do Y",
            title="my title", evidence_count=3,
        )
        [row] = reflection_list_for_ui(conn, project="proj-a")
        assert row.id == "r-1"
        assert row.title == "my title"
        assert row.project == "proj-a"
        assert row.tech == "python"
        assert row.phase == "implementation"
        assert row.polarity == "dont"
        assert row.confidence == 0.85
        assert row.status == "confirmed"
        assert row.use_cases == "when X happens"
        assert row.evidence_count == 3

    def test_limit_truncates_results(self, conn):
        for i in range(3):
            _seed(conn, rid=f"r-{i}", confidence=0.5 + i * 0.1)
        rows = reflection_list_for_ui(
            conn, project="proj-a", limit=2
        )
        assert len(rows) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_queries_reflections.py -v
```

Expected: 11 FAILs, `ImportError: cannot import name 'ReflectionListRow' from 'better_memory.ui.queries'`.

- [ ] **Step 3: Implement `ReflectionListRow` + `reflection_list_for_ui`**

Append to `better_memory/ui/queries.py`:

```python
@dataclass(frozen=True)
class ReflectionListRow:
    """Read model for one row in the Reflections list."""

    id: str
    title: str
    project: str
    tech: str | None
    phase: str
    polarity: str
    confidence: float
    status: str
    use_cases: str
    evidence_count: int
    updated_at: str


# Default status filter — matches retrieve_reflections (active set only).
_DEFAULT_REFLECTION_STATUSES = ("pending_review", "confirmed")


def reflection_list_for_ui(
    conn: sqlite3.Connection,
    *,
    project: str,
    tech: str | None = None,
    phase: str | None = None,
    polarity: str | None = None,
    status: str | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[ReflectionListRow]:
    """Return reflections matching the six filter fields from spec §8.

    Status semantics:
    - When ``status`` is None (default), returns rows with
      ``status IN ('pending_review', 'confirmed')`` — the active set,
      matching ``ReflectionSynthesisService.retrieve_reflections``.
    - When ``status`` is given, exact match on that single value
      (lets the user surface ``retired`` or ``superseded`` reflections
      explicitly).

    Order: ``confidence DESC, updated_at DESC, rowid DESC``.
    Cap: ``limit`` rows (default 100). ``min_confidence`` is a
    floor — rows with confidence strictly less than this are dropped.
    """
    clauses: list[str] = ["project = ?"]
    params: list[object] = [project]

    if status is None:
        clauses.append(
            "status IN ("
            + ", ".join("?" * len(_DEFAULT_REFLECTION_STATUSES))
            + ")"
        )
        params.extend(_DEFAULT_REFLECTION_STATUSES)
    else:
        clauses.append("status = ?")
        params.append(status)

    if tech is not None:
        clauses.append("tech = ?")
        params.append(tech)
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    if polarity is not None:
        clauses.append("polarity = ?")
        params.append(polarity)
    if min_confidence > 0.0:
        clauses.append("confidence >= ?")
        params.append(min_confidence)

    where = " AND ".join(clauses)
    sql = (
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, evidence_count, updated_at "
        f"FROM reflections WHERE {where} "
        "ORDER BY confidence DESC, updated_at DESC, rowid DESC "
        "LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        ReflectionListRow(
            id=r["id"],
            title=r["title"],
            project=r["project"],
            tech=r["tech"],
            phase=r["phase"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
            use_cases=r["use_cases"],
            evidence_count=r["evidence_count"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_queries_reflections.py -v
```

Expected: 11 PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `406 passed, 141 skipped, 4 deselected` (11 new tests on the 395 baseline).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_reflections.py
git commit -m "Phase 9: reflection_list_for_ui with project/tech/phase/polarity/status/min_confidence filters"
```

---

## Task 3: Add `reflection_detail` query helper

The drawer needs the full reflection plus the list of source observations (joined through `reflection_sources`) annotated with each observation's owning episode's `goal` / `outcome` / `close_reason`.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Modify: `tests/ui/test_queries_reflections.py` (append new class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_queries_reflections.py` (and add the new imports to the top-of-file import block — alphabetised):

```python
# Add to top-of-file imports:
# from better_memory.ui.queries import (
#     ReflectionDetail,
#     ReflectionListRow,
#     ReflectionSourceObservation,
#     reflection_detail,
#     reflection_list_for_ui,
# )

from better_memory.services.episode import EpisodeService


class TestReflectionDetail:
    def test_returns_none_for_missing_reflection(self, conn):
        assert reflection_detail(conn, reflection_id="nope") is None

    def test_returns_reflection_with_no_sources(self, conn):
        _seed(conn, rid="r-1")
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.id == "r-1"
        assert detail.sources == []

    def test_returns_full_reflection_fields(self, conn):
        _seed(
            conn, rid="r-1", project="proj-a", tech="python",
            phase="implementation", polarity="dont", confidence=0.85,
            use_cases="when X", hints="do Y, then Z",
            title="my title", evidence_count=3,
        )
        detail = reflection_detail(conn, reflection_id="r-1")
        r = detail.reflection
        assert r.title == "my title"
        assert r.tech == "python"
        assert r.phase == "implementation"
        assert r.polarity == "dont"
        assert r.confidence == 0.85
        assert r.use_cases == "when X"
        assert r.hints == "do Y, then Z"
        assert r.evidence_count == 3

    def test_returns_sources_with_episode_outcome(self, conn):
        # Need an episode for observations to bind to.
        ep_id = EpisodeService(conn).open_background(
            session_id="s1", project="proj-a"
        )
        # Harden to give it a goal + close it as success.
        EpisodeService(conn).start_foreground(
            session_id="s1", project="proj-a", goal="ship feature", tech="python"
        )
        EpisodeService(conn).close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )

        # Insert two observations on this episode.
        for i in range(2):
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, episode_id, component, theme, outcome) "
                "VALUES (?, ?, 'proj-a', ?, 'comp', 'bug', 'failure')",
                (f"obs-{i}", f"content {i}", ep_id),
            )
        _seed(conn, rid="r-1")
        # Both observations source this reflection.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'obs-0'), ('r-1', 'obs-1')"
        )
        conn.commit()

        detail = reflection_detail(conn, reflection_id="r-1")
        assert len(detail.sources) == 2
        for src in detail.sources:
            assert src.episode_goal == "ship feature"
            assert src.episode_outcome == "success"
            assert src.episode_close_reason == "goal_complete"
            assert src.component == "comp"
            assert src.theme == "bug"

    def test_sources_ordered_by_observation_created_at_desc(self, conn):
        ep_id = EpisodeService(conn).open_background(
            session_id="s1", project="proj-a"
        )
        # Two observations with explicit created_at to control ordering.
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-old', 'older', 'proj-a', ?, "
            "'2026-04-24T08:00:00+00:00')",
            (ep_id,),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-new', 'newer', 'proj-a', ?, "
            "'2026-04-24T10:00:00+00:00')",
            (ep_id,),
        )
        _seed(conn, rid="r-1")
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'obs-old'), ('r-1', 'obs-new')"
        )
        conn.commit()

        detail = reflection_detail(conn, reflection_id="r-1")
        assert [s.observation_id for s in detail.sources] == ["obs-new", "obs-old"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_queries_reflections.py::TestReflectionDetail -v
```

Expected: 5 FAILs, all `ImportError`.

- [ ] **Step 3: Implement `reflection_detail`**

Append to `better_memory/ui/queries.py`:

```python
@dataclass(frozen=True)
class ReflectionFull:
    """Full reflection row for the drawer."""

    id: str
    title: str
    project: str
    tech: str | None
    phase: str
    polarity: str
    confidence: float
    status: str
    use_cases: str
    hints: str
    evidence_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReflectionSourceObservation:
    """One source observation with its owning episode's outcome.

    Joined: reflection_sources → observations → episodes.
    """

    observation_id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str  # observation outcome (success/failure/neutral)
    created_at: str
    episode_id: str
    episode_goal: str | None
    episode_outcome: str | None  # episode outcome — None on still-open ones
    episode_close_reason: str | None


@dataclass(frozen=True)
class ReflectionDetail:
    reflection: ReflectionFull
    sources: list[ReflectionSourceObservation]


def reflection_detail(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionDetail | None:
    """Return one reflection with its source observations.

    Sources are joined through ``reflection_sources`` to ``observations``
    and from there to ``episodes`` so the drawer can show the owning
    episode's goal + outcome + close_reason for each piece of evidence.

    Returns ``None`` if no reflection with this id exists.

    Source ordering: ``observations.created_at DESC, observations.rowid DESC``.
    Same-status policy as Phase 8's episode_detail: ALL source
    observations are returned regardless of ``observations.status``.
    """
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, "
        "created_at, updated_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
    if r_row is None:
        return None

    src_rows = conn.execute(
        """
        SELECT
            o.id              AS observation_id,
            o.content         AS content,
            o.component       AS component,
            o.theme           AS theme,
            o.outcome         AS obs_outcome,
            o.created_at      AS obs_created_at,
            e.id              AS episode_id,
            e.goal            AS episode_goal,
            e.outcome         AS episode_outcome,
            e.close_reason    AS episode_close_reason
        FROM reflection_sources rs
        JOIN observations o ON o.id = rs.observation_id
        JOIN episodes     e ON e.id = o.episode_id
        WHERE rs.reflection_id = ?
        ORDER BY o.created_at DESC, o.rowid DESC
        """,
        (reflection_id,),
    ).fetchall()
    sources = [
        ReflectionSourceObservation(
            observation_id=r["observation_id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["obs_outcome"],
            created_at=r["obs_created_at"],
            episode_id=r["episode_id"],
            episode_goal=r["episode_goal"],
            episode_outcome=r["episode_outcome"],
            episode_close_reason=r["episode_close_reason"],
        )
        for r in src_rows
    ]
    return ReflectionDetail(
        reflection=ReflectionFull(
            id=r_row["id"],
            title=r_row["title"],
            project=r_row["project"],
            tech=r_row["tech"],
            phase=r_row["phase"],
            polarity=r_row["polarity"],
            confidence=r_row["confidence"],
            status=r_row["status"],
            use_cases=r_row["use_cases"],
            hints=r_row["hints"],
            evidence_count=r_row["evidence_count"],
            created_at=r_row["created_at"],
            updated_at=r_row["updated_at"],
        ),
        sources=sources,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_queries_reflections.py -v
```

Expected: 16 PASS (11 + 5).

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `411 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_reflections.py
git commit -m "Phase 9: reflection_detail (full reflection + source obs + episode outcome)"
```

---

## Task 4: Wire `ReflectionService` into the Flask app + replace `/reflections` placeholder

Register the service. Replace the Phase 8 placeholder route with a full-page render.

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `better_memory/ui/templates/reflections.html` (replace placeholder)
- Create: `better_memory/ui/templates/fragments/reflection_filter_form.html`
- Modify: `tests/ui/test_episodes.py` (the existing reflections placeholder test must update OR move) — see Step 1

- [ ] **Step 1: Move (or rename) the existing placeholder test**

The Phase 8 test `tests/ui/test_episodes.py::TestReflectionsPlaceholder::test_reflections_route_returns_200` asserts `b"Coming in Phase 9" in response.data`. Phase 9 replaces that placeholder, so this assertion will start failing. Either:

- (a) DELETE the test class — Phase 9 supersedes the placeholder. Cleaner.
- (b) UPDATE the test to assert the new full page (e.g. `b"Reflections" in response.data` and a filter-form element). Move it to a new `tests/ui/test_reflections.py` file (Task 5+ adds more tests there).

This plan picks (b) so the new file starts with one passing test from day one. Create `tests/ui/test_reflections.py`:

```python
"""Flask test-client tests for the Reflections tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient


class TestReflectionsPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/reflections")
        assert response.status_code == 200

    def test_renders_filter_form(self, client: FlaskClient):
        response = client.get("/reflections")
        body = response.get_data(as_text=True)
        # Filter form fields from spec §8: project / tech / phase /
        # polarity / status / min confidence.
        assert 'name="project"' in body
        assert 'name="tech"' in body
        assert 'name="phase"' in body
        assert 'name="polarity"' in body
        assert 'name="status"' in body
        assert 'name="min_confidence"' in body
```

Then DELETE `TestReflectionsPlaceholder` from `tests/ui/test_episodes.py` (Phase 8's test).

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_reflections.py -v
```

Expected: 2 FAILs (placeholder still says "Coming in Phase 9", no filter form).

- [ ] **Step 3: Wire `ReflectionService` into `app.py`**

In `better_memory/ui/app.py`:

Add the import alongside the other service imports (alphabetical):

```python
from better_memory.services.reflection import ReflectionService
```

Inside `create_app`, after `app.extensions["episode_service"] = EpisodeService(conn=db_conn)`:

```python
    app.extensions["reflection_service"] = ReflectionService(conn=db_conn)
```

- [ ] **Step 4: Replace the `reflections` placeholder route**

Find the existing `@app.get("/reflections")` route (added in Phase 8 Task 9). Replace its handler with:

```python
    @app.get("/reflections")
    def reflections() -> str:
        return render_template(
            "reflections.html",
            active_tab="reflections",
            # The filter-form initial state mirrors the no-filter
            # default — current project, status=active, no others.
            initial_filters={
                "project": _project_name(),
                "tech": "",
                "phase": "",
                "polarity": "",
                "status": "",
                "min_confidence": "",
            },
        )
```

- [ ] **Step 5: Replace `reflections.html`**

Overwrite `better_memory/ui/templates/reflections.html`:

```html
{% extends "base.html" %}
{% block title %}Reflections — better-memory{% endblock %}
{% block main %}
<section class="reflections">
  {% include "fragments/reflection_filter_form.html" %}

  <div id="reflection-panel"
       hx-get="{{ url_for('reflections_panel') }}"
       hx-include="#reflection-filter-form"
       hx-trigger="load, every 30s, reflection-changed from:body"
       hx-swap="innerHTML">
  </div>

  <div id="reflection-drawer"></div>
</section>
{% endblock %}
```

- [ ] **Step 6: Create the filter-form fragment**

Create `better_memory/ui/templates/fragments/reflection_filter_form.html`:

```html
<form id="reflection-filter-form"
      class="reflection-filters"
      hx-get="{{ url_for('reflections_panel') }}"
      hx-target="#reflection-panel"
      hx-trigger="change delay:200ms, submit"
      hx-swap="innerHTML">
  <label>
    Project
    <input name="project" value="{{ initial_filters.project }}">
  </label>
  <label>
    Tech
    <input name="tech" value="{{ initial_filters.tech }}" placeholder="any">
  </label>
  <label>
    Phase
    <select name="phase">
      <option value="" {% if not initial_filters.phase %}selected{% endif %}>any</option>
      <option value="planning"        {% if initial_filters.phase == 'planning' %}selected{% endif %}>planning</option>
      <option value="implementation"  {% if initial_filters.phase == 'implementation' %}selected{% endif %}>implementation</option>
      <option value="general"         {% if initial_filters.phase == 'general' %}selected{% endif %}>general</option>
    </select>
  </label>
  <label>
    Polarity
    <select name="polarity">
      <option value="" {% if not initial_filters.polarity %}selected{% endif %}>any</option>
      <option value="do"      {% if initial_filters.polarity == 'do' %}selected{% endif %}>do</option>
      <option value="dont"    {% if initial_filters.polarity == 'dont' %}selected{% endif %}>dont</option>
      <option value="neutral" {% if initial_filters.polarity == 'neutral' %}selected{% endif %}>neutral</option>
    </select>
  </label>
  <label>
    Status
    <select name="status">
      <option value="" {% if not initial_filters.status %}selected{% endif %}>active (default)</option>
      <option value="pending_review" {% if initial_filters.status == 'pending_review' %}selected{% endif %}>pending_review</option>
      <option value="confirmed"      {% if initial_filters.status == 'confirmed' %}selected{% endif %}>confirmed</option>
      <option value="retired"        {% if initial_filters.status == 'retired' %}selected{% endif %}>retired</option>
      <option value="superseded"     {% if initial_filters.status == 'superseded' %}selected{% endif %}>superseded</option>
    </select>
  </label>
  <label>
    Min confidence
    <input name="min_confidence" type="number" step="0.05" min="0" max="1"
           value="{{ initial_filters.min_confidence }}" placeholder="0.0">
  </label>
</form>
```

- [ ] **Step 7: Add a `reflections_panel` placeholder route**

`reflections.html` references `url_for('reflections_panel')`. Add a stub that Task 5 replaces:

```python
    @app.get("/reflections/panel")
    def reflections_panel() -> str:
        return ""
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_reflections.py -v
```

Expected: 2 PASS.

- [ ] **Step 9: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `412 passed, 141 skipped, 4 deselected` (deleted placeholder test → -1; added 2 new → +2; net +1 from 411).

- [ ] **Step 10: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/reflections.html \
        better_memory/ui/templates/fragments/reflection_filter_form.html \
        tests/ui/test_episodes.py tests/ui/test_reflections.py
git commit -m "Phase 9: register ReflectionService + Reflections page shell with filter form"
```

---

## Task 5: Build the Reflections panel (filtered list)

Replace the panel stub from Task 4 with the real handler. The panel reads the filter values from `request.args`, calls `reflection_list_for_ui`, and renders `panel_reflections.html`.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/panel_reflections.html`
- Create: `better_memory/ui/templates/fragments/reflection_row.html`
- Modify: `tests/ui/test_reflections.py` (add `TestReflectionsPanel`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_reflections.py`:

```python
from better_memory.db.connection import connect


def _seed_reflection(
    db_path: Path,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    evidence_count: int = 0,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, title or f"title-{rid}", project, tech, phase, polarity,
                use_cases, hints, confidence, status, evidence_count,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestReflectionsPanel:
    def test_empty_state_when_no_reflections(self, client: FlaskClient):
        response = client.get("/reflections/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No reflections" in body

    def test_renders_seeded_reflections(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Lesson A")
        _seed_reflection(tmp_db, rid="r-2", title="Lesson B")

        response = client.get("/reflections/panel?project=proj-a")
        body = response.get_data(as_text=True)
        assert "Lesson A" in body
        assert "Lesson B" in body

    def test_applies_phase_filter(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-plan", phase="planning", title="Plan")
        _seed_reflection(tmp_db, rid="r-impl", phase="implementation", title="Impl")

        response = client.get("/reflections/panel?project=proj-a&phase=planning")
        body = response.get_data(as_text=True)
        assert "Plan" in body
        assert "Impl" not in body

    def test_min_confidence_filter_parses_decimal(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-low", confidence=0.3, title="Low")
        _seed_reflection(tmp_db, rid="r-high", confidence=0.9, title="High")

        response = client.get(
            "/reflections/panel?project=proj-a&min_confidence=0.6"
        )
        body = response.get_data(as_text=True)
        assert "High" in body
        assert "Low" not in body

    def test_blank_filter_values_are_treated_as_unset(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Visible")

        response = client.get(
            "/reflections/panel?project=proj-a"
            "&tech=&phase=&polarity=&status=&min_confidence="
        )
        body = response.get_data(as_text=True)
        assert "Visible" in body
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionsPanel -v
```

Expected: 5 FAILs.

- [ ] **Step 3: Create panel templates**

Create `better_memory/ui/templates/fragments/panel_reflections.html`:

```html
{% if not rows %}
  <div class="empty-state">
    <p>No reflections match these filters.</p>
  </div>
{% else %}
  <div class="reflection-list">
    {% for row in rows %}
      {% include "fragments/reflection_row.html" %}
    {% endfor %}
  </div>
{% endif %}
```

Create `better_memory/ui/templates/fragments/reflection_row.html`:

```html
<div class="reflection-row polarity-{{ row.polarity }}"
     hx-get="{{ url_for('reflections_drawer', id=row.id) }}"
     hx-target="#reflection-drawer"
     hx-swap="innerHTML">
  <div class="row-main">
    <span class="title">{{ row.title }}</span>
    <span class="use-cases">{{ row.use_cases | truncate(120, True) }}</span>
  </div>
  <div class="row-side">
    <span class="phase-badge phase-{{ row.phase }}">{{ row.phase }}</span>
    <span class="polarity-badge polarity-{{ row.polarity }}">{{ row.polarity }}</span>
    {% if row.tech %}<span class="tech">{{ row.tech }}</span>{% endif %}
    <div class="confidence-bar" title="confidence {{ '%.2f' | format(row.confidence) }}">
      <div class="confidence-fill" style="width: {{ (row.confidence * 100) | round(0) }}%"></div>
    </div>
    <span class="evidence-count">{{ row.evidence_count }} obs</span>
    <span class="status-{{ row.status }}">{{ row.status }}</span>
  </div>
</div>
```

- [ ] **Step 4: Replace the `reflections_panel` stub with the real handler**

In `better_memory/ui/app.py`, replace the Task 4 stub with:

```python
    @app.get("/reflections/panel")
    def reflections_panel() -> str:
        conn = app.extensions["db_connection"]
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        project = _arg("project") or _project_name()
        tech = _arg("tech")
        phase = _arg("phase")
        polarity = _arg("polarity")
        status = _arg("status")

        min_conf_raw = _arg("min_confidence")
        try:
            min_confidence = float(min_conf_raw) if min_conf_raw else 0.0
        except ValueError:
            min_confidence = 0.0

        rows = queries.reflection_list_for_ui(
            conn,
            project=project,
            tech=tech,
            phase=phase,
            polarity=polarity,
            status=status,
            min_confidence=min_confidence,
        )
        return render_template(
            "fragments/panel_reflections.html", rows=rows
        )

    # Drawer stub — Task 6 replaces.
    @app.get("/reflections/<id>/drawer")
    def reflections_drawer(id: str) -> str:
        return ""
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionsPanel -v
```

Expected: 5 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `417 passed, 141 skipped, 4 deselected`.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/panel_reflections.html \
        better_memory/ui/templates/fragments/reflection_row.html \
        tests/ui/test_reflections.py
git commit -m "Phase 9: Reflections panel with all six filters"
```

---

## Task 6: Build the reflection drawer

Replace the Task 5 stub with the real drawer. Shows full reflection + sources + action buttons.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/reflection_drawer.html`
- Modify: `tests/ui/test_reflections.py` (add `TestReflectionDrawer`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_reflections.py`:

```python
class TestReflectionDrawer:
    def test_404_for_unknown_reflection(self, client: FlaskClient):
        response = client.get("/reflections/does-not-exist/drawer")
        assert response.status_code == 404

    def test_renders_full_reflection(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", title="My lesson",
            use_cases="when X happens", hints="do Y, then Z",
            phase="implementation", polarity="dont",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "My lesson" in body
        assert "when X happens" in body
        assert "do Y, then Z" in body
        # Action buttons (status pending_review by default → confirm visible).
        assert "Confirm" in body
        assert "Retire" in body
        assert "Edit" in body

    def test_omits_confirm_for_already_confirmed(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="confirmed")
        response = client.get("/reflections/r-1/drawer")
        body = response.get_data(as_text=True)
        assert "Confirm" not in body
        assert "Retire" in body
        assert "Edit" in body

    def test_omits_actions_for_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired")
        response = client.get("/reflections/r-1/drawer")
        body = response.get_data(as_text=True)
        assert "Confirm" not in body
        assert "Retire" not in body
        assert "Edit" not in body
        # But the reflection content still renders (audit / read-only view).
        assert "title-r-1" in body
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionDrawer -v
```

Expected: 4 FAILs.

- [ ] **Step 3: Create the drawer template**

Create `better_memory/ui/templates/fragments/reflection_drawer.html`:

```html
<div class="reflection-drawer polarity-{{ detail.reflection.polarity }}"
     id="reflection-drawer-{{ detail.reflection.id }}">
  <header class="drawer-header">
    <h3>{{ detail.reflection.title }}</h3>
    <button class="close-drawer"
            type="button"
            onclick="document.getElementById('reflection-drawer').innerHTML = '';">
      ×
    </button>
  </header>

  <dl class="drawer-meta">
    <dt>Project</dt><dd>{{ detail.reflection.project }}</dd>
    {% if detail.reflection.tech %}
      <dt>Tech</dt><dd>{{ detail.reflection.tech }}</dd>
    {% endif %}
    <dt>Phase</dt>
    <dd><span class="phase-badge phase-{{ detail.reflection.phase }}">{{ detail.reflection.phase }}</span></dd>
    <dt>Polarity</dt>
    <dd><span class="polarity-badge polarity-{{ detail.reflection.polarity }}">{{ detail.reflection.polarity }}</span></dd>
    <dt>Confidence</dt>
    <dd>{{ '%.2f' | format(detail.reflection.confidence) }}</dd>
    <dt>Status</dt><dd>{{ detail.reflection.status }}</dd>
    <dt>Evidence</dt><dd>{{ detail.reflection.evidence_count }} observation{{ 's' if detail.reflection.evidence_count != 1 else '' }}</dd>
    <dt>Updated</dt><dd>{{ detail.reflection.updated_at }}</dd>
  </dl>

  <section class="drawer-section">
    <h4>Use cases</h4>
    <p>{{ detail.reflection.use_cases }}</p>
  </section>

  <section class="drawer-section">
    <h4>Hints</h4>
    <p>{{ detail.reflection.hints }}</p>
  </section>

  {% if detail.reflection.status in ('pending_review', 'confirmed') %}
    <div class="drawer-actions">
      {% if detail.reflection.status == 'pending_review' %}
        <button type="button"
                class="action-confirm"
                hx-post="{{ url_for('reflection_confirm', id=detail.reflection.id) }}"
                hx-target="#reflection-drawer"
                hx-swap="innerHTML">
          Confirm
        </button>
      {% endif %}
      <button type="button"
              class="action-retire"
              hx-post="{{ url_for('reflection_retire', id=detail.reflection.id) }}"
              hx-target="#reflection-drawer"
              hx-swap="innerHTML">
        Retire
      </button>
      <button type="button"
              class="action-edit"
              hx-get="{{ url_for('reflection_edit_form', id=detail.reflection.id) }}"
              hx-target="#reflection-drawer"
              hx-swap="innerHTML">
        Edit
      </button>
    </div>
  {% endif %}

  <section class="drawer-section">
    <h4>Source observations ({{ detail.sources | length }})</h4>
    {% if detail.sources %}
      <ul class="source-list">
        {% for src in detail.sources %}
          <li class="source-item outcome-{{ src.outcome }}">
            <div class="src-content">{{ src.content }}</div>
            <div class="src-meta">
              {% if src.component %}<span>{{ src.component }}</span>{% endif %}
              {% if src.theme %}<span>{{ src.theme }}</span>{% endif %}
              <span>obs {{ src.outcome }}</span>
              <span>{{ src.created_at }}</span>
            </div>
            <div class="src-episode">
              {% if src.episode_goal %}
                from episode <em>{{ src.episode_goal }}</em>
              {% else %}
                from background session
              {% endif %}
              {% if src.episode_outcome %}
                — closed <span class="outcome-badge outcome-{{ src.episode_outcome }}">{{ src.episode_outcome }}</span>
                {% if src.episode_close_reason %}
                  ({{ src.episode_close_reason }})
                {% endif %}
              {% else %}
                — still open
              {% endif %}
            </div>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="empty-inline">No source observations linked yet.</p>
    {% endif %}
  </section>
</div>
```

- [ ] **Step 4: Replace the drawer stub + add action stubs**

In `better_memory/ui/app.py`, replace the Task 5 stub `reflections_drawer` and add three stubs (Tasks 7, 8 implement):

```python
    @app.get("/reflections/<id>/drawer")
    def reflections_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.reflection_detail(conn, reflection_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/reflection_drawer.html", detail=detail
        )

    # Stubs — Tasks 7+8 replace.
    @app.post("/reflections/<id>/confirm")
    def reflection_confirm(id: str) -> tuple[str, int]:
        return "", 200

    @app.post("/reflections/<id>/retire")
    def reflection_retire(id: str) -> tuple[str, int]:
        return "", 200

    @app.get("/reflections/<id>/edit")
    def reflection_edit_form(id: str) -> str:
        return ""
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionDrawer -v
```

Expected: 4 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `421 passed, 141 skipped, 4 deselected`.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/reflection_drawer.html \
        tests/ui/test_reflections.py
git commit -m "Phase 9: reflection drawer with sources + action buttons"
```

---

## Task 7: Wire confirm + retire actions

Replace the Task 6 stubs with real handlers. On success: re-render the drawer (status flipped) and fire `HX-Trigger: reflection-changed` so the panel reloads.

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_reflections.py` (add `TestReflectionConfirm` and `TestReflectionRetire`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_reflections.py`:

```python
class TestReflectionConfirm:
    def test_confirms_pending(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="pending_review")

        response = client.post(
            "/reflections/r-1/confirm",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "reflection-changed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT status FROM reflections WHERE id = ?", ("r-1",)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "confirmed"

    def test_404_for_unknown(self, client: FlaskClient):
        response = client.post(
            "/reflections/does-not-exist/confirm",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 404

    def test_409_for_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired")

        response = client.post(
            "/reflections/r-1/confirm",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409


class TestReflectionRetire:
    def test_retires_pending(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="pending_review")

        response = client.post(
            "/reflections/r-1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "reflection-changed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT status FROM reflections WHERE id = ?", ("r-1",)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "retired"

    def test_retires_confirmed(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="confirmed")

        response = client.post(
            "/reflections/r-1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200

    def test_404_for_unknown(self, client: FlaskClient):
        response = client.post(
            "/reflections/does-not-exist/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 404

    def test_409_for_superseded(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="superseded")

        response = client.post(
            "/reflections/r-1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionConfirm tests/ui/test_reflections.py::TestReflectionRetire -v
```

Expected: 7 FAILs (stubs return 200 with no DB write).

- [ ] **Step 3: Replace stubs with real handlers**

In `better_memory/ui/app.py`, replace the Task 6 `reflection_confirm` and `reflection_retire` stubs with:

```python
    @app.post("/reflections/<id>/confirm")
    def reflection_confirm(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        try:
            app.extensions["reflection_service"].confirm(reflection_id=id)
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.post("/reflections/<id>/retire")
    def reflection_retire(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        try:
            app.extensions["reflection_service"].retire(reflection_id=id)
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionConfirm tests/ui/test_reflections.py::TestReflectionRetire -v
```

Expected: 7 PASS.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `428 passed, 141 skipped, 4 deselected`.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_reflections.py
git commit -m "Phase 9: POST /reflections/<id>/confirm + /retire with HX-Trigger"
```

---

## Task 8: Wire the inline edit form

GET returns the edit form (use_cases + hints inputs). POST validates + calls `update_text` + re-renders the drawer.

**Files:**
- Modify: `better_memory/ui/app.py`
- Create: `better_memory/ui/templates/fragments/reflection_edit_form.html`
- Modify: `tests/ui/test_reflections.py` (add `TestReflectionEdit`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_reflections.py`:

```python
class TestReflectionEdit:
    def test_get_returns_form(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", use_cases="old uc", hints="old h"
        )
        response = client.get("/reflections/r-1/edit")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'name="use_cases"' in body
        assert 'name="hints"' in body
        assert "old uc" in body
        assert "old h" in body

    def test_get_404_for_unknown(self, client: FlaskClient):
        response = client.get("/reflections/does-not-exist/edit")
        assert response.status_code == 404

    def test_post_saves_and_returns_drawer(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1")

        response = client.post(
            "/reflections/r-1/edit",
            data={"use_cases": "new uc", "hints": "new h"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "reflection-changed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT use_cases, hints FROM reflections WHERE id = ?",
                ("r-1",),
            ).fetchone()
        finally:
            conn.close()
        assert row["use_cases"] == "new uc"
        assert row["hints"] == "new h"

    def test_post_400_when_use_cases_empty(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1")

        response = client.post(
            "/reflections/r-1/edit",
            data={"use_cases": "  ", "hints": "valid"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400

    def test_post_409_for_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired")

        response = client.post(
            "/reflections/r-1/edit",
            data={"use_cases": "x", "hints": "y"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionEdit -v
```

Expected: 5 FAILs.

- [ ] **Step 3: Create the edit-form fragment**

Create `better_memory/ui/templates/fragments/reflection_edit_form.html`:

```html
<div class="reflection-edit-form" id="reflection-edit-{{ detail.reflection.id }}">
  <header class="drawer-header">
    <h3>Edit: {{ detail.reflection.title }}</h3>
  </header>
  <form hx-post="{{ url_for('reflection_edit_save', id=detail.reflection.id) }}"
        hx-target="#reflection-drawer"
        hx-swap="innerHTML">
    <label>
      Use cases
      <textarea name="use_cases" rows="3">{{ detail.reflection.use_cases }}</textarea>
    </label>
    <label>
      Hints
      <textarea name="hints" rows="6">{{ detail.reflection.hints }}</textarea>
    </label>
    <div class="form-actions">
      <button type="submit" class="action-save">Save</button>
      <button type="button"
              class="action-cancel"
              hx-get="{{ url_for('reflections_drawer', id=detail.reflection.id) }}"
              hx-target="#reflection-drawer"
              hx-swap="innerHTML">
        Cancel
      </button>
    </div>
  </form>
</div>
```

- [ ] **Step 4: Replace the edit-form stub + add the save handler**

In `better_memory/ui/app.py`, replace the Task 6 `reflection_edit_form` stub with:

```python
    @app.get("/reflections/<id>/edit")
    def reflection_edit_form(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.reflection_detail(conn, reflection_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/reflection_edit_form.html", detail=detail
        )

    @app.post("/reflections/<id>/edit")
    def reflection_edit_save(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        use_cases = request.form.get("use_cases", "")
        hints = request.form.get("hints", "")
        # Validate empties at the route boundary (input-validation = 400)
        # so the service-layer ValueError can mean only "lifecycle block"
        # (= 409). Avoids fragile error-message string matching.
        if not use_cases.strip() or not hints.strip():
            return (
                '<div class="card card-error">'
                "<p>use_cases and hints must both be non-empty</p>"
                "</div>"
            ), 400, {}
        try:
            app.extensions["reflection_service"].update_text(
                reflection_id=id, use_cases=use_cases, hints=hints,
            )
        except ValueError as exc:
            # After the empty-check above, the only remaining ValueError
            # path is "Cannot edit reflection in status 'retired'/'superseded'".
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_reflections.py::TestReflectionEdit -v
```

Expected: 5 PASS.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `433 passed, 141 skipped, 4 deselected`.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py \
        better_memory/ui/templates/fragments/reflection_edit_form.html \
        tests/ui/test_reflections.py
git commit -m "Phase 9: inline edit form for reflection use_cases + hints"
```

---

## Task 9: Add CSS for the Reflections tab

Match the dark-theme tokens established in Phase 8 (`#1a1a1a` cards, `#2a2a2a` borders, `#e0e0e0` text, etc.). Add new tokens for polarity badges, phase badges, and the confidence bar.

**Files:**
- Modify: `better_memory/ui/static/app.css`

- [ ] **Step 1: Append the new styles**

Append to `better_memory/ui/static/app.css`:

```css
/* ---------- Reflections tab (dark theme) ---------- */

.reflection-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  align-items: end;
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
}

.reflection-filters label {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.75rem;
  color: #888;
  text-transform: uppercase;
}

.reflection-filters input,
.reflection-filters select {
  background: #1a1a1a;
  border: 1px solid #3a3a3a;
  color: #e0e0e0;
  border-radius: 4px;
  padding: 0.3rem 0.5rem;
  font-size: 0.85rem;
  min-width: 120px;
}

.reflection-list {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.reflection-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 4px;
  padding: 0.6rem 0.85rem;
  cursor: pointer;
  transition: background 0.1s;
  color: #e0e0e0;
  gap: 1rem;
}

.reflection-row:hover {
  background: #222;
  border-color: #3a3a3a;
}

.reflection-row .row-main {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  min-width: 0; /* allow truncation */
}
.reflection-row .title { font-weight: 500; color: #f0f0f0; }
.reflection-row .use-cases { font-size: 0.8rem; color: #888; }

.reflection-row .row-side {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-shrink: 0;
  font-size: 0.75rem;
}

.phase-badge,
.polarity-badge {
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 8px;
  text-transform: uppercase;
  font-weight: 600;
}

.phase-badge.phase-planning       { background: #1a2a3a; color: #7aa9ff; }
.phase-badge.phase-implementation { background: #1a3a2a; color: #7adb9c; }
.phase-badge.phase-general        { background: #2a2a2a; color: #aaa; }

.polarity-badge.polarity-do       { background: #1a3a1a; color: #7adb7a; }
.polarity-badge.polarity-dont     { background: #3a1a1a; color: #db7a7a; }
.polarity-badge.polarity-neutral  { background: #2a2a2a; color: #aaa; }

.confidence-bar {
  width: 60px;
  height: 6px;
  background: #2a2a2a;
  border-radius: 3px;
  overflow: hidden;
}

.confidence-fill {
  height: 100%;
  background: linear-gradient(to right, #6f5d10, #fcd97a);
}

.evidence-count { color: #888; }
.status-pending_review { color: #fcd97a; }
.status-confirmed      { color: #7adb7a; }
.status-retired        { color: #888; font-style: italic; }
.status-superseded     { color: #888; font-style: italic; }

/* ---------- Reflection drawer ---------- */

.reflection-drawer {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  padding: 1rem 1.2rem;
  margin-top: 1.5rem;
  color: #e0e0e0;
}

.reflection-drawer .drawer-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.reflection-drawer .drawer-header h3 { margin: 0; color: #f0f0f0; }

.reflection-drawer .drawer-meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.25rem 1rem;
  margin: 0.75rem 0;
  font-size: 0.9rem;
}
.reflection-drawer .drawer-meta dt { color: #888; font-weight: 500; }
.reflection-drawer .drawer-meta dd { margin: 0; color: #e0e0e0; }

.reflection-drawer .drawer-section {
  margin-top: 1rem;
  border-top: 1px solid #2a2a2a;
  padding-top: 0.75rem;
}
.reflection-drawer .drawer-section h4 { margin: 0 0 0.5rem 0; color: #f0f0f0; }

.reflection-drawer .drawer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0.75rem 0;
}

.reflection-drawer .drawer-actions button {
  padding: 0.35rem 0.7rem;
  border: 1px solid #3a3a3a;
  background: #1f1f1f;
  color: #e0e0e0;
  border-radius: 4px;
  cursor: pointer;
}
.reflection-drawer .drawer-actions button:hover {
  background: #2a2a2a; border-color: #4a4a4a;
}
.reflection-drawer .drawer-actions .action-confirm:hover { border-color: #4f7f4f; }
.reflection-drawer .drawer-actions .action-retire:hover  { border-color: #7f4f4f; }

.source-list { list-style: none; padding: 0; margin: 0; }

.source-item {
  padding: 0.5rem 0;
  border-bottom: 1px dashed #2a2a2a;
  font-size: 0.9rem;
}
.source-item:last-child { border-bottom: none; }
.src-content { color: #e0e0e0; margin-bottom: 0.25rem; }
.src-meta {
  display: flex;
  gap: 0.5rem;
  color: #888;
  font-size: 0.75rem;
}
.src-episode {
  font-size: 0.8rem;
  color: #888;
  margin-top: 0.25rem;
}
.src-episode em { color: #e0e0e0; font-style: italic; }

/* ---------- Reflection edit form ---------- */

.reflection-edit-form {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 6px;
  padding: 1rem 1.2rem;
  margin-top: 1.5rem;
  color: #e0e0e0;
}

.reflection-edit-form label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
  font-size: 0.85rem;
  color: #888;
  text-transform: uppercase;
}

.reflection-edit-form textarea {
  background: #1a1a1a;
  border: 1px solid #3a3a3a;
  color: #e0e0e0;
  border-radius: 4px;
  padding: 0.5rem;
  font-family: inherit;
  font-size: 0.9rem;
  resize: vertical;
}

.reflection-edit-form .form-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.reflection-edit-form button {
  padding: 0.35rem 0.9rem;
  border-radius: 4px;
  border: 1px solid #3a3a3a;
  background: #1f1f1f;
  color: #e0e0e0;
  cursor: pointer;
}
.reflection-edit-form button:hover { background: #2a2a2a; }
.reflection-edit-form .action-save:hover { border-color: #4f7f4f; }
```

- [ ] **Step 2: Run the full suite (no behaviour change)**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `433 passed, 141 skipped, 4 deselected` (CSS isn't tested).

- [ ] **Step 3: Commit**

```bash
git add better_memory/ui/static/app.css
git commit -m "Phase 9: CSS for Reflections tab — filters, list, drawer, edit, badges"
```

---

## Task 10: Update CLAUDE.md skill snippet to mention the Reflections tab

Phase 8 added a sentence about the Episodes tab in the management UI. Phase 9 adds a sibling sentence about the Reflections tab.

**Files:**
- Modify: `better_memory/skills/CLAUDE.snippet.md`

- [ ] **Step 1: Find the management-UI section**

```bash
grep -n "Episodes tab in the management UI\|Reflections" better_memory/skills/CLAUDE.snippet.md
```

- [ ] **Step 2: Append a Reflections sentence**

After the existing Episodes-tab paragraph (added in Phase 8), append:

```
The Reflections tab in the same UI lists all reflections for the
current project with filters by tech / phase / polarity / status /
min confidence; clicking a row opens a drawer with the source
observations + their owning episode's outcome, plus actions to
confirm pending reflections, retire stale ones, or edit
``use_cases`` and ``hints`` in place.
```

(Use the Edit tool with sufficient surrounding context to make the addition unique.)

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `433 passed, 141 skipped, 4 deselected` (snippet content not tested).

- [ ] **Step 4: Commit**

```bash
git add better_memory/skills/CLAUDE.snippet.md
git commit -m "Phase 9: CLAUDE snippet — Reflections tab summary"
```

---

## Final review

After all tasks complete, dispatch a final code-review subagent across the full Phase 9 diff. Confirm:

- All tests pass: `uv run pytest --tb=no -q 2>&1 | tail -3` shows `433 passed, 141 skipped, 4 deselected`.
- Spec §8 Reflections-tab fields are complete:
  - Filter panel: project · tech · phase · polarity · status · min confidence ✓
  - Row fields: title · phase badge · polarity badge · tech · confidence bar · use_cases preview · evidence_count ✓
  - Drawer: full reflection (title, use_cases, hints, phase, polarity, tech, confidence, status, evidence_count) + source observations with episode outcome ✓
  - Actions: confirm, retire, edit ✓ (promote-to-knowledge deferred — see better-memory observation `342d81a7`).
- HX-Trigger wiring: confirm/retire/edit all fire `reflection-changed` so the panel reloads.
- No regressions in Phase 8 tests (Episodes tab unaffected).
- Promote-to-knowledge action button is NOT present (deferred); the drawer leaves room for it.

Then run `superpowers:finishing-a-development-branch` to push + open the PR.
