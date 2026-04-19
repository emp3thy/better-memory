"""Unit tests for the UI Flask app."""

from __future__ import annotations

from unittest.mock import patch

from flask.testing import FlaskClient


class TestHealthz:
    def test_returns_200_with_ok_body(self, client: FlaskClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.data == b"ok"


class TestRootRedirect:
    def test_redirects_to_pipeline(self, client: FlaskClient) -> None:
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/pipeline")


class TestLayoutShell:
    def test_pipeline_renders_base_layout(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        assert response.status_code == 200
        body = response.data.decode()
        # All five nav tabs appear in the header
        assert "Pipeline" in body
        assert "Sweep" in body
        assert "Knowledge" in body
        assert "Audit" in body
        assert "Graph" in body
        # Close UI button is rendered
        assert "Close UI" in body


class TestEmptyViews:
    def test_sweep_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/sweep")
        assert response.status_code == 200
        assert b"Sweep Review" in response.data

    def test_knowledge_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/knowledge")
        assert response.status_code == 200
        assert b"Knowledge Base" in response.data

    def test_audit_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/audit")
        assert response.status_code == 200
        assert b"Audit Timeline" in response.data

    def test_graph_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/graph")
        assert response.status_code == 200
        assert b"<h1>Graph</h1>" in response.data


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
        response = client.get("/pipeline")
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
            import os as _os
            assert args[1] is _os._exit
            assert args[2] == (0,)
            mock_timer.return_value.start.assert_called_once()
