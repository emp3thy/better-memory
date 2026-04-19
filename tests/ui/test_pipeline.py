"""Integration tests for the Pipeline Kanban (spec §4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask.testing import FlaskClient


def _insert_candidate(conn: sqlite3.Connection, project: str, id: str) -> None:
    conn.execute(
        "INSERT INTO insights (id, title, content, project, status, polarity) "
        "VALUES (?, ?, ?, ?, 'pending_review', 'neutral')",
        (id, f"title-{id}", f"content-{id}", project),
    )
    conn.commit()


def _insert_observation(conn: sqlite3.Connection, project: str, id: str) -> None:
    conn.execute(
        "INSERT INTO observations (id, content, project, status) "
        "VALUES (?, ?, ?, 'active')",
        (id, f"obs-{id}", project),
    )
    conn.commit()


class TestPipelinePage:
    def test_renders_summary_bar_with_counts(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_observation(conn, project, "o1")

        response = client.get("/pipeline")
        assert response.status_code == 200
        body = response.data.decode()
        # Four stage labels
        assert "Observations" in body
        assert "Candidates" in body
        assert "Insights" in body
        assert "Promoted" in body
        # Real counts rendered inside the <span class="count"> inside each
        # pill — assert the exact token so a stray "1" elsewhere cannot
        # trigger a false pass.
        import re
        count_tokens = re.findall(r'<span class="count">(\d+)</span>', body)
        # Observations=1, Candidates=1, Insights=0, Promoted=0 (in pill order).
        assert count_tokens == ["1", "1", "0", "0"]

    def test_default_panel_is_candidates(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        body = response.data.decode()
        # The panel-candidates fragment is loaded via HTMX — assert the
        # hx-get attribute is present and points to the candidates panel.
        assert "/pipeline/panel/candidates" in body


class TestObservationsPanel:
    def test_empty_shows_empty_state(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/observations")
        assert response.status_code == 200
        body = response.data.decode()
        assert "No observations yet" in body

    def test_lists_observations_newest_first(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_observation(conn, project, "oldest")
        _insert_observation(conn, project, "newest")

        response = client.get("/pipeline/panel/observations")
        body = response.data.decode()
        # Both visible
        assert "obs-newest" in body
        assert "obs-oldest" in body
        # Newest appears before oldest in the rendered HTML
        assert body.index("obs-newest") < body.index("obs-oldest")
