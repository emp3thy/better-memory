"""Unit tests for the UI Flask app."""

from __future__ import annotations

import sqlite3
import threading
import time as _time
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from better_memory.ui.app import create_app


@pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
class TestServiceWiring:
    def test_app_exposes_insight_service(self, client: FlaskClient) -> None:
        # The service is attached to app.extensions for routes to use.
        assert "insight_service" in client.application.extensions

    def test_app_exposes_open_db_connection(
        self, tmp_db: Path
    ) -> None:
        app = create_app(start_watchdog=False, db_path=tmp_db)
        conn = app.extensions["db_connection"]
        # Connection is open and usable against the migrated schema.
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        assert row[0] == 0


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

    @pytest.mark.skip(
        reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
    )
    def test_get_without_origin_is_allowed(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        assert response.status_code == 200

    @pytest.mark.skip(
        reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
    )
    def test_head_without_origin_is_allowed(self, client: FlaskClient) -> None:
        response = client.head("/pipeline")
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
        assert b".app-header" in response.data


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
        app = create_app(db_path=tmp_path / "memory.db")
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0  # pretend ancient
            c.get("/episodes")
            # After the request, _last_activity should be ~now.
            assert _time.monotonic() - app.config["_last_activity"] < 0.1

    def test_healthz_does_not_reset_last_activity(self, tmp_path: Path) -> None:
        app = create_app(db_path=tmp_path / "memory.db")
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0
            c.get("/healthz")
            # /healthz must not update _last_activity
            assert app.config["_last_activity"] == 0.0

    def test_check_idle_exits_when_over_threshold(self, tmp_path: Path) -> None:
        app = create_app(inactivity_timeout=60.0, db_path=tmp_path / "memory.db")
        app.config["_last_activity"] = _time.monotonic() - 120.0  # 2 min idle
        with patch("better_memory.ui.app.resolve_home", return_value=tmp_path), \
             patch("better_memory.ui.app.os._exit") as mock_exit:
            app.config["_check_idle"]()
            mock_exit.assert_called_once_with(0)

    def test_check_idle_noop_when_under_threshold(self, tmp_path: Path) -> None:
        app = create_app(inactivity_timeout=60.0, db_path=tmp_path / "memory.db")
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


@pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
class TestBadgeFragment:
    def test_badge_empty_when_zero(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/badge")
        assert response.status_code == 200
        assert response.content_type.startswith("text/html")
        # Phase 1: always zero ⇒ CSS hides the badge ⇒ fragment is empty.
        assert response.data.strip() == b""

    def test_badge_template_renders_number_when_positive(
        self, client: FlaskClient
    ) -> None:
        # Render the template directly with a non-zero count, proving
        # the Phase-2-ready code path works without needing to stub the
        # view or mock the DB.
        from flask import render_template

        with client.application.app_context():
            out = render_template("fragments/badge.html", count=7)
            assert out == "7"


@pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
class TestBadgeRealCount:
    def test_badge_shows_candidate_count_from_db(
        self, client: FlaskClient
    ) -> None:
        # Insert candidates directly via the app's connection so the
        # project name matches cwd (same as the kanban query).
        conn: sqlite3.Connection = client.application.extensions["db_connection"]
        project = Path.cwd().name
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c1', 't', 'c', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('c2', 't', 'c', ?, 'pending_review', 'neutral')",
            (project,),
        )
        conn.execute(
            "INSERT INTO insights (id, title, content, project, status, polarity) "
            "VALUES ('x', 't', 'c', ?, 'confirmed', 'neutral')",
            (project,),
        )
        conn.commit()

        response = client.get("/pipeline/badge")
        assert response.status_code == 200
        assert response.data.strip() == b"2"


class TestOnlyOneExpandedScript:
    @pytest.mark.skip(
        reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
    )
    def test_base_includes_only_one_expanded_listener(
        self, client: FlaskClient
    ) -> None:
        response = client.get("/pipeline")
        body = response.data
        # Script must listen for the HTMX event that fires before any
        # request and walk the .card-list for expanded siblings.
        assert b"htmx:beforeRequest" in body
        assert b"card-compact" in body
        assert b"data-expanded" in body
        assert b"collapse-me" in body
        # Modal target div exists for promote / merge.
        assert b'id="modal"' in body


@pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)
class TestConsolidationWiring:
    def test_app_exposes_db_path_for_threaded_jobs(
        self, client: FlaskClient
    ) -> None:
        assert "_db_path" in client.application.extensions
