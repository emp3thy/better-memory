"""Tests for :mod:`better_memory.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.config import get_config


def test_defaults_resolve_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env vars set, paths land under the user's home directory."""
    for var in (
        "MEMORY_DB",
        "KNOWLEDGE_DB",
        "KNOWLEDGE_BASE",
        "SPOOL_DIR",
        "OLLAMA_HOST",
        "EMBED_MODEL",
        "AUDIT_LOG_RETRIEVED",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = get_config()
    home = Path.home()

    assert cfg.memory_db == home / ".better-memory" / "memory.db"
    assert cfg.knowledge_db == home / ".better-memory" / "knowledge.db"
    assert cfg.knowledge_base == home / "knowledge-base"
    assert cfg.spool_dir == home / ".better-memory" / "spool"
    assert cfg.ollama_host == "http://localhost:11434"
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.audit_log_retrieved is True


def test_env_overrides_are_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env vars override defaults; paths expand ``~`` and return ``Path``."""
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("KNOWLEDGE_BASE", "~/custom-kb")
    monkeypatch.setenv("SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("OLLAMA_HOST", "http://example:9999")
    monkeypatch.setenv("EMBED_MODEL", "some-other-model")
    monkeypatch.setenv("AUDIT_LOG_RETRIEVED", "false")

    cfg = get_config()

    assert cfg.memory_db == tmp_path / "m.db"
    assert cfg.knowledge_db == tmp_path / "k.db"
    assert cfg.knowledge_base == Path.home() / "custom-kb"
    assert cfg.spool_dir == tmp_path / "spool"
    assert cfg.ollama_host == "http://example:9999"
    assert cfg.embed_model == "some-other-model"
    assert cfg.audit_log_retrieved is False
    assert isinstance(cfg.memory_db, Path)
    assert isinstance(cfg.knowledge_base, Path)


def test_tmp_memory_db_fixture(tmp_memory_db: Path) -> None:
    """The ``tmp_memory_db`` fixture yields a non-existent Path."""
    assert isinstance(tmp_memory_db, Path)
    assert not tmp_memory_db.exists()


def test_tmp_knowledge_base_fixture(tmp_knowledge_base: Path) -> None:
    """The ``tmp_knowledge_base`` fixture yields an empty existing directory."""
    assert isinstance(tmp_knowledge_base, Path)
    assert tmp_knowledge_base.is_dir()
    assert list(tmp_knowledge_base.iterdir()) == []
