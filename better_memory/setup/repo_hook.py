"""Per-repo post-commit hook installer (spec row 13, concern 5 rules)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from better_memory.setup.manifest import MachineParams

_SENTINEL = "# better-memory post-commit (managed)"


def _hook_line(params: MachineParams) -> str:
    py = params.venv_py.replace("\\", "/")
    return f'"{py}" -m better_memory.hooks.post_commit || true'


def _hooks_dir(repo_root: Path) -> Path | None:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    custom = proc.stdout.strip()
    if custom:
        path = Path(custom)
        if not path.is_absolute():
            path = repo_root / path
        return path if path.is_dir() else None
    # Resolve hooks directory: handles regular repos, worktrees, and bare repos.
    # Bare repos resolve to None via git rev-parse --git-path (no hooks dir).
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    hooks_path = proc.stdout.strip()
    if not hooks_path:
        return None
    path = Path(hooks_path)
    if not path.is_absolute():
        path = repo_root / path
    return path if path.is_dir() else None


def ensure_post_commit(repo_root: Path, params: MachineParams) -> str | None:
    hooks_dir = _hooks_dir(repo_root)
    if hooks_dir is None:
        return None
    hook = hooks_dir / "post-commit"
    line = _hook_line(params)
    if not hook.exists():
        try:
            hook.write_text(f"#!/bin/sh\n{_SENTINEL}\n{line}\n",
                            encoding="utf-8", newline="\n")
            hook.chmod(0o755)
        except OSError as exc:
            return f"post-commit install failed in {hooks_dir}: {exc}"
        return f"post-commit hook installed in {hooks_dir}"
    try:
        content = hook.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"post-commit skipped: existing hook in {hooks_dir} is not a text script"
    if _SENTINEL in content or "better_memory.hooks.post_commit" in content:
        return None
    first = content.lstrip().splitlines()[0] if content.strip() else ""
    if not first.startswith("#!") or "sh" not in first:
        return f"post-commit skipped: existing hook in {hooks_dir} is not a plain sh script"
    try:
        hook.write_text(content.rstrip("\n") + f"\n{_SENTINEL}\n{line}\n",
                        encoding="utf-8", newline="\n")
    except OSError as exc:
        return f"post-commit chain failed in {hooks_dir}: {exc}"
    return f"post-commit hook chained after existing hook in {hooks_dir}"
