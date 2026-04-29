"""Playwright integration tests for the Observations tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from better_memory.db.connection import connect

pytest_plugins = ["tests.ui.conftest_browser"]


def _project_name() -> str:
    return Path.cwd().name


def _seed_episode(db_path: Path, *, eid: str = "ep-1") -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
            (eid, _project_name()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_obs(
    db_path: Path,
    *,
    oid: str,
    content: str,
    outcome: str = "neutral",
    component: str = "ui_launcher",
    status: str = "active",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, created_at) "
            "VALUES "
            "(?, ?, ?, ?, 'bug', ?, ?, 'ep-1', "
            " '2026-04-26T10:00:00+00:00')",
            (oid, content, _project_name(), component, outcome, status),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
def test_observations_tab_lists_seeded_rows(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_episode(db_path)
    _seed_obs(db_path, oid="o-1", content="visible row")

    page.goto(f"{url}/observations")
    page.wait_for_selector(".observation-row")
    expect(page.get_by_text("visible row")).to_be_visible()


@pytest.mark.integration
def test_filter_by_outcome_updates_panel(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_episode(db_path)
    _seed_obs(db_path, oid="o-fail", content="bad-row", outcome="failure")
    _seed_obs(db_path, oid="o-ok", content="good-row", outcome="success")

    page.goto(f"{url}/observations")
    page.wait_for_selector(".observation-row")
    page.locator('select[name="outcome"]').select_option("failure")
    expect(page.get_by_text("bad-row")).to_be_visible()
    expect(page.get_by_text("good-row")).not_to_be_visible()


@pytest.mark.integration
def test_clicking_row_opens_drawer(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_episode(db_path)
    _seed_obs(
        db_path,
        oid="o-1",
        content="content for drawer test",
    )

    page.goto(f"{url}/observations")
    page.wait_for_selector(".observation-row")
    page.get_by_text("content for drawer test").click()
    expect(page.locator("#observation-drawer")).to_contain_text(
        "content for drawer test"
    )
