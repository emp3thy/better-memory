# Self-Managing Setup & Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One declarative wiring manifest drives better-memory's install, per-session drift detection, and automatic repair of all Claude Code wiring.

**Architecture:** New `better_memory/setup/` package: `manifest.py` (declarative managed surface, machine-param rendered) + `engine.py` (render/inspect/diff/apply with backups and atomic writes) + `autocheck.py` (fingerprint-cached per-session drift repair) + `repo_hook.py` (per-repo post-commit installer). Consumed by new `better-memory setup` / `better-memory doctor` CLI subcommands and by `session_bootstrap`. Three currently-manual hooks become package modules.

**Tech Stack:** Python 3.12, stdlib only (argparse, json, pathlib, hashlib); pytest; ruff preset E,F,I,B,UP,SIM; uv.

**Spec:** `docs/superpowers/specs/2026-08-30-self-managing-setup-design.md`

## Guardrails (from memory / standards — read before executing)

- **[docs-sync, conf 0.95, ev 7]** README + website must ship in the same PR as code changes. Module docstrings enumerate env vars — update them in the SAME task that adds an env var. Task 10 covers user docs; docstring edits are in-task.
- **[ruff traps, standard]** No `import pytest` unless a `pytest.*` symbol is used (F401). Use `from datetime import UTC` + `datetime.now(UTC)` if adding tz-aware times (UP017). No `(str, Enum)` — use `StrEnum` (UP042; this plan avoids enums entirely). Run `uv run ruff check` before every commit.
- **[plan-ordering, standard]** `better_memory/setup/__init__.py` ships docstring-only in Task 1; no re-exports (later tasks would forward-reference).
- **[verbatim-import smoke, standard]** All imports used in test code below were verified against the tree at plan time: `better_memory.config.get_config`, `better_memory.config.project_name`, `better_memory.cli.install_hooks.merge_claude_json/merge_settings_json/_hook_entry/_backup/_atomic_write`, `better_memory.hooks._error_log.record_hook_error` all exist on this branch.
- **[fd-leak, conf 0.6]** Engine reuses the existing `_atomic_write` (tmp-file + `os.replace`) pattern — do not introduce `mkstemp + os.fdopen`.
- Dismissed: freeze-localization logging (no hang debugging here); RED-step guard-move check (no guards being moved); TS Partial excess-property (not TypeScript).

## Global Constraints

- Windows-first: interpreter split `python.exe` (needs_stdout hooks + MCP) vs `pythonw.exe` (async hooks). POSIX: same path for both.
- Hooks NEVER raise and NEVER block a session: every entry point wraps in `try/except BaseException` and exits 0 (existing pattern in `hooks/session_bootstrap.py:141-160`).
- Only the managed subset of any config file is modified; foreign entries preserved byte-for-byte. Malformed JSON → abort that file, never write.
- Every config write: timestamped backup to `~/.better-memory/install-backups/` first, then atomic tmp+`os.replace`.
- Storage backend (`sqlite`/`agentcore`) is never touched by setup/doctor.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Lint/test gates before every commit: `uv run ruff check .` and `uv run pytest <touched test paths> -q`.

## File Structure

```
better_memory/setup/__init__.py        (docstring only)
better_memory/setup/manifest.py        Task 1  — declarative managed surface
better_memory/setup/engine.py          Task 3+4 — render/inspect/diff/apply
better_memory/setup/autocheck.py       Task 8  — fingerprint cache + session repair
better_memory/setup/repo_hook.py       Task 6  — post-commit auto-install
better_memory/hooks/pre_compact.py     Task 2  — ports loose PreCompact script
better_memory/hooks/commit_checkpoint.py Task 2 — replaces echo hook
better_memory/hooks/stop_sweep.py      Task 2  — replaces echo hook
better_memory/cli/setup_cmd.py         Task 7  — setup + doctor subcommands
better_memory/cli/main.py              Task 7  — register subcommands (modify)
better_memory/cli/install_hooks.py     Task 7  — deprecation shim (modify)
better_memory/hooks/session_bootstrap.py Task 8 — autocheck call, sentinel removal (modify)
better_memory/hooks/_claude_md_sentinel.py Task 8 — DELETE (+ its test)
scripts/setup.ps1                      Task 9  — new Windows bootstrap
scripts/setup.sh                       Task 9  — slim to 3 steps, ollama removed (modify)
tests/setup/…                          Tasks 1,3,4,5,6,8
tests/hooks/…                          Tasks 2,8
tests/cli/test_setup_cmd.py            Task 7
docs + README + website                Task 10 (modify)
```

---

### Task 1: Wiring manifest (confidence 95%)

**Files:**
- Create: `better_memory/setup/__init__.py`, `better_memory/setup/manifest.py`
- Test: `tests/setup/__init__.py`, `tests/setup/test_manifest.py`

**Interfaces:**
- Consumes: nothing (pure data + stdlib).
- Produces (later tasks rely on these exact names):
  - `@dataclass(frozen=True) MachineParams: venv_py: str; venv_pyw: str; home: str; repo_root: str`
  - `detect_machine_params(home: str | None = None) -> MachineParams`
  - `@dataclass(frozen=True) HookSpec: module: str; event: str; matcher: str | None; is_async: bool; needs_stdout: bool; if_filter: str | None = None`
  - `MANAGED_HOOKS: tuple[HookSpec, ...]` (8 specs)
  - `MANAGED_ENV: dict[str, str]` == `{"BETTER_MEMORY_INJECT_MODE": "deferred"}`
  - `MANAGED_SKILLS: tuple[str, ...]` == `("better-memory-synthesize", "rate-session-memories", "start-better-memory-ui")`
  - `BEGIN_MARKER = "<!-- BEGIN better-memory (managed) -->"`, `END_MARKER = "<!-- END better-memory (managed) -->"`
  - `CLAUDE_MD_BLOCK: str` (canonical protocol text, WITHOUT markers)
  - `LEGACY_HOOK_MODULES: frozenset[str]` — the existing two legacy modules PLUS the literal loose-script command substring `pre-compact-better-memory.py` and the two echo-hook signatures `MEMORY CHECKPOINT before commit` and `MEMORY SWEEP: record any non-obvious observations` (scrub targets; see Step 3)
  - `hook_entry(spec: HookSpec, params: MachineParams) -> dict`
  - `mcp_server_entry(params: MachineParams) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/setup/__init__.py` — empty file. `tests/setup/test_manifest.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/setup/test_manifest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'better_memory.setup'`

- [ ] **Step 3: Implement**

`better_memory/setup/__init__.py`:

```python
"""Self-managing setup: declarative wiring manifest + install/doctor engine.

See docs/superpowers/specs/2026-08-30-self-managing-setup-design.md.
"""
```

`better_memory/setup/manifest.py` — the declarative managed surface. Key points:

```python
"""Declarative manifest of every Claude Code artifact better-memory manages.

Managed surface (spec table, rows 1-12): eight hook entries and one env key
in ~/.claude/settings.json, the MCP server entry in ~/.claude.json, the
CLAUDE.md managed block, and three user-scope skills. Row 13 (per-repo
post-commit hook) lives in setup/repo_hook.py.

Pure data + rendering helpers; no I/O except detect_machine_params().
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MachineParams:
    venv_py: str
    venv_pyw: str
    home: str
    repo_root: str


def detect_machine_params(home: str | None = None) -> MachineParams:
    repo_root = Path(__file__).resolve().parents[2]
    if sys.platform == "win32":
        venv_py = repo_root / ".venv" / "Scripts" / "python.exe"
        venv_pyw = repo_root / ".venv" / "Scripts" / "pythonw.exe"
    else:
        venv_py = repo_root / ".venv" / "bin" / "python"
        venv_pyw = venv_py
    resolved_home = home or str(Path.home() / ".better-memory")
    return MachineParams(
        venv_py=str(venv_py), venv_pyw=str(venv_pyw),
        home=resolved_home, repo_root=str(repo_root),
    )


@dataclass(frozen=True)
class HookSpec:
    module: str
    event: str
    matcher: str | None
    is_async: bool
    needs_stdout: bool
    if_filter: str | None = None
```

`MANAGED_HOOKS` extends the existing `_OUR_HOOKS` tuple from
`cli/install_hooks.py:55-76` (copy the five existing specs WITH their
comments) plus three new specs:

```python
    HookSpec("better_memory.hooks.commit_checkpoint", "PreToolUse", "Bash",
             False, True, if_filter="Bash(git commit*)"),
    HookSpec("better_memory.hooks.stop_sweep", "Stop", None, False, True),
    HookSpec("better_memory.hooks.pre_compact", "PreCompact", None, False, True),
```

`hook_entry()` — port `_hook_entry` from `cli/install_hooks.py:122-138`
verbatim, adding after the `is_async` branch:

```python
    if spec.if_filter is not None:
        entry["if"] = spec.if_filter
```

`mcp_server_entry()`:

```python
def mcp_server_entry(params: MachineParams) -> dict:
    return {
        "type": "stdio",
        "command": params.venv_py,
        "args": ["-m", "better_memory.mcp"],
        "env": {"BETTER_MEMORY_HOME": params.home},
    }
```

Constants: `MANAGED_ENV = {"BETTER_MEMORY_INJECT_MODE": "deferred"}`;
`MANAGED_SKILLS = ("better-memory-synthesize", "rate-session-memories", "start-better-memory-ui")`;
`BEGIN_MARKER` / `END_MARKER` as in Interfaces above.

`LEGACY_HOOK_MODULES`: the two module names from
`cli/install_hooks.py:81-84` plus these three literal command substrings
(they identify the loose script and the two echo hooks so the REMOVE pass
scrubs the hand-added versions):
`"pre-compact-better-memory.py"`, `"MEMORY CHECKPOINT before commit"`,
`"MEMORY SWEEP: record any non-obvious observations"`.

`CLAUDE_MD_BLOCK`: copy VERBATIM from `C:\Users\gethi\.claude\CLAUDE.md`
the section starting at the line `# better-memory (MANDATORY)` up to but
NOT including the line `# Process Discipline` (strip trailing blank lines).
Store as a module-level triple-quoted raw string. This is canonical v1; the
golden parity test (Task 5) locks it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/setup/test_manifest.py -q` — Expected: PASS
Run: `uv run ruff check better_memory/setup tests/setup` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/setup tests/setup
git commit -m "feat(setup): declarative wiring manifest"
```

---

### Task 2: Three new hook modules (confidence 95%)

**Files:**
- Create: `better_memory/hooks/pre_compact.py`, `better_memory/hooks/commit_checkpoint.py`, `better_memory/hooks/stop_sweep.py`
- Test: `tests/hooks/test_pre_compact.py`, `tests/hooks/test_commit_checkpoint.py`, `tests/hooks/test_stop_sweep.py`

**Interfaces:**
- Consumes: nothing package-internal (stdin/stdout JSON only).
- Produces: modules referenced by name in Task 1's `MANAGED_HOOKS` (already declared there); each has `main() -> None` and `if __name__ == "__main__": main()`.

- [ ] **Step 1: Write the failing tests**

`tests/hooks/test_pre_compact.py` (same monkeypatch-stdin/capsys style as `tests/hooks/test_session_close.py`):

```python
import json


def _run(monkeypatch, capsys, payload: str) -> dict:
    import io
    import sys as _sys
    from better_memory.hooks import pre_compact
    monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
    pre_compact.main()
    return json.loads(capsys.readouterr().out)


def test_emits_precompact_additional_context(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, json.dumps({"session_id": "abc123"}))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreCompact"
    assert "abc123" in hso["additionalContext"]
    assert "memory_observe" in hso["additionalContext"]


def test_survives_empty_stdin(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, "")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
```

`tests/hooks/test_commit_checkpoint.py`:

```python
import json


def _run(monkeypatch, capsys, payload: str) -> str:
    import io
    import sys as _sys
    from better_memory.hooks import commit_checkpoint
    monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
    commit_checkpoint.main()
    return capsys.readouterr().out


def test_git_commit_gets_checkpoint(monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "git commit -m x"}})
    out = json.loads(_run(monkeypatch, capsys, payload))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "MEMORY CHECKPOINT before commit" in ctx


def test_non_commit_command_prints_nothing(monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert _run(monkeypatch, capsys, payload) == ""
```

`tests/hooks/test_stop_sweep.py`:

```python
import json


def test_emits_sweep_system_message(monkeypatch, capsys):
    import io
    import sys as _sys
    from better_memory.hooks import stop_sweep
    monkeypatch.setattr(_sys, "stdin", io.StringIO("{}"))
    stop_sweep.main()
    out = json.loads(capsys.readouterr().out)
    assert "MEMORY SWEEP" in out["systemMessage"]
    assert "decision" not in out  # must never block the Stop
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/hooks/test_pre_compact.py tests/hooks/test_commit_checkpoint.py tests/hooks/test_stop_sweep.py -q`
Expected: FAIL — ModuleNotFoundError ×3

- [ ] **Step 3: Implement**

`pre_compact.py` — port of `~/.claude/hooks/pre-compact-better-memory.py`
(message text verbatim from that script, including the four numbered
items), with the standard robust-stdin pattern:

```python
"""PreCompact hook: directive to persist conversation state before compaction."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except BaseException:  # noqa: BLE001 — hooks never fail
        hook_input = {}
    session_id = hook_input.get("session_id", "unknown")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": (
                f"IMPORTANT (session {session_id}): Context is about to be "
                "compacted. Before proceeding, persist current conversation "
                "state to better-memory via mcp__better-memory__memory_observe. "
                "Record one observation per durable fact/decision, setting "
                "outcome (success/failure/neutral) and filling component/theme/"
                "trigger_type where applicable. Include: (1) the task currently "
                "in flight, (2) key decisions made in this session, (3) any open "
                "questions or next steps, (4) file paths and line numbers "
                "relevant to current work. This ensures continuity after "
                "compaction."
            ),
        }
    }), flush=True)


if __name__ == "__main__":
    main()
```

`commit_checkpoint.py` — defense in depth: even though the settings entry
carries `"if": "Bash(git commit*)"`, the module re-checks the payload so
machines whose Claude Code build ignores `if` don't spam every Bash call:

```python
"""PreToolUse(Bash) hook: memory-observe reminder before git commit."""
from __future__ import annotations

import json
import sys

_MESSAGE = (
    "MEMORY CHECKPOINT before commit: per CLAUDE.md mandatory triggers, if "
    "this commit fixes a non-obvious bug, addresses reviewer feedback, or "
    "wraps a phase, you MUST call mcp__better-memory__memory_observe BEFORE "
    "running git commit. Skipping is a CLAUDE.md violation."
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except BaseException:  # noqa: BLE001
        payload = {}
    command = str(payload.get("tool_input", {}).get("command", ""))
    if "git commit" not in command:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _MESSAGE,
        }
    }), flush=True)


if __name__ == "__main__":
    main()
```

`stop_sweep.py` — plain systemMessage, no decision key (never blocks;
blocking Stop is `session_close`'s job alone):

```python
"""Stop hook: reminder to sweep session observations into better-memory."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        sys.stdin.read()
    except BaseException:  # noqa: BLE001
        pass
    print(json.dumps({
        "systemMessage": (
            "MEMORY SWEEP: record any non-obvious observations from this "
            "session before stopping. See CLAUDE.md mandatory triggers — "
            "review-fix commits, phase boundaries, reviewer-flagged bugs."
        ),
    }), flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/hooks/test_pre_compact.py tests/hooks/test_commit_checkpoint.py tests/hooks/test_stop_sweep.py -q` — Expected: PASS
Run: `uv run ruff check better_memory/hooks tests/hooks` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/hooks/pre_compact.py better_memory/hooks/commit_checkpoint.py better_memory/hooks/stop_sweep.py tests/hooks/test_pre_compact.py tests/hooks/test_commit_checkpoint.py tests/hooks/test_stop_sweep.py
git commit -m "feat(hooks): pre_compact, commit_checkpoint, stop_sweep modules"
```

---

### Task 3: Engine — render, inspect, diff (confidence 92%)

**Files:**
- Create: `better_memory/setup/engine.py`
- Test: `tests/setup/test_engine.py`

**Interfaces:**
- Consumes: Task 1's `MachineParams`, `MANAGED_HOOKS`, `MANAGED_ENV`, `MANAGED_SKILLS`, `LEGACY_HOOK_MODULES`, `BEGIN_MARKER`, `END_MARKER`, `CLAUDE_MD_BLOCK`, `hook_entry`, `mcp_server_entry`.
- Produces:
  - `@dataclass(frozen=True) TargetPaths: claude_json: Path; settings_json: Path; claude_md: Path; skills_dir: Path` + `default_target_paths() -> TargetPaths` (all under `Path.home()`)
  - `render(params: MachineParams) -> dict` — desired state: `{"settings_hooks": {event: [group, ...]}, "settings_env": {...}, "mcp_entry": {...}, "claude_md_block": str, "skills": (...)}`
  - `merge_settings(existing: dict, params: MachineParams) -> dict` — two-pass REMOVE/ADD over hooks AND `env` keys (port of `merge_settings_json` from `cli/install_hooks.py:141-215`, extended to all 8 specs, `if_filter` groups, and `env` merge)
  - `patch_mcp_entry(existing: dict, params: MachineParams) -> dict`
  - `splice_managed_block(text: str, block: str) -> str`
  - `extract_managed_block(text: str) -> str | None`
  - `diff(params: MachineParams, paths: TargetPaths) -> list[str]` — human-readable drift lines; `[]` when clean
  - `fingerprint(params: MachineParams) -> str` — sha256 hex of the canonical JSON dump of `render()`

- [ ] **Step 1: Write the failing tests**

`tests/setup/test_engine.py`:

```python
import json

from better_memory.setup.engine import (
    TargetPaths,
    diff,
    extract_managed_block,
    fingerprint,
    merge_settings,
    patch_mcp_entry,
    render,
    splice_managed_block,
)
from better_memory.setup.manifest import (
    BEGIN_MARKER,
    CLAUDE_MD_BLOCK,
    END_MARKER,
    MachineParams,
)

PARAMS = MachineParams(
    venv_py="/repo/.venv/bin/python", venv_pyw="/repo/.venv/bin/python",
    home="/home/u/.better-memory", repo_root="/repo",
)


def test_merge_settings_preserves_foreign_hooks_and_env():
    existing = {
        "env": {"OTHER": "1"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "model": "fable",
    }
    merged = merge_settings(existing, PARAMS)
    assert merged["model"] == "fable"
    assert merged["env"]["OTHER"] == "1"
    assert merged["env"]["BETTER_MEMORY_INJECT_MODE"] == "deferred"
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo hi" in stop_cmds
    assert any("session_close" in c for c in stop_cmds)
    assert any("stop_sweep" in c for c in stop_cmds)


def test_merge_settings_scrubs_legacy_loose_script_and_echo_hooks():
    existing = {"hooks": {
        "PreCompact": [{"hooks": [{"type": "command",
            "command": 'python "C:\\u\\.claude\\hooks\\pre-compact-better-memory.py"'}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": "echo '{...MEMORY CHECKPOINT before commit...}'"}]}],
    }}
    merged = merge_settings(existing, PARAMS)
    pre_compact_cmds = [h["command"] for g in merged["hooks"]["PreCompact"]
                        for h in g["hooks"]]
    assert all("pre-compact-better-memory.py" not in c for c in pre_compact_cmds)
    assert any("better_memory.hooks.pre_compact" in c for c in pre_compact_cmds)
    pretool_cmds = [h["command"] for g in merged["hooks"]["PreToolUse"]
                    for h in g["hooks"]]
    assert all("MEMORY CHECKPOINT" not in c for c in pretool_cmds)
    assert any("commit_checkpoint" in c for c in pretool_cmds)


def test_merge_settings_is_idempotent():
    once = merge_settings({}, PARAMS)
    twice = merge_settings(json.loads(json.dumps(once)), PARAMS)
    assert once == twice


def test_patch_mcp_entry_touches_only_our_key():
    existing = {"mcpServers": {"other": {"type": "http"}}, "projects": {"x": 1}}
    patched = patch_mcp_entry(existing, PARAMS)
    assert patched["mcpServers"]["other"] == {"type": "http"}
    assert patched["projects"] == {"x": 1}
    assert patched["mcpServers"]["better-memory"]["command"] == PARAMS.venv_py


def test_patch_mcp_entry_preserves_user_env_extras():
    existing = {"mcpServers": {"better-memory": {
        "type": "stdio", "command": "old", "args": [],
        "env": {"BETTER_MEMORY_EMBED_LOG": "1"},
    }}}
    patched = patch_mcp_entry(existing, PARAMS)
    env = patched["mcpServers"]["better-memory"]["env"]
    assert env["BETTER_MEMORY_EMBED_LOG"] == "1"
    assert env["BETTER_MEMORY_HOME"] == PARAMS.home


def test_splice_appends_block_when_absent_and_replaces_when_stale():
    doc = "# Global Preferences\n\n- No whimsy.\n"
    spliced = splice_managed_block(doc, CLAUDE_MD_BLOCK)
    assert spliced.startswith(doc)
    assert extract_managed_block(spliced) == CLAUDE_MD_BLOCK
    stale = spliced.replace("MANDATORY", "OPTIONAL")
    healed = splice_managed_block(stale, CLAUDE_MD_BLOCK)
    assert extract_managed_block(healed) == CLAUDE_MD_BLOCK
    assert healed.count(BEGIN_MARKER) == 1 and healed.count(END_MARKER) == 1


def test_fingerprint_stable_and_param_sensitive():
    assert fingerprint(PARAMS) == fingerprint(PARAMS)
    other = MachineParams(venv_py="/x", venv_pyw="/x", home="/h", repo_root="/r")
    assert fingerprint(PARAMS) != fingerprint(other)


def test_diff_reports_missing_wiring(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    drifts = diff(PARAMS, paths)
    assert any("mcp" in d.lower() for d in drifts)
    assert any("hook" in d.lower() for d in drifts)
    assert any("claude.md" in d.lower() for d in drifts)


def test_diff_empty_after_render_applied(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    paths.claude_json.write_text(
        json.dumps(patch_mcp_entry({}, PARAMS)), encoding="utf-8")
    paths.settings_json.write_text(
        json.dumps(merge_settings({}, PARAMS)), encoding="utf-8")
    paths.claude_md.write_text(
        splice_managed_block("", CLAUDE_MD_BLOCK), encoding="utf-8")
    drifts = diff(PARAMS, paths)
    assert [d for d in drifts if "skill" not in d.lower()] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/setup/test_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'better_memory.setup.engine'`

- [ ] **Step 3: Implement `engine.py` (render/inspect/diff half)**

Port `merge_settings_json` (`cli/install_hooks.py:141-215`) as
`merge_settings`, with three changes: iterate `MANAGED_HOOKS` (8 specs);
groups with `matcher`/`if_filter` carry those keys (`if` copied from
`hook_entry`); after the hook passes, merge env:

```python
    env = dict(config.get("env", {}))
    env.update(MANAGED_ENV)
    config["env"] = env
```

The REMOVE pass strips any hook whose `command` contains any entry of
`strip_modules = {s.module for s in MANAGED_HOOKS} | LEGACY_HOOK_MODULES`
(substring match — this is why Task 1 put the loose-script filename and
echo-hook message fragments into `LEGACY_HOOK_MODULES`).

`patch_mcp_entry` ports `merge_claude_json` (`cli/install_hooks.py:90-116`)
using `mcp_server_entry(params)` as the base and preserving existing env
extras exactly as the original does.

`splice_managed_block`: if both markers present, replace everything between
them (inclusive) with `BEGIN_MARKER + "\n" + block + "\n" + END_MARKER`;
if absent, append `"\n\n" + BEGIN_MARKER + "\n" + block + "\n" + END_MARKER + "\n"`.
`extract_managed_block` returns the text between markers (stripped of the
single leading/trailing newline) or `None`.

`render(params)` builds the dict in Interfaces. `fingerprint(params)`:

```python
def fingerprint(params: MachineParams) -> str:
    canon = json.dumps(render(params), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
```

`diff(params, paths)`: load each file (missing → `{}` / `""`; malformed
JSON → drift line `"<path>: unparseable JSON (manual fix needed)"` and skip),
then compare: `merge_settings(live_settings, params) != live_settings` →
one drift line per differing event + env; `patch_mcp_entry(live_claude,
params) != live_claude` → drift line; `extract_managed_block(live_md) !=
CLAUDE_MD_BLOCK` → drift line; each skill link missing or not resolving to
the repo skill dir → drift line (use the junction-aware check from
`cli/install_hooks.py:256`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/setup/test_engine.py -q` — Expected: PASS
Run: `uv run ruff check better_memory/setup tests/setup` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/setup/engine.py tests/setup/test_engine.py
git commit -m "feat(setup): engine render/inspect/diff"
```

---

### Task 4: Engine — apply (confidence 93% after lift pass)

**Files:**
- Modify: `better_memory/setup/engine.py`
- Test: `tests/setup/test_engine_apply.py`

**Interfaces:**
- Consumes: Task 3's pure functions; `_backup`/`_atomic_write` patterns from `cli/install_hooks.py:301-332` (copy into engine.py — install_hooks becomes a shim in Task 7 and must not be a dependency).
- Produces:
  - `@dataclass ApplyReport: repaired: list[str]; warnings: list[str]`
  - `apply(params: MachineParams, paths: TargetPaths, *, home: Path) -> ApplyReport` — backups go to `home / "install-backups"`, lock file is `home / "state" / "setup-apply.lock"`; apply creates BOTH directories with `mkdir(parents=True, exist_ok=True)` before use (callers cannot be trusted to pre-create them)
  - `install_skills(paths: TargetPaths, params: MachineParams) -> list[str]` (returns warning strings; port of `install_skill_symlinks` from `cli/install_hooks.py:228-279`, parameterized by target dir and repo root, extended to `MANAGED_SKILLS`)

**Sub-90% risk being mitigated:** `~/.claude.json` concurrent rewrite by Claude Code (spec concern 3) and CLAUDE.md splice damaging user content. Mitigations are IN the steps: key-scoped patch + re-read-before-write + one retry; splice tested against a fixture copy of the real global CLAUDE.md; validate-then-write; lock file with stale timeout.

- [ ] **Step 1: Write the failing tests**

`tests/setup/test_engine_apply.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/setup/test_engine_apply.py -q`
Expected: FAIL — `ImportError: cannot import name 'apply'`

- [ ] **Step 3: Implement apply half of `engine.py`**

Copy `_backup` and `_atomic_write` from `cli/install_hooks.py:301-332`
into engine.py unchanged. Add:

```python
def _read_json_and_mtime(path: Path) -> tuple[dict | None, float]:
    """None for malformed JSON; ({}, 0.0) for missing file."""
    if not path.exists():
        return {}, 0.0
    try:
        return json.loads(path.read_text(encoding="utf-8")), path.stat().st_mtime
    except json.JSONDecodeError:
        return None, path.stat().st_mtime
```

`apply()` flow (mitigations inline):

0. `backup_dir = home / "install-backups"`; `lock_path = home / "state" / "setup-apply.lock"`;
   `mkdir(parents=True, exist_ok=True)` on both parent dirs FIRST (fresh
   machines and tests have neither).
1. Acquire lock: `os.open(lock_path, os.O_CREAT | os.O_EXCL)`; if it exists
   and is older than 60 s, delete and retry once; if still held, return
   `ApplyReport([], ["another apply in progress; skipped"])`. Release in
   `finally` (close fd + unlink).
2. `settings_json`: read; malformed → warning, skip. Else
   `merged = merge_settings(existing, params)`; if `merged != existing`:
   backup, `_atomic_write(json.dumps(merged, indent=2) + "\n")`, append
   repaired line.
3. `claude_json` (concern-3 sequence): read via `_read_json_and_mtime`;
   malformed → warning. Else compute `patched = patch_mcp_entry(...)`; if
   changed: backup, then **re-read immediately**; if mtime moved since first
   read, recompute `patched` from the fresh content (one retry); write
   atomically.
4. `claude_md`: read text (missing → `""`);
   `new = splice_managed_block(text, CLAUDE_MD_BLOCK)`; if changed: backup,
   atomic write, repaired line.
5. `install_skills(paths, params)` — port `install_skill_symlinks`
   (`cli/install_hooks.py:228-279`) with `repo_skills_dir = Path(params.repo_root) / ".claude" / "skills"`
   and `paths.skills_dir` as target, looping `MANAGED_SKILLS`; OSError →
   warning string instead of stderr print; skipped/absent source dirs →
   warning. NOTE: `start-better-memory-ui` currently lives only in
   `~/.claude/skills/` — Step 3a below vendors it into the repo first.
6. Return `ApplyReport(repaired, warnings)`.

- [ ] **Step 3a: Vendor the start-better-memory-ui skill into the repo**

Verified at plan time: `~/.claude/skills/start-better-memory-ui` is a real
directory (not a link) containing a single `SKILL.md` — a plain copy works.

```bash
cp -r ~/.claude/skills/start-better-memory-ui .claude/skills/start-better-memory-ui
```

Then open the copied `SKILL.md` and verify it has no machine-absolute paths;
if it hard-codes `C:\Users\gethi\...`, replace with repo-relative wording
(`the better-memory checkout's .venv python`). It becomes the symlink source
like the other two skills.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/setup/test_engine_apply.py tests/setup/test_engine.py -q` — Expected: PASS
Run: `uv run ruff check better_memory/setup tests/setup` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/setup/engine.py tests/setup/test_engine_apply.py .claude/skills/start-better-memory-ui
git commit -m "feat(setup): engine apply with backups, lock, concern-3 retry"
```

---

### Task 5: Golden parity fixture + test (confidence 93% after lift pass)

**Privacy constraint (repo is PUBLIC):** fixtures must NOT contain the
user's full private CLAUDE.md nor the real Windows username. The capture
script below therefore (a) extracts only the managed-relevant subset,
(b) rewrites `gethi` → `ref` in all paths, and (c) trims CLAUDE.md to the
managed section plus one foreign heading on each side (enough to prove the
splice sits among foreign content).

**Files:**
- Create: `tests/setup/fixtures/reference_settings.json`, `tests/setup/fixtures/reference_claude.json`, `tests/setup/fixtures/reference_claude_md.md`, `tests/setup/test_golden_parity.py`

**Interfaces:**
- Consumes: Task 3/4 engine functions.
- Produces: regression anchor only.

- [ ] **Step 1: Capture fixtures from the reference machine**

Run (from repo root, on the reference PC):

```bash
uv run python - <<'EOF'
import json, pathlib
home = pathlib.Path.home()
out = pathlib.Path("tests/setup/fixtures")
out.mkdir(parents=True, exist_ok=True)

def sanitize(text: str) -> str:
    # Public repo: neutralize the local Windows username in embedded paths.
    return text.replace("\\\\gethi\\\\", "\\\\ref\\\\").replace("/gethi/", "/ref/").replace("\\gethi\\", "\\ref\\")

settings = json.loads((home/".claude"/"settings.json").read_text(encoding="utf-8"))
# Managed-relevant subset: hooks + env. Other keys stay out of the fixture.
subset = {"env": settings.get("env", {}), "hooks": settings.get("hooks", {})}
(out/"reference_settings.json").write_text(
    sanitize(json.dumps(subset, indent=2)) + "\n", encoding="utf-8")

claude = json.loads((home/".claude.json").read_text(encoding="utf-8"))
mcp = {"mcpServers": {"better-memory": claude["mcpServers"]["better-memory"]}}
(out/"reference_claude.json").write_text(
    sanitize(json.dumps(mcp, indent=2)) + "\n", encoding="utf-8")

md = (home/".claude"/"CLAUDE.md").read_text(encoding="utf-8")
# Trim to: the heading immediately before the managed section, the managed
# section itself, and the first foreign heading after it (structure proof
# without publishing the user's whole private file).
start = md.index("# better-memory (MANDATORY)")
end = md.index("# Process Discipline")
trimmed = "# Global Preferences\n\n(foreign content elided)\n\n" + md[start:end] + "# Process Discipline\n\n(foreign content elided)\n"
(out/"reference_claude_md.md").write_text(trimmed, encoding="utf-8")
EOF
```

Inspect the three fixtures before committing. `reference_settings.json`
must contain the 7 hook events and the `BETTER_MEMORY_INJECT_MODE` env; no
tokens, keys, credentials, and no occurrence of the real username (grep for
`gethi` must return nothing across `tests/setup/fixtures/`). The CLAUDE.md
fixture is the trimmed managed section between two foreign headings.

- [ ] **Step 2: Write the failing test**

`tests/setup/test_golden_parity.py`:

```python
"""Golden parity: the manifest reproduces the reference machine's wiring.

The fixtures are the managed-relevant subset of the reference PC's live
config. Applying the manifest with that machine's params must change
NOTHING semantically: same hook modules per event, same env key, same MCP
entry, and a CLAUDE.md whose managed block equals the packaged canonical
text. Guards the 'runs as well as it does on this pc' requirement.
"""
import json
from pathlib import Path

from better_memory.setup.engine import (
    extract_managed_block,
    merge_settings,
    patch_mcp_entry,
    splice_managed_block,
)
from better_memory.setup.manifest import CLAUDE_MD_BLOCK, MachineParams

FIXTURES = Path(__file__).parent / "fixtures"
REF_PARAMS = MachineParams(
    venv_py=r"C:\Users\ref\source\better-memory\.venv\Scripts\python.exe",
    venv_pyw=r"C:\Users\ref\source\better-memory\.venv\Scripts\pythonw.exe",
    home=r"C:\Users\ref\.better-memory",
    repo_root=r"C:\Users\ref\source\better-memory",
)


def _modules_by_event(hooks: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for event, groups in hooks.items():
        for group in groups:
            for h in group.get("hooks", []):
                result.setdefault(event, set()).add(h["command"])
    return result


def test_settings_parity_same_commands_per_event():
    ref = json.loads((FIXTURES / "reference_settings.json").read_text("utf-8"))
    merged = merge_settings(json.loads(json.dumps(ref)), REF_PARAMS)
    ref_cmds = _modules_by_event(ref["hooks"])
    new_cmds = _modules_by_event(merged["hooks"])
    # Same events; same better_memory modules per event. The three
    # hand-added entries (loose script + two echoes) are replaced by module
    # equivalents, so compare on module-name level for those.
    assert set(new_cmds) == set(ref_cmds)
    for event in ref_cmds:
        ref_modules = {c for c in ref_cmds[event] if "better_memory" in c}
        new_modules = {c for c in new_cmds[event] if "better_memory" in c}
        assert ref_modules <= new_modules, event
    assert merged["env"]["BETTER_MEMORY_INJECT_MODE"] == "deferred"


def test_mcp_parity_env_preserved():
    ref = json.loads((FIXTURES / "reference_claude.json").read_text("utf-8"))
    patched = patch_mcp_entry(json.loads(json.dumps(ref)), REF_PARAMS)
    entry = patched["mcpServers"]["better-memory"]
    ref_env = ref["mcpServers"]["better-memory"]["env"]
    for key, value in ref_env.items():
        assert entry["env"][key] == value
    assert entry["command"] == REF_PARAMS.venv_py


def test_claude_md_canonical_matches_reference_section():
    ref_md = (FIXTURES / "reference_claude_md.md").read_text("utf-8")
    spliced = splice_managed_block(ref_md, CLAUDE_MD_BLOCK)
    assert extract_managed_block(spliced) == CLAUDE_MD_BLOCK
    # The canonical block's protocol text must equal what the reference
    # machine actually runs with (whitespace-insensitive).
    assert "# better-memory (MANDATORY)" in ref_md
    normalized_ref = " ".join(ref_md.split())
    normalized_block = " ".join(CLAUDE_MD_BLOCK.split())
    assert normalized_block in normalized_ref
```

- [ ] **Step 3: Run, fix drift, pass**

Run: `uv run pytest tests/setup/test_golden_parity.py -q`
If `test_claude_md_canonical_matches_reference_section` fails, the
`CLAUDE_MD_BLOCK` capture in Task 1 was inexact — fix the constant, not the
test. Expected end state: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/setup/fixtures tests/setup/test_golden_parity.py
git commit -m "test(setup): golden parity against reference machine wiring"
```

---

### Task 6: Per-repo post-commit installer (confidence 92%)

**Files:**
- Create: `better_memory/setup/repo_hook.py`
- Test: `tests/setup/test_repo_hook.py`

**Interfaces:**
- Consumes: Task 1's `MachineParams`.
- Produces: `ensure_post_commit(repo_root: Path, params: MachineParams) -> str | None` — returns a one-line action/warning message, or `None` when nothing to do (not a repo, already installed).

**Spec rules implemented (concern 5):** honor `core.hooksPath` when set and writable; chain only plain sh scripts; never edit unparseable hooks; never overwrite.

- [ ] **Step 1: Write the failing tests**

`tests/setup/test_repo_hook.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/setup/test_repo_hook.py -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Implement `repo_hook.py`**

```python
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
    hooks = git_dir / "hooks" if git_dir.is_dir() else None
    return hooks


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/setup/test_repo_hook.py -q` — Expected: PASS
Run: `uv run ruff check better_memory/setup tests/setup` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/setup/repo_hook.py tests/setup/test_repo_hook.py
git commit -m "feat(setup): per-repo post-commit auto-installer"
```

---

### Task 7: CLI subcommands + install_hooks shim (confidence 93%)

**Files:**
- Create: `better_memory/cli/setup_cmd.py`
- Modify: `better_memory/cli/main.py`, `better_memory/cli/install_hooks.py`
- Test: `tests/cli/test_setup_cmd.py` (create `tests/cli/__init__.py` if absent)

**Interfaces:**
- Consumes: `engine.apply/diff/default_target_paths/fingerprint`, `manifest.detect_machine_params`, `autocheck` NOT yet (Task 8).
- Produces:
  - `setup_cmd.add_setup_parser(subparsers)` and `add_doctor_parser(subparsers)`; `handle_setup(args) -> int`; `handle_doctor(args) -> int`
  - `better-memory setup` — full apply + home-layout creation + default `settings.json` (`{"storage_backend": "sqlite"}`) only when the file is absent. Exit 0.
  - `better-memory doctor [--fix] [--json]` — report drift; `--fix` applies. Exit 0 clean / 1 drift found (in report mode).

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_setup_cmd.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_setup_cmd.py -q`
Expected: FAIL — argparse error `invalid choice: 'setup'`

- [ ] **Step 3: Implement**

`cli/setup_cmd.py`: thin orchestration —

```python
"""`better-memory setup` and `better-memory doctor` subcommands."""
from __future__ import annotations

import json
from pathlib import Path

from better_memory.setup import engine, manifest


def add_setup_parser(subparsers) -> None:
    subparsers.add_parser("setup", help="Install/repair all Claude Code wiring.")


def add_doctor_parser(subparsers) -> None:
    p = subparsers.add_parser("doctor", help="Check wiring drift; --fix repairs.")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")


def _home_layout(home: Path) -> None:
    for sub in ("spool", "state", "install-backups",
                "knowledge-base/standards", "knowledge-base/languages",
                "knowledge-base/projects"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    if not settings.exists():
        settings.write_text('{"storage_backend": "sqlite"}\n', encoding="utf-8")


def handle_setup(args) -> int:
    params = manifest.detect_machine_params()
    home = Path(params.home)
    _home_layout(home)
    paths = engine.default_target_paths()
    report = engine.apply(params, paths, home=home)
    for line in report.repaired:
        print(f"  OK {line}")
    for line in report.warnings:
        print(f"  WARN {line}")
    print("[better-memory setup] Done. Restart Claude Code to load changes.")
    return 0


def handle_doctor(args) -> int:
    params = manifest.detect_machine_params()
    paths = engine.default_target_paths()
    if args.fix:
        home = Path(params.home)
        report = engine.apply(params, paths, home=home)
        for line in report.repaired:
            print(f"  FIXED {line}")
        for line in report.warnings:
            print(f"  WARN {line}")
        return 0
    drift = engine.diff(params, paths)
    if args.as_json:
        print(json.dumps({"drift": drift}))
    elif drift:
        print(f"[better-memory doctor] {len(drift)} drift item(s):")
        for line in drift:
            print(f"  DRIFT {line}")
    else:
        print("[better-memory doctor] wiring clean")
    return 1 if drift else 0
```

`cli/main.py`: in `_build_parser()` after the agentcore block add
`from better_memory.cli import setup_cmd` then
`setup_cmd.add_setup_parser(subparsers)` and
`setup_cmd.add_doctor_parser(subparsers)`; in `main()` add:

```python
    if args.command == "setup":
        from better_memory.cli import setup_cmd
        return setup_cmd.handle_setup(args)
    if args.command == "doctor":
        from better_memory.cli import setup_cmd
        return setup_cmd.handle_doctor(args)
```

`cli/install_hooks.py`: replace the entire module body with a shim (module
docstring updated per docs-sync guardrail; keep the same CLI arguments so
old docs/scripts don't break):

```python
"""DEPRECATED: superseded by `better-memory setup` (better_memory/cli/setup_cmd.py).

Kept as a thin shim: parses the legacy flags, ignores them (machine params
are now auto-detected), and delegates to the setup engine.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="better_memory.cli.install_hooks")
    parser.add_argument("--venv-py")
    parser.add_argument("--venv-pyw")
    parser.add_argument("--home")
    parser.parse_args(argv)
    print("[install_hooks] DEPRECATED — running `better-memory setup` instead.",
          file=sys.stderr)
    from better_memory.cli.setup_cmd import handle_setup
    sys.exit(handle_setup(argparse.Namespace()))


if __name__ == "__main__":
    main()
```

Also DELETE the tests that exercised the old module internals if they break
(`grep -l install_hooks tests/`) and port any still-relevant merge cases to
`tests/setup/test_engine.py` (most already are, via Task 3).

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -q` — Expected: PASS (fix any install_hooks test fallout)
Run: `uv run ruff check .` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli tests/cli tests/setup
git commit -m "feat(cli): better-memory setup + doctor; install_hooks shim"
```

---

### Task 8: Bootstrap auto-check + sentinel retirement (confidence 92% after lift pass)

**Lift-pass fix (real bug):** the first draft cached the fingerprint even
when `apply()` returned warnings (e.g. malformed `~/.claude.json` left in
place). Next session the mtime+fingerprint cache would hit and the drift
would never be reported again — a one-shot warning for a persistent
problem. Rule now: NEVER write the fingerprint cache when
`report.warnings` is non-empty; the warning then re-surfaces every session
until the underlying file is fixed.

**Files:**
- Create: `better_memory/setup/autocheck.py`
- Modify: `better_memory/hooks/session_bootstrap.py`
- Delete: `better_memory/hooks/_claude_md_sentinel.py`, `tests/hooks/test_claude_md_sentinel.py`
- Test: `tests/setup/test_autocheck.py`; extend `tests/hooks/test_session_bootstrap.py`

**Interfaces:**
- Consumes: `engine.fingerprint/diff/apply/default_target_paths`, `manifest.detect_machine_params`, `repo_hook.ensure_post_commit`.
- Produces: `autocheck.maybe_repair(home: Path, cwd: Path) -> str | None` — returns the report line to append to bootstrap context, or `None` when clean/skipped.

**Sub-90% risk mitigated in-task:** a repair bug hitting every session start. Mitigations: `BETTER_MEMORY_WIRING_AUTOCHECK=off` kill switch (env, documented in module docstring AND Task 10 docs); mtime+fingerprint short-circuit; whole call wrapped in the bootstrap's existing `try/except BaseException`.

- [ ] **Step 1: Write the failing tests**

`tests/setup/test_autocheck.py`:

```python
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
```

Add to `tests/hooks/test_session_bootstrap.py` (following its existing
monkeypatch style): a test asserting the bootstrap output contains the
autocheck report line when `autocheck.maybe_repair` is monkeypatched to
return `"better-memory doctor: repaired 1 item(s)..."`, and a test that a
raising `maybe_repair` still produces normal bootstrap output.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/setup/test_autocheck.py -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Implement `autocheck.py`**

```python
"""Per-session wiring drift check with mtime+fingerprint short-circuit.

Called from hooks.session_bootstrap. Near-zero cost on the happy path:
state/wiring_fingerprint.json stores the manifest fingerprint plus the
mtimes of the target files; when nothing moved, no diff runs. Kill switch:
BETTER_MEMORY_WIRING_AUTOCHECK=off (add to website/configuration.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from better_memory.setup import engine, manifest, repo_hook

_STATE_NAME = "wiring_fingerprint.json"


def _mtimes(paths: engine.TargetPaths) -> dict[str, float]:
    result = {}
    for p in (paths.claude_json, paths.settings_json, paths.claude_md):
        result[str(p)] = p.stat().st_mtime if p.exists() else 0.0
    return result


def maybe_repair(home: Path, cwd: Path) -> str | None:
    if os.environ.get("BETTER_MEMORY_WIRING_AUTOCHECK", "").lower() == "off":
        return None
    params = manifest.detect_machine_params(home=str(home))
    paths = engine.default_target_paths()
    fp = engine.fingerprint(params)
    state_path = home / "state" / _STATE_NAME
    current = {"fingerprint": fp, "mtimes": _mtimes(paths)}
    try:
        cached = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    repo_msg = repo_hook.ensure_post_commit(cwd, params)
    if cached == current and repo_msg is None:
        return None
    messages: list[str] = [repo_msg] if repo_msg else []
    warnings: list[str] = []
    drift = engine.diff(params, paths)
    if drift:
        report = engine.apply(params, paths, home=home)
        if report.repaired:
            messages.append(
                f"better-memory doctor: repaired {len(report.repaired)} "
                f"item(s): {'; '.join(report.repaired)} (effective next session)"
            )
        warnings = report.warnings
        messages.extend(f"better-memory doctor: WARN {w}" for w in warnings)
    if not warnings:
        # Cache ONLY a clean outcome. A cached fingerprint after warnings
        # would silence a persistent problem after one report (lift-pass fix).
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"fingerprint": fp, "mtimes": _mtimes(paths)}),
            encoding="utf-8",
        )
    return " | ".join(messages) if messages else None
```

`hooks/session_bootstrap.py`: replace the sentinel block (lines 125-140)
with:

```python
        try:
            from better_memory.setup.autocheck import maybe_repair

            autocheck_line = maybe_repair(cfg.home, Path(cwd_str))
            if autocheck_line:
                rendered = rendered + "\n\n" + autocheck_line
        except BaseException:  # noqa: BLE001 — autocheck is best-effort
            pass
```

Update the module docstring's last sentence to mention the wiring
autocheck. Delete `hooks/_claude_md_sentinel.py` and its test.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/setup tests/hooks -q` — Expected: PASS
Run: `uv run ruff check .` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add better_memory/setup/autocheck.py better_memory/hooks/session_bootstrap.py tests/setup/test_autocheck.py tests/hooks/test_session_bootstrap.py
git rm better_memory/hooks/_claude_md_sentinel.py tests/hooks/test_claude_md_sentinel.py
git commit -m "feat(setup): session-start wiring autocheck; retire CLAUDE.md sentinel"
```

---

### Task 9: Bootstrap scripts (confidence 92% after lift pass)

**Files:**
- Create: `scripts/setup.ps1`
- Modify: `scripts/setup.sh`

**Interfaces:**
- Consumes: `better-memory setup` CLI (Task 7).
- Produces: operator entry points only.

- [ ] **Step 1: Write `scripts/setup.ps1`**

```powershell
# better-memory Windows bootstrap: uv -> deps -> wiring. Idempotent.
# Runnable from anywhere: cds to the repo root itself (lift-pass fix — the
# first draft assumed the caller's cwd was the repo root).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[setup] uv not found - installing..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        powershell -ExecutionPolicy ByPass -NoProfile -Command `
            "irm https://astral.sh/uv/install.ps1 | iex"
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + `
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "[setup] uv installed but not on PATH - open a new shell and re-run."
        exit 1
    }
}

Write-Host "[setup] Syncing dependencies..."
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[setup] Installing Claude Code wiring..."
uv run better-memory setup
exit $LASTEXITCODE
```

- [ ] **Step 2: Rewrite `scripts/setup.sh`**

Replace the whole body with the three steps (keep the shebang and platform
detect only if trivially reusable). Delete: the Ollama detect/install/pull
section, the venv interpreter detection, and the `install_hooks` invocation:

```bash
#!/usr/bin/env bash
# better-memory POSIX bootstrap: uv -> deps -> wiring. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "[setup] uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "[setup] Syncing dependencies..."
uv sync

echo "[setup] Installing Claude Code wiring..."
uv run better-memory setup
```

- [ ] **Step 3: Verify**

Run: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` on the
reference machine. Expected output ends with
`[better-memory setup] Done. Restart Claude Code to load changes.` and a
follow-up `uv run better-memory doctor` prints `wiring clean` (exit 0).
Run: `bash -n scripts/setup.sh` — Expected: no syntax errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/setup.ps1 scripts/setup.sh
git commit -m "feat(scripts): setup.ps1 with uv bootstrap; slim setup.sh, drop ollama"
```

---

### Task 10: Documentation sweep (confidence 95%)

**Files:**
- Modify: `README.md`, `website/configuration.md`, `website/architecture.md`, `website/index.md`, `docs/hooks-setup.md`

**Interfaces:** none — prose only, but every claim verified against the code landed in Tasks 1-9 (docs-sync guardrail: copy CLI flags from the argparse source in `cli/setup_cmd.py`, not from this plan).

- [ ] **Step 1: README.md**

- Quick start: replace the `setup.sh` + manual steps with the two bootstrap
  scripts (`scripts/setup.ps1` for Windows, `scripts/setup.sh` for POSIX)
  and the one-command story; show interactive and scripted paths separately.
- Remove Ollama from Requirements/prerequisites.
- Add a `## Doctor` subsection: `better-memory doctor` / `--fix` / `--json`,
  exit codes, the session-start autocheck, and the repo-move recovery
  sentence (run doctor --fix from the new location).
- Env-var table: add `BETTER_MEMORY_WIRING_AUTOCHECK` (default on; `off`
  disables).
- Note `install_hooks` deprecation.

- [ ] **Step 2: website/configuration.md**

- Add `BETTER_MEMORY_WIRING_AUTOCHECK` to the env table.
- State-layout section: add `state/wiring_fingerprint.json`,
  `state/setup-apply.lock`, and note `install-backups/` now also receives
  doctor-repair backups.

- [ ] **Step 3: website/architecture.md + website/index.md**

- Architecture: add a short "Self-managing wiring" paragraph (manifest →
  engine → setup/doctor/autocheck), mention sentinel retirement.
- index.md: only touch if it names the sentinel or manual setup; verify
  tool counts unchanged (no MCP tools added/removed by this work).

- [ ] **Step 4: docs/hooks-setup.md**

- Replace the manual hook-JSON walkthrough with: run `better-memory setup`;
  keep the JSON shapes as reference material clearly labeled "managed
  automatically — reference only".
- Replace the manual post-commit section (`:171-232`) with the auto-install
  description and the concern-5 skip rules.

- [ ] **Step 5: Verify + commit**

Cross-read each edited doc against `cli/setup_cmd.py` argparse and
`autocheck.py` env-var read (exact flag and var names). Run:
`uv run pytest -q` (full suite, final green).

```bash
git add README.md website docs/hooks-setup.md
git commit -m "docs: self-managing setup, doctor, autocheck; drop ollama and manual steps"
```

---

## Verification (whole-branch, after Task 10)

1. `uv run pytest -q` — full suite green.
2. `uv run ruff check .` — clean.
3. On the reference machine: `uv run better-memory doctor` → expect a small
   drift list (loose script + echo hooks being replaced by module entries);
   `uv run better-memory doctor --fix`; restart a Claude session; confirm
   memories inject, the commit checkpoint fires on a `git commit`, and
   `doctor` now reports `wiring clean`.
4. Golden parity test guards that step 3's repair changed nothing
   semantically.

## Self-review record

- Spec coverage: rows 1-12 → Tasks 1,3,4; row 13 → Tasks 6,8; concerns 1-6
  → Tasks 6/8 (1,5), 2 (2), 4 (3), 9+10 (4,6), CLI+docs (4). Ollama removal
  → Task 9. Docs → Task 10. Sentinel retirement → Task 8. install_hooks
  shim → Task 7. Golden parity → Task 5. No uncovered spec section.
- Placeholders: none (all code steps carry code; fixture capture is an
  exact executable command).
- Type consistency: `MachineParams`/`TargetPaths`/`ApplyReport` names and
  fields identical across Tasks 1-8; `maybe_repair(home, cwd)` signature
  matches the bootstrap call site.
