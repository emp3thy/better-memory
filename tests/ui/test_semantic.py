"""Route tests for the /semantic UI tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient


class TestSemanticPage:
    def test_returns_200_with_active_nav_tab(
        self, client: FlaskClient,
    ):
        response = client.get("/semantic")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "rail-link active" in body
        assert "Semantic" in body


class TestSemanticPanel:
    def test_empty_state_when_no_memories(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-empty")
        response = client.get("/semantic/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No semantic memories yet" in body
        assert 'id="semantic-create-form"' in body

    def test_renders_seeded_rows_newest_first(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','older rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00'),"
                "('m2','newer rule','proj-a','general',"
                " '2026-05-04T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/panel")
        body = response.get_data(as_text=True)
        assert "older rule" in body
        assert "newer rule" in body
        assert body.index("newer rule") < body.index("older rule")

    def test_includes_general_from_other_projects(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','proj-a project rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00'),"
                "('m2','proj-b general rule','proj-b','general',"
                " '2026-05-04T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/panel")
        body = response.get_data(as_text=True)
        assert "proj-a project rule" in body
        assert "proj-b general rule" in body


class TestSemanticCreate:
    def test_creates_row_and_returns_hx_trigger(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic",
            data={"content": "new rule", "scope": "general"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT content, scope, project FROM semantic_memories"
            ).fetchone()
        assert row[0] == "new rule"
        assert row[1] == "general"
        assert row[2] == "proj-a"

    def test_empty_content_returns_400_card_error(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic", data={"content": "   "},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400
        body = response.get_data(as_text=True)
        assert "card-error" in body


class TestSemanticScope:
    def test_toggle_project_to_general(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/semantic/m1/scope", data={"scope": "general"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT scope FROM semantic_memories WHERE id='m1'"
            ).fetchone()
        assert row[0] == "general"

    def test_missing_id_returns_400(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic/ghost/scope", data={"scope": "general"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400


class TestSemanticDelete:
    def test_delete_removes_row(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/semantic/m1/delete",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            count = check.execute(
                "SELECT COUNT(*) FROM semantic_memories"
            ).fetchone()[0]
        assert count == 0

    def test_delete_missing_is_idempotent(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic/ghost/delete",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200


class TestSemanticDrawer:
    def test_drawer_renders_edit_form_with_current_values(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','existing rule','proj-a','general',"
                " '2026-05-01T10:00:00+00:00','2026-05-04T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.get("/semantic/m1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "existing rule" in body
        assert "general" in body
        assert "proj-a" in body

    def test_drawer_returns_404_for_missing(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.get("/semantic/ghost/drawer")
        assert response.status_code == 404


class TestSemanticUpdate:
    def test_update_changes_content(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import sqlite3
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','old text','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()
        response = client.post(
            "/semantic/m1/update", data={"content": "new text"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "semantic-changed"
        with sqlite3.connect(tmp_db) as check:
            row = check.execute(
                "SELECT content FROM semantic_memories WHERE id='m1'"
            ).fetchone()
        assert row[0] == "new text"

    def test_update_empty_content_returns_400(
        self, client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.post(
            "/semantic/anything/update", data={"content": "   "},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 400
