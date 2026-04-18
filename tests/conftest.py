"""Shared pytest fixtures for the better-memory test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_memory_db(tmp_path: Path) -> Iterator[Path]:
    """Yield a path to a fresh (non-existent) SQLite database file.

    The file itself is not created; callers are responsible for opening /
    initialising the database. ``tmp_path`` cleanup removes any file the test
    creates.
    """
    yield tmp_path / "memory.db"


@pytest.fixture
def tmp_knowledge_base(tmp_path: Path) -> Iterator[Path]:
    """Yield a path to an empty temporary directory for knowledge-base files."""
    kb = tmp_path / "knowledge-base"
    kb.mkdir()
    yield kb
