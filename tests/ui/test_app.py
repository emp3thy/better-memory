"""Unit tests for the UI Flask app."""

from __future__ import annotations

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
