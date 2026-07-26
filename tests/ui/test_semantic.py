"""Route tests for the /semantic UI tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.services.semantic import SemanticMemory


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
        # Form must include an error feedback target so 400 responses
        # surface to the user (htmx hx-swap=innerHTML target).
        assert 'id="semantic-create-error"' in body

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


class TestSemanticPanelFilters:
    def _seed(self, tmp_db: Path) -> None:
        import sqlite3
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','use ruff for linting','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00'),"
                "('m2','prefer terse replies','proj-a','general',"
                " '2026-05-02T10:00:00+00:00','2026-05-02T10:00:00+00:00'),"
                "('m3','python 3.12 only','proj-b','general',"
                " '2026-05-03T10:00:00+00:00','2026-05-03T10:00:00+00:00')"
            )
            seed_conn.commit()

    def test_page_renders_filter_form(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        response = client.get("/semantic")
        body = response.get_data(as_text=True)
        assert 'id="semantic-filter-form"' in body
        assert 'name="scope_filter"' in body
        assert 'name="search"' in body

    def test_panel_default_returns_project_and_general_rows(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed(tmp_db)
        response = client.get("/semantic/panel")
        body = response.get_data(as_text=True)
        assert "use ruff for linting" in body
        assert "prefer terse replies" in body
        assert "python 3.12 only" in body  # general from proj-b

    def test_panel_scope_filter_project_only(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed(tmp_db)
        response = client.get("/semantic/panel?scope_filter=project")
        body = response.get_data(as_text=True)
        assert "use ruff for linting" in body
        assert "prefer terse replies" not in body
        assert "python 3.12 only" not in body

    def test_panel_scope_filter_general_only(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed(tmp_db)
        response = client.get("/semantic/panel?scope_filter=general")
        body = response.get_data(as_text=True)
        assert "use ruff for linting" not in body
        assert "prefer terse replies" in body
        assert "python 3.12 only" in body

    def test_panel_search_filters_by_substring_case_insensitive(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed(tmp_db)
        response = client.get("/semantic/panel?search=RUFF")
        body = response.get_data(as_text=True)
        assert "use ruff for linting" in body
        assert "prefer terse replies" not in body
        assert "python 3.12 only" not in body

    def test_panel_unknown_scope_filter_falls_back_to_default(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module
        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed(tmp_db)
        response = client.get("/semantic/panel?scope_filter=garbage")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "use ruff for linting" in body
        assert "prefer terse replies" in body


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


class TestSemanticDrawerMisledAlwaysShown:
    def test_drawer_shows_misled_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        # Precise: the Misled <dt> renders even though times_misled == 0.
        assert "<dt>Misled</dt>" in body


class TestSemanticDrawerOverlookedAlwaysShown:
    def test_drawer_shows_overlooked_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "<dt>Overlooked</dt>" in body

    def test_drawer_shows_overlooked_count_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import re
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, times_overlooked, "
                " created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project', 3,"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "<dt>Overlooked</dt>" in body
        # Anchor on the Overlooked <dd> so an incidental "3" elsewhere
        # cannot satisfy the assertion.
        m = re.search(r"<dt>Overlooked</dt>\s*<dd>\s*(\d+)", body)
        assert m is not None and m.group(1) == "3"


class TestSemanticDrawerRatingEvidence:
    def _seed_memory(self, tmp_db: Path) -> None:
        import sqlite3

        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()

    def _seed_rating_evidence(
        self, tmp_db: Path, *, classification: str, evidence: str,
    ) -> None:
        from better_memory.db.connection import connect

        conn = connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO session_memory_exposure "
                "(session_id, memory_kind, memory_id, exposed_at, source, "
                "rated_at, classification, evidence) "
                "VALUES ('s-1', 'semantic', 'm1', "
                "'2026-05-01T10:00:00+00:00', 'bootstrap', "
                "'2026-05-01T11:00:00+00:00', ?, ?)",
                (classification, evidence),
            )
            conn.commit()
        finally:
            conn.close()

    def test_drawer_shows_section_and_evidence_when_present(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed_memory(tmp_db)
        self._seed_rating_evidence(
            tmp_db, classification="cited", evidence="quoted verbatim in the fix",
        )
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "Rating evidence" in body
        assert "quoted verbatim in the fix" in body
        assert "chip outcome-success" in body

    def test_drawer_omits_section_when_no_evidence(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        self._seed_memory(tmp_db)
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "Rating evidence" not in body


class TestSemanticRowRatingStat:
    def test_row_shows_all_three_badges_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "useful 0" in body
        assert "overlooked 0" in body
        assert "misled 0" in body
        assert body.count("rating-zero") >= 2

    def test_badges_coloured_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, useful_count, times_misled, "
                " created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project', 4, 1,"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "useful 4" in body
        assert "rating-useful" in body
        assert "misled 1" in body
        assert "rating-misled" in body

    def test_row_shows_overlooked_badge_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "overlooked 0" in body

    def test_overlooked_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, times_overlooked, "
                " created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project', 3,"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "overlooked 3" in body
        assert "rating-overlooked" in body


class TestSemanticEmbeddingWiring:
    """UI create/update routes must embed, not just MCP-driven writes.

    The shared `client` fixture pins BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite
    (see tests/ui/conftest.py) so ordinary route tests don't depend on a
    live Ollama instance. These tests build their own app with an explicit
    fake sync_embedder to prove the wiring — app.py passes
    app.extensions["sync_embedder"] into every SemanticMemoryService(...)
    construction site.
    """

    def _make_client(self, tmp_db: Path, fake_embedder):
        from unittest.mock import patch as _patch

        from better_memory.embeddings.sync_embed import SyncEmbedder
        from better_memory.ui.app import create_app

        app = create_app(
            start_watchdog=False, db_path=tmp_db,
            sync_embedder=SyncEmbedder(lambda: fake_embedder),
        )
        app.config["TESTING"] = True
        self._timer_patch = _patch("better_memory.ui.app.threading.Timer")
        self._timer_patch.start()
        return app.test_client()

    def _vec_count(self, tmp_db: Path) -> int:
        # sqlite_vec's vec0 virtual table needs its extension loaded — a
        # bare sqlite3.connect() raises "no such module: vec0". Use the
        # project's connect() helper, which loads it.
        from better_memory.db.connection import connect as _connect

        conn = _connect(tmp_db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM semantic_embeddings"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_create_writes_semantic_embedding_row(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from better_memory.ui import app as app_module
        from tests.services._embedding_fakes import FakeEmbedder

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        fake = FakeEmbedder()
        client = self._make_client(tmp_db, fake)
        try:
            response = client.post(
                "/semantic",
                data={"content": "new rule", "scope": "general"},
                headers={"Origin": "http://localhost"},
            )
            assert response.status_code == 200
        finally:
            self._timer_patch.stop()

        assert self._vec_count(tmp_db) == 1
        assert fake.calls == ["new rule"]

    def test_update_reembeds_semantic_memory(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sqlite3

        from better_memory.ui import app as app_module
        from tests.services._embedding_fakes import FakeEmbedder

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as seed_conn:
            seed_conn.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','old text','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            seed_conn.commit()

        fake = FakeEmbedder()
        client = self._make_client(tmp_db, fake)
        try:
            response = client.post(
                "/semantic/m1/update", data={"content": "new text"},
                headers={"Origin": "http://localhost"},
            )
            assert response.status_code == 200
        finally:
            self._timer_patch.stop()

        assert self._vec_count(tmp_db) == 1  # replaced, not duplicated
        assert fake.calls == ["new text"]


class _CapsStub:
    """Exposes the six capability flags the PR1 caps context-processor reads
    at render time (all True: PR2 does no gating, values are irrelevant, but
    the attributes MUST exist or rendering KeyErrors)."""
    supports_episodes = True
    supports_observations = True
    supports_provenance = True
    supports_retention_runs = True
    supports_reflection_review = True
    supports_reflection_text_edit = True


class _SemanticStubBackend(_CapsStub):
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    def semantic_list(self, *, project=None, scope_filter=None, search=None,
                      track_exposure=True):
        self.calls.append(("list", project, scope_filter, search))
        return self._rows

    def semantic_observe(self, *, content, project=None, scope="project"):
        self.calls.append(("observe", content, scope))
        return "new-id"

    def semantic_set_scope(self, *, id, scope):
        self.calls.append(("scope", id, scope))

    def semantic_delete(self, *, id):
        self.calls.append(("delete", id))

    def semantic_update_text(self, *, id, content):
        self.calls.append(("update", id, content))

    def semantic_get(self, *, id) -> SemanticMemory | None:
        self.calls.append(("get", id))
        return None


def _semantic_row(id="ac-1", content="agentcore rule", scope="project"):
    return SemanticMemory(
        id=id, content=content, project="testproj", scope=scope,
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:00+00:00",
    )


def test_semantic_panel_lists_from_backend_not_local_sqlite(
    client, tmp_db, monkeypatch
):
    """[[server-boot-real-call]] dead-content-table: a sentinel row in the
    LOCAL semantic_memories table must NEVER render; the panel content comes
    from the stubbed backend."""
    import sqlite3
    with sqlite3.connect(tmp_db) as seed:
        seed.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at) VALUES "
            "('local-sentinel','LOCAL SENTINEL ROW','testproj','project',"
            " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
        )
        seed.commit()
    stub = _SemanticStubBackend([_semantic_row(content="BACKEND ROW")])
    client.application.extensions["backend"] = stub
    body = client.get("/semantic/panel").get_data(as_text=True)
    assert "BACKEND ROW" in body
    assert "LOCAL SENTINEL ROW" not in body
    assert stub.calls and stub.calls[0][0] == "list"


def test_semantic_create_calls_backend_observe(client):
    stub = _SemanticStubBackend([])
    client.application.extensions["backend"] = stub
    resp = client.post("/semantic", data={"content": "new fact", "scope": "general"},
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "semantic-changed"
    assert ("observe", "new fact", "general") in stub.calls


def test_semantic_scope_and_delete_and_update_call_backend(client):
    stub = _SemanticStubBackend([])
    client.application.extensions["backend"] = stub
    h = {"Origin": "http://localhost"}
    client.post("/semantic/x1/scope", data={"scope": "general"}, headers=h)
    client.post("/semantic/x1/delete", headers=h)
    client.post("/semantic/x1/update", data={"content": "edited"}, headers=h)
    assert ("scope", "x1", "general") in stub.calls
    assert ("delete", "x1") in stub.calls
    assert ("update", "x1", "edited") in stub.calls


def test_semantic_drawer_reads_from_backend_not_local(client, tmp_db):
    import sqlite3
    with sqlite3.connect(tmp_db) as seed:
        seed.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at) VALUES "
            "('d1','LOCAL SENTINEL','testproj','project',"
            "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:00+00:00')"
        )
        seed.commit()
    class _Stub(_SemanticStubBackend):
        def semantic_get(self, *, id):
            self.calls.append(("get", id))
            return _semantic_row(id="d1", content="BACKEND DRAWER ROW")
    stub = _Stub([])
    client.application.extensions["backend"] = stub
    body = client.get("/semantic/d1/drawer").get_data(as_text=True)
    assert "BACKEND DRAWER ROW" in body
    assert "LOCAL SENTINEL" not in body
    assert ("get", "d1") in stub.calls


def test_semantic_drawer_404_when_backend_returns_none(client):
    class _Stub(_SemanticStubBackend):
        def semantic_get(self, *, id):
            return None
    client.application.extensions["backend"] = _Stub([])
    assert client.get("/semantic/nope/drawer").status_code == 404


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
