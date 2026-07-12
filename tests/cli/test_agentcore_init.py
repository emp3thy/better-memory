"""Tests for `better-memory agentcore init`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from better_memory.cli.agentcore import _handle_init, add_subparsers


def _make_args(
    home: Path,
    *,
    force: bool = False,
    region: str = "eu-west-2",
    no_activate: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace the handler accepts."""
    return argparse.Namespace(
        home=str(home),
        region=region,
        force=force,
        no_activate=no_activate,
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


def _happy_control(epi_id: str = "epi-XYZ", sem_id: str = "sem-XYZ") -> MagicMock:
    """Build a control-plane mock where both memories go ACTIVE immediately."""
    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator
    control.create_memory.side_effect = [
        _create_memory_response(epi_id, "epi-strat-1"),
        _create_memory_response(sem_id, "sem-strat-1"),
    ]
    control.get_memory.side_effect = [
        _active_memory_response(epi_id, "epi-strat-1"),
        _active_memory_response(sem_id, "sem-strat-1"),
    ]
    return control


def _patch_control(monkeypatch, control: MagicMock) -> None:
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)


def test_init_refuses_when_config_exists_without_force(
    tmp_path, monkeypatch
) -> None:
    """If agentcore.json already exists, init refuses unless --force."""
    (tmp_path / "agentcore.json").write_text("{}")

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 1
    # Refusal must not activate anything either.
    assert not (tmp_path / "settings.json").exists()


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


def test_init_deletes_orphan_when_poll_raises_after_create(
    tmp_path, monkeypatch
) -> None:
    """CreateMemory succeeds, _poll_until_active hits FAILED state ->
    init must still delete the AWS resource even though no MemoryRecord
    was ever returned. Without the created_ids list this leak would slip
    past the orphan cleanup (which previously only ran when episodic was
    not None)."""
    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator

    control.create_memory.side_effect = [
        _create_memory_response("epi-poll-fail", "epi-strat"),
    ]
    # First poll returns FAILED -> _poll_until_active raises RuntimeError
    control.get_memory.side_effect = [{
        "memory": {
            "id": "epi-poll-fail",
            "arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/epi-poll-fail",
            "name": "better_memory_episodic",
            "status": "FAILED",
            "strategies": [
                {"strategyId": "epi-strat", "status": "FAILED", "name": "x"}
            ],
        }
    }]

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="FAILED state"):
        _handle_init(_make_args(tmp_path))

    # Even though episodic MemoryRecord was never returned, the raw id
    # tracked via created_ids must be deleted.
    control.delete_memory.assert_called_once_with(memoryId="epi-poll-fail")


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


# ---------------------------------------------------------------------------
# settings.json activation (UD-3) + next-steps stdout contract
# ---------------------------------------------------------------------------


def test_init_argparse_accepts_no_activate() -> None:
    """`--no-activate` is a registered init flag; defaults to False."""
    parser = argparse.ArgumentParser()
    add_subparsers(parser)
    args = parser.parse_args(
        ["init", "--no-activate", "--home", "x", "--region", "us-east-1"]
    )
    assert args.no_activate is True
    assert parser.parse_args(["init"]).no_activate is False


def test_init_writes_settings_activation_by_default(
    tmp_path, monkeypatch
) -> None:
    """Successful init persists {"storage_backend": "agentcore"} into
    <home>/settings.json (atomically — no .tmp residue)."""
    _patch_control(monkeypatch, _happy_control())

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 0

    settings_path = tmp_path / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["storage_backend"] == "agentcore"
    assert not (tmp_path / "settings.json.tmp").exists()


def test_init_no_activate_skips_settings_write(tmp_path, monkeypatch, capsys) -> None:
    """`--no-activate` provisions AWS + agentcore.json but leaves the
    backend selection untouched; stdout explains how to activate later."""
    _patch_control(monkeypatch, _happy_control())

    rc = _handle_init(_make_args(tmp_path, no_activate=True))
    assert rc == 0
    assert (tmp_path / "agentcore.json").exists()
    assert not (tmp_path / "settings.json").exists()

    out = capsys.readouterr().out
    assert "--no-activate" in out
    assert "settings.json" in out
    assert "Export BETTER_MEMORY_STORAGE_BACKEND" not in out
    # BugBot PR#79: next-steps must not contradict the skip message — no
    # "picks up the new backend" restart instruction when nothing changed.
    assert "picks up the new backend" not in out
    assert "no backend change was made yet" in out


def test_init_failure_path_does_not_write_settings(tmp_path, monkeypatch) -> None:
    """When memory creation fails, init must not activate the backend
    (no settings.json), just as it writes no agentcore.json."""
    control = MagicMock(name="bedrock-agentcore-control")
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"memories": []}])
    control.get_paginator.return_value = paginator
    control.create_memory.side_effect = [
        _create_memory_response("epi-orphan", "epi-strat"),
        RuntimeError("simulated semantic create failure"),
    ]
    control.get_memory.side_effect = [
        _active_memory_response("epi-orphan", "epi-strat"),
    ]
    _patch_control(monkeypatch, control)

    with pytest.raises(RuntimeError, match="semantic create"):
        _handle_init(_make_args(tmp_path))

    assert not (tmp_path / "agentcore.json").exists()
    assert not (tmp_path / "settings.json").exists()


def test_init_activation_preserves_existing_settings_keys(
    tmp_path, monkeypatch
) -> None:
    """An existing settings.json with unrelated keys is merged into, not
    clobbered."""
    (tmp_path / "settings.json").write_text(
        json.dumps({"other_key": 42}), encoding="utf-8"
    )
    _patch_control(monkeypatch, _happy_control())

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 0

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["storage_backend"] == "agentcore"
    assert settings["other_key"] == 42


def test_init_activation_replaces_corrupt_settings(tmp_path, monkeypatch) -> None:
    """A corrupt settings.json is replaced with a valid activation object
    (init is the remediation path, it must not crash on the broken file)."""
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    _patch_control(monkeypatch, _happy_control())

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 0

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"storage_backend": "agentcore"}


def test_init_next_steps_stdout_contract(tmp_path, monkeypatch, capsys) -> None:
    """Next-steps text names the settings.json mechanism and env precedence,
    and no longer tells the user to export an env var (the onboarding trap:
    an exported var in one shell never reaches Claude Code's hooks)."""
    _patch_control(monkeypatch, _happy_control())

    rc = _handle_init(_make_args(tmp_path))
    assert rc == 0

    out = capsys.readouterr().out
    # The onboarding trap must be gone.
    assert "Export BETTER_MEMORY_STORAGE_BACKEND" not in out
    # Activation mechanism + env precedence are named.
    assert "settings.json" in out
    assert "BETTER_MEMORY_STORAGE_BACKEND" in out
    assert "overrides" in out
    # Revert path.
    assert "revert" in out.lower()
    assert "sqlite" in out
    # Follow-ups: restart, status, then smoke — with the smoke caveat.
    assert "Restart" in out
    assert "agentcore status" in out
    assert "agentcore smoke" in out
    assert "not MCP registration" in out
