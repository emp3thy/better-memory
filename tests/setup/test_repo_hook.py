import subprocess

from better_memory.setup.manifest import MachineParams
from better_memory.setup.repo_hook import ensure_post_commit

PARAMS = MachineParams(venv_py="/venv/bin/python", venv_pyw="/venv/bin/python",
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
