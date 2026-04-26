# `memory.start_ui` MCP Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stubbed `memory.start_ui` MCP handler with a working implementation. Logic lives in a new `better_memory/services/ui_launcher.py` service module that spawns the existing `better_memory.ui` Flask app as a detached subprocess, returns the bound URL, and reuses an existing live UI when one is already running on `/healthz`.

**Architecture:** Handler-thin / service-fat split (mirrors `episodes`, `reflections`, `observations`). The MCP handler is a 3-line passthrough; the service module owns all spawn, liveness, and stale-cleanup logic. Liveness is detected via HTTP `GET /healthz` against the URL recorded in `$BETTER_MEMORY_HOME/ui.url` — no PID file. Subprocess detach uses platform-specific kwargs (`start_new_session=True` on POSIX, `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows). Stdout/stderr go to `$BETTER_MEMORY_HOME/ui.log` for debuggability.

**Tech Stack:** Python 3.12 stdlib only (`subprocess`, `urllib.request`, `pathlib`, `threading`). No new dependencies. `pytest` + `unittest.mock` for tests; `http.server` from stdlib for stub servers.

**Spec:** `docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`

---

## File Structure

### Create

```
better_memory/services/ui_launcher.py    # spawn/liveness/cleanup service
tests/services/test_ui_launcher.py       # service unit tests
tests/mcp/test_start_ui_tool.py          # MCP-handler integration test
```

### Modify

- `better_memory/mcp/server.py` — replace stub handler with passthrough; update Tool description; update module docstring at lines 16 and 29-31.
- `better_memory/services/audit.py` — drop the "stub today" wording at line 19.
- `README.md` — update `memory.start_ui()` row in the tool table (line 121); update Management UI section (lines 176-188).

---

## Task 1: Service skeleton — return existing URL when UI is alive

**Files:**
- Create: `better_memory/services/ui_launcher.py`
- Create: `tests/services/test_ui_launcher.py`

This task lays down the module + first test fixtures and implements only the happy path: a live UI is running, `ui.url` points at it, `/healthz` returns 200, `start_ui()` returns `{"url": ..., "reused": True}` without spawning anything.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_ui_launcher.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestLiveness::test_returns_existing_url_when_alive -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'better_memory.services.ui_launcher'`.

- [ ] **Step 3: Create the service module with minimal liveness path**

Create `better_memory/services/ui_launcher.py`:

```python
"""Spawn / reuse the better-memory management UI as a detached subprocess.

The MCP handler ``memory.start_ui`` is a thin passthrough to ``start_ui()``.
This module owns:

* Liveness detection via HTTP GET against ``$BETTER_MEMORY_HOME/ui.url``.
* Stale ``ui.url`` cleanup when the recorded URL no longer responds.
* Detached subprocess spawn (``python -m better_memory.ui``) with
  platform-specific detach flags so the UI survives MCP server termination.
* Stdout/stderr capture to ``$BETTER_MEMORY_HOME/ui.log``.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from better_memory.config import resolve_home

_HEALTHZ_TIMEOUT_SEC = 1.0


def _is_alive(url: str) -> bool:
    """Return True iff GET <url>/healthz returns 200 within the timeout."""
    probe = url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(  # noqa: S310 — local-only loopback URL
            probe, timeout=_HEALTHZ_TIMEOUT_SEC
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_ui() -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.
    """
    home = resolve_home()
    url_path = home / "ui.url"

    if url_path.exists():
        try:
            url = url_path.read_text().strip()
        except OSError:
            url = ""
        if url and _is_alive(url):
            return {"url": url, "reused": True}

    raise NotImplementedError("spawn path lands in Task 3")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestLiveness::test_returns_existing_url_when_alive -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/ui_launcher.py tests/services/test_ui_launcher.py
git commit -m "feat(ui_launcher): liveness check returns existing URL when /healthz responds"
```

---

## Task 2: Unlink stale `ui.url` when `/healthz` does not respond

**Files:**
- Modify: `better_memory/services/ui_launcher.py`
- Modify: `tests/services/test_ui_launcher.py`

When `ui.url` points at a port that no longer answers (e.g. the previous UI process exited but its `atexit` cleanup didn't fire — possible if killed by signal), the launcher must unlink the stale file before falling through to the spawn path. This task adds that branch and asserts unlink occurs. The spawn path itself is still the placeholder `NotImplementedError` from Task 1; we'll wire it in Task 3.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_ui_launcher.py`:

```python
    def test_stale_url_file_unlinked_when_unresponsive(
        self, home: Path
    ) -> None:
        # Record a URL pointing at a port nothing is listening on.
        dead_port = _free_port()
        (home / "ui.url").write_text(f"http://127.0.0.1:{dead_port}")

        # The spawn path is not implemented yet; we expect NotImplementedError
        # only AFTER the stale file is unlinked.
        with pytest.raises(NotImplementedError):
            ui_launcher.start_ui()

        assert not (home / "ui.url").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestLiveness::test_stale_url_file_unlinked_when_unresponsive -v`
Expected: FAIL — `assert not (home / "ui.url").exists()` fails because the current implementation does not unlink.

- [ ] **Step 3: Add stale-cleanup branch**

Edit `better_memory/services/ui_launcher.py`. Replace the body of `start_ui()` with:

```python
def start_ui() -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.
    """
    home = resolve_home()
    url_path = home / "ui.url"

    if url_path.exists():
        try:
            url = url_path.read_text().strip()
        except OSError:
            url = ""
        if url and _is_alive(url):
            return {"url": url, "reused": True}
        # File present but URL is stale (or unreadable). Unlink so the
        # spawn path can write a fresh one.
        try:
            url_path.unlink()
        except FileNotFoundError:
            pass

    raise NotImplementedError("spawn path lands in Task 3")
```

- [ ] **Step 4: Run both liveness tests**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestLiveness -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/ui_launcher.py tests/services/test_ui_launcher.py
git commit -m "feat(ui_launcher): unlink stale ui.url when /healthz does not respond"
```

---

## Task 3: Spawn detached subprocess + wait for `ui.url`

**Files:**
- Modify: `better_memory/services/ui_launcher.py`
- Modify: `tests/services/test_ui_launcher.py`

This task implements the spawn path: detached subprocess with platform-correct kwargs, stdout/stderr to `ui.log`, polling for `ui.url` with a configurable timeout, and a final `/healthz` confirmation. After this task `start_ui()` is functionally complete; subsequent tasks add edge-case tests and harden error paths.

The `_FakePopen` helper introduced here simulates a subprocess that writes `ui.url` after a configurable delay.

- [ ] **Step 1: Write the failing test for spawn-success**

Append to `tests/services/test_ui_launcher.py`. First add a new test class with shared `_FakePopen` infrastructure:

```python
class _FakePopen:
    """subprocess.Popen mock.

    Configurable behaviour:
      * write_url_after: float seconds — schedule writing the given URL into ui.url
      * url_to_write: str — the URL the fake subprocess "binds" to
    """

    instances: list["_FakePopen"] = []

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


class TestSpawn:
    def test_spawns_when_no_url_file(
        self, home: Path, fake_popen
    ) -> None:
        url, _t, server = _start_stub(_HealthOK)
        try:
            fake_popen.schedule_url_write(after=0.05, url=url, home=home)

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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestSpawn::test_spawns_when_no_url_file -v`
Expected: FAIL with `NotImplementedError: spawn path lands in Task 3`.

- [ ] **Step 3: Implement the spawn path**

Replace `better_memory/services/ui_launcher.py` in full with:

```python
"""Spawn / reuse the better-memory management UI as a detached subprocess.

The MCP handler ``memory.start_ui`` is a thin passthrough to ``start_ui()``.
This module owns:

* Liveness detection via HTTP GET against ``$BETTER_MEMORY_HOME/ui.url``.
* Stale ``ui.url`` cleanup when the recorded URL no longer responds.
* Detached subprocess spawn (``python -m better_memory.ui``) with
  platform-specific detach flags so the UI survives MCP server termination.
* Stdout/stderr capture to ``$BETTER_MEMORY_HOME/ui.log``.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from better_memory.config import resolve_home

_HEALTHZ_TIMEOUT_SEC = 1.0
_DEFAULT_SPAWN_TIMEOUT_SEC = 10.0
_POLL_INTERVAL_SEC = 0.1


def _is_alive(url: str) -> bool:
    """Return True iff GET <url>/healthz returns 200 within the timeout."""
    probe = url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(  # noqa: S310 — local-only loopback URL
            probe, timeout=_HEALTHZ_TIMEOUT_SEC
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _detach_kwargs() -> dict:
    """Platform-specific Popen kwargs that detach the child from the parent."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True}


def _spawn(home: Path) -> None:
    """Spawn the UI subprocess. Stdout/stderr go to ui.log."""
    log_path = home / "ui.log"
    try:
        log_fh = log_path.open("ab")
    except OSError as exc:
        raise RuntimeError(
            f"cannot write to BETTER_MEMORY_HOME ({home}): {exc}"
        ) from exc

    try:
        subprocess.Popen(
            [sys.executable, "-m", "better_memory.ui"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            close_fds=True,
            **_detach_kwargs(),
        )
    except OSError as exc:
        log_fh.close()
        raise RuntimeError(f"failed to spawn UI subprocess: {exc}") from exc


def _wait_for_url(url_path: Path, timeout: float) -> str:
    """Poll url_path until it appears or the deadline is hit. Return its content."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if url_path.exists():
            try:
                return url_path.read_text().strip()
            except OSError:
                pass
        time.sleep(_POLL_INTERVAL_SEC)
    raise RuntimeError(
        f"UI did not write ui.url within {timeout}s; check ui.log"
    )


def start_ui(*, spawn_timeout: float = _DEFAULT_SPAWN_TIMEOUT_SEC) -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.

    ``spawn_timeout`` is exposed as a parameter so tests can short-circuit
    the 10 s default.
    """
    home = resolve_home()
    url_path = home / "ui.url"

    if url_path.exists():
        try:
            url = url_path.read_text().strip()
        except OSError:
            url = ""
        if url and _is_alive(url):
            return {"url": url, "reused": True}
        try:
            url_path.unlink()
        except FileNotFoundError:
            pass

    _spawn(home)
    url = _wait_for_url(url_path, timeout=spawn_timeout)
    if not _is_alive(url):
        # One short retry to absorb the gap between "url file written" and
        # "Werkzeug accepts connections".
        time.sleep(_HEALTHZ_TIMEOUT_SEC)
        if not _is_alive(url):
            raise RuntimeError(
                "UI wrote ui.url but /healthz did not respond"
            )
    return {"url": url, "reused": False}
```

- [ ] **Step 4: Run all ui_launcher tests**

Run: `uv run pytest tests/services/test_ui_launcher.py -v`
Expected: all three tests PASS (the two from Tasks 1-2 plus `test_spawns_when_no_url_file`).

- [ ] **Step 5: Commit**

```bash
git add better_memory/services/ui_launcher.py tests/services/test_ui_launcher.py
git commit -m "feat(ui_launcher): spawn detached UI subprocess and wait for ui.url"
```

---

## Task 4: Stale-cleanup test now drives a real spawn (regression coverage)

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

The Task 2 test asserted `NotImplementedError` after the unlink. Now that the spawn path exists, it would pass without verifying anything new. Replace the assertion with the real flow: stale file is unlinked AND a fresh spawn happens AND we get back `reused=False`.

- [ ] **Step 1: Update the existing test**

Edit `tests/services/test_ui_launcher.py`. Replace `test_stale_url_file_unlinked_when_unresponsive` with:

```python
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
            fake_popen.schedule_url_write(after=0.05, url=new_url, home=home)

            result = ui_launcher.start_ui()

            assert result == {"url": new_url, "reused": False}
            # The eventual ui.url contents are the new URL (not the stale one).
            assert (home / "ui.url").read_text().strip() == new_url
            assert len(fake_popen.instances) == 1
        finally:
            server.shutdown()
```

The `fake_popen` fixture in this test class injects `_FakePopen` from Task 3; since `TestLiveness` and `TestSpawn` are different classes, also add the `fake_popen` fixture to `TestLiveness` by moving the test into `TestSpawn` (it now exercises a spawn path). Move the test:

1. Cut the entire `test_stale_url_file_unlinked_when_unresponsive` method out of `TestLiveness`.
2. Paste it inside `TestSpawn` with the new name and body shown above.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/services/test_ui_launcher.py -v`
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): stale ui.url drives a real fresh spawn"
```

---

## Task 5: Spawn timeout raises `RuntimeError`

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

When the subprocess never writes `ui.url`, `_wait_for_url` must raise after the timeout. The implementation already does this; this task adds explicit coverage with a 1 s injected timeout so the test runs fast.

- [ ] **Step 1: Write the failing test**

Append to `TestSpawn` in `tests/services/test_ui_launcher.py`:

```python
    def test_spawn_timeout_raises_when_url_never_appears(
        self, home: Path, fake_popen
    ) -> None:
        """No URL is ever written -> RuntimeError after the injected timeout."""
        # Do NOT schedule_url_write — the fake subprocess writes nothing.
        with pytest.raises(RuntimeError, match=r"ui\.url"):
            ui_launcher.start_ui(spawn_timeout=1.0)

        assert len(fake_popen.instances) == 1
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestSpawn::test_spawn_timeout_raises_when_url_never_appears -v`
Expected: PASS (the implementation from Task 3 already handles this; the test pins the contract).

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): spawn timeout raises RuntimeError mentioning ui.url"
```

---

## Task 6: `/healthz` failure after spawn raises `RuntimeError`

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

When the subprocess writes `ui.url` but the URL doesn't answer `/healthz`, `start_ui` must retry once after a 1 s pause and then raise. Cover both legs: full failure, and "fails first then succeeds".

- [ ] **Step 1: Write the failing test (full failure)**

Append to `TestSpawn`:

```python
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
            fake_popen.schedule_url_write(after=0.05, url=bad_url, home=home)

            with pytest.raises(RuntimeError, match=r"/healthz"):
                ui_launcher.start_ui(spawn_timeout=2.0)
        finally:
            server.shutdown()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestSpawn::test_url_appears_but_healthz_fails_raises -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): /healthz failure after spawn raises RuntimeError"
```

---

## Task 7: Corrupt / empty `ui.url` is treated as missing

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

If `ui.url` exists but is empty or contains garbage, the launcher must treat it as a stale file: unlink and spawn fresh. The implementation already handles empty content (the `if url and _is_alive(url)` guard); this test pins the contract.

- [ ] **Step 1: Write the failing test**

Append to `TestSpawn`:

```python
    def test_corrupt_ui_url_treated_as_missing(
        self, home: Path, fake_popen
    ) -> None:
        """Empty ui.url file is unlinked and replaced by a fresh spawn."""
        (home / "ui.url").write_text("")  # corrupt: empty

        new_url, _t, server = _start_stub(_HealthOK)
        try:
            fake_popen.schedule_url_write(after=0.05, url=new_url, home=home)

            result = ui_launcher.start_ui()

            assert result == {"url": new_url, "reused": False}
            assert len(fake_popen.instances) == 1
        finally:
            server.shutdown()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestSpawn::test_corrupt_ui_url_treated_as_missing -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): empty ui.url treated as stale and replaced"
```

---

## Task 8: Verify spawn kwargs — log file, DEVNULL stdin, detach flags

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

Pin the Popen kwargs contract so future refactors don't accidentally drop the log redirect or the detach flags.

- [ ] **Step 1: Write the failing test**

Append to `TestSpawn`:

```python
    def test_popen_kwargs_log_file_and_devnull_stdin(
        self, home: Path, fake_popen
    ) -> None:
        url, _t, server = _start_stub(_HealthOK)
        try:
            fake_popen.schedule_url_write(after=0.05, url=url, home=home)
            ui_launcher.start_ui()

            inst = fake_popen.instances[0]
            assert inst.stdin is subprocess.DEVNULL
            # stdout and stderr both point at an open file under home/
            assert hasattr(inst.stdout, "write")
            assert hasattr(inst.stderr, "write")
            # Same handle (one shared log file).
            assert inst.stdout is inst.stderr
            # Log file path resolves under home.
            assert (home / "ui.log").exists()
        finally:
            server.shutdown()

    def test_popen_kwargs_detach_flags_match_platform(
        self, home: Path, fake_popen
    ) -> None:
        url, _t, server = _start_stub(_HealthOK)
        try:
            fake_popen.schedule_url_write(after=0.05, url=url, home=home)
            ui_launcher.start_ui()

            inst = fake_popen.instances[0]
            if sys.platform == "win32":
                expected = (
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
                assert inst.kwargs.get("creationflags") == expected
            else:
                assert inst.kwargs.get("start_new_session") is True
        finally:
            server.shutdown()
```

Add the corresponding imports at the top of the file if missing:

```python
import subprocess
import sys
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestSpawn -v`
Expected: all spawn tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): pin Popen kwargs (log file, DEVNULL stdin, detach flags)"
```

---

## Task 9: `_detach_kwargs` per-platform unit test

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

Branch coverage for `_detach_kwargs` itself — Task 8 only exercises the current platform's branch. This task uses `monkeypatch.setattr` on `sys.platform` to assert both branches.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_ui_launcher.py`:

```python
class TestDetachKwargs:
    def test_posix_uses_start_new_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        assert ui_launcher._detach_kwargs() == {"start_new_session": True}

    def test_windows_uses_creationflags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        kwargs = ui_launcher._detach_kwargs()
        expected = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        assert kwargs == {"creationflags": expected}
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/services/test_ui_launcher.py::TestDetachKwargs -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_ui_launcher.py
git commit -m "test(ui_launcher): cover both platform branches of _detach_kwargs"
```

---

## Task 10: Wire MCP handler — passthrough to `ui_launcher.start_ui`

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_start_ui_tool.py`

Replace the stub handler with a 3-line passthrough. Update the Tool description and module docstring. Add an integration test mirroring the `test_episode_tools.py` pattern.

- [ ] **Step 1: Write the failing test**

Create `tests/mcp/test_start_ui_tool.py`:

```python
"""Integration tests for the memory.start_ui MCP tool.

The handler is a thin passthrough; we verify it (a) is registered by name
and (b) returns the JSON shape the service produces.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


class TestStartUITool:
    def test_tool_is_registered_in_factory(self) -> None:
        """memory.start_ui appears in the tool list."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.start_ui" in tool_names

    def test_tool_description_no_longer_says_stub(self) -> None:
        """The Tool description was updated when the implementation landed."""
        from better_memory.mcp.server import _tool_definitions

        tool = next(
            t for t in _tool_definitions() if t.name == "memory.start_ui"
        )
        assert "stub" not in tool.description.lower()
        assert "Plan 2" not in tool.description

    @pytest.mark.asyncio
    async def test_handler_passes_through_to_service(self) -> None:
        """memory.start_ui handler delegates to ui_launcher.start_ui."""
        from better_memory.mcp import server as mcp_server

        with patch(
            "better_memory.services.ui_launcher.start_ui",
            return_value={"url": "http://127.0.0.1:54321", "reused": True},
        ) as svc_mock:
            result = await mcp_server._dispatch_tool(
                name="memory.start_ui", arguments={}
            )

        svc_mock.assert_called_once_with()
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload == {
            "url": "http://127.0.0.1:54321",
            "reused": True,
        }
```

Note: `_dispatch_tool` may not exist by that name in `server.py`. The existing handler dispatch is inside `call_tool` which is the registered MCP callback. To make this testable, refactor the handler dispatch into a module-level function we can call directly. See Step 3.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/mcp/test_start_ui_tool.py -v`
Expected: FAIL — either `_dispatch_tool` is missing, or the description still says "stub".

- [ ] **Step 3: Refactor `server.py` — extract `_dispatch_tool`, replace stub, update Tool description**

Edit `better_memory/mcp/server.py`. Three changes:

**Change A — module docstring at lines 16 and 29-31.** Find and replace:

```
* ``memory.start_ui``      — Plan 2 stub; returns an explanatory error.
```

with:

```
* ``memory.start_ui``      — spawn or reuse the management UI; returns ``{url, reused}``.
```

And find:

```
The ``memory.start_ui`` stub is *not* an error — it returns a normal
``{"error": "UI not yet implemented ..."}`` JSON payload so clients can
display the message without treating it as a tool crash.
```

Delete those three lines entirely (the surrounding paragraph about the standard isError convention stays).

**Change B — Tool description at line 237-247.** Replace the existing `Tool(name="memory.start_ui", ...)` block with:

```python
        Tool(
            name="memory.start_ui",
            description=(
                "Spawn or reuse the better-memory management UI. Returns "
                '{"url": str, "reused": bool}. Reuses an existing live UI '
                "when one is already running on /healthz."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
```

**Change C — handler at line 546-558.** Replace the stub block with a passthrough. Add the import near the other service imports at the top of the file:

```python
from better_memory.services import ui_launcher
```

Replace the stub handler:

```python
        if name == "memory.start_ui":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": (
                                "UI not yet implemented — planned for Plan 2."
                            )
                        }
                    ),
                )
            ]
```

with:

```python
        if name == "memory.start_ui":
            result = ui_launcher.start_ui()
            return [
                TextContent(type="text", text=json.dumps(result))
            ]
```

- [ ] **Step 4: Extract `_dispatch_tool` so the test can call it directly**

The existing handler is the inner function `call_tool` registered via `@server.call_tool()`. It captures `name` and `arguments` and dispatches via the long `if name == "..."` chain.

Refactor: pull the dispatch chain into a module-level `async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]` and have `call_tool` delegate to it. This makes the dispatch testable without the MCP framework.

Locate the `@server.call_tool()` registration in `server.py`. The current shape is:

```python
        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            # ... the long if/elif chain ...
```

Refactor to:

```python
async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]:
    # ... the long if/elif chain, lifted verbatim ...


def _register_handlers(server: Server) -> None:
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await _dispatch_tool(name, arguments)
```

The dispatch chain references closures (e.g. service instances). If those are constructed inside the same outer function, lift them too — pass them in as arguments to `_dispatch_tool`, OR (simpler) keep them as module-level singletons initialized once. **Choose whichever requires fewer lines changed in this commit.** If service singletons already exist at module level (check the top of `call_tool`'s enclosing function), use that pattern; otherwise wrap the dispatch logic in a thin object that holds the services.

To minimise refactor scope: add a module-level `_DISPATCH_REGISTRY: dict[str, Callable] | None = None` populated by `_register_handlers`, and have `_dispatch_tool` look up by `name`. But that's a larger change. Simplest:

If the handler currently has the form `async def call_tool(name, arguments) -> list[TextContent]:` with a long chain referencing local variables, change the test to drive it via a different route: import the module, replace `ui_launcher.start_ui` via `monkeypatch`, build the same JSON payload by constructing a fake handler call. Document this in the test:

```python
# Note: the MCP handler closes over service instances, so we test the
# end-to-end JSON shape by patching the service and asserting the wire
# format the handler produces. We deliberately do not invoke
# server.call_tool() — that requires the full MCP runtime.
```

If the existing handler IS already easily extractable, do the extraction. Inspect `server.py` to decide. The current task budget assumes the simpler "patch service, assert JSON" approach if extraction is non-trivial.

**Decision rule:** if `call_tool` is inside `async def main()` or another non-trivial outer scope, do NOT refactor it for this task — instead simplify the test to exercise the `ui_launcher.start_ui` boundary directly + assert tool registration. Drop the third test (`test_handler_passes_through_to_service`) and replace with:

```python
    @pytest.mark.asyncio
    async def test_handler_returns_service_result_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler wraps the service result as JSON TextContent.

        We patch ui_launcher.start_ui and call the same code path the MCP
        handler uses, asserting the wire format.
        """
        import json as _json

        from better_memory.services import ui_launcher
        from mcp.types import TextContent

        monkeypatch.setattr(
            ui_launcher,
            "start_ui",
            lambda: {"url": "http://127.0.0.1:54321", "reused": True},
        )

        # Mirror the handler body
        result = ui_launcher.start_ui()
        wrapped = [
            TextContent(type="text", text=_json.dumps(result))
        ]
        assert len(wrapped) == 1
        assert _json.loads(wrapped[0].text) == {
            "url": "http://127.0.0.1:54321",
            "reused": True,
        }
```

This is a weaker test (it tests the contract, not the wire-up), but combined with `test_tool_is_registered_in_factory` and `test_tool_description_no_longer_says_stub` it pins all the surface that this PR changes without risking a large `server.py` refactor.

- [ ] **Step 5: Run all MCP tests**

Run: `uv run pytest tests/mcp/test_start_ui_tool.py tests/services/test_ui_launcher.py -v`
Expected: all PASS.

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest`
Expected: all PASS, no new failures.

- [ ] **Step 7: Commit**

```bash
git add better_memory/mcp/server.py tests/mcp/test_start_ui_tool.py
git commit -m "feat(mcp): wire memory.start_ui handler to ui_launcher service"
```

---

## Task 11: Update `audit.py` docstring

**Files:**
- Modify: `better_memory/services/audit.py`

The "stub today" comment is stale. The action still performs no DB state transition — keep the bullet, but update the wording.

- [ ] **Step 1: Edit the module docstring**

Edit `better_memory/services/audit.py`. Find the bullet at lines 19-20:

```
* The ``memory.start_ui`` MCP tool is a stub today and performs no state
  transition; nothing to audit until it actually does something.
```

Replace with:

```
* The ``memory.start_ui`` MCP tool spawns / reuses a UI subprocess but
  performs no database state transition — the spawned process is runtime
  state, not persisted state. Nothing to audit.
```

- [ ] **Step 2: Verify no tests reference the old wording**

Run: `uv run pytest tests/services/test_audit.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add better_memory/services/audit.py
git commit -m "docs(audit): drop 'stub today' wording for memory.start_ui"
```

---

## Task 12: Update `README.md`

**Files:**
- Modify: `README.md`

Two locations to update:

1. The MCP tool table at line 121.
2. The "Management UI" section at lines 176-188.

- [ ] **Step 1: Update the tool-table row**

Edit `README.md`. Find:

```
| `memory.start_ui()` | Plan 2 stub. |
```

Replace with:

```
| `memory.start_ui()` | Spawn or reuse the management UI; returns `{url, reused}`. |
```

- [ ] **Step 2: Replace the Management UI section**

Find:

```markdown
## Management UI

Spawn the UI on demand (Phase 10 of Plan 2 will expose this as the
`memory.start_ui()` MCP tool). Until then, start it manually:

```
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
cat ~/.better-memory/ui.url   # print the bound URL
```

The UI exits after 30 minutes of inactivity, or when you click
**Close UI** in the header.
```

Replace with:

```markdown
## Management UI

Call the `memory.start_ui` MCP tool. It returns `{"url": ..., "reused": ...}`:
the URL is the loopback address the UI bound to; `reused` is `true` when an
existing live UI was returned and `false` when a fresh one was spawned. Open
the URL in a browser. Stdout and stderr from the UI subprocess are written
to `$BETTER_MEMORY_HOME/ui.log`.

The UI exits after 30 minutes of inactivity or when you click **Close UI**
in the header.

To launch it manually for debugging, the entry point is unchanged:

```
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
```
```

- [ ] **Step 3: Verify README renders cleanly**

Run: `uv run python -c "import pathlib; print(pathlib.Path('README.md').read_text()[:500])"`
Expected: prints the first 500 chars without error.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): memory.start_ui is no longer a stub"
```

---

## Task 13: Final integration smoke test

**Files:**
- (none — runs the full suite)

Confirm everything still works together.

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 2: Hand-test the MCP tool against a real subprocess (optional)**

This is a manual verification, not automated:

```bash
BETTER_MEMORY_HOME=/tmp/bm-smoke uv run python -c "
from better_memory.services import ui_launcher
import json
print(json.dumps(ui_launcher.start_ui(), indent=2))
"
```

Expected: prints `{"url": "http://127.0.0.1:NNNNN", "reused": false}`. Visit the URL — the UI should load. Run the same command again — `reused` should now be `true`.

Then visit `<url>/shutdown` (POST) or wait 30 min — process should exit.

- [ ] **Step 3: Confirm no regressions**

Run: `uv run pytest -v`
Expected: every test from before this PR still passes.

---

## Self-Review Notes

- **Spec coverage:** §3 architecture (Tasks 1-3, 10), §4 liveness/spawn flow (Tasks 1-3, 5-6), §5 concurrency (covered by docs only — no test, intentional per spec "documented as known limitation"), §6 error handling (Tasks 5-6), §7 testing (Tasks 1-9), §8 skill update (out of scope for this plan — separate task in the parent task list, post-merge), §9 deviations (codified in implementation), §10 out of scope (none built).
- **Out-of-scope confirmation:** no `memory.shutdown_ui`, no PID file, no browser opening, no audit row, no log rotation, no fs-lock — matches §1 and §10 of the spec.
- **Test coverage:** 9 service tests + 3 handler tests. Each covers one named behaviour from §7 of the spec.
- **No placeholders:** every step has either exact code, an exact command, or an exact text replacement. The one judgement call (Task 10 Step 4 — refactor `call_tool` or test the contract) has explicit decision criteria and a fallback path.
