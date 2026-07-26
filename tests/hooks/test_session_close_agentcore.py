"""Tests for the Stop hook's agentcore-mode no-op invariant.

Decision (user directive, 2026-07): agentcore-mode session-lifecycle
emissions are NO-OPS. The Stop hook used to fire one closure
``CreateEvent(role=OTHER)`` per session end to nudge AWS's episodic
extraction — but that marker was often the ONLY event in a thin/empty/
system-only session, so AWS extracted a low-value "no actionable
content" reflection from it. The hook must now touch AWS for NOTHING:
it only ever writes the local ``session_end`` spool marker (which drives
sqlite-mode synthesis and the idle-timer path for agentcore). Real
sessions still get extracted on AWS's own idle timer.

These tests prove the invariant via an import guard: if ``main()`` never
imports boto3/botocore/the agentcore persistence loader/the closure-event
helpers, it is structurally impossible for it to have called AWS's
CreateEvent — a stronger oracle than mocking a client that no longer
exists in the hook module.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
from pathlib import Path

import pytest


def _write_agentcore_json(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "agentcore.json").write_text(json.dumps({
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
    }), encoding="utf-8")


def _forbid_imports(monkeypatch, *fragments: str) -> list[str]:
    """Install a builtins.__import__ guard that records (and rejects) any
    import whose module name contains one of ``fragments``. Returns the
    hit list — assert it stays empty."""
    real_import = builtins.__import__
    hits: list[str] = []

    def _guard(name, *args, **kwargs):
        if any(fragment in name for fragment in fragments):
            hits.append(name)
            raise AssertionError(f"forbidden import on this path: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    return hits


def _session_end_markers(spool_dir: Path) -> list[Path]:
    if not spool_dir.is_dir():
        return []
    return sorted(spool_dir.glob("*_session_end_*.json"))


@pytest.fixture
def agentcore_config_present(tmp_path, monkeypatch):
    """Set BETTER_MEMORY_HOME with a populated agentcore.json + env mode."""
    _write_agentcore_json(tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-sess-abc")
    return tmp_path


@pytest.fixture
def agentcore_home_no_env(tmp_path, monkeypatch):
    """agentcore.json present but BETTER_MEMORY_STORAGE_BACKEND unset —
    the installed-hook reality: Claude Code passes hooks no env."""
    _write_agentcore_json(tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-sess-abc")
    return tmp_path


def _run_main_with_empty_stdin(monkeypatch, home: Path) -> Path:
    """Run session_close.main() against an empty stdin (synthesised
    marker) with the spool dir redirected under ``home``. Returns the
    spool dir. main() always exits 0 — capture the SystemExit."""
    spool_dir = home / "spool"
    monkeypatch.setattr(
        "better_memory.hooks.session_close.default_spool_dir",
        lambda: spool_dir,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    from better_memory.hooks.session_close import main

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0
    return spool_dir


def test_agentcore_env_mode_writes_marker_and_touches_no_aws(
    agentcore_config_present, monkeypatch
):
    """Invariant: with BETTER_MEMORY_STORAGE_BACKEND=agentcore and a valid
    agentcore.json, running the Stop hook writes ONLY the spool marker.
    No boto3/botocore import, no agentcore-persistence import, no
    storage.session closure-payload import — none of the machinery a
    CreateEvent call would require is ever touched."""
    hits = _forbid_imports(
        monkeypatch,
        "boto3",
        "botocore",
        "agentcore_persistence",
        "storage.session",
    )

    spool_dir = _run_main_with_empty_stdin(monkeypatch, agentcore_config_present)

    assert hits == []
    markers = _session_end_markers(spool_dir)
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    body = json.loads(markers[0].read_text(encoding="utf-8"))
    assert body["event_type"] == "session_end"


def test_agentcore_settings_json_mode_writes_marker_and_touches_no_aws(
    agentcore_home_no_env, monkeypatch
):
    """Same invariant when the backend resolves to agentcore via
    settings.json (env absent) — the installed-hook reality."""
    (agentcore_home_no_env / "settings.json").write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    hits = _forbid_imports(
        monkeypatch,
        "boto3",
        "botocore",
        "agentcore_persistence",
        "storage.session",
    )

    spool_dir = _run_main_with_empty_stdin(monkeypatch, agentcore_home_no_env)

    assert hits == []
    markers = _session_end_markers(spool_dir)
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"


def test_sqlite_mode_writes_marker_and_touches_no_aws(monkeypatch, tmp_path):
    """Sqlite-mode regression guard: never touched AWS before, must not
    start now."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-sess-sqlite")
    hits = _forbid_imports(
        monkeypatch,
        "boto3",
        "botocore",
        "agentcore_persistence",
        "storage.session",
    )

    spool_dir = _run_main_with_empty_stdin(monkeypatch, tmp_path)

    assert hits == []
    markers = _session_end_markers(spool_dir)
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"


def test_agentcore_functions_removed_from_module():
    """Documents the removal: the closure-firing function and its boto3
    client builder no longer exist on the module. If this starts failing
    because someone re-added them, the invariant tests above must be
    revisited alongside whatever reintroduced them."""
    import better_memory.hooks.session_close as session_close_mod

    assert not hasattr(session_close_mod, "_fire_agentcore_closure")
    assert not hasattr(session_close_mod, "_build_agentcore_data_client")
