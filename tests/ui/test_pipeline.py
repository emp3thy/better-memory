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


class TestInsightActions:
    def _insert_insight(
        self, conn: sqlite3.Connection, project: str, id: str,
        status: str = "confirmed"
    ) -> None:
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES (?, ?, ?, ?, ?, 'neutral')",
            (id, f"title-{id}", f"content-{id}", project, status),
        )
        conn.commit()

    def test_retire_moves_insight_to_retired(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")

        response = client.post(
            "/insights/i1/retire",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.data.strip() == b""
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'i1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_demote_promoted_to_confirmed(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "pr1", status="promoted")

        response = client.post(
            "/insights/pr1/demote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'pr1'"
        ).fetchone()
        assert row["status"] == "confirmed"

    def test_edit_form_and_save(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")

        form_response = client.get("/insights/i1/edit")
        assert form_response.status_code == 200
        assert b"<form" in form_response.data

        save_response = client.post(
            "/insights/i1/edit",
            data={"title": "new", "content": "new-content"},
            headers={"Origin": "http://localhost"},
        )
        assert save_response.status_code == 200
        assert b"new" in save_response.data
        row = conn.execute(
            "SELECT title FROM insights WHERE id = 'i1'"
        ).fetchone()
        assert row["title"] == "new"

    def test_view_sources_returns_linked_observations(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")
        _insert_observation(conn, project, "oA")
        conn.execute(
            "INSERT INTO insight_sources (insight_id, observation_id) "
            "VALUES ('i1', 'oA')"
        )
        conn.commit()

        response = client.get("/insights/i1/sources")
        assert response.status_code == 200
        assert b"obs-oA" in response.data

    def test_view_sources_empty(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        self._insert_insight(conn, project, "i1")
        response = client.get("/insights/i1/sources")
        assert response.status_code == 200
        assert b"No source observations" in response.data


class TestPromoteStub:
    def test_promote_renders_deferred_message(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('i1', 't', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/insights/i1/promote")
        assert response.status_code == 200
        assert b"Phase 7" in response.data


class TestMergePicker:
    def test_picker_lists_other_pending_candidates(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_candidate(conn, project, "c2")
        _insert_candidate(conn, project, "c3")

        response = client.get("/candidates/c1/merge")
        assert response.status_code == 200
        body = response.data.decode()
        assert "c2" in body
        assert "c3" in body
        assert "id=\"merge-target-c1\"" not in body

    def test_merge_post_combines_candidates(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        _insert_candidate(conn, project, "c2")

        response = client.post(
            "/candidates/c1/merge?target=c2",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.data.strip() == b""
        row = conn.execute(
            "SELECT status FROM insights WHERE id = 'c1'"
        ).fetchone()
        assert row["status"] == "retired"

    def test_merge_post_validation_error_surfaces(
        self, client: FlaskClient
    ) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        _insert_candidate(conn, project, "c1")
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, "
            "polarity) VALUES ('tgt', 't', 'c', ?, 'retired', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.post(
            "/candidates/c1/merge?target=tgt",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert b"Cannot merge into status 'retired'" in response.data


class TestConsolidationButton:
    def test_click_returns_running_job_fragment(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert b'data-job-id="' in response.data

    def test_completed_job_renders_branch_candidates(
        self, client: FlaskClient
    ) -> None:
        """End-to-end: seed a cluster, run consolidate, wait for the job
        thread to finish deterministically, verify the rendered fragment
        lists the candidate."""
        import re
        import threading

        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name
        for i in range(3):
            conn.execute(
                "INSERT INTO observations (id, content, project, component, "
                "theme, status, validated_true, outcome) VALUES "
                "(?, ?, ?, 'api', 'retry', 'active', 1, 'success')",
                (f"obs-{i}", f"content {i}", project),
            )
        conn.commit()

        fake = client.application.config["_fake_chat"]
        fake.responses.append(
            "Always retry 5xx responses with exponential backoff; "
            "never retry 4xx."
        )

        post_resp = client.post(
            "/pipeline/consolidate",
            headers={"Origin": "http://localhost"},
        )
        match = re.search(rb'data-job-id="([a-f0-9]+)"', post_resp.data)
        assert match is not None
        job_id = match.group(1).decode()

        # Deterministic wait — join the consolidation thread by name.
        for t in threading.enumerate():
            if t.name.startswith("consolidation-"):
                t.join(timeout=5.0)
                assert not t.is_alive(), "consolidation thread did not exit"

        get_resp = client.get(f"/jobs/{job_id}")
        assert b"job-status-complete" in get_resp.data
        assert b"Always retry 5xx" in get_resp.data
