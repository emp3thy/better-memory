"""Tests for the memory.synthesize_next_* MCP tools.

These exercise three layers:

1. **Tool registration**: both new tool names appear in the factory's
   tool list with the expected input schemas.
2. **JSON payload helpers** (``_serialize_synth_get_context``,
   ``_serialize_synth_apply_ok``,
   ``_serialize_synth_apply_validation_error``): pure-function shape
   tests; the handler closures call these directly so the JSON wire
   format is what these helpers emit.
3. **End-to-end through the service**: seed a DB, drive
   ``ReflectionSynthesisService`` + the JSON helpers exactly as the
   handler closures do, and verify the round-trip from "fetch context"
   to "apply decision" lands the right rows.

Why not invoke ``_call_tool`` directly: the handler is a closure over
six service singletons assembled in ``create_server``. Lifting it to
a module-level function would touch all six. The helper-extraction
above (used by both the closure and these tests) eliminates the
"mirror drift" risk that ``test_start_ui_tool.py`` accepts as a
documented tradeoff.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.mcp.server import (
    _serialize_synth_apply_ok,
    _serialize_synth_apply_state_error,
    _serialize_synth_apply_validation_error,
    _serialize_synth_get_context,
    _tool_definitions,
)
from better_memory.services.reflection import (
    EpisodeContext,
    EpisodeForPrompt,
    EpisodeQueueCounts,
    ObservationForPrompt,
    ReflectionForPrompt,
    ReflectionSynthesisService,
    SynthesisStep,
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


# ---------------------------------------------------------------- registration


class TestSynthesizeToolRegistration:
    def test_get_context_tool_is_registered(self) -> None:
        names = {t.name for t in _tool_definitions()}
        assert "memory.synthesize_next_get_context" in names

    def test_apply_tool_is_registered(self) -> None:
        names = {t.name for t in _tool_definitions()}
        assert "memory.synthesize_next_apply" in names

    def test_get_context_schema_has_optional_project(self) -> None:
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.synthesize_next_get_context"
        )
        props = tool.inputSchema["properties"]
        assert "project" in props
        # Optional — not in required.
        assert "required" not in tool.inputSchema or \
            "project" not in tool.inputSchema["required"]

    def test_apply_schema_requires_episode_id_and_decision(self) -> None:
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.synthesize_next_apply"
        )
        required = set(tool.inputSchema.get("required", []))
        assert "episode_id" in required
        assert "decision" in required
        props = tool.inputSchema["properties"]
        assert props["decision"]["type"] == "object"


# ---------------------------------------------------------- helper-shape tests


class TestGetContextSerializer:
    def _empty_queue(self) -> EpisodeQueueCounts:
        return EpisodeQueueCounts(done=0, pending=0, in_cooldown=0)

    def test_returns_null_episode_when_ctx_is_none(self) -> None:
        queue = EpisodeQueueCounts(done=2, pending=0, in_cooldown=1)
        payload = _serialize_synth_get_context(None, queue)
        assert payload == {
            "episode_id": None,
            "queue": {"pending": 0, "in_cooldown": 1, "done": 2},
        }

    def test_serializes_episode_metadata(self) -> None:
        ctx = EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="myapp", goal="fix bug",
                tech="python", outcome="success",
            ),
            observations=[],
            reflections=[],
        )
        payload = _serialize_synth_get_context(ctx, self._empty_queue())
        assert payload["episode_id"] == "ep1"
        assert payload["episode"] == {
            "id": "ep1",
            "project": "myapp",
            "goal": "fix bug",
            "tech": "python",
            "outcome": "success",
        }
        assert payload["observations"] == []
        assert payload["reflections"] == []

    def test_serializes_observations_with_all_fields(self) -> None:
        obs = ObservationForPrompt(
            id="o-1", content="bug found", outcome="failure",
            component="api", theme="bug", tech="python",
            created_at="2026-04-01T00:30:00+00:00",
            episode_goal="g", episode_outcome="success",
            status="active",
        )
        ctx = EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p", goal="g", tech=None,
                outcome="success",
            ),
            observations=[obs],
            reflections=[],
        )
        payload = _serialize_synth_get_context(ctx, self._empty_queue())
        assert payload["observations"] == [{
            "id": "o-1",
            "content": "bug found",
            "outcome": "failure",
            "component": "api",
            "theme": "bug",
            "tech": "python",
            "created_at": "2026-04-01T00:30:00+00:00",
            "status": "active",
        }]

    def test_decodes_reflection_hints_from_json_string(self) -> None:
        """The DB stores hints as ``json.dumps(list[str])``; the wire
        format must hand the LLM a real list, not a JSON-string-of-list."""
        refl = ReflectionForPrompt(
            id="r-1", title="prefer pathlib",
            tech="python", phase="implementation", polarity="do",
            use_cases="filesystem ops",
            hints='["use Path", "supports / operator"]',
            confidence=0.7, status="confirmed",
        )
        ctx = EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p", goal=None, tech=None,
                outcome="success",
            ),
            observations=[],
            reflections=[refl],
        )
        payload = _serialize_synth_get_context(ctx, self._empty_queue())
        assert payload["reflections"] == [{
            "id": "r-1",
            "title": "prefer pathlib",
            "tech": "python",
            "phase": "implementation",
            "polarity": "do",
            "use_cases": "filesystem ops",
            "hints": ["use Path", "supports / operator"],
            "confidence": 0.7,
            "status": "confirmed",
        }]

    def test_handles_empty_hints_string(self) -> None:
        """A hints column of '' or None must serialize to []."""
        refl = ReflectionForPrompt(
            id="r-1", title="t", tech=None, phase="general",
            polarity="neutral", use_cases="uc",
            hints="",
            confidence=0.5, status="pending_review",
        )
        ctx = EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p", goal=None, tech=None,
                outcome="success",
            ),
            observations=[],
            reflections=[refl],
        )
        payload = _serialize_synth_get_context(ctx, self._empty_queue())
        assert payload["reflections"][0]["hints"] == []

    def test_payload_is_json_serializable(self) -> None:
        """The handler wraps the dict in json.dumps; nothing in the
        payload should be a non-JSON-native type."""
        ctx = EpisodeContext(
            episode=EpisodeForPrompt(
                id="ep1", project="p", goal="g",
                tech=None, outcome="success",
            ),
            observations=[
                ObservationForPrompt(
                    id="o-1", content="c", outcome="success",
                    component=None, theme=None, tech=None,
                    created_at="2026-04-01T00:00:00+00:00",
                    episode_goal=None, episode_outcome=None,
                ),
            ],
            reflections=[],
        )
        payload = _serialize_synth_get_context(ctx, self._empty_queue())
        # Round-trip through json must be lossless.
        encoded = json.dumps(payload)
        assert json.loads(encoded) == payload


class TestApplyOkSerializer:
    def test_serializes_step_to_ok_payload(self) -> None:
        step = SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 1, "augmented": 0, "merged": 0,
                    "ignored": 2, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=5, pending=3, in_cooldown=0),
            failure=None,
        )
        payload = _serialize_synth_apply_ok(step)
        assert payload == {
            "ok": True,
            "episode_id": "ep1",
            "counts": {"created": 1, "augmented": 0, "merged": 0,
                       "ignored": 2, "auto_ignored": 0},
            "queue": {"pending": 3, "in_cooldown": 0, "done": 5},
        }

    def test_payload_is_json_serializable(self) -> None:
        step = SynthesisStep(
            processed=True, episode_id="ep1",
            counts={"created": 0, "augmented": 0, "merged": 0,
                    "ignored": 0, "auto_ignored": 0},
            queue=EpisodeQueueCounts(done=0, pending=0, in_cooldown=0),
            failure=None,
        )
        payload = _serialize_synth_apply_ok(step)
        assert json.loads(json.dumps(payload)) == payload


class TestApplyValidationErrorSerializer:
    def test_returns_validation_error_shape(self) -> None:
        payload = _serialize_synth_apply_validation_error(
            "new entry: missing required field 'source_observation_ids'"
        )
        assert payload == {
            "ok": False,
            "error": "validation",
            "message": (
                "new entry: missing required field 'source_observation_ids'"
            ),
        }


class TestApplyStateErrorSerializer:
    def test_returns_state_error_shape(self) -> None:
        payload = _serialize_synth_apply_state_error(
            "Episode 'ep1' is already synthesized"
        )
        assert payload == {
            "ok": False,
            "error": "state",
            "message": "Episode 'ep1' is already synthesized",
        }


# ----------------------------------------------------- end-to-end via service


def _seed_episode(
    conn,
    *,
    eid: str,
    project: str = "p1",
    tech: str | None = None,
    goal: str = "goal",
    ended_at: str = "2026-04-01T01:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, "
        "outcome, close_reason, goal, tech, synthesized_at) VALUES "
        "(?, ?, '2026-04-01T00:00:00+00:00', ?, 'success', "
        "'goal_complete', ?, ?, NULL)",
        (eid, project, ended_at, goal, tech),
    )


def _seed_obs(conn, *, oid: str, eid: str, project: str = "p1") -> None:
    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, outcome,
            reinforcement_score, episode_id, tech, created_at, status,
            status_changed_at
        ) VALUES (?, ?, ?, NULL, NULL, 'success', 0.0, ?, NULL,
                  '2026-04-01T00:30:00+00:00', 'active',
                  '2026-04-01T00:30:00+00:00')
        """,
        (oid, f"observation {oid}", project, eid),
    )


class TestEndToEndGetContext:
    def test_empty_queue_yields_null_episode_payload(
        self, conn, fixed_clock,
    ):
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        ctx = svc.get_next_pending_context(project="p1")
        queue = svc.read_queue_counts(project="p1")
        payload = _serialize_synth_get_context(ctx, queue)
        assert payload == {
            "episode_id": None,
            "queue": {"pending": 0, "in_cooldown": 0, "done": 0},
        }

    def test_pending_episode_yields_full_payload(self, conn, fixed_clock):
        _seed_episode(conn, eid="ep1", tech="python", goal="fix flake")
        _seed_obs(conn, oid="o-1", eid="ep1")
        conn.commit()

        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        ctx = svc.get_next_pending_context(project="p1")
        queue = svc.read_queue_counts(project="p1")
        assert ctx is not None
        payload = _serialize_synth_get_context(ctx, queue)

        assert payload["episode_id"] == "ep1"
        assert payload["episode"]["goal"] == "fix flake"
        assert payload["episode"]["tech"] == "python"
        assert [o["id"] for o in payload["observations"]] == ["o-1"]
        assert payload["queue"]["pending"] == 1
        assert payload["queue"]["done"] == 0

    def test_oldest_episode_first(self, conn, fixed_clock):
        _seed_episode(
            conn, eid="newer",
            ended_at="2026-04-02T01:00:00+00:00",
        )
        _seed_episode(
            conn, eid="older",
            ended_at="2026-04-01T01:00:00+00:00",
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        ctx = svc.get_next_pending_context(project="p1")
        queue = svc.read_queue_counts(project="p1")
        payload = _serialize_synth_get_context(ctx, queue)
        assert payload["episode_id"] == "older"


class TestEndToEndApplyDecision:
    def _good_decision(self, obs_id: str) -> dict:
        return {
            "new": [{
                "title": "lesson", "phase": "implementation",
                "polarity": "do", "use_cases": "uc",
                "hints": ["h"], "tech": None, "confidence": 0.5,
                "source_observation_ids": [obs_id],
            }],
            "augment": [], "merge": [], "ignore": [],
        }

    def test_good_decision_creates_reflection_and_marks_synthesized(
        self, conn, fixed_clock,
    ):
        _seed_episode(conn, eid="ep1")
        _seed_obs(conn, oid="o-1", eid="ep1")
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)

        # Mirror the handler: parse_response → apply_decision → serialize.
        decision = self._good_decision("o-1")
        response = svc.parse_response(json.dumps(decision))
        step = svc.apply_decision(
            episode_id="ep1", response=response, project="p1",
        )
        payload = _serialize_synth_apply_ok(step)

        assert payload["ok"] is True
        assert payload["episode_id"] == "ep1"
        assert payload["counts"]["created"] == 1
        assert payload["queue"]["done"] == 1
        assert payload["queue"]["pending"] == 0

        # Verify the reflection actually landed in the DB.
        n = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE project='p1'"
        ).fetchone()[0]
        assert n == 1

        # Verify the episode is marked synthesized.
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] is not None

    def test_bad_decision_yields_validation_error_payload(
        self, conn, fixed_clock,
    ):
        """A decision missing a required field must surface as
        ``{ok: false, error: 'validation'}`` from the helper, with the
        episode left untouched (no synthesized_at, no synth_failed_at).
        """
        from better_memory.services.reflection import SynthesisResponseError

        _seed_episode(conn, eid="ep1")
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)

        # Missing required field 'source_observation_ids'.
        bad = {
            "new": [{
                "title": "x", "phase": "general", "polarity": "do",
                "use_cases": "u", "hints": ["h"], "tech": None,
                "confidence": 0.5,
                # no source_observation_ids
            }],
            "augment": [], "merge": [], "ignore": [],
        }

        # Mirror the handler's try/except: parse_response raises;
        # serialize_synth_apply_validation_error builds the payload.
        with pytest.raises(SynthesisResponseError) as excinfo:
            svc.parse_response(json.dumps(bad))
        payload = _serialize_synth_apply_validation_error(str(excinfo.value))

        assert payload["ok"] is False
        assert payload["error"] == "validation"
        assert "source_observation_ids" in payload["message"]

        # Episode untouched — no DB-level failure stamp.
        row = conn.execute(
            "SELECT synthesized_at, synth_failed_at FROM episodes "
            "WHERE id='ep1'"
        ).fetchone()
        assert row["synthesized_at"] is None
        assert row["synth_failed_at"] is None

    def test_state_error_when_episode_belongs_to_other_project(
        self, conn, fixed_clock,
    ):
        """The MCP layer turns ValueError from apply_decision into a
        structured state-error payload (so the LLM can refetch context
        instead of retrying the same stale id)."""
        _seed_episode(conn, eid="ep1", project="project_a")
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)

        empty = {"new": [], "augment": [], "merge": [], "ignore": []}
        response = svc.parse_response(json.dumps(empty))
        # Mirror the handler: catch ValueError → state-error payload.
        try:
            svc.apply_decision(
                episode_id="ep1", response=response, project="project_b",
            )
        except ValueError as exc:
            payload = _serialize_synth_apply_state_error(str(exc))
        else:
            pytest.fail("apply_decision should have raised ValueError")

        assert payload["ok"] is False
        assert payload["error"] == "state"
        assert "project" in payload["message"]
        # Episode untouched.
        row = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()
        assert row[0] is None

    def test_state_error_when_episode_already_synthesized(
        self, conn, fixed_clock,
    ):
        _seed_episode(conn, eid="ep1")
        conn.execute(
            "UPDATE episodes SET synthesized_at = ? WHERE id = 'ep1'",
            ("2026-04-01T02:00:00+00:00",),
        )
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)

        empty = {"new": [], "augment": [], "merge": [], "ignore": []}
        response = svc.parse_response(json.dumps(empty))
        try:
            svc.apply_decision(
                episode_id="ep1", response=response, project="p1",
            )
        except ValueError as exc:
            payload = _serialize_synth_apply_state_error(str(exc))
        else:
            pytest.fail("apply_decision should have raised ValueError")

        assert payload["error"] == "state"
        assert "already synthesized" in payload["message"]

    def test_empty_decision_marks_episode_synthesized(
        self, conn, fixed_clock,
    ):
        """All-empty decision is a valid 'nothing to distill' result —
        episode should still be marked synthesized so the queue advances."""
        _seed_episode(conn, eid="ep1")
        _seed_obs(conn, oid="o-1", eid="ep1")
        conn.commit()
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)

        empty = {"new": [], "augment": [], "merge": [], "ignore": []}
        response = svc.parse_response(json.dumps(empty))
        step = svc.apply_decision(
            episode_id="ep1", response=response, project="p1",
        )
        payload = _serialize_synth_apply_ok(step)

        assert payload["counts"]["created"] == 0
        # Auto-ignore catches the still-active observation.
        assert payload["counts"]["auto_ignored"] == 1

        status = conn.execute(
            "SELECT status FROM observations WHERE id='o-1'"
        ).fetchone()[0]
        assert status == "consumed_without_reflection"

        synthesized_at = conn.execute(
            "SELECT synthesized_at FROM episodes WHERE id='ep1'"
        ).fetchone()[0]
        assert synthesized_at is not None
