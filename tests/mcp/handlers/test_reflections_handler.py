"""ReflectionHandlers: synthesize_next_get_context + synthesize_next_apply (3 result_kinds)."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.reflections import ReflectionHandlers
from better_memory.services.reflection import SynthesisResponseError


def _stub_services(tmp_path) -> ServiceContainer:
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
    # ServiceContainer types config as Config (home is read-only on the
    # real class); we substitute a MagicMock at runtime so cast through
    # Any to assign home without tripping pyright's read-only check.
    config: Any = services.config
    config.home = tmp_path
    return services


@pytest.mark.asyncio
async def test_get_context_returns_empty_payload_when_queue_empty(tmp_path) -> None:
    services = _stub_services(tmp_path)
    services.reflections.get_next_pending_context = MagicMock(return_value=None)
    queue = MagicMock()
    queue.pending = 0
    queue.in_cooldown = 0
    queue.done = 3
    services.reflections.read_queue_counts = MagicMock(return_value=queue)
    handler = ReflectionHandlers()
    result = await handler.get_context(services, {})
    payload = json.loads(result[0].text)
    assert payload == {
        "episode_id": None,
        "queue": {"pending": 0, "in_cooldown": 0, "done": 3},
    }


@pytest.mark.asyncio
async def test_get_context_returns_episode_bundle_when_queue_non_empty(tmp_path) -> None:
    services = _stub_services(tmp_path)
    ctx = MagicMock()
    ctx.episode.id = "ep-1"
    ctx.episode.project = "p"
    ctx.episode.goal = "g"
    ctx.episode.tech = "py"
    ctx.episode.outcome = "success"
    ctx.observations = []
    ctx.reflections = []
    services.reflections.get_next_pending_context = MagicMock(return_value=ctx)
    queue = MagicMock()
    queue.pending = 1
    queue.in_cooldown = 0
    queue.done = 2
    services.reflections.read_queue_counts = MagicMock(return_value=queue)
    handler = ReflectionHandlers()
    result = await handler.get_context(services, {})
    payload = json.loads(result[0].text)
    assert payload["episode_id"] == "ep-1"
    assert payload["queue"] == {"pending": 1, "in_cooldown": 0, "done": 2}
    assert payload["episode"]["id"] == "ep-1"


@pytest.mark.asyncio
async def test_apply_returns_validation_error_payload_on_synthesis_response_error(tmp_path) -> None:
    services = _stub_services(tmp_path)
    services.reflections.parse_response_dict = MagicMock(
        side_effect=SynthesisResponseError("bad shape")
    )
    handler = ReflectionHandlers()
    result = await handler.apply(services, {"episode_id": "ep-1", "decision": {}})
    payload = json.loads(result[0].text)
    assert payload == {
        "ok": False,
        "error": "validation",
        "message": "bad shape",
    }
    # apply_decision must NOT have been called when parse fails. The
    # underlying reflections service is a MagicMock at runtime; cast
    # through Any so pyright accepts the assert_not_called attr.
    apply_decision: Any = services.reflections.apply_decision
    apply_decision.assert_not_called()


@pytest.mark.asyncio
async def test_apply_returns_state_error_payload_on_value_error(tmp_path) -> None:
    services = _stub_services(tmp_path)
    services.reflections.parse_response_dict = MagicMock(return_value=MagicMock())
    services.reflections.apply_decision = MagicMock(side_effect=ValueError("stale"))
    handler = ReflectionHandlers()
    result = await handler.apply(services, {"episode_id": "ep-1", "decision": {}})
    payload = json.loads(result[0].text)
    assert payload == {
        "ok": False,
        "error": "state",
        "message": "stale",
    }


@pytest.mark.asyncio
async def test_apply_returns_ok_payload_on_success(tmp_path) -> None:
    services = _stub_services(tmp_path)
    services.reflections.parse_response_dict = MagicMock(return_value=MagicMock())
    step = MagicMock()
    step.episode_id = "ep-1"
    step.counts = {"new": 1, "augment": 0, "merge": 0, "ignore": 0}
    step.queue.pending = 0
    step.queue.in_cooldown = 0
    step.queue.done = 5
    services.reflections.apply_decision = MagicMock(return_value=step)
    handler = ReflectionHandlers()
    result = await handler.apply(services, {"episode_id": "ep-1", "decision": {}})
    payload = json.loads(result[0].text)
    assert payload == {
        "ok": True,
        "episode_id": "ep-1",
        "counts": {"new": 1, "augment": 0, "merge": 0, "ignore": 0},
        "queue": {"pending": 0, "in_cooldown": 0, "done": 5},
    }


def test_handlers_registers_both_capability_gated() -> None:
    handler = ReflectionHandlers()
    handlers_list = handler.handlers()
    assert [h.name for h in handlers_list] == [
        "memory.synthesize_next_get_context",
        "memory.synthesize_next_apply",
    ]
    assert all(h.requires_synthesis for h in handlers_list)
