"""Configuration resolution for better-memory.

Resolves environment variables to concrete paths and settings. All path-like
values are returned as :class:`pathlib.Path` objects with ``~`` expanded to
the current user's home directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_ROOT = Path("~/.better-memory")

_DEFAULTS: dict[str, str] = {
    "MEMORY_DB": str(_DEFAULT_ROOT / "memory.db"),
    "KNOWLEDGE_DB": str(_DEFAULT_ROOT / "knowledge.db"),
    "KNOWLEDGE_BASE": "~/knowledge-base",
    "SPOOL_DIR": str(_DEFAULT_ROOT / "spool"),
    "OLLAMA_HOST": "http://localhost:11434",
    "EMBED_MODEL": "nomic-embed-text",
}


def _resolve_path(env_var: str) -> Path:
    """Return the env var (or its default) as a Path with ``~`` expanded."""
    raw = os.environ.get(env_var, _DEFAULTS[env_var])
    return Path(raw).expanduser()


def _resolve_str(env_var: str) -> str:
    """Return the env var (or its default) as a plain string."""
    return os.environ.get(env_var, _DEFAULTS[env_var])


def _resolve_bool(env_var: str, default: bool) -> bool:
    """Return a boolean from the env var, accepting common truthy strings."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Resolved better-memory configuration."""

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
    return Config(
        memory_db=_resolve_path("MEMORY_DB"),
        knowledge_db=_resolve_path("KNOWLEDGE_DB"),
        knowledge_base=_resolve_path("KNOWLEDGE_BASE"),
        spool_dir=_resolve_path("SPOOL_DIR"),
        ollama_host=_resolve_str("OLLAMA_HOST"),
        embed_model=_resolve_str("EMBED_MODEL"),
        audit_log_retrieved=_resolve_bool("AUDIT_LOG_RETRIEVED", default=True),
    )
