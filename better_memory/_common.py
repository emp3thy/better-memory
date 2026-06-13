"""Tiny shared helpers used across hooks, services, storage, and the MCP layer.

Pure stdlib with no intra-package imports, so hooks can import this module
without paying for SQLite / sqlite-vec / boto3 / config at invocation time
(hooks must start fast and never fail). Everything heavier belongs in
:mod:`better_memory.config` or the relevant service module.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_DEFAULT_HOME = "~/.better-memory"


def env_session_id() -> str | None:
    """Return the Claude session id from the environment, or ``None``.

    Reads ``CLAUDE_SESSION_ID`` first (kept as the primary name for
    back-compat), then ``CLAUDE_CODE_SESSION_ID`` (the name Claude Code
    actually exports). The shared resolution order makes hook-written
    events and MCP-written observations agree on the same session id.
    """
    return (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
    )


def get_session_id() -> str:
    """Return the current Claude session id, generating one if no env var
    is set. Falls back to a fresh ``uuid4().hex`` (32 chars)."""
    return env_session_id() or uuid4().hex


def default_clock() -> datetime:
    """UTC-aware ``now``. The conventional default for injectable ``clock``
    parameters across services."""
    return datetime.now(UTC)


def resolve_home() -> Path:
    """Return ``BETTER_MEMORY_HOME`` (or its default) with ``~`` expanded."""
    raw = os.environ.get("BETTER_MEMORY_HOME", _DEFAULT_HOME)
    return Path(raw).expanduser()


def default_spool_dir() -> Path:
    """Return ``$BETTER_MEMORY_HOME/spool``, defaulting to ``~/.better-memory``."""
    return resolve_home() / "spool"


def safe_timestamp(raw: str | None) -> str:
    """Return a filesystem-safe timestamp component.

    Replaces ``:`` (illegal on NTFS) with ``-``. Falls back to current UTC
    time if ``raw`` is missing or empty.
    """
    if not raw:
        raw = datetime.now(UTC).isoformat()
    return raw.replace(":", "-")
