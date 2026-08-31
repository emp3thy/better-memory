import json

from better_memory.setup import autocheck, engine, manifest


def _wire(tmp_path, monkeypatch):
    paths = engine.TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    params = manifest.MachineParams(
        venv_py="/v/py", venv_pyw="/v/py",
        home=str(tmp_path / "home"), repo_root="/repo",
    )
    monkeypatch.setattr(engine, "default_target_paths", lambda: paths)
    monkeypatch.setattr(manifest, "detect_machine_params", lambda home=None: params)
    # install_skills always warns here regardless of repo_root: the fixture's
    # repo_root is fictional, and even a real one would hit "source not
    # found" or (on Windows without Developer Mode / symlink privilege) an
    # OSError-wrapped "skipped" warning from the real symlink attempt. Either
    # way that's orthogonal to what these tests exercise (the mtime+
    # fingerprint cache), and per the lift-pass fix any warning correctly
    # blocks the cache write — so a real skill-install warning would make
    # the "clean first repair" tests below un-satisfiable on this machine.
    # Stub it out here; skill-install behavior itself is covered by
    # tests/setup/test_engine_apply.py.
    monkeypatch.setattr(engine, "install_skills", lambda *a, **k: [])
    (tmp_path / "home" / "state").mkdir(parents=True)
    return paths, params


def test_repairs_and_reports_on_first_run(tmp_path, monkeypatch):
    paths, params = _wire(tmp_path, monkeypatch)
    line = autocheck.maybe_repair(tmp_path / "home", tmp_path)
    assert line and "repaired" in line and "next session" in line
    assert paths.settings_json.exists()


def test_second_run_short_circuits_via_cache(tmp_path, monkeypatch):
    paths, params = _wire(tmp_path, monkeypatch)
    autocheck.maybe_repair(tmp_path / "home", tmp_path)
    calls = []
    monkeypatch.setattr(engine, "diff",
                        lambda *a, **k: calls.append(1) or [])
    assert autocheck.maybe_repair(tmp_path / "home", tmp_path) is None
    assert calls == []  # mtime+fingerprint cache skipped the diff entirely


def test_kill_switch(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_WIRING_AUTOCHECK", "off")
    assert autocheck.maybe_repair(tmp_path / "home", tmp_path) is None


def test_cache_invalidated_when_config_touched(tmp_path, monkeypatch):
    paths, params = _wire(tmp_path, monkeypatch)
    autocheck.maybe_repair(tmp_path / "home", tmp_path)
    settings = json.loads(paths.settings_json.read_text("utf-8"))
    settings["hooks"].pop("PreCompact")
    paths.settings_json.write_text(json.dumps(settings), encoding="utf-8")
    line = autocheck.maybe_repair(tmp_path / "home", tmp_path)
    assert line and "repaired" in line


def test_warnings_prevent_cache_write_so_drift_rereports(tmp_path, monkeypatch):
    paths, params = _wire(tmp_path, monkeypatch)
    paths.claude_json.write_text("{not json", encoding="utf-8")
    first = autocheck.maybe_repair(tmp_path / "home", tmp_path)
    assert first and "WARN" in first
    # Nothing changed on disk; a cached fingerprint would silence this.
    second = autocheck.maybe_repair(tmp_path / "home", tmp_path)
    assert second and "WARN" in second
