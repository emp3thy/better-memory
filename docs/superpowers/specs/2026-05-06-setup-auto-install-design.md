# Setup-script auto-install — design

**Status:** Approved 2026-05-06
**Branch target:** new feature branch off `main` (e.g. `cli/install-hooks`)
**Predecessor:** PR #46 (`session_retrieve` SessionStart hook). Track B installs that hook + the three pre-existing ones (`session_start`, `observer`, `session_close`) plus the MCP server registration.

## Goal

Replace `scripts/setup.sh`'s "print snippets, ask user to paste" tail block with an automated install of better-memory's MCP server (into `~/.claude.json`) and four hooks (into `~/.claude/settings.json`). Idempotent — running setup twice produces the same end state. Smart-merge — preserves user customizations like a non-default `BETTER_MEMORY_HOME` env override.

## Why now

Track A landed `session_retrieve` as a SessionStart hook that ELIMINATES the "Claude skips memory_retrieve on first turn" failure mode — but only if the hook is registered in `~/.claude/settings.json`. Without auto-install, every install (or re-install on a new machine) requires a manual JSON paste. The user's framing was explicit when commissioning Track A: *"can I set it up so that we install the hook when we set up the MCP?"* — auto-install is the operationalization of that ask.

Beyond convenience, auto-install also makes the docs-vs-reality drift problem self-correcting: the canonical hook layout becomes whatever `_OUR_HOOKS` declares, not whatever the README example happens to say.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Scope | Both `~/.claude.json` (MCP server registration) and `~/.claude/settings.json` (4 hooks) | "Install the hook when we set up the MCP" requires both registrations to land for a working install. |
| Implementation language | Python module (`better_memory/cli/install_hooks.py`) called from `setup.sh` | JSON merging in bash without `jq` is painful; `jq` isn't reliably present on Windows Git Bash. Python is already a prereq for better-memory; the `.venv` is already primed by the time we hit the install step. Testable via pytest. |
| Idempotency / conflict | Smart merge — user wins on values; installer wins on adding-missing-fields and on path/command refresh | Predictable, preserves customizations. A user who set `BETTER_MEMORY_HOME` to a non-default path doesn't lose it on re-run. |
| Hook merge strategy | Remove-then-add with canonical placement | Two deterministic passes (much simpler than edit-in-place). Hook layout is infrastructure-managed; user shouldn't customize where our hooks live. Their other (non-better-memory) hooks remain untouched. |
| Single source of truth | `_OUR_HOOKS` registry — tuple of `HookSpec` dataclass instances | Adding/removing/renaming a hook is a one-line edit; the merge logic doesn't know specific module names. |
| Backup | Centralized at `$BETTER_MEMORY_HOME/install-backups/{filename}.{YYYYMMDD-HHMMSS}.bak` | Clean separation from `~/.claude/`, full history, no clutter. Audit trail for rollback. |
| Flags | None — always auto-install | Single-user tool currently; opt-out flags add complexity for no payoff. Future multi-user releases can add `--dry-run` / `--print-snippets` if needed. |
| File-state handling | Pragmatic — missing file → create as `{}`; malformed JSON → refuse with `path:line: error` | Missing files are normal (fresh Claude Code install may not have customized `settings.json` yet). Malformed JSON is user error; we shouldn't try to be clever about repair. |
| JSON formatting | Accept normalization (write via `json.dumps(..., indent=2)`) | Single-user / no-git on these files; trivia-preserving JSON parsers add a dep without payoff. |
| Atomicity | `os.replace` after writing to `.tmp` sibling | POSIX-atomic on Linux/macOS; atomic-on-existing-target on Windows. The microsecond window where both files exist (Windows missing-target case) is acceptable. |
| Console-flash | All 4 hooks use `pythonw.exe` on Windows; `python.exe` on Linux/macOS (same binary) | Mirrors existing observer/session_close convention in the current setup.sh. Smoke test post-install confirms `pythonw` pipes stdout for the new SessionStart hooks. |

## Approach

Single branch off `main`, three logical commits:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `feat(cli): install_hooks module with pure-function merge logic` | `better_memory/cli/__init__.py` (new), `better_memory/cli/install_hooks.py` (new), `tests/cli/test_install_hooks.py` (new — pure-function tests for the two merge functions, plus `_load_or_empty`, `_atomic_write`, `_backup`) | Feature |
| 2 | `feat(cli): install_hooks CLI orchestration + integration tests` | `better_memory/cli/install_hooks.py` (extend), `tests/cli/test_install_hooks.py` (extend with `TestCLIIntegration`) | Feature |
| 3 | `feat(setup): replace print-snippets block with install_hooks shell-out` | `scripts/setup.sh`, `README.md` (Manual setup section adjustment to mention `setup.sh` does this for you), `website/configuration.md` (cross-reference update) | Feature + docs |

CI green at each commit boundary.

## Commit 1 — Pure-function merge logic

### Module skeleton

`better_memory/cli/__init__.py` — empty package marker.

`better_memory/cli/install_hooks.py`:

```python
"""Auto-installer for better-memory's MCP server registration + hooks.

Invoked by ``scripts/setup.sh`` after the filesystem-layout step. Merges the
canonical entries into ``~/.claude.json`` (MCP server) and
``~/.claude/settings.json`` (4 hooks). Idempotent: running twice produces
the same end state. Smart-merge: user's customizations (custom env values,
non-better-memory hooks) are preserved.

Public CLI: ``python -m better_memory.cli.install_hooks --venv-py X
--venv-pyw Y --home Z``. Reads/writes the user's home; the bash caller
already resolved the right paths per platform.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

# --------------------------------------------------------------- hook registry

@dataclass(frozen=True)
class HookSpec:
    module: str         # e.g. "better_memory.hooks.session_start"
    event: str          # "SessionStart" | "PostToolUse" | "Stop"
    matcher: str | None # None for SessionStart/Stop, "Write|Edit|Bash" for observer
    is_async: bool      # True for PostToolUse + Stop, False for SessionStart


_OUR_HOOKS: tuple[HookSpec, ...] = (
    HookSpec("better_memory.hooks.session_start",    "SessionStart", None,              False),
    HookSpec("better_memory.hooks.session_retrieve", "SessionStart", None,              False),
    HookSpec("better_memory.hooks.observer",         "PostToolUse",  "Write|Edit|Bash", True),
    HookSpec("better_memory.hooks.session_close",    "Stop",         None,              True),
)


# ------------------------------------------------------------- pure functions

def merge_claude_json(existing: dict, *, command: str, home: str) -> dict:
    """Smart-merge the better-memory MCP server into ~/.claude.json.

    Preserves user's custom ``env`` values; ensures ``BETTER_MEMORY_HOME`` is
    set if absent. Refreshes ``command``/``args`` to current paths. Other
    keys in ``mcpServers`` (other servers, other top-level config) are
    untouched.
    """
    config = dict(existing)
    mcp_servers = dict(config.get("mcpServers", {}))
    existing_bm = mcp_servers.get("better-memory", {})

    merged = {
        "type": "stdio",
        "command": command,
        "args": ["-m", "better_memory.mcp"],
    }
    if "env" in existing_bm:
        env = dict(existing_bm["env"])
        env.setdefault("BETTER_MEMORY_HOME", home)
        merged["env"] = env
    else:
        merged["env"] = {"BETTER_MEMORY_HOME": home}

    mcp_servers["better-memory"] = merged
    config["mcpServers"] = mcp_servers
    return config


def merge_settings_json(existing: dict, *, venv_pyw: str) -> dict:
    """Smart-merge the four hook entries into ~/.claude/settings.json.

    Two-pass: REMOVE existing better-memory hook entries (matching by
    ``command`` substring against module names in _OUR_HOOKS), then ADD
    canonical matcher-groups at the end of each event's array. User's
    other hooks and matcher-groups are untouched.
    """
    config = dict(existing)
    hooks = dict(config.get("hooks", {}))

    our_module_paths = {spec.module for spec in _OUR_HOOKS}

    # Pass 1: REMOVE
    for event_name in list(hooks.keys()):
        groups = []
        for group in hooks[event_name]:
            kept_hooks = [
                h for h in group.get("hooks", [])
                if not any(mp in h.get("command", "") for mp in our_module_paths)
            ]
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                groups.append(new_group)
            # else: matcher-group is now empty after removal; drop it
        hooks[event_name] = groups

    # Pass 2: ADD canonical groups
    # SessionStart: pair the two hooks in one matcher-group.
    session_start_specs = [s for s in _OUR_HOOKS if s.event == "SessionStart"]
    if session_start_specs:
        hooks.setdefault("SessionStart", []).append({
            "hooks": [
                _hook_entry(spec, venv_pyw) for spec in session_start_specs
            ],
        })

    # PostToolUse: each spec in its own matcher-group.
    for spec in (s for s in _OUR_HOOKS if s.event == "PostToolUse"):
        group = {"hooks": [_hook_entry(spec, venv_pyw)]}
        if spec.matcher is not None:
            group["matcher"] = spec.matcher
        hooks.setdefault("PostToolUse", []).append(group)

    # Stop: each spec in its own matcher-group, no matcher.
    for spec in (s for s in _OUR_HOOKS if s.event == "Stop"):
        hooks.setdefault("Stop", []).append({
            "hooks": [_hook_entry(spec, venv_pyw)],
        })

    config["hooks"] = hooks
    return config


def _hook_entry(spec: HookSpec, venv_pyw: str) -> dict:
    """Build the JSON object for a single hook entry."""
    entry = {
        "type": "command",
        "command": f'"{venv_pyw}" -m {spec.module}',
    }
    if spec.is_async:
        entry["async"] = True
    return entry
```

### `_load_or_empty`, `_atomic_write`, `_backup`

```python
def _load_or_empty(path: Path) -> dict:
    """Read JSON from path. Missing → {}. Malformed → exits with line# message."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(
            f"[install_hooks] {path}:{e.lineno}: {e.msg}\n"
            f"[install_hooks] Fix the file then re-run scripts/setup.sh.",
            file=sys.stderr,
        )
        sys.exit(1)


def _atomic_write(path: Path, content: str) -> None:
    """Write to path.tmp then os.replace. Caller is responsible for backups."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _backup(src: Path, dst_dir: Path, *, clock=None) -> Path | None:
    """Copy src into dst_dir with timestamp suffix. Returns the backup path,
    or None if src didn't exist (no backup needed)."""
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    now = (clock or datetime.now)()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    dst = dst_dir / f"{src.name}.{timestamp}.bak"
    shutil.copy2(src, dst)
    return dst
```

### Test seeds

`tests/cli/test_install_hooks.py` — 6 test classes, 25 tests total. Commit 1 ships the first 5 classes (21 tests, all pure-function or low-level I/O); Commit 2 adds `TestCLIIntegration` (4 tests).

**TestMergeClaudeJson** (5):
- `test_empty_config_adds_entry` — `{}` input → `{"mcpServers": {"better-memory": {...}}}`.
- `test_existing_user_env_preserved` — input has `mcpServers.better-memory.env = {"FOO": "bar"}` → output's env has `FOO=bar` AND `BETTER_MEMORY_HOME=<home>`.
- `test_user_custom_BETTER_MEMORY_HOME_wins` — input has user-set `BETTER_MEMORY_HOME=/custom/path` → output preserves `/custom/path`, doesn't overwrite to default.
- `test_command_path_refreshed_on_rerun` — input has `command="/old/python"` → output has the new command from kwargs.
- `test_other_mcp_servers_untouched` — input has `mcpServers.someother = {...}` → present and unchanged in output.

**TestMergeSettingsJson** (8):
- `test_empty_hooks_adds_all_four` — `{}` input → output has SessionStart pair + PostToolUse observer + Stop session_close.
- `test_existing_user_postooluse_preserved` — input has user's `PostToolUse: [{"matcher":"Bash","hooks":[{"command":"echo hi"}]}]` → output preserves that group AND adds our observer.
- `test_stale_better_memory_paths_refreshed` — input has `better_memory.hooks.observer` with old path → removed, fresh canonical group added.
- `test_mixed_matcher_group_user_preserved` — input has matcher-group containing one user hook + one better-memory hook → ours stripped, user's preserved in original group.
- `test_empty_matcher_groups_pruned` — input has matcher-group with ONLY our hook → group removed entirely after pass 1.
- `test_session_start_pair_shares_matcher_group` — output's `SessionStart` array has exactly one matcher-group containing both `session_start` and `session_retrieve` entries.
- `test_user_session_start_hook_preserved` — input has user's own `SessionStart` matcher-group → preserved.
- `test_idempotent_second_run_is_noop` — running merge twice on same input produces identical output.

**TestLoadOrEmpty** (3):
- `test_missing_file_returns_empty_dict` — `tmp_path / "nonexistent.json"` → `{}`.
- `test_valid_json_returns_parsed` — write `{"a": 1}`, load → `{"a": 1}`.
- `test_malformed_json_refuses_with_line_number` — write `{not json}`, load → `SystemExit(1)`, stderr contains `:1:` (line number) and `Fix the file then re-run`.

**TestAtomicWrite** (3):
- `test_write_creates_missing_file` — target doesn't exist; `_atomic_write` creates it with content.
- `test_write_replaces_existing_content` — target exists with old content; `_atomic_write` replaces.
- `test_tmp_remains_when_replace_fails` — monkeypatch `os.replace` to raise; tmp file persists for forensics, original untouched.

**TestBackup** (2):
- `test_missing_source_no_backup` — src doesn't exist → returns None, no file created in dst_dir.
- `test_existing_source_copied_with_timestamp` — src exists; `_backup` returns path matching `<name>.YYYYMMDD-HHMMSS.bak`; bytes match src; src unchanged.

### Test fixture

```python
@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home() reads this
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / ".better-memory"))
    (tmp_path / ".better-memory" / "install-backups").mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    return tmp_path
```

Used by I/O tests; pure-function tests don't need it.

## Commit 2 — CLI orchestration + integration tests

### `main()` + argparse

```python
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="better_memory.cli.install_hooks")
    parser.add_argument("--venv-py",  required=True, help="Path to venv python (Linux/macOS) or python.exe (Windows). Used for the MCP server command.")
    parser.add_argument("--venv-pyw", required=True, help="Path to venv pythonw (Windows) or python (Linux/macOS). Used for hook commands.")
    parser.add_argument("--home",     required=True, help="Resolved $BETTER_MEMORY_HOME.")
    args = parser.parse_args(argv)

    home_dir = Path(args.home)
    backup_dir = home_dir / "install-backups"

    targets = [
        ("MCP server", Path.home() / ".claude.json", lambda d: merge_claude_json(d, command=args.venv_py, home=args.home)),
        ("hooks",      Path.home() / ".claude" / "settings.json", lambda d: merge_settings_json(d, venv_pyw=args.venv_pyw)),
    ]

    print("[install_hooks] Installing better-memory MCP server + hooks...")
    for label, path, merge in targets:
        existing = _load_or_empty(path)
        backup_path = _backup(path, backup_dir)
        merged = merge(existing)
        _atomic_write(path, json.dumps(merged, indent=2) + "\n")
        if backup_path:
            print(f"  ✓ {label}: {path} (backup: {backup_path.relative_to(home_dir)})")
        else:
            print(f"  ✓ {label}: {path} (created fresh)")
    print("[install_hooks] Restart Claude Code to load the new MCP server.")


if __name__ == "__main__":
    main()
```

### TestCLIIntegration (4 tests)

- `test_fresh_install_writes_both_files` — `mock_home` fresh; invoke `main(["--venv-py","/x","--venv-pyw","/y","--home",...])`; assert `~/.claude.json` and `~/.claude/settings.json` exist with expected content.
- `test_idempotent_rerun_is_clean` — call `main()` twice; assert second invocation produces identical content (file size + parsed-dict equality).
- `test_malformed_settings_refuses_without_writing` — pre-seed `~/.claude/settings.json` with `{not json}`; `main()` exits 1; the file is unchanged from pre-seed.
- `test_summary_lines_printed_on_success` — capture stdout; assert "Installing better-memory" + per-file ✓ lines + restart hint.

## Commit 3 — `setup.sh` integration + docs

### `setup.sh` change

Replace lines 196–264 (the `cat <<EOF ... EOF` print-snippets block) with:

```bash
# ---------------------------------------------------------------------------
# 6. Install MCP server + hooks into Claude Code config files
# ---------------------------------------------------------------------------

log "Installing into ~/.claude.json and ~/.claude/settings.json..."
(cd "$PROJECT_DIR" && uv run python -m better_memory.cli.install_hooks \
    --venv-py "$(win_path "$VENV_PY")" \
    --venv-pyw "$(win_path "$VENV_PYW")" \
    --home "$BETTER_MEMORY_HOME") || {
    error "install_hooks failed; see message above. setup.sh aborting."
    exit 1
}

log "Setup complete. Restart Claude Code to load the new MCP server."
```

### `README.md` change (Manual setup section)

The existing Manual setup section becomes optional rather than primary. Replace its prose:
- Was: "If you'd rather do it by hand: ..."
- Now: "`./scripts/setup.sh` writes both files for you. The JSON examples below are for reference if you need to edit by hand."

### `website/configuration.md` cross-reference

The `## Hooks` section (added in Track A's Task 8) gains a leading line: "Installed automatically by `./scripts/setup.sh`. The descriptions below are reference material if you need to inspect or hand-edit the config."

## Out of scope

Explicitly deferred:

- **`--dry-run` / `--print-snippets` flags** — single-user tool currently; no payoff for opt-out. Add when there are users who'd benefit.
- **Uninstall command** — only if requested.
- **Trivia-preserving JSON parsing** — accept `json.dumps(indent=2)` normalization. User's own formatting will be normalized on first install; subsequent installs are stable.
- **Cleanup of `CONSOLIDATE_MODEL` chat-model pull in `setup.sh:171–181`** — dead code per Track C audit (env var is never read). Out of scope for Track B; track separately.
- **Cross-platform smoke test of `pythonw.exe` SessionStart hook** — listed under Real Concern 3 mitigation; verified by user post-install on Windows. Not part of the unit/integration test matrix.

## Confidence per implementation step

| # | Step | Conf. | Notes |
|---|---|---|---|
| 1 | Module scaffolding (`cli/__init__.py`, argparse) | 95% | |
| 2 | `_load_or_empty` + malformed-JSON refusal | 95% | |
| 3 | `_atomic_write` (write tmp + `os.replace`) | 90% | Tested for failure-path tmp persistence. |
| 4 | `_backup` to install-backups with timestamp | 95% | |
| 5 | `_merge_claude_json` (env preservation) | 90% | Pure function; 5 tests pin behaviour. |
| 6 | `_merge_settings_json` (4 hooks, smart merge) | 92% | Iterated 80% → 92%: introduced `_OUR_HOOKS` registry, switched to remove-then-add pure-function strategy, prescribed canonical placement. 8 tests pin behaviour. |
| 7 | `setup.sh` shell-out integration | 90% | Single shell-out replacing the print block. |
| 8 | Test suite (25 scenarios, prescribed matrix) | 92% | Iterated 85% → 92%: prescribed exact 6-class matrix with named test methods (5 + 8 + 3 + 3 + 2 + 4). Implementer follows TDD over fixed list. |

All ≥90%. No mitigations required at plan-stage.

## References

- Track A spec: [`docs/superpowers/specs/2026-05-06-session-memory-injection-hook-design.md`](2026-05-06-session-memory-injection-hook-design.md) — defines the SessionStart hooks this installer registers.
- Existing setup script: `scripts/setup.sh:196-264` (the print-snippets block being replaced).
- Existing hook entry points used by `_OUR_HOOKS`:
  - `better_memory/hooks/session_start.py` — spool marker on SessionStart
  - `better_memory/hooks/session_retrieve.py` — memory injection on SessionStart (Track A, PR #46)
  - `better_memory/hooks/observer.py` — tool-call snapshots on PostToolUse
  - `better_memory/hooks/session_close.py` — close marker on Stop
- Claude Code hooks contract: `code.claude.com/docs/en/hooks-guide.md` (concatenation of `additionalContext` from multiple SessionStart hooks; matcher-group structure for PostToolUse).
