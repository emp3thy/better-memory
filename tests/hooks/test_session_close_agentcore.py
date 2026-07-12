"""Tests for Stop hook's agentcore-mode closure event.

Backend-gate contract (defect 4 fix): the closure fires when EITHER the
BETTER_MEMORY_STORAGE_BACKEND env var says agentcore (env always wins,
zero file I/O) OR — env unset — ``$BETTER_MEMORY_HOME/settings.json``
resolves to agentcore via the shared ``resolve_storage_backend`` helper.
Env unset + no settings.json stays sqlite even when agentcore.json exists
(existence is not consent). Resolver errors are recorded to hook_errors
and never block the spool marker.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


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


def _migrate_home_db(home: Path) -> None:
    """Apply migrations so record_hook_error's INSERT actually lands."""
    home.mkdir(parents=True, exist_ok=True)
    conn = connect(home / "memory.db")
    try:
        apply_migrations(conn)
    finally:
        conn.close()


def _hook_error_rows(home: Path) -> list[dict]:
    conn = connect(home / "memory.db")
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT hook_name, exception_type, exception_message "
                "FROM hook_errors"
            ).fetchall()
        ]
    finally:
        conn.close()


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
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    from better_memory.hooks.session_close import main

    # main() exits 0 always — capture the SystemExit
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0

    # Spool marker FILE was written despite the closure event raising
    spool_dir = Path(agentcore_config_present) / "spool"
    markers = list(spool_dir.glob("*_session_end_*.json"))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"


# ---------------------------------------------------------------------------
# settings.json-aware backend resolution (defect 4)
# ---------------------------------------------------------------------------


def test_settings_json_resolves_agentcore_and_fires_closure(
    agentcore_home_no_env, monkeypatch
):
    """Env unset + settings.json {"storage_backend": "agentcore"} → the Stop
    hook fires the closure event. This is the defect-4 fix: installed hooks
    receive no env from Claude Code, so the settings file written by
    `agentcore init` must be what activates the closure."""
    (agentcore_home_no_env / "settings.json").write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    fake_client = MagicMock(name="bedrock-agentcore-data")
    fake_client.create_event.return_value = {"event": {"eventId": "evt-close"}}
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is True
    assert fake_client.create_event.call_count == 1
    call = fake_client.create_event.call_args.kwargs
    assert call["memoryId"] == "epi-test"
    assert call["sessionId"] == "test-sess-abc"


def test_env_unset_no_settings_json_stays_sqlite_even_with_agentcore_json(
    agentcore_home_no_env, monkeypatch
):
    """Sqlite-safety oracle: env absent AND no settings.json → no closure,
    even though agentcore.json exists. A sqlite user who once provisioned
    (agentcore.json persists) must not be silently switched — existence is
    not consent. Silent skip: no hook_errors row, no memory.db at all."""
    fake_client = MagicMock(name="bedrock-agentcore-data")
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is False
    assert fake_client.create_event.call_count == 0
    # Silent skip, not an error path: nothing wrote memory.db.
    assert not (agentcore_home_no_env / "memory.db").exists()


def test_explicit_env_sqlite_wins_over_settings_json(
    agentcore_home_no_env, monkeypatch
):
    """Env always wins: BETTER_MEMORY_STORAGE_BACKEND=sqlite beats a
    settings.json that says agentcore, and the fast path performs zero file
    I/O — the shared resolver is never even called."""
    (agentcore_home_no_env / "settings.json").write_text(
        json.dumps({"storage_backend": "agentcore"}), encoding="utf-8"
    )
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")

    import better_memory.config as config_mod

    def _resolver_forbidden():
        raise AssertionError(
            "resolve_storage_backend called on the explicit-env fast path"
        )

    monkeypatch.setattr(
        config_mod, "resolve_storage_backend", _resolver_forbidden
    )
    fake_client = MagicMock(name="bedrock-agentcore-data")
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is False
    assert fake_client.create_event.call_count == 0
    # Fast path is silent — no error row, no memory.db.
    assert not (agentcore_home_no_env / "memory.db").exists()


def test_explicit_env_agentcore_wins_over_settings_json_sqlite(
    agentcore_home_no_env, monkeypatch
):
    """Env precedence, other direction: env=agentcore fires the closure even
    when settings.json pins sqlite."""
    (agentcore_home_no_env / "settings.json").write_text(
        json.dumps({"storage_backend": "sqlite"}), encoding="utf-8"
    )
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    fake_client = MagicMock(name="bedrock-agentcore-data")
    fake_client.create_event.return_value = {"event": {"eventId": "evt-close"}}
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is True
    assert fake_client.create_event.call_count == 1


def test_corrupt_settings_json_records_hook_error_and_skips_closure(
    agentcore_home_no_env, monkeypatch
):
    """Env unset + malformed settings.json → resolver ValueError is caught,
    recorded to hook_errors (hook_name=session_close_agentcore, message
    naming the file), and the closure is skipped. Hooks never fail."""
    _migrate_home_db(agentcore_home_no_env)
    (agentcore_home_no_env / "settings.json").write_text(
        "{not json", encoding="utf-8"
    )
    fake_client = MagicMock(name="bedrock-agentcore-data")
    monkeypatch.setattr(
        "better_memory.hooks.session_close._build_agentcore_data_client",
        lambda region: fake_client,
    )

    from better_memory.hooks.session_close import _fire_agentcore_closure
    rc = _fire_agentcore_closure(session_id="test-sess-abc", project="testproj")
    assert rc is False
    assert fake_client.create_event.call_count == 0
    rows = _hook_error_rows(agentcore_home_no_env)
    closure_rows = [
        r for r in rows if r["hook_name"] == "session_close_agentcore"
    ]
    assert len(closure_rows) == 1
    assert closure_rows[0]["exception_type"] == "ValueError"
    assert "settings.json" in closure_rows[0]["exception_message"]


def test_corrupt_settings_json_marker_still_written(
    agentcore_home_no_env, monkeypatch
):
    """main()-level never-fail contract: a corrupt settings.json must not
    block the spool marker — exit 0, marker written, error row recorded."""
    _migrate_home_db(agentcore_home_no_env)
    (agentcore_home_no_env / "settings.json").write_text(
        "{not json", encoding="utf-8"
    )
    monkeypatch.setattr(
        "better_memory.hooks.session_close.default_spool_dir",
        lambda: Path(agentcore_home_no_env) / "spool",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    from better_memory.hooks.session_close import main

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0

    markers = list(
        (Path(agentcore_home_no_env) / "spool").glob("*_session_end_*.json")
    )
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    rows = _hook_error_rows(agentcore_home_no_env)
    assert any(r["hook_name"] == "session_close_agentcore" for r in rows)


# ---------------------------------------------------------------------------
# Fast-path import hygiene + boto3 hint
# ---------------------------------------------------------------------------


def test_env_sqlite_fast_exit_skips_boto3_and_agentcore_imports(
    monkeypatch, tmp_path
):
    """Explicit env=sqlite returns False before any lazy import: no boto3,
    no botocore, no agentcore_persistence import is attempted."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "sqlite")

    from better_memory.hooks.session_close import _fire_agentcore_closure

    hits = _forbid_imports(
        monkeypatch, "boto3", "botocore", "agentcore_persistence"
    )
    rc = _fire_agentcore_closure(session_id="x", project="p")
    assert rc is False
    assert hits == []


def test_env_unset_no_settings_resolves_sqlite_without_boto3(
    monkeypatch, tmp_path
):
    """Env unset + no settings.json resolves sqlite via the shared resolver
    and still never attempts a boto3/agentcore import."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    from better_memory.hooks.session_close import _fire_agentcore_closure

    hits = _forbid_imports(
        monkeypatch, "boto3", "botocore", "agentcore_persistence"
    )
    rc = _fire_agentcore_closure(session_id="x", project="p")
    assert rc is False
    assert hits == []


def test_build_client_missing_boto3_raises_install_hint(monkeypatch):
    """The lazy boto3 import carries the same `better-memory[agentcore]`
    install hint as the storage factory: ModuleNotFoundError, chained from
    the original ImportError."""
    from better_memory.hooks.session_close import _build_agentcore_data_client

    real_import = builtins.__import__

    def _no_boto3(name, *args, **kwargs):
        if name == "boto3" or name.startswith("botocore"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        _build_agentcore_data_client("eu-west-2")
    assert "better-memory[agentcore]" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ImportError)
