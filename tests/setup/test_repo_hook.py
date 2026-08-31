import subprocess

from better_memory.setup.manifest import MachineParams
from better_memory.setup.repo_hook import _SENTINEL, ensure_post_commit

PARAMS = MachineParams(venv_py="/venv/bin/python", venv_pyw="/venv/bin/python",
                       home="/h", repo_root="/repo")
PARAMS_A = MachineParams(venv_py="/venvA/bin/python", venv_pyw="/venvA/bin/python",
                         home="/h", repo_root="/repo")
PARAMS_B = MachineParams(venv_py="/venvB/bin/python", venv_pyw="/venvB/bin/python",
                         home="/h", repo_root="/repo")


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_installs_into_fresh_repo(tmp_path):
    repo = _git_repo(tmp_path)
    msg = ensure_post_commit(repo, PARAMS)
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.exists()
    content = hook.read_text(encoding="utf-8")
    assert "better_memory.hooks.post_commit" in content
    assert msg and "installed" in msg


def test_noop_when_already_installed(tmp_path):
    repo = _git_repo(tmp_path)
    ensure_post_commit(repo, PARAMS)
    assert ensure_post_commit(repo, PARAMS) is None


def test_chains_after_existing_sh_hook(tmp_path):
    repo = _git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")
    msg = ensure_post_commit(repo, PARAMS)
    content = hook.read_text(encoding="utf-8")
    assert "echo existing" in content            # original preserved
    assert "better_memory.hooks.post_commit" in content
    assert msg and "chained" in msg


def test_skips_non_sh_hook_with_warning(tmp_path):
    repo = _git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_bytes(b"MZ\x90\x00binarygarbage")
    msg = ensure_post_commit(repo, PARAMS)
    assert hook.read_bytes().startswith(b"MZ")   # untouched
    assert msg and "skip" in msg.lower()


def test_honors_core_hookspath(tmp_path):
    repo = _git_repo(tmp_path)
    custom = tmp_path / "custom-hooks"
    custom.mkdir()
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath",
                    str(custom)], check=True)
    ensure_post_commit(repo, PARAMS)
    assert (custom / "post-commit").exists()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


def test_not_a_repo_returns_none(tmp_path):
    assert ensure_post_commit(tmp_path, PARAMS) is None


def test_repoints_stale_hook_after_venv_moves(tmp_path):
    """Regression: after a repo/venv move, the already-installed check must
    not just be presence-only — it must also compare the managed invocation
    line and re-point it if the venv path changed, otherwise the hook keeps
    invoking a dead interpreter forever (the `|| true` swallows the error
    silently)."""
    repo = _git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"

    msg_a = ensure_post_commit(repo, PARAMS_A)
    assert msg_a and "installed" in msg_a
    content_a = hook.read_text(encoding="utf-8")
    assert "/venvA/bin/python" in content_a

    msg_b = ensure_post_commit(repo, PARAMS_B)
    assert msg_b == f"post-commit hook re-pointed in {hook.parent}"
    content_b = hook.read_text(encoding="utf-8")
    assert "/venvB/bin/python" in content_b
    assert "/venvA/bin/python" not in content_b
    assert "#!/bin/sh" in content_b
    assert _SENTINEL in content_b

    # Idempotent: calling again with PARAMS_B (already re-pointed) is a no-op.
    assert ensure_post_commit(repo, PARAMS_B) is None


def test_repoints_stale_hook_preserves_chained_foreign_content(tmp_path):
    """The re-point rewrite must touch ONLY the managed invocation line —
    a pre-existing chained hook's own content must survive untouched."""
    repo = _git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    ensure_post_commit(repo, PARAMS_A)
    content_a = hook.read_text(encoding="utf-8")
    assert "echo existing" in content_a

    msg = ensure_post_commit(repo, PARAMS_B)
    content_b = hook.read_text(encoding="utf-8")
    assert msg and "re-pointed" in msg
    assert "echo existing" in content_b  # foreign content untouched
    assert "/venvB/bin/python" in content_b
    assert "/venvA/bin/python" not in content_b


def test_installs_in_worktree(tmp_path):
    # Create main repo with an initial commit
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "--allow-empty", "-m", "initial"],
                   check=True)
    # Create worktree
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", str(wt), "-b", "tmp"],
                   check=True)
    # Install hook from worktree path
    msg = ensure_post_commit(wt, PARAMS)
    # Hook should land in main repo's .git/hooks, not worktree
    hook = main / ".git" / "hooks" / "post-commit"
    assert hook.exists(), "hook should be in main repo's .git/hooks"
    content = hook.read_text(encoding="utf-8")
    assert "better_memory.hooks.post_commit" in content
    assert msg and "installed" in msg
