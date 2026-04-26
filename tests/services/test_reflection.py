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
        """All four closed-episode outcomes feed synthesis; only open
        episodes are excluded."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        # Success episode.
        ep_succ = epsvc.start_foreground(session_id="s1", project="p", goal="a")
        epsvc.close_active(session_id="s1", outcome="success", close_reason="goal_complete")
        # Abandoned episode.
        ep_ab = epsvc.start_foreground(session_id="s2", project="p", goal="b")
        epsvc.close_active(session_id="s2", outcome="abandoned", close_reason="abandoned")
        # no_outcome episode (reconciled or superseded). Phase 2's
        # supersede path writes outcome=no_outcome with the work the
        # user just generated under the prior goal — synthesis must
        # see it. Bugbot caught the prior exclusion as a real bug.
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
        # All three closed outcomes feed synthesis; only the still-
        # open episode is excluded.
        assert ids == {"a", "b", "c"}

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


class TestBuildPrompt:
    def test_prompt_contains_goal_and_tech(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = SynthesisContext(reflections=[], observations=[], last_run_at=None)
        prompt = svc.build_prompt(goal="ship it", tech="python", context=ctx)
        assert "ship it" in prompt
        assert "python" in prompt

    def test_prompt_renders_no_tech_as_any(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = SynthesisContext(reflections=[], observations=[], last_run_at=None)
        prompt = svc.build_prompt(goal="ship it", tech=None, context=ctx)
        assert "TECH: (unspecified)" in prompt

    def test_prompt_renders_existing_reflections(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        refl = ReflectionForPrompt(
            id="r-abc", title="Always SAVEPOINT", tech="python",
            phase="implementation", polarity="do",
            use_cases="when writing multi-statement transactions",
            hints='["wrap in SAVEPOINT", "commit once at end"]',
            confidence=0.8, status="confirmed",
        )
        ctx = SynthesisContext(reflections=[refl], observations=[], last_run_at=None)
        prompt = svc.build_prompt(goal="g", tech=None, context=ctx)
        assert "r-abc" in prompt
        assert "Always SAVEPOINT" in prompt
        assert "when writing multi-statement" in prompt
        assert "0.8" in prompt

    def test_prompt_renders_new_observations_with_episode_context(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        obs = ObservationForPrompt(
            id="obs-1", content="fix drain race", outcome="success",
            component="spool", theme="refactor", tech="python",
            created_at="2026-04-22T10:00:00+00:00",
            episode_goal="Phase 3 hook", episode_outcome="success",
        )
        ctx = SynthesisContext(reflections=[], observations=[obs], last_run_at=None)
        prompt = svc.build_prompt(goal="g", tech="python", context=ctx)
        assert "obs-1" in prompt
        assert "fix drain race" in prompt
        assert "episode goal=\"Phase 3 hook\"" in prompt
        assert "episode outcome=success" in prompt

    def test_prompt_includes_schema_instructions(self, conn, fixed_clock):
        """Response-shape keys must appear verbatim in the instructions."""
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = SynthesisContext(reflections=[], observations=[], last_run_at=None)
        prompt = svc.build_prompt(goal="g", tech=None, context=ctx)
        for key in (
            '"new"', '"augment"', '"merge"', '"ignore"',
            "title", "phase", "polarity", "use_cases", "hints", "confidence",
            "source_observation_ids", "reflection_id", "add_hints",
            "confidence_delta", "source_id", "target_id", "justification",
        ):
            assert key in prompt, f"prompt missing schema token: {key}"

    def test_prompt_is_deterministic(self, conn, fixed_clock):
        """Same inputs → byte-identical prompt."""
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        ctx = SynthesisContext(reflections=[], observations=[], last_run_at=None)
        a = svc.build_prompt(goal="g", tech=None, context=ctx)
        b = svc.build_prompt(goal="g", tech=None, context=ctx)
        assert a == b


from better_memory.services.reflection import (  # noqa: E402
    AugmentAction,
    MergeAction,
    NewAction,
    SynthesisResponseError,
)


class TestParseResponse:
    def test_empty_response_returns_empty_buckets(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [], "augment": [], "merge": [], "ignore": []}'
        )
        assert resp.new == []
        assert resp.augment == []
        assert resp.merge == []
        assert resp.ignore == []

    def test_valid_new_action(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [{"title": "t", "phase": "general", "polarity": "do", '
            '"use_cases": "uc", "hints": ["h1"], "tech": null, '
            '"confidence": 0.7, "source_observation_ids": ["o1", "o2"]}], '
            '"augment": [], "merge": [], "ignore": []}'
        )
        assert len(resp.new) == 1
        n = resp.new[0]
        assert n.title == "t"
        assert n.phase == "general"
        assert n.polarity == "do"
        assert n.use_cases == "uc"
        assert n.hints == ["h1"]
        assert n.tech is None
        assert n.confidence == 0.7
        assert n.source_observation_ids == ["o1", "o2"]

    def test_valid_augment_action(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [], "augment": [{"reflection_id": "r1", '
            '"add_hints": ["x"], "rewrite_use_cases": null, '
            '"confidence_delta": 0.1, "add_source_observation_ids": ["o1"]}], '
            '"merge": [], "ignore": []}'
        )
        assert len(resp.augment) == 1
        a = resp.augment[0]
        assert a.reflection_id == "r1"
        assert a.add_hints == ["x"]
        assert a.rewrite_use_cases is None
        assert a.confidence_delta == 0.1
        assert a.add_source_observation_ids == ["o1"]

    def test_valid_merge_action(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [], "augment": [], "merge": [{"source_id": "s", '
            '"target_id": "t", "justification": "dupes"}], "ignore": []}'
        )
        assert len(resp.merge) == 1
        m = resp.merge[0]
        assert m.source_id == "s"
        assert m.target_id == "t"
        assert m.justification == "dupes"

    def test_valid_ignore(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [], "augment": [], "merge": [], "ignore": ["o1", "o2"]}'
        )
        assert resp.ignore == ["o1", "o2"]

    def test_malformed_json_raises(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        with pytest.raises(SynthesisResponseError):
            svc.parse_response("not json")

    def test_missing_top_level_key_raises(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        with pytest.raises(SynthesisResponseError):
            svc.parse_response('{"new": []}')  # missing augment/merge/ignore

    def test_wrong_top_level_type_raises(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        with pytest.raises(SynthesisResponseError):
            svc.parse_response('["not", "an", "object"]')

    def test_unknown_extra_field_silently_dropped(self, conn, fixed_clock):
        """LLMs may add commentary — we drop unknown keys rather than reject."""
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        resp = svc.parse_response(
            '{"new": [], "augment": [], "merge": [], "ignore": [], '
            '"rationale": "some extra commentary from the LLM"}'
        )
        assert resp.new == []
        assert resp.augment == []
        assert resp.merge == []
        assert resp.ignore == []

    def test_new_missing_required_field_raises(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        with pytest.raises(SynthesisResponseError):
            svc.parse_response(
                '{"new": [{"title": "t"}], "augment": [], "merge": [], "ignore": []}'
            )

    def test_new_invalid_enum_raises(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        with pytest.raises(SynthesisResponseError):
            svc.parse_response(
                '{"new": [{"title": "t", "phase": "bogus", "polarity": "do", '
                '"use_cases": "uc", "hints": [], "tech": null, '
                '"confidence": 0.5, "source_observation_ids": []}], '
                '"augment": [], "merge": [], "ignore": []}'
            )


class TestApplyNew:
    def test_applies_single_new_reflection(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = NewAction(
            title="Always test", phase="general", polarity="do",
            use_cases="when writing code", hints=["write tests first"],
            tech="python", confidence=0.6,
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        refl = conn.execute(
            "SELECT title, phase, polarity, confidence, tech, status, "
            "evidence_count, hints, use_cases "
            "FROM reflections WHERE title = ?",
            ("Always test",),
        ).fetchone()
        assert refl is not None
        assert refl["phase"] == "general"
        assert refl["polarity"] == "do"
        assert refl["confidence"] == 0.6
        assert refl["tech"] == "python"
        assert refl["status"] == "pending_review"
        assert refl["evidence_count"] == 1
        import json as _json
        assert _json.loads(refl["hints"]) == ["write tests first"]

        obs = conn.execute(
            "SELECT status FROM observations WHERE id = ?", ("obs-1",)
        ).fetchone()
        assert obs["status"] == "consumed_into_reflection"

    def test_clamps_confidence_above_1(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None,
            confidence=1.5,  # above max
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()
        row = conn.execute(
            "SELECT confidence FROM reflections WHERE title = ?", ("t",)
        ).fetchone()
        assert row["confidence"] == 1.0

    def test_clamps_confidence_below_0_1(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None,
            confidence=0.05,  # below min
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()
        row = conn.execute(
            "SELECT confidence FROM reflections WHERE title = ?", ("t",)
        ).fetchone()
        assert row["confidence"] == 0.1

    def test_drops_unknown_source_observations(self, conn, fixed_clock):
        """Unknown obs ids are dropped; reflection still created as long as >=1 source survives."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None, confidence=0.5,
            source_observation_ids=["obs-1", "obs-bogus"],  # one real, one fake
        )
        svc._apply_new([action], project="p")
        conn.commit()

        # Reflection exists, evidence_count == 1 (only real source counted).
        refl = conn.execute(
            "SELECT evidence_count FROM reflections WHERE title = ?", ("t",)
        ).fetchone()
        assert refl["evidence_count"] == 1

        # Only obs-1 linked; obs-bogus was silently dropped.
        sources = conn.execute(
            "SELECT observation_id FROM reflection_sources "
            "JOIN reflections ON reflections.id = reflection_sources.reflection_id "
            "WHERE reflections.title = ?",
            ("t",),
        ).fetchall()
        assert {s["observation_id"] for s in sources} == {"obs-1"}

    def test_skips_entry_when_all_sources_invalid(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=[], tech=None, confidence=0.5,
            source_observation_ids=["obs-bogus"],
        )
        svc._apply_new([action], project="p")
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflections"
        ).fetchone()["c"]
        assert count == 0


class TestApplyAugment:
    def test_appends_hints_deduped(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p",
            hints='["old-hint"]', confidence=0.5, evidence_count=1,
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="r1",
            add_hints=["old-hint", "new-hint-1", "new-hint-2"],
            rewrite_use_cases=None,
            confidence_delta=0.0,
            add_source_observation_ids=[],
        )
        svc._apply_augment([action])
        conn.commit()
        row = conn.execute(
            "SELECT hints FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        import json as _json
        hints = _json.loads(row["hints"])
        # Order preserved: existing first, then new ones; duplicates dropped.
        assert hints == ["old-hint", "new-hint-1", "new-hint-2"]

    def test_rewrites_use_cases_when_provided(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p", use_cases="old uc",
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="r1", add_hints=[],
            rewrite_use_cases="new uc",
            confidence_delta=0.0, add_source_observation_ids=[],
        )
        svc._apply_augment([action])
        conn.commit()
        row = conn.execute(
            "SELECT use_cases FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["use_cases"] == "new uc"

    def test_leaves_use_cases_when_rewrite_is_null(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p", use_cases="keep me",
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="r1", add_hints=[],
            rewrite_use_cases=None,
            confidence_delta=0.0, add_source_observation_ids=[],
        )
        svc._apply_augment([action])
        conn.commit()
        row = conn.execute(
            "SELECT use_cases FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["use_cases"] == "keep me"

    def test_applies_confidence_delta_and_clamps(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p", confidence=0.8,
        )
        _insert_reflection(
            conn, refl_id="r2", project="p", confidence=0.2,
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_augment(
            [
                AugmentAction(
                    reflection_id="r1", add_hints=[],
                    rewrite_use_cases=None,
                    confidence_delta=0.5,  # 0.8+0.5=1.3 → clamp to 1.0
                    add_source_observation_ids=[],
                ),
                AugmentAction(
                    reflection_id="r2", add_hints=[],
                    rewrite_use_cases=None,
                    confidence_delta=-0.5,  # 0.2-0.5=-0.3 → clamp to 0.1
                    add_source_observation_ids=[],
                ),
            ]
        )
        conn.commit()
        r1 = conn.execute(
            "SELECT confidence FROM reflections WHERE id = 'r1'"
        ).fetchone()
        r2 = conn.execute(
            "SELECT confidence FROM reflections WHERE id = 'r2'"
        ).fetchone()
        assert r1["confidence"] == 1.0
        assert r2["confidence"] == 0.1

    def test_adds_source_links_and_recomputes_evidence_count(
        self, conn, fixed_clock
    ):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-A", project="p", episode_id=ep)
        _insert_obs(conn, obs_id="obs-B", project="p", episode_id=ep)
        _insert_reflection(
            conn, refl_id="r1", project="p", evidence_count=1,
        )
        # Existing source link for obs-A.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)", ("r1", "obs-A"),
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="r1", add_hints=[],
            rewrite_use_cases=None,
            confidence_delta=0.0,
            add_source_observation_ids=["obs-A", "obs-B"],  # A already linked
        )
        svc._apply_augment([action])
        conn.commit()

        # Evidence count = actual COUNT = 2 (A + B).
        row = conn.execute(
            "SELECT evidence_count FROM reflections WHERE id = 'r1'"
        ).fetchone()
        assert row["evidence_count"] == 2

        # obs-B marked consumed. obs-A might already be consumed from prior path;
        # here we just assert the two observations are in the consumed state.
        statuses = conn.execute(
            "SELECT status FROM observations WHERE id IN ('obs-A', 'obs-B')"
        ).fetchall()
        # Both should be consumed_into_reflection after augment.
        assert {s["status"] for s in statuses} == {"consumed_into_reflection"}

    def test_drops_unknown_reflection_id(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="nope", add_hints=["h"],
            rewrite_use_cases=None, confidence_delta=0.0,
            add_source_observation_ids=[],
        )
        svc._apply_augment([action])  # should not raise
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflections"
        ).fetchone()["c"]
        assert count == 0

    def test_skips_retired_reflection(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p", status="retired", confidence=0.5,
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = AugmentAction(
            reflection_id="r1", add_hints=["h"],
            rewrite_use_cases=None, confidence_delta=0.3,
            add_source_observation_ids=[],
        )
        svc._apply_augment([action])
        conn.commit()
        row = conn.execute(
            "SELECT confidence, hints FROM reflections WHERE id = 'r1'"
        ).fetchone()
        assert row["confidence"] == 0.5  # unchanged
        assert row["hints"] == "[]"  # unchanged


class TestApplyMerge:
    def test_merges_two_reflections(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-A", project="p", episode_id=ep)
        _insert_obs(conn, obs_id="obs-B", project="p", episode_id=ep)
        _insert_reflection(
            conn, refl_id="src", project="p", evidence_count=1,
        )
        _insert_reflection(
            conn, refl_id="tgt", project="p", evidence_count=1,
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)", ("src", "obs-A"),
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)", ("tgt", "obs-B"),
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        action = MergeAction(
            source_id="src", target_id="tgt",
            justification="dupes",
        )
        svc._apply_merge([action])
        conn.commit()

        src_row = conn.execute(
            "SELECT status, superseded_by FROM reflections WHERE id = 'src'"
        ).fetchone()
        assert src_row["status"] == "superseded"
        assert src_row["superseded_by"] == "tgt"

        tgt_row = conn.execute(
            "SELECT evidence_count FROM reflections WHERE id = 'tgt'"
        ).fetchone()
        assert tgt_row["evidence_count"] == 2

        src_sources = conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_sources WHERE reflection_id = 'src'"
        ).fetchone()
        assert src_sources["c"] == 0

        tgt_sources = conn.execute(
            "SELECT observation_id FROM reflection_sources WHERE reflection_id = 'tgt' "
            "ORDER BY observation_id"
        ).fetchall()
        assert [s["observation_id"] for s in tgt_sources] == ["obs-A", "obs-B"]

    def test_merge_dedupes_shared_sources(self, conn, fixed_clock):
        """If both reflections already link the same observation, target count is still correct."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-X", project="p", episode_id=ep)
        _insert_reflection(conn, refl_id="src", project="p")
        _insert_reflection(conn, refl_id="tgt", project="p")
        for rid in ("src", "tgt"):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)", (rid, "obs-X"),
            )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_merge(
            [MergeAction(source_id="src", target_id="tgt", justification="")]
        )
        conn.commit()

        tgt_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_sources WHERE reflection_id = 'tgt'"
        ).fetchone()["c"]
        assert tgt_count == 1

    def test_drops_unknown_source(self, conn, fixed_clock):
        _insert_reflection(conn, refl_id="tgt", project="p")
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_merge(
            [MergeAction(source_id="nope", target_id="tgt", justification="")]
        )
        conn.commit()
        # Nothing changed.
        status = conn.execute(
            "SELECT status FROM reflections WHERE id = 'tgt'"
        ).fetchone()
        assert status["status"] == "pending_review"  # unchanged

    def test_drops_unknown_target(self, conn, fixed_clock):
        _insert_reflection(conn, refl_id="src", project="p")
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_merge(
            [MergeAction(source_id="src", target_id="nope", justification="")]
        )
        conn.commit()
        src = conn.execute(
            "SELECT status FROM reflections WHERE id = 'src'"
        ).fetchone()
        assert src["status"] == "pending_review"  # unchanged

    def test_skips_already_superseded_source(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="src", project="p", status="superseded",
        )
        _insert_reflection(conn, refl_id="tgt", project="p")
        conn.commit()
        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_merge(
            [MergeAction(source_id="src", target_id="tgt", justification="")]
        )
        conn.commit()
        # Source already superseded → target unchanged.
        tgt_count = conn.execute(
            "SELECT evidence_count FROM reflections WHERE id = 'tgt'"
        ).fetchone()["evidence_count"]
        assert tgt_count == 0

    def test_self_merge_is_rejected(self, conn, fixed_clock):
        """source_id == target_id would DELETE the target's sources — reject it.

        Without this guard: INSERT OR IGNORE from self → no-op, then DELETE
        FROM reflection_sources WHERE reflection_id = source_id would wipe
        the target's sources (source and target are the same row). Then the
        reflection is marked superseded. Double damage. Guard at the top.
        """
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-X", project="p", episode_id=ep)
        _insert_reflection(conn, refl_id="r1", project="p", evidence_count=1)
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)", ("r1", "obs-X"),
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_merge(
            [MergeAction(source_id="r1", target_id="r1", justification="bogus")]
        )
        conn.commit()

        # Reflection still has its source row and is not superseded.
        row = conn.execute(
            "SELECT status, evidence_count FROM reflections WHERE id = 'r1'"
        ).fetchone()
        assert row["status"] == "pending_review"
        assert row["evidence_count"] == 1
        src_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_sources WHERE reflection_id = 'r1'"
        ).fetchone()["c"]
        assert src_count == 1


class TestApplyIgnore:
    def test_marks_observations_consumed_without_reflection(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        _insert_obs(conn, obs_id="obs-2", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        svc._apply_ignore(["obs-1", "obs-2", "obs-bogus"])
        conn.commit()

        rows = conn.execute(
            "SELECT id, status FROM observations ORDER BY id"
        ).fetchall()
        by_id = {r["id"]: r["status"] for r in rows}
        assert by_id["obs-1"] == "consumed_without_reflection"
        assert by_id["obs-2"] == "consumed_without_reflection"


class TestSynthesizeOrchestrator:
    def test_end_to_end_with_fake_chat(self, conn, fixed_clock):
        import json as _json
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        # FakeChat response: create one new reflection from obs-1.
        fake = FakeChat(
            responses=[
                _json.dumps({
                    "new": [
                        {
                            "title": "A new lesson",
                            "phase": "general",
                            "polarity": "do",
                            "use_cases": "uc",
                            "hints": ["do X"],
                            "tech": None,
                            "confidence": 0.7,
                            "source_observation_ids": ["obs-1"],
                        }
                    ],
                    "augment": [],
                    "merge": [],
                    "ignore": [],
                })
            ]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        result = asyncio.run(
            svc.synthesize(goal="g2", tech=None, project="p")
        )

        # Reflection was created.
        refl = conn.execute(
            "SELECT title FROM reflections"
        ).fetchone()
        assert refl["title"] == "A new lesson"

        # obs-1 consumed into reflection.
        obs = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert obs["status"] == "consumed_into_reflection"

        # Watermark upserted.
        wm = conn.execute(
            "SELECT last_run_at, last_goal FROM synthesis_runs "
            "WHERE project = 'p' AND tech = ''"
        ).fetchone()
        assert wm is not None
        assert wm["last_goal"] == "g2"

        # Result: dict[do, dont, neutral] with the new reflection in `do`.
        assert len(result["do"]) == 1
        assert result["do"][0]["title"] == "A new lesson"
        assert result["dont"] == []
        assert result["neutral"] == []

    def test_atomic_rollback_on_parse_error(self, conn, fixed_clock):
        """Malformed LLM response → nothing committed, no watermark."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        fake = FakeChat(responses=["not json"])
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        with pytest.raises(SynthesisResponseError):
            asyncio.run(
                svc.synthesize(goal="g", tech=None, project="p")
            )

        # No reflection, no watermark, obs unchanged.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflections"
        ).fetchone()["c"]
        assert count == 0
        wm = conn.execute(
            "SELECT COUNT(*) AS c FROM synthesis_runs"
        ).fetchone()["c"]
        assert wm == 0
        obs = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert obs["status"] == "active"

    def test_tech_defaults_to_empty_string_in_watermark(self, conn, fixed_clock):
        """tech=None → synthesis_runs.tech=''."""
        import json as _json
        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="g", tech=None, project="p")
        )
        wm = conn.execute(
            "SELECT tech FROM synthesis_runs WHERE project = 'p'"
        ).fetchone()
        assert wm["tech"] == ""

    def test_empty_response_still_updates_watermark(self, conn, fixed_clock):
        """No actions → still record that synthesis ran."""
        import json as _json
        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="g", tech=None, project="p")
        )
        wm = conn.execute(
            "SELECT last_run_at, last_goal FROM synthesis_runs WHERE project = 'p'"
        ).fetchone()
        assert wm is not None
        assert wm["last_goal"] == "g"

    def test_savepoint_rolls_back_on_apply_failure(self, conn, fixed_clock, monkeypatch):
        """If an apply method raises mid-synthesis, all state must roll back.

        This is the true SAVEPOINT-rollback test: the exception fires INSIDE
        the SAVEPOINT (from _apply_merge), not before it opens. Verifies
        that _apply_new's effects also roll back — the SAVEPOINT covers
        all four apply methods + watermark as a unit.
        """
        import json as _json

        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        # Response has a new action (succeeds) + a merge action (forced to raise).
        fake = FakeChat(
            responses=[_json.dumps({
                "new": [
                    {
                        "title": "Would be created",
                        "phase": "general", "polarity": "do",
                        "use_cases": "uc", "hints": [], "tech": None,
                        "confidence": 0.5,
                        "source_observation_ids": ["obs-1"],
                    }
                ],
                "augment": [],
                "merge": [
                    {"source_id": "irrelevant", "target_id": "also",
                     "justification": "forced-fail"}
                ],
                "ignore": [],
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced apply failure")

        monkeypatch.setattr(svc, "_apply_merge", boom)

        import asyncio
        with pytest.raises(RuntimeError, match="forced apply failure"):
            asyncio.run(svc.synthesize(goal="g", tech=None, project="p"))

        # Nothing from _apply_new persists.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reflections"
        ).fetchone()["c"]
        assert count == 0

        # Watermark not written (upsert is inside SAVEPOINT, after apply).
        wm_count = conn.execute(
            "SELECT COUNT(*) AS c FROM synthesis_runs"
        ).fetchone()["c"]
        assert wm_count == 0

        # obs-1 not marked consumed (the _apply_new consumed update was rolled back).
        obs = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert obs["status"] == "active"


class TestShortCircuit:
    def test_same_goal_within_window_skips_llm(self, conn, fixed_clock):
        """Same goal + tech + no new obs within 10 min → no LLM call."""
        # Pre-seed a recent synthesis_runs row + one existing reflection.
        _insert_reflection(
            conn, refl_id="r1", project="p", tech=None,
            status="confirmed", confidence=0.5,
        )
        # last_run_at = 5 min before the clock's fixed time (10:00).
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal) "
            "VALUES (?, ?, ?, ?)",
            ("p", "", "2026-04-22T09:55:00+00:00", "resume goal"),
        )
        conn.commit()

        # Empty responses list — if LLM is called, FakeChat raises.
        fake = FakeChat(responses=[])
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        result = asyncio.run(
            svc.synthesize(goal="resume goal", tech=None, project="p")
        )

        # FakeChat was never called.
        assert fake.calls == []

        # Result still returns the existing reflection.
        assert len(result["do"]) == 1
        assert result["do"][0]["id"] == "r1"

    def test_different_goal_does_not_short_circuit(self, conn, fixed_clock):
        import json as _json
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal) "
            "VALUES (?, ?, ?, ?)",
            ("p", "", "2026-04-22T09:55:00+00:00", "old goal"),
        )
        conn.commit()

        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="new goal", tech=None, project="p")
        )
        # LLM WAS called — different goal.
        assert len(fake.calls) == 1

    def test_outside_window_does_not_short_circuit(self, conn, fixed_clock):
        """Same goal but more than 10 min since last run → synthesis runs."""
        import json as _json
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal) "
            "VALUES (?, ?, ?, ?)",
            ("p", "", "2026-04-22T09:30:00+00:00", "same goal"),  # 30 min ago
        )
        conn.commit()

        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="same goal", tech=None, project="p")
        )
        assert len(fake.calls) == 1

    def test_new_observations_invalidate_short_circuit(self, conn, fixed_clock):
        """Same goal, within window, but new obs arrived → synthesis runs."""
        import json as _json
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal) "
            "VALUES (?, ?, ?, ?)",
            ("p", "", "2026-04-22T09:55:00+00:00", "resume"),
        )
        # Observation timestamp AFTER last_run_at.
        _insert_obs(
            conn, obs_id="new-obs", project="p", episode_id=ep,
            created_at="2026-04-22T09:57:00+00:00",
        )
        conn.commit()

        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="resume", tech=None, project="p")
        )
        assert len(fake.calls) == 1

    def test_no_prior_run_does_not_short_circuit(self, conn, fixed_clock):
        import json as _json
        fake = FakeChat(
            responses=[_json.dumps({
                "new": [], "augment": [], "merge": [], "ignore": []
            })]
        )
        svc = ReflectionSynthesisService(conn, chat=fake, clock=fixed_clock)
        import asyncio
        asyncio.run(
            svc.synthesize(goal="g", tech=None, project="p")
        )
        assert len(fake.calls) == 1


class TestRetrieveReflections:
    def test_returns_buckets_for_project(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r1", project="p", polarity="do",
            status="confirmed", confidence=0.9,
        )
        _insert_reflection(
            conn, refl_id="r2", project="p", polarity="dont",
            status="pending_review", confidence=0.6,
        )
        _insert_reflection(
            conn, refl_id="r3", project="p", polarity="neutral",
            status="confirmed", confidence=0.3,
        )
        _insert_reflection(
            conn, refl_id="r4", project="other", polarity="do",
            status="confirmed", confidence=0.8,
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")
        assert {r["id"] for r in result["do"]} == {"r1"}
        assert {r["id"] for r in result["dont"]} == {"r2"}
        assert {r["id"] for r in result["neutral"]} == {"r3"}

    def test_excludes_retired_and_superseded(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r-ok", project="p", polarity="do",
            status="confirmed", confidence=0.5,
        )
        _insert_reflection(
            conn, refl_id="r-retired", project="p", polarity="do",
            status="retired", confidence=0.5,
        )
        _insert_reflection(
            conn, refl_id="r-superseded", project="p", polarity="do",
            status="superseded", confidence=0.5,
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")
        assert {r["id"] for r in result["do"]} == {"r-ok"}

    def test_filter_by_phase(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r-plan", project="p", phase="planning",
            status="confirmed", polarity="do",
        )
        _insert_reflection(
            conn, refl_id="r-impl", project="p", phase="implementation",
            status="confirmed", polarity="do",
        )
        _insert_reflection(
            conn, refl_id="r-gen", project="p", phase="general",
            status="confirmed", polarity="do",
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p", phase="planning")
        assert {r["id"] for r in result["do"]} == {"r-plan"}

    def test_filter_by_polarity(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r-do", project="p", polarity="do",
            status="confirmed",
        )
        _insert_reflection(
            conn, refl_id="r-dont", project="p", polarity="dont",
            status="confirmed",
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p", polarity="dont")
        assert result["do"] == []
        assert {r["id"] for r in result["dont"]} == {"r-dont"}
        assert result["neutral"] == []

    def test_orders_by_confidence_desc(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r-high", project="p", polarity="do",
            status="confirmed", confidence=0.9,
        )
        _insert_reflection(
            conn, refl_id="r-low", project="p", polarity="do",
            status="confirmed", confidence=0.2,
        )
        _insert_reflection(
            conn, refl_id="r-mid", project="p", polarity="do",
            status="confirmed", confidence=0.5,
        )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")
        assert [r["id"] for r in result["do"]] == ["r-high", "r-mid", "r-low"]


class TestRetrieveReflectionsLimit:
    """Phase 6: retrieve_reflections caps each bucket at limit_per_bucket."""

    def test_limit_per_bucket_caps_each_polarity(self, conn, fixed_clock):
        # Insert 5 'do', 5 'dont', 5 'neutral' confirmed reflections.
        for polarity in ("do", "dont", "neutral"):
            for i in range(5):
                _insert_reflection(
                    conn, refl_id=f"{polarity}-{i}", project="p",
                    polarity=polarity, status="confirmed",
                    confidence=0.9 - (i * 0.1),
                )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)

        result = svc.retrieve_reflections(project="p", limit_per_bucket=2)
        assert len(result["do"]) == 2
        assert len(result["dont"]) == 2
        assert len(result["neutral"]) == 2

    def test_default_limit_is_20(self, conn, fixed_clock):
        # Insert 25 'do' reflections — default cap should trim to 20.
        for i in range(25):
            _insert_reflection(
                conn, refl_id=f"r-{i}", project="p", polarity="do",
                status="confirmed", confidence=0.9 - (i * 0.01),
            )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")
        assert len(result["do"]) == 20

    def test_limit_preserves_confidence_order(self, conn, fixed_clock):
        # 5 reflections with descending confidence; limit 3 keeps top 3.
        confidences = [0.9, 0.8, 0.7, 0.6, 0.5]
        for i, c in enumerate(confidences):
            _insert_reflection(
                conn, refl_id=f"r-{i}", project="p", polarity="do",
                status="confirmed", confidence=c,
            )
        conn.commit()

        svc = ReflectionSynthesisService(conn, chat=FakeChat(responses=[]), clock=fixed_clock)
        result = svc.retrieve_reflections(project="p", limit_per_bucket=3)
        assert [r["id"] for r in result["do"]] == ["r-0", "r-1", "r-2"]


class TestStatusChangedAtOnTransition:
    def test_apply_new_bumps_status_changed_at(self, conn, fixed_clock):
        """Verify _apply_new updates observations.status_changed_at to
        clock-now (not just the status column)."""
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-1", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        action = NewAction(
            title="Always test", phase="general", polarity="do",
            use_cases="when X", hints=["do Y"], tech="python",
            confidence=0.6, source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        assert row["status_changed_at"] == fixed_clock().isoformat()

    def test_apply_augment_bumps_status_changed_at(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_reflection(conn, refl_id="r1", project="p")
        _insert_obs(
            conn, obs_id="obs-new", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        action = AugmentAction(
            reflection_id="r1", add_hints=["another hint"],
            rewrite_use_cases=None, confidence_delta=0.0,
            add_source_observation_ids=["obs-new"],
        )
        svc._apply_augment([action])
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-new'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        assert row["status_changed_at"] == fixed_clock().isoformat()

    def test_apply_ignore_bumps_status_changed_at(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-1", project="p", episode_id=ep,
            created_at="2026-04-01T00:00:00+00:00",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        svc._apply_ignore(["obs-1"])
        conn.commit()

        row = conn.execute(
            "SELECT status, status_changed_at FROM observations "
            "WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_without_reflection"
        assert row["status_changed_at"] == fixed_clock().isoformat()
