"""Unit tests for better_memory.services.ui_launcher."""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

from better_memory.services import ui_launcher

# --------------------------------------------------------------------------- helpers


def _free_port() -> int:
    """Bind ephemeral port, return it, immediately release."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _HealthOK(http.server.BaseHTTPRequestHandler):
    """Stub handler: GET /healthz → 200 'ok'; everything else → 404."""

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def log_message(self, *_a, **_kw) -> None:
        return  # silence


def _start_stub(handler_cls: type) -> tuple[str, threading.Thread, http.server.HTTPServer]:
    """Start handler_cls on a free port in a daemon thread. Return (url, thread, server)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t, server


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set BETTER_MEMORY_HOME to tmp_path; return the path."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- tests


class TestLiveness:
    def test_returns_existing_url_when_alive(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url, _t, server = _start_stub(_HealthOK)
        try:
            (home / "ui.url").write_text(url)

            # Popen must NOT be called when a live UI is found.
            calls: list = []

            def _fail(*a, **kw):
                calls.append((a, kw))
                raise AssertionError("Popen called when UI was alive")

            monkeypatch.setattr("subprocess.Popen", _fail)

            result = ui_launcher.start_ui()
            assert result == {"url": url, "reused": True}
            assert calls == []
        finally:
            server.shutdown()


# --------------------------------------------------------------------------- _FakePopen


class _FakePopen:
    """subprocess.Popen mock.

    Configurable behaviour:
      * write_url_after: float seconds — schedule writing the given URL into ui.url
      * url_to_write: str — the URL the fake subprocess "binds" to
    """

    instances: list[_FakePopen] = []

    def __init__(
        self,
        argv,
        *,
        stdin=None,
        stdout=None,
        stderr=None,
        close_fds=True,
        **kwargs,
    ) -> None:
        self.argv = list(argv)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.close_fds = close_fds
        self.kwargs = kwargs
        type(self).instances.append(self)

        plan = type(self)._next_plan
        if plan is None:
            return
        delay, url, home = plan
        type(self)._next_plan = None

        def _write_after_delay() -> None:
            import time as _time

            _time.sleep(delay)
            (home / "ui.url").write_text(url)

        threading.Thread(target=_write_after_delay, daemon=True).start()

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls._next_plan = None

    @classmethod
    def schedule_url_write(cls, *, after: float, url: str, home: Path) -> None:
        cls._next_plan = (after, url, home)


_FakePopen._next_plan = None  # type: ignore[attr-defined]


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch):
    _FakePopen.reset()
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    yield _FakePopen
    _FakePopen.reset()


# --------------------------------------------------------------------------- tests


class TestSpawn:
    def test_stale_url_file_replaced_by_fresh_spawn(
        self, home: Path, fake_popen
    ) -> None:
        """Stale ui.url is unlinked, then a fresh spawn writes a new URL."""
        # Pre-write a URL pointing at a port nothing is listening on.
        dead_port = _free_port()
        (home / "ui.url").write_text(f"http://127.0.0.1:{dead_port}")

        # Stub a healthy server on a different free port for the fresh spawn.
        new_url, _t, server = _start_stub(_HealthOK)
        try:
            fake_popen.schedule_url_write(after=0.025, url=new_url, home=home)

            result = ui_launcher.start_ui()

            assert result == {"url": new_url, "reused": False}
            # The eventual ui.url contents are the new URL (not the stale one).
            assert (home / "ui.url").read_text().strip() == new_url
            assert len(fake_popen.instances) == 1
        finally:
            server.shutdown()

    def test_spawns_when_no_url_file(
        self, home: Path, fake_popen
    ) -> None:
        url, _t, server = _start_stub(_HealthOK)
        try:
            # 25 ms — under the 50 ms poll interval so the test never races.
            fake_popen.schedule_url_write(after=0.025, url=url, home=home)

            result = ui_launcher.start_ui()

            assert result == {"url": url, "reused": False}
            assert len(fake_popen.instances) == 1
            inst = fake_popen.instances[0]
            # Argv: [sys.executable, "-m", "better_memory.ui"]
            import sys

            assert inst.argv[0] == sys.executable
            assert inst.argv[1:] == ["-m", "better_memory.ui"]
        finally:
            server.shutdown()

    def test_spawn_timeout_raises_when_url_never_appears(
        self, home: Path, fake_popen
    ) -> None:
        """No URL is ever written -> RuntimeError after the injected timeout."""
        # Do NOT schedule_url_write — the fake subprocess writes nothing.
        with pytest.raises(RuntimeError, match=r"ui\.url"):
            ui_launcher.start_ui(spawn_timeout=1.0)

        assert len(fake_popen.instances) == 1

    def test_url_appears_but_healthz_fails_raises(
        self, home: Path, fake_popen
    ) -> None:
        """ui.url written, but the URL never serves /healthz -> RuntimeError."""

        class _NotFound(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_error(404)

            def log_message(self, *_a, **_kw) -> None:
                return

        bad_url, _t, server = _start_stub(_NotFound)
        try:
            fake_popen.schedule_url_write(after=0.025, url=bad_url, home=home)

            with pytest.raises(RuntimeError, match=r"/healthz"):
                ui_launcher.start_ui(
                    spawn_timeout=2.0,
                    confirm_retry_sleep=0.05,
                )
        finally:
            server.shutdown()
