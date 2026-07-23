"""Tests for :mod:`better_memory.config`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from better_memory.config import get_config, project_name


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=str(path), check=True)


def _fake_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``~`` (``Path.home()`` / ``expanduser``) at a tmp dir.

    Tests that ``delenv("BETTER_MEMORY_HOME")`` to exercise the default-home
    branch must NOT resolve against the developer's real home: ``get_config``
    reads ``<home>/settings.json`` for the storage backend, so an un-isolated
    default-home test reads (and depends on) the developer's REAL
    ``~/.better-memory/settings.json``. Both ``HOME`` (POSIX) and
    ``USERPROFILE`` (Windows, checked first by ``ntpath.expanduser``) are
    patched so ``Path.home()`` and ``Path("~/...").expanduser()`` agree.
    """
    fake_home = tmp_path / "fake-user-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


def test_defaults_resolve_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no env vars set, everything lands under ``~/.better-memory``."""
    fake_home = _fake_user_home(monkeypatch, tmp_path)
    for var in (
        "BETTER_MEMORY_HOME",
        "OLLAMA_HOST",
        "EMBED_MODEL",
        "AUDIT_LOG_RETRIEVED",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = get_config()
    home = Path.home() / ".better-memory"
    # Isolation guard: the default home is the FAKE user home, not the
    # developer's real one.
    assert cfg.home == fake_home / ".better-memory"

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


def test_home_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``BETTER_MEMORY_HOME`` expands ``~`` to the user's home directory."""
    fake_home = _fake_user_home(monkeypatch, tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_HOME", "~/custom-bm")

    cfg = get_config()
    assert cfg.home == fake_home / "custom-bm"
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


def test_paths_are_path_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All path fields are :class:`pathlib.Path`, not strings."""
    _fake_user_home(monkeypatch, tmp_path)
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
    """The env var alone selects agentcore — no memory-id env vars required.

    The vestigial idvar gate (BETTER_MEMORY_AGENTCORE_{SEMANTIC,EPISODIC}_MEMORY_ID)
    is deleted; runtime memory ids come exclusively from agentcore.json via the
    storage factory.
    """
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    cfg = get_config()
    assert cfg.storage_backend == "agentcore"


def test_storage_backend_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "nope")
    with pytest.raises(ValueError, match="BETTER_MEMORY_STORAGE_BACKEND"):
        get_config()


# ---------------------------------------------------------------------------
# $BETTER_MEMORY_HOME/settings.json storage_backend resolution
# ---------------------------------------------------------------------------


def _pin_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point BETTER_MEMORY_HOME at a fresh tmp dir and return it."""
    home = tmp_path / "bm"
    home.mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    return home


def _write_settings(home: Path, text: str) -> None:
    (home / "settings.json").write_text(text, encoding="utf-8")


def test_storage_backend_no_env_no_settings_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Byte-identical default: no env var, no settings.json → sqlite."""
    _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    assert get_config().storage_backend == "sqlite"


def test_storage_backend_settings_file_selects_agentcore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """settings.json {"storage_backend": "agentcore"} + no env → agentcore."""
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, '{"storage_backend": "agentcore"}')
    assert get_config().storage_backend == "agentcore"


@pytest.mark.parametrize(
    "env_value,file_value,expected",
    [
        ("sqlite", "agentcore", "sqlite"),
        ("agentcore", "sqlite", "agentcore"),
    ],
)
def test_storage_backend_env_wins_over_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_value: str,
    file_value: str,
    expected: str,
) -> None:
    """BETTER_MEMORY_STORAGE_BACKEND beats settings.json in both directions."""
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", env_value)
    _write_settings(home, f'{{"storage_backend": "{file_value}"}}')
    assert get_config().storage_backend == expected


def test_storage_backend_settings_without_key_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A settings.json without a storage_backend key falls back to sqlite."""
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, '{"unrelated": true}')
    assert get_config().storage_backend == "sqlite"


def test_storage_backend_settings_malformed_json_raises_naming_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, "{not valid json")
    with pytest.raises(ValueError, match="settings.json") as excinfo:
        get_config()
    msg = str(excinfo.value)
    assert str(home / "settings.json") in msg
    assert "BETTER_MEMORY_STORAGE_BACKEND" in msg  # remediation: env override


def test_storage_backend_settings_non_object_raises_naming_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, '["storage_backend"]')
    with pytest.raises(ValueError, match="settings.json"):
        get_config()


def test_storage_backend_settings_invalid_value_raises_naming_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invalid storage_backend in settings.json errors symmetrically to the env var."""
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, '{"storage_backend": "bogus"}')
    with pytest.raises(ValueError, match="storage_backend") as excinfo:
        get_config()
    msg = str(excinfo.value)
    assert str(home / "settings.json") in msg
    assert "'bogus'" in msg
    assert "sqlite" in msg and "agentcore" in msg  # valid values listed


def test_storage_backend_settings_unreadable_oserror_falls_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any OSError reading settings.json (not just FileNotFoundError) falls
    back to the default instead of crashing get_config.

    A directory named ``settings.json`` makes ``read_text`` raise
    ``PermissionError`` on Windows / ``IsADirectoryError`` on POSIX — both
    OSError subclasses. Before the broadening, only FileNotFoundError was
    caught and get_config crashed on an unreadable file.
    """
    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    (home / "settings.json").mkdir()

    assert get_config().storage_backend == "sqlite"


def test_storage_backend_settings_permission_error_falls_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit PermissionError (the live-found crash) returns the default;
    ValueError semantics for a READABLE-but-malformed file are untouched
    (covered by the malformed/non-object/invalid-value tests above)."""
    from better_memory.config import _read_settings_storage_backend

    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    _write_settings(home, '{"storage_backend": "agentcore"}')

    original_read_text = Path.read_text

    def _deny(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "settings.json":
            raise PermissionError(13, "Permission denied", str(self))
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _deny)
    assert _read_settings_storage_backend(home) is None
    assert get_config().storage_backend == "sqlite"


def test_resolve_storage_backend_public_helper_re_resolves_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """resolve_storage_backend() is exported for hooks and never memoized —
    editing settings.json between calls takes effect immediately."""
    from better_memory.config import resolve_storage_backend

    home = _pin_home(monkeypatch, tmp_path)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    assert resolve_storage_backend() == "sqlite"
    _write_settings(home, '{"storage_backend": "agentcore"}')
    assert resolve_storage_backend() == "agentcore"
    _write_settings(home, '{"storage_backend": "sqlite"}')
    assert resolve_storage_backend() == "sqlite"


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


@pytest.mark.parametrize("val,expected", [
    (None, "both"), ("userprompt", "userprompt"), ("pretool", "pretool"),
    ("both", "both"), ("off", "off"),
])
def test_context_inject_mode_valid(monkeypatch, val, expected):
    from better_memory import config as cfg_mod
    if val is None:
        monkeypatch.delenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", raising=False)
    else:
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", val)
    assert cfg_mod._resolve_context_inject_mode() == expected


def test_context_inject_mode_invalid(monkeypatch):
    from better_memory import config as cfg_mod
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_INJECT_MODE", "bogus")
    with pytest.raises(ValueError):
        cfg_mod._resolve_context_inject_mode()


def test_injection_knobs_defaults(monkeypatch):
    for var in (
        "BETTER_MEMORY_BOOTSTRAP_TOP_N",
        "BETTER_MEMORY_CONTEXT_MIN_HITS",
        "BETTER_MEMORY_CONTEXT_MAX_ITEMS",
        "BETTER_MEMORY_CONTEXT_REINJECT_TURNS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = get_config()
    assert cfg.bootstrap_top_n == 5
    assert cfg.context_min_hits == 2
    assert cfg.context_max_items == 3
    assert cfg.context_reinject_turns == 0


def test_injection_knobs_env_override(monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_BOOTSTRAP_TOP_N", "0")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "1")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MAX_ITEMS", "5")
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_REINJECT_TURNS", "20")
    cfg = get_config()
    assert cfg.bootstrap_top_n == 0
    assert cfg.context_min_hits == 1
    assert cfg.context_max_items == 5
    assert cfg.context_reinject_turns == 20


def test_injection_knobs_invalid_raises(monkeypatch):
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "banana")
    with pytest.raises(ValueError):
        get_config()
    monkeypatch.setenv("BETTER_MEMORY_CONTEXT_MIN_HITS", "-1")
    with pytest.raises(ValueError):
        get_config()


class TestInjectModeConfig:
    def test_default_is_legacy(self, monkeypatch):
        monkeypatch.delenv("BETTER_MEMORY_INJECT_MODE", raising=False)
        assert get_config().inject_mode == "legacy"

    def test_deferred_selected(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        assert get_config().inject_mode == "deferred"

    def test_unknown_coerces_to_legacy(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "yolo")
        assert get_config().inject_mode == "legacy"


class TestVecFloorConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", raising=False)
        assert get_config().context_vec_floor == 0.55

    def test_override_and_clamp(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "0.7")
        assert get_config().context_vec_floor == 0.7
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "1.7")
        assert get_config().context_vec_floor == 1.0

    def test_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_CONTEXT_VEC_FLOOR", "high")
        assert get_config().context_vec_floor == 0.55
