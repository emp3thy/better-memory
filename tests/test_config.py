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


def test_config_auto_prune_defaults_false(monkeypatch) -> None:
    """Config.auto_prune defaults to False when env var unset."""
    monkeypatch.delenv("BETTER_MEMORY_AUTO_PRUNE", raising=False)
    from better_memory.config import get_config
    cfg = get_config()
    assert cfg.auto_prune is False


def test_config_auto_prune_true_when_env_set(monkeypatch) -> None:
    """Config.auto_prune is True when BETTER_MEMORY_AUTO_PRUNE=1."""
    monkeypatch.setenv("BETTER_MEMORY_AUTO_PRUNE", "1")
    from better_memory.config import get_config
    cfg = get_config()
    assert cfg.auto_prune is True


def test_project_name_defaults_to_cwd_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no override file, project_name returns cwd's leaf name."""
    cwd = tmp_path / "my-service"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    from better_memory.config import project_name
    assert project_name() == "my-service"


def test_project_name_explicit_cwd(tmp_path: Path) -> None:
    """When cwd is passed explicitly, it is used over Path.cwd()."""
    cwd = tmp_path / "explicit"
    cwd.mkdir()
    from better_memory.config import project_name
    assert project_name(cwd) == "explicit"


def test_project_name_override_file_wins(tmp_path: Path) -> None:
    """A .better-memory file in cwd overrides the directory name."""
    cwd = tmp_path / "renamed"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("canonical-name\n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "canonical-name"


def test_project_name_empty_override_falls_back(tmp_path: Path) -> None:
    """An empty override file falls back to cwd.name."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "leaf"


def test_project_name_whitespace_only_override_falls_back(tmp_path: Path) -> None:
    """A whitespace-only override file falls back to cwd.name."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("   \n  \n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "leaf"


def test_project_name_multi_line_override_takes_first_non_empty(
    tmp_path: Path,
) -> None:
    """A multi-line override takes the first non-empty stripped line."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text(
        "  first-line  \nignored-second\n", encoding="utf-8"
    )
    from better_memory.config import project_name
    assert project_name(cwd) == "first-line"
