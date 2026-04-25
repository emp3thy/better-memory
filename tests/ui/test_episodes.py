"""Flask test-client tests for the Episodes tab."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.services.episode import EpisodeService


def _seed(db_path: Path, project: str = "better-memory") -> str:
    conn = connect(db_path)
    try:
        clock = lambda: datetime(2026, 4, 24, 9, 0, 0, tzinfo=UTC)
        svc = EpisodeService(conn, clock=clock)
        ep_id = svc.open_background(session_id="ui-test", project=project)
        svc.start_foreground(
            session_id="ui-test",
            project=project,
            goal="ship Episodes tab",
            tech="python",
        )
        return ep_id
    finally:
        conn.close()


class TestEpisodesPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/episodes")
        assert response.status_code == 200

    def test_empty_state_when_no_episodes(self, client: FlaskClient):
        response = client.get("/episodes/panel")
        body = response.get_data(as_text=True)
        assert "No episodes yet" in body

    def test_shows_episode_in_timeline(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The UI infers project from cwd().name. Monkeypatch _project_name
        # to a stable value matching the seed.
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes/panel")
        body = response.get_data(as_text=True)
        assert "ship Episodes tab" in body
        assert "python" in body

    def test_groups_by_day_heading(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes/panel")
        body = response.get_data(as_text=True)
        # ISO date prefix from started_at appears in a day-group heading.
        assert "2026-04-24" in body


class TestEpisodesBanner:
    def test_banner_zero_when_no_open_episodes(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        response = client.get("/episodes/banner")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Empty banner partial — no "unclosed" text.
        assert "unclosed" not in body.lower()

    def test_banner_shows_count_when_open(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed(tmp_db, project="proj-a")
        response = client.get("/episodes/banner")
        body = response.get_data(as_text=True)
        assert "1" in body
        assert "unclosed" in body.lower()


class TestEpisodeDrawer:
    def test_404_for_unknown_episode(self, client: FlaskClient):
        response = client.get("/episodes/does-not-exist/drawer")
        assert response.status_code == 404

    def test_renders_drawer_for_open_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")
        response = client.get(f"/episodes/{ep_id}/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "ship Episodes tab" in body
        # Open-episode actions present.
        assert "Close as success" in body
        assert "Close as partial" in body
        assert "Close as abandoned" in body
        assert "Close as no_outcome" in body
        assert "Continuing" in body

    def test_omits_close_actions_for_closed_episode(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        ep_id = _seed(tmp_db, project="proj-a")
        # Close it.
        conn = connect(tmp_db)
        try:
            EpisodeService(conn).close_active(
                session_id="ui-test",
                outcome="success",
                close_reason="goal_complete",
            )
        finally:
            conn.close()

        response = client.get(f"/episodes/{ep_id}/drawer")
        body = response.get_data(as_text=True)
        assert "Close as success" not in body
        assert "Continuing" not in body
        assert "success" in body  # outcome badge still rendered
