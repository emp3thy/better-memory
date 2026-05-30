"""Tests for `better-memory agentcore migrate-from-sqlite` (stubbed)."""

from __future__ import annotations

import argparse

import pytest

from better_memory.cli.agentcore import _handle_migrate


def test_migrate_raises_not_implemented_with_pointer() -> None:
    args = argparse.Namespace(subcommand="migrate-from-sqlite")
    with pytest.raises(NotImplementedError) as excinfo:
        _handle_migrate(args)
    msg = str(excinfo.value)
    # Pointer text must mention the deferred spec / future work
    assert "future" in msg.lower() or "deferred" in msg.lower()
