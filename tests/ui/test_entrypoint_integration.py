"""Integration tests for the UI entry point (`python -m better_memory.ui`).

Launches the real module as a subprocess, waits for ``ui.url`` to appear,
hits ``/healthz`` on the reported URL, then kills the subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def spawn_ui(tmp_path: Path):
    """Spawn the UI subprocess with an isolated BETTER_MEMORY_HOME.

    Yields (process, ui_url_path). Caller is responsible for asserting
    on the URL file; teardown terminates the process.
    """
    proc: subprocess.Popen | None = None

    def _spawn() -> tuple[subprocess.Popen, Path]:
        nonlocal proc
        env = {**os.environ, "BETTER_MEMORY_HOME": str(tmp_path)}
        # Discard child stdout/stderr — werkzeug prints one line per
        # request, and we don't want to risk pipe-buffer deadlocks on
        # platforms with small pipe sizes (notably Windows).
        proc = subprocess.Popen(
            [sys.executable, "-m", "better_memory.ui"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc, tmp_path / "ui.url"

    yield _spawn

    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{path} did not appear within {timeout}s")


class TestEntrypoint:
    def test_writes_ui_url_and_serves_healthz(self, spawn_ui) -> None:
        proc, url_path = spawn_ui()
        _wait_for_file(url_path, timeout=5.0)
        url = url_path.read_text().strip()
        assert url.startswith("http://127.0.0.1:")

        # The server is up by the time ui.url is written.
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as resp:
            assert resp.status == 200
            assert resp.read() == b"ok"

    def test_ui_url_deleted_on_clean_shutdown(self, spawn_ui) -> None:
        proc, url_path = spawn_ui()
        _wait_for_file(url_path, timeout=5.0)
        url = url_path.read_text().strip()

        # Hit /shutdown with a valid Origin — UI schedules os._exit.
        req = urllib.request.Request(
            f"{url}/shutdown",
            method="POST",
            headers={"Origin": url},
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            # os._exit races the response flush; connection reset is OK.
            pass

        # Subprocess should exit shortly.
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "UI did not exit after /shutdown"

        # ui.url should be gone (best-effort cleanup in __main__).
        # Allow a brief moment for the atexit hook to run.
        for _ in range(20):
            if not url_path.exists():
                break
            time.sleep(0.05)
        assert not url_path.exists()
