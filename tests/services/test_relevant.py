"""Contextual relevance: BM25 + vec + Wilson prior behind an evidence gate.

The gate is the point: a memory injects only with positive relevance
evidence (BM25 match on the query, or vec cosine >= floor). The Wilson
prior RANKS qualifiers but can never qualify a memory alone — popularity
must not force irrelevant injections (that failure mode measured 13% useful
as bootstrap). Vectors are unit-norm, so cosine >= c is L2 dist^2 <= 2(1-c).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.relevant import RelevantMemory, retrieve_relevant
from better_memory.storage.sqlite import SqliteBackend
from tests.services._embedding_fakes import DirectedEmbedder, FakeEmbedder

FIXED_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _backend(conn, *, sync_embedder=None, project="p"):
    return SqliteBackend(
        memory_conn=conn, sync_embedder=sync_embedder,
        session_id=None, project=project,
    )


def _seed_reflection(
    conn, rid, *, title, use_cases="uc", hints="[]",
    useful=0, overlooked=0, ignored=0, polarity="do",
    updated_at="2026-01-01T00:00:00+00:00",
):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at,
            useful_count, times_overlooked, times_ignored)
           VALUES (?, ?, 'p', 'general', ?, ?, ?, 0.5,
                   '2026-01-01T00:00:00+00:00', ?, ?, ?, ?)""",
        (rid, title, polarity, use_cases, hints, updated_at,
         useful, overlooked, ignored),
    )
    conn.commit()


def _seed_semantic(
    conn, sid, *, content, useful=0, overlooked=0, ignored=0,
    updated_at="2026-01-01T00:00:00+00:00",
):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at,
            useful_count, times_overlooked, times_ignored)
           VALUES (?, ?, 'p', 'project', '2026-01-01T00:00:00+00:00', ?,
                   ?, ?, ?)""",
        (sid, content, updated_at, useful, overlooked, ignored),
    )
    conn.commit()


def _embed_reflection(conn, rid, vector):
    conn.execute(
        "INSERT INTO reflection_embeddings (reflection_id, embedding) VALUES (?, ?)",
        (rid, sqlite_vec.serialize_float32(vector)),
    )
    conn.commit()


def _embed_semantic(conn, sid, vector):
    conn.execute(
        "INSERT INTO semantic_embeddings (memory_id, embedding) VALUES (?, ?)",
        (sid, sqlite_vec.serialize_float32(vector)),
    )
    conn.commit()


class TestBM25Gate:
    def test_bm25_match_qualifies(self, conn):
        _seed_reflection(conn, "r1", title="Retention archives by confidence")
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=conn, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r1"]

    def test_no_evidence_no_injection(self, conn):
        _seed_reflection(conn, "r1", title="Zebra flamingo unrelated topic", useful=50)
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=conn, now=lambda: FIXED_NOW,
        )
        assert out == []


class TestVecGate:
    def test_vec_match_qualifies_without_token_overlap(self, conn):
        # DirectedEmbedder maps EITHER trigger phrase to the same unit
        # vector, so the title (matching via "Stdout handling") and the
        # query (matching via "console output") land at cosine 1 despite
        # sharing zero tokens -- isolating the vec leg from BM25.
        title = "Stdout handling on win32 interpreters"
        emb = DirectedEmbedder("Stdout handling", "console output")
        _seed_reflection(conn, "r1", title=title)
        _embed_reflection(conn, "r1", emb._vec(title))
        sync_embedder = SyncEmbedder(lambda: emb)
        backend = _backend(conn, sync_embedder=sync_embedder)
        out = retrieve_relevant(
            backend, query="console output disappears on windows", project="p",
            conn=conn, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r1"]

    def test_vec_below_floor_does_not_qualify(self, conn):
        # Orthogonal unit vectors: the title's trigger is absent from the
        # query, so DirectedEmbedder's else-branch gives the query the
        # noise vector [0,1,...] while the stored embedding is [1,0,...].
        title = "Zebra flamingo unrelated topic"
        emb = DirectedEmbedder("only-in-the-title-xyz")
        _seed_reflection(conn, "r1", title=title)
        _embed_reflection(conn, "r1", emb._vec("only-in-the-title-xyz"))
        sync_embedder = SyncEmbedder(lambda: emb)
        backend = _backend(conn, sync_embedder=sync_embedder)
        out = retrieve_relevant(
            backend, query="totally different wording", project="p",
            conn=conn, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
        )
        assert out == []


class TestWilsonRanking:
    def test_wilson_ranks_among_qualifiers(self, conn):
        _seed_reflection(conn, "r-hi", title="Retention thresholds alpha",
                          useful=5, ignored=1)
        _seed_reflection(conn, "r-lo", title="Retention thresholds beta",
                          useful=0, ignored=6)
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=conn, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r-hi", "r-lo"]


class TestSemantics:
    def test_semantic_qualifies_via_vec(self, conn):
        content = "Stdout handling on win32 interpreters"
        emb = DirectedEmbedder("Stdout handling", "console output")
        _seed_semantic(conn, "s1", content=content)
        _embed_semantic(conn, "s1", emb._vec(content))
        sync_embedder = SyncEmbedder(lambda: emb)
        backend = _backend(conn, sync_embedder=sync_embedder)
        out = retrieve_relevant(
            backend, query="console output disappears on windows", project="p",
            conn=conn, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("semantic", "s1")]

    def test_semantic_fallback_keyword_when_no_embedder(self, conn):
        _seed_semantic(conn, "s1", content="repo uses uv run pytest on windows")
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="uv run pytest windows setup", project="p",
            conn=conn, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("semantic", "s1")]


class TestCapsAndDegradation:
    def test_max_items_cap(self, conn):
        for i in range(5):
            _seed_reflection(conn, f"r{i}", title=f"Retention thresholds variant {i}")
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=conn, max_items=3, now=lambda: FIXED_NOW,
        )
        assert len(out) == 3

    def test_no_conn_falls_back_to_keywords(self, conn):
        _seed_reflection(conn, "r-hi", title="Retention thresholds alpha",
                          useful=5, ignored=1)
        _seed_reflection(conn, "r-lo", title="Retention thresholds beta",
                          useful=0, ignored=6)
        backend = _backend(conn)
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r-hi", "r-lo"]

    def test_embedder_failure_degrades_to_bm25(self, conn):
        _seed_reflection(conn, "r1", title="Retention archives by confidence")
        sync_embedder = SyncEmbedder(lambda: FakeEmbedder(fail=True))
        backend = _backend(conn, sync_embedder=sync_embedder)
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=conn, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r1"]


def test_returns_relevantmemory(conn):
    _seed_reflection(conn, "r1", title="Retention archives by confidence")
    backend = _backend(conn)
    out = retrieve_relevant(
        backend, query="how does retention archive things", project="p",
        conn=conn, now=lambda: FIXED_NOW,
    )
    assert out and all(isinstance(m, RelevantMemory) for m in out)
