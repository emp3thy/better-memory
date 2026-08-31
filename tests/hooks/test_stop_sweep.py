import json


def test_emits_sweep_system_message(monkeypatch, capsys):
    import io
    import sys as _sys

    from better_memory.hooks import stop_sweep

    monkeypatch.setattr(_sys, "stdin", io.StringIO("{}"))
    stop_sweep.main()
    out = json.loads(capsys.readouterr().out)
    assert "MEMORY SWEEP" in out["systemMessage"]
    assert "decision" not in out  # must never block the Stop
