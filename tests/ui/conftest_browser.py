"""Fixtures for Playwright-driven browser tests of the Management UI.

Spawns ``python -m better_memory.ui`` as a subprocess with an isolated
``BETTER_MEMORY_HOME``, waits for ``ui.url``, and yields the URL (plus
the tmp home dir so tests can seed data). Teardown terminates the
subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def ui_url(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Spawn the UI, apply migrations, yield (url, home_dir)."""
    db_path = tmp_path / "memory.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    env = {**os.environ, "BETTER_MEMORY_HOME": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "better_memory.ui"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url_path = tmp_path / "ui.url"

    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if url_path.exists():
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("UI did not write ui.url within 5s")

        url = url_path.read_text().strip()
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as resp:
            assert resp.status == 200

        yield url, tmp_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
