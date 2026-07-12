"""Meta-run: the e2e suite executed under a seeded hostile canary home
(design F: ``meta-canary-home-run``).

The single worst harness hazard, verified in source: ``install_hooks`` has
no ``--target`` flag — it writes ``Path.home()/.claude.json`` (line 339),
``~/.claude/settings.json`` and rmtree's skill dirs unconditionally. Any e2e
fixture that forgets to override USERPROFILE (Windows) or HOME (POSIX) would
therefore corrupt the real user's Claude config. This meta-test reruns the
suite in a subprocess whose HOME/USERPROFILE point at a seeded canary home
and proves, from outside, that nothing in the canary changed.

Judge fixes applied:

* The outer env starts from ``os.environ`` and **case-insensitively strips**
  every ``BETTER_MEMORY_*`` / ``CLAUDE_*`` / ``AWS_*`` / ``OLLAMA_*`` key
  (a dev shell exporting BETTER_MEMORY_HOME would otherwise shield an
  un-redirected fixture from the canary), then overrides
  HOME/USERPROFILE/HOMEDRIVE/HOMEPATH. ``BETTER_MEMORY_HOME`` is deliberately
  NOT set: a correctly isolated suite must not need outer-shell protection,
  so an un-redirected fixture falls back into ``<canary>/.better-memory``
  where it is caught.
* Explicit exit-5 vacuity assertion: suite deletion is reported as
  "meta-run is vacuous", not as a generic failure.

Inner-suite scoping contract (documented per the implementation task):

* ``BM_E2E_CANARY_INNER_SCOPE`` unset and ``CI`` unset → REDUCED default
  slice (fast local runs): ``test_install_hooks.py`` (the highest
  real-home-corruption-risk surface) + ``test_hooks_contracts.py`` (sync
  hook + MCP server spawns).
* ``BM_E2E_CANARY_INNER_SCOPE`` unset and ``CI`` set (every mainstream CI
  exports it) → the FULL ``tests/e2e`` suite — the actual isolation proof.
* ``BM_E2E_CANARY_INNER_SCOPE=full`` → full ``tests/e2e`` anywhere.
* ``BM_E2E_CANARY_INNER_SCOPE="<path> [<path> ...]"`` → explicit targets.

Sentinel assertions are decoupled from the inner exit code: each facet is
its own test against a module-scoped single run, so a failing inner suite
still gets its canary-integrity verdict reported separately.

The inner run's own ``real_home_canary`` fixture plants dot-canary files
(``.bm-e2e-canary-*``) inside the canary home (its ``Path.home()``); they
are self-deleting but excluded from the file-set diff for robustness
against an interrupted inner run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SCOPE_VAR = "BM_E2E_CANARY_INNER_SCOPE"
REDUCED_DEFAULT: tuple[str, ...] = (
    "tests/e2e/test_install_hooks.py",
    "tests/e2e/test_hooks_contracts.py",
)
STRIP_PREFIXES: tuple[str, ...] = ("BETTER_MEMORY_", "CLAUDE_", "AWS_", "OLLAMA_")

#: The inner fixture's own additive artifacts — excluded from the diff.
CANARY_FIXTURE_PREFIX = ".bm-e2e-canary"

#: Relative paths of every seeded sentinel file (sha256-pinned).
SENTINEL_FILES: tuple[str, ...] = (
    ".claude.json",
    ".claude/settings.json",
    ".claude/skills/user-skill/SKILL.md",
    ".better-memory/memory.db",
)

INNER_TIMEOUT = 540  # full tests/e2e comfortably fits; reduced slice ≪ this


def _set_ci(env: dict[str, str], key: str, value: str) -> None:
    """Set ``key``, removing case-insensitive duplicates first (Windows)."""
    for existing in [k for k in env if k.upper() == key.upper()]:
        del env[existing]
    env[key] = value


def _hostile_outer_env(canary: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES)}
    # PYTEST_ADDOPTS could inject -m filters/plugins into the inner run and
    # silently turn the meta-run vacuous — drop it.
    for key in [k for k in env if k.upper() == "PYTEST_ADDOPTS"]:
        del env[key]
    _set_ci(env, "HOME", str(canary))
    _set_ci(env, "USERPROFILE", str(canary))
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(str(canary))
        if drive:
            _set_ci(env, "HOMEDRIVE", drive)
            _set_ci(env, "HOMEPATH", tail or "\\")
    return env


def _inner_scope() -> list[str]:
    raw = os.environ.get(SCOPE_VAR, "").strip()
    if raw == "full":
        return ["tests/e2e"]
    if raw:
        return raw.split()
    if os.environ.get("CI"):
        return ["tests/e2e"]
    return list(REDUCED_DEFAULT)


def _sha_map(canary: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256((canary / rel).read_bytes()).hexdigest() for rel in SENTINEL_FILES
    }


def _path_set(canary: Path) -> set[str]:
    """Full relative path set (files AND dirs), inner-fixture canaries excluded."""
    return {
        p.relative_to(canary).as_posix()
        for p in canary.rglob("*")
        if not p.name.startswith(CANARY_FIXTURE_PREFIX)
    }


def _seed_canary_home(canary: Path) -> str:
    """Seed the hostile canary tree; returns the .claude.json uuid marker."""
    marker = str(uuid.uuid4())
    (canary / ".claude" / "skills" / "user-skill").mkdir(parents=True)
    (canary / ".better-memory").mkdir()
    (canary / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"user-precious": {"command": "sentinel"}}, "__canary": marker},
            indent=2,
        ),
        encoding="utf-8",
    )
    foreign_hook = {"type": "command", "command": "user-own-hook"}
    (canary / ".claude" / "settings.json").write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [foreign_hook]}]}},
            indent=2,
        ),
        encoding="utf-8",
    )
    (canary / ".claude" / "skills" / "user-skill" / "SKILL.md").write_text(
        f"user skill sentinel {uuid.uuid4()}\n", encoding="utf-8"
    )
    # Deliberately NOT a valid sqlite file: any inner code path that opens
    # the canary's memory.db errors loudly instead of silently mutating it.
    (canary / ".better-memory" / "memory.db").write_bytes(os.urandom(64))
    return marker


@dataclass(frozen=True)
class CanaryRun:
    canary: Path
    scope: list[str]
    rc: int
    stdout: str
    stderr: str
    marker: str
    pre_sha: dict[str, str]
    pre_paths: set[str]


@pytest.fixture(scope="module")
def canary_run(tmp_path_factory: pytest.TempPathFactory) -> CanaryRun:
    canary = tmp_path_factory.mktemp("canary-home")
    marker = _seed_canary_home(canary)
    pre_sha = _sha_map(canary)
    pre_paths = _path_set(canary)
    scope = _inner_scope()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            *scope,
        ],
        cwd=str(REPO_ROOT),
        env=_hostile_outer_env(canary),
        capture_output=True,
        text=True,
        timeout=INNER_TIMEOUT,
    )
    return CanaryRun(
        canary=canary,
        scope=scope,
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        marker=marker,
        pre_sha=pre_sha,
        pre_paths=pre_paths,
    )


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


class TestInnerRun:
    def test_inner_run_not_vacuous(self, canary_run: CanaryRun) -> None:
        """Exit-5 means pytest collected NOTHING — a deleted/renamed suite
        must be reported as vacuity, not mistaken for a green isolation proof
        (judge fix)."""
        assert canary_run.rc != 5, (
            f"{canary_run.scope} collected nothing — meta-run is vacuous\n"
            f"{_tail(canary_run.stdout)}"
        )

    def test_inner_suite_green_under_hostile_home(self, canary_run: CanaryRun) -> None:
        """The suite passes with HOME pointed at a hostile location — proves
        no test depends on (or requires protection of) the real home."""
        assert canary_run.rc == 0, (
            f"inner pytest rc={canary_run.rc} for scope {canary_run.scope}\n"
            f"--- stdout tail ---\n{_tail(canary_run.stdout)}\n"
            f"--- stderr tail ---\n{_tail(canary_run.stderr)}"
        )
        assert "passed" in canary_run.stdout, _tail(canary_run.stdout)


class TestCanaryIntegrity:
    """Sentinel facets — asserted regardless of the inner exit code."""

    def test_sentinel_files_byte_identical(self, canary_run: CanaryRun) -> None:
        post_sha = {}
        for rel in SENTINEL_FILES:
            path = canary_run.canary / rel
            assert path.exists(), f"seeded sentinel vanished: {rel}"
            post_sha[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        changed = [rel for rel in SENTINEL_FILES if post_sha[rel] != canary_run.pre_sha[rel]]
        assert not changed, (
            f"HARNESS ISOLATION BREACH: canary files mutated by the inner run: {changed}"
        )

    def test_file_set_diff_is_empty(self, canary_run: CanaryRun) -> None:
        """No leak artifacts appeared: no <canary>/.better-memory/
        install-backups or spool, no better-memory-* skill entries, nothing
        deleted."""
        post_paths = _path_set(canary_run.canary)
        created = sorted(post_paths - canary_run.pre_paths)
        deleted = sorted(canary_run.pre_paths - post_paths)
        assert created == [], f"inner run CREATED paths in the canary home: {created}"
        assert deleted == [], f"inner run DELETED paths from the canary home: {deleted}"

    def test_mcp_servers_semantics_preserved(self, canary_run: CanaryRun) -> None:
        """Parsed-JSON subset check: catches install_hooks leaking through a
        forgotten USERPROFILE even if serialization were byte-stable."""
        data = json.loads((canary_run.canary / ".claude.json").read_text(encoding="utf-8"))
        assert data["mcpServers"] == {"user-precious": {"command": "sentinel"}}
        assert "better-memory" not in data["mcpServers"]
        assert data["__canary"] == canary_run.marker

    def test_user_skill_dir_survives(self, canary_run: CanaryRun) -> None:
        """Catches install_skill_symlinks' backup-less rmtree escaping
        isolation."""
        skill = canary_run.canary / ".claude" / "skills" / "user-skill" / "SKILL.md"
        assert skill.is_file()
