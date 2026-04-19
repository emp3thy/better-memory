"""End-to-end browser tests for Phase 2 Pipeline Kanban JS behavior.

Covers the only-one-expanded card invariant defined in spec §4. Future
phases extend with modal open/close, merge picker, etc.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

from better_memory.db.connection import connect

pytest_plugins = ["tests.ui.conftest_browser"]


def _seed_two_candidates(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c1', 'first', 'content one', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c2', 'second', 'content two', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.commit()
    finally:
        conn.close()


def test_expanding_second_card_collapses_first(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    _seed_two_candidates(home / "memory.db")

    page.goto(f"{url}/pipeline")

    page.wait_for_selector('.candidate-card[data-id="c1"]')
    page.wait_for_selector('.candidate-card[data-id="c2"]')

    page.click('.candidate-card[data-id="c1"]')
    page.wait_for_selector('.card-expanded[data-id="c1"]')
    expect(page.locator('[data-expanded="true"]')).to_have_count(1)

    page.click('.candidate-card[data-id="c2"]')
    page.wait_for_selector('.card-expanded[data-id="c2"]')

    expanded = page.locator('[data-expanded="true"]')
    expect(expanded).to_have_count(1)
    expect(expanded).to_have_attribute("data-id", "c2")
