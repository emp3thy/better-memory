"""Unit tests for the UI Flask app."""

from __future__ import annotations

from flask.testing import FlaskClient


class TestHealthz:
    def test_returns_200_with_ok_body(self, client: FlaskClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.data == b"ok"
