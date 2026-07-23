"""Query fusion gains a vector leg; missing embeddings self-heal on retrieve.

Degradation contract: no embedder / breaker open / row unembedded -> exactly
the two-leg (prior + BM25) behaviour that shipped in #81.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.reflection import ReflectionSynthesisService
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid, *, title, useful=0, ignored=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?, ?)""",
        (rid, title, useful, ignored),
    )
    conn.commit()


class DirectedEmbedder(FakeEmbedder):
    """Maps texts containing any trigger phrase to one vector; noise else.

    Lets a test make the query and one reflection 'semantically identical'
    while sharing zero tokens — isolating the vec leg from BM25.
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


def _svc(conn, embedder):
    return ReflectionSynthesisService(
        conn, sync_embedder=SyncEmbedder(lambda: embedder))


class TestVecFusion:
    def test_semantic_match_promoted_without_token_overlap(self, conn):
        _seed(conn, "r-target", title="Stdout handling on win32 interpreters")
        _seed(conn, "r-noise-1", title="Unrelated advice alpha", useful=5, ignored=1)
        _seed(conn, "r-noise-2", title="Unrelated advice beta", useful=4, ignored=1)
        emb = DirectedEmbedder("Stdout handling", "console output")
        svc = _svc(conn, emb)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="console output disappears on windows",
        )["do"]]
        assert ids[0] == "r-target"

    def test_no_embedder_matches_shipped_behaviour(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = ReflectionSynthesisService(conn)
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]

    def test_embedder_failure_degrades_silently(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful=1)
        svc = _svc(conn, FakeEmbedder(fail=True))
        ids = [r["id"] for r in svc.retrieve_reflections(
            project="p", query="retention")["do"]]
        assert ids == ["r-a"]


class TestSelfHeal:
    def test_unembedded_candidates_healed_on_query_retrieve(self, conn):
        _seed(conn, "r-a", title="Alpha")
        _seed(conn, "r-b", title="Beta")
        svc = _svc(conn, FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything at all")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 2

    def test_heal_capped_at_batch_limit(self, conn):
        from better_memory.services.reflection import SELF_HEAL_BATCH_CAP
        for i in range(SELF_HEAL_BATCH_CAP + 5):
            _seed(conn, f"r-{i:03}", title=f"Title {i}")
        svc = _svc(conn, FakeEmbedder())
        svc.retrieve_reflections(project="p", query="anything")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == SELF_HEAL_BATCH_CAP

    def test_no_query_no_heal_and_no_embed_calls(self, conn):
        _seed(conn, "r-a", title="Alpha")
        fake = FakeEmbedder()
        svc = _svc(conn, fake)
        svc.retrieve_reflections(project="p")
        n = conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]
        assert n == 0
        assert fake.calls == []

    def test_heal_failure_silent(self, conn):
        _seed(conn, "r-a", title="Alpha")
        svc = _svc(conn, FakeEmbedder(fail=True))
        rows = svc.retrieve_reflections(project="p", query="anything")
        assert rows["do"]
