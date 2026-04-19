"""Tests for :mod:`better_memory.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.config import get_config


def test_defaults_resolve_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env vars set, everything lands under ``~/.better-memory``."""
    for var in (
        "BETTER_MEMORY_HOME",
        "OLLAMA_HOST",
        "EMBED_MODEL",
        "AUDIT_LOG_RETRIEVED",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = get_config()
    home = Path.home() / ".better-memory"

    assert cfg.home == home
    assert cfg.memory_db == home / "memory.db"
    assert cfg.knowledge_db == home / "knowledge.db"
    assert cfg.knowledge_base == home / "knowledge-base"
    assert cfg.spool_dir == home / "spool"
    assert cfg.ollama_host == "http://localhost:11434"
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.audit_log_retrieved is True


def test_home_override_roots_all_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Setting ``BETTER_MEMORY_HOME`` reroots every derived path."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / "bm"))

    cfg = get_config()
    root = tmp_path / "bm"

    assert cfg.home == root
    assert cfg.memory_db == root / "memory.db"
    assert cfg.knowledge_db == root / "knowledge.db"
    assert cfg.knowledge_base == root / "knowledge-base"
    assert cfg.spool_dir == root / "spool"


def test_home_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BETTER_MEMORY_HOME`` expands ``~`` to the user's home directory."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", "~/custom-bm")

    cfg = get_config()
    assert cfg.home == Path.home() / "custom-bm"
    assert cfg.memory_db == Path.home() / "custom-bm" / "memory.db"


def test_external_service_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """External-service env vars override independently of path layout."""
    monkeypatch.setenv("OLLAMA_HOST", "http://example:9999")
    monkeypatch.setenv("EMBED_MODEL", "some-other-model")
    monkeypatch.setenv("AUDIT_LOG_RETRIEVED", "false")

    cfg = get_config()

    assert cfg.ollama_host == "http://example:9999"
    assert cfg.embed_model == "some-other-model"
    assert cfg.audit_log_retrieved is False


def test_paths_are_path_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """All path fields are :class:`pathlib.Path`, not strings."""
    monkeypatch.delenv("BETTER_MEMORY_HOME", raising=False)
    cfg = get_config()
    for attr in ("home", "memory_db", "knowledge_db", "knowledge_base", "spool_dir"):
        assert isinstance(getattr(cfg, attr), Path), attr


def test_tmp_memory_db_fixture(tmp_memory_db: Path) -> None:
    """The ``tmp_memory_db`` fixture yields a non-existent Path."""
    assert isinstance(tmp_memory_db, Path)
    assert not tmp_memory_db.exists()


def test_tmp_knowledge_base_fixture(tmp_knowledge_base: Path) -> None:
    """The ``tmp_knowledge_base`` fixture yields an empty existing directory."""
    assert isinstance(tmp_knowledge_base, Path)
    assert tmp_knowledge_base.is_dir()
    assert list(tmp_knowledge_base.iterdir()) == []


def test_consolidate_model_defaults_to_llama3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONSOLIDATE_MODEL", raising=False)
    cfg = get_config()
    assert cfg.consolidate_model == "llama3"


def test_consolidate_model_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLIDATE_MODEL", "mistral")
    cfg = get_config()
    assert cfg.consolidate_model == "mistral"
