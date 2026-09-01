"""Shared fixtures for UI tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
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
def client(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FlaskClient]:
    """Yield a Flask test client backed by a migrated tmp DB.

    Patches ``threading.Timer`` for the lifetime of the fixture so
    ``TestOriginCheck`` POST-to-/shutdown tests don't fire the real
    100 ms timer that calls ``os._exit`` and kills the pytest process.

    Task 2 (remove-ollama-embeddings) deleted create_app's sync_embedder
    auto-detection entirely — every write-path service now receives
    ``sync_embedder=None`` unconditionally, so no env pin is needed to
    keep route tests from depending on a live Ollama instance.
    """
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c


class _FakeAgentCoreBackend:
    """Flags-all-False stand-in for AgentCoreBackend used by gating tests.

    Only the content methods the *still-reachable* agentcore routes call
    are stubbed; gated-off routes 404 before touching the backend.
    """

    supports_episodes = False
    supports_observations = False
    supports_provenance = False
    supports_retention_runs = False
    supports_reflection_review = False
    supports_reflection_text_edit = False

    def reflection_list(self, **_kwargs: Any) -> list[Any]:
        return []

    def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
        return None

    def semantic_list(self, **_kwargs: Any) -> list[Any]:
        return []

    def distinct_projects(self) -> list[str]:
        return []


@pytest.fixture
def agentcore_client(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FlaskClient]:
    """Flask client whose ``app.extensions['backend']`` is the all-False
    fake, so every ``caps.*`` gate reads False and every route guard fires.

    The context processor reads ``caps`` off ``app.extensions['backend']``
    at render time (PR 2 wiring), so swapping the extension flips the gates
    without a live boto/factory build.
    """
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    app.extensions["backend"] = _FakeAgentCoreBackend()
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c
