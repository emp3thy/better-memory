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
