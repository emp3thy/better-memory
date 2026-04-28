"""Flask test-client tests for the Observations tab."""

from __future__ import annotations

from pathlib import Path

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
