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
            popen_spy = pytest.MonkeyPatch()
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
