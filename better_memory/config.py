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

_DEFAULT_HOME = "~/.better-memory"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"


def _resolve_home() -> Path:
    """Return ``BETTER_MEMORY_HOME`` (or its default) with ``~`` expanded."""
    raw = os.environ.get("BETTER_MEMORY_HOME", _DEFAULT_HOME)
    return Path(raw).expanduser()


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


def get_config() -> Config:
    """Resolve the current environment into a :class:`Config`.

    Called each time so tests can override env vars between calls.
    """
    home = _resolve_home()
    return Config(
        home=home,
        memory_db=home / "memory.db",
        knowledge_db=home / "knowledge.db",
        knowledge_base=home / "knowledge-base",
        spool_dir=home / "spool",
        ollama_host=_resolve_str("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST),
        embed_model=_resolve_str("EMBED_MODEL", _DEFAULT_EMBED_MODEL),
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
    )
