"""Unit tests for the UI Flask app."""

from __future__ import annotations

import threading
import time as _time
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from better_memory.ui.app import create_app


class TestServiceWiring:
    def test_app_exposes_open_db_connection(
        self, tmp_db: Path
    ) -> None:
        app = create_app(start_watchdog=False, db_path=tmp_db)
        conn = app.extensions["db_connection"]
        # Connection is open and usable against the migrated schema.
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        assert row[0] == 0


class TestSyncEmbedderWiring:
    """UI-driven writes must embed like MCP-driven writes.

    Before this wiring, better_memory.ui.app constructed ReflectionService
    and SemanticMemoryService with no sync_embedder at all, so UI edits/
    creates never populated reflection_embeddings / semantic_embeddings —
    only a manual CLI backfill run would fix those rows up. create_app now
    auto-builds a shared SyncEmbedder from get_config().embeddings_backend
    (mirroring better_memory/mcp/server.py and better_memory/storage/
    sqlite.py), and accepts an explicit sync_embedder= override for tests.
    """

    def test_sync_embedder_none_when_backend_sqlite(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        app = create_app(start_watchdog=False, db_path=tmp_db)
        assert app.extensions["sync_embedder"] is None

    def test_sync_embedder_built_when_backend_ollama(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from better_memory.embeddings.sync_embed import SyncEmbedder

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "ollama")
        app = create_app(start_watchdog=False, db_path=tmp_db)
        assert isinstance(app.extensions["sync_embedder"], SyncEmbedder)

    def test_explicit_sync_embedder_overrides_config_auto_detect(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from better_memory.embeddings.sync_embed import SyncEmbedder

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        fake_sync_embedder = SyncEmbedder(lambda: None)
        app = create_app(
            start_watchdog=False, db_path=tmp_db,
            sync_embedder=fake_sync_embedder,
        )
        assert app.extensions["sync_embedder"] is fake_sync_embedder

    def test_reflection_service_receives_the_shared_sync_embedder(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from better_memory.embeddings.sync_embed import SyncEmbedder

        fake_sync_embedder = SyncEmbedder(lambda: None)
        app = create_app(
            start_watchdog=False, db_path=tmp_db,
            sync_embedder=fake_sync_embedder,
        )
        assert (
            app.extensions["reflection_service"]._sync_embedder
            is fake_sync_embedder
        )


class TestHealthz:
    def test_returns_200_with_ok_body(self, client: FlaskClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.data == b"ok"


class TestRootRedirect:
    def test_redirects_to_episodes(self, client: FlaskClient) -> None:
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/episodes")



class TestNav:
    def test_nav_shows_episodes_and_reflections(self, client: FlaskClient) -> None:
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert ">Episodes<" in body
        assert ">Reflections<" in body

    def test_nav_hides_old_tabs(self, client: FlaskClient) -> None:
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        for label in ("Pipeline", "Sweep", "Knowledge", "Audit", "Graph"):
            assert f">{label}<" not in body


class TestEpisodesGate:
    _RAIL_LINK = '<span class="rail-label">Episodes</span>'

    def test_episodes_link_present_in_sqlite_mode(
        self, client: FlaskClient
    ) -> None:
        # sqlite backend -> supports_episodes True -> link renders as today.
        body = client.get("/episodes").get_data(as_text=True)
        assert self._RAIL_LINK in body

    def test_episodes_link_hidden_when_flag_false(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [[guard-needs-triggering-test]]: seed supports_episodes=False to
        # trigger the {% if %} false branch. [[playwright-domtext]]: assert
        # on nav-element markup presence/absence, not CSS visibility.
        from unittest.mock import MagicMock

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        StubBackend = type("StubBackend", (), {})
        stub = StubBackend()
        # Only Episodes is gated this phase; the other five stay True so
        # the rest of the rail renders normally.
        setattr(type(stub), "supports_episodes", property(lambda self: False))
        for name in (
            "supports_observations", "supports_provenance",
            "supports_retention_runs", "supports_reflection_review",
            "supports_reflection_text_edit",
        ):
            setattr(type(stub), name, property(lambda self: True))
        monkeypatch.setattr(
            "better_memory.ui.app.build_backend",
            MagicMock(return_value=stub),
        )
        app = create_app(start_watchdog=False, db_path=tmp_db)
        app.config["TESTING"] = True
        with app.test_client() as c:
            body = c.get("/episodes").get_data(as_text=True)
        assert self._RAIL_LINK not in body
        # Sibling links unaffected -- prove only Episodes was gated.
        assert '<span class="rail-label">Reflections</span>' in body


class TestOriginCheck:
    def test_post_without_origin_or_referer_is_rejected(
        self, client: FlaskClient
    ) -> None:
        response = client.post("/shutdown")
        assert response.status_code == 403

    def test_post_with_matching_origin_is_accepted(
        self, client: FlaskClient
    ) -> None:
        # Flask test client "serves" on http://localhost (no port) —
        # SERVER_NAME is localhost by default.
        response = client.post(
            "/shutdown",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 204

    def test_post_with_matching_referer_is_accepted(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/shutdown",
            headers={"Referer": "http://localhost/pipeline"},
        )
        assert response.status_code == 204

    def test_post_with_foreign_origin_is_rejected(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/shutdown",
            headers={"Origin": "http://evil.example.com"},
        )
        assert response.status_code == 403

    def test_get_without_origin_is_allowed(self, client: FlaskClient) -> None:
        response = client.get("/episodes")
        assert response.status_code == 200

    def test_head_without_origin_is_allowed(self, client: FlaskClient) -> None:
        response = client.head("/episodes")
        assert response.status_code == 200


class TestStaticAssets:
    def test_htmx_js_is_served(self, client: FlaskClient) -> None:
        response = client.get("/static/htmx.min.js")
        assert response.status_code == 200
        assert response.content_type.startswith("application/javascript") or \
               response.content_type.startswith("text/javascript")
        # HTMX's minified bundle begins with a standard UMD-ish header;
        # assert something from the real file rather than an exact hash.
        assert b"htmx" in response.data.lower()

    def test_app_css_is_served(self, client: FlaskClient) -> None:
        response = client.get("/static/app.css")
        assert response.status_code == 200
        assert response.content_type.startswith("text/css")
        assert b".rail" in response.data


class TestShutdown:
    def test_shutdown_schedules_exit_via_timer(
        self, client: FlaskClient
    ) -> None:
        with patch("better_memory.ui.app.threading.Timer") as mock_timer:
            response = client.post(
                "/shutdown", headers={"Origin": "http://localhost"}
            )
            assert response.status_code == 204
            mock_timer.assert_called_once()
            args, _ = mock_timer.call_args
            assert args[0] == 0.1
            assert callable(args[1])
            mock_timer.return_value.start.assert_called_once()


class TestInactivityTimeout:
    def test_request_resets_last_activity(self, tmp_path: Path) -> None:
        # start_watchdog=False: a live watchdog thread from this app would
        # see the fabricated _last_activity and os._exit(0) the whole pytest
        # process one poll interval later.
        app = create_app(start_watchdog=False, db_path=tmp_path / "memory.db")
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0  # pretend ancient
            c.get("/episodes")
            # After the request, _last_activity should be ~now.
            assert _time.monotonic() - app.config["_last_activity"] < 0.1

    def test_healthz_does_not_reset_last_activity(self, tmp_path: Path) -> None:
        # start_watchdog=False: this test leaves _last_activity at 0.0, which
        # a real watchdog thread reads as "idle since boot" (time.monotonic()
        # epoch) and os._exit(0)s the pytest process ~30s later — a silent
        # exit-0 suite kill.
        app = create_app(start_watchdog=False, db_path=tmp_path / "memory.db")
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0
            c.get("/healthz")
            # /healthz must not update _last_activity
            assert app.config["_last_activity"] == 0.0

    def test_check_idle_exits_when_over_threshold(self, tmp_path: Path) -> None:
        # start_watchdog=False: _check_idle is driven synchronously below; a
        # live watchdog would see the fabricated 120s idle and os._exit(0)
        # the pytest process for real.
        app = create_app(
            inactivity_timeout=60.0, start_watchdog=False,
            db_path=tmp_path / "memory.db",
        )
        app.config["_last_activity"] = _time.monotonic() - 120.0  # 2 min idle
        with patch("better_memory.ui.app.resolve_home", return_value=tmp_path), \
             patch("better_memory.ui.app.os._exit") as mock_exit:
            app.config["_check_idle"]()
            mock_exit.assert_called_once_with(0)

    def test_check_idle_noop_when_under_threshold(self, tmp_path: Path) -> None:
        # start_watchdog=False: with a 60s timeout, a live watchdog from this
        # app would os._exit(0) the pytest process ~60s into the rest of the
        # suite run.
        app = create_app(
            inactivity_timeout=60.0, start_watchdog=False,
            db_path=tmp_path / "memory.db",
        )
        app.config["_last_activity"] = _time.monotonic()  # just now
        with patch("better_memory.ui.app.os._exit") as mock_exit:
            app.config["_check_idle"]()
            mock_exit.assert_not_called()

    def test_watchdog_thread_started_by_default(self, tmp_path: Path) -> None:
        before = sum(1 for t in threading.enumerate() if t.name == "ui-watchdog")
        create_app(db_path=tmp_path / "memory.db")
        after = sum(1 for t in threading.enumerate() if t.name == "ui-watchdog")
        assert after == before + 1

    def test_watchdog_thread_skipped_when_disabled(self, tmp_path: Path) -> None:
        # Tests that don't want the thread can pass start_watchdog=False.
        app = create_app(start_watchdog=False, db_path=tmp_path / "memory.db")
        assert app.config["_check_idle"]  # helper still registered


class TestBackendWiring:
    def test_create_app_builds_sqlite_backend_and_retains_conn(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from better_memory.storage.sqlite import SqliteBackend

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        app = create_app(start_watchdog=False, db_path=tmp_db)
        backend = app.extensions["backend"]
        assert isinstance(backend, SqliteBackend)
        # Operational conn retained and still usable.
        conn = app.extensions["db_connection"]
        assert conn.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0] == 0
        # Sqlite path shares the single connection -- no second store.
        assert backend._conn is conn

    def test_build_backend_called_with_canonical_kwargs(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        from better_memory.config import project_name

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        stub = MagicMock(name="stub-backend")
        spy = MagicMock(return_value=stub)
        monkeypatch.setattr("better_memory.ui.app.build_backend", spy)
        app = create_app(start_watchdog=False, db_path=tmp_db)
        assert app.extensions["backend"] is stub
        spy.assert_called_once()
        _, kwargs = spy.call_args
        assert kwargs["memory_conn"] is app.extensions["db_connection"]
        assert kwargs["sync_embedder"] is app.extensions["sync_embedder"]
        assert kwargs["session_id"] is None
        assert kwargs["project"] == project_name()
        assert "config" in kwargs  # get_config() forwarded to the factory

    def test_caps_read_from_backend_on_a_real_route(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # [[server-boot-real-call]]: drive an ACTUAL route through a stubbed
        # agentcore backend and prove the six caps were sourced FROM the
        # backend during the render -- no leaked local-content read.
        from unittest.mock import MagicMock, PropertyMock

        monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
        # Fresh throwaway type per test so PropertyMocks don't leak.
        StubBackend = type("StubBackend", (), {})
        stub = StubBackend()
        props: dict[str, PropertyMock] = {}
        for name in (
            "supports_episodes", "supports_observations",
            "supports_provenance", "supports_retention_runs",
            "supports_reflection_review", "supports_reflection_text_edit",
        ):
            p = PropertyMock(return_value=False)
            setattr(StubBackend, name, p)
            props[name] = p
        monkeypatch.setattr(
            "better_memory.ui.app.build_backend",
            MagicMock(return_value=stub),
        )
        app = create_app(start_watchdog=False, db_path=tmp_db)
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.get("/episodes")
        assert resp.status_code == 200
        # Every cap was read off the backend object during the real render.
        for name, p in props.items():
            p.assert_called()

