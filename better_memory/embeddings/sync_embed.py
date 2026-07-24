"""Sync facade over the async embedder, for thread-bound sync services.

Two hazards this class exists to contain:

1. ``httpx.AsyncClient`` is bound to the event loop that first uses it,
   and ``run_async_in_worker`` runs a fresh loop per call — so the
   embedder must be CONSTRUCTED inside the worker coroutine and closed
   there. The factory is invoked per call; ``OllamaEmbedder`` construction
   is cheap and contacts nothing.
2. Retrieval must never hang on a dead Ollama. Any failure opens a
   circuit breaker: for ``cooldown`` seconds every call returns ``None``
   immediately. Worst case is one bounded stall per cooldown window.

Returns ``None`` on every failure path — callers treat a missing vector
as "no vec leg", never as an error.

The in-memory ``_down_until`` breaker is per-process, so it does nothing
for hook invocations, which are short-lived subprocesses that exit and
restart on every event. ``down_state_file`` persists the cooldown deadline
to disk (wall-clock ``time.time()``, not the monotonic ``clock``) so a
fresh hook process can see that Ollama was recently marked down and skip
straight to ``None`` instead of re-probing a dead endpoint on every event.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from better_memory.async_bridge import run_async_in_worker

#: Bridge-level hard stop. The wiring uses OllamaEmbedder(timeout=5.0,
#: max_retries=1), so a healthy-but-slow call ends well inside this; the
#: bridge timeout is the backstop against pathological hangs.
_WORKER_TIMEOUT = 15.0
_DEFAULT_COOLDOWN = 60.0


class SyncEmbedder:
    def __init__(
        self,
        factory: Callable[[], Any] | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = _DEFAULT_COOLDOWN,
        timeout: float = _WORKER_TIMEOUT,
        down_state_file: Path | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cooldown = cooldown
        self._timeout = timeout
        self._down_until = 0.0
        self._down_file = down_state_file

    def embed_text(self, text: str) -> list[float] | None:
        return self._run(lambda emb: emb.embed(text))

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        return self._run(lambda emb: emb.embed_batch(texts))

    def _run(self, op: Callable[[Any], Any]):
        if self._factory is None:
            return None
        if self._clock() < self._down_until:
            return None
        if self._down_file is not None and self._file_down():
            return None

        factory = self._factory

        async def _go():
            emb = factory()
            try:
                return await op(emb)
            finally:
                aclose = getattr(emb, "aclose", None)
                if aclose is not None:
                    await aclose()

        try:
            return run_async_in_worker(_go, timeout=self._timeout)
        except Exception:
            self._down_until = self._clock() + self._cooldown
            self._write_down_file()
            return None

    def _file_down(self) -> bool:
        down = self._down_file
        if down is None:
            return False
        try:
            until = float(down.read_text(encoding="utf-8").strip())
        except Exception:
            return False
        return time.time() < until

    def _write_down_file(self) -> None:
        down = self._down_file
        if down is None:
            return
        try:
            down.parent.mkdir(parents=True, exist_ok=True)
            down.write_text(
                str(time.time() + self._cooldown), encoding="utf-8")
        except Exception:
            pass
