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


class TestCandidatesPanel:
    def test_empty_shows_run_consolidation_message(
        self, client: FlaskClient
    ) -> None:
        response = client.get("/pipeline/panel/candidates")
        assert response.status_code == 200
        assert b"No candidates" in response.data

    def test_lists_candidates_with_approve_reject(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.get("/pipeline/panel/candidates")
        body = response.data.decode()
        assert "title-c1" in body
        assert "Approve" in body
        assert "Reject" in body


class TestInsightsPanel:
    def test_empty(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/insights")
        assert response.status_code == 200
        assert b"No insights" in response.data

    def test_lists_insights_with_promote_retire(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 'title-i1', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()
        response = client.get("/pipeline/panel/insights")
        body = response.data.decode()
        assert "title-i1" in body
        assert "Promote" in body
        assert "Retire" in body


class TestPromotedPanel:
    def test_empty(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/panel/promoted")
        assert response.status_code == 200
        assert b"No promoted" in response.data

    def test_lists_promoted_with_view_doc_demote(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('pr1', 'title-pr1', 'c', ?, 'promoted', 'neutral')",
            (project,),
        )
        conn.commit()
        response = client.get("/pipeline/panel/promoted")
        body = response.data.decode()
        assert "title-pr1" in body
        assert "View doc" in body
        assert "Demote" in body


class TestExpandedCards:
    def test_candidate_expanded_shows_full_content_and_all_actions(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.get("/candidates/c1/card")
        assert response.status_code == 200
        body = response.data.decode()
        assert "title-c1" in body
        assert "content-c1" in body
        # All four actions: Approve, Reject, Edit, Merge
        assert "Approve" in body
        assert "Reject" in body
        assert "Edit" in body
        assert "Merge" in body
        assert 'data-expanded="true"' in body

    def test_insight_expanded_shows_edit_and_view_sources(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 'title-i1', 'content-i1', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/insights/i1/card")
        body = response.data.decode()
        assert "content-i1" in body
        assert "Promote" in body
        assert "Retire" in body
        assert "Edit" in body
        assert "View sources" in body

    def test_missing_card_returns_404(self, client: FlaskClient) -> None:
        response = client.get("/candidates/does-not-exist/card")
        assert response.status_code == 404


class TestCandidateActions:
    def test_approve_moves_candidate_to_confirmed(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.post(
            "/candidates/c1/approve",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.data.strip() == b""
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "confirmed"

    def test_reject_moves_candidate_to_retired(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        response = client.post(
            "/candidates/c1/reject",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_edit_form_then_save(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")

        form_response = client.get("/candidates/c1/edit")
        assert form_response.status_code == 200
        assert b"<form" in form_response.data
        assert b"title-c1" in form_response.data

        save_response = client.post(
            "/candidates/c1/edit",
            data={"title": "new title", "content": "new content"},
            headers={"Origin": "http://localhost"},
        )
        assert save_response.status_code == 200
        assert b"new title" in save_response.data

        row = conn.execute(
            "SELECT title, content FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["title"] == "new title"
        assert row["content"] == "new content"
