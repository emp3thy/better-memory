"""Unit tests for :mod:`better_memory.embeddings.ollama`.

These tests use :class:`httpx.MockTransport` to stub responses so they run
without Ollama. Async tests rely on ``asyncio_mode = "auto"`` from pyproject.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from better_memory.embeddings.ollama import EmbeddingError, OllamaEmbedder

# A deterministic 768-length vector used in stubbed responses.
_VEC_768 = [0.1] * 768
_VEC_512 = [0.1] * 512


def _make_client(handler: Any, *, base_url: str = "http://localhost:11434") -> httpx.AsyncClient:
    """Return an ``AsyncClient`` wired to a ``MockTransport`` of ``handler``."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url=base_url)


async def test_embed_sends_post_to_api_embed_with_correct_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(model="nomic-embed-text", client=client)
    try:
        out = await embedder.embed("hello")
    finally:
        await embedder.aclose()
        await client.aclose()

    assert out == _VEC_768
    assert captured["method"] == "POST"
    assert captured["url"] == "http://localhost:11434/api/embed"
    assert captured["body"] == {"model": "nomic-embed-text", "input": "hello"}


async def test_embed_returns_vector_from_response_body() -> None:
    distinctive = [float(i) / 1000 for i in range(768)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [distinctive]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        out = await embedder.embed("x")
    finally:
        await client.aclose()

    assert out == distinctive
    assert len(out) == 768


async def test_embed_dimension_mismatch_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [_VEC_512]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed("x")
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    assert "768" in msg
    assert "512" in msg


async def test_embed_batch_sends_single_request_with_list_input() -> None:
    captured: dict[str, Any] = {}
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"embeddings": [_VEC_768, _VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(model="nomic-embed-text", client=client)
    try:
        out = await embedder.embed_batch(["a", "b"])
    finally:
        await client.aclose()

    assert call_count == 1
    assert captured["body"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    assert len(out) == 2
    assert out[0] == _VEC_768
    assert out[1] == _VEC_768


async def test_embed_batch_empty_returns_empty_without_request() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "should not be called"})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        out = await embedder.embed_batch([])
    finally:
        await client.aclose()

    assert out == []
    assert call_count == 0


async def test_embed_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("better_memory.embeddings.ollama.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client, max_retries=3, backoff_base=0.5)
    try:
        out = await embedder.embed("hi")
    finally:
        await client.aclose()

    assert out == _VEC_768
    assert call_count == 2
    assert sleeps == [0.5]  # one retry => single backoff call at attempt=0


async def test_embed_retries_exhausted_raises_embedding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("better_memory.embeddings.ollama.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, json={"error": "nope"})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client, max_retries=3, backoff_base=0.5)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed("hi")
    finally:
        await client.aclose()

    # max_retries=3 means 3 attempts total, 2 backoffs in between.
    assert call_count == 3
    assert sleeps == [0.5, 1.0]
    assert "503" in str(excinfo.value)


async def test_embed_4xx_raises_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("better_memory.embeddings.ollama.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, json={"error": "model not found"})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client, max_retries=3)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed("hi")
    finally:
        await client.aclose()

    assert call_count == 1
    assert sleeps == []
    assert "400" in str(excinfo.value)
    assert "model not found" in str(excinfo.value)


async def test_embed_connect_error_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("better_memory.embeddings.ollama.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client, max_retries=3, backoff_base=0.5)
    try:
        out = await embedder.embed("hi")
    finally:
        await client.aclose()

    assert out == _VEC_768
    assert call_count == 2
    assert sleeps == [0.5]


async def test_embed_connect_error_exhausted_raises_embedding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("better_memory.embeddings.ollama.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("refused")

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client, max_retries=2, backoff_base=0.5)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed("hi")
    finally:
        await client.aclose()

    assert call_count == 2
    assert sleeps == [0.5]
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


async def test_embed_empty_string_still_calls_ollama() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        out = await embedder.embed("")
    finally:
        await client.aclose()

    assert out == _VEC_768
    assert captured["body"]["input"] == ""


async def test_embed_batch_dimension_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [_VEC_768, _VEC_512]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            await embedder.embed_batch(["a", "b"])
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    assert "768" in msg
    assert "512" in msg


async def test_injected_client_is_not_closed_by_aclose() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    embedder = OllamaEmbedder(client=client)
    try:
        await embedder.embed("hi")
        await embedder.aclose()
        # The injected client must still be usable.
        assert not client.is_closed
    finally:
        await client.aclose()


async def test_context_manager_delegates_to_aclose() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    client = _make_client(handler)
    async with OllamaEmbedder(client=client) as embedder:
        out = await embedder.embed("x")
    assert out == _VEC_768
    # Injected client survives.
    assert not client.is_closed
    await client.aclose()


async def test_defaults_come_from_get_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://example.invalid:9999")
    monkeypatch.setenv("EMBED_MODEL", "custom-model")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"embeddings": [_VEC_768]})

    # We still inject the transport (so no real network), but omit host/model
    # to verify they're read from config.
    client = _make_client(handler, base_url="http://example.invalid:9999")
    embedder = OllamaEmbedder(client=client)
    try:
        await embedder.embed("hi")
    finally:
        await client.aclose()

    assert captured["url"] == "http://example.invalid:9999/api/embed"
    assert captured["body"]["model"] == "custom-model"
