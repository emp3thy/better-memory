"""Unmocked e2e journey: `better-memory setup` / `doctor` through the real
CLI entry point.

Background: the self-managing-setup whole-branch review flagged a gap — no
unmocked end-to-end test of the install path remained. `tests/e2e/
test_install_hooks.py` was deleted with the old `install_hooks` contract it
pinned, and `tests/e2e/test_setup_sh.py` was module-skipped (it drove
`scripts/setup.sh`, which targeted the pre-rewrite Ollama/win_path/
install_hooks-flag behavior — see that module's removal in this same
change). `tests/cli/test_setup_cmd.py` and `tests/setup/test_engine_apply.py`
cover the same merge/render logic but monkeypatch `engine.default_target_paths`
/ `manifest.detect_machine_params`, so neither exercises the real
subprocess -> argparse -> `Path.home()` path a user's `better-memory setup`
invocation actually runs.

This module closes that gap: `python -m better_memory.cli.main setup` /
`doctor` are spawned as real subprocesses with env built by `isolated_env()`
(tests/e2e/_env.py), which redirects HOME/USERPROFILE (and therefore
`Path.home()`) to a tmp directory and pins `BETTER_MEMORY_HOME` under it. No
better_memory internals are mocked or monkeypatched — this is the same code
path Claude Code's user runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from better_memory.setup.manifest import MANAGED_HOOKS
from tests.e2e._env import isolated_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    args: list[str], tmp_path: Path, *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    env = isolated_env(tmp_path)
    return subprocess.run(  # noqa: S603 — test harness, fixed argv
        [sys.executable, "-m", "better_memory.cli.main", *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_setup_creates_wiring_under_isolated_home(tmp_path: Path) -> None:
    proc = _run_cli(["setup"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})

    # MANAGED_HOOKS has 8 specs (7 unique modules — contextual_inject is
    # wired to both UserPromptSubmit and PreToolUse); every one must be
    # present under its event in the written settings.json.
    assert len(MANAGED_HOOKS) == 8
    for spec in MANAGED_HOOKS:
        commands = [
            hook.get("command", "")
            for group in hooks.get(spec.event, [])
            for hook in group.get("hooks", [])
        ]
        assert any(spec.module in cmd for cmd in commands), (
            f"{spec.event}/{spec.module} missing from settings.json hooks: {hooks}"
        )

    claude_json = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "better-memory" in claude_json["mcpServers"]

    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.count("<!-- BEGIN better-memory (managed) -->") == 1
    assert claude_md.count("<!-- END better-memory (managed) -->") == 1

    bm_settings_path = tmp_path / ".better-memory" / "settings.json"
    assert json.loads(bm_settings_path.read_text(encoding="utf-8")) == {
        "storage_backend": "sqlite"
    }

    bm_home = tmp_path / ".better-memory"
    for sub in ("state", "spool", "install-backups"):
        assert (bm_home / sub).is_dir(), sub


def test_setup_idempotent_second_run(tmp_path: Path) -> None:
    first = _run_cli(["setup"], tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    settings_path = tmp_path / ".claude" / "settings.json"
    first_bytes = settings_path.read_bytes()

    second = _run_cli(["setup"], tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert settings_path.read_bytes() == first_bytes


def test_doctor_reports_clean_after_setup(tmp_path: Path) -> None:
    """After `setup`, `doctor` must observe (effectively) no drift.

    Investigated empirically before writing this assertion (see report.md):
    on this Windows dev machine, with no elevation and no Developer Mode,
    `install_skills()`'s `Path.symlink_to()` call fails with `OSError` (no
    `SeCreateSymbolicLinkPrivilege`), but the junction fallback (`mklink
    /J`) succeeds unconditionally — Windows directory junctions are plain
    NTFS reparse points that need no special privilege, unlike symlinks.
    `engine.diff()` treats a junction as a valid link
    (`os.path.isjunction`), so `doctor` reports "wiring clean" with rc 0 —
    confirmed by hand-running `setup` then `doctor` under an isolated home.
    The same holds on POSIX, where `symlink_to()` itself needs no privilege.

    Kept defensive anyway per the plan: if a locked-down host ever denies
    BOTH symlinks and junctions, `doctor` would report skill-link drift
    lines only (engine.diff's "skill %r: not linked" / "does not resolve"
    messages) — tolerated here as long as every drift line mentions only
    "skill" and nothing else managed (hooks/mcp entry/CLAUDE.md block),
    since those are asserted byte-exact in the previous test and any drift
    there would be a real regression.
    """
    setup_proc = _run_cli(["setup"], tmp_path)
    assert setup_proc.returncode == 0, setup_proc.stdout + setup_proc.stderr

    doctor_proc = _run_cli(["doctor"], tmp_path)
    combined = doctor_proc.stdout + doctor_proc.stderr
    if "wiring clean" in doctor_proc.stdout:
        assert doctor_proc.returncode == 0, combined
        return

    assert doctor_proc.returncode == 1, combined
    drift_lines = [ln for ln in doctor_proc.stdout.splitlines() if "DRIFT" in ln]
    assert drift_lines, combined
    non_skill = [ln for ln in drift_lines if "skill" not in ln.lower()]
    assert not non_skill, f"non-skill drift after setup: {non_skill}\n{combined}"
