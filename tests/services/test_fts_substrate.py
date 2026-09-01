"""Pins the FTS5/BM25 evidence leg as unconditional.

After the Ollama/vector removal, BM25 is the only local evidence leg for
sqlite-storage retrieval. This test constructs services with NO embedder
(the only construction mode after this branch) and proves observe -> FTS
row -> BM25-evidenced retrieval works end to end.

EXPECTED GREEN from the moment it is written (verification pin, not
TDD red) -- see
``.superpowers/sdd/2026-09-01-remove-ollama-embeddings/task-1-brief.md``.

Deviation from the brief's illustrative pseudo-code
----------------------------------------------------
The brief named ``retrieve_relevant`` (``better_memory/services/relevant.py``)
as the retrieval leg to exercise together with ``ObservationService``.
That function only ever reads the ``reflections`` and ``semantic_memories``
tables (via ``backend.retrieve`` / ``backend.semantic_list``) -- it has no
code path that touches ``observations`` at all, so an
``observe() -> retrieve_relevant()`` test could never pass, regardless of
the FTS/embedder question, because the two subsystems don't share a table.

The actual end-to-end BM25 retrieval path for observations is
``ObservationService.retrieve()``, backed by
``better_memory.search.hybrid.hybrid_search`` -- a module whose own
docstring says it is "deliberately pure SQLite: it never calls the
embedder", and which falls back to trigram-FTS5 BM25 (``second_source=
"trigram"``) instead of sqlite-vec kNN as its second RRF leg exactly when
no embedder is present (see ``ObservationService.create``/``retrieve``:
``second_source = "vec0" if self._embedder is not None else "trigram"``).
That is the function this test exercises instead, to keep the assertion
shape (observe -> FTS row -> BM25-evidenced retrieval hit -> no embedder
anywhere) truthful to what the code actually does.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


async def test_observe_populates_fts_without_embedder(
    conn: sqlite3.Connection,
) -> None:
    """``ObservationService(conn, None, ...)`` -- no embedder constructed at
    all -- still populates ``observation_fts`` via the AFTER INSERT/UPDATE/
    DELETE triggers defined in 0001_init.sql / 0002_episodic.sql. FTS
    population is unconditional: it is not gated on any embeddings backend
    (see task-1 Step 1 grep evidence in the task report)."""
    svc = ObservationService(conn, None, episodes=EpisodeService(conn))
    await svc.create(
        content="growatt inverter polling uses Timespan.hour", project="p1"
    )

    fts_rows = conn.execute(
        "SELECT count(*) FROM observation_fts WHERE observation_fts MATCH 'growatt'"
    ).fetchone()[0]
    assert fts_rows >= 1


async def test_retrieve_returns_bm25_evidence_without_embedder(
    conn: sqlite3.Connection,
) -> None:
    """Same substrate as above; ``ObservationService.retrieve()`` must
    surface the observation on BM25 evidence alone -- word-FTS5 BM25 plus
    trigram-FTS5 BM25, both embedder-free -- when the service is
    constructed with no embedder (``second_source`` resolves to
    ``"trigram"``, never ``"vec0"``)."""
    svc = ObservationService(conn, None, episodes=EpisodeService(conn))
    await svc.create(
        content="growatt inverter polling uses Timespan.hour", project="p1"
    )

    results = await svc.retrieve(query="growatt inverter polling", project="p1")

    hits = results.do + results.dont + results.neutral
    assert any("growatt" in r.content.lower() for r in hits)
