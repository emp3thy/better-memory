"""Tests for ReflectionSynthesisService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.llm.fake import FakeChat
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import (
    ObservationForPrompt,
    ReflectionForPrompt,
    ReflectionSynthesisService,
    SynthesisContext,
)


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
) -> None:
    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, outcome,
            reinforcement_score, episode_id, tech, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?)
        """,
        (obs_id, content, project, component, theme, outcome,
         episode_id, tech, created_at, status),
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
        # convenience: accept a list too
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


class TestLoadContext:
    def test_empty_db_returns_empty_context(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech=None)
        assert ctx.reflections == []
        assert ctx.observations == []
        assert ctx.last_run_at is None

    def test_loads_active_reflections_for_project(self, conn, fixed_clock):
        _insert_reflection(conn, refl_id="r1", project="p", status="pending_review")
        _insert_reflection(conn, refl_id="r2", project="p", status="confirmed")
        _insert_reflection(conn, refl_id="r3", project="p", status="retired")
        _insert_reflection(conn, refl_id="r4", project="other", status="pending_review")
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech=None)
        ids = {r.id for r in ctx.reflections}
        # pending_review + confirmed only; retired excluded; other project excluded.
        assert ids == {"r1", "r2"}

    def test_filters_reflections_by_tech_when_specified(self, conn, fixed_clock):
        _insert_reflection(conn, refl_id="r1", project="p", tech="python")
        _insert_reflection(conn, refl_id="r2", project="p", tech="sqlite")
        _insert_reflection(conn, refl_id="r3", project="p", tech=None)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech="python")
        ids = {r.id for r in ctx.reflections}
        # Match both tech=python (exact match) and tech=NULL (cross-tech reflection).
        assert ids == {"r1", "r3"}

    def test_loads_new_observations_since_watermark(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep_id = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-old", project="p", episode_id=ep_id,
            created_at="2026-04-22T08:00:00+00:00",
        )
        _insert_obs(
            conn, obs_id="obs-new", project="p", episode_id=ep_id,
            created_at="2026-04-22T09:30:00+00:00",
        )
        # Watermark: 09:00:00 — only obs-new is "new".
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "", "2026-04-22T09:00:00+00:00"),
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech=None)
        ids = {o.id for o in ctx.observations}
        assert ids == {"obs-new"}
        assert ctx.last_run_at == "2026-04-22T09:00:00+00:00"

    def test_filters_observations_by_episode_outcome(self, conn, fixed_clock):
        """Only episodes with outcome in {success, partial, abandoned} contribute."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        # Success episode.
        ep_succ = epsvc.start_foreground(session_id="s1", project="p", goal="a")
        epsvc.close_active(session_id="s1", outcome="success", close_reason="goal_complete")
        # Abandoned episode.
        ep_ab = epsvc.start_foreground(session_id="s2", project="p", goal="b")
        epsvc.close_active(session_id="s2", outcome="abandoned", close_reason="abandoned")
        # no_outcome episode (reconciled without a verdict).
        ep_no = epsvc.start_foreground(session_id="s3", project="p", goal="c")
        epsvc.close_active(
            session_id="s3", outcome="no_outcome", close_reason="session_end_reconciled",
        )
        # Open (ended_at NULL) episode — not yet feed-eligible.
        ep_open = epsvc.open_background(session_id="s4", project="p")

        _insert_obs(conn, obs_id="a", project="p", episode_id=ep_succ)
        _insert_obs(conn, obs_id="b", project="p", episode_id=ep_ab)
        _insert_obs(conn, obs_id="c", project="p", episode_id=ep_no)
        _insert_obs(conn, obs_id="d", project="p", episode_id=ep_open)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech=None)
        ids = {o.id for o in ctx.observations}
        # success + abandoned feed synthesis; no_outcome and open episodes don't.
        assert ids == {"a", "b"}

    def test_observations_carry_joined_episode_context(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep_id = epsvc.start_foreground(
            session_id="s1", project="p", goal="ship it", tech="python"
        )
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn,
            obs_id="obs-1",
            project="p",
            episode_id=ep_id,
            outcome="failure",
            content="hit a bug",
            component="hooks",
            theme="bug",
            tech="python",
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = svc.load_context(project="p", tech=None)
        assert len(ctx.observations) == 1
        obs = ctx.observations[0]
        assert obs.id == "obs-1"
        assert obs.content == "hit a bug"
        assert obs.outcome == "failure"
        assert obs.component == "hooks"
        assert obs.theme == "bug"
        assert obs.tech == "python"
        assert obs.episode_goal == "ship it"
        assert obs.episode_outcome == "success"
