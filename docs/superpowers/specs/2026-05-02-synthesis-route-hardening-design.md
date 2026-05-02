# Synthesis route hardening (sub-design B) — design

**Status:** Approved 2026-05-02
**Branch target:** new feature branch off `main` (name TBD by user, e.g. `synth-bridge`)
**Predecessor:** sub-design A (`2026-05-01-repo-hygiene-a-design.md`) — landed in PR #17
**Successor sub-designs:** C (background lifecycle), D (type & identity infra) — out of scope here.

## Goal

Extract the worker-thread / fresh-event-loop / setup-error-capture pattern from `/observations/synthesize` into a reusable helper, add a 60-second timeout, and address four concerns surfaced during the assumption audit:

1. The current refactor risk of dropping the protective `try/except` around `chat.aclose()`.
2. The current absence of any concurrency control on the synthesize route.
3. SQLite lock contention from an abandoned worker thread (no `busy_timeout` configured).
4. The 30s timeout candidate being too short for Ollama cold-start.

## Why now

The synthesize route on `main` is ~75 LOC of inline thread-+-loop-+-resource dance (`better_memory/ui/app.py:432-506`). Every Bugbot fix in PRs #11–#14 hit a different fragile invariant in this same code (memories `5da4601f`, `27f77fb6`, `3f9977486f`, `4f7afb58`, `777c89b2`). The next async route a developer adds will copy-paste this and almost certainly get one invariant wrong. Extracting a helper now, before there's a second async route, prevents recurrence.

Adding the timeout closes a separate hang risk: today, an unresponsive Ollama causes `worker.join()` to block the Flask request indefinitely, and with the single shared SQLite connection (separate concern, on `main`), that blocks every other UI request including `/healthz`.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Helper interface | `run_async_in_worker(coro_factory, *, timeout)` — generic callable bridge | Encapsulates the bug-prone parts (thread + loop + setup error capture) without overspecifying resources. The factory pattern co-locates resource construction with the coroutine that uses them. |
| Helper file location | `better_memory/async_bridge.py` (top-level) | Reusable package-wide infrastructure, alongside `config.py`. Locking it inside `ui/` would invite copy-paste at the next caller. |
| Timeout duration | 60s hardcoded | Covers Ollama cold-start (model load) on first call after server restart. Single-user local tool — env-var promotion is a one-line change if it ever becomes a complaint. |
| Timeout HTTP response | 504 Gateway Timeout + `card-error` | Semantically right (gateway-style timeout to LLM), reuses existing error template. |
| Worker thread on timeout | Abandon (daemon=True) | Python threads can't be cancelled safely. Daemon thread dies with process. The worker continues to completion in the background and clears the busy flag in its own `finally`. |
| Concurrency control | Module-level busy flag, returns 429 if a worker is in-flight | Prevents the failure-mode where repeated user clicks during a hang spawn multiple workers, each holding fresh httpx pools and DB connections. Flag is cleared from inside the worker's `finally`, so abandoned workers don't strand it. |
| SQLite contention | `PRAGMA busy_timeout=5000` in `db/connection.py` | One-line addition applies repo-wide. If an abandoned worker holds the write lock during synthesize, the next writer waits up to 5s instead of immediately failing with `database is locked`. More permissive than current behaviour — no regression risk. |
| Resource cleanup placement | Inside the inner `_run` coroutine's `finally`, wrapped in `try/except BaseException: pass` | Matches the loop that owns the resources. The `try/except` preserves the intent of memory `777c89b2` ("cleanup failure must not mask the synthesize result/exception"). |

## Approach

Single branch, three logical commits:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `feat(async-bridge): add run_async_in_worker helper` | `better_memory/async_bridge.py` (new), `tests/test_async_bridge.py` (new) | Feature |
| 2 | `chore(db): add busy_timeout PRAGMA to connect()` | `better_memory/db/connection.py`, `tests/db/test_connection.py` (new or extend) | Chore |
| 3 | `refactor(ui): synthesize uses async_bridge with timeout + concurrency guard` | `better_memory/ui/app.py`, `tests/ui/test_observations.py` | Refactor |

Commits are independent — sequencing is for review clarity. Single PR.

## Commit 1 — `better_memory/async_bridge.py`

New module. Generic callable bridge that encapsulates four traps:

1. `asyncio.run()` fails inside pytest-asyncio's auto-mode running loop.
2. `httpx.AsyncClient` pools are bound to the event loop they were created on; sharing across short-lived loops produces transport errors after the first close (memory `27f77fb6`).
3. `sqlite3.Connection` objects are bound to the thread that created them and raise `ProgrammingError` if used elsewhere (memory `5da4601f`).
4. Setup errors raised before the inner `try/except` body escape silently if only the `loop.run_until_complete(...)` call is wrapped (memory `4f7afb58`).

**Public API:**

```python
def run_async_in_worker[T](
    coro_factory: Callable[[], Coroutine[object, object, T]],
    *,
    timeout: float | None = None,
) -> T:
    """Run an async coroutine in a fresh thread with a fresh event loop.

    The factory is invoked INSIDE the worker thread, so any resources it
    constructs (sqlite connections, httpx clients) are bound to that
    thread and that loop.

    The entire worker body is wrapped in try/except BaseException, so
    setup errors before the awaitable starts (e.g. sqlite3.connect()
    raising) propagate cleanly.

    Returns the coroutine's value, or raises:
    - WorkerTimeout if `timeout` elapses before the worker joins.
      The worker thread is daemon=True and abandoned (Python threads
      can't be cancelled). It continues to completion in the background;
      any locks held by the coroutine stay held until then.
    - Any exception the coroutine raises (re-raised on the caller).
    """


class WorkerTimeout(TimeoutError):
    """Raised when run_async_in_worker exceeds its timeout."""
```

**Implementation outline:**

```python
def run_async_in_worker[T](coro_factory, *, timeout=None):
    result_holder: list[T] = []
    exc_holder: list[BaseException] = []

    def _worker():
        try:
            coro = coro_factory()
            loop = asyncio.new_event_loop()
            try:
                value = loop.run_until_complete(coro)
                result_holder.append(value)
            finally:
                loop.close()
        except BaseException as exc:
            exc_holder.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise WorkerTimeout(f"worker did not complete within {timeout}s")
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0]
```

**Tests** (`tests/test_async_bridge.py`):

| Test | Asserts |
|---|---|
| `test_returns_coroutine_result` | Happy path: `async def: return 42` returns 42. |
| `test_raises_coroutine_exception` | Coroutine `raise ValueError("x")` → caller catches `ValueError("x")` (same type, same message). |
| `test_captures_factory_setup_error` | Factory raises before coroutine starts (e.g. simulates `sqlite3.connect()` raising). Caller sees the exception. This is the PR #11 root-cause regression test. |
| `test_timeout_raises_WorkerTimeout` | Coroutine `await asyncio.sleep(5)`, timeout=0.1 → `WorkerTimeout`. |
| `test_each_call_gets_fresh_loop` | Two sequential calls each get a fresh loop. Use a closeable mock that records its loop binding; second call's resource doesn't see closed-loop errors. |
| `test_works_inside_pytest_asyncio_running_loop` | The test itself runs in pytest-asyncio's auto loop (default for this project). The bridge must work — verifies `asyncio.run()` substitute behaviour. |

## Commit 2 — `PRAGMA busy_timeout=5000` in `db/connection.py`

One-line addition to `connect()` next to the existing PRAGMAs:

```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")  # 5s wait on contended write
```

Applies to every `connect()` call repo-wide. If an abandoned synthesize worker still holds the write lock when the user retries, the next writer waits up to 5s for the lock instead of immediately raising `OperationalError: database is locked`.

**No regression risk** — this is more permissive than current behaviour. Tests that verify lock-contention failure mode (none exist) would need updating; tests that exercise normal writes are unaffected.

**Test** (`tests/db/test_connection.py`):

```python
def test_busy_timeout_pragma_set(tmp_path):
    conn = connect(tmp_path / "test.db")
    try:
        result = conn.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == 5000
    finally:
        conn.close()
```

## Commit 3 — Refactored `/observations/synthesize`

The route currently spans `app.py:432-506` (~75 LOC). After refactor, ~45 LOC including the new busy-flag plumbing.

**Module-level concurrency guard** (added near the top of `create_app` or as module-level — implementation choice for the implementer):

```python
_synth_busy = False
_synth_lock = threading.Lock()

def _try_acquire_synth() -> bool:
    """Atomically check-and-set the synth busy flag. Returns True if acquired."""
    global _synth_busy
    with _synth_lock:
        if _synth_busy:
            return False
        _synth_busy = True
        return True

def _release_synth() -> None:
    """Release the synth busy flag. Called from inside the worker thread."""
    global _synth_busy
    with _synth_lock:
        _synth_busy = False
```

**Refactored route:**

```python
@app.post("/observations/synthesize")
def observations_synthesize() -> tuple[str, int, dict[str, str]]:
    if not _try_acquire_synth():
        return (
            '<div class="card card-error">Synthesis already in progress. '
            'Wait for it to finish, then try again.</div>',
            429, {},
        )

    project = _project_name()
    db_path = app.extensions["db_path"]
    ollama_host = app.extensions["ollama_host"]
    consolidate_model = app.extensions["consolidate_model"]

    def _build_coro():
        local_conn = connect(db_path)
        chat = OllamaChat(host=ollama_host, model=consolidate_model)
        svc = ReflectionSynthesisService(local_conn, chat=chat)

        async def _run():
            try:
                result = await svc.synthesize(
                    goal="manual synthesis", tech=None, project=project,
                )
                return result, dict(svc.last_run_counts)
            finally:
                # Cleanup must NOT mask the synthesize result or exception
                # — memory 777c89b2.
                try:
                    await chat.aclose()
                except BaseException:  # noqa: BLE001
                    pass
                try:
                    local_conn.close()
                except BaseException:  # noqa: BLE001
                    pass
                _release_synth()
        return _run()

    try:
        result, run_counts = run_async_in_worker(_build_coro, timeout=60.0)
    except WorkerTimeout:
        # Worker abandoned but still running. _release_synth() will fire
        # whenever it actually finishes. Until then, new requests get 429.
        return (
            '<div class="card card-error">Synthesis timed out after 60s. '
            'The worker is still running; the UI will be available again '
            'shortly.</div>',
            504, {},
        )
    except BaseException as exc:  # noqa: BLE001
        return (
            f'<div class="card card-error"><p>{escape(str(exc))}</p></div>',
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

**Key invariants:**

- `_release_synth()` lives in the inner coroutine's `finally`. That `finally` runs in the worker thread regardless of whether the route returned via 200 / 500 / 504. So abandoned workers don't strand the busy flag.
- Cleanup wrappers (`try: ...; except BaseException: pass`) preserve the intent of memory `777c89b2`.
- The route's three exit paths (200 success, 504 timeout, 500 other-exception) each have a clear semantic.

**Tests** (`tests/ui/test_observations.py`, extend existing class):

| Test | Setup | Asserts |
|---|---|---|
| Existing `test_synthesize_*` | Real DB conn + monkeypatch `OllamaChat.complete` | (Unchanged — verifies happy path still works after refactor.) |
| `test_synthesize_returns_504_on_timeout` | Monkeypatch `OllamaChat.complete` to `await asyncio.sleep(120)`. Override route timeout to 0.1s if testable, or accept the longer test runtime. | Status 504; body contains "timed out". |
| `test_synthesize_returns_429_when_already_in_flight` | Two concurrent POSTs (via `threading.Thread` or two test clients). | Exactly one returns 200/504/500; the other returns 429. |
| `test_busy_flag_cleared_after_completion` | Sequential POSTs. | Second POST returns success (proves flag was released after first finished). |
| `test_busy_flag_cleared_after_exception` | First call's `OllamaChat.complete` raises. | Second sequential POST still succeeds (proves flag is released on exception path too). |

The 504 test needs the timeout to be parameterized (or the monkeypatch needs to make `complete` slow enough to exceed 60s). Implementation choice — the cleanest is to inject the timeout as a constructor arg to `create_app`, defaulting to 60.0; tests pass a smaller value.

## Risks (accepted)

1. **Per-call OllamaChat construction.** Same as current `main`. No regression. If synthesize ever becomes high-frequency, this would become wasteful — out of scope for B.
2. **60s timeout is opinionated.** If real-world cold starts consistently exceed 60s, the only fix is editing the route. Acceptable for v1; promote to env var if it becomes a complaint.
3. **Module-level busy flag is per-process**, not per-Flask-app-instance. If multiple Flask app instances ever run in the same process (e.g. parallel tests with their own apps), they would share the flag. Acceptable for current single-instance UI; if it becomes a test-isolation problem, move the flag to `app.extensions`.
4. **`WorkerTimeout` does not kill the underlying coroutine.** If Ollama actually takes 5 minutes, the worker holds the busy flag for those 5 minutes; the user sees 429 the whole time. The 504 error message names this explicitly so the user understands.
5. **The new 504 test may run slowly** if the timeout has to be a real 60s — implementer should expose the timeout as a constructor arg so tests can use a small value.

## Out of scope

| Item | Where it goes |
|---|---|
| Replace any remaining deep-monkeypatch tests of `synthesize` | Already addressed in PR #17; one sweep test in B is sufficient |
| `_last_run_counts` contract widening (the docstring/error-path gap) | Defer — fold into the next reflection.py edit |
| Per-tech / per-project synthesis routes | Future work — not yet a need |
| Streaming synthesis progress (chunked response, SSE) | Future work — not yet a need |
| A second async route to validate the helper's reusability | Will get exercised when something needs one |
| Retention scheduler, hook observability log | Sub-design C |
| Pyright in CI, `_project_name()` ambiguity | Sub-design D |
| Move the single-shared-sqlite-connection pattern to per-request | Out of scope — large refactor that would touch every route |

## Verification

End-of-branch acceptance criteria:

- `uv run pytest -q` — all 547 existing tests + the new tests pass.
- `uv run ruff check .` — no new errors versus `main` baseline (28 pre-existing).
- Manual smoke: trigger synthesize from the UI, observe new behaviour:
  - Banner renders with run counts on success.
  - Two rapid clicks: second one shows the 429 error card.
  - With Ollama stopped: 504 after 60s with the explicit message.
- The new helper has unit-test coverage for all four trap categories.
