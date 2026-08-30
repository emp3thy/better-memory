import json


def _run(monkeypatch, capsys, payload: str) -> str:
    import io
    import sys as _sys

    from better_memory.hooks import commit_checkpoint

    monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
    commit_checkpoint.main()
    return capsys.readouterr().out


def test_git_commit_gets_checkpoint(monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "git commit -m x"}})
    out = json.loads(_run(monkeypatch, capsys, payload))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "MEMORY CHECKPOINT before commit" in ctx


def test_non_commit_command_prints_nothing(monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert _run(monkeypatch, capsys, payload) == ""
