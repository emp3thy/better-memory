"""RatingHandlers: list_session_exposures + apply_session_ratings + credit."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.ratings import RatingHandlers


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
    services.config.home = tmp_path  # type: ignore[misc]
    return services


@pytest.mark.asyncio
async def test_apply_session_ratings_raises_multi_line_value_error_when_no_session(
    tmp_path,
) -> None:
    services = _stub_services(tmp_path)
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value=None,
    ):
        with pytest.raises(ValueError) as exc:
            await handler.apply_session_ratings(services, {"ratings": []})
    # Pin the exact 3-line error text
    assert str(exc.value) == (
        "No active session: CLAUDE_SESSION_ID / "
        "CLAUDE_CODE_SESSION_ID not set and no session marker "
        "found (SessionStart hook may not have run)"
    )


@pytest.mark.asyncio
async def test_apply_session_ratings_forwards_ratings_when_session_present(
    tmp_path,
) -> None:
    services = _stub_services(tmp_path)
    services.memory_rating.apply_session_ratings = MagicMock(
        return_value={"applied": 2, "skipped": 0}
    )
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value="sess-1",
    ):
        result = await handler.apply_session_ratings(
            services,
            {"ratings": [{"kind": "reflection", "id": "r-1", "class": "cited"}]},
        )
    payload = json.loads(result[0].text)
    assert payload == {"applied": 2, "skipped": 0}
    services.memory_rating.apply_session_ratings.assert_called_once_with(
        session_id="sess-1",
        ratings=[{"kind": "reflection", "id": "r-1", "class": "cited"}],
    )


@pytest.mark.asyncio
async def test_credit_returns_no_session_shape_when_unresolved(tmp_path) -> None:
    services = _stub_services(tmp_path)
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value=None,
    ):
        result = await handler.credit(
            services, {"kind": "reflection", "id": "r-1", "class": "cited"}
        )
    payload = json.loads(result[0].text)
    assert payload == {"applied": None, "skipped": "no_session"}


@pytest.mark.asyncio
async def test_credit_calls_credit_one_when_session_present(tmp_path) -> None:
    services = _stub_services(tmp_path)
    services.memory_rating.credit_one = MagicMock(
        return_value={"applied": "cited", "skipped": None}
    )
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value="sess-1",
    ):
        result = await handler.credit(
            services, {"kind": "reflection", "id": "r-1", "class": "cited"}
        )
    payload = json.loads(result[0].text)
    assert payload == {"applied": "cited", "skipped": None}
    services.memory_rating.credit_one.assert_called_once_with(
        session_id="sess-1",
        kind="reflection",
        id="r-1",
        classification="cited",
    )


@pytest.mark.asyncio
async def test_list_session_exposures_coerces_none_to_empty_string(
    tmp_path,
) -> None:
    services = _stub_services(tmp_path)
    services.session_bootstrap.list_session_exposures = MagicMock(
        return_value={"session_id": None, "exposures": []}
    )
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value=None,
    ):
        result = await handler.list_session_exposures(services, {})
    payload = json.loads(result[0].text)
    assert payload == {"session_id": None, "exposures": []}
    services.session_bootstrap.list_session_exposures.assert_called_once_with(
        session_id="",
    )


@pytest.mark.asyncio
async def test_list_session_exposures_passes_resolved_session_id(
    tmp_path,
) -> None:
    services = _stub_services(tmp_path)
    services.session_bootstrap.list_session_exposures = MagicMock(
        return_value={"session_id": "sess-1", "exposures": []}
    )
    handler = RatingHandlers()
    with patch(
        "better_memory.mcp.handlers.ratings.resolve_session_id",
        return_value="sess-1",
    ):
        await handler.list_session_exposures(services, {})
    services.session_bootstrap.list_session_exposures.assert_called_once_with(
        session_id="sess-1",
    )


def test_handlers_registers_three() -> None:
    handler = RatingHandlers()
    assert [h.name for h in handler.handlers()] == [
        "memory.list_session_exposures",
        "memory.apply_session_ratings",
        "memory.credit",
    ]


def test_handlers_are_not_capability_gated() -> None:
    handler = RatingHandlers()
    assert all(not h.requires_synthesis for h in handler.handlers())
