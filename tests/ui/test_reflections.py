"""Flask test-client tests for the Reflections tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

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
    scope: str = "project",
    useful_count: int = 0,
    times_misled: int = 0,
    times_overlooked: int = 0,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, useful_count, "
            "times_misled, times_overlooked, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, title or f"title-{rid}", project, tech, phase, polarity,
                use_cases, hints, confidence, status, evidence_count, scope,
                useful_count, times_misled, times_overlooked,
            ),
        )
        conn.commit()
    finally:
        conn.close()


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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Visible")

        response = client.get(
            "/reflections/panel?project=proj-a"
            "&tech=&phase=&polarity=&status=&min_confidence="
        )
        body = response.get_data(as_text=True)
        assert "Visible" in body


class TestReflectionDrawer:
    def test_404_for_unknown_reflection(self, client: FlaskClient):
        response = client.get("/reflections/does-not-exist/drawer")
        assert response.status_code == 404

    def test_renders_full_reflection(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", title="My lesson",
            use_cases="when X happens", hints="do Y, then Z",
            phase="implementation", polarity="dont",
            status="pending_review",  # so Confirm button is visible
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "My lesson" in body
        assert "when X happens" in body
        assert "do Y, then Z" in body
        # Action buttons (status pending_review → confirm visible).
        assert "Confirm" in body
        assert "Retire" in body
        assert "Edit" in body

    def test_omits_confirm_for_already_confirmed(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired")
        response = client.get("/reflections/r-1/drawer")
        body = response.get_data(as_text=True)
        assert "Confirm" not in body
        assert "Retire" not in body
        assert "Edit" not in body
        # But the reflection content still renders (audit / read-only view).
        assert "title-r-1" in body


class TestReflectionConfirm:
    def test_confirms_pending(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="superseded")

        response = client.post(
            "/reflections/r-1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409


class TestReflectionEdit:
    def test_get_returns_form(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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
        # Hints stored as JSON-encoded list (synthesis contract).
        assert row["hints"] == '["new h"]'

    def test_post_400_when_use_cases_empty(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired")

        response = client.post(
            "/reflections/r-1/edit",
            data={"use_cases": "x", "hints": "y"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409


class TestReflectionPromote:
    def test_promotes_project_pending(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="pending_review", scope="project")

        response = client.post(
            "/reflections/r-1/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "reflection-changed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT scope FROM reflections WHERE id = ?", ("r-1",)
            ).fetchone()
        finally:
            conn.close()
        assert row["scope"] == "general"

    def test_404_for_unknown(self, client: FlaskClient):
        response = client.post(
            "/reflections/does-not-exist/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 404

    def test_409_for_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired", scope="project")

        response = client.post(
            "/reflections/r-1/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
        body = response.get_data(as_text=True)
        assert "card-error" in body


class TestReflectionDrawerScope:
    def test_drawer_shows_scope_meta_and_promote_button_for_active_project(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="pending_review", scope="project",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row visible.
        assert "Scope" in body
        assert ">project<" in body
        # Promote button rendered.
        assert "Promote to general" in body
        assert "action-promote" in body
        assert "/reflections/r-1/promote" in body

    def test_drawer_hides_promote_button_when_already_general(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="pending_review", scope="general",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row still visible (now general).
        assert "Scope" in body
        assert ">general<" in body
        # No promote button.
        assert "Promote to general" not in body
        assert "action-promote" not in body

    def test_drawer_hides_promote_button_when_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="retired", scope="project",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row still visible.
        assert "Scope" in body
        # No promote button (existing actions block already hidden on retired).
        assert "Promote to general" not in body
        assert "action-promote" not in body


class TestReflectionDrawerMisledAlwaysShown:
    def test_drawer_shows_misled_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="confirmed")
        body = client.get("/reflections/r-1/drawer").get_data(as_text=True)
        # Precise: the Misled <dt> renders even though times_misled == 0.
        assert "<dt>Misled</dt>" in body


class TestReflectionRowRatingStat:
    def test_row_shows_all_three_badges_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Zero rated")

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "useful 0" in body
        assert "overlooked 0" in body
        assert "misled 0" in body
        assert body.count("rating-zero") >= 2

    def test_useful_badge_inked_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Useful one", useful_count=3)

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "useful 3" in body
        assert "rating-useful" in body

    def test_misled_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Misled one", times_misled=2)

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "misled 2" in body
        assert "rating-misled" in body

    def test_mixed_row_one_coloured_one_grey(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A row with useful > 0 and misled == overlooked == 0 classes each
        badge by its own count: useful inked, overlooked + misled grey."""
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", title="Mixed", useful_count=1, times_misled=0
        )

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "rating-useful" in body
        assert "rating-zero" in body
        assert "rating-misled" not in body
        assert "rating-overlooked" not in body

    def test_row_shows_overlooked_badge_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Zero rated")
        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "overlooked 0" in body

    def test_overlooked_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", title="Overlooked one", times_overlooked=2,
        )
        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "overlooked 2" in body
        assert "rating-overlooked" in body
