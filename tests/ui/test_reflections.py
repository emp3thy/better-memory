"""Flask test-client tests for the Reflections tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient


class TestReflectionsPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/reflections")
        assert response.status_code == 200

    def test_renders_filter_form(self, client: FlaskClient):
        response = client.get("/reflections")
        body = response.get_data(as_text=True)
        # Filter form fields from spec §8: project / tech / phase /
        # polarity / status / min confidence.
        assert 'name="project"' in body
        assert 'name="tech"' in body
        assert 'name="phase"' in body
        assert 'name="polarity"' in body
        assert 'name="status"' in body
        assert 'name="min_confidence"' in body
