"""Ollama chat/generation client for consolidation drafting.

Wraps the ``/api/generate`` endpoint. Pattern mirrors
:class:`better_memory.embeddings.ollama.OllamaEmbedder`:

- httpx AsyncClient with configurable host.
- Exponential backoff on transient (5xx, transport) failures.
- 4xx fails immediately; wrapped in :class:`ChatError`.
- Optional externally-owned client (caller handles lifecycle).
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

import httpx

from better_memory.config import get_config


class ChatError(RuntimeError):
    """Raised when chat generation fails."""


class OllamaChat:
    """Async chat/generation client for a local Ollama server."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")

        cfg = get_config()
        self._host = host if host is not None else cfg.ollama_host
        self._model = model if model is not None else cfg.consolidate_model
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

        if client is None:
            self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    async def complete(self, prompt: str) -> str:
        """Return a single completion for ``prompt``."""
        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.post("/api/generate", json=body)
            except httpx.TransportError as exc:
                last_exc = exc
                await self._sleep_for_attempt(attempt)
                continue
            except httpx.HTTPError as exc:
                # Non-transport httpx errors (InvalidURL, etc.) — don't retry;
                # wrap into ChatError for a single catch surface.
                raise ChatError(f"Ollama chat failed: {exc}") from exc

            if 500 <= resp.status_code < 600:
                err = ChatError(
                    f"Ollama returned {resp.status_code}: {resp.text[:200]}"
                )
                if attempt + 1 >= self._max_retries:
                    raise err
                last_exc = err
                await self._sleep_for_attempt(attempt)
                continue

            if resp.status_code >= 400:
                raise ChatError(
                    f"Ollama returned {resp.status_code}: {resp.text[:200]}"
                )

            data: dict[str, Any] = resp.json()
            if "response" not in data:
                raise ChatError(
                    f"Ollama response missing 'response' field: {data!r}"
                )
            return data["response"]

        raise ChatError(
            f"Ollama chat failed after {self._max_retries} attempts"
        ) from last_exc

    async def _sleep_for_attempt(self, attempt: int) -> None:
        if attempt + 1 < self._max_retries:
            await asyncio.sleep(self._backoff_base * (2**attempt))

    async def aclose(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OllamaChat":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
