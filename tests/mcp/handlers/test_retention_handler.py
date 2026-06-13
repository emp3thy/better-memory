"""RetentionHandlers: memory.run_retention."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.retention import RetentionHandlers
from better_memory.services.retention import RetentionReport


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


@pytest.mark.asyncio
async def test_run_retention_serializes_all_four_report_fields() -> None:
    services = _stub_services()
    report = RetentionReport(
        archived_via_retired_reflection=3,
        archived_via_consumed_without_reflection=5,
        archived_via_no_outcome_episode=2,
        pruned=7,
    )
    services.retention.run = MagicMock(return_value=report)
    handler = RetentionHandlers()
    result = await handler.run_retention(services, {})
    payload = json.loads(result[0].text)
    assert payload == {
        "archived_via_retired_reflection": 3,
        "archived_via_consumed_without_reflection": 5,
        "archived_via_no_outcome_episode": 2,
        "pruned": 7,
    }


@pytest.mark.asyncio
async def test_run_retention_forwards_args_with_spec_defaults() -> None:
    services = _stub_services()
    report = RetentionReport(
        archived_via_retired_reflection=0,
        archived_via_consumed_without_reflection=0,
        archived_via_no_outcome_episode=0,
        pruned=0,
    )
    services.retention.run = MagicMock(return_value=report)
    handler = RetentionHandlers()
    await handler.run_retention(services, {})
    services.retention.run.assert_called_once_with(
        retention_days=90,
        prune=False,
        prune_age_days=365,
        dry_run=False,
    )


@pytest.mark.asyncio
async def test_run_retention_forwards_explicit_args() -> None:
    services = _stub_services()
    report = RetentionReport(
        archived_via_retired_reflection=0,
        archived_via_consumed_without_reflection=0,
        archived_via_no_outcome_episode=0,
        pruned=0,
    )
    services.retention.run = MagicMock(return_value=report)
    handler = RetentionHandlers()
    await handler.run_retention(
        services,
        {
            "retention_days": 30,
            "prune": True,
            "prune_age_days": 180,
            "dry_run": True,
        },
    )
    services.retention.run.assert_called_once_with(
        retention_days=30,
        prune=True,
        prune_age_days=180,
        dry_run=True,
    )


def test_handlers_registers_one_tool() -> None:
    handler = RetentionHandlers()
    assert [h.name for h in handler.handlers()] == ["memory.run_retention"]
