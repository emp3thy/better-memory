"""Shared fake embedder for embedding-path tests."""
from __future__ import annotations


class FakeEmbedder:
    def __init__(self, fail: bool = False):
        self.calls: list = []
        self.closed = 0
        self.fail = fail

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("ollama down")
        return [0.1] * 768

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("ollama down")
        return [[0.1] * 768 for _ in texts]

    async def aclose(self) -> None:
        self.closed += 1
