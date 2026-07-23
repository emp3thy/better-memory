"""Synthesis writes reflection embeddings, best-effort.

reflection_embeddings sat at 0 rows from migration 0002 until this change:
the write path simply never embedded. Failures must never block synthesis.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import (
    AugmentAction,
    MergeAction,
    NewAction,
    ReflectionService,
    ReflectionSynthesisService,
    _embedding_source_text,
)
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _vec_count(conn):
    return conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0]


def _insert_obs(
    conn,
    *,
    obs_id: str,
    project: str,
    episode_id: str,
    outcome: str = "success",
    content: str = "obs content",
    component: str | None = None,
    theme: str | None = None,
    tech: str | None = None,
    created_at: str = "2026-04-22T09:00:00+00:00",
    status: str = "active",
    status_changed_at: str | None = None,
) -> None:
    if status_changed_at is None:
        status_changed_at = created_at
    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, outcome,
            reinforcement_score, episode_id, tech, created_at, status,
            status_changed_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)
        """,
        (obs_id, content, project, component, theme, outcome,
         episode_id, tech, created_at, status, status_changed_at),
    )


def _insert_reflection(
    conn,
    *,
    refl_id: str,
    project: str,
    phase: str = "general",
    polarity: str = "do",
    status: str = "pending_review",
    tech: str | None = None,
    confidence: float = 0.5,
    use_cases: str = "uc",
    hints: str = "[]",
    title: str = "t",
    evidence_count: int = 0,
) -> None:
    import json as _json
    if not hints.startswith("["):
        hints = _json.dumps(hints)
    conn.execute(
        """
        INSERT INTO reflections (
            id, title, project, tech, phase, polarity, use_cases, hints,
            confidence, status, evidence_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (refl_id, title, project, tech, phase, polarity, use_cases, hints,
         confidence, status, evidence_count,
         "2026-04-22T08:00:00+00:00", "2026-04-22T08:00:00+00:00"),
    )


def test_embedding_source_text_joins_fields():
    assert _embedding_source_text("T", "when X", ["h1", "h2"]) == "T\nwhen X\nh1\nh2"


class TestApplyNewEmbedding:
    def test_embeds_on_new_reflection(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        fake = FakeEmbedder()
        sync_embedder = SyncEmbedder(lambda: fake)
        svc = ReflectionSynthesisService(
            conn, clock=fixed_clock, sync_embedder=sync_embedder
        )
        action = NewAction(
            title="Always test", phase="general", polarity="do",
            use_cases="when writing code", hints=["write tests first"],
            tech="python", confidence=0.6,
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        assert _vec_count(conn) == 1
        assert len(fake.calls) == 1
        assert "Always test" in fake.calls[0]

    def test_no_embed_row_when_embedder_fails(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        sync_embedder = SyncEmbedder(lambda: FakeEmbedder(fail=True))
        svc = ReflectionSynthesisService(
            conn, clock=fixed_clock, sync_embedder=sync_embedder
        )
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None, confidence=0.5,
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        refl = conn.execute(
            "SELECT id FROM reflections WHERE title = 't'"
        ).fetchone()
        assert refl is not None
        assert _vec_count(conn) == 0

    def test_no_embed_when_sync_embedder_not_passed(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None, confidence=0.5,
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        refl = conn.execute(
            "SELECT id FROM reflections WHERE title = 't'"
        ).fetchone()
        assert refl is not None
        assert _vec_count(conn) == 0


class TestApplyAugmentEmbedding:
    def test_reembeds_on_augment(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            hints='["old-hint"]', confidence=0.5, evidence_count=1,
            title="Existing", use_cases="old uc",
        )
        conn.commit()

        fake = FakeEmbedder()
        sync_embedder = SyncEmbedder(lambda: fake)
        svc = ReflectionSynthesisService(
            conn, clock=fixed_clock, sync_embedder=sync_embedder
        )
        action = NewAction(
            title="seed", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None, confidence=0.5,
            source_observation_ids=[],
        )
        # Seed one embedding row directly to prove augment replaces it
        # (DELETE+INSERT), not merely inserting a duplicate.
        import sqlite_vec
        conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            ("r1", sqlite_vec.serialize_float32([0.0] * 768)),
        )
        conn.commit()
        assert _vec_count(conn) == 1

        augment_action = AugmentAction(
            reflection_id="r1",
            add_hints=["new-hint"],
            rewrite_use_cases=None,
            confidence_delta=0.0,
            add_source_observation_ids=[],
        )
        svc._apply_augment([augment_action])
        conn.commit()

        assert _vec_count(conn) == 1
        assert len(fake.calls) == 1
        assert "new-hint" in fake.calls[0]


class TestReflectionServiceUpdateTextEmbedding:
    """ReflectionService.update_text is the UI-facing edit path.

    Editing use_cases/hints changes the discriminating text a stored
    vector indexes (title + use_cases + hints, see
    `_embedding_source_text`); without a re-embed here the vector goes
    stale the moment the UI edits a reflection.
    """

    def test_update_text_reembeds(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            title="Existing", use_cases="old uc", hints='["old-hint"]',
        )
        conn.commit()

        fake = FakeEmbedder()
        svc = ReflectionService(
            conn, clock=fixed_clock, sync_embedder=SyncEmbedder(lambda: fake),
        )
        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new-hint")

        assert _vec_count(conn) == 1
        assert len(fake.calls) == 1
        assert fake.calls[0] == _embedding_source_text(
            "Existing", "new uc", ["new-hint"]
        )

    def test_update_text_replaces_not_duplicates_vec_row(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            title="Existing", use_cases="old uc", hints='["old-hint"]',
        )
        conn.commit()
        import sqlite_vec
        conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            ("r1", sqlite_vec.serialize_float32([0.0] * 768)),
        )
        conn.commit()
        assert _vec_count(conn) == 1

        fake = FakeEmbedder()
        svc = ReflectionService(
            conn, clock=fixed_clock, sync_embedder=SyncEmbedder(lambda: fake),
        )
        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new-hint")

        assert _vec_count(conn) == 1  # replaced, not duplicated

    def test_update_text_works_without_sync_embedder(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            title="Existing", use_cases="old uc", hints='["old-hint"]',
        )
        conn.commit()

        svc = ReflectionService(conn, clock=fixed_clock)  # no sync_embedder
        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new-hint")

        row = conn.execute(
            "SELECT use_cases FROM reflections WHERE id = 'r1'"
        ).fetchone()
        assert row["use_cases"] == "new uc"
        assert _vec_count(conn) == 0

    def test_no_embed_row_when_embedder_fails(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            title="Existing", use_cases="old uc", hints='["old-hint"]',
        )
        conn.commit()

        svc = ReflectionService(
            conn, clock=fixed_clock,
            sync_embedder=SyncEmbedder(lambda: FakeEmbedder(fail=True)),
        )
        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new-hint")

        row = conn.execute(
            "SELECT use_cases FROM reflections WHERE id = 'r1'"
        ).fetchone()
        assert row["use_cases"] == "new uc"
        assert _vec_count(conn) == 0


class TestApplyMergeEmbedding:
    def test_merge_deletes_source_embedding_no_reembed_of_target(
        self, conn, fixed_clock
    ):
        _insert_reflection(conn, refl_id="src", project="p")
        _insert_reflection(conn, refl_id="tgt", project="p")
        conn.commit()

        import sqlite_vec
        conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            ("src", sqlite_vec.serialize_float32([0.1] * 768)),
        )
        conn.execute(
            "INSERT INTO reflection_embeddings (reflection_id, embedding) "
            "VALUES (?, ?)",
            ("tgt", sqlite_vec.serialize_float32([0.2] * 768)),
        )
        conn.commit()

        fake = FakeEmbedder()
        sync_embedder = SyncEmbedder(lambda: fake)
        svc = ReflectionSynthesisService(
            conn, clock=fixed_clock, sync_embedder=sync_embedder
        )
        action = MergeAction(source_id="src", target_id="tgt", justification="dupes")
        svc._apply_merge([action])
        conn.commit()

        source_count = conn.execute(
            "SELECT COUNT(*) FROM reflection_embeddings WHERE reflection_id = ?",
            ("src",),
        ).fetchone()[0]
        assert source_count == 0
        # Target's embedding is untouched; no new embed call was made.
        assert fake.calls == []
        target_count = conn.execute(
            "SELECT COUNT(*) FROM reflection_embeddings WHERE reflection_id = ?",
            ("tgt",),
        ).fetchone()[0]
        assert target_count == 1
