import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from better_memory.setup.engine import (
    TargetPaths,
    apply,
    diff,
    install_skills,
)
from better_memory.setup.manifest import MANAGED_SKILLS, MachineParams

PARAMS = MachineParams(
    venv_py="/repo/.venv/bin/python", venv_pyw="/repo/.venv/bin/python",
    home="/home/u/.better-memory", repo_root="/repo",
)

# The real repo checkout — used only by the symlink test below, which needs
# an actual skill directory (with a real SKILL.md) to link against.
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PARAMS = MachineParams(
    venv_py=str(REPO_ROOT / ".venv" / "bin" / "python"),
    venv_pyw=str(REPO_ROOT / ".venv" / "bin" / "python"),
    home="/home/u/.better-memory",
    repo_root=str(REPO_ROOT),
)


def _symlinks_available() -> bool:
    """Return True if the current process can create directory symlinks."""
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            lnk = Path(td) / "lnk"
            lnk.symlink_to(src, target_is_directory=True)
            return lnk.is_symlink()
    except (OSError, NotImplementedError):
        return False


requires_symlinks = pytest.mark.skipif(
    not _symlinks_available(),
    reason="Symlink creation requires admin rights or Developer Mode on Windows",
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


@requires_symlinks
def test_install_skills_creates_real_symlink_to_repo_skill(tmp_path):
    """Port of the deleted tests/cli/test_install_skill_symlink.py case:
    after install_skills runs against the real repo checkout, the user-level
    skill symlink exists and resolves to the in-repo SKILL.md."""
    skills_dir = tmp_path / "skills"
    paths = _paths(tmp_path)
    paths = TargetPaths(
        claude_json=paths.claude_json, settings_json=paths.settings_json,
        claude_md=paths.claude_md, skills_dir=skills_dir,
    )

    warnings = install_skills(paths, REAL_PARAMS)

    assert warnings == []
    link = skills_dir / "rate-session-memories"
    assert link.is_symlink()
    target = link.resolve()
    assert target.name == "rate-session-memories"
    assert (target / "SKILL.md").exists()


# ------------------------- ported from deleted tests/e2e/test_install_hooks.py
#
# The A6/A7 scenarios (symlink OSError fallback, symlink replacement ladder,
# junction recognition) exercised install_skill_symlinks() over a real
# subprocess install. That function's logic now lives verbatim in
# engine.install_skills(); these cases had no engine-level coverage, so they
# are ported here (unit-level, against a fake repo skill tree instead of a
# subprocess) rather than dropped.

LADDER_SKILL = "better-memory-synthesize"


def _fake_repo_params(tmp_path: Path) -> MachineParams:
    """A fake repo checkout with every MANAGED_SKILLS entry seeded as a real
    directory, so install_skills() never warns about a missing source."""
    repo = tmp_path / "repo"
    for name in MANAGED_SKILLS:
        skill_dir = repo / ".claude" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"{name} sentinel\n", encoding="utf-8")
    return MachineParams(
        venv_py=str(repo / ".venv" / "bin" / "python"),
        venv_pyw=str(repo / ".venv" / "bin" / "python"),
        home=str(tmp_path / "home" / ".better-memory"),
        repo_root=str(repo),
    )


def _seed_obstacle(kind: str, link: Path, tmp: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "wrong-symlink":
        wrong_target = tmp / "wrong-target"
        wrong_target.mkdir()
        link.symlink_to(wrong_target, target_is_directory=True)
    elif kind == "plain-file":
        link.write_text("user-materialized skill file", encoding="utf-8")
    elif kind == "real-dir-with-sentinel":
        link.mkdir(parents=True)
        (link / "SENTINEL.txt").write_text("user sentinel content", encoding="utf-8")
    else:  # pragma: no cover - parametrize guards this
        raise AssertionError(kind)


@requires_symlinks
@pytest.mark.parametrize(
    "obstacle", ["wrong-symlink", "plain-file", "real-dir-with-sentinel"]
)
def test_install_skills_replacement_ladder(tmp_path, obstacle):
    """A pre-existing wrong symlink / plain file / real dir with a sentinel
    at the skill path each get replaced by a correct symlink with no warning;
    the sentinel is gone (pins the documented no-backup destructive
    contract)."""
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )
    link = skills_dir / LADDER_SKILL
    _seed_obstacle(obstacle, link, tmp_path)

    warnings = install_skills(paths, params)

    assert warnings == []
    expected_source = (Path(params.repo_root) / ".claude" / "skills" / LADDER_SKILL).resolve()
    assert link.is_symlink(), f"{obstacle}: obstacle not replaced by symlink"
    assert link.resolve() == expected_source
    if obstacle == "real-dir-with-sentinel":
        assert not (link / "SENTINEL.txt").exists()
        assert not (expected_source / "SENTINEL.txt").exists()


@requires_symlinks
def test_install_skills_already_correct_symlink_is_noop(tmp_path):
    """An already-correct link is untouched (inode identity), not
    unlinked/recreated on every run."""
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )
    source = Path(params.repo_root) / ".claude" / "skills" / LADDER_SKILL
    skills_dir.mkdir(parents=True)
    link = skills_dir / LADDER_SKILL
    link.symlink_to(source, target_is_directory=True)
    before = os.lstat(link)
    before_identity = (before.st_ino, before.st_ctime_ns)

    warnings = install_skills(paths, params)

    assert warnings == []
    after = os.lstat(link)
    assert (after.st_ino, after.st_ctime_ns) == before_identity, (
        "already-correct symlink was recreated instead of no-op'd"
    )


def test_install_skills_wraps_oserror_as_warning_and_continues(tmp_path, monkeypatch):
    """Symlink creation failure (Windows without Developer Mode: WinError
    1314) is non-fatal — a warning per skill, not a crash — and does not
    stop the rest of the loop."""
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )

    def boom(self, target, target_is_directory=False):  # noqa: ARG001
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)

    warnings = install_skills(paths, params)

    assert len(warnings) == len(MANAGED_SKILLS)
    assert all("skipped" in w for w in warnings)
    for name in MANAGED_SKILLS:
        assert not (skills_dir / name).exists()
    assert skills_dir.is_dir()  # dir creation is separate from link creation


def test_junction_skill_link_is_recognised_and_skipped(tmp_path):
    """A junction (mklink /J) pointing at the right skill source is treated
    as an already-correct link, not a real directory to rmtree — junctions
    report is_symlink() == False but shutil.rmtree refuses them outright."""
    if sys.platform != "win32" or not hasattr(os.path, "isjunction"):
        pytest.skip("junctions are a Windows/3.12+ concept")

    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )
    source = Path(params.repo_root) / ".claude" / "skills" / LADDER_SKILL
    junction = skills_dir / LADDER_SKILL

    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(source)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot create junction here: {proc.stderr.strip()}")
    assert os.path.isjunction(junction)

    warnings = install_skills(paths, params)

    # Other skills may warn if this environment lacks symlink privilege
    # (see requires_symlinks); only the junction'd skill must be silent.
    assert not any(LADDER_SKILL in w for w in warnings)
    assert os.path.isjunction(junction)  # untouched, not rmtree'd
