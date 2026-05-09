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
    HookSpec("better_memory.hooks.session_bootstrap", "SessionStart", None,              False),
    HookSpec("better_memory.hooks.observer",          "PostToolUse",  "Write|Edit|Bash", True),
    HookSpec("better_memory.hooks.session_close",     "Stop",         None,              True),
)

# Module paths that are no longer registered but may be present in users'
# settings.json from prior installs. Scrubbed by the REMOVE pass on every
# install so upgrades land cleanly.
_LEGACY_HOOK_MODULES: frozenset[str] = frozenset({
    "better_memory.hooks.session_start",
    "better_memory.hooks.session_retrieve",
})


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
    strip_modules = our_module_paths | _LEGACY_HOOK_MODULES

    # Pass 1: REMOVE
    for event_name in list(hooks.keys()):
        groups: list[dict] = []
        for group in hooks[event_name]:
            kept_hooks = [
                h for h in group.get("hooks", [])
                if not any(mp in h.get("command", "") for mp in strip_modules)
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


def _atomic_write(path: Path, content: str) -> None:
    """Write to ``{path}.tmp`` then ``os.replace``. Caller handles backups.

    On failure during ``os.replace``, the tmp file persists for forensics
    and the original (if any) is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


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
    # Pass 1 (VALIDATE): load + parse ALL targets before any writes. If any
    # is malformed, _load_or_empty exits 1 here, and no target file has been
    # modified yet. Prevents partial-install state where the first target
    # gets written before the second is discovered to be malformed.
    loaded: list[tuple[str, Path, Callable[[dict], dict], dict]] = []
    for label, path, merge in targets:
        existing = _load_or_empty(path)
        loaded.append((label, path, merge, existing))

    # Pass 2 (WRITE): backup + merge + atomic-write each target.
    for label, path, merge, existing in loaded:
        backup_path = _backup(path, backup_dir)
        merged = merge(existing)
        _atomic_write(path, json.dumps(merged, indent=2) + "\n")
        if backup_path:
            try:
                rel = backup_path.relative_to(home_dir)
            except ValueError:
                rel = backup_path
            print(f"  OK {label}: {path} (backup: {rel})")
        else:
            print(f"  OK {label}: {path} (created fresh)")
    print("[install_hooks] Restart Claude Code to load the new MCP server.")


if __name__ == "__main__":
    main()
