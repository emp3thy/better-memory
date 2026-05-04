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
    EpisodeContext,
    EpisodeForPrompt,
    EpisodeQueueCounts,
    ObservationForPrompt,
    ReflectionForPrompt,
    ReflectionSynthesisService,
    SynthesisStep,
)
from tests.conftest import run_async


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


class TestTechNormalization:
    """`tech` is lowercased at every entry point so case mismatches
    don't cause silent miss-on-retrieval.

    EpisodeService.start_foreground and ObservationService.create
    already normalise. Reflection retrieval/synthesis must do the
    same so that, e.g., the MCP tool surface accepting tech="React"
    matches reflections stored as tech="react".
    """

    def test_retrieve_reflections_normalizes_tech_arg(self, conn, fixed_clock):
        _insert_reflection(
            conn, refl_id="r-react", project="p", tech="react",
            polarity="do", status="confirmed",
        )
        _insert_reflection(
            conn, refl_id="r-python", project="p", tech="python",
            polarity="do", status="confirmed",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        result = svc.retrieve_reflections(project="p", tech="REACT")
        assert {r["id"] for r in result["do"]} == {"r-react"}

    def test_apply_new_lowercases_action_tech(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock
        )
        action = NewAction(
            title="Mixed-case tech from LLM",
            phase="general", polarity="do",
            use_cases="React work",
            hints=["use hooks"],
            tech="React",  # LLM may emit mixed case
            confidence=0.6,
            source_observation_ids=["obs-1"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        row = conn.execute(
            "SELECT tech FROM reflections WHERE title = ?",
            ("Mixed-case tech from LLM",),
        ).fetchone()
        assert row["tech"] == "react"


class TestApplyAugmentTimestampFreshness:
    """Each iteration of `_apply_augment` must stamp `updated_at` with
    the time of THAT iteration, not a stale earlier-iteration value.

    Bug: `now` was set pre-loop and only conditionally reassigned
    inside `if valid_sources:`. An iteration with empty valid_sources
    that ran AFTER an iteration with valid_sources would reuse the
    earlier iteration's timestamp on its reflection's UPDATE.
    """

    def test_iteration_without_sources_uses_fresh_clock_after_iteration_with_sources(
        self, conn,
    ):
        epsvc_clock_seq = [
            datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 1, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 2, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 3, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 4, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 5, tzinfo=UTC),
        ]
        # Service clock advances every call so we can detect stale reuse.
        i = {"n": 0}
        def counter_clock() -> datetime:
            t = epsvc_clock_seq[i["n"]]
            i["n"] += 1
            return t

        # Episode + observation seed (uses its own ticks of the same clock).
        epsvc = EpisodeService(conn, clock=counter_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-1", project="p", episode_id=ep)
        _insert_reflection(
            conn, refl_id="r1", project="p",
            confidence=0.5, evidence_count=0,
        )
        _insert_reflection(
            conn, refl_id="r2", project="p",
            confidence=0.5, evidence_count=0,
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=counter_clock,
        )
        action_with = AugmentAction(
            reflection_id="r1", add_hints=[],
            rewrite_use_cases=None, confidence_delta=0.0,
            add_source_observation_ids=["obs-1"],
        )
        action_empty = AugmentAction(
            reflection_id="r2", add_hints=[],
            rewrite_use_cases=None, confidence_delta=0.0,
            add_source_observation_ids=[],
        )
        svc._apply_augment([action_with, action_empty])
        conn.commit()

        ts1 = conn.execute(
            "SELECT updated_at FROM reflections WHERE id = 'r1'"
        ).fetchone()["updated_at"]
        ts2 = conn.execute(
            "SELECT updated_at FROM reflections WHERE id = 'r2'"
        ).fetchone()["updated_at"]
        # Fresh-per-iteration semantics: the second iteration's
        # reflection should not share the first iteration's stamp.
        assert ts1 != ts2
        assert ts2 > ts1


class TestApplyMergeTimestampFreshness:
    """Same per-iteration freshness contract as _apply_augment.

    `_apply_merge` previously stamped ``now`` once before the loop,
    so a skipped iteration (self-merge, unknown id, retired source)
    followed by an executing iteration would carry the pre-loop
    stamp into source/target ``updated_at`` — losing per-iteration
    fidelity.
    """

    def test_each_merge_iteration_uses_fresh_clock(self, conn):
        ticks = [
            datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 1, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 2, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 3, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 4, tzinfo=UTC),
        ]
        i = {"n": 0}
        def counter_clock() -> datetime:
            t = ticks[i["n"]]
            i["n"] += 1
            return t

        # First merge: src1 -> tgt1. Second merge: src2 -> tgt2.
        for refl_id in ("src1", "tgt1", "src2", "tgt2"):
            _insert_reflection(
                conn, refl_id=refl_id, project="p",
                confidence=0.5, evidence_count=0,
            )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=counter_clock,
        )
        merge1 = MergeAction(
            source_id="src1", target_id="tgt1",
            justification="duplicate",
        )
        merge2 = MergeAction(
            source_id="src2", target_id="tgt2",
            justification="duplicate",
        )
        svc._apply_merge([merge1, merge2])
        conn.commit()

        ts_src1 = conn.execute(
            "SELECT updated_at FROM reflections WHERE id = 'src1'"
        ).fetchone()["updated_at"]
        ts_src2 = conn.execute(
            "SELECT updated_at FROM reflections WHERE id = 'src2'"
        ).fetchone()["updated_at"]
        # Second merge iteration must not share the first's stamp.
        assert ts_src1 != ts_src2
        assert ts_src2 > ts_src1


class TestArchivedObservationGuards:
    """Archived observations must not feed synthesis nor be
    de-archived by apply methods. Two layers:

    1. ``load_context`` filters ``status = 'active'`` so archived
       rows never reach the LLM prompt.
    2. ``_filter_existing_observations`` enforces ``status = 'active'``
       so even if the LLM hallucinates an archived id, the apply
       methods skip it (no UPDATE → no de-archiving).
    """

    def test_apply_ignore_does_not_dearchive(self, conn, fixed_clock):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-archived", project="p", episode_id=ep,
            status="archived",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        # Hallucinated archived id from the LLM's ignore list.
        svc._apply_ignore(["obs-archived"])
        conn.commit()

        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-archived'"
        ).fetchone()
        assert row["status"] == "archived"

    def test_apply_new_does_not_dearchive_via_source(
        self, conn, fixed_clock,
    ):
        epsvc = EpisodeService(conn, clock=fixed_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(
            conn, obs_id="obs-active", project="p", episode_id=ep,
            status="active",
        )
        _insert_obs(
            conn, obs_id="obs-archived", project="p", episode_id=ep,
            status="archived",
        )
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        action = NewAction(
            title="t", phase="general", polarity="do",
            use_cases="uc", hints=["h"], tech=None, confidence=0.6,
            source_observation_ids=["obs-active", "obs-archived"],
        )
        svc._apply_new([action], project="p")
        conn.commit()

        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-archived'"
        ).fetchone()
        assert row["status"] == "archived"
        # Active one was consumed as expected.
        active_row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-active'"
        ).fetchone()
        assert active_row["status"] == "consumed_into_reflection"


class TestApplyNewTimestampFreshness:
    """`_apply_new` must stamp each iteration with a fresh clock,
    matching the contract enforced for `_apply_augment` and
    `_apply_merge`. Skipped iterations (no valid sources) followed
    by executing ones must not carry the pre-loop timestamp.
    """

    def test_each_apply_new_iteration_uses_fresh_clock(self, conn):
        ticks = [
            datetime(2026, 4, 22, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 1, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 2, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 3, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 4, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 5, tzinfo=UTC),
            datetime(2026, 4, 22, 10, 0, 6, tzinfo=UTC),
        ]
        i = {"n": 0}
        def counter_clock() -> datetime:
            t = ticks[i["n"]]
            i["n"] += 1
            return t

        epsvc = EpisodeService(conn, clock=counter_clock)
        ep = epsvc.start_foreground(session_id="s1", project="p", goal="g")
        epsvc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        _insert_obs(conn, obs_id="obs-A", project="p", episode_id=ep)
        _insert_obs(conn, obs_id="obs-B", project="p", episode_id=ep)
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=counter_clock,
        )
        action_a = NewAction(
            title="ref-A", phase="general", polarity="do",
            use_cases="uc", hints=["h"], tech=None, confidence=0.6,
            source_observation_ids=["obs-A"],
        )
        action_b = NewAction(
            title="ref-B", phase="general", polarity="do",
            use_cases="uc", hints=["h"], tech=None, confidence=0.6,
            source_observation_ids=["obs-B"],
        )
        svc._apply_new([action_a, action_b], project="p")
        conn.commit()

        ts_a = conn.execute(
            "SELECT created_at FROM reflections WHERE title = 'ref-A'"
        ).fetchone()["created_at"]
        ts_b = conn.execute(
            "SELECT created_at FROM reflections WHERE title = 'ref-B'"
        ).fetchone()["created_at"]
        assert ts_a != ts_b
        assert ts_b > ts_a


class TestPickOldestPending:
    def test_returns_none_when_no_closed_episodes(
        self, conn, fixed_clock,
    ):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is None

    def test_returns_none_when_only_open_episodes(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) "
            "VALUES ('open-ep', 'p1', '2026-04-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_returns_oldest_pending_closed_episode(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) VALUES "
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','newer goal',NULL,NULL),"
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','older goal','python',NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is not None
        assert result.id == "older"
        assert result.project == "p1"
        assert result.goal == "older goal"
        assert result.tech == "python"
        assert result.outcome == "success"

    def test_excludes_already_synthesized_episodes(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('done','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','done goal','2026-04-01T01:00:00+00:00')"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_excludes_episodes_in_cooldown_window(self, conn, fixed_clock):
        from datetime import timedelta
        # Stamp 30s before fixed_clock — inside the 300s cooldown window.
        recent = (fixed_clock() - timedelta(seconds=30)).isoformat()
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at, synth_failed_at) VALUES "
            "('cooled','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL, ?)",
            (recent,),
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        assert svc._pick_oldest_pending(project="p1") is None

    def test_picks_episodes_after_cooldown_elapses(self, conn, fixed_clock):
        from datetime import timedelta
        # Stamp 600s before fixed_clock — outside the 300s cooldown window.
        old = (fixed_clock() - timedelta(seconds=600)).isoformat()
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at, synth_failed_at) VALUES "
            "('cooled','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL, ?)",
            (old,),
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        result = svc._pick_oldest_pending(project="p1")
        assert result is not None
        assert result.id == "cooled"


class TestLoadEpisodeContext:
    def test_loads_all_observations_regardless_of_status(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','test goal','python')"
        )
        _insert_obs(
            conn, obs_id="o-active", project="p1", episode_id="ep1",
            content="active obs", status="active",
            created_at="2026-04-01T00:30:00+00:00",
        )
        _insert_obs(
            conn, obs_id="o-consumed", project="p1", episode_id="ep1",
            content="consumed obs", status="consumed_into_reflection",
            created_at="2026-04-01T00:31:00+00:00",
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="test goal", tech="python", outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert {o.id for o in ctx.observations} == {"o-active", "o-consumed"}
        statuses = {o.id: o.status for o in ctx.observations}
        assert statuses["o-active"] == "active"
        assert statuses["o-consumed"] == "consumed_into_reflection"

    def test_filters_reflections_by_episode_tech_or_null(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal','python')"
        )
        _insert_reflection(conn, refl_id="r-py", project="p1", tech="python")
        _insert_reflection(conn, refl_id="r-any", project="p1", tech=None)
        _insert_reflection(conn, refl_id="r-rust", project="p1", tech="rust")
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech="python", outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        ids = {r.id for r in ctx.reflections}
        assert ids == {"r-py", "r-any"}
        assert "r-rust" not in ids

    def test_episode_with_no_tech_loads_all_reflections(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        _insert_reflection(conn, refl_id="r-py", project="p1", tech="python")
        _insert_reflection(conn, refl_id="r-any", project="p1", tech=None)
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert {r.id for r in ctx.reflections} == {"r-py", "r-any"}

    def test_excludes_retired_and_superseded_reflections(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        _insert_reflection(conn, refl_id="r-pending", project="p1", status="pending_review")
        _insert_reflection(conn, refl_id="r-confirmed", project="p1", status="confirmed")
        _insert_reflection(conn, refl_id="r-retired", project="p1", status="retired")
        _insert_reflection(conn, refl_id="r-super", project="p1", status="superseded")
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="goal", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        ids = {r.id for r in ctx.reflections}
        assert ids == {"r-pending", "r-confirmed"}


class TestBuildEpisodePrompt:
    def _ctx(self, observations=None, reflections=None, tech: str | None = "python"):
        return EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p1", goal="finish feature X", tech=tech,
                outcome="success",
            ),
            observations=observations or [],
            reflections=reflections or [],
        )

    def test_includes_episode_metadata(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx())
        assert "EPISODE" in prompt
        assert "finish feature X" in prompt
        assert "python" in prompt
        assert "success" in prompt

    def test_renders_unspecified_tech_when_none(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx(tech=None))
        assert "(unspecified)" in prompt

    def test_includes_each_observation_with_status(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        obs = ObservationForPrompt(
            id="o-1", content="found bug", outcome="success",
            component="api", theme="bug", tech="python",
            created_at="2026-04-01T00:30:00+00:00",
            episode_goal="g", episode_outcome="success",
            status="active",
        )
        prompt = svc._build_episode_prompt(self._ctx(observations=[obs]))
        assert "id=o-1" in prompt
        assert "found bug" in prompt
        assert "active" in prompt

    def test_marks_consumed_observations_status(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        obs = ObservationForPrompt(
            id="o-c", content="historical", outcome="success",
            component=None, theme=None, tech=None,
            created_at="2026-04-01T00:30:00+00:00",
            episode_goal="g", episode_outcome="success",
            status="consumed_into_reflection",
        )
        prompt = svc._build_episode_prompt(self._ctx(observations=[obs]))
        assert "consumed_into_reflection" in prompt

    def test_includes_existing_reflections(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        refl = ReflectionForPrompt(
            id="r-1", title="prefer try/except over LBYL",
            tech="python", phase="implementation", polarity="do",
            use_cases="error handling", hints='["wrap with except Exception"]',
            confidence=0.8, status="confirmed",
        )
        prompt = svc._build_episode_prompt(self._ctx(reflections=[refl]))
        assert "id=r-1" in prompt
        assert "prefer try/except over LBYL" in prompt
        assert "0.8" in prompt
        assert "confirmed" in prompt

    def test_includes_json_shape_instructions(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        prompt = svc._build_episode_prompt(self._ctx())
        assert '"new"' in prompt
        assert '"augment"' in prompt
        assert '"merge"' in prompt
        assert '"ignore"' in prompt


class TestMarkSynthesized:
    def test_sets_synthesized_at_to_clock_value(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._mark_synthesized("ep1")
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] == "2026-04-22T10:00:00+00:00"


class TestReadQueueCounts:
    def test_counts_zero_for_empty_project(self, conn, fixed_clock):
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="empty")
        assert counts.done == 0
        assert counts.pending == 0
        assert counts.in_cooldown == 0
        assert counts.total == 0

    def test_counts_done_pending_and_cooldown_separately(
        self, conn, fixed_clock,
    ):
        from datetime import timedelta
        # Stamp 30s before fixed_clock — well inside the 300s cooldown window.
        cooldown_recent = (fixed_clock() - timedelta(seconds=30)).isoformat()

        rows = [
            ("2026-04-01T01:00:00+00:00", None),  # done
            ("2026-04-01T01:00:00+00:00", None),  # done
            (None, None),                          # pending
            (None, None),                          # pending
            (None, None),                          # pending
            (None, cooldown_recent),               # cooldown
        ]
        for i, (synthesized_at, synth_failed_at) in enumerate(rows):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, ended_at, "
                "outcome, close_reason, goal, synthesized_at, synth_failed_at) "
                "VALUES (?, 'p1', '2026-04-01T00:00:00+00:00', "
                "'2026-04-01T01:00:00+00:00', 'success', 'goal_complete', 'g', ?, ?)",
                (f"e{i}", synthesized_at, synth_failed_at),
            )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="p1")
        assert counts.done == 2
        assert counts.pending == 3
        assert counts.in_cooldown == 1
        assert counts.total == 6

    def test_excludes_open_episodes_from_total(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) VALUES "
            "('open','p1','2026-04-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        counts = svc._read_queue_counts(project="p1")
        assert counts.total == 0


class TestSynthesizeNextHappyPath:
    def _empty_response(self) -> str:
        import json
        return json.dumps(
            {"new": [], "augment": [], "merge": [], "ignore": []}
        )

    def test_returns_processed_false_when_empty_queue(
        self, conn, fixed_clock,
    ):
        chat = FakeChat(responses=[])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is False
        assert step.episode_id is None
        assert step.failure is None
        assert step.queue.total == 0
        assert chat.calls == []

    def test_processes_oldest_pending_and_marks_synthesized(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','test goal','python',NULL)"
        )
        _insert_obs(
            conn, obs_id="o1", project="p1", episode_id="ep1",
            content="bug found", status="active",
            created_at="2026-04-01T00:30:00+00:00",
        )
        conn.commit()
        chat = FakeChat(responses=[self._empty_response()])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.episode_id == "ep1"
        assert step.failure is None
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] == "2026-04-22T10:00:00+00:00"
        assert step.queue.done == 1
        assert step.queue.pending == 0

    def test_counts_reflect_apply_actions(self, conn, fixed_clock):
        import json
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal')"
        )
        _insert_obs(
            conn, obs_id="o1", project="p1", episode_id="ep1",
            content="bug", status="active",
        )
        conn.commit()
        response = json.dumps({
            "new": [{
                "title": "lesson", "phase": "implementation",
                "polarity": "do", "use_cases": "uc",
                "hints": ["h1"], "tech": None, "confidence": 0.5,
                "source_observation_ids": ["o1"],
            }],
            "augment": [], "merge": [], "ignore": [],
        })
        chat = FakeChat(responses=[response])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.counts["created"] == 1
        assert step.counts["augmented"] == 0
        n = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE project='p1'"
        ).fetchone()[0]
        assert n == 1

    def test_oldest_first_order(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','newer goal',NULL),"
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','older goal',NULL)"
        )
        conn.commit()
        chat = FakeChat(responses=[self._empty_response()])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.episode_id == "older"
        assert step.queue.pending == 1
        chat.responses.append(self._empty_response())
        step2 = run_async(svc.synthesize_next(project="p1"))
        assert step2.episode_id == "newer"
        assert step2.queue.pending == 0


class TestSynthesizeNextFailurePaths:
    def test_chat_error_stamps_synth_failed_at_and_returns_failure(
        self, conn, fixed_clock,
    ):
        from better_memory.llm.ollama import ChatError

        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        class BoomChat:
            calls: list[str] = []
            async def complete(self, prompt: str) -> str:
                self.calls.append(prompt)
                raise ChatError("ollama unreachable")

        svc = ReflectionSynthesisService(
            conn, chat=BoomChat(), clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.episode_id == "ep1"
        assert step.failure == "ollama unreachable"
        assert step.counts == {"created": 0, "augmented": 0, "merged": 0,
                               "ignored": 0, "auto_ignored": 0}

        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is not None

    def test_parse_error_stamps_synth_failed_at(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        chat = FakeChat(responses=["not even valid json{{{"])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        step = run_async(svc.synthesize_next(project="p1"))
        assert step.processed is True
        assert step.failure is not None
        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is not None

    def test_cooldown_excludes_failed_episode_from_next_pick(
        self, conn, fixed_clock,
    ):
        from better_memory.llm.ollama import ChatError

        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('older','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','o',NULL),"
            "('newer','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
            "'success','goal_complete','n',NULL)"
        )
        conn.commit()

        class FlakyChat:
            calls = 0
            async def complete(self, prompt: str) -> str:
                FlakyChat.calls += 1
                if FlakyChat.calls == 1:
                    raise ChatError("transient")
                import json
                return json.dumps({"new": [], "augment": [], "merge": [], "ignore": []})

        svc = ReflectionSynthesisService(
            conn, chat=FlakyChat(), clock=fixed_clock,
        )
        step1 = run_async(svc.synthesize_next(project="p1"))
        assert step1.episode_id == "older"
        assert step1.failure is not None

        step2 = run_async(svc.synthesize_next(project="p1"))
        assert step2.episode_id == "newer"
        assert step2.failure is None

    def test_db_integrity_error_propagates_and_no_synth_failed_at(
        self, conn, fixed_clock, monkeypatch,
    ):
        import sqlite3

        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, synthesized_at) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','goal',NULL)"
        )
        conn.commit()

        def boom(self, actions, *, project):
            raise sqlite3.IntegrityError("simulated FK violation")

        monkeypatch.setattr(
            ReflectionSynthesisService, "_apply_new", boom
        )

        import json
        chat = FakeChat(responses=[json.dumps({
            "new": [{"title": "x", "phase": "general", "polarity": "do",
                     "use_cases": "u", "hints": ["h"], "tech": None,
                     "confidence": 0.5, "source_observation_ids": []}],
            "augment": [], "merge": [], "ignore": [],
        })])
        svc = ReflectionSynthesisService(
            conn, chat=chat, clock=fixed_clock,
        )
        with pytest.raises(sqlite3.IntegrityError):
            run_async(svc.synthesize_next(project="p1"))

        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is None


class TestSynthesisScopeDerivation:
    def test_apply_new_creates_general_when_all_sources_general(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        for i in (1, 2):
            _insert_obs(
                conn, obs_id=f"o{i}", project="p1", episode_id="ep1",
                content=f"obs {i}", status="active",
            )
            conn.execute(
                "UPDATE observations SET scope='general' WHERE id=?", (f"o{i}",)
            )
        conn.commit()

        from better_memory.services.reflection import NewAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_new(
            [NewAction(
                title="general rule", phase="general", polarity="do",
                use_cases="uc", hints=["h"], tech=None, confidence=0.5,
                source_observation_ids=["o1", "o2"],
            )],
            project="p1",
        )
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE title='general rule'"
        ).fetchone()
        assert row[0] == "general"

    def test_apply_new_creates_project_when_any_source_project(self, conn, fixed_clock):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        _insert_obs(conn, obs_id="o-general", project="p1", episode_id="ep1",
                    content="general obs", status="active")
        conn.execute("UPDATE observations SET scope='general' WHERE id='o-general'")
        _insert_obs(conn, obs_id="o-project", project="p1", episode_id="ep1",
                    content="project obs", status="active")
        conn.commit()

        from better_memory.services.reflection import NewAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_new(
            [NewAction(
                title="mixed rule", phase="general", polarity="do",
                use_cases="uc", hints=["h"], tech=None, confidence=0.5,
                source_observation_ids=["o-general", "o-project"],
            )],
            project="p1",
        )
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE title='mixed rule'"
        ).fetchone()
        assert row[0] == "project"

    def test_apply_augment_preserves_general_scope(self, conn, fixed_clock):
        _insert_reflection(conn, refl_id="r-general", project="p1")
        conn.execute("UPDATE reflections SET scope='general' WHERE id='r-general'")
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g')"
        )
        _insert_obs(conn, obs_id="o-proj", project="p1", episode_id="ep1",
                    content="project obs", status="active")
        conn.commit()

        from better_memory.services.reflection import AugmentAction
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        svc._apply_augment([AugmentAction(
            reflection_id="r-general", add_hints=["new"], rewrite_use_cases=None,
            confidence_delta=0.1, add_source_observation_ids=["o-proj"],
        )])
        conn.commit()
        row = conn.execute(
            "SELECT scope FROM reflections WHERE id='r-general'"
        ).fetchone()
        assert row[0] == "general"

    def test_load_episode_context_includes_general_reflections_from_other_projects(
        self, conn, fixed_clock,
    ):
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech) VALUES "
            "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
            "'success','goal_complete','g',NULL)"
        )
        _insert_reflection(conn, refl_id="r-other-general", project="p2", tech=None)
        conn.execute("UPDATE reflections SET scope='general' WHERE id='r-other-general'")
        conn.commit()

        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        episode = EpisodeForPrompt(
            id="ep1", project="p1", goal="g", tech=None, outcome="success",
        )
        ctx = svc._load_episode_context(episode)
        assert "r-other-general" in {r.id for r in ctx.reflections}


class TestRetrieveReflectionsScope:
    def test_retrieve_reflections_includes_general_from_other_projects(
        self, conn, fixed_clock,
    ):
        _insert_reflection(conn, refl_id="r-p1", project="p1")
        _insert_reflection(conn, refl_id="r-p2-general", project="p2")
        conn.execute(
            "UPDATE reflections SET scope='general' WHERE id='r-p2-general'"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        buckets = svc.retrieve_reflections(project="p1")
        all_ids = {r["id"] for bucket in buckets.values() for r in bucket}
        assert "r-p1" in all_ids
        assert "r-p2-general" in all_ids

    def test_retrieve_reflections_excludes_general_from_other_status(
        self, conn, fixed_clock,
    ):
        _insert_reflection(
            conn, refl_id="r-retired-general", project="p2", status="retired",
        )
        conn.execute(
            "UPDATE reflections SET scope='general' WHERE id='r-retired-general'"
        )
        conn.commit()
        svc = ReflectionSynthesisService(
            conn, chat=FakeChat(responses=[]), clock=fixed_clock,
        )
        buckets = svc.retrieve_reflections(project="p1")
        all_ids = {r["id"] for bucket in buckets.values() for r in bucket}
        assert "r-retired-general" not in all_ids
