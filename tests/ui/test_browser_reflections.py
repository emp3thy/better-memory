"""End-to-end Playwright tests for the Reflections tab.

Spec §11: "Reflections tab: seed a reflection, confirm it, verify
status transition."
"""

from __future__ import annotations

import json
from pathlib import Path

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
