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
    url, _home = ui_url
    page.goto(f"{url}/")
    expect(
        page.get_by_role("link", name="Diagnostics")
    ).to_be_visible(timeout=_HTMX_TIMEOUT_MS)


@pytest.mark.integration
def test_hook_errors_panel_lists_seeded_rows(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")

    page.goto(f"{url}/diagnostics")
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
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")

    page.goto(f"{url}/diagnostics")
    expect(page.locator(".hook-error-row")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".hook-error-row .time").first.click()
    expect(page.locator("#hook-error-drawer")).to_contain_text(
        "Traceback (most recent call last):", timeout=_HTMX_TIMEOUT_MS
    )


@pytest.mark.integration
def test_per_row_delete_removes_the_row(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")
    _seed_hook_error(db_path, error_id="e-2")

    page.goto(f"{url}/diagnostics")
    page.on("dialog", lambda d: d.accept())

    expect(page.locator(".hook-error-row")).to_have_count(
        2, timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".hook-error-row .delete-row").first.click()
    expect(page.locator(".hook-error-row")).to_have_count(
        1, timeout=_HTMX_TIMEOUT_MS
    )


@pytest.mark.integration
def test_purge_all_clears_panel(
    ui_url: tuple[str, Path], page: Page
) -> None:
    url, home = ui_url
    db_path = home / "memory.db"
    _seed_hook_error(db_path, error_id="e-1")
    _seed_hook_error(db_path, error_id="e-2")

    page.goto(f"{url}/diagnostics")
    page.on("dialog", lambda d: d.accept())

    expect(page.locator(".purge-all")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
    page.locator(".purge-all").click()
    expect(page.locator(".hook-error-row")).to_have_count(
        0, timeout=_HTMX_TIMEOUT_MS
    )
    expect(page.get_by_text("No hook errors recorded")).to_be_visible(
        timeout=_HTMX_TIMEOUT_MS
    )
