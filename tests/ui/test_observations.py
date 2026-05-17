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

    def test_does_not_render_synth_button(self, client: FlaskClient):
        """Synthesis is now driven by the IDE-LLM via MCP tools, not a UI button."""
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert "Run synthesis" not in body
        assert "btn-synth" not in body


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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
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
        assert 'rail-link active' in body
        assert "Observations" in body


class TestPromoteToSemantic:
    def _seed_active_observation(self, conn, *, obs_id="o1", project="proj-a"):
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', ?, '2026-04-01T00:00:00+00:00')",
            (project,),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "(?, 'durable rule', ?, 'ep1', 'active', 'success',"
            " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, project),
        )
        conn.commit()

    def test_promote_creates_memory_and_flips_status(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            self._seed_active_observation(seed_conn, obs_id="o1")
        response = client.post(
            "/observations/o1/promote-to-semantic",
            data={"scope": "general"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "general" in body
        trigger = response.headers.get("HX-Trigger") or ""
        assert "observations-changed" in trigger
        assert "semantic-changed" in trigger
        with sqlite3.connect(tmp_db) as check:
            mem = check.execute(
                "SELECT content, scope, project FROM semantic_memories"
            ).fetchone()
            obs = check.execute(
                "SELECT status FROM observations WHERE id='o1'"
            ).fetchone()
        assert mem[0] == "durable rule"
        assert mem[1] == "general"
        assert mem[2] == "proj-a"
        assert obs[0] == "consumed_without_reflection"

    def test_promote_default_scope_is_project(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            self._seed_active_observation(seed_conn, obs_id="o1")
        response = client.post(
            "/observations/o1/promote-to-semantic",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT scope FROM semantic_memories"
            ).fetchone()
        assert row[0] == "project"

    def test_promote_already_consumed_returns_400(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO episodes (id, project, started_at) VALUES "
                "('ep1', 'proj-a', '2026-04-01T00:00:00+00:00')"
            )
            seed_conn.execute(
                "INSERT INTO observations (id, content, project, episode_id, "
                "status, outcome, created_at, status_changed_at) VALUES "
                "('o1','x','proj-a','ep1','consumed_into_reflection','success',"
                " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/observations/o1/promote-to-semantic",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400
        body = response.get_data(as_text=True)
        assert "card-error" in body

    def test_promote_missing_observation_returns_400(
        self, client: FlaskClient,
    ):
        response = client.post(
            "/observations/ghost/promote-to-semantic",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400

    def test_promote_invalid_scope_returns_400(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            self._seed_active_observation(seed_conn, obs_id="o1")
        response = client.post(
            "/observations/o1/promote-to-semantic",
            data={"scope": "bogus"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400
        body = response.get_data(as_text=True)
        assert "card-error" in body
        with sqlite3.connect(tmp_db) as check:
            count = check.execute(
                "SELECT COUNT(*) FROM semantic_memories"
            ).fetchone()[0]
            obs_status = check.execute(
                "SELECT status FROM observations WHERE id='o1'"
            ).fetchone()[0]
        assert count == 0
        assert obs_status == "active"


class TestObservationRowReinforcement:
    def _seed_obs_score(self, db_path: Path, *, oid: str, score: float) -> None:
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, component, theme, outcome, status, "
                " episode_id, reinforcement_score, created_at) VALUES "
                "(?, 'obs body', 'proj-a', 'ui_launcher', 'bug', 'neutral', "
                " 'active', 'ep-1', ?, '2026-04-26T10:00:00+00:00')",
                (oid, score),
            )
            conn.commit()
        finally:
            conn.close()

    def test_positive_score_takes_pos_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=2.5)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf 2.5" in body
        assert "reinf-pos" in body

    def test_negative_score_takes_neg_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=-1.5)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf -1.5" in body
        assert "reinf-neg" in body

    def test_zero_score_takes_zero_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=0.0)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf 0.0" in body
        assert "reinf-zero" in body


class TestObservationDrawerPromoteForm:
    def _drawer_for(self, client, conn, obs_id, status):
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', 'proj-a', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, "
            "status, outcome, created_at, status_changed_at) VALUES "
            "(?, 'rule text', 'proj-a', 'ep1', ?, 'success',"
            " '2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, status),
        )
        conn.commit()
        return client.get(f"/observations/{obs_id}/drawer").get_data(as_text=True)

    def test_drawer_shows_promote_form_when_active(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            body = self._drawer_for(client, seed_conn, "o1", "active")
        assert "promote-to-semantic" in body
        assert 'name="scope"' in body
        assert 'value="project"' in body
        assert 'value="general"' in body

    def test_drawer_hides_promote_form_when_consumed(
        self, client: FlaskClient, tmp_db: Path,
    ):
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            body = self._drawer_for(client, seed_conn, "o1", "consumed_into_reflection")
        assert "promote-to-semantic" not in body
