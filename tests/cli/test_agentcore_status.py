"""Tests for `better-memory agentcore status`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

from better_memory.cli.agentcore import _handle_status


def _make_args(home: Path, region: str | None = None) -> argparse.Namespace:
    """Build an argparse.Namespace the handler accepts."""
    return argparse.Namespace(
        home=str(home),
        region=region,
        subcommand="status",
    )


def _write_config(home: Path) -> None:
    cfg = {
        "schema_version": 1,
        "region": "eu-west-2",
        "episodic": {
            "memory_id": "epi-X",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/epi-X",
            "memory_name": "better_memory_episodic",
            "strategy_id": "epi-strat",
            "strategy_name": "episodicReflections",
            "event_expiry_duration_days": 90,
        },
        "semantic": {
            "memory_id": "sem-X",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/sem-X",
            "memory_name": "better_memory_semantic",
            "strategy_id": "sem-strat",
            "strategy_name": "userPreference",
            "event_expiry_duration_days": 365,
        },
    }
    (home / "agentcore.json").write_text(json.dumps(cfg))


def test_status_exits_1_when_config_missing(tmp_path, capsys) -> None:
    rc = _handle_status(_make_args(tmp_path))
    assert rc == 1
    err = capsys.readouterr().err
    assert "agentcore.json" in err


def test_status_prints_both_memories_and_exits_0_when_active(
    tmp_path, monkeypatch, capsys
) -> None:
    _write_config(tmp_path)
    control = MagicMock(name="bedrock-agentcore-control")
    control.get_memory.side_effect = [
        {"memory": {
            "id": "epi-X", "name": "better_memory_episodic", "status": "ACTIVE",
            "strategies": [
                {
                    "strategyId": "epi-strat",
                    "status": "ACTIVE",
                    "name": "episodicReflections",
                }
            ],
            "eventExpiryDuration": 90,
        }},
        {"memory": {
            "id": "sem-X", "name": "better_memory_semantic", "status": "ACTIVE",
            "strategies": [
                {
                    "strategyId": "sem-strat",
                    "status": "ACTIVE",
                    "name": "userPreference",
                }
            ],
            "eventExpiryDuration": 365,
        }},
    ]
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )

    rc = _handle_status(_make_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "epi-X" in out and "ACTIVE" in out
    assert "sem-X" in out


def _active_control() -> MagicMock:
    """Control-plane mock where both memories report ACTIVE."""
    control = MagicMock(name="bedrock-agentcore-control")
    control.get_memory.side_effect = [
        {"memory": {
            "id": "epi-X", "name": "better_memory_episodic", "status": "ACTIVE",
            "strategies": [
                {
                    "strategyId": "epi-strat",
                    "status": "ACTIVE",
                    "name": "episodicReflections",
                }
            ],
            "eventExpiryDuration": 90,
        }},
        {"memory": {
            "id": "sem-X", "name": "better_memory_semantic", "status": "ACTIVE",
            "strategies": [
                {
                    "strategyId": "sem-strat",
                    "status": "ACTIVE",
                    "name": "userPreference",
                }
            ],
            "eventExpiryDuration": 365,
        }},
    ]
    return control


def test_status_reports_effective_backend_from_settings(
    tmp_path, monkeypatch, capsys
) -> None:
    """settings.json activation (no env var) -> agentcore, source=settings."""
    _write_config(tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: _active_control(),
    )

    rc = _handle_status(_make_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "effective backend: agentcore (source: settings)" in out


def test_status_reports_env_var_overriding_settings(
    tmp_path, monkeypatch, capsys
) -> None:
    """Env var wins over settings.json — status makes the precedence
    surprise diagnosable by naming the source."""
    _write_config(tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: _active_control(),
    )

    rc = _handle_status(_make_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "effective backend: sqlite (source: env)" in out


def test_status_reports_default_backend_without_env_or_settings(
    tmp_path, monkeypatch, capsys
) -> None:
    """No env var, no settings.json -> sqlite, source=default."""
    _write_config(tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: _active_control(),
    )

    rc = _handle_status(_make_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "effective backend: sqlite (source: default)" in out


def test_status_corrupt_settings_warns_but_still_reports_memories(
    tmp_path, monkeypatch, capsys
) -> None:
    """A corrupt settings.json must not crash status — it warns (naming the
    file) and still prints the per-memory report."""
    _write_config(tmp_path)
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: _active_control(),
    )

    rc = _handle_status(_make_args(tmp_path))
    assert rc == 0
    captured = capsys.readouterr()
    assert "settings.json" in captured.err
    assert "epi-X" in captured.out and "sem-X" in captured.out


def test_status_exits_1_when_any_memory_not_active(
    tmp_path, monkeypatch
) -> None:
    _write_config(tmp_path)
    control = MagicMock()
    control.get_memory.side_effect = [
        {"memory": {
            "id": "epi-X", "name": "better_memory_episodic", "status": "CREATING",
            "strategies": [{"strategyId": "epi-strat", "status": "CREATING", "name": "x"}],
            "eventExpiryDuration": 90,
        }},
        {"memory": {
            "id": "sem-X", "name": "better_memory_semantic", "status": "ACTIVE",
            "strategies": [{"strategyId": "sem-strat", "status": "ACTIVE", "name": "y"}],
            "eventExpiryDuration": 365,
        }},
    ]
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_control_client",
        lambda region: control,
    )
    rc = _handle_status(_make_args(tmp_path))
    assert rc == 1
