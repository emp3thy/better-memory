"""Shared fixtures for UI tests."""

from __future__ import annotations

import time
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


@pytest.fixture(autouse=True)
def _synth_busy_isolation():
    """Reset the module-level _synth_busy flag between UI tests.

    The /observations/synthesize route uses a process-wide busy flag
    to refuse concurrent calls (returns 429). The 504-on-timeout test
    leaves a daemon worker thread running for ~1.5s after the route
    returns; without this fixture, a fast next test could observe the
    stranded flag.

    Strategy:
    - Before yield: wait up to 3 seconds for any in-flight worker to
      release the flag naturally; then force-reset.
    - After yield: force-reset again so leakage from the current test
      doesn't pollute the next.
    """
    from better_memory.ui import app as _app_module

    deadline = time.monotonic() + 3.0
    while _app_module._synth_busy and time.monotonic() < deadline:
        time.sleep(0.05)
    _app_module._synth_busy = False
    yield
    _app_module._synth_busy = False
