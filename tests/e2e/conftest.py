"""Shared fixtures and subprocess helpers for the hermetic e2e suite.

Provides:

* ``clean_slate_home`` — a brand-new fake user home (empty directory).
* ``run_hook`` — spawn a better_memory hook module exactly as Claude Code
  would (stdin JSON payload, captured stdout/stderr).
* ``mcp_session`` — async context manager driving the real MCP stdio server
  via the ``mcp`` client SDK with hermetic env handling.
* auto-marking: every test collected under ``tests/e2e`` gets the ``e2e``
  marker (declared in pyproject) so module authors cannot forget it.

``real_home_canary`` (the autouse session tripwire from design section 1F)
lives at the bottom of this file: it plants additive dot-canaries in the
REAL home and semantically compares the better-memory config subtrees at
session teardown, turning a single hand-rolled-env spawn into an immediate
local failure instead of corrupted personal Claude config days later.

Import helpers from test modules as::

    from tests.e2e.conftest import mcp_session, run_hook
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_E2E_DIR = Path(__file__).resolve().parent

#: Default MCP server invocation (the real production entry point).
SERVER_ARGS: tuple[str, ...] = ("-m", "better_memory.mcp")

#: Generous read timeout: first boot runs migrations + knowledge reindex.
DEFAULT_READ_TIMEOUT = timedelta(seconds=60)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply the ``e2e`` marker to everything under tests/e2e."""
    for item in items:
        try:
            path = Path(str(item.path))
        except Exception:
            continue
        if _E2E_DIR in path.parents or path.parent == _E2E_DIR:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def clean_slate_home(tmp_path: Path) -> Path:
    """A truly clean-slate fake user home.

    The directory itself exists (a real user's home always does) but is
    completely empty: no ``.claude/``, no ``.claude.json``, no
    ``.better-memory/``. It is a *subdirectory* of ``tmp_path`` so tests can
    park side artifacts (errlog files, seeded repos, ...) in ``tmp_path``
    without polluting the fake home — several scenarios assert the home's
    exact file set.
    """
    home = tmp_path / "home"
    home.mkdir()
    return home


def run_hook(
    module: str,
    payload: dict[str, Any] | str | None,
    env: dict[str, str],
    *,
    cwd: Path | str | None = None,
    timeout: float = 60,
) -> tuple[int, str, str]:
    """Run a hook module as Claude Code does and return (rc, stdout, stderr).

    ``module`` is the dotted module path (e.g. ``better_memory.hooks.
    session_bootstrap``). ``payload`` is the hook's stdin: a dict is JSON
    encoded, a str is passed verbatim (for malformed-stdin cases), ``None``
    sends empty stdin. ``env`` MUST come from ``isolated_env`` /
    ``agentcore_env`` — enforced by the e2e_meta contract test.
    """
    if payload is None:
        stdin = ""
    elif isinstance(payload, str):
        stdin = payload
    else:
        stdin = json.dumps(payload)
    proc = subprocess.run(  # noqa: S603 — test harness, fixed argv
        [sys.executable, "-m", module],
        input=stdin,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


@asynccontextmanager
async def mcp_session(
    env: dict[str, str],
    *,
    errlog: IO[str] | None = None,
    cwd: Path | str | None = None,
    initialize: bool = True,
    read_timeout: timedelta = DEFAULT_READ_TIMEOUT,
    args: tuple[str, ...] = SERVER_ARGS,
) -> AsyncIterator[ClientSession]:
    """Drive the real MCP stdio server hermetically.

    * ``env`` MUST explicitly contain ``USERPROFILE`` and ``HOME``: on
      Windows the mcp SDK force-inherits the *real* USERPROFILE/APPDATA
      underneath ``server.env`` (``{**get_default_environment(),
      **server.env}`` in ``mcp/client/stdio/__init__.py``), so omitting them
      leaks the real home into the server. Guarded here so a bad env fails
      at the call site, not days later as corrupted personal config.
    * ``errlog`` must be a real file object with a working ``fileno()``
      (the SDK hands it to subprocess creation as the stderr handle);
      ``io.StringIO`` will NOT work. ``None`` inherits pytest's stderr.
    * ``cwd`` supports the marker-bridge tests (server resolves the project
      dir from its working directory when ``CLAUDE_PROJECT_DIR`` is unset).
    * ``initialize=False`` lets negative tests wrap ``session.initialize()``
      in ``pytest.raises(McpError)`` themselves (pre-handshake deaths
      surface as McpError code -32000 "Connection closed").
    """
    env_uppers = {k.upper() for k in env}
    missing = {"HOME", "USERPROFILE"} - env_uppers
    if missing:
        raise ValueError(
            f"mcp_session env is missing {sorted(missing)} — build it with "
            "isolated_env()/agentcore_env(); never hand-roll spawn envs."
        )
    if errlog is not None:
        errlog.fileno()  # raises for StringIO — must be a real OS-level file

    params = StdioServerParameters(
        command=sys.executable,
        args=list(args),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )
    stdio_kwargs: dict[str, Any] = {}
    if errlog is not None:
        stdio_kwargs["errlog"] = errlog

    async with stdio_client(params, **stdio_kwargs) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
            if initialize:
                await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# real_home_canary — autouse session tripwire (design section 1F)
# ---------------------------------------------------------------------------

#: Import-time environment snapshot. tests/conftest.py's autouse
#: ``_strip_leaked_claude_env`` deletes BOTH session-id vars before every
#: test, so live-Claude-Code-session detection MUST read the environment at
#: module import (collection) time, before any fixture can strip it. Both
#: var names are covered: Claude Code sets CLAUDE_CODE_SESSION_ID in shell
#: subprocess envs; older harness code reads CLAUDE_SESSION_ID.
_PRISTINE_SESSION_VARS: dict[str, str | None] = {
    "CLAUDE_SESSION_ID": os.environ.get("CLAUDE_SESSION_ID"),
    "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID"),
}
#: The real home, resolved once at import time from the pristine outer env.
_PRISTINE_REAL_HOME: Path = Path.home()

#: Canary filename prefix. Files are dot-prefixed, additive-only, and at
#: most two ~36-byte files linger if a run is hard-killed; the next run
#: silently overwrites them (never fails on stale canaries).
CANARY_PREFIX = ".bm-e2e-canary"


def _canary_read_json(path: Path) -> Any:
    """Parse JSON, folding all failure modes into stable sentinel strings.

    Unreadable-at-setup == unreadable-at-teardown compares equal, so a
    malformed real ~/.claude.json can never crash or false-fail the tripwire.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "<absent>"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"<unreadable: {type(exc).__name__}>"


def _canary_bm_mcp_server(claude_json: Any) -> Any:
    """The mcpServers['better-memory'] subtree (semantic, live-session-safe)."""
    if not isinstance(claude_json, dict):
        return claude_json
    servers = claude_json.get("mcpServers")
    if not isinstance(servers, dict):
        return servers
    return servers.get("better-memory")


def _canary_bm_hook_entries(settings_json: Any) -> Any:
    """Normalized better-memory hook entries per event from settings.json.

    Only entries whose command references better_memory are captured, so a
    concurrent live Claude session editing OTHER hooks cannot trip this,
    while a leaked install_hooks run (which rewrites the better-memory
    entries' command to a tmp venv interpreter, or adds them on a fresh box)
    always changes the subtree.
    """
    if not isinstance(settings_json, dict):
        return settings_json
    hooks = settings_json.get("hooks")
    if not isinstance(hooks, dict):
        return hooks
    out: dict[str, list[str]] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        entries: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks", []) or []:
                if not isinstance(entry, dict):
                    continue
                command = str(entry.get("command", ""))
                if "better_memory" in command or "better-memory" in command:
                    entries.append(json.dumps(entry, sort_keys=True))
        if entries:
            out[str(event)] = sorted(entries)
    return out


def _canary_entry_names(directory: Path) -> Any:
    """Sorted entry names of a real-home dir; canary files excluded."""
    try:
        if not directory.is_dir():
            return "<absent>"
        return sorted(p.name for p in directory.iterdir() if not p.name.startswith(CANARY_PREFIX))
    except OSError as exc:
        return f"<unreadable: {type(exc).__name__}>"


def _canary_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<absent>"


#: Values only the harness ever pins (tests/e2e/_env.py defaults). A live
#: Claude session writes spool files and DB rows constantly, but never with
#: these — fingerprint-filtered tell-tales are live-session-safe.
_HARNESS_FINGERPRINTS = ("e2e-session", "e2e-project")


def _canary_dataplane(home: Path) -> dict[str, Any]:
    """Data-plane tell-tales in the REAL ~/.better-memory (review finding
    isolation-canary-blind-to-dataplane-writes): a leaked hook/server spawn
    that kept the real USERPROFILE writes episode/exposure rows and spool or
    session-marker files carrying the harness's pinned session/project values.
    Config-subtree comparison alone is blind to that."""
    bm = home / ".better-memory"
    out: dict[str, Any] = {}

    db = bm / "memory.db"
    for key, sql in (
        (
            "db_episode_sessions",
            "SELECT COUNT(*) FROM episode_sessions WHERE session_id LIKE 'e2e-session%'",
        ),
        (
            "db_exposures",
            "SELECT COUNT(*) FROM session_memory_exposure WHERE session_id LIKE 'e2e-session%'",
        ),
    ):
        try:
            with closing(
                sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
            ) as conn:
                (out[key],) = conn.execute(sql).fetchone()
        except (sqlite3.Error, OSError):
            out[key] = "<n/a>"  # absent DB / locked / pre-migration schema

    hits: list[str] = []
    for sub in ("spool", "runtime/sessions"):
        directory = bm / Path(sub)
        if not directory.is_dir():
            continue
        for p in sorted(directory.iterdir()):
            if p.name.startswith(CANARY_PREFIX) or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(fp in text for fp in _HARNESS_FINGERPRINTS):
                hits.append(f"{sub}/{p.name}")
    out["fingerprinted_files"] = hits
    return out


def _real_home_snapshot(home: Path) -> dict[str, Any]:
    """Tell-tale state that only a leaked e2e subprocess would mutate."""
    claude_json = _canary_read_json(home / ".claude.json")
    settings_json = _canary_read_json(home / ".claude" / "settings.json")
    return {
        "mcp_bm": _canary_bm_mcp_server(claude_json),
        "hook_bm": _canary_bm_hook_entries(settings_json),
        "install_backups": _canary_entry_names(home / ".better-memory" / "install-backups"),
        "skills": _canary_entry_names(home / ".claude" / "skills"),
        "claude_json_sha": _canary_sha256(home / ".claude.json"),
        "settings_sha": _canary_sha256(home / ".claude" / "settings.json"),
        "dataplane": _canary_dataplane(home),
    }


def _bm_home_env_value(mcp_bm: Any) -> str | None:
    if not isinstance(mcp_bm, dict):
        return None
    env = mcp_bm.get("env")
    if not isinstance(env, dict):
        return None
    value = env.get("BETTER_MEMORY_HOME")
    return value if isinstance(value, str) else None


def _points_under_tempdir(value: str) -> bool:
    try:
        return Path(value).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


@pytest.fixture(scope="session", autouse=True)
def real_home_canary() -> Iterator[None]:
    """Session tripwire: no e2e test may touch the REAL user home.

    Mechanism (judge-fixed spec, design section 1F):

    * Plants two additive dot-canary files (uuid4 content) in the real
      ``~/.claude/skills/`` and ``~/.better-memory/`` — only where the
      parent ALREADY exists (never creates those dirs on a machine that
      lacks them). Filenames carry the xdist worker id so parallel workers
      cannot collide; stale canaries from a killed run are overwritten.
    * Snapshots semantic tell-tales at setup and compares at teardown:
      the parsed mcpServers['better-memory'] subtree, the better-memory
      hook entries in settings.json, the install-backups entry set, and
      the skills entry-name set. Semantic (not byte-hash) comparison is
      live-session-safe: a concurrent Claude Code session rewrites
      ~/.claude.json routinely but never rewrites the better-memory
      subtrees to a tmp venv, never adds install-backups entries, and
      never touches dot-prefixed canaries.
    * Smoking gun: mcpServers['better-memory'].env.BETTER_MEMORY_HOME
      pointing under tempfile.gettempdir() is a leaked install_hooks run.
    * Whole-file hash mismatch WITHOUT a semantic tell-tale is demoted to
      a warning (and suppressed entirely inside a live Claude session).

    KNOWN BLIND SPOT (documented per judge round): on a machine where
    better-memory is already installed with the same interpreter paths, a
    leaked install_hooks run is semantically idempotent and only the
    (demoted) hash warning would notice. That case is owned by the
    whole-suite meta-run in tests/e2e_meta/test_canary_home.py, which runs
    against a fresh seeded canary home where any leak is loud.

    Data-plane coverage (review fix): the snapshot also fingerprints the
    real ``~/.better-memory`` data plane — episode/exposure rows and
    spool / runtime-session files carrying the harness-pinned
    ``e2e-session*`` / ``e2e-project`` values — so a leaked hook or server
    spawn that writes the user's real memory.db no longer goes unnoticed
    just because the config subtrees stayed intact.
    """
    home = _PRISTINE_REAL_HOME
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    canary_name = f"{CANARY_PREFIX}-{worker}"
    live_session = any(_PRISTINE_SESSION_VARS.values())

    planted: dict[Path, str] = {}
    for parent in (home / ".claude" / "skills", home / ".better-memory"):
        if not parent.is_dir():
            continue  # record 'absent' — NEVER create real-home dirs
        target = parent / canary_name
        content = str(uuid.uuid4())
        try:
            target.write_text(content, encoding="utf-8")  # overwrite stale
        except OSError:
            continue  # unwritable home (locked-down CI) — skip this canary
        planted[target] = content

    before = _real_home_snapshot(home)

    yield

    after = _real_home_snapshot(home)
    breaches: list[str] = []

    for target, content in planted.items():
        try:
            actual = target.read_text(encoding="utf-8")
        except OSError:
            breaches.append(f"canary file deleted or unreadable: {target}")
            continue
        if actual != content:
            breaches.append(f"canary file overwritten: {target}")

    for key, label in (
        ("mcp_bm", "~/.claude.json mcpServers['better-memory']"),
        ("hook_bm", "~/.claude/settings.json better-memory hook entries"),
        ("install_backups", "~/.better-memory/install-backups entries"),
        ("skills", "~/.claude/skills entry names"),
        ("dataplane", "~/.better-memory data-plane harness fingerprints"),
    ):
        if before[key] != after[key]:
            breaches.append(
                f"{label} changed during the e2e session:\n"
                f"  setup:    {before[key]!r}\n"
                f"  teardown: {after[key]!r}"
            )

    # Smoking gun: a tmp-path BETTER_MEMORY_HOME in the real config.
    after_bm_home = _bm_home_env_value(after["mcp_bm"])
    if after_bm_home is not None and _points_under_tempdir(after_bm_home):
        message = (
            "real ~/.claude.json mcpServers['better-memory'].env.BETTER_MEMORY_HOME "
            f"points under tempfile.gettempdir(): {after_bm_home!r}"
        )
        if after_bm_home == _bm_home_env_value(before["mcp_bm"]):
            # Pre-existing damage from an earlier leak — this run did not do
            # it; warn instead of permanently bricking every suite run.
            warnings.warn(
                f"PRE-EXISTING real-home damage (not caused by this run): {message}",
                stacklevel=1,
            )
        else:
            breaches.append(f"SMOKING GUN (leaked install_hooks run): {message}")

    # Byte-hash drift with NO semantic tell-tale: warning only (a live
    # Claude session rewrites these files routinely — suppress there).
    hash_drift = [
        name
        for name, key in ((".claude.json", "claude_json_sha"), ("settings.json", "settings_sha"))
        if before[key] != after[key]
    ]
    if hash_drift and not breaches and not live_session:
        warnings.warn(
            "real-home file bytes changed without a semantic better-memory "
            f"tell-tale: {hash_drift} — not treated as a breach (see fixture "
            "docstring); investigate if it repeats on quiescent machines.",
            stacklevel=1,
        )

    for target in planted:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass

    if breaches:
        pytest.fail(
            "HARNESS ISOLATION BREACH — an e2e test touched the REAL user "
            "home. Every subprocess env must come from isolated_env()/"
            "agentcore_env(); details:\n" + "\n".join(breaches),
            pytrace=False,
        )
