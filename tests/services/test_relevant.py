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

    def test_semantic_fallback_keyword_when_conn_none_agentcore(self, conn):
        """agentcore mode: conn=None but the embedder is healthy and qvec is
        real. The vec candidate set is still structurally empty because vec
        queries need the sqlite conn -- so the keyword fallback must fire
        here too, not only when qvec itself is None."""
        _seed_semantic(conn, "s1", content="repo uses uv run pytest on windows")
        emb = DirectedEmbedder("unrelated trigger phrase")
        sync_embedder = SyncEmbedder(lambda: emb)
        backend = _backend(conn, sync_embedder=sync_embedder)
        out = retrieve_relevant(
            backend, query="uv run pytest windows setup", project="p",
            conn=None, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
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


@pytest.mark.parametrize("blank_query", ["", "   ", "\t\n"])
def test_blank_query_returns_empty(conn, blank_query):
    # DirectedEmbedder with no trigger args maps EVERY text to the same
    # unit vector, so it would match anything if the vec leg were reached --
    # the point is that a contentless query short-circuits before that.
    title = "Retention archives by confidence"
    emb = DirectedEmbedder()
    _seed_reflection(conn, "r1", title=title)
    _embed_reflection(conn, "r1", emb._vec(title))
    sync_embedder = SyncEmbedder(lambda: emb)
    backend = _backend(conn, sync_embedder=sync_embedder)
    out = retrieve_relevant(
        backend, query=blank_query, project="p",
        conn=conn, sync_embedder=sync_embedder, now=lambda: FIXED_NOW,
    )
    assert out == []
    assert emb.calls == []


class _StubAgentCoreBackend:
    """Minimal agentcore-flavored stub for retrieve_relevant's agentcore
    evidence-gate branch, isolated from the real AgentCoreBackend (which
    needs boto3 mocks -- see tests/storage/test_agentcore_unit.py for
    relevance_ranks's own wire-shape tests). Exposes exactly the surface
    retrieve_relevant touches: retrieve / semantic_list / relevance_ranks
    / supports_synthesis=False (the capability flag that gates the branch
    on, alongside conn=None)."""

    supports_synthesis = False

    def __init__(self, *, reflections=None, semantics=None, rank_map=None,
                 return_none=False, raise_on_ranks=False):
        """``rank_map``: the dict relevance_ranks returns on success
        (defaults to ``{}`` -- a legitimate "ran fine, nothing matched"
        result). ``return_none=True`` simulates the REAL AgentCoreBackend's
        own best-effort contract for a failed lookup (returns ``None``
        directly, never raises). ``raise_on_ranks=True`` additionally
        exercises retrieve_relevant's own defensive try/except around the
        call (belt-and-suspenders, since a real backend is not supposed to
        raise at all)."""
        self._reflections = reflections or {"do": [], "dont": [], "neutral": []}
        self._semantics = semantics or []
        self._rank_map = rank_map if rank_map is not None else {}
        self._return_none = return_none
        self._raise_on_ranks = raise_on_ranks

    def retrieve(self, **kwargs):
        return self._reflections

    def semantic_list(self, **kwargs):
        return self._semantics

    def relevance_ranks(self, *, query, kinds=("reflection", "semantic"), top_k=50):
        if self._raise_on_ranks:
            raise RuntimeError("AWS boom")
        if self._return_none:
            return None
        return dict(self._rank_map)


def _stub_reflection(rid, *, title, use_cases="", useful=0, overlooked=0, ignored=0):
    return {
        "id": rid, "title": title, "use_cases": use_cases, "hints": [],
        "confidence": 0.5, "useful_count": useful,
        "times_overlooked": overlooked, "times_ignored": ignored,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


class TestAgentCoreRelevanceGate:
    """conn=None + supports_synthesis=False routes retrieve_relevant
    through backend.relevance_ranks instead of the BM25/vec legs (which
    are structurally unavailable with no sqlite conn -- see
    services/relevant.py's agentcore_mode gate)."""

    def test_backend_rank_membership_qualifies_without_token_overlap(self):
        # Zero shared tokens between title and query -- the old
        # keyword-hit fallback would never qualify this; only the backend
        # rank map's membership does, mirroring the vec-gate's
        # DirectedEmbedder scenario but with no embedder involved at all.
        reflections = {
            "do": [_stub_reflection("r1", title="Zebra flamingo unrelated topic")],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(
            reflections=reflections, rank_map={("reflection", "r1"): 0},
        )
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("reflection", "r1")]

    def test_backend_rank_membership_qualifies_semantic_without_token_overlap(self):
        from better_memory.services.semantic import SemanticMemory

        semantics = [SemanticMemory(
            id="s1", content="unrelated flamingo content", project="p",
            scope="project", created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )]
        backend = _StubAgentCoreBackend(
            semantics=semantics, rank_map={("semantic", "s1"): 0},
        )
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("semantic", "s1")]

    def test_zero_match_rank_map_does_not_fall_back_to_keywords(self):
        # relevance_ranks returns {} -- the lookup RAN and genuinely found
        # nothing. Even though the reflection has strong keyword overlap
        # with the query, it must NOT qualify: a legitimate negative
        # result from the server-side gate is not a "leg unavailable"
        # situation, so the keyword-hit floor must not override it (design
        # spec 2026-07-24-agentcore-parity-design.md §3 -- conflating {}
        # with an AWS error was the bug this test pins).
        reflections = {
            "do": [_stub_reflection(
                "r1", title="Retention thresholds alpha",
                use_cases="retention thresholds tuning guide",
            )],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(reflections=reflections, rank_map={})
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert out == []

    def test_none_rank_map_falls_back_to_keywords(self):
        # relevance_ranks returns None -- the REAL AgentCoreBackend's own
        # signal that the lookup itself failed (AWS error on every
        # namespace/kind), not that it found nothing. THIS is the only
        # case the keyword-hit floor should fire for in agentcore mode,
        # matching the pre-existing conn=None sqlite behaviour
        # (test_no_conn_falls_back_to_keywords).
        reflections = {
            "do": [_stub_reflection(
                "r1", title="Retention thresholds alpha",
                use_cases="retention thresholds tuning guide",
            )],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(reflections=reflections, return_none=True)
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("reflection", "r1")]

    def test_no_matching_evidence_no_injection(self):
        # Empty rank map AND no keyword overlap -- nothing qualifies
        # regardless of the None/{} distinction.
        reflections = {
            "do": [_stub_reflection("r1", title="Zebra flamingo unrelated topic")],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(reflections=reflections, rank_map={})
        out = retrieve_relevant(
            backend, query="how does retention archive things", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert out == []

    def test_relevance_ranks_error_degrades_to_keyword_fallback(self):
        # relevance_ranks raising must never propagate out of
        # retrieve_relevant -- it degrades exactly like a None return
        # (retrieve_relevant's own try/except around the call).
        reflections = {
            "do": [_stub_reflection(
                "r1", title="Retention thresholds alpha",
                use_cases="retention thresholds tuning guide",
            )],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(
            reflections=reflections, raise_on_ranks=True,
        )
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [(m.kind, m.id) for m in out] == [("reflection", "r1")]

    def test_wilson_still_ranks_not_qualifies_in_agentcore_mode(self):
        # Both reflections are present in the rank map (both qualify), but
        # Wilson still decides ranking among qualifiers via RRF -- a
        # popular-but-irrelevant reflection absent from the rank map must
        # NOT be injected just because it's popular.
        reflections = {
            "do": [
                _stub_reflection("r-hi", title="Retention thresholds alpha",
                                  useful=5, ignored=1),
                _stub_reflection("r-lo", title="Retention thresholds beta",
                                  useful=0, ignored=6),
                _stub_reflection("r-popular-irrelevant",
                                  title="Zebra flamingo unrelated topic",
                                  useful=50),
            ],
            "dont": [], "neutral": [],
        }
        backend = _StubAgentCoreBackend(
            reflections=reflections,
            rank_map={("reflection", "r-hi"): 0, ("reflection", "r-lo"): 1},
        )
        out = retrieve_relevant(
            backend, query="retention thresholds", project="p",
            conn=None, now=lambda: FIXED_NOW,
        )
        assert [m.id for m in out] == ["r-hi", "r-lo"]


def test_returns_relevantmemory(conn):
    _seed_reflection(conn, "r1", title="Retention archives by confidence")
    backend = _backend(conn)
    out = retrieve_relevant(
        backend, query="how does retention archive things", project="p",
        conn=conn, now=lambda: FIXED_NOW,
    )
    assert out and all(isinstance(m, RelevantMemory) for m in out)
