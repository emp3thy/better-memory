"""Shared fixtures for UI tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient

from better_memory.ui.app import create_app


@pytest.fixture
def client() -> Iterator[FlaskClient]:
    """Yield a Flask test client for a freshly created app."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
