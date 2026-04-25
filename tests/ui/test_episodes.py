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
