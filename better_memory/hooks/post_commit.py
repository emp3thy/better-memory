"""Post-commit hook — opt-in episode close via commit-message trailer.

Runs after every git commit. Reads the latest commit message via
``git log --format=%B -1 HEAD`` and checks for a ``Closes-Episode``
trailer. When the trailer value is truthy (``true``, ``yes``, ``1``,
case-insensitive), writes a ``commit_close`` marker to the spool.
SpoolService.drain processes the marker on the next drain by calling
``EpisodeService.close_active`` for the session.

Like the other hooks (observer, session_start, session_close):
- No SQLite imports.
- Never raises; always exits 0.
- Bounded stdin read (this hook doesn't actually consume stdin, but
  we follow the pattern of defensively draining it anyway so buffered
  input from git's hook wiring can't block).

Mirrors the ``session_start.py`` cwd-resolution note: ``os.getcwd()`` is
primary with ``PWD`` as fallback because ``PWD`` is stale on Windows +
``subprocess.run(cwd=)`` chains.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_MAX_STDIN_BYTES = 1_048_576

# Truthy values for the Closes-Episode trailer (case-insensitive).
_TRUTHY_VALUES = {"true", "yes", "1"}

# The trailer key we look for (matched case-insensitively).
_TRAILER_KEY = "closes-episode"


def _default_spool_dir() -> Path:
    home = os.environ.get("BETTER_MEMORY_HOME")
    if home:
        return Path(home).expanduser() / "spool"
    return Path.home() / ".better-memory" / "spool"


def _safe_timestamp(raw: str | None) -> str:
    if not raw:
        raw = datetime.now(UTC).isoformat()
    return raw.replace(":", "-")


def _read_head_commit_message() -> tuple[str, str]:
    """Return ``(subject_plus_body, commit_sha)`` for HEAD.

    Uses ``git log -1 --format=%B%n==SEP==%n%H HEAD`` so the caller can
    split on a sentinel that cannot appear inside a commit message or
    a git SHA. Raises ``subprocess.CalledProcessError`` on failure
    (not a git repo, no commits, missing git binary) — the main() wrapper
    swallows it.
    """
    result = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%B%n==SEP==%n%H",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = result.stdout
    sep = "\n==SEP==\n"
    if sep not in stdout:
        # Malformed git output — treat as no trailer.
        return ("", "")
    message, sha = stdout.rsplit(sep, 1)
    return (message, sha.strip())


def _parse_trailer_value(message: str, key: str) -> str | None:
    """Return the last occurrence of trailer ``key`` in ``message``, or None.

    Trailer format: ``Key: value`` on its own line, typically at the end
    of the message after a blank line. We scan ALL lines (not only the
    "trailer block") so the rule is simpler and robust against commit
    messages that don't have a blank-line separator. Git's own trailer
    parsing is more subtle but the simpler rule is adequate for an opt-in
    signal where the user controls the commit message.

    Key match is case-insensitive. Value is stripped of surrounding
    whitespace but otherwise preserved. Last occurrence wins (matches
    git convention for duplicate trailers).
    """
    key_lower = key.lower()
    value: str | None = None
    for line in message.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        if k.strip().lower() == key_lower:
            value = v.strip()
    return value


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in _TRUTHY_VALUES


def _resolve_cwd() -> str:
    # PWD is stale when subprocess.run cwd= is used (Windows/git-bash sets
    # PWD in the parent shell and doesn't propagate); os.getcwd() is
    # authoritative.
    try:
        return os.getcwd()
    except Exception:
        return os.environ.get("PWD") or "/"


def main() -> None:
    try:
        # Drain stdin defensively even though git's post-commit hook typically
        # provides none. Following the other hooks' pattern.
        try:
            sys.stdin.read(_MAX_STDIN_BYTES + 1)
        except Exception:
            pass

        try:
            message, commit_sha = _read_head_commit_message()
        except Exception:
            # Not a git repo, no commits, or git binary missing. Exit 0 silently.
            sys.exit(0)

        trailer_value = _parse_trailer_value(message, _TRAILER_KEY)
        if not _is_truthy(trailer_value):
            # No opt-in — hook does nothing.
            sys.exit(0)

        now_iso = datetime.now(UTC).isoformat()
        session_id = os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
        cwd = _resolve_cwd()

        data: dict[str, object] = {
            "event_type": "commit_close",
            "timestamp": now_iso,
            "session_id": session_id,
            "cwd": cwd,
            "project": Path(cwd).name,
            "commit_sha": commit_sha,
        }

        spool_dir = _default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = _safe_timestamp(now_iso)
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_commit_close_{hash_hex}.json"
        (spool_dir / file_name).write_text(
            json.dumps(data), encoding="utf-8"
        )
    except Exception:
        # Hooks must never fail.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
