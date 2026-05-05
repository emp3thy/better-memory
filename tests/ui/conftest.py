"""Shared fixtures for UI tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[Path]:
    """Yield a fresh migrated memory.db path in an isolated tmp dir."""
    db_path = tmp_path / "memory.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    yield db_path


@pytest.fixture
def client(tmp_db: Path) -> Iterator[FlaskClient]:
    """Yield a Flask test client backed by a migrated tmp DB.

    Patches ``threading.Timer`` for the lifetime of the fixture so
    ``TestOriginCheck`` POST-to-/shutdown tests don't fire the real
    100 ms timer that calls ``os._exit`` and kills the pytest process.
    """
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c
