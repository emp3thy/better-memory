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


def test_timeout_raises_WorkerTimeout() -> None:
    """If the worker doesn't finish within `timeout`, raises
    WorkerTimeout. The worker thread is abandoned and continues
    running in the background."""
    from better_memory.async_bridge import WorkerTimeout, run_async_in_worker

    async def _slow() -> None:
        await asyncio.sleep(2.0)

    with pytest.raises(WorkerTimeout):
        run_async_in_worker(lambda: _slow(), timeout=0.1)


def test_each_call_gets_fresh_loop() -> None:
    """Sequential calls to run_async_in_worker each create a fresh
    event loop, sidestepping the httpx-pool-bound-to-loop bug
    (memory 27f77fb6)."""
    import gc

    from better_memory.async_bridge import run_async_in_worker

    captured_loop_ids: list[int] = []

    async def _capture_loop_id() -> None:
        captured_loop_ids.append(id(asyncio.get_running_loop()))

    run_async_in_worker(lambda: _capture_loop_id())
    # Force GC so CPython doesn't re-use the first loop's memory address
    # for the second loop, which would produce a spurious equality.
    gc.collect()
    run_async_in_worker(lambda: _capture_loop_id())

    assert len(captured_loop_ids) == 2
    assert captured_loop_ids[0] != captured_loop_ids[1], (
        "expected each call to get a fresh event loop"
    )


async def test_works_inside_pytest_asyncio_running_loop() -> None:
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
