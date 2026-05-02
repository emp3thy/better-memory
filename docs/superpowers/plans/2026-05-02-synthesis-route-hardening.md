# Synthesis Route Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a reusable `run_async_in_worker` helper from `/observations/synthesize`, add a 60-second timeout with module-level concurrency guard, restore the protective `try/except` around `chat.aclose()` lost in the refactor, and add `PRAGMA busy_timeout=5000` to mitigate abandoned-worker SQLite contention.

**Architecture:** Three logical commits on a new branch off `main`. (1) New `better_memory/async_bridge.py` module with full unit-test coverage of the four bug-prone traps. (2) One-line PRAGMA addition to `db/connection.py` for repo-wide SQLite contention tolerance. (3) Refactored `/observations/synthesize` route consuming the helper, gated by a module-level busy flag, returning 504 on timeout and 429 on contention.

**Tech Stack:** Python 3.12 (PEP 695 type params), asyncio, threading, sqlite3, Flask, pytest, pytest-asyncio (auto mode), httpx.

**Spec:** `docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md`

---

## File Structure

| File | Commit | Type | Responsibility |
|---|---|---|---|
| `better_memory/async_bridge.py` | 1 | Create | Generic `run_async_in_worker` helper + `WorkerTimeout` exception. ~50 LOC. |
| `tests/test_async_bridge.py` | 1 | Create | 6 unit tests covering: happy path, coroutine exception, factory setup error, timeout, fresh loop per call, running-loop compatibility. |
| `better_memory/db/connection.py` | 2 | Modify (+1 line) | Add `PRAGMA busy_timeout=5000` next to existing PRAGMAs. |
| `tests/db/test_connection.py` | 2 | Create | Single test asserting `PRAGMA busy_timeout` returns 5000. |
| `better_memory/ui/app.py` | 3 | Modify | Add module-level `_synth_busy` flag with lock + helpers; refactor `/observations/synthesize` to use the bridge with 60s timeout; restore try/except around cleanup; expose `synth_timeout` via `create_app`. |
| `tests/ui/conftest.py` | 3 | Modify (+~15 LOC) | Add `_synth_busy_isolation` autouse fixture covering ALL UI tests: waits up to 3s for any in-flight worker to release the flag naturally, then force-resets before/after each test. Prevents test-pollution from the 504 test's daemon thread. |
| `tests/ui/test_observations.py` | 3 | Modify | Add 4 new route tests. NO inline class-scoped fixture &mdash; the conftest one handles isolation across all UI tests. Update existing test if it relies on the removed inline thread structure. |
| `docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md` | 4 | Already exists | Spec is committed at end of branch. |
| `docs/superpowers/plans/2026-05-02-synthesis-route-hardening.md` | 4 | Already exists (this file) | Plan committed at end of branch. |

---

## Confidence summary

Per memory `dde30588` (confidence-scoring on every implementation plan; embed mitigations inside task body for sub-90%).

| Task | Confidence | Notes |
|---|---|---|
| 1. Pre-flight + branch | 98% | Pure verification. |
| 2. Helper test-first: happy path | 95% | Standard pytest-asyncio. |
| 3. Helper test-first: coroutine exception | 95% | Standard. |
| 4. Helper test-first: factory setup error | 92% | Captures the PR #11 root cause; pattern is straightforward. |
| 5. Helper test-first: timeout | 88% | **<90%**: timing-sensitive; embedded mitigation in task body (use generous timing margins, don't rely on exact intervals). |
| 6. Helper test-first: fresh loop per call | 88% | **<90%**: requires a closeable mock that records its loop binding; embedded sketch of the mock. |
| 7. Helper test-first: works inside running loop | 88% | **<90%**: pytest-asyncio's auto mode means the test itself runs in a loop — exactly the scenario we need to verify. Embedded note that the test passing is itself the assertion. |
| 8. Commit 1 (helper module) | 95% | Mechanical commit. |
| 9. PRAGMA + test + commit | 96% | Trivial change. |
| 10. Write 4 new route tests (failing) + add conftest isolation fixture | 90% | Up from 85% after adding the wider conftest fixture (eliminates the test-pollution risk from the 504 test's daemon thread). |
| 11. Refactor route (implement to make tests pass) | 80% | **<90%**: This is THE most-bug-prone code path in the project (memories `5da4601f`, `27f77fb6`, `3f9977486f`, `4f7afb58`, `777c89b2`). Embedded mitigations: (a) implement strictly to spec pseudocode in `2026-05-02-...-design.md` Commit 3; (b) verify each cleanup line is wrapped in try/except; (c) confirm `_release_synth()` is the LAST line of the inner finally so cleanup errors don't bypass it. |
| 12. Run full suite + ruff + commit refactor | 92% | Verification + commit. |
| 13. Commit spec + plan | 96% | Already drafted. |
| 14. Push + open PR (USER GATED) | 95% | Pause-before-push, no merge. |

**Plan-wide notes:**
- All `git commit -m "$(cat <<'EOF' ... EOF)"` HEREDOC commits MUST be executed via the **Bash tool**, not PowerShell. PowerShell here-string syntax (`@'...'@`) is incompatible. Bash is available on Windows per the env.
- The repo has 28 pre-existing baseline ruff errors that are out of scope. The new code must add ZERO additional errors.
- pytest output capture: use `> file 2>&1` (stdout-first) when redirecting; the reverse `2>&1 > file` truncates at ~600 bytes on Windows (memory `2c17449099`). Or use `--junit-xml=junit.xml` for authoritative results.

---

## Task 1: Pre-flight + branch

**Files:** none (read-only checks)

- [ ] **Step 1: Confirm we're on `main` with a clean working tree**

```bash
git status --porcelain
git branch --show-current
```

Expected: `main`. Working tree clean (or only `.claude/` untracked, which is fine).

- [ ] **Step 2: Confirm tests pass on `main` baseline**

```bash
uv run pytest --junit-xml=junit.xml -q > /tmp/preflight.log 2>&1; cat /tmp/preflight.log | tail -3; rm -f junit.xml
```

Expected: `547 passed, 22 skipped, 6 deselected` (or close). If anything fails, STOP.

- [ ] **Step 3: Create the feature branch**

```bash
git checkout -b synth-bridge
```

- [ ] **Step 4: Confirm we're on the new branch**

```bash
git branch --show-current
```

Expected: `synth-bridge`.

---

## Task 2: Helper test — happy path

**Files:**
- Create: `tests/test_async_bridge.py`

- [ ] **Step 1: Create the test file with the first failing test**

Create `tests/test_async_bridge.py`:

```python
"""Unit tests for better_memory.async_bridge.run_async_in_worker."""

from __future__ import annotations

import asyncio

import pytest


def test_returns_coroutine_result() -> None:
    """Happy path: bridge returns whatever the coroutine returns."""
    from better_memory.async_bridge import run_async_in_worker

    async def _coro() -> int:
        return 42

    result = run_async_in_worker(lambda: _coro())
    assert result == 42
```

- [ ] **Step 2: Run the test, confirm it FAILS with ImportError**

```bash
uv run pytest tests/test_async_bridge.py::test_returns_coroutine_result -v
```

Expected: `ModuleNotFoundError: No module named 'better_memory.async_bridge'`.

- [ ] **Step 3: Create the module with the minimum implementation**

Create `better_memory/async_bridge.py`:

```python
"""Bridge sync code into a fresh-thread, fresh-event-loop async runner.

Encapsulates the four traps that bit us in PRs #11–#14:
1. asyncio.run() fails inside pytest-asyncio's auto-mode running loop.
2. httpx.AsyncClient pools are bound to their creation loop.
3. SQLite connections are bound to their creation thread.
4. Setup errors before the inner try/except escape silently if only
   loop.run_until_complete(...) is wrapped.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine


class WorkerTimeout(TimeoutError):
    """Raised when run_async_in_worker exceeds its timeout.

    The worker thread is daemon=True and is abandoned (Python threads
    can't be cancelled). It continues to completion in the background;
    any locks held by the coroutine stay held until then.
    """


def run_async_in_worker[T](
    coro_factory: Callable[[], Coroutine[object, object, T]],
    *,
    timeout: float | None = None,
) -> T:
    """Run an async coroutine in a fresh thread with a fresh event loop.

    The factory is invoked INSIDE the worker thread, so any resources
    it constructs (sqlite connections, httpx clients) are bound to
    that thread and that loop.

    The entire worker body is wrapped in try/except BaseException so
    setup errors before the awaitable starts (e.g. sqlite3.connect()
    raising) propagate cleanly.
    """
    result_holder: list[T] = []
    exc_holder: list[BaseException] = []

    def _worker() -> None:
        try:
            coro = coro_factory()
            loop = asyncio.new_event_loop()
            try:
                value = loop.run_until_complete(coro)
                result_holder.append(value)
            finally:
                loop.close()
        except BaseException as exc:  # noqa: BLE001 — re-raised on caller
            exc_holder.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise WorkerTimeout(
            f"worker did not complete within {timeout}s"
        )
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0]
```

- [ ] **Step 4: Run the test, confirm it PASSES**

```bash
uv run pytest tests/test_async_bridge.py::test_returns_coroutine_result -v
```

Expected: PASS.

---

## Task 3: Helper test — coroutine exception propagates

**Files:**
- Modify: `tests/test_async_bridge.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_async_bridge.py`:

```python
def test_raises_coroutine_exception() -> None:
    """If the coroutine raises, the same exception type+message is
    re-raised on the caller's thread."""
    from better_memory.async_bridge import run_async_in_worker

    class CustomError(RuntimeError):
        pass

    async def _coro() -> None:
        raise CustomError("boom")

    with pytest.raises(CustomError, match="boom"):
        run_async_in_worker(lambda: _coro())
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/test_async_bridge.py::test_raises_coroutine_exception -v
```

Expected: PASS (the implementation from Task 2 already handles this via `exc_holder`).

---

## Task 4: Helper test — factory setup error captured

**Files:**
- Modify: `tests/test_async_bridge.py`

- [ ] **Step 1: Add the failing test**

This is the PR #11 root-cause regression test. Memory `4f7afb58`: "Worker-thread bridge pattern silently swallows setup errors if the inner try/except only wraps the loop.run_until_complete call."

Append to `tests/test_async_bridge.py`:

```python
def test_captures_factory_setup_error() -> None:
    """Regression for PR #11 root cause (memory 4f7afb58):
    if the factory itself raises (e.g. simulating sqlite3.connect()
    failing) BEFORE the coroutine starts, the bridge must still
    propagate the error rather than hanging or returning silently."""
    from better_memory.async_bridge import run_async_in_worker

    class SetupError(RuntimeError):
        pass

    def _factory():
        raise SetupError("connect failed")

    with pytest.raises(SetupError, match="connect failed"):
        run_async_in_worker(_factory)
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/test_async_bridge.py::test_captures_factory_setup_error -v
```

Expected: PASS (the impl wraps `coro = coro_factory()` inside the outer try/except).

---

## Task 5: Helper test — timeout raises WorkerTimeout

**Files:**
- Modify: `tests/test_async_bridge.py`

**Confidence: 88% — timing-sensitive.**

**Mitigation embedded in task:** Use generous time margins. The coroutine sleeps 2 seconds; timeout is 0.1 seconds — that's a 20× ratio. Don't pin to tighter timing.

- [ ] **Step 1: Add the failing test**

Append to `tests/test_async_bridge.py`:

```python
def test_timeout_raises_WorkerTimeout() -> None:
    """If the worker doesn't finish within `timeout`, raises
    WorkerTimeout. The worker thread is abandoned and continues
    running in the background."""
    from better_memory.async_bridge import WorkerTimeout, run_async_in_worker

    async def _slow() -> None:
        await asyncio.sleep(2.0)

    with pytest.raises(WorkerTimeout):
        run_async_in_worker(lambda: _slow(), timeout=0.1)
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/test_async_bridge.py::test_timeout_raises_WorkerTimeout -v
```

Expected: PASS in roughly 0.1-0.3s.

---

## Task 6: Helper test — fresh event loop per call

**Files:**
- Modify: `tests/test_async_bridge.py`

**Confidence: 88% — requires a closeable mock that records loop binding.**

**Mitigation embedded:** Use a tiny fake httpx-style class that records `id(asyncio.get_running_loop())` at construction. Run the bridge twice. Assert the recorded loop ids differ. This proves each call gets a fresh loop (regression test for memory `27f77fb6`).

- [ ] **Step 1: Add the failing test**

Append to `tests/test_async_bridge.py`:

```python
def test_each_call_gets_fresh_loop() -> None:
    """Sequential calls to run_async_in_worker each create a fresh
    event loop, sidestepping the httpx-pool-bound-to-loop bug
    (memory 27f77fb6)."""
    from better_memory.async_bridge import run_async_in_worker

    captured_loop_ids: list[int] = []

    async def _capture_loop_id() -> None:
        captured_loop_ids.append(id(asyncio.get_running_loop()))

    run_async_in_worker(lambda: _capture_loop_id())
    run_async_in_worker(lambda: _capture_loop_id())

    assert len(captured_loop_ids) == 2
    assert captured_loop_ids[0] != captured_loop_ids[1], (
        "expected each call to get a fresh event loop"
    )
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/test_async_bridge.py::test_each_call_gets_fresh_loop -v
```

Expected: PASS.

---

## Task 7: Helper test — works inside an already-running loop

**Files:**
- Modify: `tests/test_async_bridge.py`

**Confidence: 88% — pytest-asyncio's auto mode is the test environment.**

**Mitigation embedded:** This project sets `asyncio_mode = "auto"` in `pyproject.toml`. Every sync test in pytest-asyncio's auto mode runs with an active event loop on the main thread. The bridge's whole reason for existing (instead of `asyncio.run`) is to work in this scenario. So any of the previous tests passing already demonstrates this. The explicit test below is documentation: it asserts `asyncio.get_running_loop()` succeeds in the test scope, then runs the bridge anyway.

- [ ] **Step 1: Add the failing test**

Append to `tests/test_async_bridge.py`:

```python
def test_works_inside_pytest_asyncio_running_loop() -> None:
    """Sanity: pytest-asyncio's auto mode means this test runs with
    a running loop on the main thread. asyncio.run() would raise.
    The bridge must work — that's its whole reason for existing
    (vs asyncio.run)."""
    from better_memory.async_bridge import run_async_in_worker

    # Confirm the test environment really does have a running loop.
    # If pytest-asyncio's mode changes and this assertion fails,
    # the test is no longer exercising what it claims to.
    try:
        asyncio.get_running_loop()
        loop_present = True
    except RuntimeError:
        loop_present = False
    assert loop_present, (
        "expected a running loop in the test scope (pytest-asyncio auto)"
    )

    async def _coro() -> str:
        return "ok"

    assert run_async_in_worker(lambda: _coro()) == "ok"
```

- [ ] **Step 2: Run, confirm PASS**

```bash
uv run pytest tests/test_async_bridge.py::test_works_inside_pytest_asyncio_running_loop -v
```

Expected: PASS.

- [ ] **Step 3: Run all 6 helper tests together — confirm they all pass**

```bash
uv run pytest tests/test_async_bridge.py -v
```

Expected: 6 passed.

- [ ] **Step 4: Run ruff on the new files**

```bash
uv run ruff check better_memory/async_bridge.py tests/test_async_bridge.py
```

Expected: All checks passed.

---

## Task 8: Commit 1 — async_bridge module

**Files:**
- `better_memory/async_bridge.py` (new)
- `tests/test_async_bridge.py` (new)

- [ ] **Step 1: Stage the two new files explicitly**

```bash
git add better_memory/async_bridge.py tests/test_async_bridge.py
```

- [ ] **Step 2: Verify the staged diff**

```bash
git diff --cached --stat
```

Expected: 2 files added, ~+50 / +120 LOC.

- [ ] **Step 3: Commit using the Bash tool (HEREDOC requires bash)**

```bash
git commit -m "$(cat <<'EOF'
feat(async-bridge): add run_async_in_worker helper

Generic callable bridge that encapsulates the four traps that bit
us in PRs #11–#14:
- asyncio.run() fails inside pytest-asyncio's auto-mode running loop
- httpx.AsyncClient pools are bound to their creation loop
- sqlite3 connections are bound to their creation thread
- setup errors raised before the inner try/except escape silently

The factory pattern means any resources the caller constructs
(sqlite connections, httpx clients) are bound to the worker thread
and worker loop, sidestepping the cross-thread/cross-loop bugs.

Optional timeout raises WorkerTimeout; the worker is daemon=True
and abandoned (Python threads can't be cancelled). It continues
to completion in the background.

6 unit tests cover happy path, coroutine exception, factory setup
error (the PR #11 regression), timeout, fresh-loop-per-call (the
PR #11 regression for httpx pool binding), and works-inside-a-
running-loop (the whole reason for not using asyncio.run).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify**

```bash
git log --oneline -1
git status --porcelain
```

Expected: new commit, clean working tree.

---

## Task 9: Commit 2 — busy_timeout PRAGMA

**Files:**
- Modify: `better_memory/db/connection.py:43-46`
- Create: `tests/db/test_connection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_connection.py`:

```python
"""Unit tests for better_memory.db.connection."""

from __future__ import annotations

from pathlib import Path


def test_busy_timeout_pragma_set(tmp_path: Path) -> None:
    """connect() applies PRAGMA busy_timeout=5000 so abandoned-worker
    SQLite locks don't immediately fail subsequent writers."""
    from better_memory.db.connection import connect

    conn = connect(tmp_path / "test.db")
    try:
        result = conn.execute("PRAGMA busy_timeout").fetchone()
        # PRAGMA busy_timeout returns a single value column.
        assert result[0] == 5000
    finally:
        conn.close()
```

- [ ] **Step 2: Run, confirm FAIL**

```bash
uv run pytest tests/db/test_connection.py::test_busy_timeout_pragma_set -v
```

Expected: FAIL — `assert 0 == 5000` (default busy_timeout is 0).

- [ ] **Step 3: Add the PRAGMA in `better_memory/db/connection.py`**

Find lines 43-45:

```python
        # WAL must be set via ``PRAGMA``; it persists across connections.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
```

Replace with:

```python
        # WAL must be set via ``PRAGMA``; it persists across connections.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # busy_timeout: wait up to 5s for write-lock contention before
        # raising OperationalError. Mitigates the case where an abandoned
        # synthesize worker (memory 4f7afb58) holds the lock briefly
        # after the route has already 504'd.
        conn.execute("PRAGMA busy_timeout=5000")
```

- [ ] **Step 4: Run the test, confirm PASS**

```bash
uv run pytest tests/db/test_connection.py::test_busy_timeout_pragma_set -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regression**

```bash
uv run pytest --junit-xml=junit.xml -q > /tmp/c2.log 2>&1; tail -3 /tmp/c2.log; rm -f junit.xml
```

Expected: 554 passed (547 prior + 6 helper from Commit 1 + 1 new PRAGMA test = 554), 22 skipped, 6 deselected.

- [ ] **Step 6: Run ruff**

```bash
uv run ruff check better_memory/db/connection.py tests/db/test_connection.py
```

Expected: All checks passed.

- [ ] **Step 7: Commit using the Bash tool**

```bash
git add better_memory/db/connection.py tests/db/test_connection.py
git commit -m "$(cat <<'EOF'
chore(db): add PRAGMA busy_timeout=5000 to connect()

Applies repo-wide. If an abandoned synthesize worker holds the
write lock briefly after the route has already returned 504, the
next writer waits up to 5s for the lock instead of immediately
raising OperationalError: database is locked.

More permissive than the default (0ms wait) — no regression risk.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Write 4 route tests (failing) + add conftest isolation fixture

**Files:**
- Modify: `tests/ui/conftest.py` (add autouse fixture)
- Modify: `tests/ui/test_observations.py` (add 4 tests)

**Confidence: 90% — wider conftest fixture eliminates the test-pollution risk from the 504 test's daemon thread.**

**Mitigation embedded:** The autouse fixture lives in `tests/ui/conftest.py` so it covers ALL UI tests (not just the synthesize class). Before each test, it WAITS up to 3 seconds for any in-flight worker to release `_synth_busy` naturally; then force-resets. After each test, force-reset again. This handles the 504 test's daemon thread (which keeps running ~1.5s after the route returns) without coupling the next test to its timing. Tests that don't touch synthesis pay one global read + one set per test.

- [ ] **Step 1: Add the `_synth_busy_isolation` autouse fixture to `tests/ui/conftest.py`**

Open `tests/ui/conftest.py` and append this fixture (also add `import time` to the imports if not already present):

```python
import time


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
```

The fixture imports `_synth_busy` lazily inside the function body (not at module level) so the fixture file doesn't fail to load if the attribute hasn't been added to `app.py` yet (e.g. mid-execution between Task 10 and Task 11).

- [ ] **Step 2: Confirm the target test class name (already verified at plan-write time)**

The class is `TestObservationsSynthesize` at `tests/ui/test_observations.py:272`. Confirm with:

```bash
grep -n "class TestObservationsSynthesize" tests/ui/test_observations.py
```

If the class no longer exists or has been renamed, STOP and report.

- [ ] **Step 3: Add the 4 new tests (no inline fixture — conftest handles it)**

In `tests/ui/test_observations.py`, locate the `TestObservationsSynthesize` class. Add these 4 tests at the bottom of the class. (Adjust imports at the top of the test file if needed: `import asyncio`, `from better_memory.llm.ollama import OllamaChat`.)

```python
    def test_synthesize_returns_429_when_already_in_flight(self, client):
        """If a synthesis worker is already in-flight (busy flag set),
        a second concurrent request returns 429 immediately."""
        from better_memory.ui import app as _app_module

        _app_module._synth_busy = True
        try:
            resp = client.post("/observations/synthesize")
            assert resp.status_code == 429
            assert b"already in progress" in resp.data
        finally:
            _app_module._synth_busy = False

    def test_synthesize_returns_504_on_timeout(
        self, tmp_path, monkeypatch
    ):
        """If the worker exceeds the timeout, the route returns 504
        with a clear error card. The test creates an app with a tiny
        synth_timeout to avoid actually waiting 60s."""
        from better_memory.ui.app import create_app

        async def _slow_complete(self, prompt):
            await asyncio.sleep(2.0)
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'

        monkeypatch.setattr(OllamaChat, "complete", _slow_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path,
            start_watchdog=False,
            synth_timeout=0.5,  # added in Task 11
        )
        c = app.test_client()
        resp = c.post("/observations/synthesize")
        assert resp.status_code == 504
        assert b"timed out" in resp.data

    def test_busy_flag_cleared_after_completion(
        self, tmp_path, monkeypatch
    ):
        """After a synthesis completes (success path), _synth_busy is
        False so the next call goes through."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        async def _ok_complete(self, prompt):
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'

        monkeypatch.setattr(OllamaChat, "complete", _ok_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )
        c = app.test_client()

        # First call succeeds (or short-circuits) and clears busy.
        resp1 = c.post("/observations/synthesize")
        assert resp1.status_code == 200
        assert _app_module._synth_busy is False

        # Second call must be allowed through (proves cleared).
        resp2 = c.post("/observations/synthesize")
        assert resp2.status_code == 200

    def test_busy_flag_cleared_after_exception(
        self, tmp_path, monkeypatch
    ):
        """If the synthesize coroutine raises, _synth_busy is still
        cleared (cleanup happens in the inner finally)."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        async def _bad_complete(self, prompt):
            raise RuntimeError("simulated synthesis failure")

        monkeypatch.setattr(OllamaChat, "complete", _bad_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )
        c = app.test_client()

        resp = c.post("/observations/synthesize")
        assert resp.status_code == 500
        assert _app_module._synth_busy is False
```

- [ ] **Step 4: Run the 4 new tests, confirm they FAIL**

```bash
uv run pytest tests/ui/test_observations.py -k "synthesize_returns_429 or synthesize_returns_504 or busy_flag_cleared" -v
```

Expected: 4 FAIL — variously with `AttributeError: module ... has no attribute '_synth_busy'`, `TypeError: create_app() got an unexpected keyword argument 'synth_timeout'`, or assertion failures. All four failing means the new behaviour isn't implemented yet, which is what TDD wants.

The conftest fixture from Step 1 will fail too on the import (`_app_module._synth_busy` doesn't exist yet), so all UI tests will error during fixture setup. That's expected — Task 11 fixes it.

---

## Task 11: Refactor route — implement to make tests pass

**Files:**
- Modify: `better_memory/ui/app.py:432-506` (route + module-level additions)

**Confidence: 80% — most-bug-prone area in the project.**

**Mitigations embedded:**
1. Implement strictly to spec pseudocode in `docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md` "Commit 3 — Refactored `/observations/synthesize`".
2. Verify EACH cleanup line is wrapped in its own `try/except BaseException: pass`. Two cleanups → two wrappers. Don't share a wrapper.
3. Confirm `_release_synth()` is the LAST line of the inner coroutine's `finally`. Cleanup errors must not bypass it. Place it AFTER the cleanup wrappers.
4. The `synth_timeout` parameter on `create_app` defaults to `60.0` so production behaviour is unchanged; only tests pass smaller values.

- [ ] **Step 1: Add the module-level imports + busy flag at the top of `better_memory/ui/app.py`**

Near the existing imports (after `from urllib.parse import urlparse`), add:

```python
from better_memory.async_bridge import WorkerTimeout, run_async_in_worker
```

Below the imports and `_project_name`, add the module-level concurrency primitives:

```python
_synth_busy: bool = False
_synth_lock = threading.Lock()


def _try_acquire_synth() -> bool:
    """Atomically check-and-set the synth busy flag.

    Returns True if acquired (caller now owns the slot), False if
    another synthesize is already in flight.
    """
    global _synth_busy
    with _synth_lock:
        if _synth_busy:
            return False
        _synth_busy = True
        return True


def _release_synth() -> None:
    """Release the synth busy flag. Called from inside the worker
    thread's coroutine finally, so it fires regardless of whether
    the route returned via 200 / 500 / 504."""
    global _synth_busy
    with _synth_lock:
        _synth_busy = False
```

- [ ] **Step 2: Add `synth_timeout` parameter to `create_app`**

Find the `create_app` signature:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
) -> Flask:
```

Replace with:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
    synth_timeout: float = 60.0,
) -> Flask:
```

Update the docstring accordingly (add a `synth_timeout` block).

- [ ] **Step 3: Replace the existing `/observations/synthesize` route body (currently `app.py:432-506`)**

Find:

```python
    @app.post("/observations/synthesize")
    def observations_synthesize() -> tuple[str, int, dict[str, str]]:
        # synthesize() is async; the route is sync. We can't use
        ...
        return rendered, 200, {"HX-Trigger": "observations-synthesized"}
```

Replace with the entire refactored route (~45 LOC):

```python
    @app.post("/observations/synthesize")
    def observations_synthesize() -> tuple[str, int, dict[str, str]]:
        # Concurrency guard: only one synthesize at a time per process.
        # If a previous worker timed out and is still running, the busy
        # flag stays True until that worker's inner finally fires.
        if not _try_acquire_synth():
            return (
                '<div class="card card-error">Synthesis already in '
                'progress. Wait for it to finish, then try again.</div>',
                429, {},
            )

        project = _project_name()
        db_path_local = app.extensions["db_path"]
        ollama_host = app.extensions["ollama_host"]
        consolidate_model = app.extensions["consolidate_model"]

        def _build_coro():
            local_conn = connect(db_path_local)
            chat = OllamaChat(host=ollama_host, model=consolidate_model)
            svc = ReflectionSynthesisService(local_conn, chat=chat)

            async def _run():
                try:
                    result = await svc.synthesize(
                        goal="manual synthesis",
                        tech=None,
                        project=project,
                    )
                    return result, dict(svc.last_run_counts)
                finally:
                    # Cleanup must NOT mask the synthesize result or
                    # exception (memory 777c89b2). Each cleanup gets
                    # its own wrapper so one failing doesn't skip the
                    # other.
                    try:
                        await chat.aclose()
                    except BaseException:  # noqa: BLE001
                        pass
                    try:
                        local_conn.close()
                    except BaseException:  # noqa: BLE001
                        pass
                    # Always-last: release the busy flag. Runs in the
                    # worker thread regardless of route exit path.
                    _release_synth()
            return _run()

        try:
            result, run_counts = run_async_in_worker(
                _build_coro, timeout=synth_timeout,
            )
        except WorkerTimeout:
            # Worker abandoned but still running; _release_synth()
            # will fire when it eventually finishes. Until then,
            # new requests get 429.
            return (
                f'<div class="card card-error">Synthesis timed out '
                f'after {synth_timeout}s. The worker is still running; '
                f'the UI will be available again shortly.</div>',
                504, {},
            )
        except BaseException as exc:  # noqa: BLE001
            return (
                f'<div class="card card-error"><p>{escape(str(exc))}'
                f'</p></div>',
                500, {},
            )

        bucket_counts = {k: len(v) for k, v in result.items()}
        rendered = render_template(
            "fragments/observations_synth_banner.html",
            counts=bucket_counts,
            run_counts=run_counts,
        )
        return rendered, 200, {"HX-Trigger": "observations-synthesized"}
```

(`asyncio` import that the old code used is no longer needed by this route; if no other route uses it, the import can be removed. Check first; don't remove if other routes need it.)

- [ ] **Step 4: Verify imports are correct**

```bash
uv run python -c "from better_memory.ui.app import create_app; app = create_app(start_watchdog=False); print('OK')"
```

Expected: `OK`.

- [ ] **Step 5: Run the 4 new route tests, confirm they PASS**

```bash
uv run pytest tests/ui/test_observations.py -k "synthesize_returns_429 or synthesize_returns_504 or busy_flag_cleared" -v
```

Expected: 4 PASS.

---

## Task 12: Run full suite + lint + commit refactor

**Files:** none new

- [ ] **Step 1: Run the full pytest suite — use the safe redirect order**

```bash
uv run pytest --junit-xml=junit.xml -q > /tmp/full.log 2>&1; PYTEST_EXIT=$?; tail -3 /tmp/full.log; echo "EXIT=$PYTEST_EXIT"; if [ -f junit.xml ]; then python3 -c "import xml.etree.ElementTree as ET; r=ET.parse('junit.xml').getroot(); s=r if r.tag=='testsuite' else r.find('testsuite'); print(f'tests={s.get(\"tests\")} failures={s.get(\"failures\")} errors={s.get(\"errors\")} skipped={s.get(\"skipped\")} time={s.get(\"time\")}')"; fi; rm -f junit.xml</antml-thought>
```

Expected: `EXIT=0`, `failures=0 errors=0`. Total tests: 558 (547 baseline + 6 helper + 1 PRAGMA + 4 route). Time: ~70-90s.

- [ ] **Step 2: Run ruff on the changed files**

```bash
uv run ruff check better_memory/ui/app.py tests/ui/test_observations.py
```

Expected: All checks passed.

- [ ] **Step 3: Stage the refactored files**

```bash
git add better_memory/ui/app.py tests/ui/test_observations.py
```

- [ ] **Step 4: Verify the diff size**

```bash
git diff --cached --stat
```

Expected: 2 files, roughly +130 / -55 LOC.

- [ ] **Step 5: Commit using the Bash tool**

```bash
git commit -m "$(cat <<'EOF'
refactor(ui): synthesize route uses async_bridge + 60s timeout + concurrency guard

Replaces the inline 75-LOC thread/loop/sqlite/httpx dance in
/observations/synthesize with a call to async_bridge.run_async_in_worker.
Adds a 60s timeout (returns 504) and a module-level busy flag
(returns 429 if a worker is already in-flight).

Restores the per-cleanup try/except wrappers around chat.aclose()
and local_conn.close() that were intent of memory 777c89b2 — cleanup
failures must not mask the synthesize result or exception.

The busy flag is cleared from inside the worker's coroutine
finally, so abandoned-on-timeout workers still release it when they
eventually finish.

create_app gains a `synth_timeout` parameter (default 60.0) so tests
can use a small value without actually sleeping.

Tests added:
- test_synthesize_returns_429_when_already_in_flight
- test_synthesize_returns_504_on_timeout
- test_busy_flag_cleared_after_completion
- test_busy_flag_cleared_after_exception

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Verify commit landed**

```bash
git log --oneline -3
git status --porcelain
```

Expected: 3 commits ahead of main; working tree clean except spec/plan untracked.

---

## Task 13: Commit spec + plan

**Files:**
- `docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md` (already exists)
- `docs/superpowers/plans/2026-05-02-synthesis-route-hardening.md` (already exists)

- [ ] **Step 1: Stage the spec and plan**

```bash
git add docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md \
        docs/superpowers/plans/2026-05-02-synthesis-route-hardening.md
```

- [ ] **Step 2: Commit using the Bash tool**

```bash
git commit -m "$(cat <<'EOF'
docs(superpowers): spec + plan for synth-bridge

Captures the design and TDD-shaped implementation plan for sub-design
B of the tech-debt audit (extract async-bridge helper + 60s timeout +
concurrency guard + busy_timeout PRAGMA).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Final verification + push (USER GATED)

**Files:** none

- [ ] **Step 1: Confirm 4 commits ahead of main, working tree clean**

```bash
git log --oneline main..HEAD
git status --porcelain
```

Expected: 4 commits (spec/plan + refactor + PRAGMA + helper); zero working-tree dirty.

- [ ] **Step 2: One last full-suite check**

```bash
uv run pytest --junit-xml=junit.xml -q > /tmp/final.log 2>&1; tail -3 /tmp/final.log; rm -f junit.xml
```

Expected: same total as Task 12 Step 1 (558 passed, 22 skipped).

- [ ] **Step 3: PAUSE — ask user before push**

Surface to user:

> "Branch `synth-bridge` ready with 4 commits ahead of main (helper, PRAGMA, refactor, spec+plan). All 552 tests pass. Want me to push and open a PR?"

Wait for explicit confirmation. **Do not push without it.**

- [ ] **Step 4: On confirmation, push and open PR**

```bash
git push -u origin synth-bridge
gh pr create --title "synth-bridge: extract async helper, 60s timeout, concurrency guard" --body "$(cat <<'EOF'
## Summary

Sub-design B of the tech-debt audit. Extracts the worker-thread / fresh-event-loop / setup-error-capture pattern from `/observations/synthesize` into a reusable `run_async_in_worker` helper, and hardens the route against the failure modes that have caused 4+ Bugbot fixes (PRs #11–#14).

- **feat(async-bridge):** new `better_memory/async_bridge.py` with `run_async_in_worker(coro_factory, *, timeout)` + `WorkerTimeout`. Encapsulates the four traps: `asyncio.run` fails in pytest-asyncio auto loop, httpx loop binding, sqlite same-thread enforcement, setup errors silently swallowed. 6 unit tests cover each.
- **chore(db):** `PRAGMA busy_timeout=5000` in `db/connection.py`. Repo-wide. If an abandoned synthesize worker holds the write lock briefly, next writers wait up to 5s instead of immediately failing.
- **refactor(ui):** `/observations/synthesize` route uses the helper with a 60s timeout. Returns 504 on timeout, 429 if a worker is already in flight (module-level busy flag, cleared from inside the worker's coroutine finally so abandoned workers still release it). Cleanup wrappers restored per memory `777c89b2`.

Spec: `docs/superpowers/specs/2026-05-02-synthesis-route-hardening-design.md`
Plan: `docs/superpowers/plans/2026-05-02-synthesis-route-hardening.md`

## Test plan

- [x] `uv run pytest -q` — 558 passed, 22 skipped
- [x] `uv run ruff check .` — no new errors over baseline
- [ ] Manual smoke: trigger synthesis, observe banner. Click twice rapidly — second click returns 429 card.
- [ ] Manual smoke: stop Ollama, trigger synthesis, observe 504 after 60s with explicit error message.
- [ ] Manual smoke: with Ollama running cold, trigger synthesis — verify completes within 60s for typical model load.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Memory sweep before declaring done**

Per CLAUDE.md mandatory triggers ("at the end of each phase / PR cycle, before invoking finishing-a-development-branch"), pause and review what was learned. Skip if nothing in this work was non-obvious.

Candidates:
- Was the bridge helper's design vindicated by easy test-writing? (Worth a `success` observation if so.)
- Did any hidden assumption surface during implementation that wasn't in the spec's assumption audit? (Record as `failure`.)
- Did the route refactor stay strictly within the spec, or did the implementer have to deviate? (Record any deviation.)

If nothing rose to the bar, no observation is needed — quality over quota.

---

## Self-Review

**Spec coverage check** (against `2026-05-02-synthesis-route-hardening-design.md`):

| Spec requirement | Plan task |
|---|---|
| Helper module `better_memory/async_bridge.py` | Tasks 2-8 |
| `run_async_in_worker[T]` signature | Task 2 Step 3 |
| `WorkerTimeout(TimeoutError)` | Task 2 Step 3 |
| Helper unit test: happy path | Task 2 |
| Helper unit test: coroutine exception | Task 3 |
| Helper unit test: factory setup error (PR #11 regression) | Task 4 |
| Helper unit test: timeout raises | Task 5 |
| Helper unit test: fresh loop per call | Task 6 |
| Helper unit test: works in running loop | Task 7 |
| `PRAGMA busy_timeout=5000` in `db/connection.py` | Task 9 |
| Connection test: PRAGMA value | Task 9 |
| Module-level `_synth_busy` + lock + `_try_acquire_synth` / `_release_synth` | Task 11 |
| `create_app(synth_timeout=60.0)` | Task 11 Step 2 |
| Refactored route with 60s timeout | Task 11 Step 3 |
| Per-cleanup `try/except BaseException` (memory `777c89b2`) | Task 11 Step 3 + Task 11 mitigation note |
| `_release_synth()` last in inner finally | Task 11 Step 3 + Task 11 mitigation note |
| Route test: 429 when in-flight | Task 10 + Task 11 verification |
| Route test: 504 on timeout | Task 10 + Task 11 verification |
| Route test: busy flag cleared after success | Task 10 + Task 11 verification |
| Route test: busy flag cleared after exception | Task 10 + Task 11 verification |
| Acceptance: `pytest -q` all pass + ruff clean for new code | Task 12 |

No spec gaps.

**Placeholder scan:** No "TBD", "TODO", "implement later", or "similar to Task N". Every code step has full code; every command step has the exact command and expected output. The two "implementation choice" notes in the spec are pinned in the plan (busy flag is module-level, not per-app; `synth_timeout` is exposed via `create_app`).

**Type consistency:** `WorkerTimeout` defined in Task 2 Step 3 used in Task 11 Step 3. `run_async_in_worker[T](coro_factory, *, timeout)` signature consistent across tasks. `_try_acquire_synth() -> bool`, `_release_synth() -> None` used consistently. `synth_timeout` parameter name consistent.

**Plan ready to execute.**
