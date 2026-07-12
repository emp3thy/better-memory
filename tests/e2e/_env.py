"""THE single environment choke point for every e2e subprocess spawn.

Every subprocess / stdio-server spawn in ``tests/e2e`` MUST build its
environment through :func:`isolated_env` (or ``_agentcore_env.agentcore_env``,
which extends it). Hand-rolled env dicts and ``{**os.environ, ...}`` patterns
are banned — enforced by ``tests/e2e_meta/test_env_helper_contract.py``.

Contract (design section 2, ``2026-07-12-e2e-clean-slate-smoke-design.md``):

* Built from an **allowlist**, never ``os.environ.copy()``. Only
  ``{PATH, SYSTEMROOT, COMSPEC, PATHEXT, TEMP, TMP, WINDIR, LANG, LC_ALL,
  PYTHONIOENCODING}`` are preserved from the outer environment, matched
  **case-insensitively** (Windows env keys carry arbitrary case:
  ``SystemRoot``, ``Path``, ``windir``...).
* ``HOME`` and ``USERPROFILE`` are BOTH set, unconditionally, on every OS.
  A POSIX contributor deleting the "redundant" USERPROFILE line is the exact
  bug that torches a Windows dev's real ``~/.claude.json`` — do not touch.
* ``HOMEDRIVE``/``HOMEPATH`` are pinned to the tmp home on Windows
  (``Path.home()``'s fallback chain).
* All outer ``CLAUDE_*`` / ``BETTER_MEMORY_*`` / ``AWS_*`` / ``OLLAMA_*``
  vars are dropped case-insensitively (automatic: allowlist construction
  never copies them in the first place).
* Pins: ``BETTER_MEMORY_HOME=<tmp>/.better-memory``, ``BETTER_MEMORY_PROJECT``,
  ``CLAUDE_SESSION_ID``, ``BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite`` and a
  poisoned ``OLLAMA_HOST`` so nothing can ever reach a real Ollama daemon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Outer-environment variables that survive into the isolated env.
#: SYSTEMROOT/COMSPEC: child pythons on Windows fail to import ssl/sqlite3
#: without them. TEMP/TMP: tempfile.gettempdir() must not fall back to CWD.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "WINDIR",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
    }
)

#: Prefixes that must never leak from the outer environment (checked
#: case-insensitively). The allowlist construction guarantees this; the
#: constant is exported so meta tests can assert against the same source
#: of truth.
STRIPPED_PREFIXES: tuple[str, ...] = (
    "CLAUDE_",
    "BETTER_MEMORY_",
    "AWS_",
    "OLLAMA_",
)

#: Poisoned Ollama endpoint: `.invalid` is an IETF-reserved TLD, so no DNS
#: resolution can ever succeed — any code path that tries to reach Ollama
#: fails fast instead of touching a real daemon.
POISONED_OLLAMA_HOST = "http://does-not-exist.invalid:1"

DEFAULT_PROJECT = "e2e-project"
DEFAULT_SESSION_ID = "e2e-session-1"


def _set(env: dict[str, str], key: str, value: str) -> None:
    """Set ``key`` replacing any case-insensitive duplicate first."""
    upper = key.upper()
    for existing in [k for k in env if k.upper() == upper]:
        del env[existing]
    env[key] = value


def _delete(env: dict[str, str], key: str) -> None:
    upper = key.upper()
    for existing in [k for k in env if k.upper() == upper]:
        del env[existing]


def isolated_env(tmp_home: Path, **pins: str | None) -> dict[str, str]:
    """Build a hermetic child-process environment homed at ``tmp_home``.

    ``pins`` override or extend the defaults; a pin of ``None`` removes the
    key entirely (e.g. ``CLAUDE_SESSION_ID=None`` for marker-bridge tests).

    Returns a plain dict suitable for ``subprocess.run(env=...)`` and
    ``StdioServerParameters(env=...)``.
    """
    home = str(tmp_home)
    env: dict[str, str] = {}

    # 1. Allowlist copy from the outer env, case-insensitive, deduped.
    seen: set[str] = set()
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in ALLOWLIST and upper not in seen:
            seen.add(upper)
            env[key] = value

    # 2. Home redirection — both vars, every OS (see module docstring).
    _set(env, "HOME", home)
    _set(env, "USERPROFILE", home)
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(home)
        if drive:
            _set(env, "HOMEDRIVE", drive)
            _set(env, "HOMEPATH", tail or "\\")

    # 3. Deliberate better-memory pins.
    _set(env, "BETTER_MEMORY_HOME", str(Path(tmp_home) / ".better-memory"))
    _set(env, "BETTER_MEMORY_PROJECT", DEFAULT_PROJECT)
    _set(env, "CLAUDE_SESSION_ID", DEFAULT_SESSION_ID)
    _set(env, "BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    _set(env, "OLLAMA_HOST", POISONED_OLLAMA_HOST)

    # 4. Caller pins (override defaults; None removes).
    for key, value in pins.items():
        if value is None:
            _delete(env, key)
        else:
            _set(env, key, value)

    # 5. Invariant: case-insensitive key uniqueness (duplicate 'Path'/'PATH'
    # keys corrupt Windows CreateProcess environment blocks).
    uppers = [k.upper() for k in env]
    if len(set(uppers)) != len(uppers):
        raise AssertionError(f"case-insensitive duplicate keys in isolated_env: {sorted(env)}")

    return env
