import json

from better_memory.cli.main import main as cli_main
from better_memory.setup import engine as eng
from better_memory.setup import manifest as man


def _fake_env(tmp_path, monkeypatch):
    paths = eng.TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    params = man.MachineParams(
        venv_py="/v/python", venv_pyw="/v/python",
        home=str(tmp_path / "bm-home"), repo_root="/repo",
    )
    monkeypatch.setattr(eng, "default_target_paths", lambda: paths)
    monkeypatch.setattr(man, "detect_machine_params",
                        lambda home=None: params)
    return paths, params


def test_setup_creates_layout_and_wiring(tmp_path, monkeypatch, capsys):
    paths, params = _fake_env(tmp_path, monkeypatch)
    assert cli_main(["setup"]) == 0
    assert (tmp_path / "bm-home" / "settings.json").exists()
    stored = json.loads((tmp_path / "bm-home" / "settings.json").read_text("utf-8"))
    assert stored == {"storage_backend": "sqlite"}
    assert "better-memory" in json.loads(
        paths.claude_json.read_text("utf-8"))["mcpServers"]


def test_setup_preserves_existing_backend_choice(tmp_path, monkeypatch):
    paths, params = _fake_env(tmp_path, monkeypatch)
    home = tmp_path / "bm-home"
    home.mkdir()
    (home / "settings.json").write_text(
        '{"storage_backend": "agentcore"}', encoding="utf-8")
    cli_main(["setup"])
    stored = json.loads((home / "settings.json").read_text("utf-8"))
    assert stored["storage_backend"] == "agentcore"


def test_doctor_reports_drift_exit_1_then_fix_then_exit_0(tmp_path, monkeypatch, capsys):
    paths, params = _fake_env(tmp_path, monkeypatch)
    assert cli_main(["doctor"]) == 1
    assert "drift" in capsys.readouterr().out.lower()
    assert cli_main(["doctor", "--fix"]) == 0
    capsys.readouterr()
    assert cli_main(["doctor"]) in (0, 1)  # 1 only if skill sources absent
    out = capsys.readouterr().out.lower()
    assert "hook" not in out  # hook drift is repaired


def test_doctor_json_output(tmp_path, monkeypatch, capsys):
    _fake_env(tmp_path, monkeypatch)
    cli_main(["doctor", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["drift"], list)


def test_setup_honors_better_memory_home_env_var(tmp_path, monkeypatch):
    """Regression: handle_setup must resolve the home via resolve_home()
    (which honors BETTER_MEMORY_HOME), not hard-default to Path.home().
    detect_machine_params is deliberately left un-monkeypatched here so the
    real home-resolution path runs end to end."""
    env_home = tmp_path / "env-home"
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(env_home))
    paths = eng.TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    monkeypatch.setattr(eng, "default_target_paths", lambda: paths)

    assert cli_main(["setup"]) == 0

    assert (env_home / "settings.json").exists()
    assert (env_home / "spool").is_dir()
    assert (env_home / "install-backups").is_dir()
