"""Tests for Stop hook's agentcore-mode closure event."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def agentcore_config_present(tmp_path, monkeypatch):
    """Set BETTER_MEMORY_HOME with a populated agentcore.json + env mode."""
    import json
    (tmp_path / "agentcore.json").write_text(json.dumps({
        "schema_version": 1,
        "region": "eu-west-2",
        "episodic": {
            "memory_id": "epi-test",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/epi-test",
            "memory_name": "better_memory_episodic",
            "strategy_id": "epi-strat",
            "strategy_name": "episodicReflections",
            "event_expiry_duration_days": 90,
        },
        "semantic": {
            "memory_id": "sem-test",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/sem-test",
            "memory_name": "better_memory_semantic",
            "strategy_id": "sem-strat",
            "strategy_name": "userPreference",
            "event_expiry_duration_days": 365,
        },
    }))
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-sess-abc")
    return tmp_path


def test_agentcore_mode_fires_closure_event(agentcore_config_present, monkeypatch):
    """In agentcore mode, the Stop hook fires one CreateEvent with role=OTHER."""
    fake_data_client = MagicMock(name="bedrock-agentcore-data")
    fake_data_client.create_event.return_value = {"event": {"eventId": "evt-close"}}

    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_data_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is True
    assert fake_data_client.create_event.call_count == 1

    call = fake_data_client.create_event.call_args.kwargs
    assert call["memoryId"] == "epi-test"
    assert call["sessionId"] == "test-sess-abc"
    payload = call["payload"][0]["conversational"]
    assert payload["role"] == "OTHER"


def test_sqlite_mode_does_not_fire_closure(monkeypatch, tmp_path):
    """In sqlite mode, _fire_agentcore_closure short-circuits to False."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "x")

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="x", project="testproj")
    assert rc is False


def test_agentcore_failure_is_non_fatal(agentcore_config_present, monkeypatch):
    """If the closure event raises, the hook must NOT propagate. Returns False."""
    fake_client = MagicMock()
    fake_client.create_event.side_effect = RuntimeError("simulated AWS failure")
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    # Must NOT raise
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is False


def test_spool_marker_written_even_when_closure_event_raises(
    agentcore_config_present, monkeypatch
):
    """Regression: closure-event failure MUST NOT block the spool marker.
    Branch-order bug protection — if someone refactors main() and puts the
    closure call after the spool write, this catches it; if someone moves
    the closure call into a try-block that early-exits on failure, this
    catches that too."""
    import sys
    from pathlib import Path

    fake_client = MagicMock()
    fake_client.create_event.side_effect = RuntimeError("AWS down")
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )
    # Force the hook to read from agentcore_config_present's tmp_path
    monkeypatch.setattr(
        "better_memory.hooks.session_close.default_spool_dir",
        lambda: Path(agentcore_config_present) / "spool",
    )
    # Feed an empty stdin so the hook synthesises the marker
    monkeypatch.setattr(sys, "stdin", type("StdIn", (), {"read": lambda _self, n: ""})())

    from better_memory.hooks.session_close import main

    # main() exits 0 always — capture the SystemExit
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0

    # Spool marker FILE was written despite the closure event raising
    spool_dir = Path(agentcore_config_present) / "spool"
    markers = list(spool_dir.glob("*_session_end_*.json"))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"


def test_env_guard_short_circuits_before_any_import(monkeypatch):
    """If BETTER_MEMORY_STORAGE_BACKEND != 'agentcore', the env guard must
    return False BEFORE any boto3-related import runs. Use a sentinel that
    raises on import to prove no agentcore_persistence import happens."""
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")

    # Patch agentcore_persistence so any import raises — proves we never
    # reach the lazy-import block
    sentinel_raised = []
    import importlib
    real_import = importlib.import_module

    def _raising_import(name, *a, **kw):
        if "agentcore_persistence" in name:
            sentinel_raised.append(name)
            raise AssertionError(
                "agentcore_persistence imported even though env=sqlite"
            )
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _raising_import)

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="x", project="p")
    assert rc is False
    assert sentinel_raised == []
