# Episodic Memory Phase 12 — End-to-End Browser Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Playwright-driven end-to-end browser tests for the Episodes and Reflections tabs, covering the spec §11 e2e scenarios. Real Flask subprocess + real SQLite + real browser; no mocks of HTMX or routing.

**Architecture:** Reuse the existing `ui_url` fixture in `tests/ui/conftest_browser.py` — it already spawns `python -m better_memory.ui` as a subprocess with an isolated `BETTER_MEMORY_HOME`, applies migrations, and yields `(url, home_dir)`. Tests open a fresh second connection to `home / "memory.db"` to seed data (services run committed SQLite writes that the subprocess sees on its next read). Tests use `pytest-playwright`'s `page` fixture (already a dev dep) to drive Chromium against the spawned URL. The whole suite runs in CI under the existing `ui-tests.yml` workflow which already does `playwright install --with-deps chromium`.

**Tech Stack:** Python 3.12 · Flask · HTMX · pytest · pytest-playwright · Chromium · uv.

**Scope boundary.** Two test files: `tests/ui/test_browser_episodes.py` and `tests/ui/test_browser_reflections.py`. Cover the spec §11 scenarios:
- Episodes: create an episode, close it, verify it appears in the timeline (with the right outcome badge).
- Reflections: seed a reflection, confirm it, verify status transition.

Plus a small set of complementary tests to give the e2e layer real coverage of the HTMX interactions: row click → drawer, action button → DB write → panel reload, filter form → panel re-render, inline edit → save → drawer rerender.

**Out of scope** (explicit):
- **Real Ollama** — spec §11 says "Real Ollama only under `pytest -m integration`"; this plan does NOT add `-m integration` tests. Synthesis-driven scenarios (e.g. start an episode and watch reflections appear) are out — they need a real LLM.
- **Multi-browser matrix** — Chromium only, matching the existing CI install.
- **Visual regression / screenshot comparison** — assertions are DOM-shape only.
- **Mobile viewport / accessibility audits** — desktop default viewport.
- **Promote-to-knowledge** (deferred per better-memory `342d81a7`).

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §11 (e2e section), §14 (build order phase 12).

**Reference plans:** Phase 8 (`2026-04-25-episodic-phase-8-episodes-ui.md`) and Phase 9 (`2026-04-26-episodic-phase-9-reflections-ui.md`) for the routes / templates / HTMX wiring being exercised.

**Pre-existing constraints:**
- `tests/ui/conftest_browser.py` exists with the `ui_url` fixture. Reuse, don't reinvent.
- `tests/ui/test_browser.py` was deleted in Phase 10 (the Phase-2 candidate-card test was orphaned). Phase 12 introduces fresh files.
- The subprocess infers `project = Path.cwd().name`. Tests run from the repo root, so seeding must use that same value (e.g. `Path.cwd().name`) to make data visible. Don't try to monkeypatch — monkeypatching is in-process only.
- `pytest-playwright` provides the `page` fixture (Chromium by default). No additional fixture wiring needed.
- `pyproject.toml` has `addopts = "-m 'not integration'"` — by default integration-marked tests are excluded. Don't accidentally mark e2e tests as `integration` (they should run on every PR).

---

## File Structure

**New files:**
- `tests/ui/test_browser_episodes.py` — 4 Playwright tests against the Episodes tab.
- `tests/ui/test_browser_reflections.py` — 5 Playwright tests against the Reflections tab.

**No modified files.** The `ui_url` fixture, `conftest_browser.py`, `pyproject.toml`, and `ui-tests.yml` workflow are all already in place.

---

## Task 0: Worktree

Already created at `C:/Users/gethi/source/better-memory-episodic-phase-12-e2e` on branch `episodic-phase-12-e2e` (based on `origin/episodic-phase-1-schema` post-Phase-11-merge). Baseline: **466 passed, 22 skipped, 3 deselected**. Skip this task.

---

## Task 1: Browser tests for the Episodes tab

Cover the spec §11 e2e scenarios for the Episodes tab plus the HTMX interactions Phase 8 added.

**Files:**
- Create: `tests/ui/test_browser_episodes.py`

- [ ] **Step 1: Verify the Playwright page fixture works**

Before writing test bodies, confirm the test infrastructure runs:

```bash
uv run python -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
```

Expected: `playwright OK`.

```bash
uv run playwright install --with-deps chromium
```

(May already be installed locally; the install is idempotent. CI does this in the workflow.)

- [ ] **Step 2: Write the failing tests**

Create `tests/ui/test_browser_episodes.py`:

```python
"""End-to-end Playwright tests for the Episodes tab.

Spec §11: "Episodes tab: create an episode, close it, verify it
appears in the timeline."

Uses the `ui_url` fixture from `conftest_browser.py` which spawns
a real Flask subprocess against an isolated SQLite home.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from better_memory.db.connection import connect
from better_memory.services.episode import EpisodeService

pytest_plugins = ["tests.ui.conftest_browser"]


def _project_name() -> str:
    """Match the subprocess's project resolution (Path.cwd().name)."""
    return Path.cwd().name


def _seed_episode_via_service(
    home: Path,
    *,
    goal: str,
    tech: str | None = None,
    closed: bool = False,
    outcome: str = "success",
) -> str:
    """Seed an episode (and optionally close it) via the service layer.

    Opens a fresh second connection to memory.db — committed writes are
    visible to the subprocess on its next read because SQLite respects
    cross-connection reads of committed data in default journal mode.
    """
    db_path = home / "memory.db"
    conn = connect(db_path)
    try:
        clock = lambda: datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        svc = EpisodeService(conn, clock=clock)
        ep_id = svc.open_background(
            session_id="e2e-test", project=_project_name()
        )
        svc.start_foreground(
            session_id="e2e-test",
            project=_project_name(),
            goal=goal,
            tech=tech,
        )
        if closed:
            svc.close_active(
                session_id="e2e-test",
                outcome=outcome,
                close_reason="goal_complete",
            )
        return ep_id
    finally:
        conn.close()


class TestEpisodesPageRenders:
    def test_root_redirects_to_episodes_and_renders_two_tabs(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, _ = ui_url

        page.goto(url)  # bare /
        page.wait_for_url(f"{url}/episodes")

        # Both tabs visible in the nav.
        expect(page.locator(".tab", has_text="Episodes")).to_be_visible()
        expect(page.locator(".tab", has_text="Reflections")).to_be_visible()

        # Empty-state shows when no episodes seeded yet (HTMX loads
        # the panel partial after page render).
        page.wait_for_selector(".empty-state, .timeline")
        # If the panel rendered before any seeding, it must be empty-state.
        if page.locator(".empty-state").count() > 0:
            assert "No episodes yet" in page.content()


class TestEpisodesTabSeededFlow:
    """Spec §11: create an episode, close it, verify it appears in the
    timeline."""

    def test_seeded_episode_appears_in_timeline(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url

        ep_id = _seed_episode_via_service(
            home, goal="ship Phase 12 e2e tests", tech="python",
            closed=True, outcome="success",
        )

        page.goto(f"{url}/episodes")
        # Wait for the timeline panel to load via HTMX.
        page.wait_for_selector(".timeline, .empty-state")

        body = page.content()
        assert "ship Phase 12 e2e tests" in body
        assert "python" in body
        # Outcome badge present for the closed episode.
        expect(
            page.locator(".outcome-badge.outcome-success").first
        ).to_be_visible()

    def test_clicking_row_opens_drawer_with_close_actions(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url

        # Seed an OPEN episode so close-action buttons are present.
        _seed_episode_via_service(
            home, goal="open episode for drawer test", tech="python",
            closed=False,
        )

        page.goto(f"{url}/episodes")
        page.wait_for_selector(".episode-row")

        # Click the row → drawer slot fills via HTMX.
        page.locator(".episode-row").first.click()
        page.wait_for_selector("#drawer .episode-drawer")

        # All 4 close actions + Continuing button visible.
        for label in (
            "Close as success",
            "Close as partial",
            "Close as abandoned",
            "Close as no_outcome",
            "Continuing",
        ):
            expect(
                page.locator(".drawer-actions button", has_text=label)
            ).to_be_visible()


class TestEpisodeCloseFlowEndToEnd:
    """Spec §11: close an episode end-to-end via the UI; assert DB +
    DOM both reflect the change."""

    def test_close_as_success_writes_db_and_reloads_timeline(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url
        ep_id = _seed_episode_via_service(
            home, goal="finish task", tech="python", closed=False,
        )

        page.goto(f"{url}/episodes")
        page.wait_for_selector(".episode-row")
        page.locator(".episode-row").first.click()
        page.wait_for_selector("#drawer .drawer-actions")

        # Click "Close as success".
        page.locator(
            ".drawer-actions button", has_text="Close as success"
        ).click()

        # Drawer re-renders with closed metadata; timeline reloads
        # (episode-closed HX-Trigger).
        page.wait_for_selector("#drawer .drawer-meta dd >> text=success")
        # Status in DB.
        db_path = home / "memory.db"
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT outcome, close_reason FROM episodes WHERE id = ?",
                (ep_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "success"
        assert row["close_reason"] == "goal_complete"

        # Timeline now shows the success badge for this row (panel
        # reload triggered by reflection-changed/episode-closed event).
        page.wait_for_selector(".outcome-badge.outcome-success")
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_browser_episodes.py -v
```

Expected: 4 PASS. If `playwright install` hasn't been run locally, expect `Browser was not installed` — run the install command from Step 1 first.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `470 passed, 22 skipped, 3 deselected` (4 new tests on the 466 baseline).

- [ ] **Step 5: Commit**

```bash
git add tests/ui/test_browser_episodes.py
git commit -m "Phase 12: e2e browser tests for the Episodes tab"
```

---

## Task 2: Browser tests for the Reflections tab

**Files:**
- Create: `tests/ui/test_browser_reflections.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_browser_reflections.py`:

```python
"""End-to-end Playwright tests for the Reflections tab.

Spec §11: "Reflections tab: seed a reflection, confirm it, verify
status transition."
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from better_memory.db.connection import connect

pytest_plugins = ["tests.ui.conftest_browser"]


def _project_name() -> str:
    return Path.cwd().name


def _seed_reflection(
    home: Path,
    *,
    refl_id: str,
    title: str,
    use_cases: str = "when X happens",
    hints: list[str] | None = None,
    phase: str = "general",
    polarity: str = "do",
    tech: str | None = None,
    confidence: float = 0.7,
    status: str = "pending_review",
) -> None:
    """Seed a reflection via raw SQL (no synthesis service involved).

    `hints` are JSON-encoded to match the synthesis-service contract.
    """
    if hints is None:
        hints = ["do Y", "then Z"]
    db_path = home / "memory.db"
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, "
            "hints, confidence, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                refl_id, title, _project_name(), tech, phase, polarity,
                use_cases, json.dumps(hints), confidence, status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestReflectionsPageRenders:
    def test_reflections_route_renders_filter_form(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, _ = ui_url
        page.goto(f"{url}/reflections")

        # Filter form fields from spec §8.
        for name in ("project", "tech", "phase", "polarity", "status",
                     "min_confidence"):
            expect(
                page.locator(f"[name='{name}']")
            ).to_be_visible()


class TestReflectionsListSeededFlow:
    def test_seeded_reflection_appears_in_panel(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url
        _seed_reflection(
            home, refl_id="r-1", title="Always commit small",
            use_cases="when refactoring",
            phase="implementation", polarity="do",
            status="pending_review",
        )
        page.goto(f"{url}/reflections")
        page.wait_for_selector(".reflection-row")

        body = page.content()
        assert "Always commit small" in body
        assert "when refactoring" in body
        # Phase + polarity badges present.
        expect(
            page.locator(
                ".phase-badge.phase-implementation"
            ).first
        ).to_be_visible()
        expect(
            page.locator(".polarity-badge.polarity-do").first
        ).to_be_visible()


class TestReflectionDrawerAndConfirmFlow:
    """Spec §11: seed a reflection, confirm it, verify status transition."""

    def test_clicking_row_opens_drawer_with_confirm_button(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url
        _seed_reflection(
            home, refl_id="r-pending", title="Test pending lesson",
            status="pending_review",
        )
        page.goto(f"{url}/reflections")
        page.wait_for_selector(".reflection-row")
        page.locator(".reflection-row").first.click()
        page.wait_for_selector("#reflection-drawer .reflection-drawer")

        for label in ("Confirm", "Retire", "Edit"):
            expect(
                page.locator(
                    ".drawer-actions button", has_text=label
                )
            ).to_be_visible()

    def test_confirm_pending_reflection_writes_db_and_updates_drawer(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url
        _seed_reflection(
            home, refl_id="r-pending", title="Confirm me",
            status="pending_review",
        )
        page.goto(f"{url}/reflections")
        page.wait_for_selector(".reflection-row")
        page.locator(".reflection-row").first.click()
        page.wait_for_selector("#reflection-drawer .drawer-actions")
        page.locator(
            ".drawer-actions button", has_text="Confirm"
        ).click()

        # Drawer re-renders with status='confirmed' and the Confirm
        # button is gone (status no longer pending_review). Use
        # auto-retrying expect() rather than wait_for_function — same
        # semantics, less brittle.
        expect(
            page.locator(".drawer-actions button.action-confirm")
        ).not_to_be_visible()
        # Retire and Edit are still visible (confirmed reflections
        # can still be retired or edited).
        expect(
            page.locator(".drawer-actions button.action-retire")
        ).to_be_visible()

        # Status in DB.
        db_path = home / "memory.db"
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM reflections WHERE id = 'r-pending'"
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "confirmed"


class TestReflectionInlineEditFlow:
    def test_edit_use_cases_and_hints_via_form(
        self, ui_url: tuple[str, Path], page: Page
    ) -> None:
        url, home = ui_url
        _seed_reflection(
            home, refl_id="r-edit", title="Editable lesson",
            use_cases="old uc", hints=["old hint a", "old hint b"],
            status="pending_review",
        )
        page.goto(f"{url}/reflections")
        page.wait_for_selector(".reflection-row")
        page.locator(".reflection-row").first.click()
        page.wait_for_selector("#reflection-drawer .drawer-actions")
        page.locator(
            ".drawer-actions button", has_text="Edit"
        ).click()

        # Form pre-populated with current values.
        page.wait_for_selector(".reflection-edit-form")
        use_cases_field = page.locator("textarea[name='use_cases']")
        hints_field = page.locator("textarea[name='hints']")
        expect(use_cases_field).to_have_value("old uc")
        # Hints are JSON-decoded by the decode_hints filter and joined
        # with newlines for the textarea.
        expect(hints_field).to_have_value("old hint a\nold hint b")

        # Replace with new values.
        use_cases_field.fill("new uc")
        hints_field.fill("brand new hint")
        page.locator(
            ".reflection-edit-form button.action-save"
        ).click()

        # Drawer re-renders with new content.
        page.wait_for_selector("#reflection-drawer .reflection-drawer")
        body = page.content()
        assert "new uc" in body
        # Hints render as a list — single hint becomes a single <li>.
        assert "brand new hint" in body

        # DB state.
        db_path = home / "memory.db"
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT use_cases, hints FROM reflections "
                "WHERE id = 'r-edit'"
            ).fetchone()
        finally:
            conn.close()
        assert row["use_cases"] == "new uc"
        # Hints stored as JSON-encoded list[str].
        assert json.loads(row["hints"]) == ["brand new hint"]
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
uv run pytest tests/ui/test_browser_reflections.py -v
```

Expected: 5 PASS.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `475 passed, 22 skipped, 3 deselected` (5 new tests on the 470 baseline after Task 1).

- [ ] **Step 4: Commit**

```bash
git add tests/ui/test_browser_reflections.py
git commit -m "Phase 12: e2e browser tests for the Reflections tab"
```

---

## Task 3: Verify CI runs the new tests

`tests/ui/` is already in scope of `.github/workflows/ui-tests.yml` (it runs `uv run pytest tests/ui/ -v --tb=short`). Playwright browsers install via `playwright install --with-deps chromium`. No workflow edit needed — but verify the new files are picked up.

**Files:**
- Verify only — no edits expected.

- [ ] **Step 1: Confirm CI configuration is sufficient**

```bash
cat .github/workflows/ui-tests.yml
```

Confirm the workflow:
1. Triggers on PRs that touch `better_memory/**` OR `tests/ui/**` OR `pyproject.toml` OR `uv.lock` OR the workflow itself. ✓
2. Runs `uv sync --dev` (which installs `pytest-playwright`). ✓
3. Runs `uv run playwright install --with-deps chromium`. ✓
4. Runs `uv run pytest tests/ui/ -v --tb=short`. ✓ (Picks up new files automatically.)

If any step is missing, add it. Otherwise, no edit.

- [ ] **Step 2: Push the branch and confirm the GitHub Actions workflow picks up the new tests**

When this branch is pushed (via the finishing-branch flow at the end of Phase 12), the `ui-tests` job will run and exercise the new e2e files automatically. No workflow change required.

If Step 1 found gaps that needed editing, this Step 2 runs the workflow file too:

```bash
# Only if Step 1 needed edits:
git add .github/workflows/ui-tests.yml
git commit -m "Phase 12: ensure CI installs Playwright + runs e2e tests"
```

- [ ] **Step 3: Confirm no test count regressions**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `475 passed, 22 skipped, 3 deselected`. If Step 1 required no edits, this is the same count as Task 2 — confirm the suite is green.

---

## Task 4: Update CLAUDE snippet (optional)

The CLAUDE snippet doesn't currently document the test suite layout, so there's no obvious place to mention "e2e tests live in `tests/ui/test_browser_*.py`". Skip this task unless we identify a snippet section that would benefit. Phase 12's deliverable is the tests themselves — no behavioural change needs documentation.

If you want a brief mention, add to the development-discipline section a one-liner: "End-to-end browser tests live under `tests/ui/test_browser_*.py` and run on every PR via the `ui-tests` GitHub Actions workflow." Otherwise, skip.

---

## Final review

After all tasks complete, dispatch a final code-review subagent across the full Phase 12 diff. Confirm:

- All tests pass: `uv run pytest --tb=no -q 2>&1 | tail -3` shows `475 passed, 22 skipped, 3 deselected`.
- Spec §11 e2e scenarios are covered (Episodes create+close+timeline; Reflections seed+confirm+status).
- Tests run in real Flask subprocess + real SQLite + real Chromium — no mocks.
- No `pytest.mark.integration` markers (these tests must run on every PR, not just the integration profile).
- The `ui_url` fixture is reused, not duplicated.
- The `pytest_plugins = ["tests.ui.conftest_browser"]` line is present so the fixture is discoverable.
- New test files don't introduce any production-code changes.

Then run `superpowers:finishing-a-development-branch` to push + open the PR (which will trigger the auto-babysit Bugbot loop per the standing instruction).
