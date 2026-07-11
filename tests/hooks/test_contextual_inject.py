"""Tests for the contextual_inject hook."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.hooks import contextual_inject as hook

_PROJECT = "ctx-inject-proj"


def _run(payload: dict, monkeypatch, capsys, mode="both"):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", mode)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


@pytest.fixture
def bm_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolated BETTER_MEMORY_HOME with migrations applied and a fixed project."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", _PROJECT)
    conn = connect(tmp_path / "memory.db")
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return tmp_path


def _seed_reflection(
    home: Path, rid: str, *, title: str, use_cases: str = "context",
    hints: list[str] | None = None, useful_count: int = 0,
    confidence: float = 0.8, polarity: str = "do",
) -> None:
    conn = connect(home / "memory.db")
    try:
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, useful_count)
               VALUES (?, ?, ?, 'general', ?, ?, ?, ?, '2026-01-01',
                       '2026-01-01', ?)""",
            (rid, title, _PROJECT, polarity, use_cases,
             json.dumps(hints or []), confidence, useful_count),
        )
        conn.commit()
    finally:
        conn.close()


def _diag_value(home: Path, metric: str) -> int | None:
    conn = connect(home / "memory.db")
    try:
        row = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric = ?", (metric,)
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def test_userprompt_emits_envelope(monkeypatch, capsys):
    res = _run({"hook_event_name": "UserPromptSubmit", "prompt": "write the plan",
                "cwd": "."}, monkeypatch, capsys)
    assert res["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in res["hookSpecificOutput"]


def test_mode_off_is_noop(monkeypatch, capsys):
    res = _run({"hook_event_name": "UserPromptSubmit", "prompt": "write the plan",
                "cwd": "."}, monkeypatch, capsys, mode="off")
    assert res["hookSpecificOutput"]["additionalContext"] == ""


def test_pretool_disabled_when_mode_userprompt(monkeypatch, capsys):
    res = _run({"hook_event_name": "PreToolUse", "tool_name": "Skill",
                "tool_input": {"skill": "writing-plans"}, "cwd": "."},
               monkeypatch, capsys, mode="userprompt")
    assert res["hookSpecificOutput"]["additionalContext"] == ""


def test_pretool_event_echoed(monkeypatch, capsys):
    res = _run({"hook_event_name": "PreToolUse", "tool_name": "Skill",
                "tool_input": {"skill": "writing-plans"}, "cwd": "."},
               monkeypatch, capsys, mode="both")
    assert res["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_never_throws_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "both")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0


def test_injection_renders_project_memory_block(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-1", title="widget deploy playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-1"},
        monkeypatch, capsys,
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("<project-memory")
    assert "refl-widget-deploy-1" in ctx


def test_exposure_row_written_with_contextual_source(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-2", title="widget deploy playbook")
    _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-2"},
        monkeypatch, capsys,
    )
    conn = connect(bm_home / "memory.db")
    try:
        row = conn.execute(
            "SELECT source FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_id = ?",
            ("sess-2", "refl-widget-deploy-2"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["source"] == "contextual"


def test_second_run_suppressed_by_seen_store(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-deploy-3", title="widget deploy playbook")
    payload = {
        "hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
        "cwd": ".", "session_id": "sess-3",
    }
    first = _run(payload, monkeypatch, capsys)
    assert first["hookSpecificOutput"]["additionalContext"] != ""

    second = _run(payload, monkeypatch, capsys)
    assert second["hookSpecificOutput"]["additionalContext"] == ""
    assert _diag_value(bm_home, "contextual_suppressed_dedup") == 1


def test_below_floor_injects_nothing(bm_home, monkeypatch, capsys):
    _seed_reflection(bm_home, "refl-widget-only-4", title="widget playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-4"},
        monkeypatch, capsys,
    )
    assert res["hookSpecificOutput"]["additionalContext"] == ""
    assert _diag_value(bm_home, "contextual_suppressed_floor") == 1


def test_fired_counters(bm_home, monkeypatch, capsys):
    _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "hello world",
         "cwd": ".", "session_id": "sess-5a"},
        monkeypatch, capsys,
    )
    assert _diag_value(bm_home, "contextual_fired_userprompt") == 1

    _run(
        {"hook_event_name": "PreToolUse", "tool_name": "Skill",
         "tool_input": {"skill": "writing-plans"}, "cwd": ".", "session_id": "sess-5b"},
        monkeypatch, capsys,
    )
    assert _diag_value(bm_home, "contextual_fired_pretool") == 1


def test_exposure_write_failure_does_not_block_injection(bm_home, monkeypatch, capsys):
    from better_memory.storage.sqlite import SqliteBackend

    def _raise(*args, **kwargs):
        raise RuntimeError("exposure write boom")

    monkeypatch.setattr(SqliteBackend, "record_exposures", _raise)

    _seed_reflection(bm_home, "refl-widget-deploy-6", title="widget deploy playbook")
    res = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": "deploy the widget service now",
         "cwd": ".", "session_id": "sess-6"},
        monkeypatch, capsys,
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "refl-widget-deploy-6" in ctx
