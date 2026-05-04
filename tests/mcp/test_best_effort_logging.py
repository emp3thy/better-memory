"""Unit tests for the best-effort wrapper used by ``memory.retrieve``.

Both the spool drain and the retention-scheduler call inside
``memory.retrieve`` are run through ``_run_best_effort`` so an exception
in either path is swallowed (the call must not fail) but is logged
through the module logger so failures don't vanish silently.

Regression for issue #29.
"""

from __future__ import annotations

import logging

import pytest

from better_memory.mcp.server import _run_best_effort


def test_run_best_effort_swallows_exception(caplog: pytest.LogCaptureFixture) -> None:
    """An ``Exception`` raised by the callable must not propagate."""

    def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="better_memory.mcp.server"):
        _run_best_effort("spool.drain", boom)  # must not raise


def test_run_best_effort_logs_exception_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure is logged with the operation name and a traceback."""

    def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="better_memory.mcp.server"):
        _run_best_effort("spool.drain", boom)

    matching = [
        r for r in caplog.records
        if r.name == "better_memory.mcp.server"
        and r.levelno == logging.ERROR
        and "spool.drain" in r.getMessage()
    ]
    assert matching, (
        f"expected an ERROR record naming the operation, "
        f"got {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # logger.exception attaches exc_info; that's what makes the record
    # discoverable as a real failure rather than a generic message.
    assert matching[0].exc_info is not None


def test_run_best_effort_returns_none_on_success() -> None:
    """A successful callable's result is discarded; no exception either way."""
    calls: list[int] = []

    def ok() -> None:
        calls.append(1)

    assert _run_best_effort("noop", ok) is None
    assert calls == [1]
