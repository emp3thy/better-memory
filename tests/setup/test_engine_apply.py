import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import better_memory.setup.engine as eng
from better_memory.setup.engine import (
    TargetPaths,
    apply,
    diff,
    install_skills,
)
from better_memory.setup.manifest import (
    BEGIN_MARKER,
    CLAUDE_MD_BLOCK,
    END_MARKER,
    MachineParams,
)

# Skill names seeded into the fake repo checkout by _fake_repo_params().
# With managed_skills() enumerating the repo, the fake tree defines the
# managed set for these tests.
SEEDED_SKILLS = (
    "better-memory-synthesize",
    "rate-session-memories",
    "start-better-memory-ui",
)

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


def test_apply_heals_legacy_claude_md_section_outside_markers(tmp_path):
    """A CLAUDE.md with BOTH a marked block and a stray legacy section
    outside it (the exact corruption left by a pre-splice-fix run that
    appended without excising) is healed by apply(): the legacy section is
    excised, the heading collapses to one occurrence, and diff() goes
    clean afterward."""
    paths = _paths(tmp_path)
    legacy = "# Global Preferences\n\n- No whimsy.\n\n" + CLAUDE_MD_BLOCK + "\n"
    doc = legacy + "\n" + BEGIN_MARKER + "\n" + CLAUDE_MD_BLOCK + "\n" + END_MARKER + "\n"
    paths.claude_md.write_text(doc, encoding="utf-8")

    before = diff(PARAMS, paths)
    assert any("legacy unmarked" in d.lower() for d in before)

    apply(PARAMS, paths, home=tmp_path / "home")

    healed_text = paths.claude_md.read_text(encoding="utf-8")
    assert healed_text.count("# better-memory (MANDATORY)") == 1
    assert healed_text.count(BEGIN_MARKER) == 1
    assert "# Global Preferences" in healed_text  # foreign content preserved

    after = [d for d in diff(PARAMS, paths) if "skill" not in d.lower()]
    assert after == []


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
    """A fake repo checkout with every SEEDED_SKILLS entry seeded as a real
    directory, so install_skills() never warns about a missing source."""
    repo = tmp_path / "repo"
    for name in SEEDED_SKILLS:
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
    stop the rest of the loop. The mklink /J fallback is also mocked to
    fail here so this pins the "everything failed" end of the ladder; the
    fallback succeeding is covered separately below."""
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )

    def boom(self, target, target_is_directory=False):  # noqa: ARG001
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(
        eng.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1),
    )

    warnings = install_skills(paths, params)

    assert len(warnings) == len(SEEDED_SKILLS)
    assert all("skipped" in w for w in warnings)
    for name in SEEDED_SKILLS:
        assert not (skills_dir / name).exists()
    assert skills_dir.is_dir()  # dir creation is separate from link creation


def test_install_skills_junction_fallback_heals_when_symlink_denied(tmp_path, monkeypatch):
    """When symlink_to raises OSError, install_skills falls back to
    `mklink /J`; a successful junction creation leaves no warning at all —
    this is what lets a Developer-Mode-less machine reach "wiring clean"."""
    if sys.platform != "win32":
        pytest.skip("mklink /J junction fallback is Windows-only")
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )

    def boom(self, target, target_is_directory=False):  # noqa: ARG001
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)

    calls = []

    def fake_run(cmd, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        Path(cmd[4]).mkdir(parents=True, exist_ok=True)  # simulate mklink /J
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(eng.subprocess, "run", fake_run)

    warnings = install_skills(paths, params)

    assert warnings == []
    assert len(calls) == len(SEEDED_SKILLS)
    for cmd in calls:
        assert cmd[:4] == ["cmd", "/c", "mklink", "/J"]
    for name in SEEDED_SKILLS:
        assert (skills_dir / name).exists()


def test_install_skills_junction_fallback_warns_when_both_fail(tmp_path, monkeypatch):
    """If the mklink /J fallback also fails (non-zero exit), the original
    symlink-skipped warning is still surfaced — nothing is swallowed."""
    if sys.platform != "win32":
        pytest.skip("mklink /J junction fallback is Windows-only")
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )

    def boom(self, target, target_is_directory=False):  # noqa: ARG001
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(
        eng.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1, stderr=b"denied"),
    )

    warnings = install_skills(paths, params)

    assert len(warnings) == len(SEEDED_SKILLS)
    assert all("skipped" in w for w in warnings)
    for name in SEEDED_SKILLS:
        assert not (skills_dir / name).exists()


def test_install_skills_junction_fallback_warns_when_subprocess_raises(tmp_path, monkeypatch):
    """If the mklink /J fallback subprocess itself raises (e.g. it times
    out), that must not crash apply() — it's treated as a fallback failure,
    same as a non-zero exit, and the original symlink-skipped warning is
    still surfaced."""
    if sys.platform != "win32":
        pytest.skip("mklink /J junction fallback is Windows-only")
    params = _fake_repo_params(tmp_path)
    skills_dir = tmp_path / "skills"
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=skills_dir,
    )

    def boom(self, target, target_is_directory=False):  # noqa: ARG001
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)

    def raise_timeout(cmd, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr(eng.subprocess, "run", raise_timeout)

    warnings = install_skills(paths, params)

    assert len(warnings) == len(SEEDED_SKILLS)
    assert all("skipped" in w for w in warnings)
    for name in SEEDED_SKILLS:
        assert not (skills_dir / name).exists()


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


# ------------------------- dynamic enumeration + stale-link prune


def _make_link(link: Path, target: Path) -> None:
    """Create a directory link the way this environment allows: a symlink
    where privileged, else a junction (mklink /J needs no privilege)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"cannot create links here: {proc.stderr.strip()}")


def _link_exists(link: Path) -> bool:
    return os.path.lexists(link) or os.path.isjunction(link)


def test_install_skills_picks_up_newly_added_skill_dir(tmp_path):
    """A skill directory added to the repo after install is enumerated and
    linked on the next run — no hardcoded skill list to update."""
    params = _fake_repo_params(tmp_path)
    new_skill = Path(params.repo_root) / ".claude" / "skills" / "zz-brand-new"
    new_skill.mkdir(parents=True)
    (new_skill / "SKILL.md").write_text("new skill\n", encoding="utf-8")
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=tmp_path / "skills",
    )

    warnings = install_skills(paths, params)

    link = paths.skills_dir / "zz-brand-new"
    if any("zz-brand-new" in w for w in warnings):
        pytest.skip("environment can create neither symlinks nor junctions")
    assert _link_exists(link)
    assert Path(os.path.realpath(link)) == new_skill.resolve()


def test_install_skills_prunes_stale_repo_targeted_link(tmp_path):
    """A link in skills_dir that targets <repo>/.claude/skills/<name> whose
    source no longer exists (skill deleted/renamed in repo) is removed."""
    params = _fake_repo_params(tmp_path)
    gone = Path(params.repo_root) / ".claude" / "skills" / "old-deleted-skill"
    gone.mkdir(parents=True)
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=tmp_path / "skills",
    )
    stale = paths.skills_dir / "old-deleted-skill"
    _make_link(stale, gone)
    shutil.rmtree(gone)  # now dangling, repo-targeted

    install_skills(paths, params)

    assert not _link_exists(stale)


def test_install_skills_preserves_foreign_entries(tmp_path):
    """Entries in skills_dir that are not better-memory's — a user's real
    skill directory, or a link pointing outside the repo skills tree — are
    never touched by the prune pass."""
    params = _fake_repo_params(tmp_path)
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=tmp_path / "skills",
    )
    user_dir = paths.skills_dir / "user-skill"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text("user's own skill\n", encoding="utf-8")
    elsewhere = tmp_path / "other-tool-skills" / "other-skill"
    elsewhere.mkdir(parents=True)
    foreign_link = paths.skills_dir / "other-skill"
    _make_link(foreign_link, elsewhere)

    install_skills(paths, params)

    assert (user_dir / "SKILL.md").exists()
    assert _link_exists(foreign_link)


def test_diff_reports_stale_repo_targeted_link(tmp_path):
    """diff() must surface a stale repo-targeted link as drift, or the
    autocheck's `if drift:` guard would never trigger the pruning apply()."""
    params = _fake_repo_params(tmp_path)
    gone = Path(params.repo_root) / ".claude" / "skills" / "old-deleted-skill"
    gone.mkdir(parents=True)
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json", settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md", skills_dir=tmp_path / "skills",
    )
    stale = paths.skills_dir / "old-deleted-skill"
    _make_link(stale, gone)
    shutil.rmtree(gone)

    drift = diff(params, paths)

    assert any("skill" in d and "stale link" in d for d in drift)
