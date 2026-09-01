"""Ollama embedding client.

Wraps the local Ollama HTTP API's ``/api/embed`` endpoint. Exposes a single
:class:`OllamaEmbedder` class with async ``embed`` / ``embed_batch`` methods.

Design choices:
    * Uses the newer ``POST /api/embed`` endpoint which supports batching
      natively (request body ``{"model": "...", "input": "text" | [...]}`` and
      response body ``{"embeddings": [[...], ...]}``).
    * Every returned vector is length-checked against ``expected_dim`` (768 for
      ``nomic-embed-text``). A mismatch raises :class:`EmbeddingError` rather
      than silently padding / truncating.
    * Transient transport failures (``httpx.TransportError`` — connect errors,
      connect/read timeouts, protocol errors, read errors, etc.) and 5xx
      responses are retried with exponential backoff. 4xx responses fail
      immediately — they indicate a configuration error (missing model, bad
      request body) that retrying will not fix.
    * Any ``httpx.HTTPError`` that escapes the specific branches is wrapped in
      :class:`EmbeddingError` so callers only ever need to catch a single
      type. Exception chaining via ``raise ... from exc`` preserves the
      original cause.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import uuid4

import httpx

from better_memory import _diag

# Diagnostic logging for embed calls. Off by default; enable via
# Config.diag_logging (BETTER_MEMORY_DIAG_LOGGING) to localize
# hang-on-first-call freezes that the MCP server has hit intermittently on
# Windows. Each ``_post_embed`` call writes a paired ``[bm-embed start ...]``
# / ``[bm-embed done ...]`` line via the shared diagnostic logger (stderr +
# ``{home}/logs/diag.log``) with a short call-id, attempt counter, and
# elapsed ms. A ``start`` line with no matching ``done`` localises the hang
# to ``httpx.AsyncClient.post()``.
from better_memory._diag import log as _embed_log

#: Module-level defaults for ``host``/``model`` when the caller omits them.
#: No longer sourced from :class:`~better_memory.config.Config` — this
#: package is scheduled for deletion (remove-ollama-embeddings epic), so the
#: two config fields backing these were dropped from ``Config`` rather than
#: threaded through here.
_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "nomic-embed-text"


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails."""


class OllamaEmbedder:
    """Async embedding client for a local Ollama server.

    Parameters
    ----------
    host:
        Base URL of the Ollama server. Defaults to ``"http://localhost:11434"``.
    model:
        Name of the embedding model. Defaults to ``"nomic-embed-text"``.
    timeout:
        Per-request timeout in seconds.
    max_retries:
        Maximum number of attempts per call (not retries *in addition to* the
        first attempt — a value of ``3`` means up to three total attempts).
    backoff_base:
        Base for exponential backoff. Sleep between attempt ``i`` and ``i+1``
        is ``backoff_base * 2**i`` seconds.
    expected_dim:
        Required length of every returned embedding vector. Defaults to 768 to
        match ``nomic-embed-text``.
    client:
        Optional pre-built :class:`httpx.AsyncClient`. When supplied, the
        caller owns its lifecycle; :meth:`aclose` will not close it. When
        omitted, the embedder creates its own client and closes it in
        :meth:`aclose`.
    """

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        expected_dim: int = 768,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")

        self._host = host if host is not None else _DEFAULT_HOST
        self._model = model if model is not None else _DEFAULT_MODEL
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._expected_dim = expected_dim

        if client is None:
            self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    # ------------------------------------------------------------------ public
    async def embed(self, text: str) -> list[float]:
        """Return a single embedding vector for ``text``."""
        with _diag.trace("OllamaEmbedder.embed", text_len=len(text)):
            embeddings = await self._post_embed(text)
            if len(embeddings) != 1:
                raise EmbeddingError(
                    f"Expected 1 embedding for single input, got {len(embeddings)}"
                )
            vec = embeddings[0]
            self._check_dim(vec)
            return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input string.

        An empty ``texts`` list returns ``[]`` without issuing an HTTP request.
        """
        with _diag.trace("OllamaEmbedder.embed_batch", n=len(texts)):
            if not texts:
                return []

            embeddings = await self._post_embed(texts)
            if len(embeddings) != len(texts):
                raise EmbeddingError(
                    f"Expected {len(texts)} embeddings, got {len(embeddings)}"
                )
            for vec in embeddings:
                self._check_dim(vec)
            return embeddings

    async def aclose(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OllamaEmbedder:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ----------------------------------------------------------------- helpers
    async def _post_embed(self, payload_input: str | list[str]) -> list[list[float]]:
        """Issue ``POST /api/embed`` with retries, return the ``embeddings`` list."""
        body: dict[str, Any] = {"model": self._model, "input": payload_input}

        call_id = uuid4().hex[:8]
        n = 1 if isinstance(payload_input, str) else len(payload_input)
        _embed_log(
            f"[bm-embed start cid={call_id} model={self._model} host={self._host} "
            f"n={n} ts={datetime.now(UTC).isoformat()}]"
        )
        t_call = time.monotonic()

        def _done(status: str) -> None:
            attempt_ms = int((time.monotonic() - t_attempt) * 1000)
            total_ms = int((time.monotonic() - t_call) * 1000)
            _embed_log(
                f"[bm-embed done cid={call_id} status={status} "
                f"attempt_ms={attempt_ms} total_ms={total_ms}]"
            )

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            t_attempt = time.monotonic()
            _embed_log(
                f"[bm-embed attempt cid={call_id} n_attempt={attempt + 1}/"
                f"{self._max_retries}]"
            )
            # Log right before AND right after the httpx.post call so we can
            # tell whether a hang sits inside the network round-trip or
            # somewhere on either side (event-loop handoff, retry sleep, etc).
            # Also capture the current asyncio loop id so we can spot the
            # "client bound to a different loop than the caller" deadlock
            # documented in reflection 7a4fdb8e.
            try:
                loop_id = id(asyncio.get_running_loop())
            except RuntimeError:
                loop_id = -1
            _embed_log(
                f"[bm-embed pre-post cid={call_id} loop_id={loop_id} "
                f"client_loop_id={id(getattr(self._client, '_state', None))}]"
            )
            try:
                response = await self._client.post("/api/embed", json=body)
                _embed_log(
                    f"[bm-embed post-returned cid={call_id} "
                    f"status={response.status_code} "
                    f"elapsed_ms={int((time.monotonic() - t_attempt) * 1000)}]"
                )
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 >= self._max_retries:
                    _done("transport_error_final")
                    raise EmbeddingError(
                        f"Failed to reach Ollama at {self._host} after "
                        f"{self._max_retries} attempts: {exc}"
                    ) from exc
                _done("transport_error_retry")
                await asyncio.sleep(self._backoff_base * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                # Any other httpx error (e.g. InvalidURL, UnsupportedProtocol,
                # a non-transport RequestError subclass) is surfaced as an
                # EmbeddingError so callers never see raw httpx exceptions.
                _done("http_error")
                raise EmbeddingError(
                    f"Unexpected httpx error talking to Ollama: {exc}"
                ) from exc
            except BaseException as exc:
                # Diagnostic last-resort: name the exception type that escapes
                # the post call but isn't covered by the handlers above (e.g.
                # asyncio.CancelledError, RuntimeError from wrong-loop bind).
                # Logs the type AND the wallclock timestamp so we can measure
                # any gap between the raise and the final embed exit log.
                _embed_log(
                    f"[bm-embed escaped cid={call_id} "
                    f"type={type(exc).__name__} "
                    f"msg={exc!r} "
                    f"elapsed_ms={int((time.monotonic() - t_attempt) * 1000)} "
                    f"ts={datetime.now(UTC).isoformat()}]"
                )
                _done(f"escaped_{type(exc).__name__}")
                raise

            if 500 <= response.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    f"{response.status_code} {response.reason_phrase}",
                    request=response.request,
                    response=response,
                )
                if attempt + 1 >= self._max_retries:
                    _done(f"http_{response.status_code}_final")
                    raise EmbeddingError(
                        f"Ollama returned {response.status_code} after "
                        f"{self._max_retries} attempts: {response.text}"
                    ) from last_exc
                _done(f"http_{response.status_code}_retry")
                await asyncio.sleep(self._backoff_base * (2**attempt))
                continue

            if 400 <= response.status_code < 500:
                _done(f"http_{response.status_code}")
                raise EmbeddingError(
                    f"Ollama returned {response.status_code}: {response.text}"
                )

            # 2xx — parse and return.
            try:
                data = response.json()
            except ValueError as exc:
                _done("non_json")
                raise EmbeddingError(
                    f"Ollama returned non-JSON body: {response.text!r}"
                ) from exc

            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list):
                _done("missing_embeddings")
                raise EmbeddingError(
                    f"Ollama response missing 'embeddings' list: {data!r}"
                )
            _done("ok")
            return embeddings  # type: ignore[no-any-return]

        # Should be unreachable — the loop always either returns or raises.
        raise EmbeddingError(
            f"Exhausted {self._max_retries} retries without a response"
        ) from last_exc

    def _check_dim(self, vec: list[float]) -> None:
        if len(vec) != self._expected_dim:
            raise EmbeddingError(
                f"Embedding dimension mismatch: expected {self._expected_dim}, "
                f"got {len(vec)}"
            )
