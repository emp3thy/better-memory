import json


def _run(monkeypatch, capsys, payload: str) -> dict:
    import io
    import sys as _sys

    from better_memory.hooks import pre_compact

    monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
    pre_compact.main()
    return json.loads(capsys.readouterr().out)


def test_emits_precompact_additional_context(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, json.dumps({"session_id": "abc123"}))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreCompact"
    assert "abc123" in hso["additionalContext"]
    assert "memory_observe" in hso["additionalContext"]


def test_survives_empty_stdin(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, "")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"


def test_survives_null_json(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, "null")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
    assert "unknown" in out["hookSpecificOutput"]["additionalContext"]


def test_survives_array_json(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, "[]")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
    assert "unknown" in out["hookSpecificOutput"]["additionalContext"]
