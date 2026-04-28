"""Flask test-client tests for the Observations tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect


def _seed_episode(
    db_path: Path, *, eid: str = "ep-1", project: str = "proj-a"
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
            (eid, project),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_obs(
    db_path: Path,
    *,
    oid: str,
    project: str = "proj-a",
    component: str | None = "ui_launcher",
    theme: str | None = "bug",
    outcome: str = "neutral",
    status: str = "active",
    content: str = "test obs",
    episode_id: str = "ep-1",
    created_at: str = "2026-04-26T10:00:00+00:00",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, content, project, component, theme, outcome, status,
             episode_id, created_at),
        )
        conn.commit()
    finally:
        conn.close()


class TestObservationsPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/observations")
        assert response.status_code == 200

    def test_renders_filter_form(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert 'name="status"' in body
        assert 'name="outcome"' in body
        assert 'name="component"' in body

    def test_renders_run_synthesis_button(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert "Run synthesis" in body


class TestServiceWiring:
    def test_synthesis_dependencies_are_in_app_extensions(
        self, client: FlaskClient
    ) -> None:
        app = client.application
        assert "chat" in app.extensions
        assert "db_path" in app.extensions


class TestObservationsPanel:
    def test_empty_state_when_no_observations(self, client: FlaskClient):
        response = client.get("/observations/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No observations" in body

    def test_renders_seeded_rows(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="hello world")
        _seed_obs(tmp_db, oid="o-2", content="second")

        response = client.get("/observations/panel?project=proj-a")
        body = response.get_data(as_text=True)
        assert "hello world" in body
        assert "second" in body

    def test_filters_by_outcome(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-fail", outcome="failure", content="bad")
        _seed_obs(tmp_db, oid="o-ok", outcome="success", content="good")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=failure"
        )
        body = response.get_data(as_text=True)
        assert "bad" in body
        assert "good" not in body

    def test_filters_by_status(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-active", status="active", content="A")
        _seed_obs(tmp_db, oid="o-arch", status="archived", content="X")

        response = client.get(
            "/observations/panel?project=proj-a&status=active"
        )
        body = response.get_data(as_text=True)
        assert "A" in body
        assert "X" not in body

    def test_filters_by_component(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-ui", component="ui_launcher", content="ui")
        _seed_obs(tmp_db, oid="o-mcp", component="mcp", content="mcp")

        response = client.get(
            "/observations/panel?project=proj-a&component=ui_launcher"
        )
        body = response.get_data(as_text=True)
        assert "ui" in body
        assert "mcp" not in body

    def test_blank_filter_values_are_treated_as_unset(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", outcome="failure", content="A")
        _seed_obs(tmp_db, oid="o-2", outcome="success", content="B")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=&status="
        )
        body = response.get_data(as_text=True)
        # Both should appear when filters are blank.
        assert "A" in body
        assert "B" in body


class TestObservationDrawer:
    def test_renders_full_content(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="full content for drawer")

        response = client.get("/observations/o-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "full content for drawer" in body

    def test_returns_404_for_unknown_id(self, client: FlaskClient):
        response = client.get("/observations/nope/drawer")
        assert response.status_code == 404

    def test_renders_metadata_grid(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        conn = connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, component, theme, outcome, status, "
                " episode_id, tech, trigger_type, reinforcement_score, "
                " created_at) "
                "VALUES "
                "('o-1', 'x', 'proj-a', 'ui_launcher', 'bug', 'failure', "
                " 'active', 'ep-1', 'python', 'review', 1.5, "
                " '2026-04-26T10:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get("/observations/o-1/drawer")
        body = response.get_data(as_text=True)
        assert "ui_launcher" in body
        assert "bug" in body
        assert "python" in body
        assert "review" in body
        assert "ep-1" in body
        # reinforcement_score appears as text
        assert "1.5" in body


class TestNavTab:
    def test_observations_tab_appears_in_base_layout(
        self, client: FlaskClient
    ):
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert ">Observations<" in body
        assert "/observations" in body

    def test_observations_tab_marked_active_on_observations_page(
        self, client: FlaskClient
    ):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert 'class="tab active"' in body
        assert "Observations" in body


class TestObservationsSynthesize:
    def test_calls_service_and_returns_banner(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def fake_synthesize(self, *, goal, tech, project):
            assert goal == "manual synthesis"
            assert tech is None
            assert project == "proj-a"
            return {
                "do": [{"id": "r1"}, {"id": "r2"}],
                "dont": [{"id": "r3"}],
                "neutral": [],
            }

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", fake_synthesize
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == (
            "observations-synthesized"
        )
        body = response.get_data(as_text=True)
        # Banner mentions the bucket counts.
        assert "2" in body and "do" in body
        assert "1" in body and "dont" in body
        assert "0" in body and "neutral" in body

    def test_returns_500_card_error_on_service_failure(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def boom(self, *, goal, tech, project):
            raise RuntimeError("ollama unreachable")

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", boom
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "card-error" in body
        assert "ollama unreachable" in body

    def test_synthesize_uses_worker_thread_connection_not_app_connection(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: the route must NOT reuse the app's db_connection.

        sqlite3 connections are not thread-safe by default. The route
        dispatches synthesize() to a worker thread, so it must open a
        fresh connection there. We verify by stubbing OllamaChat.complete
        (the lowest LLM-touching boundary) so the real synthesize body
        runs against a real per-thread connection.
        """
        from better_memory.llm.ollama import OllamaChat
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        # Return a parseable empty SynthesisResponse so the synthesize
        # body finishes without producing reflections — we don't care
        # about output, only that no ProgrammingError fires.
        async def fake_complete(self, prompt: str) -> str:
            return '{"new": [], "augment": [], "merge": [], "ignore": []}'

        monkeypatch.setattr(OllamaChat, "complete", fake_complete)

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        # Critically: not 500. Specifically not a ProgrammingError card.
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_data(as_text=True)
        assert "ProgrammingError" not in body
        assert "thread" not in body.lower()
        assert response.headers.get("HX-Trigger") == (
            "observations-synthesized"
        )
