"""End-to-end Playwright tests for the Episodes tab.

Spec §11: "Episodes tab: create an episode, close it, verify it
appears in the timeline."

Uses the `ui_url` fixture from `conftest_browser.py` which spawns
a real Flask subprocess against an isolated SQLite home.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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

        _seed_episode_via_service(
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
