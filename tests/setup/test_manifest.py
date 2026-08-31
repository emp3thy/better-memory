"""Manifest completeness and rendering tests."""
from better_memory.setup.manifest import (
    CLAUDE_MD_BLOCK,
    MANAGED_ENV,
    MANAGED_HOOKS,
    MANAGED_SKILLS,
    MachineParams,
    detect_machine_params,
    hook_entry,
    mcp_server_entry,
)

PARAMS = MachineParams(
    venv_py=r"C:\repo\.venv\Scripts\python.exe",
    venv_pyw=r"C:\repo\.venv\Scripts\pythonw.exe",
    home=r"C:\Users\u\.better-memory",
    repo_root=r"C:\repo",
)


def test_managed_hooks_cover_all_eight_entries():
    seen = {(s.module, s.event) for s in MANAGED_HOOKS}
    assert seen == {
        ("better_memory.hooks.session_bootstrap", "SessionStart"),
        ("better_memory.hooks.contextual_inject", "UserPromptSubmit"),
        ("better_memory.hooks.contextual_inject", "PreToolUse"),
        ("better_memory.hooks.commit_checkpoint", "PreToolUse"),
        ("better_memory.hooks.observer", "PostToolUse"),
        ("better_memory.hooks.session_close", "Stop"),
        ("better_memory.hooks.stop_sweep", "Stop"),
        ("better_memory.hooks.pre_compact", "PreCompact"),
    }


def test_observer_is_async_pythonw_and_bootstrap_keeps_stdout():
    by_mod = {s.module: s for s in MANAGED_HOOKS}
    obs = by_mod["better_memory.hooks.observer"]
    boot = by_mod["better_memory.hooks.session_bootstrap"]
    assert obs.is_async and not obs.needs_stdout
    assert not boot.is_async and boot.needs_stdout
    assert hook_entry(obs, PARAMS)["command"].startswith(f'"{PARAMS.venv_pyw}"')
    assert hook_entry(boot, PARAMS)["command"].startswith(f'"{PARAMS.venv_py}"')


def test_commit_checkpoint_carries_if_filter():
    spec = next(s for s in MANAGED_HOOKS
                if s.module == "better_memory.hooks.commit_checkpoint")
    assert spec.event == "PreToolUse"
    assert spec.matcher == "Bash"
    assert spec.if_filter == "Bash(git commit*)"
    assert hook_entry(spec, PARAMS)["if"] == "Bash(git commit*)"


def test_mcp_server_entry_shape():
    entry = mcp_server_entry(PARAMS)
    assert entry == {
        "type": "stdio",
        "command": PARAMS.venv_py,
        "args": ["-m", "better_memory.mcp"],
        "env": {"BETTER_MEMORY_HOME": PARAMS.home},
    }


def test_managed_env_and_skills():
    assert MANAGED_ENV == {"BETTER_MEMORY_INJECT_MODE": "deferred"}
    assert MANAGED_SKILLS == (
        "better-memory-synthesize",
        "rate-session-memories",
        "start-better-memory-ui",
    )


def test_claude_md_block_is_nonempty_and_unmarkered():
    assert "# better-memory (MANDATORY)" in CLAUDE_MD_BLOCK
    assert "BEGIN better-memory" not in CLAUDE_MD_BLOCK


def test_detect_machine_params_points_into_this_repo():
    params = detect_machine_params(home=r"C:\x\.better-memory")
    assert params.home == r"C:\x\.better-memory"
    assert "better-memory" in params.repo_root or "better_memory" in params.venv_py
