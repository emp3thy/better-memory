"""Session-id marker bridge: SessionStart hook → MCP server.

Claude Code does not propagate ``CLAUDE_SESSION_ID`` into the spawned stdio
MCP server's environment (verified empirically; see GitHub issue
anthropics/claude-code#1335). The SessionStart hook does receive ``session_id``
in its stdin payload, so it writes that id to a per-project marker file. The
MCP rating handlers fall back to reading the marker when env-var lookup fails.

Key: ``$CLAUDE_PROJECT_DIR`` (set by Claude Code in both hook and MCP env),
with ``os.getcwd()`` as fallback. Both sides resolve the same key, so the
marker path is consistent without further coordination.

Layout::

    $BETTER_MEMORY_HOME/runtime/sessions/<encoded-project-dir>

``<encoded-project-dir>`` replaces every non-alphanumeric character with
``-`` (matching Claude Code's transcript path encoding under
``~/.claude/projects/``). The file contains the ``session_id`` as a single
UTF-8 line.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_project_dir(project_dir: str | os.PathLike[str]) -> str:
    return _NON_ALNUM.sub("-", str(project_dir))


def _resolve_project_dir(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def marker_path(home: Path, project_dir: str | None = None) -> Path:
    return home / "runtime" / "sessions" / encode_project_dir(
        _resolve_project_dir(project_dir)
    )


def write_session_id(
    home: Path,
    session_id: str,
    project_dir: str | None = None,
) -> None:
    """Atomic-write ``session_id`` to the marker file.

    Best-effort: on any OSError, return silently. The hook entry point that
    calls this must never raise.
    """
    if not session_id:
        return
    try:
        path = marker_path(home, project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sid-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(session_id)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return


def read_session_id(
    home: Path,
    project_dir: str | None = None,
) -> str | None:
    """Read the session_id from the marker file, or None on any failure."""
    try:
        path = marker_path(home, project_dir)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None
