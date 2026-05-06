# Setup Auto-Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `scripts/setup.sh`'s "print snippets, ask user to paste" tail with a Python module that auto-installs better-memory's MCP server registration into `~/.claude.json` and the four hooks (`session_start`, `session_retrieve`, `observer`, `session_close`) into `~/.claude/settings.json`. Idempotent. Smart-merge — preserves user customizations.

**Architecture:** New Python module `better_memory/cli/install_hooks.py` invoked by `setup.sh` after the filesystem-layout step. Pure-function merge logic (`merge_claude_json`, `merge_settings_json`) wrapped by `main()` orchestration that reads/backs-up/writes the two target files. Hook registry (`_OUR_HOOKS`) is single-source-of-truth for hook metadata.

**Tech Stack:** Python 3.12 (stdlib only — `argparse`, `dataclasses`, `datetime`, `json`, `os`, `pathlib`, `shutil`), pytest, bash.

**Spec:** [`docs/superpowers/specs/2026-05-06-setup-auto-install-design.md`](../specs/2026-05-06-setup-auto-install-design.md)

**File structure:**
- `better_memory/cli/__init__.py` (new) — empty package marker
- `better_memory/cli/install_hooks.py` (new) — module + CLI entry
- `tests/cli/__init__.py` (new) — empty package marker
- `tests/cli/test_install_hooks.py` (new) — 25 tests across 6 classes
- `scripts/setup.sh` — replace lines 196–264 with shell-out
- `README.md` — Manual setup becomes reference-only
- `website/configuration.md` — Hooks section gains "installed automatically" lead

---

## Task 1: Module scaffold + `merge_claude_json` + 5 tests

**Confidence:** 95%
**Files:**
- Create: `better_memory/cli/__init__.py`
- Create: `better_memory/cli/install_hooks.py`
- Create: `tests/cli/__init__.py`
- Create: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Create empty package markers**

```bash
mkdir -p better_memory/cli tests/cli
touch better_memory/cli/__init__.py tests/cli/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/cli/test_install_hooks.py` with:

```python
"""Tests for ``better_memory.cli.install_hooks``.

Pure-function tests use no fs / network. I/O tests use ``tmp_path``.
"""

from __future__ import annotations

import pytest

from better_memory.cli.install_hooks import (
    HookSpec,
    _OUR_HOOKS,
    merge_claude_json,
)


class TestMergeClaudeJson:
    def test_empty_config_adds_entry(self) -> None:
        out = merge_claude_json(
            {}, command="/venv/bin/python", home="/home/u/.better-memory"
        )
        bm = out["mcpServers"]["better-memory"]
        assert bm["type"] == "stdio"
        assert bm["command"] == "/venv/bin/python"
        assert bm["args"] == ["-m", "better_memory.mcp"]
        assert bm["env"] == {"BETTER_MEMORY_HOME": "/home/u/.better-memory"}

    def test_existing_user_env_preserved(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "type": "stdio",
                    "command": "/old/python",
                    "args": ["-m", "better_memory.mcp"],
                    "env": {"FOO": "bar"},
                },
            },
        }
        out = merge_claude_json(
            existing, command="/new/python", home="/home/u/.better-memory"
        )
        env = out["mcpServers"]["better-memory"]["env"]
        assert env["FOO"] == "bar"
        assert env["BETTER_MEMORY_HOME"] == "/home/u/.better-memory"

    def test_user_custom_BETTER_MEMORY_HOME_wins(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "env": {"BETTER_MEMORY_HOME": "/custom/path"},
                },
            },
        }
        out = merge_claude_json(
            existing, command="/x/python", home="/default/path"
        )
        assert out["mcpServers"]["better-memory"]["env"]["BETTER_MEMORY_HOME"] == "/custom/path"

    def test_command_path_refreshed_on_rerun(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "type": "stdio",
                    "command": "/old/path/python",
                    "args": ["-m", "better_memory.mcp"],
                    "env": {"BETTER_MEMORY_HOME": "/h"},
                },
            },
        }
        out = merge_claude_json(existing, command="/new/path/python", home="/h")
        assert out["mcpServers"]["better-memory"]["command"] == "/new/path/python"

    def test_other_mcp_servers_untouched(self) -> None:
        existing = {
            "mcpServers": {
                "some-other-server": {"type": "stdio", "command": "/x"},
            },
        }
        out = merge_claude_json(existing, command="/p", home="/h")
        assert out["mcpServers"]["some-other-server"] == {"type": "stdio", "command": "/x"}
        assert "better-memory" in out["mcpServers"]


class TestHookSpec:
    """Sanity checks on the hook registry — pin the four expected entries."""

    def test_registry_has_four_entries(self) -> None:
        assert len(_OUR_HOOKS) == 4

    def test_session_start_pair_is_session_start_event_no_matcher(self) -> None:
        for spec in _OUR_HOOKS:
            if spec.module.endswith(("session_start", "session_retrieve")):
                assert spec.event == "SessionStart"
                assert spec.matcher is None
                assert spec.is_async is False

    def test_observer_is_post_tool_use_with_matcher(self) -> None:
        observer = next(s for s in _OUR_HOOKS if s.module.endswith("observer"))
        assert observer.event == "PostToolUse"
        assert observer.matcher == "Write|Edit|Bash"
        assert observer.is_async is True

    def test_session_close_is_stop_no_matcher_async(self) -> None:
        sc = next(s for s in _OUR_HOOKS if s.module.endswith("session_close"))
        assert sc.event == "Stop"
        assert sc.matcher is None
        assert sc.is_async is True
```

- [ ] **Step 3: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'better_memory.cli.install_hooks'` or similar.

- [ ] **Step 4: Write the implementation**

Create `better_memory/cli/install_hooks.py`:

```python
"""Auto-installer for better-memory's MCP server registration + hooks.

Invoked by ``scripts/setup.sh`` after the filesystem-layout step. Merges
canonical entries into ``~/.claude.json`` (MCP server) and
``~/.claude/settings.json`` (4 hooks). Idempotent: running twice produces
the same end state. Smart-merge: user's customizations (custom env values,
non-better-memory hooks) are preserved.

Public CLI: ``python -m better_memory.cli.install_hooks --venv-py X
--venv-pyw Y --home Z``.
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
from typing import Callable

# --------------------------------------------------------------- hook registry


@dataclass(frozen=True)
class HookSpec:
    module: str          # e.g. "better_memory.hooks.session_start"
    event: str           # "SessionStart" | "PostToolUse" | "Stop"
    matcher: str | None  # None for SessionStart/Stop; "Write|Edit|Bash" for observer
    is_async: bool       # True for PostToolUse + Stop


_OUR_HOOKS: tuple[HookSpec, ...] = (
    HookSpec("better_memory.hooks.session_start",    "SessionStart", None,              False),
    HookSpec("better_memory.hooks.session_retrieve", "SessionStart", None,              False),
    HookSpec("better_memory.hooks.observer",         "PostToolUse",  "Write|Edit|Bash", True),
    HookSpec("better_memory.hooks.session_close",    "Stop",         None,              True),
)


# ---------------------------------------------------------- pure merge: claude


def merge_claude_json(existing: dict, *, command: str, home: str) -> dict:
    """Smart-merge the better-memory MCP server into ~/.claude.json content.

    Preserves user's custom ``env`` values; ensures ``BETTER_MEMORY_HOME`` is
    set if absent. Refreshes ``command`` and ``args`` to current paths on
    every run. Other keys in ``mcpServers`` and other top-level config are
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
```

- [ ] **Step 5: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 9 tests pass (5 in `TestMergeClaudeJson` + 4 in `TestHookSpec`).

- [ ] **Step 6: Commit**

```bash
git add better_memory/cli/__init__.py better_memory/cli/install_hooks.py tests/cli/__init__.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): scaffold install_hooks module + merge_claude_json"
```

---

## Task 2: `merge_settings_json` + `_hook_entry` + 8 tests

**Confidence:** 92%
**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_install_hooks.py` (after the existing classes):

```python
from better_memory.cli.install_hooks import merge_settings_json


class TestMergeSettingsJson:
    def test_empty_hooks_adds_all_four(self) -> None:
        out = merge_settings_json({}, venv_pyw="/venv/bin/pythonw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1  # one shared matcher-group
        ss_hooks = ss[0]["hooks"]
        cmds = [h["command"] for h in ss_hooks]
        assert any("better_memory.hooks.session_start" in c for c in cmds)
        assert any("better_memory.hooks.session_retrieve" in c for c in cmds)

        ptu = out["hooks"]["PostToolUse"]
        assert len(ptu) == 1
        assert ptu[0]["matcher"] == "Write|Edit|Bash"
        assert "better_memory.hooks.observer" in ptu[0]["hooks"][0]["command"]
        assert ptu[0]["hooks"][0].get("async") is True

        stop = out["hooks"]["Stop"]
        assert len(stop) == 1
        assert "matcher" not in stop[0]
        assert "better_memory.hooks.session_close" in stop[0]["hooks"][0]["command"]
        assert stop[0]["hooks"][0].get("async") is True

    def test_existing_user_postooluse_preserved(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo hello"},
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pythonw")
        ptu = out["hooks"]["PostToolUse"]
        # User's group preserved.
        assert any(
            g.get("matcher") == "Bash"
            and g["hooks"][0]["command"] == "echo hello"
            for g in ptu
        )
        # Our group also present.
        assert any(
            g.get("matcher") == "Write|Edit|Bash"
            and "better_memory.hooks.observer" in g["hooks"][0]["command"]
            for g in ptu
        )

    def test_stale_better_memory_paths_refreshed(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/path/pythonw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/new/path/pythonw")
        observer_cmds = [
            h["command"]
            for g in out["hooks"]["PostToolUse"]
            for h in g["hooks"]
            if "better_memory.hooks.observer" in h["command"]
        ]
        # Exactly one observer entry; refreshed to new path.
        assert len(observer_cmds) == 1
        assert "/new/path/pythonw" in observer_cmds[0]
        assert "/old/path/pythonw" not in observer_cmds[0]

    def test_mixed_matcher_group_user_preserved(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {"type": "command", "command": "echo user-hook"},
                            {
                                "type": "command",
                                "command": "/p/pyw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        # The user's hook stays in some matcher-group; ours moves to a fresh
        # canonical group at the end. The original mixed-group has only the
        # user's hook left.
        all_groups = out["hooks"]["PostToolUse"]
        user_groups = [
            g for g in all_groups
            if any(h["command"] == "echo user-hook" for h in g["hooks"])
        ]
        assert len(user_groups) == 1
        # In the user-preserved group, our observer hook should be gone.
        assert all(
            "better_memory.hooks.observer" not in h["command"]
            for h in user_groups[0]["hooks"]
        )
        # And there must be a separate group with our observer.
        observer_groups = [
            g for g in all_groups
            if any("better_memory.hooks.observer" in h["command"] for h in g["hooks"])
        ]
        assert len(observer_groups) == 1

    def test_empty_matcher_groups_pruned(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/p/pyw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        # Old group had ONLY our hook; after REMOVE pass it would be empty
        # and is dropped. ADD pass adds a fresh canonical group.
        assert len(out["hooks"]["PostToolUse"]) == 1

    def test_session_start_pair_shares_matcher_group(self) -> None:
        out = merge_settings_json({}, venv_pyw="/p/pyw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1
        assert len(ss[0]["hooks"]) == 2
        modules = [h["command"] for h in ss[0]["hooks"]]
        assert any("session_start" in m and "session_retrieve" not in m for m in modules)
        assert any("session_retrieve" in m for m in modules)

    def test_user_session_start_hook_preserved(self) -> None:
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "echo user-on-start"},
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        ss = out["hooks"]["SessionStart"]
        # User's group preserved, our pair appended in its own group.
        assert any(
            g["hooks"][0]["command"] == "echo user-on-start"
            for g in ss
        )
        assert any(
            any("better_memory.hooks.session_start" in h["command"] for h in g["hooks"])
            for g in ss
        )

    def test_idempotent_second_run_is_noop(self) -> None:
        first = merge_settings_json({}, venv_pyw="/p/pyw")
        second = merge_settings_json(first, venv_pyw="/p/pyw")
        assert first == second
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py::TestMergeSettingsJson -v
```

Expected: FAIL — `ImportError: cannot import name 'merge_settings_json'`.

- [ ] **Step 3: Write the implementation**

Append to `better_memory/cli/install_hooks.py` (after `merge_claude_json`):

```python
# ------------------------------------------------------- pure merge: settings


def _hook_entry(spec: HookSpec, venv_pyw: str) -> dict:
    """Build the JSON object for a single hook entry."""
    entry: dict = {
        "type": "command",
        "command": f'"{venv_pyw}" -m {spec.module}',
    }
    if spec.is_async:
        entry["async"] = True
    return entry


def merge_settings_json(existing: dict, *, venv_pyw: str) -> dict:
    """Smart-merge the four hook entries into ~/.claude/settings.json content.

    Two-pass strategy:
    1. REMOVE — walk every event's every matcher-group's every hook. If the
       hook's ``command`` contains any of our 4 module paths, strip it.
       Drop matcher-groups whose ``hooks`` array is empty after removal.
    2. ADD — append canonical matcher-groups at the end of each event's
       array. SessionStart pair shares one group; PostToolUse and Stop
       each get their own group.

    User's other (non-better-memory) hooks and matcher-groups are untouched.
    """
    config = dict(existing)
    hooks = dict(config.get("hooks", {}))
    our_module_paths = {spec.module for spec in _OUR_HOOKS}

    # Pass 1: REMOVE
    for event_name in list(hooks.keys()):
        groups: list[dict] = []
        for group in hooks[event_name]:
            kept_hooks = [
                h for h in group.get("hooks", [])
                if not any(mp in h.get("command", "") for mp in our_module_paths)
            ]
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                groups.append(new_group)
            # else: empty after removal — drop it.
        hooks[event_name] = groups

    # Pass 2: ADD canonical groups.
    session_start_specs = [s for s in _OUR_HOOKS if s.event == "SessionStart"]
    if session_start_specs:
        hooks.setdefault("SessionStart", []).append({
            "hooks": [_hook_entry(s, venv_pyw) for s in session_start_specs],
        })

    for spec in (s for s in _OUR_HOOKS if s.event == "PostToolUse"):
        group: dict = {"hooks": [_hook_entry(spec, venv_pyw)]}
        if spec.matcher is not None:
            group["matcher"] = spec.matcher
        hooks.setdefault("PostToolUse", []).append(group)

    for spec in (s for s in _OUR_HOOKS if s.event == "Stop"):
        hooks.setdefault("Stop", []).append({
            "hooks": [_hook_entry(spec, venv_pyw)],
        })

    config["hooks"] = hooks
    return config
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 17 tests pass (9 from Task 1 + 8 new in `TestMergeSettingsJson`).

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): merge_settings_json with remove-then-add strategy"
```

---

## Task 3: `_load_or_empty` + 3 tests

**Confidence:** 95%
**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_install_hooks.py`:

```python
from pathlib import Path

from better_memory.cli.install_hooks import _load_or_empty


class TestLoadOrEmpty:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        out = _load_or_empty(tmp_path / "missing.json")
        assert out == {}

    def test_valid_json_returns_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        p.write_text('{"a": 1, "b": [2, 3]}', encoding="utf-8")
        assert _load_or_empty(p) == {"a": 1, "b": [2, 3]}

    def test_malformed_json_refuses_with_line_number(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"a": 1,\nnotjson}', encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            _load_or_empty(p)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        # Path + line# in stderr.
        assert str(p) in captured.err
        assert ":2:" in captured.err or ":2 " in captured.err
        assert "Fix the file then re-run" in captured.err
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py::TestLoadOrEmpty -v
```

Expected: FAIL — `ImportError: cannot import name '_load_or_empty'`.

- [ ] **Step 3: Write the implementation**

Append to `better_memory/cli/install_hooks.py`:

```python
# ----------------------------------------------------------- I/O helpers


def _load_or_empty(path: Path) -> dict:
    """Read JSON from path. Missing → {}. Malformed → SystemExit(1) with line#."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"[install_hooks] {path}:{exc.lineno}: {exc.msg}\n"
            f"[install_hooks] Fix the file then re-run scripts/setup.sh.",
            file=sys.stderr,
        )
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 20 tests pass (17 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): _load_or_empty with malformed-JSON refusal"
```

---

## Task 4: `_atomic_write` + 3 tests

**Confidence:** 90%
**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_install_hooks.py`:

```python
from better_memory.cli.install_hooks import _atomic_write


class TestAtomicWrite:
    def test_creates_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new.json"
        _atomic_write(target, '{"x": 1}')
        assert target.read_text(encoding="utf-8") == '{"x": 1}'

    def test_replaces_existing_content(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.json"
        target.write_text("OLD", encoding="utf-8")
        _atomic_write(target, "NEW")
        assert target.read_text(encoding="utf-8") == "NEW"

    def test_tmp_remains_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "x.json"
        target.write_text("ORIGINAL", encoding="utf-8")

        def boom(src: object, dst: object) -> None:  # noqa: ARG001
            raise PermissionError("simulated")

        monkeypatch.setattr("better_memory.cli.install_hooks.os.replace", boom)
        with pytest.raises(PermissionError, match="simulated"):
            _atomic_write(target, "NEW")
        # Original is unchanged.
        assert target.read_text(encoding="utf-8") == "ORIGINAL"
        # The .tmp sibling persists for forensics.
        tmp = target.with_suffix(target.suffix + ".tmp")
        assert tmp.exists()
        assert tmp.read_text(encoding="utf-8") == "NEW"
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py::TestAtomicWrite -v
```

Expected: FAIL — `ImportError: cannot import name '_atomic_write'`.

- [ ] **Step 3: Write the implementation**

Append to `better_memory/cli/install_hooks.py`:

```python
def _atomic_write(path: Path, content: str) -> None:
    """Write to ``{path}.tmp`` then ``os.replace``. Caller handles backups.

    On failure during ``os.replace``, the tmp file persists for forensics
    and the original (if any) is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 23 tests pass (20 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): _atomic_write via os.replace for crash-safe writes"
```

---

## Task 5: `_backup` + 2 tests

**Confidence:** 95%
**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_install_hooks.py`:

```python
from datetime import datetime as _datetime, timezone

from better_memory.cli.install_hooks import _backup


class TestBackup:
    def test_missing_source_returns_none(self, tmp_path: Path) -> None:
        result = _backup(
            tmp_path / "missing.json",
            tmp_path / "backups",
        )
        assert result is None
        assert not (tmp_path / "backups").exists()

    def test_existing_source_copied_with_timestamp(self, tmp_path: Path) -> None:
        src = tmp_path / "ok.json"
        src.write_text("CONTENT", encoding="utf-8")
        dst_dir = tmp_path / "backups"

        fixed = _datetime(2026, 5, 6, 19, 30, 7, tzinfo=timezone.utc)
        result = _backup(src, dst_dir, clock=lambda: fixed)
        assert result is not None
        assert result.name == "ok.json.20260506-193007.bak"
        assert result.read_text(encoding="utf-8") == "CONTENT"
        # Source unchanged.
        assert src.read_text(encoding="utf-8") == "CONTENT"
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py::TestBackup -v
```

Expected: FAIL — `ImportError: cannot import name '_backup'`.

- [ ] **Step 3: Write the implementation**

Append to `better_memory/cli/install_hooks.py`:

```python
def _backup(
    src: Path,
    dst_dir: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Path | None:
    """Copy ``src`` into ``dst_dir`` with a timestamped name. Idempotent
    in the sense that missing source → no-op (returns None).

    Timestamp format: ``YYYYMMDD-HHMMSS`` (UTC if no clock injected; local
    time of injected clock if injected).
    """
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    now = (clock or datetime.now)()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    dst = dst_dir / f"{src.name}.{timestamp}.bak"
    shutil.copy2(src, dst)
    return dst
```

Note: `clock=None` defaults to `datetime.now()` which uses local time (no tz). Tests inject a fixed clock for determinism.

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 25 tests pass (23 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): _backup helper with injectable clock"
```

---

## Task 6: `main()` + 4 integration tests

**Confidence:** 92%
**Files:**
- Modify: `better_memory/cli/install_hooks.py`
- Modify: `tests/cli/test_install_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_install_hooks.py`:

```python
import json as _json
import os as _os
import subprocess
import sys


@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.claude.json + ~/.claude/settings.json under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / ".better-memory"))
    (tmp_path / ".better-memory" / "install-backups").mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _run_cli(home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **_os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "BETTER_MEMORY_HOME": str(home / ".better-memory"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m", "better_memory.cli.install_hooks",
            "--venv-py",  "/p/python",
            "--venv-pyw", "/p/pythonw",
            "--home",     str(home / ".better-memory"),
        ],
        text=True, capture_output=True, env=env, timeout=30,
    )


class TestCLIIntegration:
    def test_fresh_install_writes_both_files(self, mock_home: Path) -> None:
        result = _run_cli(mock_home)
        assert result.returncode == 0, result.stderr

        claude_json = mock_home / ".claude.json"
        settings_json = mock_home / ".claude" / "settings.json"
        assert claude_json.exists()
        assert settings_json.exists()

        cj = _json.loads(claude_json.read_text(encoding="utf-8"))
        assert cj["mcpServers"]["better-memory"]["command"] == "/p/python"

        sj = _json.loads(settings_json.read_text(encoding="utf-8"))
        assert "SessionStart" in sj["hooks"]
        assert "PostToolUse" in sj["hooks"]
        assert "Stop" in sj["hooks"]

    def test_idempotent_rerun_is_clean(self, mock_home: Path) -> None:
        r1 = _run_cli(mock_home)
        assert r1.returncode == 0
        first_settings = (mock_home / ".claude" / "settings.json").read_text(encoding="utf-8")

        r2 = _run_cli(mock_home)
        assert r2.returncode == 0
        second_settings = (mock_home / ".claude" / "settings.json").read_text(encoding="utf-8")

        # Bytewise stable — JSON formatting + content unchanged on rerun.
        assert first_settings == second_settings

    def test_malformed_settings_refuses_without_writing(self, mock_home: Path) -> None:
        settings = mock_home / ".claude" / "settings.json"
        original = '{"hooks": {invalid'
        settings.write_text(original, encoding="utf-8")

        result = _run_cli(mock_home)
        assert result.returncode == 1
        # File is untouched.
        assert settings.read_text(encoding="utf-8") == original
        # stderr surfaces the path + a fix-and-re-run hint.
        assert str(settings) in result.stderr
        assert "Fix the file then re-run" in result.stderr

    def test_summary_lines_printed_on_success(self, mock_home: Path) -> None:
        result = _run_cli(mock_home)
        assert result.returncode == 0
        assert "Installing better-memory" in result.stdout
        assert "MCP server" in result.stdout
        assert "hooks" in result.stdout
        assert "Restart Claude Code" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/cli/test_install_hooks.py::TestCLIIntegration -v
```

Expected: FAIL — `main()` not present, the subprocess invocation will say `module has no attribute 'main'` or similar.

- [ ] **Step 3: Write the implementation**

Append to `better_memory/cli/install_hooks.py`:

```python
# --------------------------------------------------------------- orchestration


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="better_memory.cli.install_hooks")
    parser.add_argument(
        "--venv-py", required=True,
        help="Path to venv python (Linux/macOS) or python.exe (Windows). MCP-server command.",
    )
    parser.add_argument(
        "--venv-pyw", required=True,
        help="Path to venv pythonw.exe (Windows) or python (Linux/macOS). Hook command.",
    )
    parser.add_argument(
        "--home", required=True,
        help="Resolved $BETTER_MEMORY_HOME (used for backup directory).",
    )
    args = parser.parse_args(argv)

    home_dir = Path(args.home)
    backup_dir = home_dir / "install-backups"

    targets = [
        (
            "MCP server",
            Path.home() / ".claude.json",
            lambda d: merge_claude_json(
                d, command=args.venv_py, home=args.home,
            ),
        ),
        (
            "hooks",
            Path.home() / ".claude" / "settings.json",
            lambda d: merge_settings_json(d, venv_pyw=args.venv_pyw),
        ),
    ]

    print("[install_hooks] Installing better-memory MCP server + hooks...")
    for label, path, merge in targets:
        existing = _load_or_empty(path)  # may exit(1) on malformed JSON
        backup_path = _backup(path, backup_dir)
        merged = merge(existing)
        _atomic_write(path, json.dumps(merged, indent=2) + "\n")
        if backup_path:
            try:
                rel = backup_path.relative_to(home_dir)
            except ValueError:
                rel = backup_path
            print(f"  ✓ {label}: {path} (backup: {rel})")
        else:
            print(f"  ✓ {label}: {path} (created fresh)")
    print("[install_hooks] Restart Claude Code to load the new MCP server.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/cli/test_install_hooks.py -v
```

Expected: 29 tests pass (25 prior + 4 new in `TestCLIIntegration`).

- [ ] **Step 5: Commit**

```bash
git add better_memory/cli/install_hooks.py tests/cli/test_install_hooks.py
git commit -m "feat(cli): main() orchestration + CLI integration tests"
```

---

## Task 7: `setup.sh` integration

**Confidence:** 90%
**Files:**
- Modify: `scripts/setup.sh` (replace lines 196–264)

- [ ] **Step 1: Read the current setup.sh tail to confirm line numbers**

```
sed -n '195,265p' scripts/setup.sh
```

Expected: shows the print-snippets `cat <<EOF ... EOF` block. Confirm the start line (`# 6. Print the Claude config snippets`) and the line after the closing `EOF` (`log "Done."` or similar).

If the line numbers have drifted, use the section header as the anchor — the entire `# 6. Print the Claude config snippets` section is what we're replacing.

- [ ] **Step 2: Apply the replacement**

In `scripts/setup.sh`, replace the existing section starting at the `# 6. Print the Claude config snippets` header through the closing `EOF` of the heredoc:

```bash
# ---------------------------------------------------------------------------
# 6. Install MCP server + hooks into Claude Code config files
# ---------------------------------------------------------------------------

log "Installing into ~/.claude.json and ~/.claude/settings.json..."
(cd "$PROJECT_DIR" && uv run python -m better_memory.cli.install_hooks \
    --venv-py "$(win_path "$VENV_PY")" \
    --venv-pyw "$(win_path "$VENV_PYW")" \
    --home "$BETTER_MEMORY_HOME") || {
    error "install_hooks failed (see message above)."
    error "scripts/setup.sh aborting; fix the issue and re-run."
    exit 1
}
```

The trailing `log "Done."` line stays; everything between the new section and `log "Done."` is the replaced block.

- [ ] **Step 3: Verify the script still parses**

```
bash -n scripts/setup.sh
```

Expected: no output (syntax check passes).

- [ ] **Step 4: Run a focused-but-non-destructive smoke check**

We don't want to actually run setup.sh end-to-end (it'll mutate the real `~/.claude.json`). Instead, just confirm the replaced section's contract with a dry parse:

```
grep -n "install_hooks" scripts/setup.sh
```

Expected: at least one match — the `uv run python -m better_memory.cli.install_hooks` line. The line should reference `$VENV_PY`, `$VENV_PYW`, `$BETTER_MEMORY_HOME`.

- [ ] **Step 5: Verify the print-snippets block is gone**

```
grep -c "EOF" scripts/setup.sh
```

Expected: should drop to whatever count it was prior MINUS the heredoc's start/end pair. Visual verify with:

```
sed -n '/# 6\. /,/^log "Done\."/p' scripts/setup.sh
```

Expected: shows only the new install section, nothing about pasting JSON snippets manually.

- [ ] **Step 6: Commit**

```bash
git add scripts/setup.sh
git commit -m "feat(setup): replace print-snippets block with install_hooks shell-out"
```

---

## Task 8: Docs updates (README + website)

**Confidence:** 95%
**Files:**
- Modify: `README.md`
- Modify: `website/configuration.md`

- [ ] **Step 1: Update README.md Manual setup section**

Locate the `## Manual setup` section in `README.md`. Currently it begins with prose like "If you'd rather do it by hand:" and shows the JSON examples.

Add a new paragraph IMMEDIATELY ABOVE the existing `## Manual setup` heading:

```markdown
> **Note:** `./scripts/setup.sh` writes both `~/.claude.json` and `~/.claude/settings.json` for you idempotently. The Manual setup section below is reference material — useful if you need to inspect or hand-edit the config, but not required for a normal install.
```

The existing Manual setup content stays intact (the JSON examples are still useful as reference).

- [ ] **Step 2: Update website/configuration.md Hooks section lead**

In `website/configuration.md`, find the `## Hooks` section (added in Track A's PR #46). Replace its first sentence:

OLD:
```markdown
Three Claude Code hooks ship with better-memory and read or write the filesystem layout above:
```

NEW:
```markdown
Four Claude Code hooks ship with better-memory and read or write the filesystem layout above. They are installed automatically by `./scripts/setup.sh` (which calls `python -m better_memory.cli.install_hooks` to merge them idempotently into `~/.claude/settings.json`). The list below is reference material:
```

(The existing four bullets are correct; only the lead sentence changes. The "Three" → "Four" was already corrected in PR #46's Task 8.)

- [ ] **Step 3: Verify mkdocs build passes**

```
uv run mkdocs build --strict 2>&1 | tail -3
```

Expected: build succeeds with no warnings on the changes.

- [ ] **Step 4: Commit**

```bash
git add README.md website/configuration.md
git commit -m "docs: setup.sh installs hooks automatically; manual setup is reference"
```

---

## Manual smoke test (post-implementation, pre-merge)

Not a step but a recommended final sanity check:

1. From the worktree:
   ```
   ./scripts/setup.sh
   ```
2. Confirm `~/.claude.json` now has `mcpServers.better-memory` with the right command + args + env.
3. Confirm `~/.claude/settings.json` has `hooks.SessionStart` (1 group, 2 entries), `hooks.PostToolUse` (group with `Write|Edit|Bash` matcher), `hooks.Stop` (1 group).
4. Confirm `~/.better-memory/install-backups/` now has timestamped `.bak` files for whichever target files existed before.
5. Re-run `./scripts/setup.sh`; confirm the JSON in both targets is byte-identical to the prior run (idempotent).
6. Restart Claude Code; open a fresh session in this repo; verify (a) the MCP server connects (`mcp__better-memory__memory_retrieve` is callable) and (b) Claude reports memory at session start (no manual call needed — the SessionStart `session_retrieve` hook fires).

---

## Confidence summary

| # | Task | Conf. |
|---|---|---|
| 1 | Module scaffold + `merge_claude_json` + 5 tests | 95% |
| 2 | `merge_settings_json` + `_hook_entry` + 8 tests | 92% |
| 3 | `_load_or_empty` + 3 tests | 95% |
| 4 | `_atomic_write` + 3 tests | 90% |
| 5 | `_backup` + 2 tests | 95% |
| 6 | `main()` + 4 integration tests | 92% |
| 7 | `setup.sh` integration | 90% |
| 8 | Docs updates | 95% |

All ≥90%. No mitigations required at plan-stage. The two iterated cells (Task 2 and the test-suite portion of Task 6) match the spec's already-iterated values.

---

## Out of scope (explicitly deferred)

- `--dry-run` / `--print-snippets` flags
- Uninstall command
- Trivia-preserving JSON parsing (formatting normalization is acceptable)
- Cleanup of dead-code `CONSOLIDATE_MODEL` chat-model pull at `setup.sh:171–181` (separate follow-up)
- Cross-platform smoke test of `pythonw.exe` SessionStart hook stdout flushing (verified manually post-install per spec Concern 3)

---

## References

- Spec: [`docs/superpowers/specs/2026-05-06-setup-auto-install-design.md`](../specs/2026-05-06-setup-auto-install-design.md)
- Track A reference (the SessionStart hook this installer registers): [`docs/superpowers/specs/2026-05-06-session-memory-injection-hook-design.md`](../specs/2026-05-06-session-memory-injection-hook-design.md)
- Existing setup script: `scripts/setup.sh:196-264` (the print-snippets block being replaced)
- Hook entry points referenced by `_OUR_HOOKS`:
  - `better_memory/hooks/session_start.py`
  - `better_memory/hooks/session_retrieve.py` (Track A, PR #46)
  - `better_memory/hooks/observer.py`
  - `better_memory/hooks/session_close.py`
- Claude Code hooks contract: `code.claude.com/docs/en/hooks-guide.md`
