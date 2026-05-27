"""Tests for `better-memory ...` CLI dispatcher."""

from __future__ import annotations

import subprocess
import sys

import pytest

from better_memory.cli.main import main


def test_main_with_no_args_prints_help_and_exits_nonzero(capsys) -> None:
    """`better-memory` with no subcommand should print help and exit 2 (argparse default)."""
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_main_help_lists_agentcore_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "agentcore" in captured.out


def test_main_dispatches_to_agentcore_handler(monkeypatch) -> None:
    """`better-memory agentcore status` should call the agentcore subcommand."""
    called = {}

    def fake_handle(args: object) -> int:
        called["yes"] = True
        return 0

    monkeypatch.setattr(
        "better_memory.cli.agentcore.handle",
        fake_handle,
    )
    rc = main(["agentcore", "status"])
    assert rc == 0
    assert called == {"yes": True}


def test_main_unknown_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code == 2
