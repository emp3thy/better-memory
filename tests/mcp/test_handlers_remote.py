"""Handler-level dispatch tests for the agentcore ``remote`` branches.

Each data-tool handler class takes an additive keyword-only
``remote: StorageBackend | None = None``. ``remote=None`` (the default)
must leave the sqlite service path byte-identical; a non-None remote
routes the data tools to the backend (spec Task 2, defect 2).

Everything here uses mocked services / mocked backends — no sqlite file,
no wire. The e2e proof over real botocore serialization lives in
``tests/e2e/test_agentcore_t2.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from better_memory.mcp.handlers import (
    EpisodeToolHandlers,
    ObservationToolHandlers,
    ReflectionToolHandlers,
    SemanticToolHandlers,
    SessionToolHandlers,
)
from better_memory.services.semantic import SemanticMemory


def _sm(*, id: str, content: str, scope: str = "project") -> SemanticMemory:
    """Build a SemanticMemory the agentcore semantic_list returns (§6.3)."""
    return SemanticMemory(
        id=id,
        content=content,
        project="projx",
        scope=scope,
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:00+00:00",
    )

#: >= 40 chars — botocore's client-side validation floor for memoryRecordId.
_RECORD_ID_40 = "refl-unit-" + "0" * 30
assert len(_RECORD_ID_40) == 40


def _payload(result: list[Any]) -> Any:
    assert len(result) == 1
    text = getattr(result[0], "text", None)
    assert isinstance(text, str)
    return json.loads(text)


@pytest.fixture
def observations() -> MagicMock:
    svc = MagicMock(name="ObservationService")
    svc.create = AsyncMock(return_value="local-obs-1")
    svc.list_observations = AsyncMock(return_value=[{"id": "local-obs-1"}])
    svc.record_use = MagicMock(return_value=None)
    return svc


@pytest.fixture
def retention() -> MagicMock:
    return MagicMock(name="RetentionService")


@pytest.fixture
def remote() -> MagicMock:
    backend = MagicMock(name="AgentCoreBackend")
    backend.observe = AsyncMock(return_value="evt-remote-1")
    backend.list_observations = AsyncMock(return_value=[])
    backend.record_use = MagicMock(return_value=None)
    backend.semantic_observe = MagicMock(return_value="sem-remote-1")
    backend.semantic_list = MagicMock(return_value=[])
    backend.semantic_update_text = MagicMock(return_value=None)
    backend.semantic_delete = MagicMock(return_value=None)
    backend.credit_one = MagicMock(return_value={"applied": "cited", "skipped": None})
    # Sqlite-parity shape (AgentCoreBackend mirrors MemoryRatingService).
    backend.apply_session_ratings = MagicMock(
        return_value={
            "session_id": "sid-env-1",
            "applied": {
                "cited": 1, "shaped": 0, "ignored": 0, "misled": 0,
                "overlooked": 0,
            },
            "skipped": {
                "not_exposed": 0, "already_rated": 0,
                "memory_missing": 0, "memory_retired": 0,
            },
        }
    )
    backend.list_session_exposures = MagicMock(
        return_value={"session_id": "s", "exposures": []}
    )
    backend.session_bootstrap = MagicMock(
        return_value={
            "additional_context": "ctx-from-backend",
            "project": "projx",
            "source": "startup",
            "episode_id": "sess-1",
            "episode_action": "opened",
            "semantic_count": 2,
            "reflections_counts": {"do": 1, "dont": 0, "neutral": 0},
        }
    )
    return backend


# ---------------------------------------------------------------------------
# ObservationToolHandlers
# ---------------------------------------------------------------------------


class TestObservationHandlersSqlitePath:
    """remote omitted / None → the existing service path, unchanged."""

    async def test_observe_defaults_to_service(self, observations, retention) -> None:
        handlers = ObservationToolHandlers(observations=observations, retention=retention)
        result = await handlers.observe({"content": "hello"})
        assert _payload(result) == {"id": "local-obs-1"}
        observations.create.assert_awaited_once()
        assert observations.create.await_args.kwargs["content"] == "hello"

    async def test_retrieve_observations_defaults_to_service(
        self, observations, retention
    ) -> None:
        handlers = ObservationToolHandlers(observations=observations, retention=retention)
        result = await handlers.retrieve_observations({"project": "projx"})
        assert _payload(result) == [{"id": "local-obs-1"}]
        observations.list_observations.assert_awaited_once()

    async def test_record_use_defaults_to_service(self, observations, retention) -> None:
        handlers = ObservationToolHandlers(observations=observations, retention=retention)
        result = await handlers.record_use({"id": "short-local-id", "outcome": "success"})
        assert _payload(result) == {"ok": True}
        observations.record_use.assert_called_once_with("short-local-id", outcome="success")


class TestObservationHandlersRemoteBranch:
    async def test_observe_routes_to_remote_not_service(
        self, observations, retention, remote
    ) -> None:
        handlers = ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        )
        result = await handlers.observe(
            {
                "content": "hello-remote",
                "component": "comp",
                "theme": "bug",
                "trigger_type": "review",
                "outcome": "failure",
                "tech": "python",
            }
        )
        assert _payload(result) == {"id": "evt-remote-1"}
        observations.create.assert_not_awaited()
        remote.observe.assert_awaited_once()
        kwargs = remote.observe.await_args.kwargs
        assert kwargs["content"] == "hello-remote"
        assert kwargs["component"] == "comp"
        assert kwargs["theme"] == "bug"
        assert kwargs["trigger_type"] == "review"
        assert kwargs["outcome"] == "failure"
        assert kwargs["tech"] == "python"
        assert kwargs["scope"] == "project"

    async def test_observe_remote_null_scope_defaults_to_project(
        self, observations, retention, remote
    ) -> None:
        """{"scope": null} from an MCP client must coerce to 'project' —
        same defence as the sqlite path (PR #25 BugBot finding)."""
        handlers = ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        )
        await handlers.observe({"content": "x", "scope": None})
        assert remote.observe.await_args.kwargs["scope"] == "project"

    async def test_retrieve_observations_routes_to_remote_and_serializes_datetimes(
        self, observations, retention, remote
    ) -> None:
        """The remote branch passes the backend's sqlite-parity rows through
        (and json.dumps(default=str) keeps any residual datetime safe)."""
        list_mock = AsyncMock(
            return_value=[
                {
                    # AgentCoreBackend.list_observations returns rows key-
                    # identical to the sqlite list_observations rows.
                    "id": "evt-1",
                    "content": "obs one",
                    "component": None,
                    "theme": None,
                    "outcome": "success",
                    "reinforcement_score": None,
                    "created_at": datetime(
                        2026, 7, 12, 10, 30, tzinfo=UTC
                    ).isoformat(),
                }
            ]
        )
        remote.list_observations = list_mock
        handlers = ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        )
        result = await handlers.retrieve_observations({"project": "projx", "limit": 7})
        rows = _payload(result)
        assert rows[0]["id"] == "evt-1"
        assert "2026" in rows[0]["created_at"]
        assert set(rows[0]) == {
            "id", "content", "component", "theme", "outcome",
            "reinforcement_score", "created_at",
        }
        observations.list_observations.assert_not_awaited()
        assert list_mock.await_args is not None
        kwargs = list_mock.await_args.kwargs
        assert kwargs["project"] == "projx"
        assert kwargs["limit"] == 7

    async def test_record_use_short_id_rejected_before_remote(
        self, observations, retention, remote
    ) -> None:
        """Event ids returned by memory.observe are NOT ratable AgentCore
        record ids; without this guard the backend stalls ~20s in its
        transient-404 retry loop inside the serialized dispatch loop."""
        handlers = ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        )
        with pytest.raises(ValueError, match="40"):
            await handlers.record_use({"id": "evt-too-short", "outcome": "success"})
        remote.record_use.assert_not_called()
        observations.record_use.assert_not_called()

    async def test_record_use_routes_to_remote_for_record_ids(
        self, observations, retention, remote
    ) -> None:
        handlers = ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        )
        result = await handlers.record_use({"id": _RECORD_ID_40, "outcome": "success"})
        assert _payload(result) == {"ok": True}
        remote.record_use.assert_called_once_with(_RECORD_ID_40, outcome="success")
        observations.record_use.assert_not_called()


# ---------------------------------------------------------------------------
# SemanticToolHandlers
# ---------------------------------------------------------------------------


class TestSemanticHandlersSqlitePath:
    async def test_semantic_retrieve_defaults_to_service(self) -> None:
        svc = MagicMock(name="SemanticMemoryService")
        svc.list_for_project = MagicMock(
            return_value=[
                SimpleNamespace(
                    id="sm-1",
                    content="fact",
                    project="projx",
                    scope="project",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                )
            ]
        )
        handlers = SemanticToolHandlers(semantic=svc)
        result = await handlers.semantic_retrieve({"project": "projx"})
        rows = _payload(result)
        assert rows[0]["id"] == "sm-1"
        assert rows[0]["project"] == "projx"
        svc.list_for_project.assert_called_once_with(project="projx")


class TestSemanticHandlersRemoteBranch:
    async def test_semantic_observe_routes_to_remote(self, remote) -> None:
        svc = MagicMock(name="SemanticMemoryService")
        handlers = SemanticToolHandlers(semantic=svc, remote=remote)
        result = await handlers.semantic_observe({"content": "pref", "scope": "general"})
        assert _payload(result) == {"id": "sem-remote-1"}
        svc.create.assert_not_called()
        kwargs = remote.semantic_observe.call_args.kwargs
        assert kwargs["content"] == "pref"
        assert kwargs["scope"] == "general"

    async def test_semantic_retrieve_merges_project_and_general_with_stable_keys(
        self, remote
    ) -> None:
        """UD-2: two backend calls (project namespace + general namespace)
        merged, payload keeps the sqlite key set with None placeholders."""

        def _semantic_list(**kwargs: Any) -> list[SemanticMemory]:
            # §6.3: agentcore semantic_list returns SemanticMemory objects,
            # matching the sqlite backend — the handler reads them via
            # attribute access.
            if kwargs.get("scope_filter") == "general":
                return [_sm(id="g1", content="general fact", scope="general")]
            return [_sm(id="p1", content="project fact", scope="project")]

        remote.semantic_list = MagicMock(side_effect=_semantic_list)
        svc = MagicMock(name="SemanticMemoryService")
        handlers = SemanticToolHandlers(semantic=svc, remote=remote)

        result = await handlers.semantic_retrieve({"project": "projx"})
        rows = _payload(result)
        assert [r["id"] for r in rows] == ["p1", "g1"]
        for row in rows:
            assert set(row) == {
                "id", "content", "project", "scope", "created_at", "updated_at",
            }
            assert row["project"] is None
            assert row["created_at"] is None
            assert row["updated_at"] is None
        scope_filters = [
            call.kwargs.get("scope_filter")
            for call in remote.semantic_list.call_args_list
        ]
        assert scope_filters == [None, "general"]
        svc.list_for_project.assert_not_called()

    async def test_semantic_retrieve_dedupes_duplicate_ids(self, remote) -> None:
        remote.semantic_list = MagicMock(
            return_value=[_sm(id="dup-1", content="same", scope="general")]
        )
        handlers = SemanticToolHandlers(semantic=MagicMock(), remote=remote)
        rows = _payload(await handlers.semantic_retrieve({}))
        assert [r["id"] for r in rows] == ["dup-1"]

    async def test_semantic_update_routes_to_remote(self, remote) -> None:
        svc = MagicMock(name="SemanticMemoryService")
        handlers = SemanticToolHandlers(semantic=svc, remote=remote)
        result = await handlers.semantic_update({"id": "sm-9", "content": "new"})
        assert _payload(result) == {"ok": True}
        remote.semantic_update_text.assert_called_once_with(id="sm-9", content="new")
        svc.update_text.assert_not_called()

    async def test_semantic_delete_routes_to_remote(self, remote) -> None:
        svc = MagicMock(name="SemanticMemoryService")
        handlers = SemanticToolHandlers(semantic=svc, remote=remote)
        result = await handlers.semantic_delete({"id": "sm-9"})
        assert _payload(result) == {"ok": True}
        remote.semantic_delete.assert_called_once_with(id="sm-9")
        svc.delete.assert_not_called()


# ---------------------------------------------------------------------------
# SessionToolHandlers
# ---------------------------------------------------------------------------


def _bootstrap_service() -> MagicMock:
    svc = MagicMock(name="SessionBootstrapService")
    svc.bootstrap = MagicMock(
        return_value=SimpleNamespace(
            additional_context="ctx-from-service",
            project="projx",
            source="startup",
            episode_id="ep-1",
            episode_action="opened",
            semantic_count=1,
            reflections_counts={"do": 0, "dont": 0, "neutral": 0},
        )
    )
    svc.list_session_exposures = MagicMock(
        return_value={"session_id": "sid", "exposures": []}
    )
    return svc


class TestSessionHandlersSqlitePath:
    async def test_session_bootstrap_defaults_to_service(self, tmp_path: Path) -> None:
        svc = _bootstrap_service()
        handlers = SessionToolHandlers(
            session_bootstrap=svc, memory_rating=MagicMock(), home=tmp_path
        )
        payload = _payload(await handlers.session_bootstrap({"source": "startup"}))
        assert payload["additionalContext"] == "ctx-from-service"
        assert payload["episode"] == {"id": "ep-1", "action": "opened"}
        assert payload["counts"]["semantic"] == 1
        svc.bootstrap.assert_called_once()


class TestSessionHandlersRemoteBranch:
    async def test_session_bootstrap_unwraps_backend_dict(
        self, tmp_path: Path, remote
    ) -> None:
        """AgentCoreBackend.session_bootstrap returns a DICT (not the sqlite
        BootstrapResult dataclass); the remote branch must key-access it —
        naive attribute unwrap AttributeErrors."""
        svc = _bootstrap_service()
        handlers = SessionToolHandlers(
            session_bootstrap=svc,
            memory_rating=MagicMock(),
            home=tmp_path,
            remote=remote,
        )
        payload = _payload(
            await handlers.session_bootstrap(
                {"source": "startup", "session_id": "sess-1"}
            )
        )
        assert payload["additionalContext"] == "ctx-from-backend"
        assert payload["project"] == "projx"
        assert payload["source"] == "startup"
        assert payload["episode"] == {"id": "sess-1", "action": "opened"}
        assert payload["counts"] == {
            "semantic": 2,
            "reflections": {"do": 1, "dont": 0, "neutral": 0},
        }
        svc.bootstrap.assert_not_called()
        kwargs = remote.session_bootstrap.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["source"] == "startup"

    async def test_list_session_exposures_routes_to_remote(
        self, tmp_path: Path, remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-env-1")
        svc = _bootstrap_service()
        handlers = SessionToolHandlers(
            session_bootstrap=svc,
            memory_rating=MagicMock(),
            home=tmp_path,
            remote=remote,
        )
        payload = _payload(await handlers.list_session_exposures({}))
        assert payload == {"session_id": "s", "exposures": []}
        remote.list_session_exposures.assert_called_once_with(session_id="sid-env-1")
        svc.list_session_exposures.assert_not_called()

    async def test_apply_session_ratings_routes_to_remote(
        self, tmp_path: Path, remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-env-1")
        rating = MagicMock(name="MemoryRatingService")
        handlers = SessionToolHandlers(
            session_bootstrap=_bootstrap_service(),
            memory_rating=rating,
            home=tmp_path,
            remote=remote,
        )
        ratings = [{"kind": "reflection", "id": _RECORD_ID_40, "class": "cited"}]
        payload = _payload(await handlers.apply_session_ratings({"ratings": ratings}))
        # The backend's sqlite-parity dict passes through unchanged.
        assert payload["session_id"] == "sid-env-1"
        assert payload["applied"]["cited"] == 1
        assert payload["skipped"]["memory_missing"] == 0
        remote.apply_session_ratings.assert_called_once_with(
            session_id="sid-env-1", ratings=ratings
        )
        rating.apply_session_ratings.assert_not_called()

    async def test_apply_session_ratings_keeps_no_session_guard_with_remote(
        self, tmp_path: Path, remote
    ) -> None:
        handlers = SessionToolHandlers(
            session_bootstrap=_bootstrap_service(),
            memory_rating=MagicMock(),
            home=tmp_path,
            remote=remote,
        )
        with pytest.raises(ValueError, match="No active session"):
            await handlers.apply_session_ratings({"ratings": []})
        remote.apply_session_ratings.assert_not_called()

    async def test_credit_routes_to_remote(
        self, tmp_path: Path, remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-env-1")
        rating = MagicMock(name="MemoryRatingService")
        handlers = SessionToolHandlers(
            session_bootstrap=_bootstrap_service(),
            memory_rating=rating,
            home=tmp_path,
            remote=remote,
        )
        payload = _payload(
            await handlers.credit(
                {"kind": "semantic", "id": _RECORD_ID_40, "class": "cited",
                 "evidence": "quoted its retry guidance"}
            )
        )
        assert payload == {"applied": "cited", "skipped": None}
        remote.credit_one.assert_called_once_with(
            session_id="sid-env-1",
            kind="semantic",
            id=_RECORD_ID_40,
            classification="cited",
            evidence="quoted its retry guidance",
        )
        rating.credit_one.assert_not_called()

    async def test_credit_keeps_no_session_guard_with_remote(
        self, tmp_path: Path, remote
    ) -> None:
        handlers = SessionToolHandlers(
            session_bootstrap=_bootstrap_service(),
            memory_rating=MagicMock(),
            home=tmp_path,
            remote=remote,
        )
        payload = _payload(
            await handlers.credit(
                {"kind": "semantic", "id": _RECORD_ID_40, "class": "cited"}
            )
        )
        assert payload == {"applied": None, "skipped": "no_session"}
        remote.credit_one.assert_not_called()


# ---------------------------------------------------------------------------
# ReflectionToolHandlers — memory.retrieve pre-hooks gating
# ---------------------------------------------------------------------------


def _reflection_handlers(tmp_path: Path, **extra: Any) -> tuple[Any, MagicMock]:
    backend = MagicMock(name="StorageBackend")
    backend.retrieve = MagicMock(return_value={"do": [], "dont": [], "neutral": []})
    spool = MagicMock(name="SpoolService")
    handlers = ReflectionToolHandlers(
        backend=backend,
        reflections=MagicMock(name="ReflectionSynthesisService"),
        spool=spool,
        memory_conn=MagicMock(name="memory_conn"),
        home=tmp_path,
        **extra,
    )
    return handlers, spool


class TestRetrievePreHooksGating:
    async def test_sqlite_mode_drains_spool_and_runs_retention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """remote=None → the existing best-effort pre-hooks still fire."""
        scheduler_cls = MagicMock(name="RetentionScheduler")
        monkeypatch.setattr(
            "better_memory.mcp.handlers.reflections.RetentionScheduler", scheduler_cls
        )
        handlers, spool = _reflection_handlers(tmp_path)
        result = await handlers.retrieve({})
        assert set(_payload(result)) == {"do", "dont", "neutral"}
        spool.drain.assert_called_once()
        scheduler_cls.assert_called_once()

    async def test_remote_mode_skips_spool_and_retention_pre_hooks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote
    ) -> None:
        """remote set → agentcore mode stops mutating local episode /
        retention rows on every memory.retrieve."""
        scheduler_cls = MagicMock(name="RetentionScheduler")
        monkeypatch.setattr(
            "better_memory.mcp.handlers.reflections.RetentionScheduler", scheduler_cls
        )
        handlers, spool = _reflection_handlers(tmp_path, remote=remote)
        result = await handlers.retrieve({})
        assert set(_payload(result)) == {"do", "dont", "neutral"}
        spool.drain.assert_not_called()
        scheduler_cls.assert_not_called()


# ---------------------------------------------------------------------------
# EpisodeToolHandlers — reconcile_episodes remote no-op
# ---------------------------------------------------------------------------


class TestReconcileEpisodes:
    async def test_sqlite_mode_lists_open_prior_episodes(self) -> None:
        episodes = MagicMock(name="EpisodeService")
        episodes.unclosed_episodes = MagicMock(return_value=[])
        observations = MagicMock(name="ObservationService")
        observations.session_id = "sid-1"
        handlers = EpisodeToolHandlers(
            episodes=episodes,
            observations=observations,
            reflections=MagicMock(),
            backend=MagicMock(),
        )
        assert _payload(await handlers.reconcile_episodes({})) == []
        episodes.unclosed_episodes.assert_called_once()

    async def test_remote_mode_returns_empty_without_touching_sqlite(
        self, remote
    ) -> None:
        """Tool is hidden in agentcore mode but the handler stays registered
        defensively — a direct call must not read stale local episodes."""
        episodes = MagicMock(name="EpisodeService")
        handlers = EpisodeToolHandlers(
            episodes=episodes,
            observations=MagicMock(),
            reflections=MagicMock(),
            backend=MagicMock(),
            remote=remote,
        )
        assert _payload(await handlers.reconcile_episodes({})) == []
        episodes.unclosed_episodes.assert_not_called()
