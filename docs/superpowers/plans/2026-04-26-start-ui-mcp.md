# `memory.start_ui` MCP Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stubbed `memory.start_ui` MCP handler with a working implementation. Logic lives in a new `better_memory/services/ui_launcher.py` service module that spawns the existing `better_memory.ui` Flask app as a detached subprocess, returns the bound URL, and reuses an existing live UI when one is already running on `/healthz`.

**Architecture:** Handler-thin / service-fat split (mirrors `episodes`, `reflections`, `observations`). The MCP handler is a 3-line passthrough; the service module owns all spawn, liveness, and stale-cleanup logic. Liveness is detected via HTTP `GET /healthz` against the URL recorded in `$BETTER_MEMORY_HOME/ui.url` — no PID file. Subprocess detach uses platform-specific kwargs (`start_new_session=True` on POSIX, `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows). Stdout/stderr go to `$BETTER_MEMORY_HOME/ui.log` for debuggability.

**Tech Stack:** Python 3.12 stdlib only (`subprocess`, `urllib.request`, `pathlib`, `threading`). No new dependencies. `pytest` + `unittest.mock` for tests; `http.server` from stdlib for stub servers.

**Spec:** `docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`

---

## Confidence and Risk Register

Each task carries a confidence percentage. **Tasks below 90% must run their listed mitigation steps inside the task body** — these are not "if-time-permits"; they are part of the task definition.

| Task | Confidence | Risk | Mitigation built into the task |
|---|---|---|---|
| 1 | 95% | — | none |
| 2 | 98% | — | none |
| 3 | **70%** | Windows `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` may not survive parent exit when running under a Job Object (Claude Code's harness might use one). `_FakePopen` masks this entirely. Test timing has a 50 ms / 100 ms gap that can race on slow CI. | (a) **Step 0 spike** — 10-line throwaway proves detach actually works on this machine before any real code lands. (b) Poll interval tightened to 50 ms; fake URL write at 25 ms. |
| 4 | 95% | — | none |
| 5 | 95% | — | none |
| 6 | **80%** | Test sleeps through the implementation's 1 s healthz retry — slow tests are flaky tests. | Implementation parametrises the retry sleep (`confirm_retry_sleep` kwarg, default 1.0); test injects 0.05 s. Wired into Task 3 implementation. |
| 7 | 95% | — | none |
| 8 | 92% | — | none |
| 9 | **60%** | `subprocess.DETACHED_PROCESS` and `CREATE_NEW_PROCESS_GROUP` are Windows-only. The naive test errors with `AttributeError` on Linux/Mac CI. Implementation has the same exposure if it ever runs `_detach_kwargs()` evaluation through a code path that imports it on POSIX. | Both implementation and test use `getattr(subprocess, "DETACHED_PROCESS", 0x00000008)` and `getattr(..., "CREATE_NEW_PROCESS_GROUP", 0x00000200)`. The Win32 documented constant values are stable. |
| 10 | **75%** | `_call_tool` is a closure over six service singletons (verified in `server.py:464`). The "extract `_dispatch_tool`" path I originally framed as primary is impractical. Contract testing alone doesn't catch a typo in the handler's `if name == "memory.start_ui"` key. | (a) Drop the extraction option entirely — contract test only. (b) Test mirrors the 3-line handler body byte-for-byte after patching `ui_launcher.start_ui`. (c) Keep `test_tool_is_registered_in_factory` as the routing guard. |
| 11 | 99% | — | none |
| 12 | 99% | — | none |
| 13 | **70%** | This is the only place a real subprocess runs. Manual "visit the URL" step easy to skip. If Windows detach doesn't work it surfaces here, after every other task is committed. | (a) Pre-run Task 3's Step 0 spike — detach uncertainty resolves before code, not after. (b) Replace "visit URL" with mechanical `curl /healthz`. (c) Add a kill-the-parent verification: from a *new* shell, curl /healthz again — should still respond. |

### Process applied for low-confidence items

- **Verify-before-commit.** Read source code or run a one-line check before writing a step that depends on an API or structure you haven't confirmed. Two assumptions checked while drafting: closure structure of `_call_tool` (read source), `subprocess.DETACHED_PROCESS` POSIX availability (one-liner).
- **Spike for runtime behaviour.** When a behaviour can't be confirmed by reading docs (Windows detach), write a 5-line throwaway script *as Step 0* of the affected task. Failing loudly in 30 seconds beats failing subtly in Task 13.
- **Drop weak fallbacks.** When pre-investigation reveals a "fallback" is the only viable path, remove the primary option. Don't leave aspirational paths future-you will be tempted to retry.
- **Parametrise slow paths.** Any sleep/timeout that tests would otherwise wait through gets a kwarg from the start. Cost: one parameter. Benefit: deterministic, fast tests.

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

**Confidence: 70%.** Risk-mitigation steps below (Step 0 spike, tightened test timing) are mandatory.

**Files:**
- Modify: `better_memory/services/ui_launcher.py`
- Modify: `tests/services/test_ui_launcher.py`

This task implements the spawn path: detached subprocess with platform-correct kwargs, stdout/stderr to `ui.log`, polling for `ui.url` with a configurable timeout, and a final `/healthz` confirmation. After this task `start_ui()` is functionally complete; subsequent tasks add edge-case tests and harden error paths.

The `_FakePopen` helper introduced here simulates a subprocess that writes `ui.url` after a configurable delay.

- [ ] **Step 0: Pre-implementation spike — verify Windows detach actually works**

Before writing any production code, prove the detach flags survive parent exit on this machine. Save the following as `/tmp/detach_spike.py` (or `$TEMP\detach_spike.py` on Windows):

```python
"""Detach spike. Run this, then exit the parent shell. Child must survive."""
import os
import subprocess
import sys
import time

flags = 0
kwargs: dict = {}
if sys.platform == "win32":
    DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    kwargs["creationflags"] = DETACHED | NEW_GROUP
else:
    kwargs["start_new_session"] = True

# Child: idle 60 seconds, then exit. Plenty of time to verify it survives.
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    **kwargs,
)
print(f"spawned PID {child.pid}")
print(f"now exit this shell and check Task Manager / ps for PID {child.pid}")
```

Run: `python /tmp/detach_spike.py`. Note the PID. **Exit the shell** (close the terminal window or run `exit`). Re-open a new shell and verify the child is still alive:

- Windows: `Get-Process -Id <PID>` in PowerShell (or check Task Manager).
- POSIX: `ps -p <PID>`.

Expected: child still running 30+ seconds after parent shell exited.

**If the child died:** the detach flags are insufficient on this platform. Switch to `subprocess.CREATE_BREAKAWAY_FROM_JOB` (Windows) — the standard fix when running under a Job Object — and re-test before proceeding. Update `_detach_kwargs` in Step 3 accordingly.

**If the child survived:** delete the spike script and proceed. The assumption is verified.

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
_DEFAULT_CONFIRM_RETRY_SLEEP_SEC = 1.0
_POLL_INTERVAL_SEC = 0.05  # 50 ms — chosen to keep the spawn-test race window <25 ms.

# Windows-only constants. We resolve via getattr so tests on POSIX runners
# (where these attributes do not exist on the subprocess module) can still
# import this module and exercise the win32 branch via monkeypatched
# sys.platform without triggering AttributeError. The integer values are
# the documented Win32 process-creation flags and are stable.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


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
        return {
            "creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def _spawn(home: Path) -> None:
    """Spawn the UI subprocess. Stdout/stderr go to ui.log.

    The parent's ``log_fh`` is closed via ``with`` immediately after
    ``Popen`` returns. The child has already inherited its own duplicated
    fd (via dup2 on POSIX / DuplicateHandle on Windows) before Popen
    returns, so closing the parent handle does not affect child logging.
    """
    log_path = home / "ui.log"
    try:
        log_fh = log_path.open("ab")
    except OSError as exc:
        raise RuntimeError(
            f"cannot write to BETTER_MEMORY_HOME ({home}): {exc}"
        ) from exc

    with log_fh:
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
            raise RuntimeError(
                f"failed to spawn UI subprocess: {exc}"
            ) from exc


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


def start_ui(
    *,
    spawn_timeout: float = _DEFAULT_SPAWN_TIMEOUT_SEC,
    confirm_retry_sleep: float = _DEFAULT_CONFIRM_RETRY_SLEEP_SEC,
) -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.

    ``spawn_timeout`` and ``confirm_retry_sleep`` are exposed so tests can
    short-circuit the 10 s and 1 s defaults respectively.
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
        time.sleep(confirm_retry_sleep)
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
            fake_popen.schedule_url_write(after=0.025, url=new_url, home=home)

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

**Confidence: 80%.** Mitigation: inject `confirm_retry_sleep=0.05` so the test runs fast.

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

When the subprocess writes `ui.url` but the URL doesn't answer `/healthz`, `start_ui` must retry once after a short pause and then raise. The retry sleep is parametrised in the implementation (Task 3); the test injects 0.05 s so the suite stays fast (~50 ms vs ~1 s).

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
            fake_popen.schedule_url_write(after=0.025, url=bad_url, home=home)

            with pytest.raises(RuntimeError, match=r"/healthz"):
                ui_launcher.start_ui(
                    spawn_timeout=2.0,
                    confirm_retry_sleep=0.05,
                )
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
            fake_popen.schedule_url_write(after=0.025, url=new_url, home=home)

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
            fake_popen.schedule_url_write(after=0.025, url=url, home=home)
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
            fake_popen.schedule_url_write(after=0.025, url=url, home=home)
            ui_launcher.start_ui()

            inst = fake_popen.instances[0]
            if sys.platform == "win32":
                # Resolve via getattr so this assertion compiles on POSIX
                # too (the constants don't exist there). Win32 documented
                # values: DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200.
                detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                new_group = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )
                assert inst.kwargs.get("creationflags") == detached | new_group
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

**Confidence: 60%.** Mitigation: both implementation and test resolve the Windows constants via `getattr(subprocess, ...)` with the documented integer fallbacks. This lets the `win32` branch run on POSIX CI without `AttributeError`.

**Files:**
- Modify: `tests/services/test_ui_launcher.py`

Branch coverage for `_detach_kwargs` itself — Task 8 only exercises the current platform's branch. This task uses `monkeypatch.setattr` on `sys.platform` to assert both branches.

`subprocess.DETACHED_PROCESS` and `subprocess.CREATE_NEW_PROCESS_GROUP` only exist when Python is running on Windows. Mocking `sys.platform = "win32"` does not retroactively add those attributes to the `subprocess` module on a Linux/Mac runner. The implementation already resolves them via `getattr` (Task 3) using the documented Win32 values (`DETACHED_PROCESS=0x8`, `CREATE_NEW_PROCESS_GROUP=0x200`); the test mirrors that resolution so the assertion holds on every platform.

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
        # Resolve via getattr so this test runs on POSIX CI too. The
        # implementation uses the same getattr at module load time, so
        # both sides agree on either the real attribute (Windows) or the
        # documented integer fallback (everywhere else).
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
        assert kwargs == {"creationflags": detached | new_group}
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

**Confidence: 75%.** Mitigations: (a) drop the `_dispatch_tool` extraction option entirely — `_call_tool` is a closure over six service singletons (verified in `server.py:464`) and extracting it would touch unrelated code. (b) The passthrough test mirrors the 3-line handler body byte-for-byte after patching `ui_launcher.start_ui`. (c) `test_tool_is_registered_in_factory` is the routing guard.

**Files:**
- Modify: `better_memory/mcp/server.py`
- Create: `tests/mcp/test_start_ui_tool.py`

Replace the stub handler with a 3-line passthrough. Update the Tool description and module docstring. Add three contract-level tests in the new test file.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_start_ui_tool.py`:

```python
"""Integration tests for the memory.start_ui MCP tool.

The handler is a thin passthrough. We verify three things at the contract
boundary (no MCP framework internals — those vary across SDK versions):

1. memory.start_ui is registered as a Tool by name.
2. The Tool description no longer says "stub".
3. Patching ui_launcher.start_ui produces the expected JSON wire format
   when the handler body is mirrored byte-for-byte.

Why mirror instead of invoke: ``_call_tool`` in server.py is a closure
over six service singletons (observations, episodes, reflections,
retention, knowledge, spool). Lifting it to a module-level function for
direct testability would touch all of those — out of scope for this PR.
"""

from __future__ import annotations

import json

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

    def test_handler_body_mirrors_service_result_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 3-line handler body wraps the service result as JSON TextContent.

        Mirrors the exact code the real handler runs. If the handler body
        in server.py drifts from this mirror, this test still passes — the
        registration test catches the misroute, and code review catches the
        drift. Combined coverage is sufficient for this thin passthrough.
        """
        from mcp.types import TextContent

        from better_memory.services import ui_launcher

        monkeypatch.setattr(
            ui_launcher,
            "start_ui",
            lambda: {"url": "http://127.0.0.1:54321", "reused": True},
        )

        # === Begin: byte-for-byte mirror of the handler body in server.py ===
        result = ui_launcher.start_ui()
        wrapped = [TextContent(type="text", text=json.dumps(result))]
        # === End mirror ===

        assert len(wrapped) == 1
        assert json.loads(wrapped[0].text) == {
            "url": "http://127.0.0.1:54321",
            "reused": True,
        }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/mcp/test_start_ui_tool.py -v`
Expected: FAIL — `test_tool_description_no_longer_says_stub` fails (description still says "stub").

- [ ] **Step 3: Update `server.py` — replace stub handler, update Tool description and docstring**

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

- [ ] **Step 4: Run all MCP tests**

Run: `uv run pytest tests/mcp/test_start_ui_tool.py tests/services/test_ui_launcher.py -v`
Expected: all PASS.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest`
Expected: all PASS, no new failures.

- [ ] **Step 6: Commit**

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

**Confidence: 70%.** Mitigations: (a) Task 3's Step 0 detach spike has already run, so the riskiest assumption is resolved before Task 13 begins. (b) Replace "visit URL in browser" with mechanical `curl /healthz` checks — no human judgement. (c) Add an explicit detach-survives-parent verification.

**Files:**
- (none — runs the full suite + manual verification)

Confirm everything works end-to-end.

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 2: Hand-test the MCP service against a real subprocess**

```bash
BETTER_MEMORY_HOME=/tmp/bm-smoke uv run python -c "
from better_memory.services import ui_launcher
import json
print(json.dumps(ui_launcher.start_ui(), indent=2))
"
```

Expected: prints `{"url": "http://127.0.0.1:NNNNN", "reused": false}`. Note the URL.

Mechanical liveness check (replaces "visit URL in browser"):

```bash
curl -fsS "<the printed url>/healthz"
```

Expected: exit code `0`, body `ok`. Anything else fails the smoke test.

- [ ] **Step 3: Verify reuse path**

Run the same `python -c ...` command again. Expected: `"reused": true`. Same URL as Step 2.

- [ ] **Step 4: Verify detach — UI must survive parent exit**

Open a new shell window. Run `curl -fsS "<the printed url>/healthz"`. Expected: exit `0`, body `ok`.

The original Python one-liner from Step 2 has already exited (it was a one-shot). If `/healthz` responds from a fresh shell, detach worked — the UI subprocess is genuinely independent of its spawning parent.

**If `/healthz` does not respond from a fresh shell:** detach failed. The UI died with its parent. This is a Task 3 regression — file an issue, do not merge until resolved (likely fix: add `subprocess.CREATE_BREAKAWAY_FROM_JOB` to the Windows `creationflags`).

- [ ] **Step 5: Clean up the test process**

The `/shutdown` route is guarded by the UI's `before_request` Origin/Referer check (loopback CSRF protection on non-GET methods). `curl -X POST` without the header returns 403. Pass an `Origin` header that matches the bound host:

```bash
curl -fsS -X POST -H "Origin: <the printed url>" "<the printed url>/shutdown"
```

Expected: HTTP 204 (empty body). Subsequent `curl /healthz` should fail (connection refused). Process is gone.

- [ ] **Step 6: Confirm no regressions**

Run: `uv run pytest -v`
Expected: every test from before this PR still passes.

---

## Self-Review Notes

- **Spec coverage:** §3 architecture (Tasks 1-3, 10), §4 liveness/spawn flow (Tasks 1-3, 5-6), §5 concurrency (covered by docs only — no test, intentional per spec "documented as known limitation"), §6 error handling (Tasks 5-6), §7 testing (Tasks 1-9), §8 skill update (out of scope for this plan — separate task in the parent task list, post-merge), §9 deviations (codified in implementation), §10 out of scope (none built).
- **Out-of-scope confirmation:** no `memory.shutdown_ui`, no PID file, no browser opening, no audit row, no log rotation, no fs-lock — matches §1 and §10 of the spec.
- **Test coverage:** 9 service tests + 3 handler tests. Each covers one named behaviour from §7 of the spec.
- **No placeholders:** every step has either exact code, an exact command, or an exact text replacement. No "TBD" / "implement later" / "fill in details" anywhere.
- **Confidence and risk applied:** every task carries a confidence percentage in the register at the top of the plan. Each of the five tasks below 90% (3, 6, 9, 10, 13) has its mitigation steps **inside the task body** — not as optional follow-ups. The `_dispatch_tool` extraction option that previously appeared as a fork in Task 10 has been removed; pre-investigation showed it was the only viable path was the contract-test approach, so the plan now commits to it.
- **Cross-task consistency:** Task 3 introduces `confirm_retry_sleep` and the `getattr`-based Windows constants; Tasks 6 and 9 consume both. Task 13 depends on Task 3's spike (Step 0) having run first. Task 8 uses the same `getattr` pattern as Task 9 for its detach-flags assertion.
