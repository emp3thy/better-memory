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
