"""Tests for the contextual_inject hook."""
from __future__ import annotations

import io
import json
import sys

import pytest

from better_memory.hooks import contextual_inject as hook


def _run(payload: dict, monkeypatch, capsys, mode="both"):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", mode)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


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
