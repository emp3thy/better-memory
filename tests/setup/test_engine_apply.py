import json
import time

from better_memory.setup.engine import (
    TargetPaths,
    apply,
    diff,
)
from better_memory.setup.manifest import MachineParams

PARAMS = MachineParams(
    venv_py="/repo/.venv/bin/python", venv_pyw="/repo/.venv/bin/python",
    home="/home/u/.better-memory", repo_root="/repo",
)


def _paths(tmp_path) -> TargetPaths:
    return TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )


def test_apply_from_empty_reaches_zero_diff_and_backs_up_nothing(tmp_path):
    paths = _paths(tmp_path)
    report = apply(PARAMS, paths, home=tmp_path / "home")
    assert report.repaired  # something was written
    drifts = [d for d in diff(PARAMS, paths) if "skill" not in d.lower()]
    assert drifts == []


def test_apply_backs_up_existing_files(tmp_path):
    paths = _paths(tmp_path)
    paths.settings_json.write_text("{}", encoding="utf-8")
    apply(PARAMS, paths, home=tmp_path / "home")
    backups = list((tmp_path / "home" / "install-backups").glob("settings.json.*.bak"))
    assert len(backups) == 1


def test_apply_aborts_file_with_malformed_json_but_repairs_others(tmp_path):
    paths = _paths(tmp_path)
    paths.claude_json.write_text("{not json", encoding="utf-8")
    report = apply(PARAMS, paths, home=tmp_path / "home")
    assert paths.claude_json.read_text(encoding="utf-8") == "{not json"
    assert any("unparseable" in w for w in report.warnings)
    assert json.loads(paths.settings_json.read_text(encoding="utf-8"))["hooks"]


def test_apply_retries_once_on_concurrent_claude_json_change(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.claude_json.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    import better_memory.setup.engine as eng
    original_read = eng._read_json_and_mtime
    calls = {"n": 0}

    def flaky(path):
        data, mtime = original_read(path)
        if path == paths.claude_json and calls["n"] == 0:
            calls["n"] += 1
            # Simulate Claude Code rewriting the file between read and write.
            time.sleep(0.01)
            path.write_text(json.dumps({"mcpServers": {"x": {}}}),
                            encoding="utf-8")
        return data, mtime

    monkeypatch.setattr(eng, "_read_json_and_mtime", flaky)
    apply(PARAMS, paths, home=tmp_path / "home")
    final = json.loads(paths.claude_json.read_text(encoding="utf-8"))
    assert "better-memory" in final["mcpServers"]
    assert "x" in final["mcpServers"]  # concurrent edit survived


def test_apply_is_idempotent_second_run_repairs_nothing(tmp_path):
    paths = _paths(tmp_path)
    apply(PARAMS, paths, home=tmp_path / "home")
    second = apply(PARAMS, paths, home=tmp_path / "home")
    assert [r for r in second.repaired if "skill" not in r.lower()] == []
