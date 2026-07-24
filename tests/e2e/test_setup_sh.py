"""E2E scenarios B1-B4: ``scripts/setup.sh`` (design section 1.B).

Scenarios (2026-07-12-e2e-clean-slate-smoke-design.md, catalog 1.B):

* **B1** ``e2e-setup-headless-decline-completes`` — headless run to
  completion: layout dirs, install_hooks invoked, win_path-correct command
  in ``.claude.json``, py/pyw split survives the bash->argparse round-trip,
  progress markers in order.
* **B2** ``e2e-setup-eof-aborts-all-or-nothing`` — closed stdin dies at the
  Ollama ``read -rp`` under ``set -euo pipefail`` BEFORE layout and
  install_hooks (product bug #11 pinned as all-or-nothing). Self-skips on
  hosts where ollama is detectable (this dev machine); runs on ollama-free
  CI.
* **B3** ``e2e-setup-default-home-derivation`` — ``BETTER_MEMORY_HOME``
  absent: the ``${VAR:-$HOME/.better-memory}`` default is derived and
  propagated via ``--home`` into the ``mcpServers`` env block and the
  install-backups location.
* **B4** ``e2e-setup-install-hooks-failure-propagates`` — malformed seeded
  ``.claude.json``: exit 1, remediation text, no final ``Done.``, layout
  stage completed (stage-order pin), target files untouched.

Containment (design harness gap-1, embedded here): every run redirects
``UV_PROJECT_ENVIRONMENT`` to a module-shared tmp venv and pins
``UV_CACHE_DIR``/``UV_PYTHON_INSTALL_DIR`` to the *host's* warm locations
(isolated_env strips LOCALAPPDATA/APPDATA, which uv derives them from — an
unpinned run would use a cold cache under the fake home and hit the
network). The real repo ``.venv/pyvenv.cfg`` is hashed before/after every
run. The first run in the module bears one real ``uv sync`` into the tmp
venv (warm-cache); later runs are no-ops against the same venv.

Network shims: ``curl`` (always exits 1) and ``ollama`` (no-op exit 0) are
**exported bash functions** injected by a driver script, not PATH files —
Git Bash prepends ``/mingw64/bin``/``/usr/bin`` to any inherited PATH, so a
PATH shim can never shadow the real MSYS curl (verified empirically).
Functions shadow everything. The failing curl forces the daemon-unreachable
warn-and-continue branch (setup.sh:165-172) on every host, so
``ollama pull`` can never run — no network, no dependence on host daemon
state.

skipif: bash absent (contract test D in tests/e2e_meta checks for the
``shutil.which("bash")`` probe) or uv absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e._env import isolated_env

REPO = Path(__file__).resolve().parents[2]
SETUP_SH = REPO / "scripts" / "setup.sh"
_WIN = sys.platform == "win32"

#: setup.sh:107-111 — the interpreters whose (win_path-converted) paths the
#: installer writes into the config files.
VENV_PY = REPO / ".venv" / ("Scripts/python.exe" if _WIN else "bin/python")
VENV_PYW = REPO / ".venv" / ("Scripts/pythonw.exe" if _WIN else "bin/python")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _find_bash() -> str | None:
    """Locate a Git-Bash-flavored bash.

    On Windows, ``shutil.which("bash")`` is a trap twice over: it can find
    WSL's ``System32\\bash.exe`` (a different OS) or stray shims (this dev
    machine has a ``bash.exe`` inside a Python Scripts dir). Prefer paths
    derived from the git installation, fall back to which() minus System32.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe -> <Git>
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(var)
        if root:
            candidates.append(Path(root) / "Git" / "bin" / "bash.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        return which
    return None


BASH = _find_bash()

pytestmark = [
    pytest.mark.skipif(BASH is None, reason="bash (Git Bash / POSIX) not available"),
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH"),
]


def _posix(p: Path) -> str:
    """Forward-slash form — safe to hand to bash as a script argument."""
    return str(p).replace("\\", "/")


def _host_uv_dirs() -> dict[str, str]:
    """The host's real (warm) uv cache + managed-python dirs.

    Computed from the outer environment instead of spawning ``uv cache dir``
    (spawns must carry an isolated env, which would strip the very variables
    the answer depends on). Mirrors uv's documented resolution: explicit env
    var, else LOCALAPPDATA/APPDATA on Windows, XDG dirs elsewhere.
    """
    cache = os.environ.get("UV_CACHE_DIR")
    pydir = os.environ.get("UV_PYTHON_INSTALL_DIR")
    if not cache:
        if _WIN:
            local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            cache = str(Path(local) / "uv" / "cache")
        else:
            xdg = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
            cache = str(Path(xdg) / "uv")
    if not pydir:
        if _WIN:
            roaming = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
            pydir = str(Path(roaming) / "uv" / "python")
        else:
            xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            pydir = str(Path(xdg_data) / "uv" / "python")
    return {"UV_CACHE_DIR": cache, "UV_PYTHON_INSTALL_DIR": pydir}


def _base_python() -> str:
    """A concrete non-shim CPython — the base interpreter behind pytest's venv.

    setup.sh's ``python``/``python3`` version probe and uv's interpreter
    discovery must never execute PATH *shims*: on Windows hosts with the
    Python 3.14 install manager, invoking its shim under an env with
    LOCALAPPDATA stripped makes it auto-install a full CPython runtime into
    ``<cwd>/Python`` and Start Menu links under the fake profile (observed
    empirically — potentially a network download per run).
    """
    base = Path(sys.base_prefix)
    candidates = (
        [base / "python.exe"] if _WIN else [base / "bin" / "python3", base / "bin" / "python"]
    )
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return sys.executable


def _pyvenv_state() -> bytes | None:
    """Fingerprint of the real repo venv (None when absent, e.g. bare CI)."""
    cfg = REPO / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        return None
    return hashlib.sha256(cfg.read_bytes()).digest()


def _clean(text: str) -> str:
    return _ANSI.sub("", text)


@dataclass(frozen=True)
class SetupRun:
    rc: int
    stdout: str
    stderr: str
    home: Path
    cwd: Path
    bash_version: str

    @property
    def combined(self) -> str:
        return self.stdout + "\n" + self.stderr

    def lines(self) -> list[str]:
        return _clean(self.combined).splitlines()


def _run_setup(
    env: dict[str, str],
    *,
    home: Path,
    work: Path,
    stdin: str | None,
    ollama_shim: bool,
    timeout: float = 600,
) -> SetupRun:
    """Run scripts/setup.sh through the shim driver and return the outcome.

    ``stdin=None`` means closed stdin (EOF at the first ``read``); a string
    is piped verbatim. ``ollama_shim=False`` leaves ``ollama`` undefined so
    the interactive prompt branch (setup.sh:130-155) is reachable.

    Containment is asserted around every run: the real repo
    ``.venv/pyvenv.cfg`` must be byte-identical afterwards (setup.sh runs
    ``uv sync`` against the repo — UV_PROJECT_ENVIRONMENT must have
    redirected it).
    """
    pybin = _posix(Path(_base_python()))
    shim_lines = [
        "curl() { return 1; }",
        f'python() {{ "{pybin}" "$@"; }}',
        f'python3() {{ "{pybin}" "$@"; }}',
        "export -f curl python python3",
    ]
    if ollama_shim:
        shim_lines += ["ollama() { return 0; }", "export -f ollama"]
    driver = work / "driver.sh"
    driver.write_text(
        "\n".join(
            [
                *shim_lines,
                'echo "bm-e2e-bash-version=$BASH_VERSION" >&2',
                'exec bash "$1"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    cwd = work / "cwd"
    cwd.mkdir(exist_ok=True)

    before = _pyvenv_state()
    assert BASH is not None  # pytestmark skipif guarantees this
    proc = subprocess.run(  # noqa: S603 — test harness, fixed argv
        [BASH, _posix(driver), _posix(SETUP_SH)],
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    after = _pyvenv_state()
    assert after == before, (
        "CONTAINMENT BREACH: scripts/setup.sh modified the real repo "
        ".venv/pyvenv.cfg — UV_PROJECT_ENVIRONMENT redirect is broken"
    )

    match = re.search(r"bm-e2e-bash-version=(\S+)", proc.stderr)
    return SetupRun(
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        home=home,
        cwd=cwd,
        bash_version=match.group(1) if match else "unknown",
    )


# ---------------------------------------------------------------------------
# Shared uv containment + the single uv-sync-bearing B1 run
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def uv_pins(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Env pins shared by every setup.sh run in this module.

    One tmp venv for all runs: the first ``uv sync`` materializes it from
    the warm host cache; every later run (B2-B4) sees an up-to-date venv and
    syncs in ~1s. Keeps total module runtime sane while still never touching
    the real repo .venv.
    """
    base = tmp_path_factory.mktemp("uv-shared")
    return {
        "UV_PROJECT_ENVIRONMENT": str(base / "venv"),
        "UV_PYTHON": _base_python(),  # never execute PATH python shims
        **_host_uv_dirs(),
    }


@pytest.fixture(scope="module")
def decline_run(
    tmp_path_factory: pytest.TempPathFactory, uv_pins: dict[str, str]
) -> SetupRun:
    """B1: ONE headless decline-flow run shared by all B1 assertions.

    Mirrors the ``clean_slate_home`` fixture shape (home is a subdirectory
    so side artifacts stay outside the fake home) at module scope — the
    function-scoped fixture cannot back a shared run.
    """
    work = tmp_path_factory.mktemp("b1-decline")
    home = work / "home"
    home.mkdir()
    env = isolated_env(home, **uv_pins)
    return _run_setup(env, home=home, work=work, stdin="n\n", ollama_shim=True)


# ---------------------------------------------------------------------------
# B1 — e2e-setup-headless-decline-completes
# ---------------------------------------------------------------------------


class TestHeadlessDeclineCompletes:
    def test_exit_zero_and_progress_markers_in_order(self, decline_run: SetupRun) -> None:
        """set-e-safe end-to-end completion, with stage markers localizing
        any failure: uv stage before layout stage before install stage."""
        assert decline_run.rc == 0, decline_run.combined
        out = decline_run.stdout
        deps = out.find("Dependencies installed.")
        layout = out.find("Runtime layout ready.")
        assert deps != -1, f"uv stage marker missing:\n{decline_run.combined}"
        assert layout != -1, f"layout stage marker missing:\n{decline_run.combined}"
        assert deps < layout, "'Dependencies installed.' must precede 'Runtime layout ready.'"
        assert "[install_hooks] Restart Claude Code" in out
        assert any(ln == "[setup] Done." for ln in _clean(out).splitlines())

    def test_ollama_branch_is_warn_and_continue(self, decline_run: SetupRun) -> None:
        """The curl-exits-1 function shim forces the daemon-unreachable
        warn-and-continue branch (setup.sh:165-172) deterministically on
        every host — the pull (:175) is unreachable, so no network."""
        combined = decline_run.combined
        assert "Ollama daemon not reachable" in combined
        assert "After Ollama is running, re-run this script" in combined
        assert "Embedding model pulled." not in combined
        # Judged conditional mirror: if the prompt branch fired anyway
        # (no-shim refactor), it must have warn-and-continued, not aborted.
        if "Ollama not found" in combined:
            assert "Ollama still missing" in combined

    def test_runtime_layout_created(self, decline_run: SetupRun) -> None:
        """setup.sh:188-191 — including the knowledge-base dirs the MCP
        server never auto-creates (product bug #9's install-time half)."""
        bm_home = decline_run.home / ".better-memory"
        assert (bm_home / "spool").is_dir()
        for sub in ("standards", "languages", "projects"):
            assert (bm_home / "knowledge-base" / sub).is_dir(), sub

    def test_claude_json_command_is_win_path_correct(self, decline_run: SetupRun) -> None:
        """The regression this file exists for: win_path (setup.sh:42-54)
        must hand Claude Code a native Windows path, never MSYS /c/... form.
        """
        config = json.loads((decline_run.home / ".claude.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["better-memory"]
        command = server["command"]
        ctx = f"command={command!r} (bash {decline_run.bash_version})"
        assert Path(command) == VENV_PY, ctx
        if _WIN:
            # Uppercase drive + backslashes only. Path() equality above
            # already tolerates the bash<5.2 patsub_replacement double
            # backslash form (Path collapses repeated separators); these
            # pins reject the MSYS and mixed-separator forms outright.
            assert re.match(r"^[A-Z]:", command), ctx
            assert "/" not in command, ctx
            assert "\\" in command, ctx
        assert server["args"] == ["-m", "better_memory.mcp"]
        assert Path(server["env"]["BETTER_MEMORY_HOME"]) == decline_run.home / ".better-memory"

    def test_settings_hooks_py_pyw_split(self, decline_run: SetupRun) -> None:
        """Exactly 5 managed hook entries; needs_stdout hooks keep python.exe
        (pythonw silently nulls stdout -> no additionalContext), background
        hooks get pythonw.exe. The split must survive bash -> argparse."""
        settings = json.loads(
            (decline_run.home / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        entries: dict[tuple[str, str], str] = {}
        matchers: dict[tuple[str, str], str | None] = {}
        for event, groups in settings["hooks"].items():
            for group in groups:
                for hook in group.get("hooks", []):
                    command = hook.get("command", "")
                    if "better_memory.hooks." not in command:
                        continue
                    parsed = re.fullmatch(r'"([^"]+)" -m ([\w.]+)', command)
                    assert parsed, f"unquoted interpreter or format drift: {command!r}"
                    key = (event, parsed.group(2))
                    assert key not in entries, f"duplicate hook entry {key}"
                    entries[key] = parsed.group(1)
                    matchers[key] = group.get("matcher")

        expected_interp = {
            ("SessionStart", "better_memory.hooks.session_bootstrap"): VENV_PY,
            ("PostToolUse", "better_memory.hooks.observer"): VENV_PYW,
            # Sync + stdout-attached: the rating sweep's block payload only
            # lands from a blocking hook. See install_hooks.py.
            ("Stop", "better_memory.hooks.session_close"): VENV_PY,
            ("UserPromptSubmit", "better_memory.hooks.contextual_inject"): VENV_PY,
            ("PreToolUse", "better_memory.hooks.contextual_inject"): VENV_PY,
        }
        assert set(entries) == set(expected_interp)  # exactly the 5 managed entries
        for key, interp in entries.items():
            assert Path(interp) == expected_interp[key], (key, interp)
        assert matchers[("PostToolUse", "better_memory.hooks.observer")] == "Write|Edit|Bash"
        # Unscoped (matcher None -> no `matcher` key in the group), mirroring
        # how SessionStart/Stop assert: the PreToolUse latch in the hook
        # (SeenStore.pretool_fired/mark_pretool_fired) makes an all-tools
        # matcher cheap -- only the first PreToolUse event per session does
        # real work.
        assert (
            matchers[("PreToolUse", "better_memory.hooks.contextual_inject")] is None
        )

    def test_all_writes_landed_under_tmp(self, decline_run: SetupRun) -> None:
        """Containment: the fake home holds exactly the install products, the
        working dir caught no stray relative writes, and (asserted inside
        _run_setup on every run) the real repo .venv is untouched."""
        top = {p.name for p in decline_run.home.iterdir()}
        required = {".better-memory", ".claude", ".claude.json"}
        assert required <= top, top
        # Host-toolchain noise under the redirected profile is tolerated —
        # e.g. Windows Python 3.14's install manager regenerates Start Menu
        # shortcuts under a fresh USERPROFILE (observed empirically). It is
        # still under tmp, which is the containment contract. But nothing
        # product-shaped may appear beyond the three install targets.
        extras = top - required
        leaks = {e for e in extras if "claude" in e.lower() or "memory" in e.lower()}
        assert not leaks, f"unexpected product artifacts in fake home: {leaks}"
        # setup.sh/install_hooks must not create any database (A1's zero-DB
        # pin, re-checked here because this run went through the full script).
        assert not list(decline_run.home.rglob("*.db"))
        bm = {p.name for p in (decline_run.home / ".better-memory").iterdir()}
        # Fresh slate: no pre-existing configs, so no install-backups/.
        assert bm == {"spool", "knowledge-base"}, bm
        # Stray-relative-write canary: setup.sh must not write product
        # artifacts relative to the invoker's cwd. Same host-noise filter:
        # pymanager also materializes a Python/bin shim dir into cwd when
        # LOCALAPPDATA is absent from the env (observed empirically).
        cwd_names = {p.name for p in decline_run.cwd.iterdir()}
        cwd_leaks = {
            n for n in cwd_names if "claude" in n.lower() or "memory" in n.lower()
        }
        assert not cwd_leaks, f"setup.sh wrote relative to cwd: {cwd_leaks}"
        assert not list(decline_run.cwd.rglob("*.db"))
        assert not list(decline_run.cwd.rglob("*.json"))


# ---------------------------------------------------------------------------
# B2 — e2e-setup-eof-aborts-all-or-nothing
# ---------------------------------------------------------------------------

#: Replicates setup.sh:124-127's two ollama detection paths exactly — the
#: hardcoded fallback uses the *real* whoami and ignores HOME redirection,
#: so it must be probed, not assumed.
_OLLAMA_PROBE = """
if command -v ollama >/dev/null 2>&1; then echo bm-e2e-ollama=found; exit 0; fi
if [[ -x "/c/Users/$(whoami)/AppData/Local/Programs/Ollama/ollama.exe" ]]; then
    echo bm-e2e-ollama=found; exit 0
fi
echo bm-e2e-ollama=absent
"""


def _ollama_free_path() -> str:
    """The outer PATH minus any segment mentioning ollama."""
    segments = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(s for s in segments if "ollama" not in s.lower())


class TestEofAbortsAllOrNothing:
    def test_eof_abort_is_all_or_nothing(
        self, tmp_path: Path, uv_pins: dict[str, str]
    ) -> None:
        """Product bug #11 pinned: closed stdin dies at the Ollama read -rp
        (setup.sh:134/141/148) under set -euo pipefail, BEFORE the layout
        stage (:188) and install_hooks (:199). Flips red if the stages are
        reordered ('do the important stuff first') or the prompt gains an
        EOF-safe default — both are deliberate contract changes.

        Expected to execute only on ollama-free hosts (CI images); self-skips
        on dev machines with Ollama installed, e.g. this repo's primary one.
        """
        home = tmp_path / "home"
        home.mkdir()
        env = isolated_env(home, PATH=_ollama_free_path(), **uv_pins)

        assert BASH is not None
        probe = subprocess.run(  # noqa: S603
            [BASH, "-c", _OLLAMA_PROBE], env=env, capture_output=True, text=True, timeout=60
        )
        if "bm-e2e-ollama=found" in probe.stdout:
            pytest.skip(
                "host ollama detectable via setup.sh's probes (PATH or the "
                "hardcoded whoami path) — the prompt/abort branch is "
                "unreachable here; this test runs on ollama-free CI"
            )
        assert "bm-e2e-ollama=absent" in probe.stdout, probe.stderr

        result = _run_setup(env, home=home, work=tmp_path, stdin=None, ollama_shim=False)

        assert result.rc == 1, result.combined
        # Positive-progress markers: a death in the python/uv stages would
        # also exit 1 with no config writes — these pin WHERE it died.
        assert "Dependencies installed." in result.stdout, result.combined
        assert "Ollama not found" in result.stderr, result.combined
        # All-or-nothing: nothing was written before the abort.
        assert not (home / ".claude.json").exists()
        assert not (home / ".claude" / "settings.json").exists()
        assert not (home / ".better-memory" / "spool").exists()
        assert "Runtime layout ready" not in result.combined
        assert "[install_hooks]" not in result.combined


# ---------------------------------------------------------------------------
# B3 — e2e-setup-default-home-derivation
# ---------------------------------------------------------------------------


class TestDefaultHomeDerivation:
    def test_default_home_derived_and_propagated(
        self, tmp_path: Path, uv_pins: dict[str, str]
    ) -> None:
        """BETTER_MEMORY_HOME deliberately ABSENT: the ${VAR:-default}
        fallback (setup.sh:37-38) must derive $HOME/.better-memory and plumb
        it via --home into the mcpServers env block and the backup location.
        Invisible while every other test exports the var — this is the true
        brand-new-user path.

        A trivial pre-seeded .claude.json makes run 1 produce a backup, so
        the backup-location assertion needs no second uv-bearing run.
        """
        home = tmp_path / "home"
        home.mkdir()
        seeded = json.dumps({"theme": "dark"}, indent=2) + "\n"
        (home / ".claude.json").write_text(seeded, encoding="utf-8")

        env = isolated_env(home, BETTER_MEMORY_HOME=None, **uv_pins)
        assert not any(k.upper() == "BETTER_MEMORY_HOME" for k in env)
        result = _run_setup(env, home=home, work=tmp_path, stdin="n\n", ollama_shim=True)

        assert result.rc == 0, result.combined
        bm_home = home / ".better-memory"
        assert (bm_home / "spool").is_dir()
        for sub in ("standards", "languages", "projects"):
            assert (bm_home / "knowledge-base" / sub).is_dir(), sub

        config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        assert config["theme"] == "dark"  # foreign top-level key preserved
        value = config["mcpServers"]["better-memory"]["env"]["BETTER_MEMORY_HOME"]
        # Platform-correct rendering: bash hands --home through MSYS
        # argument conversion, so compare as paths, not strings.
        assert Path(value) == bm_home, f"env BETTER_MEMORY_HOME={value!r}"
        if _WIN:
            assert not value.startswith("/"), f"MSYS-form path leaked: {value!r}"

        backups = sorted((bm_home / "install-backups").iterdir())
        assert [_backup_kind(p) for p in backups] == [".claude.json"]
        assert backups[0].read_text(encoding="utf-8") == seeded


def _backup_kind(path: Path) -> str:
    """'.claude.json.20260712-093000.bak' -> '.claude.json' (name format pin)."""
    match = re.fullmatch(r"(?s)(.+)\.\d{8}-\d{6}\.bak", path.name)
    assert match, f"backup name format drift: {path.name}"
    return match.group(1)


# ---------------------------------------------------------------------------
# B4 — e2e-setup-install-hooks-failure-propagates
# ---------------------------------------------------------------------------


class TestInstallHooksFailurePropagates:
    def test_failure_surfaces_with_remediation_and_stage_order(
        self, tmp_path: Path, uv_pins: dict[str, str]
    ) -> None:
        """setup.sh:199-206's `|| { error ...; exit 1; }` must surface an
        install_hooks failure — an `|| true` or dropped subshell status would
        tell the user 'Done.' while their install silently failed. Also pins
        stage order (layout precedes install: spool EXISTS on failure) and
        validate-before-write through the bash wrapper (seed untouched, no
        backups, no settings.json)."""
        home = tmp_path / "home"
        home.mkdir()
        malformed = '{"mcpServers": {broken'
        (home / ".claude.json").write_text(malformed, encoding="utf-8")

        env = isolated_env(home, **uv_pins)
        result = _run_setup(env, home=home, work=tmp_path, stdin="n\n", ollama_shim=True)

        assert result.rc == 1, result.combined
        assert "install_hooks failed" in result.stderr
        assert "scripts/setup.sh aborting" in result.stderr
        # The underlying <path>:<lineno> diagnostic from install_hooks'
        # _load_or_empty must reach the user through the bash wrapper.
        assert re.search(r"\.claude\.json:1:", result.combined), result.combined
        assert "Fix the file then re-run" in result.combined
        assert not any(ln == "[setup] Done." for ln in result.lines())

        # Stage order pin: layout (setup.sh:188-191) ran before the failure.
        bm_home = home / ".better-memory"
        assert (bm_home / "spool").is_dir()
        # Validate-before-write held: nothing was modified or backed up.
        assert (home / ".claude.json").read_text(encoding="utf-8") == malformed
        assert not (home / ".claude" / "settings.json").exists()
        assert not (bm_home / "install-backups").exists()
        assert not list(home.glob("**/*.tmp"))
