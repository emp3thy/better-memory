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


class DirectedEmbedder(FakeEmbedder):
    """Maps texts containing any trigger phrase to one vector; noise else.

    Lets a test make the query and one reflection/semantic memory
    'semantically identical' while sharing zero tokens -- isolating the
    vec leg from BM25/keyword matching. Shared here (was previously
    private to test_vec_fusion.py) so any embedding-path test can use it.
    """

    def __init__(self, *triggers: str):
        super().__init__()
        self.triggers = triggers

    def _vec(self, text: str) -> list[float]:
        if any(t in text for t in self.triggers):
            return [1.0] + [0.0] * 767
        return [0.0, 1.0] + [0.0] * 766

    async def embed(self, text):
        self.calls.append(text)
        return self._vec(text)

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]
