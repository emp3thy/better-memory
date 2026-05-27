"""Tests for `better-memory agentcore init`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from better_memory.cli.agentcore import _handle_init


def _make_args(
    home: Path, *, force: bool = False, region: str = "eu-west-2"
) -> argparse.Namespace:
    """Build an argparse.Namespace the handler accepts."""
    return argparse.Namespace(
        home=str(home),
        region=region,
        force=force,
        subcommand="init",
    )


def _active_memory_response(memory_id: str, strategy_id: str) -> dict:
    """Mimic GetMemory's ACTIVE response shape."""
    return {
        "memory": {
            "id": memory_id,
            "arn": f"arn:aws:bedrock-agentcore:eu-west-2:123:memory/{memory_id}",
            "name": memory_id.split("-")[0],
            "status": "ACTIVE",
            "strategies": [
                {"strategyId": strategy_id, "status": "ACTIVE", "name": "foo"}
            ],
            "eventExpiryDuration": 30,
        }
    }


def _create_memory_response(memory_id: str, strategy_id: str) -> dict:
    """Mimic CreateMemory's response shape (status: CREATING)."""
    return {
        "memory": {
            "id": memory_id,
            "arn": f"arn:aws:bedrock-agentcore:eu-west-2:123:memory/{memory_id}",
            "status": "CREATING",
            "strategies": [
                {"strategyId": strategy_id, "status": "CREATING", "name": "foo"}
            ],
        }
    }


def test_init_creates_both_memories_and_writes_config(
    tmp_path, monkeypatch, capsys
) -> None:
    """Happy path: both memories transition ACTIVE; agentcore.json written."""
    control = MagicMock(name="bedrock-agentcore-control")

    # list_memories paginator returns no existing memories (clean slate)
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator

    # CreateMemory called twice: once for episodic, once for semantic
    control.create_memory.side_effect = [
        _create_memory_response("epi-XYZ", "epi-strat-1"),
        _create_memory_response("sem-XYZ", "sem-strat-1"),
    ]

    # GetMemory polled: return ACTIVE immediately
    control.get_memory.side_effect = [
        _active_memory_response("epi-XYZ", "epi-strat-1"),
        _active_memory_response("sem-XYZ", "sem-strat-1"),
    ]

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr(
        "better_memory.cli.agentcore.time.sleep",
        lambda _s: None,
    )

    rc = _handle_init(_make_args(tmp_path))

    assert rc == 0
    config_path = tmp_path / "agentcore.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text())
    assert config["schema_version"] == 1
    assert config["region"] == "eu-west-2"
    assert config["episodic"]["memory_id"] == "epi-XYZ"
    assert config["semantic"]["memory_id"] == "sem-XYZ"

    out = capsys.readouterr().out
    assert "epi-XYZ" in out
    assert "sem-XYZ" in out


def test_init_refuses_when_config_exists_without_force(
    tmp_path, monkeypatch
) -> None:
    """If agentcore.json already exists, init refuses unless --force."""
    (tmp_path / "agentcore.json").write_text("{}")

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 1


def test_init_overwrites_when_force_set(tmp_path, monkeypatch) -> None:
    """With --force, init proceeds even if agentcore.json exists."""
    (tmp_path / "agentcore.json").write_text(json.dumps({"old": True}))

    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator
    control.create_memory.side_effect = [
        _create_memory_response("epi-NEW", "epi-strat"),
        _create_memory_response("sem-NEW", "sem-strat"),
    ]
    control.get_memory.side_effect = [
        _active_memory_response("epi-NEW", "epi-strat"),
        _active_memory_response("sem-NEW", "sem-strat"),
    ]

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_init(_make_args(tmp_path, force=True))
    assert rc == 0

    config = json.loads((tmp_path / "agentcore.json").read_text())
    assert config["episodic"]["memory_id"] == "epi-NEW"
    assert "old" not in config


def test_init_polls_until_active(tmp_path, monkeypatch) -> None:
    """If GetMemory returns CREATING, init polls until ACTIVE."""
    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator
    control.create_memory.side_effect = [
        _create_memory_response("epi-X", "epi-s"),
        _create_memory_response("sem-X", "sem-s"),
    ]

    creating_epi = {
        "memory": {
            **_active_memory_response("epi-X", "epi-s")["memory"],
            "status": "CREATING",
            "strategies": [
                {"strategyId": "epi-s", "status": "CREATING", "name": "foo"}
            ],
        }
    }
    creating_sem = {
        "memory": {
            **_active_memory_response("sem-X", "sem-s")["memory"],
            "status": "CREATING",
            "strategies": [
                {"strategyId": "sem-s", "status": "CREATING", "name": "foo"}
            ],
        }
    }

    # Episodic: 2 polls CREATING then ACTIVE; Semantic: 1 poll CREATING then ACTIVE
    control.get_memory.side_effect = [
        creating_epi, creating_epi,
        _active_memory_response("epi-X", "epi-s"),
        creating_sem,
        _active_memory_response("sem-X", "sem-s"),
    ]

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 0
    assert control.get_memory.call_count == 5


def test_init_deletes_orphan_when_second_create_fails(
    tmp_path, monkeypatch
) -> None:
    """Episodic create succeeds, semantic create raises -> init must delete
    the orphan episodic memory so a re-run of `init` starts clean."""
    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator

    # First create (episodic) succeeds; second (semantic) raises
    control.create_memory.side_effect = [
        _create_memory_response("epi-orphan", "epi-strat"),
        RuntimeError("simulated semantic create failure"),
    ]
    control.get_memory.side_effect = [
        _active_memory_response("epi-orphan", "epi-strat"),
    ]

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="semantic create"):
        _handle_init(_make_args(tmp_path))

    # Orphan delete fired exactly once against the episodic memory
    control.delete_memory.assert_called_once_with(memoryId="epi-orphan")

    # No agentcore.json was written (init aborted)
    assert not (tmp_path / "agentcore.json").exists()


def test_init_rejects_validation_error_with_friendly_message(
    tmp_path, monkeypatch, capsys
) -> None:
    """ValidationException on the name regex should map to a clean error,
    not a raw boto3 trace."""
    from botocore.exceptions import ClientError

    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator
    control.create_memory.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ValidationException",
                "Message": "Memory name does not match required pattern",
            }
        },
        operation_name="CreateMemory",
    )

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ValidationException" in err or "required pattern" in err
    assert "troubleshooting" in err.lower()


def test_init_preflight_checks_both_names(tmp_path, monkeypatch, capsys) -> None:
    """If EITHER default name already exists, init refuses before any
    CreateMemory runs (no orphan risk)."""
    control = MagicMock(name="bedrock-agentcore-control")

    # list_memories returns ONE existing memory matching the SEMANTIC name
    paginator = MagicMock()
    paginator.paginate.side_effect = lambda *a, **kw: iter([{
        "memories": [{"id": "existing-sem", "status": "ACTIVE"}]
    }])
    control.get_paginator.return_value = paginator
    control.get_memory.return_value = {
        "memory": {
            "id": "existing-sem",
            "name": "better_memory_semantic",
            "status": "ACTIVE",
            "strategies": [],
        }
    }

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 1
    # CreateMemory must not have been called
    control.create_memory.assert_not_called()
    err = capsys.readouterr().err
    assert "better_memory_semantic" in err
