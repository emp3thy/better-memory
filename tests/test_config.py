"""Tests for :mod:`better_memory.config`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from better_memory.config import get_config, project_name


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=str(path), check=True)


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


def test_project_name_defaults_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no override and no git tree, project_name falls back to 'general'.

    Also exercises the no-arg form (defaults to ``Path.cwd()``).
    """
    cwd = tmp_path / "my-service"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    from better_memory.config import project_name
    assert project_name() == "general"


def test_project_name_explicit_cwd_used_over_path_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When cwd is passed explicitly, it is used over ``Path.cwd()``."""
    process_cwd = tmp_path / "process-cwd"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _git_init(explicit)

    from better_memory.config import project_name
    assert project_name(explicit) == "explicit"


def test_project_name_override_file_wins(tmp_path: Path) -> None:
    """A .better-memory file in cwd overrides the directory name."""
    cwd = tmp_path / "renamed"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("canonical-name\n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "canonical-name"


def test_project_name_empty_override_falls_back(tmp_path: Path) -> None:
    """An empty override file falls back through to git/general resolution."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "general"


def test_project_name_whitespace_only_override_falls_back(tmp_path: Path) -> None:
    """A whitespace-only override file falls back through to git/general resolution."""
    cwd = tmp_path / "leaf"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("   \n  \n", encoding="utf-8")
    from better_memory.config import project_name
    assert project_name(cwd) == "general"


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


def test_project_name_in_git_repo_root_returns_repo_dir_name(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)

    assert project_name(repo) == "myrepo"


def test_project_name_in_subdirectory_walks_to_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    assert project_name(sub) == "myrepo"


def test_project_name_outside_git_returns_general(tmp_path: Path) -> None:
    nongit = tmp_path / "loose"
    nongit.mkdir()

    assert project_name(nongit) == "general"


def test_project_name_in_worktree_returns_main_repo_name(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    # Need at least one commit for `git worktree add` to succeed.
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init", "--quiet"],
        cwd=str(repo), check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "feat", "--quiet"],
        cwd=str(repo), check=True,
    )

    assert project_name(worktree) == "myrepo"


def test_project_name_override_file_beats_git(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    (repo / ".better-memory").write_text("override-name\n")

    assert project_name(repo) == "override-name"


def test_project_name_env_var_wins_over_file_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BETTER_MEMORY_PROJECT env var takes precedence over .better-memory file."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", "from-env")
    assert project_name(cwd) == "from-env"


def test_project_name_env_var_wins_over_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BETTER_MEMORY_PROJECT env var takes precedence over git resolution."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", "from-env")
    assert project_name(repo) == "from-env"


def test_project_name_empty_env_var_falls_through_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty BETTER_MEMORY_PROJECT is treated as unset; file override still wins."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", "")
    assert project_name(cwd) == "from-file"


def test_project_name_whitespace_only_env_var_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only BETTER_MEMORY_PROJECT is treated as unset."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", "   \t\n  ")
    assert project_name(cwd) == "from-file"


def test_project_name_env_var_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surrounding whitespace in BETTER_MEMORY_PROJECT is stripped."""
    monkeypatch.setenv("BETTER_MEMORY_PROJECT", "  scoped-name  \n")
    assert project_name(tmp_path) == "scoped-name"


def test_embeddings_backend_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", raising=False)
    cfg = get_config()
    assert cfg.embeddings_backend == "ollama"


def test_embeddings_backend_sqlite_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    cfg = get_config()
    assert cfg.embeddings_backend == "sqlite"


def test_embeddings_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # `tfidf` was valid in PR #65, no longer accepted after the rename.
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "tfidf")
    with pytest.raises(ValueError, match="BETTER_MEMORY_EMBEDDINGS_BACKEND"):
        get_config()


def test_storage_backend_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    cfg = get_config()
    assert cfg.storage_backend == "sqlite"


def test_storage_backend_agentcore_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", "mem-sem-abc1234567")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", "mem-epi-xyz1234567")
    cfg = get_config()
    assert cfg.storage_backend == "agentcore"
    assert cfg.agentcore_region == "eu-west-2"
    assert cfg.agentcore_semantic_memory_id == "mem-sem-abc1234567"
    assert cfg.agentcore_episodic_memory_id == "mem-epi-xyz1234567"


def test_storage_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "nope")
    with pytest.raises(ValueError, match="BETTER_MEMORY_STORAGE_BACKEND"):
        get_config()


def test_agentcore_mode_without_memory_ids_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.delenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", raising=False)
    monkeypatch.delenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", raising=False)
    with pytest.raises(ValueError, match="agentcore init"):
        get_config()


@pytest.mark.parametrize(
    "set_var,unset_var",
    [
        (
            "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID",
            "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID",
        ),
        (
            "BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID",
            "BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID",
        ),
    ],
)
def test_agentcore_mode_with_only_one_memory_id_raises(
    monkeypatch: pytest.MonkeyPatch, set_var: str, unset_var: str
) -> None:
    """Setting only one of the two memory IDs still trips the cross-field check.

    The validation uses ``or``, so each side of the disjunction needs coverage.
    """
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv(set_var, "mem-only-one-1234567")
    monkeypatch.delenv(unset_var, raising=False)
    with pytest.raises(ValueError, match="agentcore init"):
        get_config()


def test_agentcore_region_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_REGION", "us-west-2")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_SEMANTIC_MEMORY_ID", "mem-sem-abc1234567")
    monkeypatch.setenv("BETTER_MEMORY_AGENTCORE_EPISODIC_MEMORY_ID", "mem-epi-xyz1234567")
    cfg = get_config()
    assert cfg.agentcore_region == "us-west-2"


def test_project_name_caches_negative_lookup(tmp_path: Path) -> None:
    """A 'no git tree' result is cached so repeat lookups don't re-walk.

    Replaces the older ``test_project_name_does_not_cache_subprocess_failure``
    test. The subprocess fallback (and its transient-hiccup failure mode) is
    gone — ``_walk_for_git_root`` is deterministic given filesystem state,
    so caching ``None`` is safe. The bug debugged 2026-05-13 (subprocess
    timeout cleanup hanging for ~65 s on Windows) is fixed at the root by
    eliminating the subprocess entirely; the cache poisoning rationale no
    longer applies.
    """
    import better_memory.config as bm_config

    nongit = tmp_path / "loose"
    nongit.mkdir()
    resolved = str(nongit.resolve())
    bm_config._git_project_cache.pop(resolved, None)

    assert bm_config.project_name(nongit) == "general"
    assert resolved in bm_config._git_project_cache
    assert bm_config._git_project_cache[resolved] is None
