"""SyncEmbedder: sync facade over the async embedder for thread-bound code.

Fresh embedder per call (loop-bound AsyncClient), closed in the worker;
60s circuit breaker so an Ollama outage costs one stall per cooldown
window instead of one per call.
"""
from __future__ import annotations

from better_memory.embeddings.sync_embed import SyncEmbedder
from tests.services._embedding_fakes import FakeEmbedder


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestSyncEmbedder:
    def test_embed_text_returns_vector_and_closes_embedder(self):
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake)
        vec = s.embed_text("hello")
        assert vec is not None and len(vec) == 768
        assert fake.calls == ["hello"]
        assert fake.closed == 1

    def test_embed_batch_returns_vectors(self):
        fake = FakeEmbedder()
        s = SyncEmbedder(lambda: fake)
        out = s.embed_batch(["a", "b"])
        assert out is not None and len(out) == 2

    def test_failure_returns_none_and_opens_breaker(self):
        fake = FakeEmbedder(fail=True)
        clock = FakeClock()
        s = SyncEmbedder(lambda: fake, clock=clock)
        assert s.embed_text("x") is None
        assert fake.calls == ["x"]
        # Breaker open: the embedder is not touched again.
        assert s.embed_text("y") is None
        assert fake.calls == ["x"]

    def test_breaker_closes_after_cooldown(self):
        clock = FakeClock()
        calls = []

        class FlakyThenGood(FakeEmbedder):
            async def embed(self, text):
                calls.append(text)
                if len(calls) == 1:
                    raise RuntimeError("down")
                return [0.1] * 768

        s = SyncEmbedder(FlakyThenGood, clock=clock, cooldown=60.0)
        assert s.embed_text("first") is None
        clock.t += 61.0
        assert s.embed_text("second") is not None
        assert calls == ["first", "second"]

    def test_none_factory_disables_everything(self):
        s = SyncEmbedder(None)
        assert s.embed_text("x") is None
        assert s.embed_batch(["x"]) is None

    def test_embedder_without_aclose_is_fine(self):
        class Bare:
            async def embed(self, text):
                return [0.1] * 768

        s = SyncEmbedder(Bare)
        assert s.embed_text("x") is not None
