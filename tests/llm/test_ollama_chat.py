"""Tests for better_memory.llm.ollama."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from better_memory.llm.ollama import ChatError, OllamaChat


class _StubTransport(httpx.MockTransport):
    """Record requests and return canned responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            self.calls.append({"url": str(request.url), "body": body})
            if not self._responses:
                return httpx.Response(500, text="unexpected call")
            spec = self._responses.pop(0)
            return httpx.Response(
                spec.get("status", 200),
                json=spec.get("json", {}),
                headers=spec.get("headers", {}),
            )

        super().__init__(handler)


@pytest.fixture
def transport_factory() -> Iterator[Any]:
    def _make(responses: list[dict[str, Any]]) -> _StubTransport:
        return _StubTransport(responses)

    yield _make


class TestOllamaChat:
    async def test_complete_returns_response_text(
        self, transport_factory
    ) -> None:
        transport = transport_factory(
            [{"json": {"response": "hello world", "done": True}}]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="llama3", client=client)
            out = await chat.complete("say hi")
            assert out == "hello world"

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["url"].endswith("/api/generate")
        assert call["body"]["model"] == "llama3"
        assert call["body"]["prompt"] == "say hi"
        assert call["body"]["stream"] is False

    async def test_complete_retries_on_5xx(self, transport_factory) -> None:
        transport = transport_factory(
            [
                {"status": 503, "json": {"error": "overloaded"}},
                {"json": {"response": "ok", "done": True}},
            ]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(
                model="llama3",
                client=client,
                backoff_base=0.0,
                max_retries=3,
            )
            out = await chat.complete("hi")
            assert out == "ok"
            assert len(transport.calls) == 2

    async def test_complete_does_not_retry_on_4xx(
        self, transport_factory
    ) -> None:
        transport = transport_factory(
            [{"status": 404, "json": {"error": "model not found"}}]
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="missing", client=client)
            with pytest.raises(ChatError) as exc:
                await chat.complete("hi")
            assert "404" in str(exc.value)
        assert len(transport.calls) == 1

    async def test_missing_response_field_raises(
        self, transport_factory
    ) -> None:
        transport = transport_factory([{"json": {"done": True}}])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:11434"
        ) as client:
            chat = OllamaChat(model="llama3", client=client)
            with pytest.raises(ChatError):
                await chat.complete("hi")
