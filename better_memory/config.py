"""Configuration resolution for better-memory.

Single environment variable (``BETTER_MEMORY_HOME``) roots the runtime
filesystem layout. Everything lives under that directory:

    $BETTER_MEMORY_HOME/
        memory.db
        knowledge.db
        spool/
        knowledge-base/

Default home is ``~/.better-memory``. External-service knobs
(``OLLAMA_HOST``, ``EMBED_MODEL``, ``AUDIT_LOG_RETRIEVED``) are separate env
vars because they're orthogonal to path layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from better_memory import _diag

_DEFAULT_HOME = "~/.better-memory"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"


def resolve_home() -> Path:
    """Return ``BETTER_MEMORY_HOME`` (or its default) with ``~`` expanded."""
    raw = os.environ.get("BETTER_MEMORY_HOME", _DEFAULT_HOME)
    return Path(raw).expanduser()


# Maps absolute cwd string → resolved project name (or None when no git tree
# is reachable). Both successes and ``None`` are cached for the process
# lifetime: the walk is deterministic given filesystem state, so caching the
# negative result is safe. The previous design avoided caching ``None``
# because the subprocess fallback could hang/hiccup transiently — that
# fallback is gone (see ``_walk_for_git_root``), so the rationale no longer
# applies.
_git_project_cache: dict[str, str | None] = {}


def _walk_for_git_root(cwd_str: str) -> str | None:
    """Walk up from ``cwd_str`` looking for ``.git``. Pure stdlib, no subprocess.

    Replaces the old ``git rev-parse --git-common-dir`` subprocess (which
    hung for ~65 s on Windows when ``subprocess.run``'s post-timeout cleanup
    blocked on a child that refused to reap quickly under AV/EDR scanning).
    Handles the two ``.git`` shapes git itself recognises:

    * Directory — standard repo. Returns the parent directory's ``.name``.
    * File — worktree (or submodule) marker, contents ``gitdir: <path>``
      pointing at ``<main_repo>/.git/worktrees/<name>``. The main repo
      root sits three parents above ``gitdir``; its ``.name`` is returned
      to match the worktree-aware semantics that originally motivated
      shelling out to ``git rev-parse --git-common-dir``.

    Returns ``None`` if no ``.git`` entry is found in any ancestor.
    """
    fn = "_walk_for_git_root"
    with _diag.trace(fn, cwd_str=cwd_str):
        cwd = Path(cwd_str)
        for candidate in (cwd, *cwd.parents):
            git_path = candidate / ".git"
            if not git_path.exists():
                continue
            kind = "dir" if git_path.is_dir() else "file"
            _diag.step(fn, "git_found", path=str(git_path), kind=kind)
            if git_path.is_dir():
                return candidate.name or None
            try:
                text = git_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                _diag.step(fn, "read_failed", exc=repr(exc))
                return None
            prefix = "gitdir:"
            if not text.startswith(prefix):
                _diag.step(fn, "unexpected_git_file_format")
                return None
            gitdir = Path(text[len(prefix):].strip())
            if not gitdir.is_absolute():
                gitdir = (candidate / gitdir).resolve()
            try:
                main_repo = gitdir.parents[2]
            except IndexError:
                _diag.step(fn, "gitdir_too_shallow", gitdir=str(gitdir))
                return None
            return main_repo.name or None
        return None


def _resolve_git_project(cwd_str: str) -> str | None:
    """Resolve the git repo's main directory name for ``cwd_str``.

    Walks up the directory tree looking for a ``.git`` entry (directory for
    standard repos, file containing ``gitdir: <path>`` for worktrees) and
    returns the corresponding repo root's ``.name``. Returns ``None`` when
    ``cwd_str`` is not inside any git tree.

    Both successful resolutions AND ``None`` are cached for the lifetime of
    the process by absolute path string. Caching ``None`` is safe because
    the walk is deterministic given filesystem state. The earlier subprocess
    implementation deliberately did NOT cache failures because a transient
    ``subprocess.run`` hiccup (5 s timeout tripping during cold start, fork
    race during stdio MCP spawn) could poison the process for its entire
    lifetime, silently collapsing project resolution to ``"general"`` with
    no recovery short of a restart. The walk has no such transient failure
    mode — see ``docs/debug/2026-05-13-synthesize-freeze.md``.

    Callers must pass the resolved absolute path so equivalent paths share
    a cache slot.
    """
    fn = "_resolve_git_project"
    with _diag.trace(fn, cwd_str=cwd_str):
        if cwd_str in _git_project_cache:
            cached = _git_project_cache[cwd_str]
            _diag.step(fn, "cache_hit", value=cached)
            return cached
        _diag.step(fn, "cache_miss_walking")
        result = _walk_for_git_root(cwd_str)
        _diag.step(fn, "walk_returned", value=result)
        _git_project_cache[cwd_str] = result
        return result


def project_name(cwd: Path | None = None) -> str:
    """Return the canonical project name for ``cwd`` (defaults to ``Path.cwd()``).

    Resolution order:
    1. ``<cwd>/.better-memory`` override file: first non-empty stripped line.
       The ``.better-memory`` override is checked only at ``cwd``, not at
       ancestors — the override is a deliberate per-directory signal.
    2. ``git rev-parse --git-common-dir`` (handles worktrees: returns the main
       repo's .git directory). Project name = parent dir's ``.name``.
    3. ``"general"`` if no git tree is found or git is unavailable.

    Used uniformly by knowledge search, observation writes/reads, episode
    scoping, the UI panel filter, and hook payloads — every subsystem that
    buckets state by project must call this helper, never construct the
    name inline.

    The override-file branch is intentionally **not** memoized so editing
    ``.better-memory`` mid-session takes effect immediately. The git
    resolution is memoized by absolute path via :func:`_resolve_git_project`.
    """
    fn = "project_name"
    with _diag.trace(fn):
        _diag.step(fn, "resolve_cwd")
        cwd = cwd if cwd is not None else Path.cwd()
        _diag.step(fn, "cwd_resolved", cwd=str(cwd))

        override = cwd / ".better-memory"
        _diag.step(fn, "check_override_file", path=str(override))
        if override.is_file():
            _diag.step(fn, "override_found_reading")
            text = override.read_text(encoding="utf-8").strip()
            if text:
                first = text.splitlines()[0].strip()
                _diag.step(fn, "override_returned", value=first)
                return first

        _diag.step(fn, "calling_resolve_git_project")
        cwd_resolved = str(cwd.resolve())
        _diag.step(fn, "cwd_resolve_done", value=cwd_resolved)
        result = _resolve_git_project(cwd_resolved) or "general"
        _diag.step(fn, "returning", value=result)
        return result


def _resolve_str(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


def _resolve_bool(env_var: str, default: bool) -> bool:
    """Return a boolean from the env var, accepting common truthy strings."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Resolved better-memory configuration."""

    home: Path
    memory_db: Path
    knowledge_db: Path
    knowledge_base: Path
    spool_dir: Path
    ollama_host: str
    embed_model: str
    audit_log_retrieved: bool
    auto_prune: bool
    diag_logging: bool


def get_config() -> Config:
    """Resolve the current environment into a :class:`Config`.

    Called each time so tests can override env vars between calls.
    """
    home = resolve_home()
    return Config(
        home=home,
        memory_db=home / "memory.db",
        knowledge_db=home / "knowledge.db",
        knowledge_base=home / "knowledge-base",
        spool_dir=home / "spool",
        ollama_host=_resolve_str("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST),
        embed_model=_resolve_str("EMBED_MODEL", _DEFAULT_EMBED_MODEL),
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
        auto_prune=_resolve_bool("BETTER_MEMORY_AUTO_PRUNE", default=False),
        diag_logging=_resolve_bool("BETTER_MEMORY_DIAG_LOGGING", default=False),
    )
