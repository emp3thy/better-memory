from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.semantic import SemanticMemoryService
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _vec_count(conn):
    return conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0]


def _seed_active_observation(conn, *, obs_id="o1", project="p1",
                             content="bug found", episode_id=None):
    if episode_id is None:
        episode_id = "ep-default"
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "(?, ?, '2026-04-01T00:00:00+00:00')",
            (episode_id, project),
        )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "(?, ?, ?, ?, 'active', 'success', "
        "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
        (obs_id, content, project, episode_id),
    )
    conn.commit()


class TestSemanticEmbeddingWrite:
    def test_create_embeds_content(self, conn):
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, sync_embedder=SyncEmbedder(lambda: fake))
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 1
        assert fake.calls == ["the fact"]

    def test_update_text_reembeds(self, conn):
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, sync_embedder=SyncEmbedder(lambda: fake))
        mid = svc.create(content="v1", project="p")
        svc.update_text(id=mid, content="v2")
        assert _vec_count(conn) == 1          # replaced, not duplicated
        assert fake.calls == ["v1", "v2"]

    def test_failure_never_blocks_create(self, conn):
        svc = SemanticMemoryService(
            conn, sync_embedder=SyncEmbedder(lambda: FakeEmbedder(fail=True)))
        mid = svc.create(content="the fact", project="p")
        assert mid
        assert _vec_count(conn) == 0

    def test_no_embedder_no_rows(self, conn):
        svc = SemanticMemoryService(conn)
        svc.create(content="the fact", project="p")
        assert _vec_count(conn) == 0

    def test_promote_from_observation_embeds(self, conn):
        _seed_active_observation(conn, obs_id="o1", content="rule text")
        fake = FakeEmbedder()
        svc = SemanticMemoryService(conn, sync_embedder=SyncEmbedder(lambda: fake))
        svc.create_from_observation(observation_id="o1")
        assert _vec_count(conn) == 1
        assert fake.calls == ["rule text"]
