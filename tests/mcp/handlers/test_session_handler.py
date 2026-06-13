"""SessionHandlers: session_bootstrap (4-tier session_id fallback, 5-key payload) + start_ui."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.session import SessionHandlers


def _stub_services() -> ServiceContainer:
    return ServiceContainer(
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


def _bootstrap_result() -> MagicMock:
    r = MagicMock()
    r.additional_context = "ctx"
    r.project = "p"
    r.source = "startup"
    r.episode_id = "ep-1"
    r.episode_action = "opened"
    r.semantic_count = 5
    r.reflections_counts = {"do": 3, "dont": 1}
    return r


@pytest.mark.asyncio
async def test_session_bootstrap_payload_has_5_top_keys() -> None:
    services = _stub_services()
    services.session_bootstrap.bootstrap = MagicMock(
        return_value=_bootstrap_result()
    )
    handler = SessionHandlers()
    result = await handler.session_bootstrap(services, {})
    payload = json.loads(result[0].text)
    assert set(payload.keys()) == {
        "additionalContext",
        "project",
        "source",
        "episode",
        "counts",
    }
    assert payload["episode"] == {"id": "ep-1", "action": "opened"}
    assert payload["counts"] == {
        "semantic": 5,
        "reflections": {"do": 3, "dont": 1},
    }


@pytest.mark.asyncio
async def test_session_bootstrap_uses_args_session_id_first() -> None:
    services = _stub_services()
    services.session_bootstrap.bootstrap = MagicMock(
        return_value=_bootstrap_result()
    )
    handler = SessionHandlers()
    await handler.session_bootstrap(services, {"session_id": "from-arg"})
    call_kwargs = services.session_bootstrap.bootstrap.call_args.kwargs
    assert call_kwargs["session_id"] == "from-arg"


@pytest.mark.asyncio
async def test_session_bootstrap_uses_cwd_arg_when_provided() -> None:
    services = _stub_services()
    services.session_bootstrap.bootstrap = MagicMock(
        return_value=_bootstrap_result()
    )
    handler = SessionHandlers()
    await handler.session_bootstrap(services, {"cwd": "/tmp/foo"})
    call_kwargs = services.session_bootstrap.bootstrap.call_args.kwargs
    assert call_kwargs["cwd"] == Path("/tmp/foo")


@pytest.mark.asyncio
async def test_start_ui_returns_launcher_result() -> None:
    services = _stub_services()
    handler = SessionHandlers()
    with patch(
        "better_memory.mcp.handlers.session.ui_launcher.start_ui",
        return_value={"url": "http://localhost:8000", "reused": False},
    ):
        result = await handler.start_ui(services, {})
    payload = json.loads(result[0].text)
    assert payload == {"url": "http://localhost:8000", "reused": False}


def test_handlers_registers_two() -> None:
    handler = SessionHandlers()
    assert [h.name for h in handler.handlers()] == [
        "memory.session_bootstrap",
        "memory.start_ui",
    ]
