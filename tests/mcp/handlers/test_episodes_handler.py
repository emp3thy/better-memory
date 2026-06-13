"""EpisodeHandlers: lifecycle tools (start, close try/except, reconcile, list 10-field)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.episodes import EpisodeHandlers


def _stub_services() -> ServiceContainer:
    services = ServiceContainer(
        config=MagicMock(),
        memory_conn=MagicMock(),
        backend=MagicMock(),
        episodes=MagicMock(),
        observations=MagicMock(),
        reflections=MagicMock(),
        retention=MagicMock(),
        memory_rating=MagicMock(),
        knowledge=MagicMock(),
        spool=MagicMock(),
        semantic=MagicMock(),
        session_bootstrap=MagicMock(),
    )
    services.observations.session_id = "sess-1"
    return services


@pytest.mark.asyncio
async def test_start_episode_returns_episode_id_reflections_and_pending_synthesis() -> None:
    services = _stub_services()
    services.episodes.start_foreground = MagicMock(return_value="ep-1")
    queue = MagicMock()
    queue.pending = 2
    queue.in_cooldown = 1
    queue.done = 5
    services.reflections.read_queue_counts = MagicMock(return_value=queue)
    services.backend.retrieve = MagicMock(
        return_value={"do": ["a"], "dont": [], "neutral": []},
    )
    handler = EpisodeHandlers()
    result = await handler.start_episode(services, {"goal": "ship it", "tech": "py"})
    payload = json.loads(result[0].text)
    assert payload == {
        "episode_id": "ep-1",
        "reflections": {"do": ["a"], "dont": [], "neutral": []},
        "pending_synthesis": {"pending": 2, "in_cooldown": 1, "done": 5},
    }
    services.episodes.start_foreground.assert_called_once()
    call_kwargs = services.episodes.start_foreground.call_args.kwargs
    assert call_kwargs["session_id"] == "sess-1"
    assert call_kwargs["goal"] == "ship it"
    assert call_kwargs["tech"] == "py"


@pytest.mark.asyncio
async def test_close_episode_alt_payload_when_value_error() -> None:
    """ValueError -> already_closed payload shape."""
    services = _stub_services()
    services.episodes.close_active = MagicMock(side_effect=ValueError("none active"))
    handler = EpisodeHandlers()
    result = await handler.close_episode(services, {"outcome": "success"})
    payload = json.loads(result[0].text)
    assert payload == {"closed_episode_id": None, "already_closed": True}


@pytest.mark.asyncio
async def test_close_episode_happy_path_payload() -> None:
    services = _stub_services()
    services.episodes.close_active = MagicMock(return_value="ep-42")
    handler = EpisodeHandlers()
    result = await handler.close_episode(services, {"outcome": "success"})
    payload = json.loads(result[0].text)
    assert payload == {"closed_episode_id": "ep-42", "already_closed": False}
    # Default close_reason mapping for outcome='success' is 'goal_complete'.
    assert services.episodes.close_active.call_args.kwargs["close_reason"] == "goal_complete"


@pytest.mark.asyncio
async def test_close_episode_respects_explicit_close_reason() -> None:
    services = _stub_services()
    services.episodes.close_active = MagicMock(return_value="ep-7")
    handler = EpisodeHandlers()
    await handler.close_episode(
        services,
        {"outcome": "partial", "close_reason": "superseded", "summary": "later"},
    )
    kwargs = services.episodes.close_active.call_args.kwargs
    assert kwargs["close_reason"] == "superseded"
    assert kwargs["summary"] == "later"
    assert kwargs["outcome"] == "partial"


@pytest.mark.asyncio
async def test_reconcile_episodes_excludes_current_session() -> None:
    services = _stub_services()
    ep = MagicMock()
    ep.id = "ep-9"
    ep.project = "p"
    ep.tech = "py"
    ep.goal = "g"
    ep.started_at = "t0"
    services.episodes.unclosed_episodes = MagicMock(return_value=[ep])
    handler = EpisodeHandlers()
    result = await handler.reconcile_episodes(services, {})
    payload = json.loads(result[0].text)
    assert payload == [
        {
            "episode_id": "ep-9",
            "project": "p",
            "tech": "py",
            "goal": "g",
            "started_at": "t0",
        }
    ]
    services.episodes.unclosed_episodes.assert_called_once_with(
        exclude_session_ids={"sess-1"},
    )


@pytest.mark.asyncio
async def test_list_episodes_emits_10_fields_per_episode() -> None:
    services = _stub_services()
    ep = MagicMock()
    ep.id = "e1"
    ep.project = "p"
    ep.tech = "py"
    ep.goal = "g"
    ep.started_at = "t0"
    ep.hardened_at = "t1"
    ep.ended_at = "t2"
    ep.close_reason = "r"
    ep.outcome = "success"
    ep.summary = "s"
    services.episodes.list_episodes = MagicMock(return_value=[ep])
    handler = EpisodeHandlers()
    result = await handler.list_episodes(services, {})
    payload = json.loads(result[0].text)
    assert len(payload) == 1
    assert set(payload[0].keys()) == {
        "episode_id",
        "project",
        "tech",
        "goal",
        "started_at",
        "hardened_at",
        "ended_at",
        "close_reason",
        "outcome",
        "summary",
    }
    # only_open default is False; filters pass through verbatim.
    services.episodes.list_episodes.assert_called_once_with(
        project=None, outcome=None, only_open=False,
    )


def test_handlers_registers_four() -> None:
    handler = EpisodeHandlers()
    names = [h.name for h in handler.handlers()]
    assert names == [
        "memory.start_episode",
        "memory.close_episode",
        "memory.reconcile_episodes",
        "memory.list_episodes",
    ]
