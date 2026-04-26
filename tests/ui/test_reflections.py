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
        # Hints stored as JSON-encoded list (synthesis contract).
        assert row["hints"] == '["new h"]'

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
